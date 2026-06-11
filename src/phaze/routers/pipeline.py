"""Pipeline orchestration router -- trigger endpoints and dashboard UI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import exists, select

from phaze.config import settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services import enqueue_router
from phaze.services.fingerprint import get_fingerprint_progress
from phaze.services.pipeline import get_files_by_state, get_pipeline_stats


_NO_ACTIVE_AGENT_MESSAGE = "No active agent available — start an agent worker and retry"


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["pipeline"])

# Hold references to background enqueue tasks to prevent GC (same pattern as scan.py)
_background_tasks: set[asyncio.Task[None]] = set()


async def _enqueue_analysis_jobs(queue: Any, files: list[FileRecord], agent_id: str, models_path: str) -> None:
    """Background coroutine to enqueue process_file jobs for a list of files.

    Builds a COMPLETE ``ProcessFilePayload`` per file (file_id, original_path,
    file_type, agent_id, models_path) and serializes it to enqueue kwargs via
    ``model_dump(mode="json")`` so the UUID round-trips as a string and the agent
    worker's ``ProcessFilePayload.model_validate(kwargs)`` (``extra="forbid"``)
    accepts it. Mirrors the working ``agent_files.py`` ExtractMetadataPayload
    pattern -- the pre-Phase-30 bug enqueued only ``file_id``, which dead-lettered
    every job on the agent worker once routing started delivering them.

    ``files`` attributes (``id`` / ``original_path`` / ``file_type``) are already
    loaded by ``get_files_by_state`` and the request never commits, so reading them
    here (after the request session may have closed) does not trigger a lazy load.

    Phase 31 job policy: every process_file enqueue carries an explicit ``timeout``
    and ``retries`` so a single long/bad file no longer churns four full re-analyses.
    All process_file trigger endpoints (``/api/v1/analyze`` + the HTMX ``/pipeline/analyze``)
    funnel through this one helper, so the policy is applied at every enqueue site.
    """
    for f in files:
        payload = ProcessFilePayload(
            file_id=f.id,
            original_path=f.original_path,
            file_type=f.file_type,
            agent_id=agent_id,
            models_path=models_path,
        )
        await queue.enqueue(
            "process_file",
            # 4h bounded: exceeds longest legit set (~3h) yet lets SAQ reclaim a dead/restarted
            # worker's job (spike 31-01 + restart-resilience). Hardcoded like pipeline_scans.py.
            timeout=14400,
            # retries=2 (NOT 1): apply_project_job_defaults (tasks/_shared/queue_defaults.py)
            # only fills jobs still at the SAQ default retries==1, clobbering it to
            # worker_max_retries(4). retries=2 is honored and stays in the locked 1-2 band,
            # killing the 4x re-analysis churn from the original long-file incident.
            retries=2,
            **payload.model_dump(mode="json"),
        )


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

    context = {
        "request": request,
        "stats": stats,
        "current_page": "pipeline",
        "settings_batch_size": settings.llm_batch_size,
        "agents": agents,
        "recent_scans": recent_scans_rows,
    }
    return templates.TemplateResponse(request=request, name="pipeline/dashboard.html", context=context)


@router.get("/pipeline/stats", response_class=HTMLResponse)
async def pipeline_stats_partial(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the stats bar partial for HTMX polling refresh."""
    stats = await get_pipeline_stats(session)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/stats_bar.html",
        # oob_counts=True emits the hx-swap-oob "files ready" paragraphs ONLY on
        # this poll response. The dashboard full-page include omits the flag, so
        # the OOB block is skipped at initial load (where htmx would not honor
        # hx-swap-oob and the ids would collide with stage_cards.html).
        context={"request": request, "stats": stats, "settings_batch_size": settings.llm_batch_size, "oob_counts": True},
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
