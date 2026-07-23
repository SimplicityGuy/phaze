"""Control-side restart / queue-loss recovery for the WHOLE pipeline.

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). These tasks need both
PostgreSQL via ``ctx["async_session"]`` AND the per-agent enqueuer via
``ctx["task_router"]`` (plus the controller queue ``ctx["queue"]``) -- wired in
``phaze.tasks.controller.startup``. The agent worker is deliberately Postgres-free (the
import-boundary test ``tests/shared/core/test_task_split.py`` enforces this), so this module MUST
NEVER be imported or registered by ``phaze.tasks.agent_worker`` or anything under
``phaze.tasks._shared``. Register it ONLY in ``phaze.tasks.controller``.

THE DURABILITY REFRAME (Phase 42, 42-RESEARCH §Q2 -- READ THIS BEFORE "RESTORING" ANYTHING):
Phase 36 migrated the SAQ broker from Redis to Postgres (``saq_jobs`` table, ``PostgresQueue``).
Queued and active jobs are now DURABLE across a controller restart -- SAQ re-dequeues the
surviving ``saq_jobs`` rows itself, and reclaims timed-out ``active`` jobs on its own. A genuine
"queue-loss" is now the rare, DETECTABLE asymmetry "``saq_jobs`` has zero queued/active rows while
the durable scheduling ledger still records scheduled work" (a truncate / restore-from-backup /
fresh migration). Steady state produces ZERO automatic enqueues -- DO NOT re-introduce a
steady-state auto-advance cron or the deleted ``reenqueue_discovered`` producer. (Phase 80 left this
durability contract untouched; it only changed HOW ``domain-completed`` is derived -- see below.)

THE PHASE-45 LEDGER REFRAME (45-CONTEXT, the operator spec -- READ THIS BEFORE TOUCHING RECOVERY):
Pre-Phase-45 recovery derived its work from the ``services/pipeline.py`` COMPLEMENT-OF-DONE
pending-set queries (``get_files_by_state(DISCOVERED)``, ``get_untracked_files``, ...): "everything
that has not finished this stage." There was NO record that a stage was ever SCHEDULED for an item,
so clicking "Recover" swept in ~11,400 never-scheduled ``DISCOVERED`` files and detonated the queue
to ~44,500 jobs (the 2026-06-18 incident). The operator principle: **recovery must only re-queue
work that was previously scheduled and then lost**; a never-scheduled file is not yet orphaned.

:func:`recover_orphaned_work` now drives off the DURABLE scheduling ledger (Plan 01:
``scheduling_ledger`` table, written at the single ``before_enqueue`` chokepoint, cleared on every
terminal outcome). It re-enqueues exactly::

    orphaned = (ledger rows) MINUS (live saq_jobs keys) MINUS (domain-completed)

replaying each orphaned row's STORED payload through the SAME keyed producer that originally
enqueued it (``ctx["queue"].enqueue`` for controller rows; the active agent's per-agent queue for
agent rows). A never-scheduled ``DISCOVERED`` file has NO ledger row, so the incident sweep CANNOT
recur. ``force=True`` flips to "reconcile the ledger now", bypassing ONLY the no-op DETECT gate --
never the per-item deterministic-key dedup, so a forced reconcile over a live queue stays idempotent
(no doubling, Phase-32 class). Phase 80 (READ-03) preserved this ledger-recovery contract verbatim
while cutting the ``domain-completed`` exclusion over to the derived predicate layer (see the third
reframe below): the WORK set is still, exactly, ``ledger MINUS live MINUS domain-completed``.

THE PER-STAGE DOMAIN-COMPLETED PREDICATE (the SECONDARY net for Plan 02's residual gap):
Phase 80 (READ-03) CUT this predicate over from ``FileRecord.state`` reads to the Phase-78/81
single-source derivation layer (``services/stage_status.py``): recovery now derives "done" DIRECTLY
from the per-stage output tables via the LOCKED ``done_clause`` / ``domain_completed_clause`` builders,
with ZERO ``FileRecord.state`` reads. This lands AFTER migration ``036`` backfilled
``analysis.analysis_completed_at`` (so ``done(analyze)`` reads a complete corpus) and BEFORE Phase 82
redefines "pending", closing the double-negation (D-05): the enrich branches flip from
``absent-from-pending`` (which would silently become ``done OR in_flight`` once pending = ``NOT done AND
NOT in_flight``) to a DIRECT ``in done-set`` test. The exclusion stays EXPLICIT and TOTAL per stage
(asserted in test_recovery.py):

- predicate-covered (:data:`_DOMAIN_COMPLETED_STAGES`): ``process_file`` (analyze; done via
  ``domain_completed_clause(ANALYZE)`` == done OR terminal-failed -- FAILURE_IS_TERMINAL[analyze],
  so an un-analyzable file is NEVER auto-re-driven), ``extract_file_metadata`` (done via
  ``domain_completed_clause(METADATA)`` with the D-10 ``enqueued_at <= failed_at`` gate applied at the
  call site), ``fingerprint_file`` (done via ``done_clause(FINGERPRINT) OR skipped_clause(FINGERPRINT)``
  -- a FAILED fingerprint still auto-retries, the intentional analyze/fingerprint asymmetry D-01 encodes,
  but an operator-force-SKIPPED fingerprint is excluded so recovery never re-drives a skipped stage
  [phase-87 behavior 5]), and ``push_file`` (done
  via ``cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)`` -- D-07, no backend-kind
  resolution needed because a ``push_file`` ledger row implies compute).
- live-keys-only (everything else): ``scan_live_set`` (Plan 02's terminal ack clears its ledger row
  on EVERY outcome, so any surviving row is genuinely orphaned -- no domain predicate) plus the four
  controller stages (``generate_proposals`` / ``search_tracklist`` / ``scrape_and_store_tracklist``
  / ``match_tracklist_to_discogs``; Plan 01's after_process clears them on every terminal status).

All done-sets are LEDGER-SCOPED (D-06): recovery only ever asks about files that appear in the ledger,
so every done-set query binds the ledger's file-ids as a SINGLE Postgres array (``= ANY(:ids)``), never
a bare ``.in_(fids)`` (the asyncpg 32767 param cap -- the ledger reached ~44.5K rows in the 2026-06-18
incident). Deriving "done" directly inverts the set-size characteristic (the pending sets were small;
the done set is most of a 200K corpus), which the ledger scope keeps at O(|ledger|).

Routing carries forward the Phase-32 pitfalls: agent rows route to the active agent's per-agent
queue via ``select_active_agent`` + ``ctx["task_router"].queue_for(agent.id)`` -- NEVER the
consumer-less controller queue (Pitfall 1); controller rows route to ``ctx["queue"]``. Zero live
agents (common right after a cold reboot; Pitfall 3) logs a warning and skips the agent-routed rows
instead of raising. The cached ``task_router`` is reused, never reconstructed per call (Pitfall 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
import json
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import ARRAY, Select, bindparam, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
import structlog

from phaze.config import get_settings
from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.backends import IN_FLIGHT
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, select_active_agent
from phaze.services.pipeline import count_inflight_jobs, get_live_job_keys
from phaze.services.scheduling_ledger import get_ledger_rows, insert_ledger_if_absent
from phaze.services.stage_status import domain_completed_clause, done_clause, skipped_clause
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS


if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.models.scheduling_ledger import SchedulingLedger


logger = structlog.get_logger(__name__)


# --- Phase 45: ledger-driven, gated, idempotent recovery producer -----------------------
#
# Recovery replays ``ledger MINUS live MINUS domain-completed``. A replay of any item still in
# saq_jobs dedups against its deterministic key (apply_deterministic_key, the single
# before_enqueue chokepoint) and returns None -> counted as skipped. SAFETY BACKSTOP
# (T-45-09): even if the live-key filter false-NEGATIVES (a stale read) and re-enqueues a
# still-live item, the deterministic-key dedup collapses it to a skipped no-op, so a forced
# reconcile can NEVER double the queue (the Phase-32 doubling class is closed). Every enqueue
# goes strictly through the keyed producers (never a raw random-key queue.enqueue).
#
# The FOUR agent stages whose ledger clear is NOT reliable on every terminal outcome get an
# explicit domain-completed predicate; the other five are live-keys-only (their clear IS
# reliable). The classification is TOTAL: predicate-covered XOR live-keys-only, asserted in a
# test against _KEY_BUILDERS so no stage is silently undefined (T-45-17).
_DOMAIN_COMPLETED_STAGES: frozenset[str] = frozenset(
    {
        "process_file",  # analyze: domain_completed_clause(ANALYZE) == done OR terminal-failed
        "extract_file_metadata",  # domain_completed_clause(METADATA) + the D-10 enqueued_at gate at the call site
        "fingerprint_file",  # done_clause(FINGERPRINT) OR skipped_clause -- failed auto-retries (D-01), force-skip excluded
        "push_file",  # D-07: cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)
    }
)
"""Keyed functions that carry a per-stage domain-completed predicate (the SECONDARY exclusion).

