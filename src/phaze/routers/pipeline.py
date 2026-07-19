"""Pipeline orchestration router -- trigger endpoints and dashboard UI."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast
import uuid  # noqa: TC003 -- runtime import: FastAPI resolves the `file_id: uuid.UUID` path-param annotation via get_type_hints

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import settings
from phaze.database import async_session, get_session
from phaze.enums.stage import ELIGIBILITY_DAG, ELIGIBLE_AFTER_FAILURE, Stage, Status, eligible, resolve_status
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tracklist import Tracklist
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.schemas.agent_tasks import ExtractMetadataPayload, ScanLiveSetPayload
from phaze.services import enqueue_router
from phaze.services.agent_liveness import derive_compute_lane_identities
from phaze.services.analysis_enqueue import enqueue_process_file, process_file_job_key
from phaze.services.backends import (
    LANE_RECENT_N,
    derive_cloud_hold_reason,
    get_backend_lane_snapshot,
    get_lane_queue_depths,
    get_lane_recent_completions,
    hold_awaiting_cloud,
)
from phaze.services.fingerprint import get_fingerprint_progress
from phaze.services.fingerprint_requeue import enqueue_fingerprint_jobs
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MIN_PAGE_SIZE
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.pipeline import (
    ANALYZE_FILTERS,
    _read_in_own_session,
    _stats_fanout,
    analyze_lanes_content_hash,
    count_active_agents,
    count_backfill_candidates,
    get_analysis_failed_count,
    get_analysis_failed_files,
    get_analyze_files_page,
    get_analyze_working_set,
    get_awaiting_cloud_count,
    get_backfill_candidates,
    get_cached_stage_orphan_counts,
    get_cloud_phase_counts,
    get_discovered_files_with_duration,
    get_files_page,
    get_fingerprint_pending_files,
    get_global_reconciliation,
    get_inadmissible_count,
    get_localqueue_unreachable,
    get_match_busy_count,
    get_match_pending_tracklists,
    get_metadata_failed_files,
    get_metadata_pending_files,
    get_pending_files_page,
    get_proposal_pending_batches,
    get_pushed_count,
    get_pushing_count,
    get_queue_activity,
    get_scan_busy_count,
    get_scrape_busy_count,
    get_scrape_pending_tracklists,
    get_search_busy_count,
    get_stage_busy_counts,
    get_stage_controls,
    get_stage_progress,
    get_straggler_count,
    get_trackid_files_page,
    get_tracklist_sets_page,
    get_untracked_files,
    queue_progress_percent,
)
from phaze.services.pipeline_counters import read_counters
from phaze.services.route_control import get_route_control
from phaze.services.stage_status import failed_clause
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION
from phaze.tasks.reenqueue import recover_orphaned_work


logger = structlog.get_logger(__name__)

_NO_ACTIVE_AGENT_MESSAGE = "No active agent available — start an agent worker and retry"

# Maps each DAG node whose ``done`` is DB-sourced to the maintained ``completed``
# counter function(s) backing it (35-01). Used as a DOCUMENTED degrade-fallback (D-02):
# when a node's ``get_stage_progress`` ``done`` reads 0 (its ``_safe_count`` degraded OR
# the stage is genuinely empty) AND the mapped ``completed`` counter is > 0, the counter
# value renders as the fallback ``done``. DB-truth ALWAYS wins when ``done > 0`` (D-03:
# the DB reconcile is the authority; the counter is a backstop cache, never an override).
# ``discovery`` and ``execute`` have no maintained counter (``scan_directory`` /
# ``execute_approved_batch`` are deterministic-key-exempt), so they never fall back.
# In practice the counter only exceeds 0 after real completions — at which point the DB
# reflects them too unless the DB source degraded — so applying the counter on ``done==0``
# is harmless when the stage is genuinely empty (counter is also 0 there).
# WR-03 unit constraint: a node may map ONLY to per-file SAQ functions, because the node's
# ``done`` is a distinct-file/tracklist count and the fallback renders the counter AS that
# ``done``. ``generate_proposals`` is a BATCH task (one job == N files), so its ``completed``
# counter counts batches, not files — mapping it here would render a batch count as a file
# ``done`` (e.g. 1 batch of 10 files -> proposalsDone=1). It is therefore intentionally OMITTED;
# proposalsDone falls back to DB-truth (0 when degraded) rather than a wrong-unit number. The
# remaining per-file mappings are additionally capped at the node ``total`` in ``_reconciled_done``
# so re-runs cannot inflate ``done`` past the denominator.
_NODE_COMPLETED_FNS: dict[str, tuple[str, ...]] = {
    "metadata": ("extract_file_metadata",),
    "fingerprint": ("fingerprint_file",),
    "analyze": ("process_file",),
    "scan_search": ("scan_live_set", "search_tracklist"),
    "scrape": ("scrape_and_store_tracklist",),
    "match": ("match_tracklist_to_discogs",),
}


async def _read_pipeline_counters(app_state: Any) -> dict[str, dict[str, int]]:
    """Read the maintained per-function Redis counters, degrading to ``{}`` on any failure.

    Mirrors :func:`get_queue_activity`'s failure isolation: a missing ``app.state``
    handle (the test client skips the lifespan) or any Redis hiccup must degrade the
    counter source to an empty dict so the 5s dashboard poll renders from DB-truth and
    NEVER 500s (threat T-35-09). Reads the shared ``app.state.redis`` cache client
    (decode_responses), which the lifespan always wires. Phase 36: the former
    ``controller_queue.redis`` fallback is gone -- the broker is Postgres now and has no
    Redis client to borrow. When ``app.state.redis`` is absent (the test client skips the
    lifespan) the ``getattr`` returns ``None`` and ``read_counters(None)`` degrades via the
    except below.
    """
    try:
        redis = getattr(app_state, "redis", None)
        return await read_counters(redis)
    except Exception:
        logger.warning("pipeline_counters_degraded", exc_info=True)
        return {}


def _reconciled_done(node: str, stage_done: int, stage_total: int, counters: dict[str, dict[str, int]]) -> int:
    """Return the DB-truth ``done`` (D-03), or the ``completed`` counter as a backstop.

    DB-truth wins whenever ``stage_done > 0``. Only when the DB source reads 0 do we fall
    back to the sum of the node's mapped ``completed`` counters (D-02 backstop) — and only
    if that sum is itself > 0. The fallback is capped at ``stage_total`` (when known, > 0) so
    re-run-inflated counters cannot render a ``done`` larger than the denominator (WR-03).
    """
    if stage_done > 0:
        return stage_done
    fallback = sum(counters.get(fn, {}).get("completed", 0) for fn in _NODE_COMPLETED_FNS.get(node, ()))
    if fallback <= 0:
        return stage_done
    return min(fallback, stage_total) if stage_total > 0 else fallback


def _derive_stats(stage_progress: dict[str, dict[str, int | None]]) -> dict[str, int]:
    """Re-express the seven former ``get_pipeline_stats`` keys off ``get_stage_progress`` (Phase 82, D-05/READ-02).

    The stats path no longer reads the raw ``files.state`` column: each of the seven keys ``stats_bar.html``
    consumes maps to a derived :func:`get_stage_progress` output-table count --
    ``discovered→discovery.done``, ``metadata_extracted→metadata.done``, ``fingerprinted→fingerprint.done``,
    ``analyzed→analyze.done``, ``proposal_generated→proposals.done``, ``approved→execute.total``,
    ``executed→execute.done``. The key NAMES are preserved so ``stats_bar.html``'s six visible cards +
    three OOB ``x-init`` store writes need NO template change (the Alpine ``$store.pipeline.*`` keys stay
    stable, Pitfall 4 -- only the server-side source changes). ``queue_progress_percent`` consumes the
    same derived ``analyzed`` numerator.

    SEMANTIC SHIFT (this is the deadlock dissolving, not a regression): ``metadata_extracted`` now counts
    every music/video file whose metadata is done (a ``metadata`` row with ``failed_at`` NULL), NOT the
    transient linear ``METADATA_EXTRACTED`` state (which a file leaves on advancing to
    FINGERPRINTED/ANALYZED). These numbers legitimately differ post-cutover.
    """

    def done(node: str) -> int:
        return int(stage_progress[node]["done"] or 0)

    return {
        "discovered": done("discovery"),
        "metadata_extracted": done("metadata"),
        "fingerprinted": done("fingerprint"),
        "analyzed": done("analyze"),
        "proposal_generated": done("proposals"),
        "approved": int(stage_progress["execute"]["total"] or 0),
        "executed": done("execute"),
    }


async def _build_dag_context(
    app_state: Any,
    session: AsyncSession,
    activity: dict[str, int],  # noqa: ARG001 — kept for caller stability; analyzeActive now derives from stage_progress (Phase 93)
    stage_progress: dict[str, dict[str, int | None]] | None = None,
) -> dict[str, dict[str, int]]:
    """Build the per-DAG-node store-key context consumed by stats_bar.html + the 35-05 canvas.

    Reconciles three sources (D-03): ``get_stage_progress`` (DB-truth ``done``/``total`` per
    node, the authority), the maintained Redis ``completed`` counters (a degrade backstop via
    :func:`_reconciled_done`), and the already-computed ``get_queue_activity`` (the per-node
    ACTIVE state). Every value is a plain ``int`` (``total=None`` em-dash sentinels collapse to
    0 — the Scan/Search node has NO ``tracklistTotal`` store key, so its em-dash stays a
    render-side concern) so it is safe to interpolate into the ``x-init`` numeric store writes.

    ``stage_progress`` is the already-computed :func:`get_stage_progress` result, passed through by
    both the dashboard and poll callers so the (heavy, multi-count) read happens ONCE per request
    (Phase 82, D-05 -- the former ``get_pipeline_stats`` pass-through this replaces). When omitted
    (direct test callers) it is computed here.

    Returns ``{"dag": {<storeKey>: int, ...}}`` carrying every per-node sub-key seeded into
    ``$store.pipeline`` (base.html, 35-04 Task 1).
    """
    stage = stage_progress if stage_progress is not None else await get_stage_progress(session)
    counters = await _read_pipeline_counters(app_state)

    def done(node: str) -> int:
        return _reconciled_done(node, int(stage[node]["done"] or 0), int(stage[node]["total"] or 0), counters)

    def total(node: str) -> int:
        return int(stage[node]["total"] or 0)

    dag: dict[str, int] = {
        "metadataDone": done("metadata"),
        "metadataTotal": total("metadata"),
        "fingerprintDone": done("fingerprint"),
        "fingerprintTotal": total("fingerprint"),
        "analyzeDone": done("analyze"),
        "analyzeTotal": total("analyze"),
        # Phase 93 (CONSOLE-02): the DERIVED in-flight count — the same stage_status_case bucket the
        # Files matrix renders (scheduling_ledger truth, so cloud-burst dispatch counts). The former
        # SAQ agent_active source saw only LOCAL agent queues and read 0 while thousands of analyze
        # jobs were in flight on the compute lanes.
        "analyzeActive": int(stage["analyze"].get("in_flight") or 0),
        "tracklistDone": done("scan_search"),
        "scrapeDone": done("scrape"),
        "scrapeTotal": total("scrape"),
        "matchDone": done("match"),
        "matchTotal": total("match"),
        "proposalsDone": done("proposals"),
        "proposalsTotal": total("proposals"),
        # Approve→Execute gates on the approved-proposal count; execute.total IS that count.
        "approved": total("execute"),
        "executedDone": done("execute"),
        "executedTotal": total("execute"),
    }

    # Phase 38 (38-03 / REQ-38-4): overlay the live per-stage pause/priority intent so the
    # DAG controls reflect authoritative server state across every 5s poll. get_stage_controls
    # owns the never-500 degrade (returns paused=False/priority=50 defaults on any failure), so
    # NO try/except is added here. paused is coerced to int 0/1 — never a Python bool — to keep
    # every dag value a server-computed int safe to interpolate into x-init (Pitfall 3 / T-35-11).
    controls = await get_stage_controls(session)
    for stage_name in ("metadata", "analyze", "fingerprint"):
        dag[f"{stage_name}Paused"] = int(controls[stage_name]["paused"])
        dag[f"{stage_name}Priority"] = int(controls[stage_name]["priority"])

    # Phase 87 (87-08, UI-05 / D-05): per-enrich-stage orphaned/stuck (recovery-candidate) count --
    # the exact number recover_orphaned_work would re-enqueue for the stage (ledger minus live minus
    # domain-completed minus in-flight-cloud). Phase 91 (HYG-01 / WR-02): the hot 5s /pipeline/stats
    # poll now reads the O(1) process-scope cache (get_cached_stage_orphan_counts -- no session, no
    # await) instead of materializing the full scheduling_ledger inline per tick; the FastAPI lifespan
    # _orphan_refresh_loop refreshes that cache off-request (D-01/D-02/D-04). The parity meaning is
    # unchanged: the cached ints ride the same dag.items() OOB seed loop onto the amber rail badges.
    orphans = get_cached_stage_orphan_counts()
    dag["metadataOrphan"] = int(orphans["metadata"])
    dag["analyzeOrphan"] = int(orphans["analyze"])
    dag["fingerprintOrphan"] = int(orphans["fingerprint"])

    # t7k FIX2 (REQ-260613-t7k-FIX2): per-stage in-flight busy counts REPLACE the single global
    # agentBusy gate so the three agent enqueue buttons gate independently (run in parallel).
    # get_stage_busy_counts owns the never-500 degrade (all-zeros on any DB error), so NO try/except
    # is added here; these ints ride the same dag.items() seed + OOB loop with no stats_bar.html edit.
    busy = await get_stage_busy_counts(session)
    dag["metadataBusy"] = int(busy["metadata"])
    dag["analyzeBusy"] = int(busy["analyze"])
    dag["fingerprintBusy"] = int(busy["fingerprint"])

    # Phase 39 (REQ-39-3): the search_tracklist in-flight count gates the DAG Search node "busy".
    # search_tracklist is a controller task, so it is NOT part of get_stage_busy_counts's three
    # agent stages -- get_search_busy_count owns its own never-500 SAVEPOINT degrade (returns 0 on
    # any DB error), so NO try/except is added here; the int rides the same dag.items() seed + OOB loop.
    dag["searchBusy"] = int(await get_search_busy_count(session))

    # Phase 40 (REQ-40-2/REQ-40-3): the Fingerprint-Scan node gates on both an in-flight scan_live_set
    # count ("Scan busy") and an online-agent signal ("Needs agent"). scan_live_set is a per-agent task,
    # so it is NOT part of get_stage_busy_counts's three agent stages -- get_scan_busy_count + count_
    # active_agents each own their own never-500 SAVEPOINT degrade (return 0 on any DB error), so NO
    # try/except is added here; the ints ride the same dag.items() seed + OOB loop. count_active_agents
    # is a count where 0 == "no online agent" (fail-safe default that leaves the node blocked).
    dag["scanBusy"] = int(await get_scan_busy_count(session))
    dag["agentOnline"] = int(await count_active_agents(session))

    # Phase 58 (58-04, WORK-03): the Analyze A1 lane's "compute online" capacity numeral -- a
    # READ-ONLY kind-scoped count of online compute agents, using the SAME liveness predicate as
    # agentOnline (count_active_agents owns the never-500 SAVEPOINT degrade -> 0 on any DB error,
    # so NO try/except here). It rides the existing dag.items() OOB seed loop onto the
    # dag-seed-computeOnline placeholder the Analyze workspace pre-mounts (B1: an OOB seed lands
    # only on an id already in the DOM) -- no second poll, no stats_bar.html edit, no new backend.
    dag["computeOnline"] = int(await count_active_agents(session, kind="compute"))

    # COMPUTE-02: the header "Agents · N" count includes ACTIVE compute lanes alongside
    # heartbeating agents via a NEW additive key -- agentOnline's 0-degrade fail-safe semantics
    # (scan-launch gate) are UNTOUCHED. derive_compute_lane_identities owns its own never-500
    # degrade (returns all-IDLE lanes on any DB error), so NO try/except is added here; only
    # ACTIVE lanes count (IDLE configured clusters are not "active"; WAITING is a quota alarm,
    # not an online worker). It rides the same dag.items() seed + OOB loop, no stats_bar.html edit.
    dag["computeLanesActive"] = sum(1 for lane in await derive_compute_lane_identities(session) if lane.state == "ACTIVE")

    # Phase 41 (REQ-41-3): the scrape_and_store_tracklist / match_tracklist_to_discogs in-flight counts
    # gate the DAG Scrape/Match trigger nodes "busy" (Scraping… / Matching…). Both are controller tasks
    # (NOT part of get_stage_busy_counts's three agent stages) -- get_scrape_busy_count + get_match_busy_
    # count each own their own never-500 SAVEPOINT degrade (return 0 on any DB error), so NO try/except is
    # added here; the ints ride the same dag.items() seed + OOB loop. (scrapeTotal/scrapeDone/matchTotal/
    # matchDone are already seeded above; the gate derives pending = total - done client-side.)
    dag["scrapeBusy"] = int(await get_scrape_busy_count(session))
    dag["matchBusy"] = int(await get_match_busy_count(session))

    # Phase 58 (58-02, WORK-01): the Discover "not yet enriched" backlog -- a READ-ONLY derived
    # int (music/video files whose metadata is not yet done), clamped >= 0. Phase 82 (D-05) derives
    # it from the get_stage_progress metadata node (total - done) instead of the removed
    # get_pipeline_stats (discovered - metadata_extracted), which read FileRecord.state. No new query
    # path (``stage`` is already computed above) and no new poll: it rides the existing dag.items()
    # OOB seed loop onto the dag-seed-notYetEnriched placeholder the workspaces pre-mount.
    dag["notYetEnriched"] = max(int(stage["metadata"]["total"] or 0) - int(stage["metadata"]["done"] or 0), 0)

    return {"dag": dag}


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["pipeline"])

# Hold references to background enqueue tasks to prevent GC (same pattern as scan.py)
_background_tasks: set[asyncio.Task[None]] = set()


async def _enqueue_analysis_jobs(queue: Any, files: list[FileRecord], agent_id: str, models_path: str) -> None:
    """Background coroutine to enqueue process_file jobs for a list of files.

    Delegates each enqueue to the FastAPI-free shared producer
    ``services.analysis_enqueue.enqueue_process_file``. That helper owns the
    deterministic job key (``process_file:<file_id>``), the complete 5-field
    ``ProcessFilePayload``, and the job policy (``timeout=7200`` / ``retries=2``)
    -- so this dashboard path and the Wave-2 agent-reboot re-enqueue path cannot
    drift: both emit the IDENTICAL key, letting SAQ's per-queue deterministic-key
    dedup collapse a repeat enqueue of an in-flight file to a no-op (32-RESEARCH §Q4).

    ``files`` attributes (``id`` / ``original_path`` / ``file_type``) are already
    loaded by ``get_files_by_state`` and the request never commits, so reading them
    here (after the request session may have closed) does not trigger a lazy load.

    All process_file trigger endpoints (``/api/v1/analyze`` + the HTMX
    ``/pipeline/analyze``) funnel through this one helper, so the key + policy are
    applied identically at every enqueue site.
    """
    for f in files:
        await enqueue_process_file(queue, f, agent_id, models_path)


async def _route_discovered_by_duration(
    app_state: Any,
    session: AsyncSession,
    files_with_duration: list[tuple[FileRecord, float | None]],
    threshold_sec: int,
    cloud_enabled: bool,
    models_path: str,
) -> dict[str, int]:
    """Route each DISCOVERED file to a queue by its duration (Phase 49 seam, reshaped in Phase 50).

    The single per-file routing decision shared by the "Run Analysis" trigger (this module)
    and the Plan-03 backfill producer, so the two paths cannot drift. Only the FILESERVER
    agent is resolved here (in its OWN ``try/except NoActiveAgentError``, exactly ONCE before
    the loop); its queue is obtained via ``app_state.task_router.queue_for`` (the Phase-30
    invariant -- never the consumer-less default queue).

    Per file, on the captured ``(file, duration)`` tuples:

    - ``duration is None`` or ``< threshold_sec`` AND a fileserver agent is online -> enqueue
      ``process_file`` onto the fileserver queue (``local``), unchanged.
    - ``duration is None`` or ``< threshold_sec`` AND NO fileserver agent online -> count as
      ``skipped`` (cannot route locally) -- NO enqueue, NO state change, the run continues.
    - ``duration >= threshold_sec`` -> ALWAYS set the row's state to ``AWAITING_CLOUD``
      (``awaiting``), regardless of whether a compute agent is online (Phase 50 CLOUDPIPE-01).

    Phase 50 reshape (T-50-bypass): there is NO direct-to-compute enqueue here any more. A long
    file is ALWAYS HELD in AWAITING_CLOUD; the bounded ``stage_cloud_window`` controller cron is
    the SINGLE entry to the compute pipeline (it tops the ≤N window up to ``cloud_max_in_flight``
    by staging ``push_file`` for the oldest held files). Holding in exactly one place is what
    makes the window unbypassable -- a 144-file backlog can never blow up the compute scratch
    disk. A held long file is NEVER silently analyzed locally (the load-bearing CLOUDROUTE-02
    safety invariant, T-49-03).

    The held AWAITING_CLOUD UPDATEs are committed with an explicit ``await session.commit()``
    BEFORE the enqueues are backgrounded (``get_session`` does NOT auto-commit -- RESEARCH
    Pitfall 3).

    Returns ``{"local": N, "cloud": 0, "awaiting": K, "skipped": S, "no_active_agent": 0|1}``;
    ``cloud`` is always 0 (no direct compute enqueue remains). ``no_active_agent`` is 1 when NO
    fileserver agent is online (nothing can route locally): the caller then surfaces the
    no-active-agent response, whose template still reports any HELD long files (WR-01) via the
    ``awaiting`` count -- a held long file is real, durable work the staging cron will drain.
    """
    try:
        fileserver_agent: Agent | None = await enqueue_router.select_active_agent(session, kind="fileserver")
    except enqueue_router.NoActiveAgentError:
        fileserver_agent = None

    fileserver_q = (
        app_state.task_router.queue_for(fileserver_agent.id, enqueue_router.lane_for_task("process_file")) if fileserver_agent is not None else None
    )

    local_files: list[FileRecord] = []
    skipped = 0
    held = 0

    for file, duration in files_with_duration:
        # Phase 51 (D-02): when cloud-burst is OFF nothing is "long" -- every file falls to the
        # local branch, so no row is ever held in AWAITING_CLOUD and the cloud pipeline stays dormant.
        is_long = cloud_enabled and duration is not None and duration >= threshold_sec
        if is_long:
            # Phase 50 (CLOUDPIPE-01): ALWAYS hold -- no direct-to-compute path. The bounded
            # stage_cloud_window cron is the single, unbypassable entry to the compute pipeline.
            # Phase 83 (D-01): hold via the shared writer so every go-forward hold carries its
            # cloud_job(status='awaiting', attempts=0) sidecar row -- closing the missing-writer gap
            # that violated the hard shadow invariant AWAITING_CLOUD => cloud_job(status='awaiting')
            # on every held file since migration 032. The helper dual-writes file.state (D-00c) and
            # NEVER commits; the existing post-loop commit below is the hold's own commit boundary.
            await hold_awaiting_cloud(session, file)
            held += 1
        elif fileserver_agent is not None:
            local_files.append(file)
        else:
            skipped += 1

    # Commit the AWAITING_CLOUD held-state UPDATEs BEFORE backgrounding the enqueues
    # (get_session does not auto-commit -- RESEARCH Pitfall 3).
    if held:
        await session.commit()

    if local_files and fileserver_q is not None and fileserver_agent is not None:
        local_task = asyncio.create_task(_enqueue_analysis_jobs(fileserver_q, local_files, fileserver_agent.id, models_path))
        _background_tasks.add(local_task)
        local_task.add_done_callback(_background_tasks.discard)

    return {
        "local": len(local_files),
        "cloud": 0,
        "awaiting": held,
        "skipped": skipped,
        "no_active_agent": int(fileserver_agent is None),
    }


async def _enqueue_proposal_jobs(queue: Any, batches: list[list[str]]) -> None:
    """Background coroutine to enqueue generate_proposals jobs for batched file IDs."""
    for idx, batch in enumerate(batches):
        await queue.enqueue("generate_proposals", file_ids=batch, batch_index=idx)


@router.post("/api/v1/analyze")
async def trigger_analysis(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue process_file jobs for all DISCOVERED files, routed per-file by duration (Phase 49
    D-06/D-11/D-12; Phase 50 CLOUDPIPE-01 reshape).

    Short/null-duration files route to the FILESERVER queue exactly as before. Long
    (``>= cloud_route_threshold_sec``) files are ALWAYS held in ``AWAITING_CLOUD`` -- there is no
    direct-to-compute enqueue here any more (see :func:`_route_discovered_by_duration`); the
    bounded ``stage_cloud_window`` controller cron is the sole entry to the compute pipeline.
    Short/null files with no fileserver agent online are reported ``skipped`` without aborting the
    run. One SAQ job per locally-routed file; the enqueues run in a background task (via the shared
    router helper) to avoid HTTP timeout on large file counts. Returns the split counts (``cloud``
    is always 0). The no-active-agent message is returned when NO fileserver agent is online
    (nothing can route locally) -- any long files are still committed to ``AWAITING_CLOUD``
    regardless.
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    if not files_with_duration:
        return {"enqueued": 0, "message": "No files in DISCOVERED state"}

    # Phase 71 (BEUI-02, D-08): fold the force-local override into the routing flag. The effective
    # cloud_enabled is ``registry cloud_enabled AND NOT force_local`` -- when forced, nothing is "long"
    # so every file routes local (byte-identical to an all-local registry), and no new row is held in
    # AWAITING_CLOUD. select_backend stays pure (untouched); the flag is read only here at the caller.
    effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)
    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        effective_cloud_enabled,
        settings.models_path,
    )

    if counts["no_active_agent"]:
        # Both kinds absent -- nothing was routable. Any long files were still committed to
        # AWAITING_CLOUD (surfaced via the count card); short/null files were skipped.
        return {
            "enqueued": 0,
            "local": 0,
            "cloud": 0,
            "awaiting_cloud": counts["awaiting"],
            "skipped": counts["skipped"],
            "message": _NO_ACTIVE_AGENT_MESSAGE,
        }

    enqueued = counts["local"] + counts["cloud"]
    return {
        "enqueued": enqueued,
        "local": counts["local"],
        "cloud": counts["cloud"],
        "awaiting_cloud": counts["awaiting"],
        "skipped": counts["skipped"],
        "message": (
            f"Enqueued {counts['local']} local, {counts['cloud']} cloud; "
            f"{counts['awaiting']} awaiting cloud, {counts['skipped']} skipped (no local agent)"
        ),
    }


@router.post("/api/v1/proposals/generate")
async def trigger_proposals(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue generate_proposals jobs for files with both metadata and analysis (per D-02 convergence gate).

    Uses settings.llm_batch_size (default 10) for batch chunking.
    """
    # Per D-02: convergence gate via the shared, deterministically-sorted pending-set helper
    # (D-03 anti-drift): recovery and this manual trigger build the SAME sorted batches, so their
    # generate_proposals:<sha256(sorted file_ids)> keys align and dedup (42-RESEARCH Pitfall 2).
    batches = await get_proposal_pending_batches(session, settings.llm_batch_size)
    total_files = sum(len(b) for b in batches)
    if not batches:
        return {"enqueued_batches": 0, "total_files": 0, "message": "No files ready for proposals (need both metadata and analysis)"}

    routed = await enqueue_router.resolve_queue_for_task("generate_proposals", request.app.state, session)
    task = asyncio.create_task(_enqueue_proposal_jobs(routed.queue, batches))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "enqueued_batches": len(batches),
        "total_files": total_files,
        "message": f"Enqueued {len(batches)} batches ({total_files} files) for proposal generation",
    }


