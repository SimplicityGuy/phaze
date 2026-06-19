"""Control-side restart / queue-loss recovery for the WHOLE pipeline.

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). These tasks need both
PostgreSQL via ``ctx["async_session"]`` AND the per-agent enqueuer via
``ctx["task_router"]`` (plus the controller queue ``ctx["queue"]``) -- wired in
``phaze.tasks.controller.startup``. The agent worker is deliberately Postgres-free (the
import-boundary test ``tests/test_task_split.py`` enforces this), so this module MUST
NEVER be imported or registered by ``phaze.tasks.agent_worker`` or anything under
``phaze.tasks._shared``. Register it ONLY in ``phaze.tasks.controller``.

THE DURABILITY REFRAME (Phase 42, 42-RESEARCH §Q2 -- READ THIS BEFORE "RESTORING" ANYTHING):
Phase 36 migrated the SAQ broker from Redis to Postgres (``saq_jobs`` table, ``PostgresQueue``).
Queued and active jobs are now DURABLE across a controller restart -- SAQ re-dequeues the
surviving ``saq_jobs`` rows itself, and reclaims timed-out ``active`` jobs on its own. A genuine
"queue-loss" is now the rare, DETECTABLE asymmetry "``saq_jobs`` has zero queued/active rows while
the durable scheduling ledger still records scheduled work" (a truncate / restore-from-backup /
fresh migration). Steady state produces ZERO automatic enqueues -- DO NOT re-introduce a
steady-state auto-advance cron or the deleted ``reenqueue_discovered`` producer.

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
(no doubling, Phase-32 class).

THE PER-STAGE DOMAIN-COMPLETED PREDICATE (the SECONDARY net for Plan 02's residual gap):
``FileState`` is a SINGLE column with NO per-stage FAILED states, so a FileState-derived "done"
predicate is reliable only where a stage's success advances the column past its own gate. The
exclusion is therefore EXPLICIT and TOTAL per stage (asserted in test_recovery.py):

- predicate-covered (:data:`_DOMAIN_COMPLETED_STAGES`): ``process_file`` (analyze; done when
  state in {ANALYZED, ANALYSIS_FAILED}), ``extract_file_metadata`` (done when NOT in the metadata
  pending set), ``fingerprint_file`` (done when NOT in the fingerprint pending set). Metadata and
  fingerprint have NO ``/failed`` callback (Plan 02 residual gap), so this is their PRIMARY net.
- live-keys-only (everything else): ``scan_live_set`` (Plan 02's terminal ack clears its ledger row
  on EVERY outcome, so any surviving row is genuinely orphaned -- no domain predicate) plus the four
  controller stages (``generate_proposals`` / ``search_tracklist`` / ``scrape_and_store_tracklist``
  / ``match_tracklist_to_discogs``; Plan 01's after_process clears them on every terminal status).

Routing carries forward the Phase-32 pitfalls: agent rows route to the active agent's per-agent
queue via ``select_active_agent`` + ``ctx["task_router"].queue_for(agent.id)`` -- NEVER the
consumer-less controller queue (Pitfall 1); controller rows route to ``ctx["queue"]``. Zero live
agents (common right after a cold reboot; Pitfall 3) logs a warning and skips the agent-routed rows
instead of raising. The cached ``task_router`` is reused, never reconstructed per call (Pitfall 4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
import structlog

from phaze.config import get_settings
from phaze.models.file import FileRecord, FileState
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import (
    count_inflight_jobs,
    get_fingerprint_pending_files,
    get_live_job_keys,
    get_metadata_pending_files,
)
from phaze.services.scheduling_ledger import get_ledger_rows
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS


if TYPE_CHECKING:
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
# The THREE agent stages whose ledger clear is NOT reliable on every terminal outcome get an
# explicit domain-completed predicate; the other five are live-keys-only (their clear IS
# reliable). The classification is TOTAL: predicate-covered XOR live-keys-only, asserted in a
# test against _KEY_BUILDERS so no stage is silently undefined (T-45-17).
_DOMAIN_COMPLETED_STAGES: frozenset[str] = frozenset(
    {
        "process_file",  # analyze: done when state in {ANALYZED, ANALYSIS_FAILED}
        "extract_file_metadata",  # done when NOT in get_metadata_pending_files
        "fingerprint_file",  # done when NOT in get_fingerprint_pending_files
    }
)
"""Keyed functions that carry a per-stage domain-completed predicate (the SECONDARY exclusion).