EVERY other keyed function (``scan_live_set`` + the four controller stages) is live-keys-only --
its ledger row is reliably cleared on every terminal outcome (scan via Plan 02's ack; controllers
via Plan 01's after_process), so any surviving row IS genuinely orphaned and needs no domain net.
Phase 80 (READ-03) cut every predicate over to the derived layer -- ``process_file`` /
``extract_file_metadata`` / ``fingerprint_file`` / ``push_file`` are now derived from the per-stage
output tables via the LOCKED ``done_clause`` / ``domain_completed_clause`` builders, with ZERO
``FileRecord.state`` reads (the old FileState-derived "done" is gone).
Kept in sync with ``deterministic_key._KEY_BUILDERS`` by a totality test in test_recovery.py.
"""


@dataclass(frozen=True)
class _DoneSets:
    """The ledger-scoped per-stage domain-completed derivation, computed ONCE per recovery run (D-06).

    Every set/dict is keyed by file-id STRING and scoped to the ledger's files only (``= ANY(:ids)``),
    so membership is O(1) in :func:`is_domain_completed` and the derivation is O(|ledger|), never
    O(200K corpus). The metadata cell is split into a domain-completed set + a ``failed_at`` map so the
    call site can apply the D-10 ``enqueued_at <= failed_at`` gate (which needs the per-row ledger
    timestamp) to the failed subset -- and a SEPARATE ``metadata_skipped`` set (phaze-3m5n) so the call
    site can short-circuit to domain-complete on force-skip membership BEFORE ever reaching the D-10
    gate. Without this separate cell, a file that is force-skipped AFTER a failure (the additive-only
    ``force_skip_stage`` contract, T-87-20, never clears ``failed_at``) still carries a non-NULL
    ``metadata_failed_at`` entry, so the D-10 gate would wrongly re-subject it to the
    ``enqueued_at <= failed_at`` comparison and re-drive an orphaned post-failure retry row that the
    operator explicitly terminated -- exactly the bug phaze-3m5n fixes.
    """

    analyze_done: set[str]  # domain_completed_clause(ANALYZE): done OR terminal-failed
    metadata_domain_completed: set[str]  # domain_completed_clause(METADATA): done OR skipped OR failed (D-10 gate refines the failed-only subset)
    metadata_failed_at: dict[str, datetime]  # failed_clause(METADATA) rows -> failed_at (D-10 gate)
    metadata_skipped: set[str]  # skipped_clause(METADATA) alone: force-skip is domain-complete UNCONDITIONALLY, never gated
    fingerprint_done: set[str]  # done_clause(FINGERPRINT) OR skipped_clause: failed auto-retries, force-skip excluded
    push_done: set[str]  # D-07: cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)


def _zero() -> dict[str, int]:
    """Return a fresh zero per-stage tally."""
    return {"reenqueued": 0, "skipped": 0}


def _ledger_fids(rows: Sequence[SchedulingLedger]) -> list[uuid.UUID]:
    """Extract the distinct, UUID-parseable natural file-ids from the run's ledger rows (D-06 scope).

    The done-set queries only ever ask about files that appear in the ledger, so this is the exact
    ``fids`` bound as the ``= ANY(array)`` scope. A row whose payload carries no ``file_id`` (or a
    non-UUID natural id -- e.g. a controller set-hash) is skipped: it can never be a per-file
    output-table match, so excluding it from the scope is a pure optimization with no behavior change.
    """
    out: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for row in rows:
        nid = _natural_id(row)
        if nid is None:
            continue
        try:
            fid = uuid.UUID(nid)
        except ValueError:
            continue
        if fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


def _fids_scope(fids: list[uuid.UUID], name: str) -> Any:
    """``FileRecord.id = ANY(:name)`` -- a SINGLE Postgres array bind, never a bare ``.in_(fids)``.

    D-06 / Landmine 5: a bare ``.in_(fids)`` expands to one bind per element and crashes asyncpg past
    the 32767-param wire cap (the ledger reached ~44.5K rows in the 2026-06-18 incident). Binding the
    whole id list as ONE ``uuid[]`` array param sidesteps the ceiling entirely while the Phase-77
    partial indexes still drive each per-stage probe. There is NO ``= ANY(array)`` idiom elsewhere in
    the codebase (RESEARCH §e); this is the phase's one genuinely new query pattern.
    """
    return FileRecord.id == func.any(bindparam(name, value=fids, type_=ARRAY(PGUUID(as_uuid=True))))


async def _build_done_sets(session: AsyncSession, fids: list[uuid.UUID]) -> _DoneSets:
    """Compute the ledger-scoped per-stage domain-completed derivation ONCE (D-05/D-06/D-07/D-10).

    Derives "done" DIRECTLY from the Phase-78/81 predicate layer -- ZERO ``FileRecord.state`` reads:

    - ``analyze_done``: ``domain_completed_clause(ANALYZE)`` (done OR terminal-failed -- an
      un-analyzable file stays terminal, FAILURE_IS_TERMINAL[analyze]).
    - ``metadata_domain_completed`` + ``metadata_failed_at`` + ``metadata_skipped``: ``domain_completed_clause(METADATA)``
      gives the done-OR-skipped-OR-failed set; the failed rows' ``failed_at`` is read alongside so the
      call site can REFINE ONLY the failed-only subset with the D-10 gate
      (``done OR skipped OR (failed AND enqueued_at <= failed_at)``) -- both twins stay ledger-agnostic.
      Deriving the set via ``domain_completed_clause`` (not a bare ``done_clause``) is what makes the
      D-11 lock bite: a ``~inflight_clause`` conjunct wrongly added to it would drop every
      failed-and-inflight candidate. ``metadata_skipped`` is read SEPARATELY (``skipped_clause(METADATA)``
      alone, not folded into the merged set) so the call site can test skip membership BEFORE the D-10
      gate -- a force-skipped file that ALSO carries a stale ``failed_at`` (the force-skip writer's
      additive-only contract, T-87-20, never clears it) must stay domain-complete UNCONDITIONALLY,
      never re-subjected to the failed-only ``enqueued_at`` comparison (phaze-3m5n).
    - ``fingerprint_done``: ``done_clause(FINGERPRINT) OR skipped_clause(FINGERPRINT)`` -- a FAILED
      fingerprint still auto-retries (it is NOT domain-complete: the analyze/fingerprint asymmetry D-01
      encodes, FAILURE_IS_TERMINAL[fingerprint] is False), but an operator-force-SKIPPED fingerprint is
      excluded so ``recover_orphaned_work`` never re-drives a stage the operator explicitly skipped
      (phase-87 behavior 5). NOT ``domain_completed_clause`` -- that would couple recovery to the
      terminality axis and obscure the FAIL-04 auto-retry intent (here it collapses to the same set).
    - ``push_done``: ``cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)`` (D-07). A
      ``push_file`` ledger row implies compute (``_enqueue_push_file`` is its only producer), so no
      backend-kind resolution is needed: SUCCEEDED covers the landed-but-not-yet-analyzed window and
      ``domain_completed(analyze)`` covers the onward advance; a SUBMITTED / AWAITING / no-row file is
      NOT push-done and re-drives.

    Every query keeps its own per-stage shape (one targeted probe per stage) so each Phase-77 partial
    index drives its own scan, and every probe is scoped to ``fids`` via a single ``= ANY(array)`` bind
    (:func:`_fids_scope`). An empty ledger short-circuits to empty sets (no pointless round-trips).
    """
    if not fids:
        return _DoneSets(set(), set(), {}, set(), set(), set())

    analyze_done = {str(fid) for fid in (await session.scalars(_select_done_analyze_ids(fids))).all()}
    metadata_domain_completed = {
        str(fid)
        for fid in (await session.scalars(select(FileRecord.id).where(_fids_scope(fids, "m_ids"), domain_completed_clause(Stage.METADATA)))).all()
    }
    metadata_failed_at = {
        str(file_id): failed_at
        for file_id, failed_at in (
            await session.execute(
                select(FileMetadata.file_id, FileMetadata.failed_at).where(
                    FileMetadata.file_id == func.any(bindparam("mf_ids", value=fids, type_=ARRAY(PGUUID(as_uuid=True)))),
                    FileMetadata.failed_at.isnot(None),
                )
            )
        ).all()
    }
    metadata_skipped = {
        str(fid) for fid in (await session.scalars(select(FileRecord.id).where(_fids_scope(fids, "ms_ids"), skipped_clause(Stage.METADATA)))).all()
    }
    fingerprint_done = {
        str(fid)
        for fid in (
            await session.scalars(
                select(FileRecord.id).where(_fids_scope(fids, "f_ids"), or_(done_clause(Stage.FINGERPRINT), skipped_clause(Stage.FINGERPRINT)))
            )
        ).all()
    }
    push_done = {str(fid) for fid in (await session.scalars(_select_done_push_ids(fids))).all()}

    return _DoneSets(
        analyze_done=analyze_done,
        metadata_domain_completed=metadata_domain_completed,
        metadata_failed_at=metadata_failed_at,
        metadata_skipped=metadata_skipped,
        fingerprint_done=fingerprint_done,
        push_done=push_done,
    )


def _select_done_analyze_ids(fids: list[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
    """Build the ledger-scoped SELECT for file ids whose analyze stage is DOMAIN-COMPLETE (D-01/D-06).

    ``domain_completed_clause(ANALYZE)`` == ``done OR failed`` -- both terminal, because
    ``FAILURE_IS_TERMINAL[analyze]`` is True: a genuinely un-analyzable file (analyze FAILED) is
    domain-complete and is NEVER auto-looped by ``recover_orphaned_work``. The operator-gated
    ``POST /pipeline/analysis-failed/retry`` (``routers/pipeline.py`` ``retry_analysis_failed``) is the
    manual counterpart -- it CLEARS ``analysis.failed_at`` BEFORE re-enqueuing (the Phase-81 CR-01 fix),
    so a re-driven file leaves the failed disjunct and is no longer domain-complete (and analyze has no
    ambiguous D-10 cell, unlike metadata). Scoped to the ledger's ``fids`` via a single array bind.
    """
    return select(FileRecord.id).where(_fids_scope(fids, "a_ids"), domain_completed_clause(Stage.ANALYZE))


def _select_done_push_ids(fids: list[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
    """Build the ledger-scoped SELECT for file ids whose push stage is done (D-07, sidecar-derived).

    ``push_done = cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)``. A ``push_file``
    ledger row is created ONLY by ``ComputeAgentBackend.dispatch`` -> ``_enqueue_push_file``, so a
    push_file row IMPLIES the compute lane (kueue never enqueues push_file); on that lane SUCCEEDED
    means "pushed and analyzing" and SUBMITTED means "still pushing". This is behavior-identical to the
    retired ``state IN (PUSHED, ANALYZED, ANALYSIS_FAILED)``: SUCCEEDED covers PUSHED (the
    landed-but-not-yet-analyzed window) and ``domain_completed(analyze)`` covers the onward advance. A
    file at SUBMITTED / AWAITING / no-row is NOT push-done and its push_file row correctly re-drives.
    NO ``FileRecord.state`` read. Scoped to the ledger's ``fids`` via a single array bind.
    """
    succeeded = exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status == CloudJobStatus.SUCCEEDED.value))
    return select(FileRecord.id).where(_fids_scope(fids, "p_ids"), or_(succeeded, domain_completed_clause(Stage.ANALYZE)))


async def _awaiting_cloud_job_ids(session: AsyncSession) -> set[str]:
    """File-id strings for files HELD in AWAITING_CLOUD (a ``cloud_job(status='awaiting')`` sidecar row).

    83-06 (CONSCIOUSLY REVERSES D-09): a held awaiting-cloud file is owned SOLELY by the
    :func:`~phaze.tasks.release_awaiting_cloud.stage_cloud_window` drain (via its awaiting ``cloud_job``
    row), so ``recover_orphaned_work`` EXCLUDES it -- exactly as it excludes an in-flight cloud file
    (:func:`_in_flight_cloud_job_ids`). This REPLACES the former ``_get_awaiting_cloud_ids`` +
    ``held_agent_rows`` compute-only partition, which existed only because the D-09 held-file backfill
    SEEDED a ``process_file:<id>`` ledger row for every held compute file. 83-06 removed that seed (and
    deletes the orphaned row), so no held file carries a ``process_file`` ledger row any more; the
    partition became unreachable. Excluding awaiting-cloud files here preserves the CLOUDROUTE-02 safety
    invariant ROBUSTLY (a held long file is NEVER routed kind-agnostically to a fileserver and analyzed
    locally) even for a LEGACY row seeded by the pre-83-06 backfill -- the drain, not the ledger, re-drives
    it. ``'awaiting'`` is deliberately OUT of :data:`IN_FLIGHT`, so the two exclusion sets are disjoint and
    each is read ONCE per recovery run.
    """
    return {str(fid) for fid in (await session.scalars(select(CloudJob.file_id).where(CloudJob.status == CloudJobStatus.AWAITING.value))).all()}


async def _in_flight_cloud_job_ids(session: AsyncSession) -> set[str]:
    """File-id strings for files that currently carry an in-flight ``cloud_job`` row (Phase 69, SCHED-05).

    After Phase-68 BACK-03 a cloud-burst file has BOTH an in-flight ``cloud_job`` row (any
    ``backend_id``) AND a ``process_file`` / ``push_file`` scheduling-ledger row. Both the backend
    reconcile/``/pushed`` callback and this ledger recovery could otherwise claim ownership of that
    file's re-drive -- a double-owner vector that is exactly the 44.5k over-enqueue incident class.
    Excluding every file with a live ``cloud_job`` row from the ledger orphan set makes the backend
    reconcile/callback the SINGLE owner for cloud-backed files. 83-06 (reverses D-09): a HELD
    AWAITING_CLOUD file (``'awaiting'`` ∉ ``IN_FLIGHT``) is likewise excluded, but via the disjoint
    :func:`_awaiting_cloud_job_ids` set -- the ``stage_cloud_window`` drain owns it. A file with NEITHER
    an in-flight NOR an awaiting ``cloud_job`` row (a genuinely-orphaned local re-drive) keeps its
    recovery path.

    ``IN_FLIGHT`` = {UPLOADING, UPLOADED, SUBMITTED, RUNNING} (terminal SUCCEEDED/FAILED excluded);
    the set is small and bounded (in-flight rows only), read ONCE per recovery run alongside the
    done-sets. Mirrors :func:`_awaiting_cloud_job_ids`.
    """
    return {str(fid) for fid in (await session.scalars(select(CloudJob.file_id).where(CloudJob.status.in_([s.value for s in IN_FLIGHT])))).all()}


def _natural_id(row: SchedulingLedger) -> str | None:
    """Return the file-id natural id from a predicate-covered row's stored payload, or None.

    The three predicate-covered functions are all file-keyed (``_KEY_BUILDERS`` uses ``file_id``),
    so the natural id is ``payload["file_id"]``. A missing/empty payload field yields None (treated
    as NOT domain-completed -- replay rather than silently drop).
    """
    payload = row.payload or {}
    fid = payload.get("file_id")
    return str(fid) if fid is not None else None


def is_domain_completed(row: SchedulingLedger, done_sets: _DoneSets) -> bool:
    """Return True only for a predicate-covered row whose file is DOMAIN-COMPLETE for that stage.

    ALWAYS False for the five live-keys-only functions (scan_live_set + the four controller
    stages): their ledger clear is reliable on every terminal outcome, so the live-key filter is
    the sole exclusion and any surviving row is genuinely orphaned. For the four predicate-covered
    agent stages the derivation reads DIRECTLY from the done-sets (D-05 -- the double-negation cut:
    the enrich branches now test ``fid in done_set``, never ``fid not in pending_set``, so once
    Phase 82 redefines pending as ``NOT done AND NOT in_flight`` a genuinely-orphaned in-flight-ledger
    file cannot be silently mis-classified as done):

    - ``process_file``: file id in ``analyze_done`` (domain_completed(analyze) == done OR terminal-failed).
    - ``push_file``: file id in ``push_done`` (succeeded OR domain_completed(analyze), D-07).
    - ``fingerprint_file``: file id in ``fingerprint_done`` (done OR skipped -- a failed fingerprint
      auto-retries; an operator-force-SKIPPED fingerprint is excluded, phase-87 behavior 5).
    - ``extract_file_metadata``: skip-first, THEN the D-10 cell. A force-SKIPPED file (``metadata_skipped``)
      is domain-complete UNCONDITIONALLY and returns True BEFORE the D-10 gate is ever consulted
      (phaze-3m5n) -- the force-skip writer's additive-only contract (T-87-20) never clears
      ``failed_at``, so a file skipped after a failed operator retry still carries a stale
      ``metadata_failed_at`` entry, and the D-10 gate must never be applied to it (that would
      re-classify the skip as "genuinely pending" and re-drive a stage the operator explicitly
      terminated). Absent a skip marker, domain-complete when ``done(metadata)`` OR the metadata FAILED
      AND the ledger row's ``enqueued_at <= metadata.failed_at``. metadata is the ONLY stage with this
      ambiguous failed-only cell: ``retry_metadata_failed`` LEAVES ``failed_at`` set (81 D-11) then
      re-enqueues, so ``(ledger row AND failed_at)`` is ambiguous. ``enqueued_at > failed_at`` (an
      orphaned OPERATOR retry) re-drives; ``enqueued_at < failed_at`` (a callback that wrote the marker
      but crashed before clearing the ledger) stays terminal. ``analysis.failed_at`` is cleared on retry
      (CR-01), so analyze has no such cell -- the asymmetry is intentional (D-10).
    """
    function = row.function
    if function not in _DOMAIN_COMPLETED_STAGES:
        return False
    fid = _natural_id(row)
    if fid is None:
        return False
    if function == "process_file":
        return fid in done_sets.analyze_done
    if function == "push_file":
        return fid in done_sets.push_done
    if function == "fingerprint_file":
        return fid in done_sets.fingerprint_done
    # extract_file_metadata -- force-skip is checked BEFORE the D-10 gate (phaze-3m5n): the gate must
    # refine ONLY the subset of metadata_domain_completed whose membership derives solely from the
    # failed disjunct, never a row that is ALSO domain-complete via the skipped disjunct.
    if fid in done_sets.metadata_skipped:
        return True  # force-skipped -> domain-complete unconditionally, D-10 gate never applies
    # D-10 call-site gate via SchedulingLedger.enqueued_at.
    if fid not in done_sets.metadata_domain_completed:
        return False  # neither done, skipped, nor failed -> genuinely pending -> re-drive
    failed_at = done_sets.metadata_failed_at.get(fid)
    if failed_at is None:
        return True  # done (a metadata row present, failed_at NULL) -> domain-complete
    # failed -> the D-10 cell: terminal only when the ledger row PRE-DATES the failure marker.
    # ``scheduling_ledger.enqueued_at`` is ``TIMESTAMP WITHOUT TIME ZONE`` (migration 022), so asyncpg
    # returns it NAIVE in production, while ``metadata.failed_at`` is ``timezone=True`` (aware). Coerce
    # the naive ledger stamp to UTC-aware before comparing -- a bare ``naive <= aware`` raises TypeError
    # and would abort the whole recovery run (CR-02). The in-memory D-10 unit rows are already aware, so
    # this is a no-op for them and the real fix only shows against a DB round-trip (see the CR-02 test).
    enqueued_at = row.enqueued_at if row.enqueued_at.tzinfo is not None else row.enqueued_at.replace(tzinfo=UTC)
    return enqueued_at <= failed_at


async def _replay_row(queue: Any, row: SchedulingLedger, tally: dict[str, int]) -> None:
    """Replay one orphaned ledger row through its keyed producer, updating ``tally``.

    The STORED payload is replayed verbatim with the deterministic key re-stamped from the ledger
    key (``key=row.key`` -- exactly what the ``before_enqueue`` hook would stamp from the payload,
    so a still-live item dedups to None). NEVER a raw random-key enqueue. A None return (dedup)
    counts as skipped; otherwise reenqueued. extra='forbid' agent schemas re-validate the stored
    payload on dequeue, so a malformed row dead-letters rather than executing (T-45-10).

    The stored SAQ Job policy (``row.timeout`` / ``row.retries``) is replayed too when present, so
    a recovered long ``process_file`` keeps its 7200s/retries=2 bound. Were they omitted, the
    queue's ``apply_project_job_defaults`` before_enqueue hook would stamp the job back to the 600s
    role default -- a 12x reduction that times out every long concert set on recovery (the
    recover-button timeout-loss bug). A NULL column (legacy/backfilled row, or a producer that set
    no explicit policy) is left out so the default applies exactly as before.
    """
    # Job-control kwargs only when the ledger captured them (NULL => fall back to queue defaults).
    policy: dict[str, Any] = {}
    if row.timeout is not None:
        policy["timeout"] = row.timeout
    if row.retries is not None:
        policy["retries"] = row.retries
    await queue.connect()
    job = await queue.enqueue(row.function, key=row.key, **policy, **(row.payload or {}))
    if job is None:
        tally["skipped"] += 1
    else:
        tally["reenqueued"] += 1


async def recover_orphaned_work(ctx: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Gated, ledger-driven, idempotent restart/queue-loss recovery producer (Phase 45).

    Re-enqueues exactly ``ledger MINUS live-saq_jobs-keys MINUS domain-completed`` by replaying each
    orphaned row's STORED payload through the SAME keyed producer that originally enqueued it. Both
    the controller startup hook and the manual "Recover" button (force=True) call THIS one function,
    so the automatic and manual paths cannot drift (D-03).

    Flow:

    1. DETECT gate (skipped when ``force``): if :func:`count_inflight_jobs` reports any queued/active
       ``saq_jobs`` row, this is a DURABLE Phase-36 restart -- nothing was lost. Returns a structured
       no-op ``{"detected_loss": False, "forced": False, "stages": {}}`` and enqueues NOTHING (D-02).
    2. RECOVER (when ``saq_jobs`` is empty, OR ``force=True``): read the ledger rows + the live keys +
       the per-stage done sets ONCE; ``orphaned = [r for r in rows if r.key not in live and not
       is_domain_completed(r, done_sets)]``. Partition by ``r.routing``: controller rows replay on
       ``ctx["queue"]``; agent rows replay on the active agent's per-agent queue. On
       ``NoActiveAgentError`` (cold boot, D-05) the agent rows skip with a WARNING (zero counts) while
       the controller rows still replay. Each producer's ``None`` return (deterministic-key dedup)
       counts as ``skipped``, otherwise ``reenqueued``.

    ``force=True`` bypasses ONLY the no-op DETECT gate (the manual-button path) -- it never bypasses
    the per-item deterministic-key dedup, so a forced reconcile over a live queue is still idempotent.

    Returns ``{"detected_loss": bool, "forced": bool, "stages": {<function>: {"reenqueued": N,
    "skipped": M}, ...}}`` keyed per keyed function (all eight initialized to zero so the shape is
    total). Degrade-safe: agent-stage absence skips rather than raises.
    """
    # Control-only task: get_settings() returns the ControlSettings in the controller role, so the
    # cast safely narrows BaseSettings -> ControlSettings (kept for parity with the control-side
    # producers; recovery itself no longer reads a settings field, but the role contract holds).
    _ = cast("ControlSettings", get_settings())

    async with ctx["async_session"]() as session:
        inflight = await count_inflight_jobs(session)
        detected_loss = inflight == 0

        if not force and not detected_loss:
            logger.info("recover_orphaned_work no-op: queue durable (Phase-36 restart)", inflight=inflight)
            return {"detected_loss": False, "forced": False, "stages": {}}

        rows = await get_ledger_rows(session)
        live = await get_live_job_keys(session)
        # D-06: the done-sets are scoped to the ledger's files ONLY (O(|ledger|), never O(200K)). Read
        # the fids once from the same rows and bind them as a single Postgres array (:func:`_fids_scope`).
        done_sets = await _build_done_sets(session, _ledger_fids(rows))
        # SCHED-05: a file with an in-flight cloud_job row (any backend_id) is owned SOLELY by its
        # backend reconcile/`/pushed` callback -- excluding it here keeps exactly one recovery owner
        # per backend kind, so a compute-backed cloud file gains no second recovery path (no replay
        # of the 44.5k over-enqueue incident class). Read ONCE, alongside live/done_sets.
        in_flight = await _in_flight_cloud_job_ids(session)
        # 83-06 (CONSCIOUSLY REVERSES D-09): a file HELD in AWAITING_CLOUD is owned SOLELY by the
        # stage_cloud_window drain (via its awaiting cloud_job row) -- exclude it so recovery never
        # re-drives it (and never routes a held long file kind-agnostically to a fileserver for LOCAL
        # analysis, the CLOUDROUTE-02 invariant). This REPLACES the former _get_awaiting_cloud_ids +
        # held_agent_rows compute-only partition, which was reachable only via the removed D-09 held-file
        # ledger seed. Read ONCE, alongside in_flight (the two sets are disjoint -- 'awaiting' ∉ IN_FLIGHT).
        awaiting_cloud = await _awaiting_cloud_job_ids(session)

        orphaned = [
            r
            for r in rows
            if r.key not in live
            and not is_domain_completed(r, done_sets)
            and _natural_id(r) not in in_flight
            and _natural_id(r) not in awaiting_cloud
        ]

        # Initialize every keyed function to zero so the return shape is TOTAL (and a stage with no
        # orphaned rows reads as an explicit zero, not a missing key the startup-log/UI must guess at).
        stages: dict[str, dict[str, int]] = {fn: _zero() for fn in _ALL_KEYED_FUNCTIONS}

        controller_rows = [r for r in orphaned if r.routing == "controller"]
        agent_rows = [r for r in orphaned if r.routing == "agent"]

        # 83-06 (CONSCIOUSLY REVERSES D-09): the former compute-only ``held_agent_rows`` partition is GONE.
        # It caught a ``process_file`` ledger row whose file was HELD in AWAITING_CLOUD and routed it to a
        # COMPUTE agent only (CLOUDROUTE-02: never a fileserver -> never local analysis). That partition was
        # reachable ONLY because the D-09 held-file backfill SEEDED a ``process_file:<id>`` ledger row for
        # every held compute file. 83-06 removed that seed (backfill now DELETES the orphaned row and keeps
        # only the awaiting ``cloud_job`` row as the sole registry), so no held file carries a process_file
        # ledger row any more -- the partition was provably empty. The CLOUDROUTE-02 invariant is now held
        # ROBUSTLY (even for a LEGACY pre-83-06 row) by the ``awaiting_cloud`` orphan-set exclusion above:
        # a held awaiting-cloud file never reaches ANY agent partition, so it can never be analyzed locally.
        # Phase 50 (D-10): a re-driven push_file reads the media mount, so it MUST route to a FILESERVER
        # agent (the rsync initiator), never the compute agent -- partition push rows onto their own path.
        push_rows = [r for r in agent_rows if r.function == "push_file"]
        other_agent_rows = [r for r in agent_rows if r.function != "push_file"]

        # Controller rows replay regardless of agent presence (D-05).
        for row in controller_rows:
            await _replay_row(ctx["queue"], row, stages[row.function])

        # Phase 50 (D-10): push_file re-drives route to a FILESERVER (the media-mount owner that runs
        # the rsync); with no fileserver online, skip with a WARNING (the next staging-cron tick / a
        # later recovery re-drives the still-PUSHING file -- never enqueue it onto a compute agent).
        if push_rows:
            try:
                fileserver_agent = await select_active_agent(session, kind="fileserver")
            except NoActiveAgentError:
                logger.warning(
                    "recover_orphaned_work: no fileserver agent -- push_file rows skipped for the staging cron (D-10)",
                    push_rows=len(push_rows),
                )
            else:
                # quick-260707-dh1: push_rows are all push_file -> the io lane.
                fileserver_queue = ctx["task_router"].queue_for(fileserver_agent.id, lane_for_task("push_file"))
                for row in push_rows:
                    await _replay_row(fileserver_queue, row, stages[row.function])

        # Remaining agent rows need any online FILESERVER agent (cold boot may have none -> skip, never raise).
        if other_agent_rows:
            try:
                # phaze-mits (mirrors phaze-5r8f / enqueue_router.resolve_queue_for_task): these rows are
                # ALL fileserver-local (process_file / extract_file_metadata / fingerprint_file /
                # scan_live_set / s3_upload) -- the media-mount owner is the ONLY valid consumer. An
                # UNSCOPED pick (kind=None orders by last_seen_at across ALL kinds) races the heartbeat
                # and could land a fileserver-local task on a media-less compute agent, where
                # process_file's path read FileNotFounds into terminal ANALYSIS_FAILED and the
                # fingerprint/meta lanes have NO consumer (the parked non-terminal row then wedges the
                # global saq_jobs.key forever). Scope to kind="fileserver" -- matching the push_file
                # branch just above (D-10) and CLOUDROUTE-02.
                agent = await select_active_agent(session, kind="fileserver")
            except NoActiveAgentError:
                logger.warning(
                    "recover_orphaned_work: no fileserver agent -- agent-routed ledger rows skipped (cold boot)",
                    agent_rows=len(other_agent_rows),
                )
            else:
                # quick-260707-dh1: other_agent_rows are MIXED functions (process_file /
                # extract_file_metadata / fingerprint_file) -> derive the lane PER ROW via
                # lane_for_task(row.function). An unmapped function raises loudly (never a bad queue).
                for row in other_agent_rows:
                    agent_queue = ctx["task_router"].queue_for(agent.id, lane_for_task(row.function))
                    await _replay_row(agent_queue, row, stages[row.function])

    logger.info("recover_orphaned_work complete", detected_loss=detected_loss, forced=force, stages=stages)
    return {"detected_loss": detected_loss, "forced": force, "stages": stages}