async def build_dashboard_context(app_state: Any, session: AsyncSession) -> dict[str, Any]:
    """Build the pipeline-dashboard render context, shared by ``/pipeline/`` and the shell ``/`` Analyze node.

    Factored out of :func:`dashboard` (Phase 57, RESEARCH Open-Q2 / D-01) so the legacy
    dashboard page and the v7.0 shell's Analyze default render the SAME DAG content from a
    SINGLE source — there is no duplicated query logic that could drift between the two
    entry points. Returns every context key the dashboard template tree consumes EXCEPT
    ``request`` (each caller injects its own). ``app_state`` is ``request.app.state``.

    Every read here is degrade-safe at the service layer (the services own their never-500
    SAVEPOINT/``_safe_count`` fallbacks and the queue/counter reads isolate their own
    failures), so this builder never 500s the page.
    """
    # Phase 82 (D-05, READ-02): ONE get_stage_progress read feeds both the derived seven-key `stats`
    # dict (via _derive_stats -- replacing the removed FileRecord.state get_pipeline_stats) AND the
    # per-node DAG context (passed through to _build_dag_context so the heavy multi-count read happens
    # once). queue_progress_percent's numerator is the derived stats["analyzed"] (== analyze.done).
    stage_progress = await get_stage_progress(session)
    stats = _derive_stats(stage_progress)

    # Phase 27 D-05/D-06: agents for the Trigger Scan dropdown (non-revoked, ordered).
    # SER-01: exclude kind="compute" agents (Kueue/burst backends) — they are media-less
    # and cannot be scan targets, so they must never appear in the scan-picker.
    agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
    agents = (await session.execute(agents_stmt)).scalars().all()

    # Phase 27 D-05 / UI-SPEC Component 4: last 10 non-LIVE ScanBatches with their
    # transient UI attrs (_agent_name / _elapsed_seconds / _seconds_since_progress /
    # _is_stalled) attached. PR5 gap-14: the query + attachment lives in the shared
    # build_recent_scans helper so the dashboard and the delete endpoint cannot
    # drift apart (a duplicated copy once crashed this table on a tz-aware row).
    recent_scans_rows = await build_recent_scans(session)

    # Phase 34: live queue depth so an in-flight run is visible on first load (not only
    # after the first 5s poll tick). get_queue_activity isolates its own failures and
    # degrades to zeros, so no try/except is added here. queue_progress_percent precomputes
    # the DB-derived "Processing" bar percent (guarded against divide-by-zero) server-side
    # for unit-testability; the card (Plan 03) and the button gating (Plan 04) consume these.
    activity = await get_queue_activity(app_state, session)
    queue_progress = queue_progress_percent(stats["analyzed"], activity["agent_busy"])

    # Phase 35 (35-04): per-DAG-node done/total/active reconciled from get_stage_progress
    # (DB-truth) + the maintained completed counters (backstop) + the queue activity. The
    # 35-05 canvas seeds these into $store.pipeline on the full-page render; here they ride
    # the dashboard context. _build_dag_context isolates its own counter-source failures.
    # stage_progress is passed through so _build_dag_context reuses the same read (no 2nd query).
    dag_ctx = await _build_dag_context(app_state, session, activity, stage_progress)

    # Phase 44 (44-04): the STRAGGLER count (long-running in-flight process_file jobs,
    # "still grinding") and the ANALYSIS_FAILED count ("gave up") -- two distinct buckets
    # (44-02 D-02). Both reads are degrade-safe (the Plan-02 services own the never-500
    # SAVEPOINT/_safe_count degrade and return 0 on any DB error), so NO try/except is added
    # here -- same service-owns-degrade wiring idiom as the busy counts above (175-178).
    straggler_count = await get_straggler_count(session, settings.straggler_threshold_sec)
    analysis_failed_count = await get_analysis_failed_count(session)

    # Phase 49 (49-02, D-05): the "Awaiting cloud" held-file count -- long files held back
    # because no compute agent was online when analysis routed them. get_awaiting_cloud_count
    # owns the never-500 _safe_count degrade (returns 0 on any DB error), so NO try/except is
    # added here -- same service-owns-degrade wiring idiom as the straggler/failed counts above.
    awaiting_cloud_count = await get_awaiting_cloud_count(session)

    # Phase 50 (50-07, D-09): the two bounded cloud-window count cards -- "Staged (pushing)"
    # (FileState.PUSHING, mid-rsync) and "Analyzing (cloud)" (FileState.PUSHED, landed/within
    # analysis). Both service reads own the never-500 _safe_count degrade (return 0 on any DB
    # error), so NO try/except here -- same service-owns-degrade idiom as awaiting_cloud_count.
    pushing_count = await get_pushing_count(session)
    analyzing_cloud_count = await get_pushed_count(session)

    # Phase 54 (54-04, D-06): the Inadmissible operator alert count -- cloud_job rows the reconcile
    # cron flagged as Inadmissible (a misconfigured LocalQueue/ClusterQueue, NOT a healthy quota
    # wait). get_inadmissible_count owns the never-500 _safe_count degrade (returns 0 on any DB
    # error), so NO try/except here -- same service-owns-degrade idiom as awaiting_cloud_count.
    inadmissible_count = await get_inadmissible_count(session)

    # Phase 56 (56-02, D-05, KDEPLOY-04): the K8s LocalQueue-unreachable amber alert flag -- True when
    # the controller.startup probe (56-01) set the cross-process Redis key phaze:k8s:localqueue_unreachable.
    # get_localqueue_unreachable owns the never-500 degrade (returns False on a missing handle / any Redis
    # error), so NO try/except here -- the redis handle is read off app.state like the queue counters.
    # Seeded IDENTICALLY in pipeline_stats_partial() for the 5s OOB re-push.
    localqueue_unreachable = await get_localqueue_unreachable(getattr(app_state, "redis", None))

    # Phase 55 (55-05, D-04, KROUTE-06): the four per-cloud_phase admission-state counts driving the
    # admission_state_card. get_cloud_phase_counts owns the never-500 _safe_count degrade per phase
    # (returns 0 on any DB error), so NO try/except here -- same service-owns-degrade idiom as
    # inadmissible_count. Seeded IDENTICALLY in pipeline_stats_partial() for the 5s OOB re-push.
    cloud_phase_counts = await get_cloud_phase_counts(session)

    # phaze-5462: the Analyze workspace no longer server-renders ANY file rows inline. It used to seed
    # `analyze_files` here from get_analyze_working_set, whose "active working set" branch was UNBOUNDED
    # (10,132 rows / 12.7 MB in prod -- ~180x the metadata/fingerprint tabs, which render a ~70 KB shell
    # with zero rows). phaze-zqvh bounded only the completions window and trusted a docstring assertion
    # for the other half. The list now loads exactly like its siblings: the workspace ships an empty
    # #analyze-files-view that hx-gets GET /pipeline/analyze-files on load, which serves the BOUNDED,
    # paged working set. No DB read for the file list happens on this path at all any more.

    # quick 260622-i0w: the scanned/deduped reconciliation for the Discovery DAG-node subtitle.
    # Server-rendered on full-page load ONLY (the canvas is never OOB-swapped on the 5s poll); this
    # explains the Discovery COUNT(files) vs agent scan total gap as dedup, not lost work. The service
    # owns the never-500 degrade (returns {scanned: None, deduped: None} on any error), so NO
    # try/except here — same wiring idiom as get_queue_activity / dag_ctx above.
    recon = await get_global_reconciliation(session)

    # Phase 71 (71-03, BEUI-01 / D-04): the N-lane grid snapshot -- one rank-ascending, secret-free dict
    # per registry backend {id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}. Seeded
    # IDENTICALLY in pipeline_stats_partial() below so the WHOLE #analyze-lanes grid OOB-swaps on the SAME
    # existing 5s poll (no second loop, no new read endpoint -- Pitfall 2: N is dynamic, no per-lane store
    # keys). The snapshot helper owns the never-500 degrade (-> [] on any error), so NO try/except here --
    # same service-owns-degrade idiom as the cloud counts above. This SUPERSEDES the transitional single
    # non-local lane-kind key (retired); resolved_non_local_kind stays for the :811 callers.
    lanes = await get_backend_lane_snapshot(session)

    # The Cloud Routing card's truthful hold-reason sub-caption -- derived from the SAME lane snapshot
    # above via the SAME gate order the drain (stage_cloud_window) checks, so the card can never claim
    # a blocker the drain itself would not hit next tick. derive_cloud_hold_reason is fully degrade-safe
    # (collapses to the neutral "held" copy on any error), so NO try/except here -- same
    # service-owns-degrade idiom as the cloud counts above. Seeded IDENTICALLY in pipeline_stats_partial()
    # below so the OOB-swapped card re-push agrees with this first-load render (the OOB swap contract).
    awaiting_hold_reason = await derive_cloud_hold_reason(session)

    return {
        "stats": stats,
        "current_page": "pipeline",
        "settings_batch_size": settings.llm_batch_size,
        "agents": agents,
        "recent_scans": recent_scans_rows,
        "straggler_count": straggler_count,
        "analysis_failed_count": analysis_failed_count,
        "awaiting_cloud_count": awaiting_cloud_count,
        "awaiting_hold_reason": awaiting_hold_reason,
        "pushing_count": pushing_count,
        "analyzing_cloud_count": analyzing_cloud_count,
        "inadmissible_count": inadmissible_count,
        "localqueue_unreachable": localqueue_unreachable,
        "queued_behind_quota_count": cloud_phase_counts["queued_behind_quota"],
        "admitted_count": cloud_phase_counts["admitted"],
        "running_count": cloud_phase_counts["running"],
        "finished_count": cloud_phase_counts["finished"],
        "reconcile_scanned": recon["scanned"],
        "reconcile_deduped": recon["deduped"],
        # Phase 58 (58-04): the all-in-stage Analyze file list (D-03).
        # Phase 71 (71-03, BEUI-01 / D-04): the N-lane grid snapshot (seeded above, mirrored identically
        # in pipeline_stats_partial). Retires the transitional single non-local lane-kind context key.
        "lanes": lanes,
        **activity,
        **dag_ctx,
        "queue_progress_percent": queue_progress,
    }


