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
  pending set), ``fingerprint_file`` (done when NOT in the fingerprint pending set), and
  ``push_file`` (Phase 50; done when state in {PUSHED, ANALYZED, ANALYSIS_FAILED}). Metadata and
  fingerprint have NO ``/failed`` callback (Plan 02 residual gap), so this is their PRIMARY net;
  push has no terminal clear before its callback, so FileState is its reliable done signal.
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

import json
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord, FileState
from phaze.services.backends import IN_FLIGHT
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import (
    count_inflight_jobs,
    get_fingerprint_pending_files,
    get_live_job_keys,
    get_metadata_pending_files,
)
from phaze.services.scheduling_ledger import get_ledger_rows, insert_ledger_if_absent
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
        "push_file",  # Phase 50 (D-10): done when state in {PUSHED, ANALYZED, ANALYSIS_FAILED}
    }
)
"""Keyed functions that carry a per-stage domain-completed predicate (the SECONDARY exclusion).

EVERY other keyed function (``scan_live_set`` + the four controller stages) is live-keys-only --
its ledger row is reliably cleared on every terminal outcome (scan via Plan 02's ack; controllers
via Plan 01's after_process), so any surviving row IS genuinely orphaned and needs no domain net.
``push_file`` (Phase 50) joins the predicate-covered set: a crash between the staging cron and the
push callback leaves a PUSHING file with no terminal clear, so its FileState (advanced to PUSHED on
a successful land, or onward to ANALYZED) is the reliable done signal.
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
        # push_file (Phase 50): an EXPLICIT done set (file ids that have LANDED on compute scratch
        # or advanced past it). A still-PUSHING file is absent -> orphaned -> re-driven.
        _PUSH_DONE: {str(fid) for fid in (await session.scalars(_select_done_push_ids())).all()},
    }


# Stable done-set keys (avoid stringly-typed drift between _build_done_sets and is_domain_completed).
_ANALYZE_DONE = "analyze_done"
_METADATA_PENDING = "metadata_pending"
_FINGERPRINT_PENDING = "fingerprint_pending"
_PUSH_DONE = "push_done"


def _select_done_analyze_ids() -> Any:
    """Build the SELECT for file ids whose analyze stage is terminal (ANALYZED / ANALYSIS_FAILED)."""
    return select(FileRecord.id).where(FileRecord.state.in_([FileState.ANALYZED, FileState.ANALYSIS_FAILED]))


def _select_done_push_ids() -> Any:
    """Build the SELECT for file ids whose push stage is done (PUSHED, or advanced to a terminal analyze state).

    Phase 50 (D-10): a file is push-done once it has landed on compute scratch (PUSHED) -- or moved
    onward to ANALYZED / ANALYSIS_FAILED, which can only happen after a successful push. A file still
    in PUSHING / AWAITING_CLOUD / DISCOVERED is NOT push-done, so its push_file row re-drives.
    """
    return select(FileRecord.id).where(FileRecord.state.in_([FileState.PUSHED, FileState.ANALYZED, FileState.ANALYSIS_FAILED]))


async def _get_awaiting_cloud_ids(session: AsyncSession) -> set[str]:
    """File-id strings for files currently held in AWAITING_CLOUD (Phase 49, CR-01).

    A backfill-held long file carries an agent-routed ``process_file`` ledger row (D-09) and is NOT
    analyze-done, so recovery treats it as orphaned. It must route to a COMPUTE agent ONLY -- routing
    it to the most-recently-seen agent (typically a fileserver, the exact condition that held it)
    would analyze the long file locally and violate CLOUDROUTE-02. The set is small and bounded (held
    files only), read ONCE per recovery run alongside the done-sets.
    """
    return {str(fid) for fid in (await session.scalars(select(FileRecord.id).where(FileRecord.state == FileState.AWAITING_CLOUD))).all()}


async def _in_flight_cloud_job_ids(session: AsyncSession) -> set[str]:
    """File-id strings for files that currently carry an in-flight ``cloud_job`` row (Phase 69, SCHED-05).

    After Phase-68 BACK-03 a cloud-burst file has BOTH an in-flight ``cloud_job`` row (any
    ``backend_id``) AND a ``process_file`` / ``push_file`` scheduling-ledger row. Both the backend
    reconcile/``/pushed`` callback and this ledger recovery could otherwise claim ownership of that
    file's re-drive -- a double-owner vector that is exactly the 44.5k over-enqueue incident class.
    Excluding every file with a live ``cloud_job`` row from the ledger orphan set makes the backend
    reconcile/callback the SINGLE owner for cloud-backed files, while a file with NO in-flight
    ``cloud_job`` (a genuinely-orphaned held AWAITING_CLOUD file) keeps its existing recovery path.

    ``IN_FLIGHT`` = {UPLOADING, UPLOADED, SUBMITTED, RUNNING} (terminal SUCCEEDED/FAILED excluded);
    the set is small and bounded (in-flight rows only), read ONCE per recovery run alongside the
    done-sets. Mirrors :func:`_get_awaiting_cloud_ids`.
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


