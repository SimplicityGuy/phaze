"""CUE sheet router -- selected-source preview and generation, plus a legacy bookmark redirect.

phaze-zimli: per-TRACKLIST generation (``POST /cue/{tracklist_id}/generate``, its preview/error
re-renders and ``cue/partials/cue_row.html``) is gone. A legacy tracklist never carries a qualified
offset, so that path could only refuse; a recording's CUE comes from its reviewed selected source
(docs/design/0027-retire-legacy-timestamp-qualification.md).

phaze-y4s6: the standalone CUE management LIST page (``GET /cue/`` fragment branch,
``cue/partials/cue_list.html``) and the batch-generate action (``POST /cue/generate-batch``,
which rendered the same list) had no live caller left post-v7-cutover -- the live Cue workspace
(``pipeline/partials/cue_workspace.html``) renders its cards inline with no list/pagination UI and
no bulk-generate control. Both were deleted; ``GET /cue/`` now only resolves the legacy bookmark
into the shell (SHELL-05).
"""

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.routers.request_guards import require_same_origin_form
from phaze.schemas.agent_tasks import CueSourceBinding, WriteCueSheetPayload
from phaze.services.selected_cue import agent_supports_selected_cue, build_selected_cue_artifact, validate_selected_cue_binding
from phaze.services.stage_status import is_applied
from phaze.web.template_globals import register_page_name_globals, register_set_glyph_globals


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_set_glyph_globals(templates.env)
register_page_name_globals(templates.env)
router = APIRouter(prefix="/cue", tags=["cue"])


# phaze-6bkk / phaze-9dwb: ``_get_cue_version`` lived here and did ``base_cue.exists()`` plus a full
# ``parent.iterdir()`` regex sweep of the archive directory, synchronously on the API event loop.
# It was wrong on three counts and is gone rather than fixed in place:
#   1. DIST-01 -- the api container mounts no media, so it could only ever report 0.
#   2. Redundant -- ``next_cue_path_and_version`` already computes the number during the write; this
#      re-walked the same directory microseconds later purely to recover it. The version now travels
#      back with the write, agent-side.
#   3. On-loop blocking I/O -- an O(directory entries) syscall sweep of a concert-set directory
#      (thousands of siblings), and on the error path it re-scanned a directory already known to be
#      misbehaving. There is no filesystem call left in this router at all.


@router.get("/", response_class=RedirectResponse, description="Resolve a legacy ``/cue/`` bookmark into the v7.0 shell.")
async def list_cue() -> RedirectResponse:
    """SHELL-05 (D-03): resolve a legacy ``/cue/`` bookmark into the v7.0 shell.

    phaze-y4s6: this used to also serve an in-page HX-filtered/paginated list (rendering
    ``cue/partials/cue_list.html``), but the live v7.0 Cue workspace
    (``pipeline/partials/cue_workspace.html``) renders its cards inline from
    ``services.review.get_cue_review_cards`` with no pagination and never hx-gets this bare
    path -- there is no live caller left to preserve an HX-filter branch for (unlike the sibling
    ``/proposals/`` redirect). The dead list/pagination logic and its template were deleted
    outright.
    """
    return RedirectResponse(url="/s/cue", status_code=302)


async def _selected_preview_response(request: Request, session: AsyncSession, media_id: uuid.UUID, message: str = "") -> HTMLResponse:
    artifact = None
    try:
        artifact = await build_selected_cue_artifact(session, media_id)
    except ValueError as exc:
        message = message or str(exc)
    applied = await is_applied(session, media_id)
    supported = await agent_supports_selected_cue(session, artifact.file.agent_id) if artifact is not None else False
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_selected_cue_preview.html",
        context={"request": request, "media_id": media_id, "artifact": artifact, "applied": applied, "supported": supported, "message": message},
    )


@router.get("/files/{media_id}/preview", response_class=HTMLResponse)
async def selected_cue_preview(request: Request, media_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Lazy preview of one actual reviewed authority, with no archive read or write."""
    return await _selected_preview_response(request, session, media_id)


@router.post("/files/{media_id}/generate", response_class=HTMLResponse, dependencies=[Depends(require_same_origin_form)])
async def generate_selected_cue(
    request: Request,
    media_id: uuid.UUID,
    binding: str = Form(max_length=4096),  # DoS bound on one finite source/inventory pin.
    audio_path: str = Form(max_length=16384),  # DoS bound; current_path is Text storage.
    accept_omitted_tracks: bool = Form(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        reviewed = CueSourceBinding.model_validate_json(binding)
        media = await session.get(FileRecord, media_id, populate_existing=True)
        if media is None:
            raise ValueError("Recording is no longer available")
        artifact = await validate_selected_cue_binding(session, media_id, reviewed, agent_id=media.agent_id, audio_path=audio_path)
        if artifact.omitted_track_count and not accept_omitted_tracks:
            raise ValueError("Acknowledge the tracks without usable timing before generating a CUE that excludes them")
        if not await is_applied(session, media_id):
            raise ValueError("Apply the recording before generating its CUE artifact")
        if not await agent_supports_selected_cue(session, media.agent_id):
            raise ValueError("The owning agent has not reported selected-source CUE support; update its meta worker and wait for a heartbeat")
    except (ValueError, ValidationError) as exc:
        return await _selected_preview_response(request, session, media_id, str(exc))
    payload = WriteCueSheetPayload(
        file_id=media_id, source_binding=reviewed, agent_id=artifact.file.agent_id, audio_path=artifact.file.current_path, content=artifact.content
    )
    await session.commit()  # Release the read transaction before the broker round-trip.
    try:
        await request.app.state.task_router.enqueue_for_file(file_record=artifact.file, task_name="write_cue_sheet", payload=payload)
    except Exception:
        logger.warning("selected CUE dispatch failed", media_id=str(media_id), exc_info=True)
        return await _selected_preview_response(
            request, session, media_id, "Could not queue the CUE artifact; check the agent and task broker, then retry"
        )
    return await _selected_preview_response(
        request, session, media_id, "CUE artifact queued; the worker will recheck this exact reviewed source before writing"
    )