@router.get("/pipeline/", response_class=HTMLResponse)
async def dashboard() -> RedirectResponse:
    """Redirect the legacy ``/pipeline/`` route to the v7.0 shell root.

    CUT-02 (Phase 62 / D-03b): ``/pipeline/`` was renamed to the shell root ``/`` in Phase
    57 (SHELL-01). The shell's Analyze default renders the live lane-card workspace
    (``/s/analyze``) and polls ``/pipeline/stats``; nothing hx-gets ``/pipeline/`` any more,
    so the legacy ``dashboard.html`` render path -- the ONE genuinely-dead HX branch in the
    cutover -- is removed and ``/pipeline/`` becomes a pure 302 redirect. The route stays
    registered so old bookmarks keep resolving into the shell. The DAG dashboard *context*
    still lives in :func:`build_dashboard_context`, which the shell Analyze render consumes.
    """
    return RedirectResponse(url="/", status_code=302)


@router.get("/pipeline/stats", response_class=HTMLResponse)
async def pipeline_stats_partial(
    request: Request,
    lane: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the stats bar partial for HTMX polling refresh.

    Phase 88 (88-01, DRILL-03 / D-02): the single persistent ``#pipeline-stats`` poll carries the
    pushed ``?lane=`` via ``hx-vals`` (shell.html), so this hot 5s tick re-emits the selected-lane
    highlight (aria-current + ring) on the matching card through the OOB ``_analyze_lanes`` grid.
    ``lane`` is resolved by lookup-in-known-set against the snapshot (T-88-01): an unknown/absent id
    resolves to ``None`` and highlights nothing — never a 422/500 into the poll.
    """
    # Phase 82 (D-05, READ-02): ONE get_stage_progress read feeds the derived seven-key `stats` dict
    # (via _derive_stats -- the removed FileRecord.state get_pipeline_stats) AND the per-node DAG
    # context below (passed through so the heavy multi-count read runs once on the hot 5s poll).
    stage_progress = await get_stage_progress(session)
    stats = _derive_stats(stage_progress)

    # Phase 95 (CONSOLE-04, DENORM-01 revisit): the ~12 reads below used to run as SERIAL awaits on
    # the shared request `session` (measured over the ~1s soft budget, D-07 -- see
    # .planning/milestones/2026.7.7-phases/95-analyze-view-browser-slowdown/). They are mutually
    # independent -- none consumes another's RESOLVED VALUE (derive_cloud_hold_reason re-derives its
    # own lane snapshot rather than reading the `lanes` result below) -- so they now fan out
    # CONCURRENTLY via asyncio.gather, mirroring the Phase 92 get_stage_progress pattern exactly:
    # each read runs in its OWN AsyncSession via _read_in_own_session, bounded by the SAME
    # _stats_fanout() cap (a fresh Semaphore(4) per poll in production; the test suite's
    # _route_stats_fanout fixture overrides _STATS_FANOUT to Semaphore(1) and routes
    # phaze.database.async_session onto the per-test connection, so this reuses that EXISTING
    # test-isolation seam with no new fixture). get_localqueue_unreachable needs no DB session (a
    # pure Redis read that already never raises), so it rides the SAME gather directly rather than
    # through _read_in_own_session.
    #
    # activity feeds queue_progress below AND is a required (if internally-unused-by-design --
    # see _build_dag_context's docstring) positional argument to _build_dag_context: a TRUE
    # dependency by signature, so _build_dag_context stays a sequential await AFTER this gather,
    # once activity is a resolved value rather than a pending coroutine. It reuses the shared
    # request `session` directly (unchanged from before this refactor) -- safe because nothing else
    # touches that session concurrently once the fan-out (which reads through its own sessions) is
    # under way.
    fanout = _stats_fanout()
    (
        activity,
        straggler_count,
        analysis_failed_count,
        awaiting_cloud_count,
        pushing_count,
        analyzing_cloud_count,
        inadmissible_count,
        localqueue_unreachable,
        cloud_phase_counts,
        lanes,
        awaiting_hold_reason,
        # asyncio.gather with >6 awaitables of mixed return types collapses to list[object] under
        # mypy (mirrors the identical cast in services/pipeline.py:get_stage_progress) -- pin the
        # exact per-read tuple shape with a single cast.
    ) = cast(
        "tuple[dict[str, int], int, int, int, int, int, int, bool, dict[str, int], list[dict[str, Any]], str]",
        await asyncio.gather(
            # Phase 34: surface live queue depth through the EXISTING 5s poll (no new loop).
            # get_queue_activity degrades to zeros on a Redis hiccup / missing app.state, so the
            # poll can never 500. queue_progress_percent (below) precomputes the guarded "Processing"
            # bar percent server-side; the OOB store-write nodes in stats_bar.html push agent_busy /
            # controller_busy into $store.pipeline on each tick to drive the Plan 04 button gating.
            _read_in_own_session(
                fanout,
                lambda s: get_queue_activity(request.app.state, s),
                {"agent_queued": 0, "agent_active": 0, "controller_queued": 0, "controller_active": 0, "agent_busy": 0, "controller_busy": 0},
            ),
            # Phase 44 (44-04): the same straggler + ANALYSIS_FAILED buckets the dashboard seeds,
            # re-pushed on every 5s poll so the straggler_failed_card stays live. Degrade-safe at the
            # service layer (44-02), so NO router try/except -- mirrors the dashboard() wiring.
            _read_in_own_session(fanout, lambda s: get_straggler_count(s, settings.straggler_threshold_sec), 0),
            _read_in_own_session(fanout, lambda s: get_analysis_failed_count(s), 0),
            # Phase 49 (49-02, D-05): the same AWAITING_CLOUD held count the dashboard seeds, re-pushed
            # on every 5s poll so the awaiting_cloud_card stays live via its OOB swap. Degrade-safe at the
            # service layer (Plan 01), so NO router try/except -- mirrors the straggler/failed wiring.
            _read_in_own_session(fanout, lambda s: get_awaiting_cloud_count(s), 0),
            # Phase 50 (50-07, D-09): the same PUSHING/PUSHED window counts the dashboard seeds, re-pushed
            # on every 5s poll so the staged_pushing_card / analyzing_cloud_card stay live via their OOB
            # swaps. Degrade-safe at the service layer, so NO router try/except -- mirrors the awaiting wiring.
            _read_in_own_session(fanout, lambda s: get_pushing_count(s), 0),
            _read_in_own_session(fanout, lambda s: get_pushed_count(s), 0),
            # Phase 54 (54-04, D-06): the same Inadmissible count the dashboard seeds, re-pushed on every 5s
            # poll so the inadmissible_card stays live via its OOB swap. Degrade-safe at the service layer,
            # so NO router try/except -- mirrors the awaiting_cloud_count wiring.
            _read_in_own_session(fanout, lambda s: get_inadmissible_count(s), 0),
            # Phase 56 (56-02, D-05, KDEPLOY-04): the same K8s LocalQueue-unreachable flag the dashboard seeds,
            # re-pushed on every 5s poll so the localqueue_card stays live via its OOB swap. Degrade-safe at the
            # service layer (56-01), so NO router try/except -- mirrors the inadmissible_count wiring; the redis
            # handle is read off app.state exactly like the dashboard() first-load path. No session needed --
            # runs directly (not through _read_in_own_session) rather than opening a DB connection for nothing.
            get_localqueue_unreachable(getattr(request.app.state, "redis", None)),
            # Phase 55 (55-05, D-04, KROUTE-06): the same four per-cloud_phase admission counts the dashboard
            # seeds, re-pushed on every 5s poll so the admission_state_card stays live via its OOB swap.
            # Degrade-safe at the service layer (per-phase _safe_count), so NO router try/except -- mirrors
            # the inadmissible_count wiring.
            _read_in_own_session(fanout, lambda s: get_cloud_phase_counts(s), {"queued_behind_quota": 0, "admitted": 0, "running": 0, "finished": 0}),
            # Phase 71 (71-03, BEUI-01 / D-04): the SAME N-lane snapshot the dashboard seeds, re-pushed on every
            # 5s poll so the WHOLE #analyze-lanes grid OOB-swaps as a unit (stats_bar.html includes _analyze_lanes
            # with oob=True inside the oob_counts gate). Seeded IDENTICALLY to build_dashboard_context (degrade-safe
            # -> [], never 500) -- one existing poll, no second loop, no new read endpoint.
            _read_in_own_session(fanout, lambda s: get_backend_lane_snapshot(s), cast("list[dict[str, Any]]", [])),
            # The SAME hold-reason derivation build_dashboard_context seeds on first load, re-pushed on every 5s
            # poll so the awaiting_cloud_card sub-caption stays live via its OOB swap (the OOB swap contract:
            # both render paths must agree). Degrade-safe at the service layer, so NO router try/except -- mirrors
            # the lanes wiring immediately above. "held" mirrors services.backends._HOLD_REASON_DEGRADED, the
            # SAME neutral no-causal-claim copy that function's own try/except already degrades to.
            _read_in_own_session(fanout, lambda s: derive_cloud_hold_reason(s), "held"),
        ),
    )
    queue_progress = queue_progress_percent(stats["analyzed"], activity["agent_busy"])
    # Phase 35 (35-04): same per-node reconcile as dashboard(), re-pushed on every 5s
    # poll via the OOB x-init seeds in stats_bar.html (gated behind oob_counts). The store
    # write keeps the 35-05 DAG bindings live without re-rendering the canvas or buttons.
    # stage_progress is passed through so _build_dag_context reuses the same read (no 2nd query).
    dag_ctx = await _build_dag_context(request.app.state, session, activity, stage_progress)
    # D-02 poll survival: resolve the pushed ?lane= by lookup-in-known-set (T-88-01) so the OOB
    # _analyze_lanes grid re-emits the selected ring only for a real, currently-rendered lane.
    selected_lane = lane if any(one.get("id") == lane for one in lanes) else None
    # Phase 95 (phaze-zqvh.3): the content hash of the grid's render inputs (lanes + selected highlight).
    # Emitted as data-lanes-hash so the client htmx:oobBeforeSwap hook SKIPS this OOB grid swap when the
    # state is byte-identical to what is already mounted -- bounding per-tick destroy-and-recreate churn
    # on a long-lived idle tab. Computed over the SAME inputs the initial render hashes, so the first tick
    # after an unchanged load is already a no-op swap.
    lanes_hash = analyze_lanes_content_hash(lanes, selected_lane)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/stats_bar.html",
        # oob_counts=True emits the hx-swap-oob "files ready" paragraphs ONLY on
        # this poll response. The dashboard full-page include omits the flag, so
        # the OOB block is skipped at initial load (where htmx would not honor
        # hx-swap-oob and the ids would collide with the DAG canvas seeds).
        context={
            "request": request,
            "stats": stats,
            "settings_batch_size": settings.llm_batch_size,
            "oob_counts": True,
            "straggler_count": straggler_count,
            "analysis_failed_count": analysis_failed_count,
            "awaiting_cloud_count": awaiting_cloud_count,
            "awaiting_hold_reason": awaiting_hold_reason,
            "pushing_count": pushing_count,
            "analyzing_cloud_count": analyzing_cloud_count,
            "inadmissible_count": inadmissible_count,
            "localqueue_unreachable": localqueue_unreachable,
            "queued_behind_quota_count": cloud_phase_counts["queued_behind_quota"],
            "admitted_count": cloud_phase_counts["admitted"],
            "running_count": cloud_phase_counts["running"],
            "finished_count": cloud_phase_counts["finished"],
            "lanes": lanes,
            "selected_lane": selected_lane,
            "lanes_hash": lanes_hash,
            **activity,
            **dag_ctx,
            "queue_progress_percent": queue_progress,
        },
    )


@router.get("/pipeline/lanes/{backend_id}", response_class=HTMLResponse)
async def lane_detail(
    request: Request,
    backend_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the lane-detail body fragment for a backend lane (DRILL-01 / D-06 / D-07 / D-00b).

    Swapped as innerHTML into the shared ``#detail-pane`` (88-01 shell). ``backend_id`` is operator-
    declared, so it is resolved by lookup-in-known-set against the degrade-safe
    :func:`get_backend_lane_snapshot` (T-88-03): an unknown/offline id renders the friendly "Lane
    offline" empty fragment (200, HTML -- never a 500/JSON/HTTPException, never a raw-param-driven read),
    so htmx still swaps a body into the pane. For a resolved lane the kind-adaptive body renders the
    last ``LANE_RECENT_N`` newest-first succeeded completions (compute/kueue only, D-07) and the per-lane
    queue depths; every read is bounded + degrade-safe (D-00b) and the body carries its own bounded 5s
    tick (D-03). Read-only -- no commit. Only secret-free snapshot scalars + completion status/timestamps
    leave here (T-88-04); ``backend_id``/``kind`` stay Jinja-autoescaped (T-88-05).
    """
    lanes = await get_backend_lane_snapshot(session)  # degrade-safe -> []
    lane = next((one for one in lanes if one["id"] == backend_id), None)
    if lane is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_lane_detail.html",
            context={
                "lane": None,
                "backend_id": backend_id,
                "recent_completions": [],
                "queue_depths": {},
                "refreshed_at": None,
                "recent_n": LANE_RECENT_N,
            },
        )
    recent_completions = await get_lane_recent_completions(session, backend_id, lane["kind"])
    queue_depths = await get_lane_queue_depths(request.app.state, backend_id)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_lane_detail.html",
        context={
            "lane": lane,
            "backend_id": backend_id,
            "recent_completions": recent_completions,
            "queue_depths": queue_depths,
            "refreshed_at": datetime.now(UTC),
            "recent_n": LANE_RECENT_N,
        },
    )


