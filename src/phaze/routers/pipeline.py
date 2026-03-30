"""Pipeline orchestration router -- trigger endpoints and dashboard UI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from phaze.config import settings
from phaze.database import get_session
from phaze.models.file import FileState
from phaze.services.pipeline import get_files_by_state, get_pipeline_stats


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["pipeline"])

# Hold references to background enqueue tasks to prevent GC (same pattern as scan.py)
_background_tasks: set[asyncio.Task[None]] = set()


async def _enqueue_analysis_jobs(arq_pool: Any, file_ids: list[str]) -> None:
    """Background coroutine to enqueue process_file jobs for a list of file IDs."""
    for fid in file_ids:
        await arq_pool.enqueue_job("process_file", fid)


async def _enqueue_proposal_jobs(arq_pool: Any, batches: list[list[str]]) -> None:
    """Background coroutine to enqueue generate_proposals jobs for batched file IDs."""
    for idx, batch in enumerate(batches):
        await arq_pool.enqueue_job("generate_proposals", batch, idx)


@router.post("/api/v1/analyze")
async def trigger_analysis(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue process_file jobs for all DISCOVERED files (per D-01, D-04).

    One arq job per file. Enqueue runs in a background task to avoid
    HTTP timeout on large file counts (200K+). Returns immediately
    with the expected enqueue count.
    """
    files = await get_files_by_state(session, FileState.DISCOVERED)
    if not files:
        return {"enqueued": 0, "message": "No files in DISCOVERED state"}

    arq_pool = request.app.state.arq_pool
    file_ids = [str(f.id) for f in files]

    # Background enqueue to avoid HTTP timeout (per Research pitfall 2)
    task = asyncio.create_task(_enqueue_analysis_jobs(arq_pool, file_ids))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return {"enqueued": len(file_ids), "message": f"Enqueued {len(file_ids)} files for analysis"}


@router.post("/api/v1/proposals/generate")
async def trigger_proposals(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue generate_proposals jobs for all ANALYZED files in batches (per D-01, D-05).

    Uses settings.llm_batch_size (default 10) for batch chunking.
    """
    files = await get_files_by_state(session, FileState.ANALYZED)
    if not files:
        return {"enqueued_batches": 0, "total_files": 0, "message": "No files in ANALYZED state"}

    file_ids = [str(f.id) for f in files]
    batch_size = settings.llm_batch_size
    batches = [file_ids[i : i + batch_size] for i in range(0, len(file_ids), batch_size)]

    arq_pool = request.app.state.arq_pool
    task = asyncio.create_task(_enqueue_proposal_jobs(arq_pool, batches))
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
    """Render the pipeline dashboard page (per D-03)."""
    stats = await get_pipeline_stats(session)
    context = {
        "request": request,
        "stats": stats,
        "current_page": "pipeline",
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
        context={"request": request, "stats": stats},
    )


@router.post("/pipeline/analyze", response_class=HTMLResponse)
async def trigger_analysis_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger analysis and return response fragment."""
    files = await get_files_by_state(session, FileState.DISCOVERED)
    count = len(files)

    if count > 0:
        arq_pool = request.app.state.arq_pool
        file_ids = [str(f.id) for f in files]
        task = asyncio.create_task(_enqueue_analysis_jobs(arq_pool, file_ids))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "analysis", "count": count},
    )


@router.post("/pipeline/proposals", response_class=HTMLResponse)
async def trigger_proposals_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger proposal generation and return response fragment."""
    files = await get_files_by_state(session, FileState.ANALYZED)
    count = len(files)
    batches_count = 0

    if count > 0:
        arq_pool = request.app.state.arq_pool
        file_ids = [str(f.id) for f in files]
        batch_size = settings.llm_batch_size
        batches = [file_ids[i : i + batch_size] for i in range(0, len(file_ids), batch_size)]
        batches_count = len(batches)
        task = asyncio.create_task(_enqueue_proposal_jobs(arq_pool, batches))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={"request": request, "action": "proposal generation", "count": count, "batches": batches_count},
    )
