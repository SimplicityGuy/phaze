"""Pipeline orchestration router -- trigger endpoints and dashboard UI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import exists, select
import structlog

from phaze.config import settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.services import enqueue_router
from phaze.services.analysis_enqueue import enqueue_process_file
from phaze.services.fingerprint import get_fingerprint_progress
from phaze.services.pipeline import (
    get_files_by_state,
    get_pipeline_stats,
    get_queue_activity,
    get_stage_progress,
    queue_progress_percent,
)
from phaze.services.pipeline_counters import read_counters


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
_NODE_COMPLETED_FNS: dict[str, tuple[str, ...]] = {
    "metadata": ("extract_file_metadata",),
    "fingerprint": ("fingerprint_file",),
    "analyze": ("process_file",),
    "scan_search": ("scan_live_set", "search_tracklist"),
    "scrape": ("scrape_and_store_tracklist",),
    "match": ("match_tracklist_to_discogs",),
    "proposals": ("generate_proposals",),
}


async def _read_pipeline_counters(app_state: Any) -> dict[str, dict[str, int]]:
    """Read the maintained per-function Redis counters, degrading to ``{}`` on any failure.

    Mirrors :func:`get_queue_activity`'s failure isolation: a missing ``app.state``
    handle (the test client skips the lifespan) or any Redis hiccup must degrade the
    counter source to an empty dict so the 5s dashboard poll renders from DB-truth and
    NEVER 500s (threat T-35-09). The shared ``app.state.redis`` client (decode_responses)
    is preferred; the SAQ-internal ``controller_queue.redis`` is the fallback handle.
    """
    try:
        redis = getattr(app_state, "redis", None)
        if redis is None:
            redis = app_state.controller_queue.redis
        return await read_counters(redis)
    except Exception:
        logger.warning("pipeline_counters_degraded", exc_info=True)
        return {}


def _reconciled_done(node: str, stage_done: int, counters: dict[str, dict[str, int]]) -> int:
    """Return the DB-truth ``done`` (D-03), or the ``completed`` counter as a backstop.

    DB-truth wins whenever ``stage_done > 0``. Only when the DB source reads 0 do we fall
    back to the sum of the node's mapped ``completed`` counters (D-02 backstop) — and only
    if that sum is itself > 0.
    """
    if stage_done > 0:
        return stage_done
    fallback = sum(counters.get(fn, {}).get("completed", 0) for fn in _NODE_COMPLETED_FNS.get(node, ()))
    return fallback if fallback > 0 else stage_done


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
        return _reconciled_done(node, int(stage[node]["done"] or 0), counters)

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
    ``ProcessFilePayload``, and the job policy (``timeout=14400`` / ``retries=2``)
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


async def _enqueue_proposal_jobs(queue: Any, batches: list[list[str]]) -> None:
    """Background coroutine to enqueue generate_proposals jobs for batched file IDs."""
    for idx, batch in enumerate(batches):
        await queue.enqueue("generate_proposals", file_ids=batch, batch_index=idx)


@router.post("/api/v1/analyze")
async def trigger_analysis(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue process_file jobs for all DISCOVERED files (per D-01, D-04).

    One SAQ job per file. Enqueue runs in a background task to avoid
    HTTP timeout on large file counts. Returns immediately
    with the expected enqueue count.
    """
    files = await get_files_by_state(session, FileState.DISCOVERED)
    if not files:
        return {"enqueued": 0, "message": "No files in DISCOVERED state"}

    try:
        routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        return {"enqueued": 0, "message": _NO_ACTIVE_AGENT_MESSAGE}

    # process_file is an AGENT_TASK, so resolve_queue_for_task always returns a
    # non-None agent_id (RoutedQueue.agent_id is only None for controller tasks);
    # cast narrows str | None -> str for the ProcessFilePayload.agent_id field.
    agent_id = cast("str", routed.agent_id)

    # Background enqueue to avoid HTTP timeout (per Research pitfall 2)
    task = asyncio.create_task(_enqueue_analysis_jobs(routed.queue, files, agent_id, settings.models_path))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"enqueued": len(files), "message": f"Enqueued {len(files)} files for analysis"}