_VALID_BUCKETS: frozenset[str] = frozenset(s.value for s in Status)


@router.get("/pipeline/files", response_class=HTMLResponse)
async def pipeline_files(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    stage: str | None = Query(None),
    bucket: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the paginated, per-row-derived files table (UI-01 / D-02).

    The scannable "where's this file at?" overview: each row carries the six-pill stage matrix
    derived per page (never the raw ``files.state`` column string, never a whole-corpus scan per poll
    -- see :func:`phaze.services.pipeline.get_files_page`). The ``stage``+``bucket`` query params are
    validated against the ``Stage`` / ``Status`` allowlists (T-87-14 -- an unknown value degrades to
    an unfiltered page rather than 422-ing the poll) and plumbed through NOW so Plan 05's status-filter
    bar is templates-only. The read is SAVEPOINT degrade-safe at the service layer, so NO router
    try/except -- a DB hiccup renders a safe empty page, never a 500.
    """
    stage_enum: Stage | None = None
    if stage:
        try:
            stage_enum = Stage(stage)
        except ValueError:
            stage_enum = None
    bucket_val = bucket if bucket in _VALID_BUCKETS else None
    files_page = await get_files_page(session, page=page, page_size=page_size, stage=stage_enum, bucket=bucket_val)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/files_table_view.html",
        context={
            "files_page": files_page,
            "active_stage": stage_enum.value if stage_enum is not None else None,
            "active_bucket": bucket_val,
        },
    )


@router.get("/pipeline/analyze-files", response_class=HTMLResponse)
async def analyze_files_fragment(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    status: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the Analyze per-file table fragment: the bounded default working set OR a filtered page (phaze-zqvh.2).

    The status-filter bar in ``_analyze_files.html`` hx-gets this endpoint into ``#analyze-files-view`` --
    the SAME URL-carried-lens idiom as ``/pipeline/files`` / ``_status_filter_bar.html``. ``status`` is
    validated against the ``ANALYZE_FILTERS`` allowlist (T-57-01 / T-87-14: an unknown value NEVER reaches
    a template path or SQL string -- it degrades to the default view, never a 422 into the render):

      * no / unknown ``status`` -> the DEFAULT bounded working-set view (the active-first working set,
        PAGED, plus the LIMIT-ed recent-completions window on the final page), with a pager.
      * a valid ``status`` -> the full analyze-stage listing under that lens, served as a bounded page
        (``get_analyze_files_page``: OFFSET + ``page_size + 1`` sentinel, never a whole-corpus COUNT).

    Both service reads are SAVEPOINT degrade-safe (never 500 the fragment). This endpoint is a SIBLING of
    the 5s ``/pipeline/stats`` poll -- it is NEVER in the poll's OOB fan-out, so the operator's page position
    and filter selection survive every tick (the file grid stays outside the poll, phaze-zqvh.2 acceptance).
    """
    status_val = status if status in ANALYZE_FILTERS else None
    if status_val is None:
        # DEFAULT bounded working-set view (no explicit filter): the active-first set, PAGED, with the
        # completions window appended on the final page. phaze-5462: this branch is now paged like every
        # other -- it used to return the whole unbounded working set.
        working_set = await get_analyze_working_set(session, page=page, page_size=page_size)
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_analyze_files.html",
            context={
                "analyze_rows": working_set.rows,
                "analyze_page": working_set,
                "active_status": None,
            },
        )
    analyze_page = await get_analyze_files_page(session, page=page, page_size=page_size, status=status_val)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_analyze_files.html",
        context={
            "analyze_rows": analyze_page.rows,
            "analyze_page": analyze_page,
            "active_status": status_val,
        },
    )


# phaze-5462: the stage allowlist for the shared pending-files fragment. Validated as a SET so an
# unknown value can NEVER reach SQL or a template path (T-57-01 / T-87-14) -- it degrades to
# "metadata", never a 422 into the render.
_PENDING_STAGES: dict[str, Stage] = {"metadata": Stage.METADATA, "fingerprint": Stage.FINGERPRINT}