# The eight keyed function names, sourced from ``deterministic_key._KEY_BUILDERS`` (a Postgres-free
# ``_shared`` module) so the recovery return shape can never drift from the real keyed-task universe.
# ``deterministic_key`` is import-safe here -- this module is control-only and never loaded by the
# agent worker (tests/test_task_split.py enforces the reverse direction).
_ALL_KEYED_FUNCTIONS: tuple[str, ...] = tuple(_KEY_BUILDERS)


# --- Phase 45 Plan 04: one-time idempotent startup ledger backfill (locked decision #3) --
#
# Between the 022 migration landing and the before_enqueue WRITE hook starting to populate the
# ledger, jobs ALREADY in ``saq_jobs`` (the in-flight cohort + any residual incident jobs) have no
# ledger row, so recovery could not see them. ``backfill_ledger_from_saq_jobs`` closes that gap ONCE
# by seeding the ledger from the live queued/active ``saq_jobs`` rows. It is a CONTROL-SIDE runtime
# reconcile -- NEVER an Alembic data step (Alembic must never read/write the SAQ-owned saq_jobs
# table; T-45-15). It is idempotent (``insert_ledger_if_absent`` == ON CONFLICT DO NOTHING) so it is
# safe to run on every boot and becomes a cheap no-op once the transition cohort drains.
#
# Read-only probe of the SAQ-owned table: SELECT only ``job`` (the serialized blob) + ``key``. The
# SAQ default serializer is ``json.dumps`` (build_pipeline_queue sets no custom dump/load), so the
# blob is a JSON object carrying top-level ``function`` / ``kwargs`` / ``key`` -- we parse it with
# the SAME tolerant idiom as ``pipeline._job_started_ms`` (no ``saq.Job`` construction, which would
# need the live queue object and raise on a queue-name mismatch). Only the status allowlist literal
# is in the SQL -- no operator input is interpolated (T-44-05 discipline).
_BACKFILL_SAQ_JOBS_SQL = text("SELECT job, key FROM saq_jobs WHERE status IN ('queued', 'active')")


