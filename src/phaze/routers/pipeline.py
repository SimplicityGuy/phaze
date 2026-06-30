"""Pipeline orchestration router -- trigger endpoints and dashboard UI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
import uuid  # noqa: TC003 -- runtime import: FastAPI resolves the `file_id: uuid.UUID` path-param annotation via get_type_hints

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
import structlog

from phaze.config import settings
from phaze.database import async_session, get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.schemas.agent_tasks import ExtractMetadataPayload, FingerprintFilePayload, ProcessFilePayload, ScanLiveSetPayload
from phaze.services import enqueue_router
from phaze.services.analysis_enqueue import enqueue_process_file, process_file_job_key
from phaze.services.fingerprint import get_fingerprint_progress
from phaze.services.pipeline import (
    count_active_agents,
    count_backfill_candidates,
    get_analysis_failed_count,
    get_awaiting_cloud_count,
    get_backfill_candidates,
    get_cloud_phase_counts,
    get_discovered_files_with_duration,
    get_fingerprint_pending_files,
    get_global_reconciliation,
    get_inadmissible_count,
    get_localqueue_unreachable,
    get_match_busy_count,
    get_match_pending_tracklists,
    get_metadata_pending_files,
    get_pipeline_stats,
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
    get_untracked_files,
    queue_progress_percent,
)
from phaze.services.pipeline_counters import read_counters
from phaze.services.scheduling_ledger import insert_ledger_if_absent
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


async def _build_dag_context(app_state: Any, session: AsyncSession, activity: dict[str, int]) -> dict[str, dict[str, int]]:
    """Build the per-DAG-node store-key context consumed by stats_bar.html + the 35-05 canvas.

    Reconciles three sources (D-03): ``get_stage_progress`` (DB-truth ``done``/``total`` per
    node, the authority), the maintained Redis ``completed`` counters (a degrade backstop via
    :func:`_reconciled_done`), and the already-computed ``get_queue_activity`` (the per-node
    ACTIVE state). Every value is a plain ``int`` (``total=None`` em-dash sentinels collapse to
    0 — the Scan/Search node has NO ``tracklistTotal`` store key, so its em-dash stays a
    render-side concern) so it is safe to interpolate into the ``x-init`` numeric store writes.

    Returns ``{"dag": {<storeKey>: int, ...}}`` carrying every per-node sub-key seeded into
    ``$store.pipeline`` (base.html, 35-04 Task 1).
    """
    stage = await get_stage_progress(session)
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
        "analyzeActive": activity["agent_active"],
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

    # Phase 41 (REQ-41-3): the scrape_and_store_tracklist / match_tracklist_to_discogs in-flight counts
    # gate the DAG Scrape/Match trigger nodes "busy" (Scraping… / Matching…). Both are controller tasks
    # (NOT part of get_stage_busy_counts's three agent stages) -- get_scrape_busy_count + get_match_busy_
    # count each own their own never-500 SAVEPOINT degrade (return 0 on any DB error), so NO try/except is
    # added here; the ints ride the same dag.items() seed + OOB loop. (scrapeTotal/scrapeDone/matchTotal/
    # matchDone are already seeded above; the gate derives pending = total - done client-side.)
    dag["scrapeBusy"] = int(await get_scrape_busy_count(session))
    dag["matchBusy"] = int(await get_match_busy_count(session))

    return {"dag": dag}


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.tracklist import Tracklist


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

    fileserver_q = app_state.task_router.queue_for(fileserver_agent.id) if fileserver_agent is not None else None

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
            file.state = FileState.AWAITING_CLOUD
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
    """Enqueue process_file jobs for all DISCOVERED files, routed per-file by duration (Phase 49, D-06/D-11/D-12).

    Long (``>= cloud_route_threshold_sec``) files route to a COMPUTE agent's queue;
    short/null-duration files route to the FILESERVER queue exactly as before; a long file
    with no compute agent online is HELD in ``AWAITING_CLOUD`` (committed, NEVER silently
    analyzed locally -- D-02); short/null files with no fileserver online are reported
    ``skipped`` without aborting the run. One SAQ job per routed file; the enqueues run in a
    background task (via the shared router helper) to avoid HTTP timeout on large file counts.
    Returns the split counts. The no-active-agent message is returned ONLY when BOTH agent
    kinds are absent (nothing routable at all).
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    if not files_with_duration:
        return {"enqueued": 0, "message": "No files in DISCOVERED state"}

    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        settings.cloud_target != "local",
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
    stats = await get_pipeline_stats(session)

    # Phase 27 D-05/D-06: agents for the Trigger Scan dropdown (non-revoked, ordered).
    agents_stmt = select(Agent).where(Agent.revoked_at.is_(None)).order_by(Agent.name)
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
    dag_ctx = await _build_dag_context(app_state, session, activity)

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

    # quick 260622-i0w: the scanned/deduped reconciliation for the Discovery DAG-node subtitle.
    # Server-rendered on full-page load ONLY (the canvas is never OOB-swapped on the 5s poll); this
    # explains the Discovery COUNT(files) vs agent scan total gap as dedup, not lost work. The service
    # owns the never-500 degrade (returns {scanned: None, deduped: None} on any error), so NO
    # try/except here — same wiring idiom as get_queue_activity / dag_ctx above.
    recon = await get_global_reconciliation(session)

    return {
        "stats": stats,
        "current_page": "pipeline",
        "settings_batch_size": settings.llm_batch_size,
        "agents": agents,
        "recent_scans": recent_scans_rows,
        "straggler_count": straggler_count,
        "analysis_failed_count": analysis_failed_count,
        "awaiting_cloud_count": awaiting_cloud_count,
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
        **activity,
        **dag_ctx,
        "queue_progress_percent": queue_progress,
    }