@router.get("/pipeline/pending-files", response_class=HTMLResponse)
async def pending_files_fragment(
    request: Request,
    stage: str = Query("metadata"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render ONE bounded page of the Metadata / Fingerprint pending set (phaze-5462).

    The sibling of :func:`analyze_files_fragment`, on the SAME paging contract
    (:mod:`phaze.services.pagination`): shared page size, OFFSET paging, a ``page_size + 1`` sentinel
    for ``has_next`` (never a whole-corpus COUNT), and the mandatory unique tiebreaker. Both enrich
    workspaces hx-get this on load into their empty host div, so neither server-renders a file row
    inline any more.

    ``stage`` is validated against :data:`_PENDING_STAGES` (unknown -> metadata) and is carried into
    the template only as an autoescaped query value -- never a template path (T-57-01).

    NOTE: this is the RENDER read. The EXTRACT ALL / FINGERPRINT ALL buttons still enqueue the
    UNBOUNDED pending set (paging contract rule 7) -- paging the enqueue would silently drop work.
    """
    stage_key = stage if stage in _PENDING_STAGES else "metadata"
    pending_page = await get_pending_files_page(session, _PENDING_STAGES[stage_key], page=page, page_size=page_size)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_pending_files.html",
        context={
            "pending_page": pending_page,
            "stage": stage_key,
            "host_id": f"{stage_key}-files-view",
        },
    )


@router.get("/pipeline/trackid-files", response_class=HTMLResponse)
async def trackid_files_fragment(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render ONE bounded page of the Track-ID identity table (phaze-1wvb).

    The sibling of :func:`analyze_files_fragment` / :func:`pending_files_fragment`, on the SAME
    paging contract (:mod:`phaze.services.pagination`): shared page size, OFFSET paging, a
    ``page_size + 1`` sentinel for ``has_next`` (never a whole-corpus COUNT), and the mandatory
    unique tiebreaker. The Track-ID workspace hx-gets this into its empty host div on load, so it no
    longer server-renders a single identity row inline.

    NOTE (paging contract rule 7): there is no enqueue behind this read to under-serve -- the
    Track-ID workspace is READ-ONLY (it has no trigger at all), and the neighbouring Tracklist
    workspace's bulk triggers keep reading their own UNBOUNDED pending sets.
    """
    trackid_page = await get_trackid_files_page(session, page=page, page_size=page_size)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_trackid_files.html",
        context={"trackid_page": trackid_page, "host_id": "trackid-files-view"},
    )


@router.get("/pipeline/tracklist-sets", response_class=HTMLResponse)
async def tracklist_sets_fragment(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render ONE bounded page of the per-set Tracklist coverage table (phaze-1wvb).

    Same paging contract as :func:`trackid_files_fragment`. The Tracklist workspace hx-gets this into
    the empty host div BELOW its three step cards; the step cards themselves (and their SEARCH /
    SCRAPE / MATCH ALL triggers, which enqueue the UNBOUNDED pending sets -- rule 7) are untouched
    and still server-rendered by the shell.
    """
    sets_page = await get_tracklist_sets_page(session, page=page, page_size=page_size)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_tracklist_sets.html",
        context={"sets_page": sets_page, "host_id": "tracklist-sets-view"},
    )


@router.post("/pipeline/analyze", response_class=HTMLResponse)
async def trigger_analysis_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger per-file duration-routed analysis and return the split-count fragment (Phase 49; Phase 50 CLOUDPIPE-01 reshape).

    Mirrors :func:`trigger_analysis`: short/null files route to the fileserver as before; long
    files are ALWAYS held in ``AWAITING_CLOUD`` (no direct-to-compute enqueue -- see
    :func:`_route_discovered_by_duration`), and short/null files with no fileserver online are
    skipped without aborting the run. The fragment reports ``N local, M cloud, K awaiting cloud``
    (+ a skipped bucket); ``cloud`` is always 0. The no-active-agent fragment is rendered when NO
    fileserver agent is online (nothing can route locally).
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    count = len(files_with_duration)

    if count == 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/trigger_response.html",
            context={"request": request, "action": "analysis", "count": 0, "no_active_agent": False},
        )

    # Phase 71 (BEUI-02, D-08): same force-local fold as the JSON trigger -- effective cloud_enabled is
    # ``registry cloud_enabled AND NOT force_local``, so a forced registry routes every file local.
    effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)
    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        effective_cloud_enabled,
        settings.models_path,
    )

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={
            "request": request,
            "action": "analysis",
            "count": count,
            "no_active_agent": bool(counts["no_active_agent"]),
            "split_counts": True,
            "local": counts["local"],
            "cloud": counts["cloud"],
            "awaiting": counts["awaiting"],
            "skipped": counts["skipped"],
        },
    )


@router.post("/pipeline/backfill-cloud", response_class=HTMLResponse)
async def trigger_backfill_cloud(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: backfill the timed-out long files to the cloud (Phase 49, D-08/D-10; 83-06 REVERSES D-09).

    Selects EXACTLY the timed-out long set — ``ANALYSIS_FAILED ∧ duration >= cloud_route_threshold_sec``
    (the explicit :func:`count_backfill_candidates` / :func:`get_backfill_candidates` filter, NOT a
    whole-backlog ``ANALYSIS_FAILED`` sweep), and routes the candidates through the SAME per-file
    duration router (:func:`_route_discovered_by_duration`) "Run Analysis" uses, so the two paths cannot
    drift: every candidate is long, so the router HOLDS it in ``AWAITING_CLOUD`` (an awaiting
    ``cloud_job`` sidecar row), never a direct enqueue.

    83-06 (OPTION A, CONSCIOUSLY REVERSES D-09 — accepted by the phase owner): the held file is made a
    CLEAN drainable candidate for BOTH cloud targets (compute AND kueue; the all-local case early-returns
    below). After the hold, in one transaction, the endpoint (1) CLEARS the ``analysis.failed_at`` /
    ``error_message`` marker (mirrors :func:`retry_analysis_failed`) and (2) DELETES the orphaned
    ``process_file:<id>`` scheduling-ledger row, KEEPING only the awaiting ``cloud_job`` row as the SOLE
    in-flight/recovery registry (exactly like a normal "Run Analysis"-held file and the k8s path). The
    former D-09 held-file ledger SEED (compute) / SKIP (kueue) fork is GONE — neither branch seeds a
    ledger row now, so the compute/kueue paths are unified.

    WHY the reversal is safe (net over-enqueue REDUCTION): a RETAINED ``failed_at`` made the held file
    analyze-domain-completed and a RETAINED ledger row made it analyze-in-flight, so
    ``awaiting_candidate_clause`` (``~inflight ∧ ~domain_completed``) EXCLUDED it and
    :func:`stage_cloud_window` never drained it (83-06). Clearing both markers lets the bounded drain
    dispatch it to the compute/kueue backend — the single owner. D-09's stated ledger-replay recovery
    purpose was ALREADY dead: ``analysis.failed_at`` put the held file in ``recover_orphaned_work``'s
    analyze domain-completed exclusion, so the seeded row was never replayed.

    The explicit ``failed_clause(ANALYZE)`` filter plus the ``~exists(active cloud_job)`` idempotency
    guard in :func:`_backfill_candidates_stmt` still close the over-enqueue class (D-10): a double-click
    selects nothing new (the held files now carry an awaiting ``cloud_job`` row), and short / never-failed
    files are never touched.
    """
    # Phase 51 (D-03, Pitfall 2 / T-51-02): explicit cloud on/off guard BEFORE the candidate query.
    # Gating only the routing seam is insufficient -- backfill would still reset the 144
    # ANALYSIS_FAILED long files to DISCOVERED and re-route them local to re-time-out. When the
    # registry is all-local (cloud_enabled False, Phase 67 / D-14) this is a clean no-op that mutates
    # ZERO file.state rows -- byte-identical to the former all-local selector guard.
    # Phase 71 (BEUI-02, D-08, T-71-08): the force-local override is the THIRD gate site. Forced-local
    # must behave EXACTLY like the all-local path here too -- otherwise backfill would reset the failed
    # long files to DISCOVERED and HOLD them in AWAITING_CLOUD while the (forced) drain no-ops, stranding
    # them. Folding force_local into this same early-return keeps backfill a clean ZERO-mutation no-op.
    if not settings.cloud_enabled or await get_route_control(session):
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/backfill_response.html",
            context={"request": request, "count": 0, "disabled": True},
        )

    threshold = settings.cloud_route_threshold_sec
    count = await count_backfill_candidates(session, threshold)
    if count == 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/backfill_response.html",
            context={"request": request, "count": 0},
        )

    candidates = await get_backfill_candidates(session, threshold)

    # 83-06 (OPTION A, CONSCIOUSLY REVERSES D-09): make every cloud-routed backfill candidate a CLEAN
    # drainable held file, for BOTH the compute AND the kueue target (the all-local case already
    # returned early above). The hold (``hold_awaiting_cloud`` inside ``_route_discovered_by_duration``)
    # and the two marker strips below MUST land in ONE transaction:
    #   1. Clear ``analysis.failed_at`` / ``error_message`` (mirrors :func:`retry_analysis_failed`): a
    #      RETAINED marker made the held file analyze-domain-completed, so ``~domain_completed_clause``
    #      was False and the drain skipped it.
    #   2. DELETE the orphaned ``process_file:<id>`` ledger row (the backfill candidate query REQUIRES
    #      it): its presence made the file analyze-in-flight, so ``~inflight_clause`` was False.
    # phaze-7g4t: STAGE the marker strips BEFORE routing (do NOT commit them separately). The old code
    # committed the holds inside ``_route_discovered_by_duration`` and THEN committed the marker strips
    # in a SECOND transaction -- an interruption (DB error on the UPDATE/DELETE, server restart, or
    # handler-task cancellation) between the two commits left every candidate as
    # {cloud_job='awaiting' + failed_at set + ledger row}, which every forward path excludes: the drain
    # never picks it (retained ledger => in-flight, retained failed_at => domain-completed), the
    # Awaiting-cloud card shows 0, re-running Backfill selects nothing (active cloud_job), Run Analysis
    # skips it (~failed conjunct), and recovery excludes any awaiting cloud_job -- a permanent invisible
    # strand. Staging the strips into the session first means the hold's single commit inside
    # ``_route_discovered_by_duration`` flushes ALL THREE mutations atomically. Every backfill candidate
    # is long (the query filters ``duration >= threshold``) and cloud is enabled here, so the router
    # ALWAYS holds >=1 file and therefore ALWAYS commits -- the staged strips can never be left dangling.
    candidate_ids = [file.id for file, _ in candidates]
    if candidate_ids:
        await session.execute(
            update(AnalysisResult).where(AnalysisResult.file_id.in_(candidate_ids)).values(failed_at=None, error_message=None),
        )
        await session.execute(
            delete(SchedulingLedger).where(SchedulingLedger.key.in_([process_file_job_key(fid) for fid in candidate_ids])),
        )

    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        candidates,
        threshold,
        # cloud is enabled here: the `not settings.cloud_enabled` early-return guard above already
        # short-circuited the all-local case, so the registry holds a non-local backend and the router
        # must hold the long files for the cloud path. Pass True unconditionally.
        True,
        settings.models_path,
    )

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/backfill_response.html",
        context={
            "request": request,
            "count": count,
            "cloud": counts["cloud"],
            "awaiting": counts["awaiting"],
        },
    )


@router.post("/pipeline/analysis-failed/retry", response_class=HTMLResponse)
async def retry_analysis_failed(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated BULK retry of every ANALYSIS_FAILED file (quick-260707-d79).

    ANALYSIS_FAILED is a terminal state that ``recover_orphaned_work`` deliberately treats as
    analyze-DONE (:func:`phaze.tasks.reenqueue._select_done_analyze_ids`), so a genuinely
    un-analyzable file is never auto-looped. This endpoint is that invariant's deliberate,
    operator-gated counterpart: it re-drives EVERY ANALYSIS_FAILED file through the SAME guarded
    funnel :func:`deepen_analysis` uses -- per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`enqueue_process_file` (COMPLETE ``ProcessFilePayload`` + deterministic
    ``process_file:<id>`` key) -- but with NORMAL caps: a retry is a fresh re-analysis, NOT a
    deepen, so ``fine_cap`` / ``coarse_cap`` are left at their None default (the standard 60/30
    window budget), not the deepen sentinel 0.

    Ordering follows the Phase-30 / RESEARCH-Pitfall-3 guards:
    - Resolve the per-agent queue ONCE. ``process_file`` is an AGENT_TASK; if no agent is online
      ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT flipping any
      state or enqueuing -- it never falls through to the consumer-less default queue.
    - Clear the ``analysis.failed_at`` / ``error_message`` marker, then ``commit`` BEFORE any
      enqueue (get_session does NOT auto-commit): the files leave the red bucket immediately;
      ``put_analysis`` clears it again (a no-op) on success, or ``report_analysis_failed``
      re-stamps it only if it fails AGAIN. Phase 90 (D-09) removed the companion
      ``FileRecord.state = FINGERPRINTED`` reset -- clearing the ``analysis`` marker is now the
      sole required mutation (see the inline note below).
    - The deterministic key dedups any file with a live in-flight job to a no-op, so re-enqueuing
      the WHOLE failed set is safe (dedup-safe; no silent cap).
    """
    files = await get_analysis_failed_files(session)
    if not files:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        # Do NOT flip state, do NOT enqueue, do NOT fall through to the default queue (Phase-30).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # process_file is an AGENT_TASK -- resolve always returns a non-None agent_id;
    # cast narrows str | None -> str for ProcessFilePayload.agent_id.
    agent_id = cast("str", routed.agent_id)

    # RESEARCH Pitfall 3: flip out of the terminal bucket and COMMIT before any enqueue so the
    # red count drops on the next 5s poll regardless of the enqueue outcome.
    #
    # Clear the durable `analysis.failed_at` marker and COMMIT before any enqueue so the red count
    # drops on the next 5s poll. Phase 90 (D-09): the paired `files.state = FINGERPRINTED` reset was
    # removed -- `analysis.failed_at` is now the sole failure authority (`failed_clause(Stage.ANALYZE)`,
    # readers cut over in PR-A). Clearing it moves the row off the failed disjunct so it derives
    # `not_started` -- exactly what a fresh re-analysis should see (the XOR CHECK guarantees
    # `analysis_completed_at IS NULL` on a failed row).
    await session.execute(
        update(AnalysisResult).where(AnalysisResult.file_id.in_([f.id for f in files])).values(failed_at=None, error_message=None),
    )
    await session.commit()

    for f in files:
        # NORMAL caps: NO fine_cap/coarse_cap override -- a retry is a fresh re-analysis, not a
        # deepen. The single funnel guarantees the full payload + deterministic dedup key.
        await enqueue_process_file(routed.queue, f, agent_id, settings.models_path)

    logger.info("retry_analysis_failed re-queued files", count=len(files))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/retry_failed_response.html",
        context={"request": request, "count": len(files), "no_active_agent": False},
    )


@router.post("/pipeline/metadata-failed/retry", response_class=HTMLResponse)
async def retry_metadata_failed(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated BULK retry of every terminally-failed metadata file (FAIL-03).

    Closes gap G-01 (SC#3): a metadata failure persisted by the 81-03 writer (``metadata`` row
    with ``failed_at`` set, payload NULL) derives FAILED and would otherwise be a permanent
    dead-end blocking the file from ever reaching ``propose``. This is the operator-gated retry:
    it re-drives EVERY ``metadata.failed_at IS NOT NULL`` file through the SAME guarded funnel the
    manual metadata triggers use -- per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`_enqueue_extraction_jobs` (the COMPLETE ``ExtractMetadataPayload``, not a
    dead-lettering file_id-only enqueue) + the central deterministic ``extract_file_metadata:<id>``
    dedup key.

    It mirrors :func:`retry_analysis_failed`'s Phase-30-hardened ordering, MINUS the state flip:
    - Resolve the per-agent queue ONCE. ``extract_file_metadata`` is an AGENT_TASK; if no agent is
      online ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT enqueuing
      or mutating any state -- it never falls through to the consumer-less default queue (Phase-30).
    - D-11: NO ``f.state`` flip. Metadata has no terminal FileState -- the failure lives only in the
      ``metadata`` failure row. The row is LEFT in place; clearing ``failed_at`` here would make a
      zero-metadata file read DONE forever. ``put_metadata``'s clear-on-success (81-03) wipes the
      marker only when real metadata lands, or ``report_metadata_failed`` re-stamps it on another
      failure. With no state mutation there is nothing to commit before the enqueue.
    - The deterministic key dedups any file with a live in-flight job to a no-op, so re-enqueuing
      the WHOLE failed set is safe (dedup-safe; no silent cap).
    """
    files = await get_metadata_failed_files(session)
    if not files:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        # Do NOT enqueue, do NOT mutate state, do NOT fall through to the default queue (Phase-30).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # extract_file_metadata is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    # D-11: no state flip, so no pre-enqueue commit. Build the COMPLETE payload via the shared
    # producer (a file_id-only enqueue dead-letters every job) and rely on the central
    # extract_file_metadata:<file_id> key for in-flight dedup.
    await _enqueue_extraction_jobs(routed.queue, files, agent_id)

    logger.info("retry_metadata_failed re-queued files", count=len(files))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/metadata_retry_response.html",
        context={"request": request, "count": len(files), "no_active_agent": False},
    )


