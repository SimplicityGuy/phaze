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
    """The 4-way derived per-stage status (precedence ``in_flight ≻ done ≻ failed ≻ not_started``)."""

    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    DONE = "done"
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


# --------------------------------------------------------------------------------------------------
# Per-stage resolver twins — each applies the DERIV-02 precedence ladder with ``inflight`` first.
# --------------------------------------------------------------------------------------------------
def _analyze_status(*, completed_at: Any, failed_at: Any, inflight: bool) -> Status:
    """analyze: done iff ``analysis_completed_at IS NOT NULL`` (DERIV-03 — completed_at NULL != done)."""
    if inflight:
        return Status.IN_FLIGHT
    if completed_at is not None:
        return Status.DONE
    if failed_at is not None:
        return Status.FAILED
    return Status.NOT_STARTED


def _metadata_status(*, row_present: bool, failed_at: Any, inflight: bool) -> Status:
    """metadata: done requires a row present AND ``failed_at IS NULL`` (D-03 — failure-only row = FAILED)."""
    if inflight:
        return Status.IN_FLIGHT
    if row_present and failed_at is None:
        return Status.DONE
    if failed_at is not None:
        return Status.FAILED
    return Status.NOT_STARTED


def _fingerprint_status(*, engine_statuses: list[str], inflight: bool) -> Status:
    """fingerprint: 1:N aggregation — one ``success``/``completed`` engine wins over a failed sibling (DERIV-05)."""
    if inflight:
        return Status.IN_FLIGHT
    if any(s in _DONE_FP for s in engine_statuses):
        return Status.DONE
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

    Applies the DERIV-02 precedence ladder ``in_flight ≻ done ≻ failed ≻ not_started`` (``inflight``
    from the SAQ scheduling ledger always wins). Never touches a database.
    """
    inflight = bool(scalars.get("inflight", False))
    if stage is Stage.ANALYZE:
        return _analyze_status(completed_at=scalars.get("completed_at"), failed_at=scalars.get("failed_at"), inflight=inflight)
    if stage is Stage.METADATA:
        return _metadata_status(row_present=bool(scalars.get("row_present", False)), failed_at=scalars.get("failed_at"), inflight=inflight)
    if stage is Stage.FINGERPRINT:
        return _fingerprint_status(engine_statuses=list(scalars.get("engine_statuses", [])), inflight=inflight)
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