@router.get("/pipeline/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the pipeline dashboard page (per D-03), or 302-redirect to the shell root.

    Phase 57 (SHELL-01 / D-03 true rename): ``/pipeline/`` is renamed to the v7.0 shell
    root ``/`` (whose Analyze default embeds THIS dashboard's DAG content via the shared
    :func:`build_dashboard_context`). A plain (non-HX) browser navigation / bookmark
    302-redirects to ``/``; the conditional form (``HX-Request != "true"``) preserves the
    in-page render path for HX callers and is uniform with the Plan-04 legacy redirects.

    Phase 27 D-05/D-06 extension: the dashboard exposes ``agents`` (the non-revoked agent
    list driving the Trigger Scan card dropdown) and ``recent_scans`` (the last 10 non-LIVE
    ScanBatches with ``agent_name`` + ``elapsed_seconds`` attached for the Recent Scans
    mini-table). The LIVE sentinel batches are excluded -- they are an internal
    watcher-ingestion state, not an operator-triggered event.
    """
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/", status_code=302)
    context = {"request": request, **await build_dashboard_context(request.app.state, session)}
    return templates.TemplateResponse(request=request, name="pipeline/dashboard.html", context=context)


@router.get("/pipeline/stats", response_class=HTMLResponse)
async def pipeline_stats_partial(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the stats bar partial for HTMX polling refresh."""
    stats = await get_pipeline_stats(session)
    # Phase 34: surface live queue depth through the EXISTING 5s poll (no new loop).
    # get_queue_activity degrades to zeros on a Redis hiccup / missing app.state, so the
    # poll can never 500. queue_progress_percent precomputes the guarded "Processing" bar
    # percent server-side; the OOB store-write nodes in stats_bar.html push agent_busy /
    # controller_busy into $store.pipeline on each tick to drive the Plan 04 button gating.
    activity = await get_queue_activity(request.app.state, session)
    queue_progress = queue_progress_percent(stats["analyzed"], activity["agent_busy"])
    # Phase 35 (35-04): same per-node reconcile as dashboard(), re-pushed on every 5s
    # poll via the OOB x-init seeds in stats_bar.html (gated behind oob_counts). The store
    # write keeps the 35-05 DAG bindings live without re-rendering the canvas or buttons.
    dag_ctx = await _build_dag_context(request.app.state, session, activity)
    # Phase 44 (44-04): the same straggler + ANALYSIS_FAILED buckets the dashboard seeds,
    # re-pushed on every 5s poll so the straggler_failed_card stays live. Degrade-safe at the
    # service layer (44-02), so NO router try/except -- mirrors the dashboard() wiring.
    straggler_count = await get_straggler_count(session, settings.straggler_threshold_sec)
    analysis_failed_count = await get_analysis_failed_count(session)
    # Phase 49 (49-02, D-05): the same AWAITING_CLOUD held count the dashboard seeds, re-pushed
    # on every 5s poll so the awaiting_cloud_card stays live via its OOB swap. Degrade-safe at the
    # service layer (Plan 01), so NO router try/except -- mirrors the straggler/failed wiring.
    awaiting_cloud_count = await get_awaiting_cloud_count(session)
    # Phase 50 (50-07, D-09): the same PUSHING/PUSHED window counts the dashboard seeds, re-pushed
    # on every 5s poll so the staged_pushing_card / analyzing_cloud_card stay live via their OOB
    # swaps. Degrade-safe at the service layer, so NO router try/except -- mirrors the awaiting wiring.
    pushing_count = await get_pushing_count(session)
    analyzing_cloud_count = await get_pushed_count(session)
    # Phase 54 (54-04, D-06): the same Inadmissible count the dashboard seeds, re-pushed on every 5s
    # poll so the inadmissible_card stays live via its OOB swap. Degrade-safe at the service layer,
    # so NO router try/except -- mirrors the awaiting_cloud_count wiring.
    inadmissible_count = await get_inadmissible_count(session)
    # Phase 56 (56-02, D-05, KDEPLOY-04): the same K8s LocalQueue-unreachable flag the dashboard seeds,
    # re-pushed on every 5s poll so the localqueue_card stays live via its OOB swap. Degrade-safe at the
    # service layer (56-01), so NO router try/except -- mirrors the inadmissible_count wiring; the redis
    # handle is read off app.state exactly like the dashboard() first-load path.
    localqueue_unreachable = await get_localqueue_unreachable(getattr(request.app.state, "redis", None))
    # Phase 55 (55-05, D-04, KROUTE-06): the same four per-cloud_phase admission counts the dashboard
    # seeds, re-pushed on every 5s poll so the admission_state_card stays live via its OOB swap.
    # Degrade-safe at the service layer (per-phase _safe_count), so NO router try/except -- mirrors
    # the inadmissible_count wiring.
    cloud_phase_counts = await get_cloud_phase_counts(session)
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
            "pushing_count": pushing_count,
            "analyzing_cloud_count": analyzing_cloud_count,
            "inadmissible_count": inadmissible_count,
            "localqueue_unreachable": localqueue_unreachable,
            "queued_behind_quota_count": cloud_phase_counts["queued_behind_quota"],
            "admitted_count": cloud_phase_counts["admitted"],
            "running_count": cloud_phase_counts["running"],
            "finished_count": cloud_phase_counts["finished"],
            **activity,
            **dag_ctx,
            "queue_progress_percent": queue_progress,
        },
    )