# --------------------------------------------------------------------------------------------------
# Per-file scoped retry variants (87-07 / UI-02 / D-04): the console's per-row Retry on a failed
# enrich cell. Each re-drives ONE file through the SAME Phase-30-hardened guarded funnel the bulk
# endpoints use (``enqueue_router.resolve_queue_for_task`` -> ``NoActiveAgentError`` guard ->
# enqueue), filtered to a single ``file_id`` instead of the whole failed set, and reuses the bulk
# response partials VERBATIM (a count of 1 / 0). The analyze variant preserves the manual-only
# terminal-analyze path (``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``): it flips ANALYSIS_FAILED ->
# FINGERPRINTED + clears ``analysis.failed_at`` in ONE transaction and commits BEFORE enqueue (the
# Phase-81 CR-01 rule) so the file leaves the failed disjunct -- it NEVER creates an auto-retry loop
# (the 44.5K over-enqueue guard, behavior 8).
# --------------------------------------------------------------------------------------------------
@router.post("/pipeline/files/{file_id}/analysis-failed/retry", response_class=HTMLResponse)
async def retry_analysis_failed_file(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated PER-FILE retry of ONE ANALYSIS_FAILED file (87-07, UI-02 / D-04).

    The scoped twin of :func:`retry_analysis_failed`: it re-drives EXACTLY ONE file through the
    identical guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`enqueue_process_file` with the COMPLETE ``ProcessFilePayload`` + deterministic
    ``process_file:<id>`` key), scoped by ``id == file_id AND`` the derived terminal analyze-failure
    marker (``failed_clause(Stage.ANALYZE)``, Phase 90 PR-A -- no longer the retired
    ``files.state == ANALYSIS_FAILED`` column) so a non-failed (or unknown) file is a safe no-op ack
    (T-87-27 input validation — a UUID path param + the failure-marker guard, never an unscoped
    enqueue).

    MANUAL-ONLY, no auto-loop (D-00b, behavior 8, T-87-24): analyze is the ONLY enrich carve-out
    (``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``) — a FAILED analyze is terminal and is NEVER
    auto-retried by ``recover_orphaned_work`` / the derived pending set. This endpoint is that
    invariant's deliberate operator-gated counterpart: it clears the ``analysis.failed_at`` /
    ``error_message`` marker, then ``commit`` BEFORE the enqueue (``get_session`` does NOT
    auto-commit) so the file leaves the failed disjunct immediately and derives ``not_started`` for a
    fresh re-analysis. Phase 90 (D-09) removed the companion ``FileRecord.state = FINGERPRINTED``
    reset -- clearing the ``analysis`` marker is now the sole required mutation. The deterministic key
    dedups a live in-flight job to a no-op (T-87-26). The ack is count/bool-only — no operator
    free-text crosses into Jinja (T-d79-04).
    """
    file = (
        # Phase 90 (PR-A, D-09): scope on the DERIVED terminal analyze-failure marker
        # (``failed_clause(Stage.ANALYZE)`` -- an analysis row with ``failed_at`` set), no longer the
        # retired ``files.state == ANALYSIS_FAILED`` column. A non-failed (or unknown) file is a safe no-op.
        await session.execute(select(FileRecord).where(FileRecord.id == file_id, failed_clause(Stage.ANALYZE)))
    ).scalar_one_or_none()
    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        # Do NOT flip state, do NOT enqueue, do NOT fall through to the default queue (Phase-30, T-87-25).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # process_file is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    # CR-01 (Phase 81): clear the durable `analysis.failed_at` marker and COMMIT before the enqueue so
    # the row leaves the failed disjunct and derives not_started (a fresh re-analysis). Phase 90 (D-09):
    # the paired `files.state = FINGERPRINTED` reset was removed -- `analysis.failed_at` is now the sole
    # failure authority (`failed_clause(Stage.ANALYZE)`, readers cut over in PR-A).
    await session.execute(
        update(AnalysisResult).where(AnalysisResult.file_id == file_id).values(failed_at=None, error_message=None),
    )
    await session.commit()

    # NORMAL caps: a retry is a fresh re-analysis, not a deepen -- no fine_cap/coarse_cap override.
    await enqueue_process_file(routed.queue, file, agent_id, settings.models_path)

    logger.info("retry_analysis_failed_file re-queued", file_id=str(file_id))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/retry_failed_response.html",
        context={"request": request, "count": 1, "no_active_agent": False},
    )


@router.post("/pipeline/files/{file_id}/metadata-failed/retry", response_class=HTMLResponse)
async def retry_metadata_failed_file(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated PER-FILE retry of ONE terminally-failed metadata file (87-07, UI-02 / D-04).

    The scoped twin of :func:`retry_metadata_failed`: it re-drives EXACTLY ONE file through the
    identical guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`_enqueue_extraction_jobs` with the COMPLETE ``ExtractMetadataPayload`` + the central
    ``extract_file_metadata:<id>`` dedup key), scoped by ``id == file_id AND EXISTS(a metadata row
    with failed_at)`` so a non-failed (or unknown) file is a safe no-op ack (T-87-27).

    D-11: NO ``f.state`` flip and NO ``failed_at`` clear — metadata has no terminal FileState and the
    failure lives only in the ``metadata`` row; clearing it here would make a zero-metadata file read
    DONE forever. ``put_metadata``'s clear-on-success wipes the marker only when real metadata lands.
    With no state mutation there is nothing to commit before the enqueue. The deterministic key dedups
    a live in-flight job to a no-op (T-87-26). The ack is count/bool-only (T-d79-04).
    """
    file = (
        await session.execute(
            select(FileRecord).where(
                FileRecord.id == file_id,
                exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None))),
            ),
        )
    ).scalar_one_or_none()
    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        # Do NOT enqueue, do NOT mutate state, do NOT fall through to the default queue (Phase-30, T-87-25).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/metadata_retry_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # extract_file_metadata is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    # D-11: no state flip, so no pre-enqueue commit. The shared producer builds the COMPLETE payload
    # and the central extract_file_metadata:<file_id> key dedups an in-flight job.
    await _enqueue_extraction_jobs(routed.queue, [file], agent_id)

    logger.info("retry_metadata_failed_file re-queued", file_id=str(file_id))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/metadata_retry_response.html",
        context={"request": request, "count": 1, "no_active_agent": False},
    )


