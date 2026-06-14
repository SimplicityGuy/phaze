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
surviving ``saq_jobs`` rows itself, and reclaims timed-out ``active`` jobs on its own. The old
``reenqueue_discovered`` premise ("Redis is empty after a reboot, so every DISCOVERED file
re-enqueues") was therefore OBSOLETE: a normal restart loses NOTHING. A genuine "queue-loss" is
now the rare, DETECTABLE asymmetry "``saq_jobs`` has zero queued/active rows while the domain DB
still shows pending work" (a truncate / restore-from-backup / fresh migration).

Plan 42-02 ACTED on that reframe: the Analyze-only, Redis-era ``reenqueue_discovered`` producer
AND its every-5-min controller cron were DELETED, and the controller startup hook now calls
:func:`recover_orphaned_work` instead. Steady state produces ZERO automatic enqueues -- DO NOT
re-introduce a steady-state auto-advance cron or the deleted producer.

:func:`recover_orphaned_work` is the Phase-42 producer that REPLACED ``reenqueue_discovered``:
it reconciles ALL eight pipeline stages (gated on the ``count_inflight_jobs`` loss detector),
re-enqueuing each stage's shared pending set through the IDENTICAL keyed producer the manual DAG
triggers use -- so manual and recovery paths cannot drift (D-03) and the deterministic-key dedup
collapses any surviving live item to a skipped no-op (no doubling, Phase-32 class).

Routing carries forward the Phase-32 pitfalls: agent stages (analyze/metadata/fingerprint/
scan_live_set) route to the active agent's per-agent queue via ``select_active_agent`` +
``ctx["task_router"].queue_for(agent.id)`` -- NEVER the consumer-less controller queue
(Pitfall 1); controller stages (proposals/search/scrape/match) route to ``ctx["queue"]``. Zero
live agents (common right after a cold reboot; Pitfall 3) logs a warning and skips the agent
stages with zero counts instead of raising. The cached ``task_router`` is reused, never
reconstructed per call (Pitfall 4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from phaze.config import get_settings
from phaze.models.file import FileState
from phaze.schemas.agent_tasks import ExtractMetadataPayload, FingerprintFilePayload, ScanLiveSetPayload
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import (
    count_inflight_jobs,
    get_files_by_state,
    get_fingerprint_pending_files,
    get_match_pending_tracklists,
    get_metadata_pending_files,
    get_proposal_pending_batches,
    get_scrape_pending_tracklists,
    get_untracked_files,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.models.file import FileRecord


logger = structlog.get_logger(__name__)


# --- Phase 42: gated, all-stages, idempotent recovery producer --------------------------
#
# A reconcile re-enqueue of any item still in saq_jobs dedups against its deterministic key
# (apply_deterministic_key, the single before_enqueue chokepoint) and returns None -> counted
# as skipped. SAFETY BACKSTOP (T-42-05): even a conservative detector that FALSE-POSITIVES
# "loss" and re-enqueues every pending set is safe -- the deterministic-key dedup collapses
# every still-live item to a skipped no-op, so a reconcile can NEVER double the queue (the
# Phase-32 doubling incident is closed; its legacy random-key cohort is long drained). Keep
# every enqueue strictly through the keyed producers (never a raw random-key queue.enqueue).


def _zero() -> dict[str, int]:
    """Return a fresh zero per-stage tally."""
    return {"reenqueued": 0, "skipped": 0}


async def _reconcile_controller_items(queue: Any, task_name: str, kwargs_list: list[dict[str, Any]]) -> dict[str, int]:
    """Enqueue one controller job per item, counting dedup ``None`` returns as skipped.

    ``task_name``/``kwargs`` mirror the manual controller triggers EXACTLY (no explicit
    ``key=`` -- the ``before_enqueue`` hook stamps ``<task_name>:<natural_id>``), so a recovery
    re-enqueue dedups against any surviving in-flight job.
    """
    tally = _zero()
    for kwargs in kwargs_list:
        job = await queue.enqueue(task_name, **kwargs)
        if job is None:
            tally["skipped"] += 1
        else:
            tally["reenqueued"] += 1
    return tally


async def _reconcile_agent_payloads(queue: Any, task_name: str, payloads: list[dict[str, Any]]) -> dict[str, int]:
    """Enqueue one full-payload agent job per item, counting dedup ``None`` returns as skipped.

    Each ``payload`` is the COMPLETE ``model_dump(mode="json")`` the matching manual trigger
    builds (extra="forbid" schemas), so no job dead-letters. The ``before_enqueue`` hook stamps
    ``<task_name>:<file_id>`` -- no explicit ``key=`` here.
    """
    tally = _zero()
    for payload in payloads:
        job = await queue.enqueue(task_name, **payload)
        if job is None:
            tally["skipped"] += 1
        else:
            tally["reenqueued"] += 1
    return tally


async def _reconcile_controller_stages(session: AsyncSession, queue: Any, batch_size: int) -> dict[str, dict[str, int]]:
    """Reconcile the four CONTROLLER stages (proposals/search/scrape/match) onto the controller queue."""
    await queue.connect()

    batches = await get_proposal_pending_batches(session, batch_size)
    proposals = _zero()
    for idx, batch in enumerate(batches):
        job = await queue.enqueue("generate_proposals", file_ids=batch, batch_index=idx)
        if job is None:
            proposals["skipped"] += 1
        else:
            proposals["reenqueued"] += 1

    untracked = await get_untracked_files(session)
    search = await _reconcile_controller_items(queue, "search_tracklist", [{"file_id": str(f.id)} for f in untracked])

    scrape_pending = await get_scrape_pending_tracklists(session)
    scrape = await _reconcile_controller_items(queue, "scrape_and_store_tracklist", [{"tracklist_id": str(tl.id)} for tl in scrape_pending])

    match_pending = await get_match_pending_tracklists(session)
    match = await _reconcile_controller_items(queue, "match_tracklist_to_discogs", [{"tracklist_id": str(tl.id)} for tl in match_pending])

    return {"proposals": proposals, "search": search, "scrape": scrape, "match": match}


def _extract_metadata_payload(file: FileRecord, agent_id: str) -> dict[str, Any]:
    return ExtractMetadataPayload(file_id=file.id, original_path=file.original_path, file_type=file.file_type, agent_id=agent_id).model_dump(
        mode="json"
    )


def _fingerprint_payload(file: FileRecord, agent_id: str) -> dict[str, Any]:
    return FingerprintFilePayload(file_id=file.id, original_path=file.original_path, agent_id=agent_id).model_dump(mode="json")


def _scan_payload(file: FileRecord, agent_id: str) -> dict[str, Any]:
    return ScanLiveSetPayload(file_id=file.id, original_path=file.original_path, agent_id=agent_id).model_dump(mode="json")


async def _reconcile_agent_stages(session: AsyncSession, queue: Any, agent_id: str, models_path: str) -> dict[str, dict[str, int]]:
    """Reconcile the four AGENT stages (analyze/metadata/fingerprint/scan_live_set) onto the agent queue."""
    await queue.connect()

    discovered = await get_files_by_state(session, FileState.DISCOVERED)
    analyze = _zero()
    for file in discovered:
        job = await enqueue_process_file(queue, file, agent_id, models_path)
        if job is None:
            analyze["skipped"] += 1
        else:
            analyze["reenqueued"] += 1

    metadata_files = await get_metadata_pending_files(session)
    metadata = await _reconcile_agent_payloads(queue, "extract_file_metadata", [_extract_metadata_payload(f, agent_id) for f in metadata_files])

    fingerprint_files = await get_fingerprint_pending_files(session)
    fingerprint = await _reconcile_agent_payloads(queue, "fingerprint_file", [_fingerprint_payload(f, agent_id) for f in fingerprint_files])

    scan_files = await get_untracked_files(session)
    scan = await _reconcile_agent_payloads(queue, "scan_live_set", [_scan_payload(f, agent_id) for f in scan_files])

    return {"analyze": analyze, "metadata": metadata, "fingerprint": fingerprint, "scan_live_set": scan}


async def recover_orphaned_work(ctx: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Gated, all-stages, idempotent restart/queue-loss recovery producer (Phase 42, D-02/D-03).

    Reconciles ALL eight pipeline stages by re-enqueuing each stage's shared pending set through
    the IDENTICAL keyed producer the manual DAG triggers use. Both the startup hook and the manual
    "Recover" button (Plan 42-02) call THIS one function, so the automatic and manual paths cannot
    drift.

    Flow:

    1. DETECT gate (skipped when ``force``): if :func:`count_inflight_jobs` reports any queued/active
       ``saq_jobs`` row, this is a DURABLE Phase-36 restart -- nothing was lost. Returns a structured
       no-op ``{"detected_loss": False, "forced": False, "stages": {}}`` and enqueues NOTHING (D-02).
    2. RECONCILE (when ``saq_jobs`` is empty, OR ``force=True``): controller stages (proposals/search/
       scrape/match) route to ``ctx["queue"]``; agent stages (analyze/metadata/fingerprint/
       scan_live_set) route to the active agent's per-agent queue. On ``NoActiveAgentError`` (cold
       boot, D-05) the four agent stages skip with a WARNING and zero counts while the controller
       stages still reconcile. Each producer's ``None`` return (deterministic-key dedup) counts as
       ``skipped``, otherwise ``reenqueued``.

    ``force=True`` bypasses ONLY the no-op DETECT gate (the manual-button path) -- it never bypasses
    the per-item deterministic-key dedup, so a forced reconcile over a live queue is still idempotent.

    Returns ``{"detected_loss": bool, "forced": bool, "stages": {<stage>: {"reenqueued": N,
    "skipped": M}, ...}}``. Degrade-safe: agent-stage absence skips rather than raises (Plan 42-02
    still wraps the startup call in try/except for belt-and-suspenders).
    """
    # Control-only task: get_settings() returns the ControlSettings in the controller role, so the
    # cast safely narrows BaseSettings -> ControlSettings for the control-side llm_batch_size field.
    cfg = cast("ControlSettings", get_settings())
    stages: dict[str, dict[str, int]] = {}

    async with ctx["async_session"]() as session:
        inflight = await count_inflight_jobs(session)
        detected_loss = inflight == 0

        if not force and not detected_loss:
            logger.info("recover_orphaned_work no-op: queue durable (Phase-36 restart)", inflight=inflight)
            return {"detected_loss": False, "forced": False, "stages": {}}

        # Controller stages reconcile regardless of agent presence (D-05).
        stages.update(await _reconcile_controller_stages(session, ctx["queue"], cfg.llm_batch_size))

        # Agent stages need an online agent (cold boot may have none -> skip, never raise).
        try:
            agent = await select_active_agent(session)
        except NoActiveAgentError:
            logger.warning("recover_orphaned_work: no active agent -- agent stages skipped (cold boot)")
            for stage in ("analyze", "metadata", "fingerprint", "scan_live_set"):
                stages[stage] = _zero()
        else:
            agent_queue = ctx["task_router"].queue_for(agent.id)
            stages.update(await _reconcile_agent_stages(session, agent_queue, agent.id, cfg.models_path))

    logger.info("recover_orphaned_work complete", detected_loss=detected_loss, forced=force, stages=stages)
    return {"detected_loss": detected_loss, "forced": force, "stages": stages}