EVERY other keyed function (``scan_live_set`` + the four controller stages) is live-keys-only --
its ledger row is reliably cleared on every terminal outcome (scan via Plan 02's ack; controllers
via Plan 01's after_process), so any surviving row IS genuinely orphaned and needs no domain net.
Kept in sync with ``deterministic_key._KEY_BUILDERS`` by a totality test in test_recovery.py.
"""


def _zero() -> dict[str, int]:
    """Return a fresh zero per-stage tally."""
    return {"reenqueued": 0, "skipped": 0}


async def _build_done_sets(session: AsyncSession) -> dict[str, set[str]]:
    """Compute the per-stage domain-completed file-id sets ONCE for the predicate.

    Returns a mapping keyed by the predicate-covered function name to the set of file-id strings
    that are DONE for that stage:

    - ``process_file``: a direct query for files in {ANALYZED, ANALYSIS_FAILED} (analyze has BOTH a
      success PUT and a ``/failed`` callback in Plan 02, so this is a belt-and-suspenders net for a
      crash-without-callback).
    - ``extract_file_metadata`` / ``fingerprint_file``: the COMPLEMENT of the existing pending-set
      boundary -- a file NOT returned by ``get_metadata_pending_files`` / ``get_fingerprint_pending_files``
      is "done" for that stage. We reuse those queries' membership (NOT their complement-of-done
      SWEEP semantics): the pending set is the small, bounded set of files still needing the stage,
      and we treat "absent from it" as done. This is the PRIMARY net for the metadata/fingerprint
      residual gap (Plan 02: a retries-exhausted job with NO ``/failed`` callback).

    IMPORTANT (45-03 acceptance): ``get_metadata_pending_files`` / ``get_fingerprint_pending_files``
    are imported here ONLY to derive the done-set predicate (membership), NOT to enqueue their
    complement. The recovery WORK set comes exclusively from the ledger. The old complement-of-done
    SWEEP queries (``get_files_by_state``/``get_untracked_files``/``get_proposal_pending_batches``/
    ``get_scrape_pending_tracklists``/``get_match_pending_tracklists``) are GONE from recovery.
    """
    return {
        # process_file: an EXPLICIT done set (file ids in a terminal analyze state).
        _ANALYZE_DONE: {str(fid) for fid in (await session.scalars(_select_done_analyze_ids())).all()},
        # metadata/fingerprint: the PENDING membership; is_domain_completed treats "absent" as done.
        _METADATA_PENDING: {str(f.id) for f in await get_metadata_pending_files(session)},
        _FINGERPRINT_PENDING: {str(f.id) for f in await get_fingerprint_pending_files(session)},
    }


# Stable done-set keys (avoid stringly-typed drift between _build_done_sets and is_domain_completed).
_ANALYZE_DONE = "analyze_done"
_METADATA_PENDING = "metadata_pending"
_FINGERPRINT_PENDING = "fingerprint_pending"


def _select_done_analyze_ids() -> Any:
    """Build the SELECT for file ids whose analyze stage is terminal (ANALYZED / ANALYSIS_FAILED)."""
    return select(FileRecord.id).where(FileRecord.state.in_([FileState.ANALYZED, FileState.ANALYSIS_FAILED]))


def _natural_id(row: SchedulingLedger) -> str | None:
    """Return the file-id natural id from a predicate-covered row's stored payload, or None.

    The three predicate-covered functions are all file-keyed (``_KEY_BUILDERS`` uses ``file_id``),
    so the natural id is ``payload["file_id"]``. A missing/empty payload field yields None (treated
    as NOT domain-completed -- replay rather than silently drop).
    """
    payload = row.payload or {}
    fid = payload.get("file_id")
    return str(fid) if fid is not None else None


def is_domain_completed(row: SchedulingLedger, done_sets: dict[str, set[str]]) -> bool:
    """Return True only for a predicate-covered row whose file is DONE for that stage.

    ALWAYS False for the five live-keys-only functions (scan_live_set + the four controller
    stages): their ledger clear is reliable on every terminal outcome, so the live-key filter is
    the sole exclusion and any surviving row is genuinely orphaned. For the three predicate-covered
    agent stages, "done" is:

    - ``process_file``: file id in the analyze-done set (ANALYZED / ANALYSIS_FAILED).
    - ``extract_file_metadata`` / ``fingerprint_file``: file id ABSENT from the stage's pending set
      (the complement-of-pending == done boundary).
    """
    function = row.function
    if function not in _DOMAIN_COMPLETED_STAGES:
        return False
    fid = _natural_id(row)
    if fid is None:
        return False
    if function == "process_file":
        return fid in done_sets[_ANALYZE_DONE]
    if function == "extract_file_metadata":
        return fid not in done_sets[_METADATA_PENDING]
    # fingerprint_file
    return fid not in done_sets[_FINGERPRINT_PENDING]


async def _replay_row(queue: Any, row: SchedulingLedger, tally: dict[str, int]) -> None:
    """Replay one orphaned ledger row through its keyed producer, updating ``tally``.

    The STORED payload is replayed verbatim with the deterministic key re-stamped from the ledger
    key (``key=row.key`` -- exactly what the ``before_enqueue`` hook would stamp from the payload,
    so a still-live item dedups to None). NEVER a raw random-key enqueue. A None return (dedup)
    counts as skipped; otherwise reenqueued. extra='forbid' agent schemas re-validate the stored
    payload on dequeue, so a malformed row dead-letters rather than executing (T-45-10).
    """
    await queue.connect()
    job = await queue.enqueue(row.function, key=row.key, **(row.payload or {}))
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
        done_sets = await _build_done_sets(session)

        orphaned = [r for r in rows if r.key not in live and not is_domain_completed(r, done_sets)]

        # Initialize every keyed function to zero so the return shape is TOTAL (and a stage with no
        # orphaned rows reads as an explicit zero, not a missing key the startup-log/UI must guess at).
        stages: dict[str, dict[str, int]] = {fn: _zero() for fn in _ALL_KEYED_FUNCTIONS}

        controller_rows = [r for r in orphaned if r.routing == "controller"]
        agent_rows = [r for r in orphaned if r.routing == "agent"]

        # Controller rows replay regardless of agent presence (D-05).
        for row in controller_rows:
            await _replay_row(ctx["queue"], row, stages[row.function])

        # Agent rows need an online agent (cold boot may have none -> skip, never raise).
        if agent_rows:
            try:
                agent = await select_active_agent(session)
            except NoActiveAgentError:
                logger.warning(
                    "recover_orphaned_work: no active agent -- agent-routed ledger rows skipped (cold boot)",
                    agent_rows=len(agent_rows),
                )
            else:
                agent_queue = ctx["task_router"].queue_for(agent.id)
                for row in agent_rows:
                    await _replay_row(agent_queue, row, stages[row.function])

    logger.info("recover_orphaned_work complete", detected_loss=detected_loss, forced=force, stages=stages)
    return {"detected_loss": detected_loss, "forced": force, "stages": stages}


# The eight keyed function names, sourced from ``deterministic_key._KEY_BUILDERS`` (a Postgres-free
# ``_shared`` module) so the recovery return shape can never drift from the real keyed-task universe.
# ``deterministic_key`` is import-safe here -- this module is control-only and never loaded by the
# agent worker (tests/test_task_split.py enforces the reverse direction).
_ALL_KEYED_FUNCTIONS: tuple[str, ...] = tuple(_KEY_BUILDERS)