def is_domain_completed(row: SchedulingLedger, done_sets: dict[str, set[str]]) -> bool:
    """Return True only for a predicate-covered row whose file is DONE for that stage.

    ALWAYS False for the five live-keys-only functions (scan_live_set + the four controller
    stages): their ledger clear is reliable on every terminal outcome, so the live-key filter is
    the sole exclusion and any surviving row is genuinely orphaned. For the four predicate-covered
    agent stages, "done" is:

    - ``process_file``: file id in the analyze-done set (ANALYZED / ANALYSIS_FAILED).
    - ``push_file``: file id in the push-done set (PUSHED / ANALYZED / ANALYSIS_FAILED).
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
    if function == "push_file":
        return fid in done_sets[_PUSH_DONE]
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
        done_sets = await _build_done_sets(session)
        # SCHED-05: a file with an in-flight cloud_job row (any backend_id) is owned SOLELY by its
        # backend reconcile/`/pushed` callback -- excluding it here keeps exactly one recovery owner
        # per backend kind, so a compute-backed cloud file gains no second recovery path (no replay
        # of the 44.5k over-enqueue incident class). Read ONCE, alongside live/done_sets.
        in_flight = await _in_flight_cloud_job_ids(session)

        orphaned = [r for r in rows if r.key not in live and not is_domain_completed(r, done_sets) and _natural_id(r) not in in_flight]

        # Initialize every keyed function to zero so the return shape is TOTAL (and a stage with no
        # orphaned rows reads as an explicit zero, not a missing key the startup-log/UI must guess at).
        stages: dict[str, dict[str, int]] = {fn: _zero() for fn in _ALL_KEYED_FUNCTIONS}

        controller_rows = [r for r in orphaned if r.routing == "controller"]
        agent_rows = [r for r in orphaned if r.routing == "agent"]

        # Phase 49 (CR-01): a ``process_file`` row whose file is HELD in AWAITING_CLOUD is a LONG file
        # that must NEVER be analyzed locally (CLOUDROUTE-02). Backfill (D-09) gives such held files an
        # agent-routed ledger row, and they are NOT analyze-done, so they reach here as orphaned. The
        # kind-agnostic ``select_active_agent`` below would route them to the most-recently-seen agent --
        # typically a fileserver, since "no compute agent online" is the exact condition that held the
        # file -- so partition them out and route them to a COMPUTE agent ONLY. With no compute agent
        # online they are skipped (the */5 ``release_awaiting_cloud`` cron drains the AWAITING_CLOUD
        # state-set, so the file is not lost). Non-held process_file rows stay on the kind-agnostic path.
        held_ids = await _get_awaiting_cloud_ids(session)
        held_agent_rows = [r for r in agent_rows if r.function == "process_file" and _natural_id(r) in held_ids]
        # Phase 50 (D-10): a re-driven push_file reads the media mount, so it MUST route to a FILESERVER
        # agent (the rsync initiator), never the compute agent -- partition push rows onto their own path.
        push_rows = [r for r in agent_rows if r.function == "push_file"]
        other_agent_rows = [r for r in agent_rows if r.function != "push_file" and not (r.function == "process_file" and _natural_id(r) in held_ids)]

        # Controller rows replay regardless of agent presence (D-05).
        for row in controller_rows:
            await _replay_row(ctx["queue"], row, stages[row.function])

        # Held (AWAITING_CLOUD) rows need an online COMPUTE agent; with none, skip for the release cron.
        if held_agent_rows:
            try:
                compute_agent = await select_active_agent(session, kind="compute")
            except NoActiveAgentError:
                logger.warning(
                    "recover_orphaned_work: no compute agent -- AWAITING_CLOUD rows left for release cron (CLOUDROUTE-02)",
                    held_agent_rows=len(held_agent_rows),
                )
            else:
                compute_queue = ctx["task_router"].queue_for(compute_agent.id)
                for row in held_agent_rows:
                    await _replay_row(compute_queue, row, stages[row.function])

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
                fileserver_queue = ctx["task_router"].queue_for(fileserver_agent.id)
                for row in push_rows:
                    await _replay_row(fileserver_queue, row, stages[row.function])

        # Remaining agent rows need any online agent (cold boot may have none -> skip, never raise).
        if other_agent_rows:
            try:
                agent = await select_active_agent(session)
            except NoActiveAgentError:
                logger.warning(
                    "recover_orphaned_work: no active agent -- agent-routed ledger rows skipped (cold boot)",
                    agent_rows=len(other_agent_rows),
                )
            else:
                agent_queue = ctx["task_router"].queue_for(agent.id)
                for row in other_agent_rows:
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
