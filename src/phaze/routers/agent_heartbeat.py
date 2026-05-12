"""POST /api/internal/agent/heartbeat -- agent liveness signal (phase-25 D-17, D-19)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_heartbeat import HeartbeatRequest


router = APIRouter(prefix="/api/internal/agent/heartbeat", tags=["agent-internal"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def post_heartbeat(
    body: HeartbeatRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Update agents.last_seen_at and last_status. Returns 204 No Content (D-19).

    Per D-17, body shape is `{agent_version: str, worker_pid: int, queue_depth: int}` -- all required.
    Persists payload verbatim to JSONB column `agents.last_status` (Plan 01 + migration 014).
    """
    await session.execute(
        update(Agent)
        .where(Agent.id == agent.id)
        .values(
            last_seen_at=func.now(),
            last_status=body.model_dump(),
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
