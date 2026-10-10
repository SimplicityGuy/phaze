"""Authenticated raw companion capture reports; no parser, approval or media tag mutations."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_companion_capture import CaptureReport, CaptureResponse
from phaze.services.companion_capture import store_capture_report


router = APIRouter(prefix="/api/internal/agent/companion-captures", tags=["agent-internal"])


@router.post("", response_model=CaptureResponse)
async def post_companion_capture(
    body: CaptureReport,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CaptureResponse:
    """Store only the bearer agent's source UUID; all transaction work follows the completed read."""
    try:
        response = await store_capture_report(session, agent.id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Source is not an owned companion") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return response