def _parse_job_blob(blob: object) -> dict[str, Any] | None:
    """Deserialize a SAQ ``saq_jobs.job`` blob to its dict, or None if unreadable (T-45-12).

    Mirrors ``pipeline._job_started_ms``: ``json.loads`` a str/bytes blob (the default json.dumps
    serializer), pass a pre-decoded dict through, and treat anything that is not JSON / not a dict
    as None so one malformed/malicious row skips ALONE instead of aborting the batch.
    """
    try:
        data = json.loads(blob) if isinstance(blob, (str, bytes, bytearray)) else blob
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def backfill_ledger_from_saq_jobs(session: AsyncSession) -> dict[str, int]:
    """Seed the scheduling ledger from the live queued/active ``saq_jobs`` rows (idempotent).

    For each ``saq_jobs`` row with status in ``('queued', 'active')``: deserialize its job blob to
    recover ``function`` / ``kwargs`` / ``key``; if the function is a KEYED pipeline function
    (in :data:`deterministic_key._KEY_BUILDERS`) insert a ledger row with ON CONFLICT (key) DO
    NOTHING (via the Plan-01-owned :func:`insert_ledger_if_absent`, routing stamped via
    :func:`routing_for_function`). A non-keyed / random-key row is SKIPPED (no ledger row).

    The DO NOTHING conflict clause makes this:

    - idempotent -- a second call over the same broker state inserts 0 (every key already present);
    - non-clobbering -- a row already written by the before_enqueue WRITE hook is left UNTOUCHED, so
      the (possibly fresher) hook payload always wins (T-45-13).

    Degrade-safe (T-45-14): the read runs inside a SAVEPOINT (``session.begin_nested()``); a missing
    ``saq_jobs`` table (a pre-migration env) rolls the nested scope back ALONE and returns an empty
    tally. The caller commits. NEVER raises -- a backfill failure must not abort controller boot.

    Returns ``{"inserted": N, "skipped": M}`` where ``inserted`` counts ledger ``insert_if_absent``
    calls issued for keyed rows and ``skipped`` counts rows that were not keyed or whose blob/key
    could not be parsed. (DO NOTHING makes ``inserted`` an UPPER bound on rows actually written --
    a row already present is a no-op INSERT; the integration test asserts the row count, not this
    tally, for the no-overwrite case.)
    """
    tally = {"inserted": 0, "skipped": 0}

    try:
        async with session.begin_nested():
            rows = (await session.execute(_BACKFILL_SAQ_JOBS_SQL)).all()
    except Exception:
        logger.warning("ledger_backfill_degraded: saq_jobs read failed (pre-migration env?)", exc_info=True)
        return tally

    for row in rows:
        blob, key = row[0], row[1]
        data = _parse_job_blob(blob)
        if data is None:
            tally["skipped"] += 1
            continue
        function = data.get("function")
        # Belt-and-suspenders: trust the blob's function, but fall back to the saq_jobs key prefix
        # (``<function>:<natural_id>``) so a row missing the field is still classified correctly.
        if not isinstance(function, str) and isinstance(key, str):
            function = key.split(":", 1)[0]
        if not isinstance(function, str) or function not in _KEY_BUILDERS or not isinstance(key, str):
            tally["skipped"] += 1
            continue
        kwargs = data.get("kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
        # The SAQ default json.dumps serializer writes timeout/retries (Job dataclass fields) at the
        # blob top level. Carry them through so even the in-flight transition cohort (e.g. the live
        # backlog enqueued with timeout=7200) recovers with its real bound, not the 600s default.
        timeout = data.get("timeout") if isinstance(data.get("timeout"), int) else None
        retries = data.get("retries") if isinstance(data.get("retries"), int) else None
        await insert_ledger_if_absent(session, key=key, function=function, kwargs=dict(kwargs), timeout=timeout, retries=retries)
        tally["inserted"] += 1

    return tally