@router.post("/pipeline/analyze", response_class=HTMLResponse)
async def trigger_analysis_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger per-file duration-routed analysis and return the split-count fragment (Phase 49).

    Mirrors :func:`trigger_analysis`: long files route to a compute agent, short/null files to
    the fileserver, long files with no compute agent are held in ``AWAITING_CLOUD``, and
    short/null files with no fileserver are skipped without aborting the run. The fragment
    reports ``N local, M cloud, K awaiting cloud`` (+ a skipped bucket). The no-active-agent
    fragment is rendered ONLY when BOTH agent kinds are absent (nothing routable).
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    count = len(files_with_duration)

    if count == 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/trigger_response.html",
            context={"request": request, "action": "analysis", "count": 0, "no_active_agent": False},
        )

    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        settings.cloud_target != "local",
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


def _held_backfill_ledger_payload(file: FileRecord, models_path: str) -> dict[str, Any]:
    """Build the ``process_file`` payload stored on a backfill-HELD file's scheduling-ledger row.

    A held file has NO compute agent assigned yet (that is the reason it is held), so ``agent_id``
    is recorded empty: the real agent is stamped at RELEASE time by ``enqueue_process_file``'s
    ``before_enqueue`` ON CONFLICT DO UPDATE (the Plan-04 release cron). All five required
    ``ProcessFilePayload`` fields are present so a forced ``recover_orphaned_work`` replay
    re-validates cleanly under ``extra="forbid"`` rather than dead-lettering (T-45-10).
    """
    return ProcessFilePayload(
        file_id=file.id,
        original_path=file.original_path,
        file_type=file.file_type,
        agent_id="",
        models_path=models_path,
    ).model_dump(mode="json")


