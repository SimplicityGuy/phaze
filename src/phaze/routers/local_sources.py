"""Operator local imports and explicit source decisions, within the existing admin boundary.

docs/design/0019-runtime-config-hot-reload.md section 10 and routers/admin_runtime_config.py define the private-LAN reverse
proxy trust boundary for operator routes. Agent bearer tokens authenticate workers,
not operators. This router uses that existing boundary and audits each decision/import;
it does not change access control or write media files.
"""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.database import get_session
from phaze.schemas.local_source_import import ImportLocalSource, ImportResult, SelectedRecordingSource, SourceDecision, SourceKind
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source


router = APIRouter(prefix="/api/local-sources", tags=["admin"])
logger = structlog.get_logger(__name__)


@router.post("/import", response_model=ImportResult)
@router.post("/reimport", response_model=ImportResult)
async def import_source(body: ImportLocalSource, session: Annotated[AsyncSession, Depends(get_session)]) -> ImportResult:
    """Import appends observations and pending candidates; it never changes authority."""
    try:
        result = await import_local_source(session, body)
    except (ValueError, PermissionError) as exc:
        logger.info("local_source_import_rejected", media_id=str(body.media_id), reason=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    logger.info("local_source_imported", media_id=str(body.media_id), raw_observation_id=str(result.raw_observation_id))
    return result


@router.post("/decision", response_model=SelectedRecordingSource | None)
async def decide_source(body: SourceDecision, session: Annotated[AsyncSession, Depends(get_session)]) -> SelectedRecordingSource | None:
    """Persist explicit operator intent; clients cannot supply an actor identity."""
    try:
        result = await decide_local_source(session, body, actor="admin")
    except (ValueError, PermissionError) as exc:
        logger.info("local_source_decision_rejected", media_id=str(body.media_id), decision_id=str(body.decision_id), reason=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    logger.info("local_source_decided", media_id=str(body.media_id), decision_id=str(body.decision_id), action=body.action)
    return result


@router.get("/recordings/{media_id}/selected", response_model=SelectedRecordingSource | None)
async def selected_source(
    media_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)], kind: SourceKind = "tracklist"
) -> SelectedRecordingSource | None:
    """Retained selected content is readable offline without contacting its owning agent."""
    return await get_selected_recording_source(session, media_id, kind=kind)