# --------------------------------------------------------------------------------------------------
# Force-skip writer (UI-04 / D-08/D-09/D-10): the right-pane escape hatch that lets the ``failed``
# bucket converge for genuinely-unprocessable files. The correctness-sensitive mutating endpoint of
# this phase: enrich-only (approval-bypass hazard, D-10), additive (never clears a failure marker, so
# the Phase-79 shadow-compare gate stays green), reason required + sanitized (NUL-abort footgun), and
# committed (get_session NEVER auto-commits).
# --------------------------------------------------------------------------------------------------
@router.post("/pipeline/files/{file_id}/skip/{stage}", response_class=HTMLResponse)
async def force_skip_stage(
    file_id: uuid.UUID,
    stage: str,
    reason: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Force-skip an ENRICH stage for one file: write a distinct ``skipped`` marker (UI-04, D-08/D-09/D-10).

    The escape hatch that lets the ``failed`` bucket converge for genuinely-unprocessable files. It is
    deliberately NOT ``done``: a ``stage_skip`` marker row derives the honest ``skipped`` bucket, which
    stays distinguishable from real completion forever (D-08). Discipline (mirrors the retry endpoints +
    ``pipeline_stages._validate_stage``):

    - ENRICH-ONLY (D-10, T-87-18): ``stage`` must be in :data:`STAGE_TO_FUNCTION`
      (metadata/analyze/fingerprint) — a ``propose``/``review``/``apply`` skip is an approval-bypass
      hazard and returns 422 BEFORE any write, backstopped by the Plan-01 DB CHECK.
    - REASON REQUIRED (D-09, T-87-22): a blank/whitespace reason returns the inline validation fragment
      with NO write.
    - SANITIZED (T-87-19): ``sanitize_pg_text`` strips NUL / lone surrogates before persist — a NUL in
      free text passes pydantic then aborts the PG txn (the unbounded-recovery-loop footgun).
    - ADDITIVE-ONLY (T-87-20): the writer ONLY adds the marker row; it NEVER clears ``analysis.failed_at``
      or any failure marker, so a terminally-failed stage keeps its failure fact and the shadow-compare
      gate stays green. Precedence (``done ≻ skipped ≻ failed``) — not the writer — decides the bucket.
    - COMMITTED (Pitfall 7): ``get_session`` does NOT auto-commit, so the writer commits itself.

    The pill flips to ``⊘ skipped`` on the NEXT poll tick (not optimistic) — the ack is a toast only.
    ``reason`` is never echoed back into the response (T-87-21 — no XSS surface via the free text).
    """
    if stage not in STAGE_TO_FUNCTION:  # D-10 enrich-only — mirror pipeline_stages._validate_stage
        raise HTTPException(status_code=422, detail="stage not force-skippable")
    # Sanitize BEFORE the blank check (WR-01): str.strip() alone does not remove NUL / control chars, so a
    # NUL-only reason would slip past a raw-input gate and then persist as "". Validate the SANITIZED value.
    clean_reason = sanitize_pg_text(reason).strip()  # project memory: NUL aborts the PG txn (services/pg_text.py)
    if not clean_reason:  # D-09 reason required — inline validation on the sanitized value, NO write
        return HTMLResponse(
            '<p class="text-sm font-medium text-red-600 dark:text-red-400" role="alert">A reason is required.</p>',
            status_code=422,
        )
    # Idempotent additive write (CR-01): re-submitting a force-skip for the same (file, stage) is a NORMAL
    # path — `_force_skip_dialog.html` is not hidden after success — and the UNIQUE(file_id, stage) constraint
    # would turn a bare INSERT into an unhandled IntegrityError → HTTP 500. on_conflict_do_nothing mirrors
    # `insert_ledger_if_absent`: the marker's existence IS the desired end state, so a duplicate is a no-op
    # success. Never clears failed_at (additive-only, T-87-20).
    await session.execute(
        pg_insert(StageSkip).values(file_id=file_id, stage=stage, reason=clean_reason).on_conflict_do_nothing(index_elements=["file_id", "stage"])
    )
    await session.commit()  # get_session does NOT auto-commit (Pitfall 7)
    logger.info("force_skip_stage wrote marker", file_id=str(file_id), stage=stage)
    # HTMX ack: the success toast (oob to #toast-container). stage is allowlisted (safe to interpolate);
    # the operator reason is NOT echoed (T-87-21). The pill flips ⊘ skipped on the next 5s poll.
    return HTMLResponse(
        f'<div hx-swap-oob="beforeend:#toast-container">'
        f'<div role="status" aria-live="polite" x-data="{{ show: true }}" x-show="show" '
        f'x-init="setTimeout(() => show = false, 5000)" x-transition '
        f'class="rounded bg-gray-800 px-4 py-2 text-sm text-white shadow dark:shadow-none dark:ring-1 dark:ring-phaze-border">'
        f"Skipped {stage} — reason recorded.</div></div>"
    )


# --------------------------------------------------------------------------------------------------
# Per-file eligibility trace (UI-03 / D-06/D-07): the diagnostic whose absence hid the deadlock. A
# single-row resolve_status/eligible() evaluation (NOT a corpus scan, T-87-23) that names the ONE
# unmet blocker keeping a stage out of the pending set.
# --------------------------------------------------------------------------------------------------

# Display label per stage for the six-pill matrix + trace verdict (the 7->6 remap: tracklist is
# omitted; review renders as Appr, apply as Exec). Mirrors the _stage_matrix partial pill order.
_STAGE_TRACE_LABELS: dict[Stage, str] = {
    Stage.METADATA: "Meta",
    Stage.FINGERPRINT: "FP",
    Stage.ANALYZE: "Analyze",
    Stage.PROPOSE: "Prop",
    Stage.REVIEW: "Appr",
    Stage.APPLY: "Exec",
}


async def _one_stage_scalars(session: AsyncSession, stage: Stage, file_id: uuid.UUID) -> dict[str, Any]:
    """Read ONE file's per-stage scalars in the DB-free ``resolve_status`` shape (mirrors ``load_scalars``).

    Every read is strictly ``file_id``-scoped (T-87-23 — a single-row evaluation, never a corpus scan).
    """
    func_name = STAGE_TO_FUNCTION.get(stage.value)
    inflight = False
    if func_name is not None:
        ledger_row = (await session.execute(select(SchedulingLedger.key).where(SchedulingLedger.key == f"{func_name}:{file_id}"))).first()
        inflight = ledger_row is not None

    async def _skipped() -> bool:
        found = (await session.execute(select(StageSkip.id).where(StageSkip.file_id == file_id, StageSkip.stage == stage.value))).first()
        return found is not None

    if stage is Stage.ANALYZE:
        arow = (
            await session.execute(select(AnalysisResult.analysis_completed_at, AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))
        ).first()
        return {"completed_at": arow[0] if arow else None, "failed_at": arow[1] if arow else None, "inflight": inflight, "skipped": await _skipped()}
    if stage is Stage.METADATA:
        mrow = (await session.execute(select(FileMetadata.failed_at).where(FileMetadata.file_id == file_id))).first()
        return {"row_present": mrow is not None, "failed_at": mrow[0] if mrow else None, "inflight": inflight, "skipped": await _skipped()}
    if stage is Stage.FINGERPRINT:
        rows = (await session.execute(select(FingerprintResult.status).where(FingerprintResult.file_id == file_id))).all()
        return {"engine_statuses": [r[0] for r in rows], "inflight": inflight, "skipped": await _skipped()}
    if stage is Stage.TRACKLIST:
        present = (await session.execute(select(Tracklist.id).where(Tracklist.file_id == file_id))).first() is not None
        return {"row_present": present, "failed": False, "inflight": inflight}
    if stage in (Stage.PROPOSE, Stage.REVIEW):
        present = (await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id))).first() is not None
        failed = (
            await session.execute(select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == "failed"))
        ).first() is not None
        return {"row_present": present, "failed": failed, "inflight": inflight}
    # apply: execution_log joined through proposals (execution_log has NO file_id)
    present = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "completed")
        )
    ).first() is not None
    failed = (
        await session.execute(
            select(ExecutionLog.id)
            .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
            .where(RenameProposal.file_id == file_id, ExecutionLog.status == "failed")
        )
    ).first() is not None
    return {"row_present": present, "failed": failed, "inflight": inflight}


async def _has_approved_proposal(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """file_id-scoped single-row probe: does an APPROVED proposal exist? (apply's ELIG-02 gate)."""
    row = (
        await session.execute(
            select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == ProposalStatus.APPROVED.value)
        )
    ).first()
    return row is not None


async def _eligibility_trace_context(session: AsyncSession, file_id: uuid.UUID, stage: Stage) -> dict[str, Any]:
    """Evaluate ``eligible()`` for ONE file/stage and build the named-conjunct trace context (UI-03, D-06/D-07).

    Loads the stage's own status plus its ``ELIGIBILITY_DAG`` upstream statuses (single-row reads),
    evaluates the REAL ``eligible()`` (the scheduler's source of truth) in Python, and names the single
    unmet blocker. Enrich stages have no upstream, so ``upstream met?`` is vacuously true. The upstream
    conjunct STRICTLY mirrors ``eligible()`` (upstream must be DONE): under the OQ-1 SCOPE-MINIMAL
    resolution a force-skipped enrich upstream does NOT unblock its downstream (Phase 90), so a SKIPPED
    upstream is rendered as still-gating — a lenient "skipped = met" display would make the trace claim a
    downstream is eligible when the scheduler permanently gates it (the deadlock UI-03 exists to expose).
    NOT a corpus query (T-87-23).
    """
    label = _STAGE_TRACE_LABELS.get(stage, stage.value)
    upstreams = ELIGIBILITY_DAG[stage]
    statuses: dict[Stage, Status] = {stage: resolve_status(stage, await _one_stage_scalars(session, stage, file_id))}
    for u in upstreams:
        statuses[u] = resolve_status(u, await _one_stage_scalars(session, u, file_id))
    has_approved = await _has_approved_proposal(session, file_id) if stage is Stage.APPLY else False

    target = statuses[stage]
    is_done = target == Status.DONE
    is_in_flight = target == Status.IN_FLIGHT
    is_skipped = target == Status.SKIPPED
    is_terminal_fail = stage in ELIGIBLE_AFTER_FAILURE and target == Status.FAILED and not ELIGIBLE_AFTER_FAILURE[stage]

    if stage is Stage.APPLY:
        # apply is gated on an APPROVED proposal (ELIG-02), NOT on bare done(review).
        upstream_met = has_approved
        upstream_phrase = "approved proposal exists" if has_approved else "no approved proposal"
    elif upstreams:
        # STRICT mirror of eligible()'s downstream check (upstream must be DONE). Under the OQ-1
        # SCOPE-MINIMAL resolution a force-skipped enrich upstream does NOT unblock its downstream
        # (deferred to Phase 90), so a SKIPPED upstream stays gating — the trace names it honestly
        # rather than claiming a downstream is eligible when the scheduler permanently gates it.
        unmet = [u for u in upstreams if statuses[u] != Status.DONE]
        upstream_met = not unmet
        if upstream_met:
            upstream_phrase = "all upstream done"
        elif statuses[unmet[0]] == Status.SKIPPED:
            upstream_phrase = f"{unmet[0].value} skipped — downstream stays gated (Phase 90)"
        else:
            upstream_phrase = f"{unmet[0].value} not done"
    else:  # enrich: empty upstream is vacuously met (ELIG-01 independence)
        upstream_met = True
        upstream_phrase = "no upstream (enrich stage)"

    # Verdict is the REAL eligible() — the single source of truth the scheduler uses. A diagnostic that
    # diverged from it would hide the very deadlock UI-03 exists to expose.
    is_eligible = eligible(statuses, stage, has_approved_proposal=has_approved)

    # The single blocker follows eligible()'s short-circuit order (skipped folds onto the settled done? line).
    blocker = ""
    if not is_eligible:
        if is_done or is_skipped:
            blocker = "done"
        elif is_in_flight:
            blocker = "inflight"
        elif is_terminal_fail:
            blocker = "terminal"
        elif not upstream_met:
            blocker = "upstream"

    if is_done:
        done_ok, done_phrase = True, "already done"
    elif is_skipped:
        done_ok, done_phrase = True, "force-skipped (⊘) — recorded as skipped, not done"
    else:
        done_ok, done_phrase = False, "not done"

    conjuncts = [
        {"question": "done?", "ok": done_ok, "phrase": done_phrase, "blocker": blocker == "done"},
        {
            "question": "in-flight?",
            "ok": is_in_flight,
            "phrase": "currently running" if is_in_flight else "not running",
            "blocker": blocker == "inflight",
        },
        {"question": "upstream met?", "ok": upstream_met, "phrase": upstream_phrase, "blocker": blocker == "upstream"},
        {
            "question": "terminal fail?",
            "ok": is_terminal_fail,
            "phrase": "terminal failure — retry is manual" if is_terminal_fail else "no terminal failure",
            "blocker": blocker == "terminal",
        },
    ]
    verdict = f"{label} — eligible (in the pending set)" if is_eligible else f"{label} — NOT eligible"
    return {"stage_label": label, "eligible": is_eligible, "verdict": verdict, "conjuncts": conjuncts, "unavailable": False}


@router.get("/pipeline/files/{file_id}/trace/{stage}", response_class=HTMLResponse)
async def eligibility_trace(
    request: Request,
    file_id: uuid.UUID,
    stage: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Per-file, per-stage eligibility trace (UI-03) — the diagnostic whose absence hid the deadlock.

    Renders ``_eligibility_trace.html`` under the clicked pill: a verdict line plus the four named
    conjuncts (``done?`` · ``in-flight?`` · ``upstream met?`` · ``terminal fail?``) with the single
    unmet blocker highlighted. It is a single-row ``resolve_status``/``eligible()`` evaluation
    (T-87-23 — never a corpus scan) and degrades to "Trace unavailable this tick." on any error, so a
    poll never 500s.
    """
    stage_enum: Stage | None
    try:
        stage_enum = Stage(stage)
    except ValueError:
        stage_enum = None
    context: dict[str, Any] = {"request": request}
    if stage_enum is None:
        context["unavailable"] = True
        return templates.TemplateResponse(request=request, name="pipeline/partials/_eligibility_trace.html", context=context)
    try:
        context.update(await _eligibility_trace_context(session, file_id, stage_enum))
    except Exception:
        logger.warning("eligibility_trace degraded", file_id=str(file_id), stage=stage, exc_info=True)
        context["unavailable"] = True
    return templates.TemplateResponse(request=request, name="pipeline/partials/_eligibility_trace.html", context=context)


@router.post("/pipeline/files/{file_id}/deepen", response_class=HTMLResponse)
async def deepen_analysis(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: re-analyze ONE sampled file at the full (unbounded) window budget.

    Phase 43 strides long files to bound per-file cost, leaving a "sampled" result. This
    "deepen analysis" re-trigger re-enqueues that single file's ``process_file`` job with
    ``fine_cap=0`` / ``coarse_cap=0`` -- the sentinel that ``analysis._stride_to_cap`` treats
    as the analyze-ALL-windows no-op (D-04) -- so the operator gets a full re-analysis on
    demand.

    Incident guards (D-05, MANDATORY):
    - Routing: the queue is resolved via ``enqueue_router.resolve_queue_for_task`` so the job
      lands on the per-agent ``process_file`` queue, NEVER the consumer-less default queue
      (Phase-30 misrouting incident). ``process_file`` is an AGENT_TASK; if no agent is online
      ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT enqueuing --
      it never falls through to the default queue.
    - Payload: the re-enqueue funnels through ``enqueue_process_file`` which builds the COMPLETE
      ``ProcessFilePayload`` (v4.0.8 truncation incident -- a ``file_id``-only payload would
      dead-letter under ``extra="forbid"``).
    - Dedup: ``enqueue_process_file`` uses the deterministic ``process_file:<file_id>`` key, so a
      re-deepen of a file with a live in-flight job dedups to a no-op (D-05); re-deepening an
      already-ANALYZED file with no live job is a fresh enqueue.

    The typed ``uuid.UUID`` path param yields a 422 on a malformed id; an unknown (well-formed)
    id resolves to ``None`` and returns a not-found fragment -- never a raw 500 (T-44-10).
    """
    # quick-260707-cvz: capture the click epoch BEFORE the enqueue so the poll's completion
    # predicate (analysis_completed_at > requested_at) only trips on a re-run stamped AFTER
    # this click -- a stale pre-click sampled result never shows as "complete".
    since = datetime.now(UTC).timestamp()

    result = await session.execute(select(FileRecord).where(FileRecord.id == file_id))
    file = result.scalar_one_or_none()

    not_found = file is None
    no_active_agent = False

    if file is not None:
        try:
            routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
        except enqueue_router.NoActiveAgentError:
            # Do NOT fall through to the default queue (Phase-30 guard) -- surface gracefully.
            no_active_agent = True
        else:
            # process_file is an AGENT_TASK -- resolve always returns a non-None agent_id;
            # cast narrows str | None -> str for ProcessFilePayload.agent_id.
            agent_id = cast("str", routed.agent_id)
            # fine_cap=0 / coarse_cap=0 -> _stride_to_cap no-op -> analyze ALL windows (unbounded
            # deepen, D-04). The single funnel guarantees the full payload + deterministic key.
            await enqueue_process_file(routed.queue, file, agent_id, settings.models_path, fine_cap=0, coarse_cap=0)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/deepen_response.html",
        context={
            "request": request,
            "not_found": not_found,
            "no_active_agent": no_active_agent,
            # Consumed ONLY by the success branch's bootstrap poller (guards/branches above
            # are unchanged). since is a numeric float threaded into the poll URL.
            "file_id": file_id,
            "since": since,
        },
    )


@router.get("/pipeline/files/{file_id}/deepen-progress", response_class=HTMLResponse)
async def deepen_progress(
    request: Request,
    file_id: uuid.UUID,
    since: float,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX poll target for the "Deepen analysis" progress surface (quick-260707-cvz).

    ``since`` is the deepen-click epoch (seconds, float) threaded through the poll URL. It is a
    typed query param -- FastAPI 422s a non-numeric value (T-cvz-01) and it is used ONLY in a
    datetime compare, never rendered raw. The rendered counts are numeric ints (None-guarded to
    0), never essentia strings (T-cvz-02). An unknown file_id returns a benign "gone" fragment,
    never a 500 (T-cvz-04).

    COMPLETION PREDICATE (timestamp-gated): a re-run is complete only when ``put_analysis`` has
    stamped ``analysis_completed_at`` AFTER this click (``> requested_at``). A stale pre-click
    sampled result has ``completed_at <= requested_at`` and is NOT complete -- killing the
    misleading-complete edge. ``post_analysis_progress`` is counter-only and never touches
    ``analysis_completed_at``, so a re-deepen of an already-ANALYZED file keeps its OLD
    completed_at until the fresh ``put_analysis`` lands.

    State machine (evaluated in order): missing file -> gone (terminal); complete predicate ->
    complete (terminal); fine_total truthy AND fine_done < fine_total -> running (poll);
    otherwise -> queued/starting (poll).
    """
    file_result = await session.execute(select(FileRecord).where(FileRecord.id == file_id))
    file = file_result.scalar_one_or_none()

    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/deepen_progress.html",
            context={
                "request": request,
                "gone": True,
                "complete": False,
                "running": False,
                "fine_done": 0,
                "fine_total": 0,
                "file_id": file_id,
                "since": since,
            },
        )

    analysis_result = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))
    analysis = analysis_result.scalar_one_or_none()

    requested_at = datetime.fromtimestamp(since, tz=UTC)
    complete = analysis is not None and analysis.analysis_completed_at is not None and analysis.analysis_completed_at > requested_at

    fine_done = (analysis.fine_windows_analyzed or 0) if analysis is not None else 0
    fine_total = (analysis.fine_windows_total or 0) if analysis is not None else 0
    running = (not complete) and fine_total > 0 and fine_done < fine_total

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/deepen_progress.html",
        context={
            "request": request,
            "gone": False,
            "complete": complete,
            "running": running,
            "fine_done": fine_done,
            "fine_total": fine_total,
            "file_id": file_id,
            "since": since,
        },
    )


@router.post("/pipeline/proposals", response_class=HTMLResponse)
async def trigger_proposals_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger proposal generation and return response fragment."""
    # Per D-02: convergence gate via the shared, deterministically-sorted pending-set helper
    # (D-03 anti-drift) -- same sorted batches as the API trigger + recovery, so keys align.
    batches = await get_proposal_pending_batches(session, settings.llm_batch_size)
    count = sum(len(b) for b in batches)
    batches_count = 0

    if count > 0:
        batches_count = len(batches)
        routed = await enqueue_router.resolve_queue_for_task("generate_proposals", request.app.state, session)
        task = asyncio.create_task(_enqueue_proposal_jobs(routed.queue, batches))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "proposal generation", "count": count, "batches": batches_count, "no_active_agent": False},
    )


async def _enqueue_extraction_jobs(queue: Any, files: list[FileRecord], agent_id: str) -> None:
    """Background coroutine to enqueue extract_file_metadata jobs with the COMPLETE payload.

    The agent worker validates ``ExtractMetadataPayload`` with ``extra="forbid"`` and four
    required fields (file_id, original_path, file_type, agent_id). A ``file_id``-only enqueue
    therefore fails validation and dead-letters EVERY job -- the same defect that bit the
    pre-Phase-30 ``process_file`` path (see ``analysis_enqueue.enqueue_process_file``) and the
    v4.0.8 payload incident. D-06 removed the only other producer (the agent file-upsert
    auto-enqueue), making this manual trigger the SOLE metadata producer, so the full payload
    MUST be built here. ``model_dump(mode="json")`` serializes the UUID as a string so the
    worker's ``model_validate`` accepts it. The deterministic key
    (``extract_file_metadata:<file_id>``) is applied centrally by the ``before_enqueue`` hook
    (35-01), so no explicit ``key=`` is set here.
    """
    for f in files:
        payload = ExtractMetadataPayload(
            file_id=f.id,
            original_path=f.original_path,
            file_type=f.file_type,
            agent_id=agent_id,
        )
        await queue.enqueue("extract_file_metadata", **payload.model_dump(mode="json"))


@router.post("/api/v1/extract-metadata")
async def trigger_metadata_extraction(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue extract_file_metadata jobs for eligible music/video files (READ-01 cutover).

    Per D-04: originally queued every music/video file regardless of status, for backfill. READ-01
    replaced that state-agnostic selector with the DERIVED pending set (see
    :func:`get_metadata_pending_files`, ``eligible_clause(Stage.METADATA)``): a file whose metadata
    is genuinely done is excluded, while a not-started or failed one stays eligible (auto-retry).
    Per D-09: manual API endpoint for re-extraction.
    """
    files = await get_metadata_pending_files(session)

    if not files:
        return {"enqueued": 0, "message": "No music/video files found"}

    try:
        routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        return {"enqueued": 0, "message": _NO_ACTIVE_AGENT_MESSAGE}

    # extract_file_metadata is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, files, agent_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"enqueued": len(files), "message": f"Enqueued {len(files)} files for metadata extraction"}


@router.post("/pipeline/extract-metadata", response_class=HTMLResponse)
async def trigger_extraction_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger metadata extraction and return response fragment."""
    files = await get_metadata_pending_files(session)
    count = len(files)
    no_active_agent = False

    if count > 0:
        try:
            routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session)
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            agent_id = cast("str", routed.agent_id)
            task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, files, agent_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "metadata extraction", "count": count, "no_active_agent": no_active_agent},
    )


# --- Fingerprint endpoints (Phase 16, D-14, D-15) ---