@router.post("/pipeline/backfill-cloud", response_class=HTMLResponse)
async def trigger_backfill_cloud(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: backfill the timed-out long files to the cloud (Phase 49, D-08/D-09/D-10).

    Selects EXACTLY the timed-out long set — ``ANALYSIS_FAILED ∧ duration >= cloud_route_threshold_sec``
    (the explicit :func:`count_backfill_candidates` / :func:`get_backfill_candidates` filter, NOT a
    whole-backlog ``ANALYSIS_FAILED`` sweep) — resets each row to ``DISCOVERED`` (committed BEFORE any
    enqueue, RESEARCH Pitfall 3), and routes the candidates through the SAME per-file duration router
    (:func:`_route_discovered_by_duration`) "Run Analysis" uses, so the two paths cannot drift: a
    compute agent online -> the compute queue (``cloud``); none online -> held in ``AWAITING_CLOUD``.

    For the HELD branch ONLY (never enqueued, so no ``before_enqueue`` hook fired) an explicit
    :func:`insert_ledger_if_absent` row is seeded (D-09) so the held file is durable scheduled work;
    the enqueued branch's row is owned by the hook (no double-write — RESEARCH Open-Q3). The
    deterministic ``process_file:<id>`` key plus the explicit ANALYSIS_FAILED filter close the
    over-enqueue class (D-10): a double-click is a no-op (the candidates have already left the
    ANALYSIS_FAILED state), and short / never-failed files are never touched.
    """
    # Phase 51 (D-03, Pitfall 2 / T-51-02): explicit cloud-target guard BEFORE the candidate query.
    # Gating only the routing seam is insufficient -- backfill would still reset the 144
    # ANALYSIS_FAILED long files to DISCOVERED and re-route them local to re-time-out. When the
    # target is 'local' (cloud off) this is a clean no-op that mutates ZERO file.state rows.
    if settings.cloud_target == "local":
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
    for file, _duration in candidates:
        file.state = FileState.DISCOVERED
    # RESEARCH Pitfall 3: explicit commit of the DISCOVERED reset BEFORE routing/backgrounding the
    # enqueues (get_session does NOT auto-commit). The UPDATE is a bounded set (the filtered candidates).
    await session.commit()

    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        candidates,
        threshold,
        # cloud is enabled here: the `cloud_target == "local"` early-return guard above already
        # short-circuited the local case, so cloud_target is statically 'a1' or 'k8s' (mypy narrows
        # it, which is why a literal `!= "local"` here is a redundant comparison). Pass True.
        True,
        settings.models_path,
    )

    # Phase 55 (L3 / CLOUDROUTE-02): the held-file ledger seed forks on the cloud target.
    #   - k8s: SKIP the seed entirely. A ``process_file:<id>`` ledger row would let
    #     ``recover_orphaned_work`` replay the held file onto a LOCAL agent queue -- the ``cloud_job``
    #     row (seeded by the ``stage_cloud_window`` k8s branch), NOT the ledger, is the k8s in-flight
    #     registry. The k8s held file is advanced purely by the duration router + staging cron.
    #   - a1 (cloud_target != "k8s"; "local" already returned early above): seed the row (D-09) so the
    #     held file -- never enqueued, so no ``before_enqueue`` hook fired -- is durable scheduled work.
    if settings.cloud_target == "k8s":
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

    # D-09 / RESEARCH Open-Q3: seed a ledger row ONLY for files the router HELD in AWAITING_CLOUD
    # (every backfill candidate is long, so the router never produces local/skipped here). The router
    # mutates ``file.state`` in place for held files, so the held set is detectable on the in-memory
    # candidate records (expire_on_commit=False preserves attribute values across its commit).
    held_files = [file for file, _ in candidates if file.state == FileState.AWAITING_CLOUD]
    for file in held_files:
        await insert_ledger_if_absent(
            session,
            key=process_file_job_key(file.id),
            function="process_file",
            kwargs=_held_backfill_ledger_payload(file, settings.models_path),
            timeout=7200,
            retries=2,
        )
    if held_files:
        await session.commit()

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
        context={"request": request, "not_found": not_found, "no_active_agent": no_active_agent},
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
    """Enqueue extract_file_metadata jobs for all music/video files.

    Per D-04: queues all files regardless of state for backfill.
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
    """Background coroutine to enqueue fingerprint_file jobs with the COMPLETE payload.

    ``FingerprintFilePayload`` (``extra="forbid"``) requires file_id, original_path and
    agent_id; a ``file_id``-only enqueue dead-letters every job (same class as the metadata
    defect above). Build the full payload and serialize via ``model_dump(mode="json")``. The
    deterministic key (``fingerprint_file:<file_id>``) is applied centrally by the
    ``before_enqueue`` hook (35-01).
    """
    for f in files:
        payload = FingerprintFilePayload(
            file_id=f.id,
            original_path=f.original_path,
            agent_id=agent_id,
        )
        await queue.enqueue("fingerprint_file", **payload.model_dump(mode="json"))


@router.post("/api/v1/fingerprint")
async def trigger_fingerprint(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue fingerprint_file jobs for eligible files (per D-14).

    Eligible: files in METADATA_EXTRACTED state, plus files with failed fingerprint results for retry.
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