@router.post("/api/v1/proposals/generate")
async def trigger_proposals(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue generate_proposals jobs for files with both metadata and analysis (per D-02 convergence gate).

    Uses settings.llm_batch_size (default 10) for batch chunking.
    """
    # Per D-02: convergence gate -- only propose for files with BOTH metadata AND analysis
    stmt = (
        select(FileRecord)
        .where(
            FileRecord.state.in_(
                [
                    FileState.ANALYZED,
                    FileState.METADATA_EXTRACTED,
                ]
            )
        )
        .where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id)))
        .where(exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id)))
    )
    result = await session.execute(stmt)
    files = list(result.scalars().all())
    if not files:
        return {"enqueued_batches": 0, "total_files": 0, "message": "No files ready for proposals (need both metadata and analysis)"}

    file_ids = [str(f.id) for f in files]
    batch_size = settings.llm_batch_size
    batches = [file_ids[i : i + batch_size] for i in range(0, len(file_ids), batch_size)]

    routed = await enqueue_router.resolve_queue_for_task("generate_proposals", request.app.state, session)
    task = asyncio.create_task(_enqueue_proposal_jobs(routed.queue, batches))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {
        "enqueued_batches": len(batches),
        "total_files": len(file_ids),
        "message": f"Enqueued {len(batches)} batches ({len(file_ids)} files) for proposal generation",
    }


@router.get("/pipeline/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the pipeline dashboard page (per D-03).

    Phase 27 D-05/D-06 extension: the dashboard now exposes ``agents`` (the
    non-revoked agent list driving the Trigger Scan card dropdown) and
    ``recent_scans`` (the last 10 non-LIVE ScanBatches with ``agent_name`` +
    ``elapsed_seconds`` attached for the Recent Scans mini-table). The LIVE
    sentinel batches are excluded -- they are an internal watcher-ingestion
    state, not an operator-triggered event.
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
    activity = await get_queue_activity(request.app.state, session)
    queue_progress = queue_progress_percent(stats["analyzed"], activity["agent_busy"])

    # Phase 35 (35-04): per-DAG-node done/total/active reconciled from get_stage_progress
    # (DB-truth) + the maintained completed counters (backstop) + the queue activity. The
    # 35-05 canvas seeds these into $store.pipeline on the full-page render; here they ride
    # the dashboard context. _build_dag_context isolates its own counter-source failures.
    dag_ctx = await _build_dag_context(request.app.state, session, activity)

    context = {
        "request": request,
        "stats": stats,
        "current_page": "pipeline",
        "settings_batch_size": settings.llm_batch_size,
        "agents": agents,
        "recent_scans": recent_scans_rows,
        **activity,
        **dag_ctx,
        "queue_progress_percent": queue_progress,
    }
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
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/stats_bar.html",
        # oob_counts=True emits the hx-swap-oob "files ready" paragraphs ONLY on
        # this poll response. The dashboard full-page include omits the flag, so
        # the OOB block is skipped at initial load (where htmx would not honor
        # hx-swap-oob and the ids would collide with stage_cards.html).
        context={
            "request": request,
            "stats": stats,
            "settings_batch_size": settings.llm_batch_size,
            "oob_counts": True,
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
    """HTMX endpoint: trigger analysis and return response fragment."""
    files = await get_files_by_state(session, FileState.DISCOVERED)
    count = len(files)
    no_active_agent = False

    if count > 0:
        try:
            routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session)
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            # process_file is an AGENT_TASK -- resolve always returns a non-None
            # agent_id; cast narrows str | None -> str for ProcessFilePayload.
            agent_id = cast("str", routed.agent_id)
            task = asyncio.create_task(_enqueue_analysis_jobs(routed.queue, files, agent_id, settings.models_path))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "analysis", "count": count, "no_active_agent": no_active_agent},
    )


@router.post("/pipeline/proposals", response_class=HTMLResponse)
async def trigger_proposals_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger proposal generation and return response fragment."""
    # Per D-02: convergence gate -- only propose for files with BOTH metadata AND analysis
    stmt = (
        select(FileRecord)
        .where(
            FileRecord.state.in_(
                [
                    FileState.ANALYZED,
                    FileState.METADATA_EXTRACTED,
                ]
            )
        )
        .where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id)))
        .where(exists(select(AnalysisResult.id).where(AnalysisResult.file_id == FileRecord.id)))
    )
    result = await session.execute(stmt)
    files = list(result.scalars().all())
    count = len(files)
    batches_count = 0

    if count > 0:
        file_ids = [str(f.id) for f in files]
        batch_size = settings.llm_batch_size
        batches = [file_ids[i : i + batch_size] for i in range(0, len(file_ids), batch_size)]
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


async def _enqueue_extraction_jobs(queue: Any, file_ids: list[str]) -> None:
    """Background coroutine to enqueue extract_file_metadata jobs."""
    for fid in file_ids:
        await queue.enqueue("extract_file_metadata", file_id=fid)


