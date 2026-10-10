"""Reviewed local-source JSON APIs and native HTMX form controls.

Operator access follows docs/design/0019-runtime-config-hot-reload.md section 10,
with server-owned audit identity. Source review does not authorize media/tag/path writes.
"""

from json import JSONDecodeError
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.routers.record import templates
from phaze.schemas.local_source_import import ImportLocalSource, ImportResult, SelectedRecordingSource, SourceDecision, SourceKind
from phaze.services.companion_view import command_body, fragment, html_requested, request_schema, validation_message
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source


router = APIRouter(prefix="/api/local-sources", tags=["admin"])
logger = structlog.get_logger(__name__)


def _error(request: Request, error: ValueError | PermissionError) -> HTMLResponse:
    message = validation_message(error) if isinstance(error, ValidationError) else str(error)
    if not html_requested(request):
        raise HTTPException(status_code=422 if isinstance(error, (ValidationError, JSONDecodeError)) else 409, detail=message) from error
    return fragment(templates, request, "record/companions/_action.html", message=message, failed=True)


@router.post("/import", response_model=ImportResult, openapi_extra=request_schema(ImportLocalSource))
@router.post("/reimport", response_model=ImportResult, openapi_extra=request_schema(ImportLocalSource))
async def import_source(request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> ImportResult | HTMLResponse:
    """Both source kinds remain pending until a separate explicit review decision."""
    try:
        parsed = await command_body(request, ImportLocalSource)
        body = ImportLocalSource.model_validate(parsed)
        async with session.begin_nested():
            result = await import_local_source(session, body)
    except (ValueError, PermissionError) as exc:
        logger.info("local_source_import_rejected", reason=str(exc))
        return _error(request, exc)
    await session.commit()
    logger.info("local_source_imported", media_id=str(body.media_id), raw_observation_id=str(result.raw_observation_id))
    if html_requested(request):
        return fragment(
            templates,
            request,
            "record/companions/_action.html",
            message="Stored text imported; source review remains separate.",
            result=result,
            file_id=body.media_id,
            failed=False,
        )
    return result


@router.post("/decision", response_model=SelectedRecordingSource | None, openapi_extra=request_schema(SourceDecision))
async def decide_source(request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> SelectedRecordingSource | HTMLResponse | None:
    """Expected revisions/tokens and explicit conflict/CUE mapping are checked by the service."""
    try:
        parsed = await command_body(request, SourceDecision)
        body = SourceDecision.model_validate(parsed)
        async with session.begin_nested():
            result = await decide_local_source(session, body, actor="admin")
    except (ValueError, PermissionError) as exc:
        logger.info("local_source_decision_rejected", reason=str(exc))
        return _error(request, exc)
    await session.commit()
    logger.info("local_source_decided", media_id=str(body.media_id), decision_id=str(body.decision_id), action=body.action)
    if html_requested(request):
        return fragment(
            templates,
            request,
            "record/companions/_action.html",
            message="Source selected." if body.action == "select" else "Candidate rejected; reviewed content retained.",
            file_id=body.media_id,
            failed=False,
        )
    return result


@router.get("/recordings/{media_id}/selected", response_model=SelectedRecordingSource | None)
async def selected_source(
    request: Request, media_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)], kind: SourceKind = "tracklist"
) -> SelectedRecordingSource | HTMLResponse | None:
    """Reviewed content remains readable when its source is offline or stale."""
    result = await get_selected_recording_source(session, media_id, kind=kind)
    if html_requested(request):
        return fragment(templates, request, "record/companions/_selected.html", selected=result, file_id=media_id, kind=kind)
    return result
