"""Owning-agent validation of reviewed CUE authority immediately before writing."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_tasks import CueSourceCheck
from phaze.services.selected_cue import validate_selected_cue_binding
from phaze.services.stage_status import is_applied


router = APIRouter(prefix="/api/internal/agent/cue-source-check", tags=["agent-internal"])


@router.post("", status_code=204)
async def check_cue_source(
    body: CueSourceCheck,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    owned = await session.scalar(select(FileRecord.id).where(FileRecord.id == body.file_id, FileRecord.agent_id == agent.id))
    if owned is None:
        raise HTTPException(status_code=403, detail="Recording is not owned by this agent")
    if not await is_applied(session, body.file_id):
        raise HTTPException(status_code=409, detail="Recording is no longer applied")
    try:
        await validate_selected_cue_binding(session, body.file_id, body.source_binding, agent_id=agent.id, audio_path=body.audio_path)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)
