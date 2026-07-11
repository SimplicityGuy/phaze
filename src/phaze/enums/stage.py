"""Pipeline stage / status enums + DB-free per-row status resolver + eligibility DAG (Phase 78, D-04).

Lives outside :mod:`phaze.models` (like :mod:`phaze.enums.execution`) so the Postgres-free
compute / file-server agent worker can import it WITHOUT transitively pulling in SQLAlchemy /
:mod:`phaze.database`. See Phase 26 D-03 (agent import boundary) and Phase 78 D-04 (the two-module
split: this DB-free half is the CONTRACT the Wave-2 SQL twin ``services/stage_status.py`` is locked
against by the DERIV-04 equivalence test).

Hard constraint (T-78-01, enforced by ``tests/shared/test_stage_resolver.py``): this module imports
ONLY the stdlib — NO ``phaze.models`` / ``phaze.database`` / ``sqlalchemy``. ``resolve_status`` and
``eligible`` are pure functions over plain scalars owned by the caller.

Per-stage semantics (locked in 78-CONTEXT.md):
- DERIV-02: precedence ladder ``in_flight ≻ done ≻ failed ≻ not_started`` (the SAQ ledger wins).
- DERIV-03: ``done(analyze)`` requires ``analysis_completed_at IS NOT NULL`` — a partial in-flight
  row upserted at analysis START has ``completed_at`` NULL and is NOT done.
- DERIV-05: ``done(fingerprint)`` is a 1:N aggregation — one ``success``/``completed`` engine wins
  over a sibling ``failed`` engine.
- D-03: ``done(metadata)`` requires a row present AND ``failed_at IS NULL`` — a failure-only row
  derives FAILED, not DONE.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Mapping


class Stage(enum.StrEnum):
    """The seven pipeline stages whose per-file status is derived from output rows."""

    METADATA = "metadata"
    ANALYZE = "analyze"
    FINGERPRINT = "fingerprint"
    TRACKLIST = "tracklist"
    PROPOSE = "propose"
    REVIEW = "review"
    APPLY = "apply"


class Status(enum.StrEnum):
    """The 5-way derived per-stage status (precedence ``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``).

    ``SKIPPED`` (D-08) is the reported bucket for a force-skipped enrich stage: a ``stage_skip`` marker
    row exists for the ``(file, stage)`` pair. It is ordered ``done ≻ skipped ≻ failed`` — a completed
    stage still reads DONE, but a skipped stage outranks a lingering failure (the writer is additive and
    never clears ``failed_at``, so the ordering — not the writer — decides). Only the three enrich stages
    ever carry it; the downstream stages stay 4-way.
    """

    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


# DERIV-05: a fingerprint engine row in either of these states counts the stage as done. Mirrors the
# Phase-59 WR-02 spelling and the ``ix_fprint_success`` partial index (``= ANY(ARRAY['success','completed'])``).
_DONE_FP: frozenset[str] = frozenset({"success", "completed"})


# ELIG-01/02: upstream conjuncts per stage. Enrich stages (metadata/analyze/fingerprint) have NO
# upstream — a discovered file is simultaneously eligible for all three in any order.
ELIGIBILITY_DAG: dict[Stage, tuple[Stage, ...]] = {
    Stage.METADATA: (),
    Stage.ANALYZE: (),
    Stage.FINGERPRINT: (),
    Stage.TRACKLIST: (Stage.FINGERPRINT,),
    Stage.PROPOSE: (Stage.METADATA, Stage.ANALYZE),
    Stage.REVIEW: (Stage.PROPOSE,),
    Stage.APPLY: (Stage.REVIEW,),
}


# D-15: two ORTHOGONAL axes for the three enrich stages — conflating them is a live trap.
#
# FAILURE_IS_TERMINAL answers "does a FAILED stage count as domain-complete, so recovery must NOT
# re-run it?" (used by ``domain_completed`` + the ``domain_completed_clause`` SQL twin). A terminal
# failure is the durable "we tried and it is un-processable" fact the 44.5K over-enqueue guard rests
# on — analyze + metadata failures are terminal (retry is operator-driven), a fingerprint failure is
# NOT (it auto-retries).
#
# ELIGIBLE_AFTER_FAILURE answers "is a FAILED stage still eligible to auto-retry?" (consumed by
# ``eligible``). It is the exact negation of the analyze carve-out formerly inlined in ``eligible`` —
# ANALYZE is False (ELIG-03 terminal, manual retry only), METADATA/FINGERPRINT are True (ELIG-04).
#
# Encoding FAIL-01 (analyze terminal) and FAIL-04 (fingerprint auto-retryable) as two named tables
# stops the recovery guard and the fingerprint retry from being coincidences two readers (the pure
# ``eligible`` path and the ``domain_completed`` recovery path) must independently remember.
FAILURE_IS_TERMINAL: dict[Stage, bool] = {Stage.ANALYZE: True, Stage.METADATA: True, Stage.FINGERPRINT: False}
ELIGIBLE_AFTER_FAILURE: dict[Stage, bool] = {Stage.ANALYZE: False, Stage.METADATA: True, Stage.FINGERPRINT: True}


# --------------------------------------------------------------------------------------------------
# Per-stage resolver twins — each applies the DERIV-02 precedence ladder with ``inflight`` first.
# --------------------------------------------------------------------------------------------------
def _analyze_status(*, completed_at: Any, failed_at: Any, inflight: bool, skipped: bool = False) -> Status:
    """analyze: done iff ``analysis_completed_at IS NOT NULL`` (DERIV-03 — completed_at NULL != done)."""
    if inflight:
        return Status.IN_FLIGHT
    if completed_at is not None:
        return Status.DONE
    if skipped:  # D-08: after done, before failed (load-bearing precedence — Pitfall 2)
        return Status.SKIPPED
    if failed_at is not None:
        return Status.FAILED
    return Status.NOT_STARTED


def _metadata_status(*, row_present: bool, failed_at: Any, inflight: bool, skipped: bool = False) -> Status:
    """metadata: done requires a row present AND ``failed_at IS NULL`` (D-03 — failure-only row = FAILED)."""
    if inflight:
        return Status.IN_FLIGHT
    if row_present and failed_at is None:
        return Status.DONE
    if skipped:  # D-08: after done, before failed (load-bearing precedence — Pitfall 2)
        return Status.SKIPPED
    if failed_at is not None:
        return Status.FAILED
    return Status.NOT_STARTED


def _fingerprint_status(*, engine_statuses: list[str], inflight: bool, skipped: bool = False) -> Status:
    """fingerprint: 1:N aggregation — one ``success``/``completed`` engine wins over a failed sibling (DERIV-05)."""
    if inflight:
        return Status.IN_FLIGHT
    if any(s in _DONE_FP for s in engine_statuses):
        return Status.DONE
    if skipped:  # D-08: after done, before failed (load-bearing precedence — Pitfall 2)
        return Status.SKIPPED
    if any(s == "failed" for s in engine_statuses):
        return Status.FAILED
    return Status.NOT_STARTED


def _presence_status(*, present: bool, failed: bool, inflight: bool) -> Status:
    """Downstream presence twin — done iff an output row exists; failed iff a failure marker exists."""
    if inflight:
        return Status.IN_FLIGHT
    if present:
        return Status.DONE
    if failed:
        return Status.FAILED
    return Status.NOT_STARTED


def _tracklist_status(*, row_present: bool, failed: bool, inflight: bool) -> Status:
    return _presence_status(present=row_present, failed=failed, inflight=inflight)


def _propose_status(*, row_present: bool, failed: bool, inflight: bool) -> Status:
    return _presence_status(present=row_present, failed=failed, inflight=inflight)


def _review_status(*, row_present: bool, failed: bool, inflight: bool) -> Status:
    return _presence_status(present=row_present, failed=failed, inflight=inflight)


def _apply_status(*, row_present: bool, failed: bool, inflight: bool) -> Status:
    return _presence_status(present=row_present, failed=failed, inflight=inflight)


def resolve_status(stage: Stage, scalars: Mapping[str, Any]) -> Status:
    """Resolve the 4-way :class:`Status` for ``stage`` from plain scalars (DB-free).

    ``scalars`` carries only the keys the stage's twin needs (all optional, safe defaults):
    - analyze: ``completed_at``, ``failed_at``, ``inflight``
    - metadata: ``row_present``, ``failed_at``, ``inflight``
    - fingerprint: ``engine_statuses`` (list[str]), ``inflight``
    - downstream (tracklist/propose/review/apply): ``row_present``, ``failed``, ``inflight``

    Applies the DERIV-02 precedence ladder ``in_flight ≻ done ≻ skipped ≻ failed ≻ not_started``
    (``inflight`` from the SAQ scheduling ledger always wins; ``skipped`` from the ``stage_skip`` marker,
    D-08, sits just under ``done``). Never touches a database. The ``skipped`` scalar is passed ONLY to
    the three enrich branches — the downstream stages have no force-skip marker and ignore it.
    """
    inflight = bool(scalars.get("inflight", False))
    skipped = bool(scalars.get("skipped", False))
    if stage is Stage.ANALYZE:
        return _analyze_status(completed_at=scalars.get("completed_at"), failed_at=scalars.get("failed_at"), inflight=inflight, skipped=skipped)
    if stage is Stage.METADATA:
        return _metadata_status(
            row_present=bool(scalars.get("row_present", False)), failed_at=scalars.get("failed_at"), inflight=inflight, skipped=skipped
        )
    if stage is Stage.FINGERPRINT:
        return _fingerprint_status(engine_statuses=list(scalars.get("engine_statuses", [])), inflight=inflight, skipped=skipped)
    row_present = bool(scalars.get("row_present", False))
    failed = bool(scalars.get("failed", False))
    if stage is Stage.TRACKLIST:
        return _tracklist_status(row_present=row_present, failed=failed, inflight=inflight)
    if stage is Stage.PROPOSE:
        return _propose_status(row_present=row_present, failed=failed, inflight=inflight)
    if stage is Stage.REVIEW:
        return _review_status(row_present=row_present, failed=failed, inflight=inflight)
    if stage is Stage.APPLY:
        return _apply_status(row_present=row_present, failed=failed, inflight=inflight)
    raise ValueError(f"unknown stage: {stage!r}")  # pragma: no cover - exhaustive dispatch above


def domain_completed(status_map: Mapping[Stage, Status], stage: Stage) -> bool:
    """Pure predicate: has ``stage`` reached a DOMAIN-COMPLETE state that recovery must NOT re-run?

    ``DONE`` and ``SKIPPED`` (D-08 force-skip marker) are always domain-complete; a ``FAILED`` stage is
    domain-complete ONLY when the failure is
    terminal (:data:`FAILURE_IS_TERMINAL`) — i.e. analyze / metadata (retry is operator-driven) but NOT
    fingerprint (which auto-retries). This is the DB-free twin of
    :func:`phaze.services.stage_status.domain_completed_clause`; the two are drift-locked by the Phase 78
    equivalence test (``tests/integration/test_stage_status_equivalence.py``). It is the durable "we tried
    and it is un-processable" fact the 44.5K over-enqueue guard rests on (D-17).

    Defined ONLY for the three enrich stages (the keys of :data:`FAILURE_IS_TERMINAL`). The downstream
    stages have NO domain predicate by design — ``phaze.tasks.reenqueue`` classifies them "live-keys-only"
    because their terminal ack clears the ledger row on every outcome. Raising here (rather than defaulting)
    keeps that exclusion explicit and total: a caller that reaches for terminality on ``propose``/``review``
    is asking a question this layer has deliberately not answered. The SQL twin raises identically.
    """
    if stage not in FAILURE_IS_TERMINAL:
        # `getattr(..., "value", stage)`: a raw-`str` stage hashes equal to its StrEnum member, so it can
        # reach here; `.value` would then AttributeError instead of raising the intended ValueError.
        got = getattr(stage, "value", stage)
        raise ValueError(f"domain_completed is defined only for the enrich stages {sorted(s.value for s in FAILURE_IS_TERMINAL)}; got {got!r}")
    # WR-03: compare by VALUE, not identity. `Status` is a StrEnum, so a status_map that came back
    # through a SQL/JSON round-trip carries raw `str`s -- and `stage_status_case` emits exactly
    # `Status.X.value` strings. Under `is`, a raw "done" matched nothing and this returned False for a
    # genuinely-complete stage. Equality makes both spellings agree.
    st = Status(status_map.get(stage, Status.NOT_STARTED))
    return st in (Status.DONE, Status.SKIPPED) or (st == Status.FAILED and FAILURE_IS_TERMINAL[stage])


def eligible(status_map: Mapping[Stage, Status], stage: Stage, *, has_approved_proposal: bool = False) -> bool:
    """Pure predicate: is ``stage`` eligible to run given the derived per-stage ``status_map``?

    Dispatched per stage — the enrich stages do NOT share one uniform rule (D-04 boundary; no DB):

    - ``METADATA`` / ``FINGERPRINT`` / ``ANALYZE`` (the three enrich stages): eligible iff status NOT in
      ``(DONE, IN_FLIGHT, SKIPPED)`` AND (status is not ``FAILED`` OR :data:`ELIGIBLE_AFTER_FAILURE`\\ ``[stage]``).
      A ``SKIPPED`` stage (D-08 force-skip marker) is never eligible — it leaves the pending set.
      The per-stage failure axis lives in the :data:`ELIGIBLE_AFTER_FAILURE` table, not inlined here
      (D-15/D-16). This yields IDENTICAL truth to the prior per-branch dispatch: METADATA/FINGERPRINT
      (``True``) → eligible when ``NOT_STARTED`` OR ``FAILED`` (ELIG-01 independence; ELIG-04 — a FAILED
      fingerprint is NOT terminal and stays eligible for auto-retry); ANALYZE (``False``, the ONLY enrich
      carve-out) → eligible iff ``NOT_STARTED`` — a FAILED analyze is excluded (ELIG-03 terminal, retry is
      manual-only; the 44.5K over-enqueue guard). Mirrors
      ``phaze.tasks.reenqueue._select_done_analyze_ids`` which treats ANALYSIS_FAILED as analyze-DONE so
      ``recover_orphaned_work`` never auto-loops an un-analyzable file (do NOT import it — kept DB-free).
    - ``APPLY``: eligible iff ``has_approved_proposal`` AND apply not already ``DONE`` — apply is gated on
      an APPROVED proposal existing (ELIG-02), NOT on bare ``done(review)`` (which only means a proposal
      exists). ``has_approved_proposal`` is the approval flag the caller supplies (the SQL twin filters
      ``proposals.status = 'approved'``).
    - ``TRACKLIST`` / ``PROPOSE`` / ``REVIEW``: every upstream in ``ELIGIBILITY_DAG[stage]`` must be
      ``DONE`` AND the stage itself not already ``DONE`` (ELIG-02 upstream conjuncts).
    """
    if stage in (Stage.METADATA, Stage.FINGERPRINT, Stage.ANALYZE):
        # WR-03: coerce, then compare by VALUE. `Status` is a StrEnum, so a raw-`str` status_map (a SQL
        # or JSON round-trip; `stage_status_case` emits `Status.X.value`) used to slip past
        # `status is not Status.FAILED` -- identity fails for an equal string -- and reported a FAILED
        # analyze as ELIGIBLE. That is the 44.5K over-enqueue class ELIG-03 exists to guard.
        status = Status(status_map.get(stage, Status.NOT_STARTED))
        return status not in (Status.DONE, Status.IN_FLIGHT, Status.SKIPPED) and (status != Status.FAILED or ELIGIBLE_AFTER_FAILURE[stage])
    if stage is Stage.APPLY:
        return has_approved_proposal and status_map.get(Stage.APPLY, Status.NOT_STARTED) != Status.DONE
    upstream_done = all(status_map.get(u, Status.NOT_STARTED) == Status.DONE for u in ELIGIBILITY_DAG[stage])
    return upstream_done and status_map.get(stage, Status.NOT_STARTED) != Status.DONE