async def _enqueue_fingerprint_jobs(queue: Any, files: list[FileRecord], agent_id: str) -> None:
    """Background coroutine wrapper over the shared fingerprint enqueue funnel.

    The funnel itself now lives in :func:`phaze.services.fingerprint_requeue.enqueue_fingerprint_jobs`
    so the recovery CLI (``phaze fingerprint requeue``, phaze-rf04.1) enqueues through the
    IDENTICAL payload construction. Keeping two copies is how the payload shape drifts and
    one producer starts dead-lettering; there is exactly one now.
    """
    result = await enqueue_fingerprint_jobs(queue, files, agent_id)
    # phaze-e57w: this HTTP path is fire-and-forget (no operator response to carry counts), but a
    # BLOCKED collision -- a file whose deterministic key is held by a dead 'aborting'/failed row --
    # must not vanish silently. Surface it in the logs (the aborting-reaper frees the key).
    if result.blocked:
        logger.warning(
            "fingerprint enqueue: files BLOCKED by a dead job row (not in flight)",
            blocked=result.blocked,
            blocked_keys=list(result.blocked_keys),
        )


@router.post("/api/v1/fingerprint")
async def trigger_fingerprint(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue fingerprint_file jobs for eligible files (per D-14; READ-01 cutover).

    Eligible: music/video files not yet fingerprinted, plus files with a failed fingerprint result
    (auto-retry eligible), minus dedup-resolved files -- see :func:`get_fingerprint_pending_files`
    (derived via ``eligible_clause(Stage.FINGERPRINT)``; no longer a ``METADATA_EXTRACTED``
    file-state filter).
    """
    # Shared pending-set helper (D-03 anti-drift): METADATA_EXTRACTED + failed-retry, deduped by id.
    all_files = await get_fingerprint_pending_files(session)

    if not all_files:
        return {"enqueued": 0, "message": "No files eligible for fingerprinting"}

    try:
        routed = await enqueue_router.resolve_queue_for_task("fingerprint_file", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        return {"enqueued": 0, "message": _NO_ACTIVE_AGENT_MESSAGE}

    # fingerprint_file is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)
    task = asyncio.create_task(_enqueue_fingerprint_jobs(routed.queue, all_files, agent_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"enqueued": len(all_files), "message": f"Enqueued {len(all_files)} files for fingerprinting"}


@router.get("/api/v1/fingerprint/progress")
async def fingerprint_progress(
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Return fingerprint progress counts (per D-15)."""
    return await get_fingerprint_progress(session)


@router.post("/pipeline/fingerprint", response_class=HTMLResponse)
async def trigger_fingerprint_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger fingerprinting and return response fragment.

    Phase 42: now reads the shared :func:`get_fingerprint_pending_files` helper, so this HTMX
    endpoint is ALIGNED with the ``/api/v1/fingerprint`` endpoint and recovery -- it GAINS the
    failed-fingerprint-retry scope (D-03 anti-drift). Previously it queried ONLY
    ``METADATA_EXTRACTED``; the broadened, deduped pending set is the intended consistency fix.
    """
    files = await get_fingerprint_pending_files(session)
    count = len(files)
    no_active_agent = False

    if count > 0:
        try:
            routed = await enqueue_router.resolve_queue_for_task("fingerprint_file", request.app.state, session)
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            agent_id = cast("str", routed.agent_id)
            task = asyncio.create_task(_enqueue_fingerprint_jobs(routed.queue, files, agent_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "fingerprinting", "count": count, "no_active_agent": no_active_agent},
    )


# --- Tracklist name-search endpoint (Phase 39, REQ-39-1) ---


async def _enqueue_search_jobs(queue: Any, files: list[FileRecord]) -> None:
    """Background coroutine to enqueue ``search_tracklist`` jobs (one per eligible file).

    ``search_tracklist`` is a CONTROLLER task taking only ``file_id`` (mirrors the single-file
    ``tracklists.manual_search`` trigger); the deterministic key ``search_tracklist:<file_id>`` is
    applied centrally by the ``before_enqueue`` hook (Phase 35), so a double-click / refresh
    collapses an in-flight re-run to a no-op (D, T-39-02). Background-enqueued to avoid HTTP timeout
    on a large eligible archive (Research pitfall 2). ``files`` attributes are already loaded by the
    eligible-set query and the request never commits, so reading ``f.id`` here is not a lazy load.
    """
    for f in files:
        await queue.enqueue("search_tracklist", file_id=str(f.id))


@router.post("/pipeline/search-tracklists", response_class=HTMLResponse)
async def trigger_search_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: bulk-trigger name-based tracklist search over eligible files (Phase 39).

    Eligible = music/video files that do NOT already have a tracklist (skip already-matched files so
    re-runs are cheap and idempotent). ``search_tracklist`` is a CONTROLLER task, routed via
    :func:`enqueue_router.resolve_queue_for_task` to the controller queue (Phase-30 rule) -- never
    the consumer-less default queue. Controller tasks never raise ``NoActiveAgentError`` (mirrors
    ``manual_search``), so no no-active-agent branch is needed. Manual only -- NO auto-trigger
    (the Phase-39 boundary; automatic enqueue is reserved for the Phase-42 recovery pass).
    """
    # Shared pending-set helper (D-03 anti-drift): the SAME untracked-files set the Phase-40 scan
    # trigger and Phase-42 recovery read, so the three paths cannot drift.
    files = await get_untracked_files(session)
    count = len(files)

    if count > 0:
        routed = await enqueue_router.resolve_queue_for_task("search_tracklist", request.app.state, session)
        task = asyncio.create_task(_enqueue_search_jobs(routed.queue, files))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "tracklist search", "count": count, "no_active_agent": False},
    )


# --- Tracklist fingerprint-scan endpoint (Phase 40, REQ-40-1) ---


async def _enqueue_scan_jobs(queue: Any, files: list[FileRecord], agent_id: str) -> None:
    """Background coroutine to enqueue ``scan_live_set`` jobs with the COMPLETE payload (T-40-DL).

    ``ScanLiveSetPayload`` (``extra="forbid"``) requires file_id, original_path AND agent_id; a
    ``file_id``-only enqueue dead-letters EVERY job -- the v4.0.8 payload-incident class. The buggy
    single-file ``tracklists.trigger_scan`` (which enqueues only ``file_id``) is deliberately NOT
    copied; this mirrors the CORRECT full-payload producer ``_enqueue_fingerprint_jobs`` (identical
    field set) instead. Build the complete payload and serialize via ``model_dump(mode="json")`` so
    the UUID is sent as a string the worker's ``model_validate`` accepts. The deterministic key
    (``scan_live_set:<file_id>``) is applied centrally by the ``before_enqueue`` hook (Phase 35), so a
    double-click/refresh dedups in flight (T-40-02) -- no explicit ``key=`` is set here.
    Background-enqueued to avoid HTTP timeout on a large eligible archive (Pitfall 2).
    """
    for f in files:
        payload = ScanLiveSetPayload(
            file_id=f.id,
            original_path=f.original_path,
            agent_id=agent_id,
        )
        await queue.enqueue("scan_live_set", **payload.model_dump(mode="json"))


@router.post("/pipeline/scan-live-sets", response_class=HTMLResponse)
async def trigger_scan_live_sets_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: bulk-trigger agent-side fingerprint scan over eligible files (Phase 40).

    Eligible = music/video files that do NOT already have a tracklist (skip already-matched files so
    re-runs are cheap and idempotent), the SAME query the Phase-39 Search trigger uses. ``scan_live_set``
    is a PER-AGENT task, routed via :func:`enqueue_router.resolve_queue_for_task` to the active agent's
    queue (``phaze-agent-<id>``, Phase-30 rule) -- NEVER the consumer-less default queue. With eligible
    files but no online agent the resolve raises ``NoActiveAgentError``; that is caught, nothing is
    enqueued, and the no-active-agent empty-state renders (status 200, never 500). Manual only -- NO
    auto-trigger (automatic enqueue is reserved for the Phase-42 recovery pass).
    """
    # Shared pending-set helper (D-03 anti-drift): the SAME untracked-files set the Phase-39 search
    # trigger and Phase-42 recovery read.
    files = await get_untracked_files(session)
    count = len(files)
    no_active_agent = False

    if count > 0:
        try:
            routed = await enqueue_router.resolve_queue_for_task("scan_live_set", request.app.state, session)
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            # scan_live_set is an AGENT_TASK -- resolve always returns a non-None agent_id.
            agent_id = cast("str", routed.agent_id)
            task = asyncio.create_task(_enqueue_scan_jobs(routed.queue, files, agent_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "fingerprint scan", "count": count, "no_active_agent": no_active_agent},
    )


# --- Bulk scrape + match tracklist endpoints (Phase 41, REQ-41-1/REQ-41-2) ---


async def _enqueue_scrape_jobs(queue: Any, tracklists: list[Tracklist]) -> None:
    """Background coroutine to enqueue ``scrape_and_store_tracklist`` jobs (one per pending tracklist).

    ``scrape_and_store_tracklist`` is a CONTROLLER task taking only ``tracklist_id`` (mirrors the
    single-tracklist ``tracklists.rescrape_tracklist`` trigger); the deterministic key
    ``scrape_and_store_tracklist:<tracklist_id>`` is applied centrally by the ``before_enqueue`` hook
    (Phase 35), so a double-click / refresh collapses an in-flight re-run to a no-op (D, T-41-02). Set
    NO explicit ``key=``. Background-enqueued to avoid HTTP timeout on a large pending set (Pitfall 2).
    ``tracklists`` rows are already loaded by the eligible-set query and the request never commits, so
    reading ``tl.id`` here is not a lazy load.
    """
    for tl in tracklists:
        await queue.enqueue("scrape_and_store_tracklist", tracklist_id=str(tl.id))


async def _enqueue_match_jobs(queue: Any, tracklists: list[Tracklist]) -> None:
    """Background coroutine to enqueue ``match_tracklist_to_discogs`` jobs (one per pending tracklist).

    ``match_tracklist_to_discogs`` is a CONTROLLER task taking only ``tracklist_id`` (mirrors the
    single-tracklist ``tracklists.match_discogs`` trigger); the deterministic key
    ``match_tracklist_to_discogs:<tracklist_id>`` is applied centrally by the ``before_enqueue`` hook
    (Phase 35), so a double-click / refresh dedups in flight (D, T-41-02). Set NO explicit ``key=``.
    Background-enqueued to avoid HTTP timeout on a large pending set (Pitfall 2).
    """
    for tl in tracklists:
        await queue.enqueue("match_tracklist_to_discogs", tracklist_id=str(tl.id))


@router.post("/pipeline/scrape-tracklists", response_class=HTMLResponse)
async def trigger_scrape_tracklists_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: bulk-trigger tracklist scraping over the pending set (Phase 41).

    Pending = tracklists with NO scraped version yet (the exact complement of
    :func:`get_stage_progress`'s ``scrape.done``); already-scraped tracklists are skipped so re-runs
    are cheap and idempotent. ``scrape_and_store_tracklist`` is a CONTROLLER task, routed via
    :func:`enqueue_router.resolve_queue_for_task` to the controller queue (Phase-30 rule) -- never the
    consumer-less default queue. Controller tasks never raise ``NoActiveAgentError`` (mirrors
    ``rescrape_tracklist``), so no no-active-agent branch is needed. Manual only -- NO auto-trigger
    (automatic enqueue is reserved for the Phase-42 recovery pass).
    """
    tracklists = await get_scrape_pending_tracklists(session)
    count = len(tracklists)

    if count > 0:
        routed = await enqueue_router.resolve_queue_for_task("scrape_and_store_tracklist", request.app.state, session)
        task = asyncio.create_task(_enqueue_scrape_jobs(routed.queue, tracklists))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_tracklist_response.html",
        context={"request": request, "action": "scraping", "count": count},
    )


@router.post("/pipeline/match-tracklists", response_class=HTMLResponse)
async def trigger_match_tracklists_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: bulk-trigger Discogs matching over the pending set (Phase 41).

    Pending = tracklists NOT yet reachable from ``discogs_links`` (the exact complement of
    :func:`get_stage_progress`'s ``match.done``); already-linked tracklists are skipped so re-runs are
    cheap and idempotent. ``match_tracklist_to_discogs`` is a CONTROLLER task, routed via
    :func:`enqueue_router.resolve_queue_for_task` to the controller queue (Phase-30 rule) -- never the
    consumer-less default queue. Controller tasks never raise ``NoActiveAgentError`` (mirrors
    ``match_discogs``), so no no-active-agent branch is needed. Manual only -- NO auto-trigger
    (automatic enqueue is reserved for the Phase-42 recovery pass).
    """
    tracklists = await get_match_pending_tracklists(session)
    count = len(tracklists)

    if count > 0:
        routed = await enqueue_router.resolve_queue_for_task("match_tracklist_to_discogs", request.app.state, session)
        task = asyncio.create_task(_enqueue_match_jobs(routed.queue, tracklists))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_tracklist_response.html",
        context={"request": request, "action": "matching", "count": count},
    )


# --- Manual recovery endpoint (Phase 42, D-02/D-05) ---


async def _run_recovery(ctx: dict[str, Any]) -> None:
    """Background coroutine: run the gated all-stages recovery producer (force=True).

    Calls the SAME :func:`recover_orphaned_work` producer the controller startup hook runs
    (Phase 42, D-03), so the manual and automatic recovery paths cannot drift. ``force=True``
    bypasses ONLY the no-op queue-loss DETECT gate (this is the operator-driven cold-boot
    safety net, D-05) -- it never bypasses the per-item deterministic-key dedup, so a forced
    reconcile over a live queue collapses every still-in-flight item to a skipped no-op and
    can NEVER double the backlog (Phase-32 doubling class is closed).
    """
    await recover_orphaned_work(ctx, force=True)


@router.post("/pipeline/recover", response_class=HTMLResponse)
async def trigger_recover_ui(request: Request) -> HTMLResponse:
    """HTMX endpoint: manually trigger the gated all-stages recovery pass (Phase 42, D-02/D-05).

    The global DAG "Recover" button posts here. It builds a worker-shaped ``ctx`` from the API
    app -- the module-level :data:`phaze.database.async_session` sessionmaker (same DB as the
    ``saq_jobs`` broker), the lifespan-created ``app.state.controller_queue`` (controller stages),
    and ``app.state.task_router`` (per-agent stages) -- and schedules :func:`recover_orphaned_work`
    with ``force=True`` as a fire-and-forget background task (same ``_background_tasks`` discipline
    as every other pipeline trigger, so a large reconcile never blocks the HTTP response). Because
    the producer runs in the background, this returns immediately with a "recovery started" fragment
    rather than the final per-stage counts. The endpoint calls the SAME producer as controller
    startup, so the manual and automatic recovery paths cannot drift (D-03), and the deterministic-key
    dedup keeps a forced reconcile idempotent (T-42-06/T-42-07) -- it can never 500 on a healthy queue.
    """
    ctx: dict[str, Any] = {
        "async_session": async_session,
        "queue": request.app.state.controller_queue,
        "task_router": request.app.state.task_router,
    }
    task = asyncio.create_task(_run_recovery(ctx))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/recover_response.html",
        context={"request": request},
    )
