"""POST /api/internal/agent/companion-features -- store what the agent read inside its companions (phaze-osy6j).

Posted by the scan and the watcher right after the upsert that created a companion's row, and by the
``extract_companion_features`` backfill task. Idempotent: a replay rewrites the same row. ``agent_id``
comes from the bearer token, never the body (AUTH-01), and a record can only land on one of the
calling agent's own companion rows.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002  # FastAPI dependency annotation.
import structlog

from phaze.database import get_session
from phaze.models.agent import Agent  # noqa: TC001  # FastAPI dependency annotation.
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_companion_features import CompanionFeaturesChunk, CompanionFeaturesResponse
from phaze.services.companion_content import store_companion_features


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/internal/agent/companion-features", tags=["agent-internal"])


@router.post("", status_code=status.HTTP_200_OK, response_model=CompanionFeaturesResponse)
async def post_companion_features(
    body: CompanionFeaturesChunk,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CompanionFeaturesResponse:
    """Upsert one chunk of companion content features and refresh the stamp verdicts they touch."""
    outcome = await store_companion_features(session, agent.id, body.features)
    await session.commit()
    if outcome.unknown:
        logger.info("companion features for unknown paths dropped", agent_id=agent.id, unknown=outcome.unknown, stored=outcome.stored)
    return CompanionFeaturesResponse(agent_id=agent.id, stored=outcome.stored, unknown=outcome.unknown)