@router.post("/api/v1/extract-metadata")
async def trigger_metadata_extraction(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue extract_file_metadata jobs for all music/video files.

    Per D-04: queues all files regardless of state for backfill.
    Per D-09: manual API endpoint for re-extraction.
    """
    music_video_types = [ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in (FileCategory.MUSIC, FileCategory.VIDEO)]
    stmt = select(FileRecord).where(FileRecord.file_type.in_(music_video_types))
    result = await session.execute(stmt)
    files = list(result.scalars().all())

    if not files:
        return {"enqueued": 0, "message": "No music/video files found"}

    try:
        routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        return {"enqueued": 0, "message": _NO_ACTIVE_AGENT_MESSAGE}

    file_ids = [str(f.id) for f in files]

    task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, file_ids))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"enqueued": len(file_ids), "message": f"Enqueued {len(file_ids)} files for metadata extraction"}


@router.post("/pipeline/extract-metadata", response_class=HTMLResponse)
async def trigger_extraction_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger metadata extraction and return response fragment."""
    music_video_types = [ext.lstrip(".") for ext, cat in EXTENSION_MAP.items() if cat in (FileCategory.MUSIC, FileCategory.VIDEO)]
    stmt = select(FileRecord).where(FileRecord.file_type.in_(music_video_types))
    result = await session.execute(stmt)
    files = list(result.scalars().all())
    count = len(files)
    no_active_agent = False

    if count > 0:
        try:
            routed = await enqueue_router.resolve_queue_for_task("extract_file_metadata", request.app.state, session)
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            file_ids = [str(f.id) for f in files]
            task = asyncio.create_task(_enqueue_extraction_jobs(routed.queue, file_ids))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "metadata extraction", "count": count, "no_active_agent": no_active_agent},
    )


# --- Fingerprint endpoints (Phase 16, D-14, D-15) ---


async def _enqueue_fingerprint_jobs(queue: Any, file_ids: list[str]) -> None:
    """Background coroutine to enqueue fingerprint_file jobs."""
    for fid in file_ids:
        await queue.enqueue("fingerprint_file", file_id=fid)


@router.post("/api/v1/fingerprint")
async def trigger_fingerprint(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue fingerprint_file jobs for eligible files (per D-14).

    Eligible: files in METADATA_EXTRACTED state, plus files with failed fingerprint results for retry.
    """
    # Files in METADATA_EXTRACTED state (ready for fingerprinting)
    files = await get_files_by_state(session, FileState.METADATA_EXTRACTED)

    # Also include files with failed fingerprint results (retry per D-16)
    failed_stmt = (
        select(FileRecord)
        .join(FingerprintResult, FingerprintResult.file_id == FileRecord.id)
        .where(FingerprintResult.status == "failed")
        .where(FileRecord.state != FileState.FINGERPRINTED)
    )
    failed_result = await session.execute(failed_stmt)
    failed_files = list(failed_result.scalars().all())

    # Deduplicate by ID
    seen_ids: set[str] = set()
    all_file_ids: list[str] = []
    for f in [*files, *failed_files]:
        fid = str(f.id)
        if fid not in seen_ids:
            seen_ids.add(fid)
            all_file_ids.append(fid)

    if not all_file_ids:
        return {"enqueued": 0, "message": "No files eligible for fingerprinting"}

    try:
        routed = await enqueue_router.resolve_queue_for_task("fingerprint_file", request.app.state, session)
    except enqueue_router.NoActiveAgentError:
        return {"enqueued": 0, "message": _NO_ACTIVE_AGENT_MESSAGE}

    task = asyncio.create_task(_enqueue_fingerprint_jobs(routed.queue, all_file_ids))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"enqueued": len(all_file_ids), "message": f"Enqueued {len(all_file_ids)} files for fingerprinting"}


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
    """HTMX endpoint: trigger fingerprinting and return response fragment."""
    files = await get_files_by_state(session, FileState.METADATA_EXTRACTED)
    count = len(files)
    no_active_agent = False

    if count > 0:
        try:
            routed = await enqueue_router.resolve_queue_for_task("fingerprint_file", request.app.state, session)
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True
        else:
            file_ids = [str(f.id) for f in files]
            task = asyncio.create_task(_enqueue_fingerprint_jobs(routed.queue, file_ids))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "fingerprinting", "count": count, "no_active_agent": no_active_agent},
    )
