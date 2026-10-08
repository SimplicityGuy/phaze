"""PATCH /api/internal/agent/junk-quarantine/{review_id} -- the agent's report of one quarantine move (phaze-lwuf6).

The control plane moves an approved ``companion_junk_review`` row to ``executing`` and enqueues
``quarantine_companion`` on the owning agent (``services.junk_quarantine.enqueue_quarantine``); the
agent moves the file, or refuses to, and reports here. ``agent_id`` comes from the bearer token, never
the body (AUTH-01), and a report lands only on a row that agent owns: any other id is a 404, so a
token cannot probe other agents' rows. Idempotent: a row that already left ``executing`` is not
rewritten (200, ``applied=false``).
"""

from __future__ import annotations

from typing import Annotated
import uuid  # noqa: TC003  # FastAPI path parameter annotation.

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002  # FastAPI dependency annotation.

from phaze.database import get_session
from phaze.models.agent import Agent  # noqa: TC001  # FastAPI dependency annotation.
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_junk_quarantine import JunkQuarantineResultPayload, JunkQuarantineResultResponse
from phaze.services.junk_quarantine import QuarantineReportRefused, record_quarantine_result


router = APIRouter(prefix="/api/internal/agent/junk-quarantine", tags=["agent-internal"])


@router.patch("/{review_id}", status_code=status.HTTP_200_OK, response_model=JunkQuarantineResultResponse)
async def patch_junk_quarantine(
    review_id: uuid.UUID,
    body: JunkQuarantineResultPayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JunkQuarantineResultResponse:
    """Record a quarantine move's outcome; on success, retire the moved file's ``files`` row in the same transaction."""
    try:
        outcome = await record_quarantine_result(session, agent.id, review_id, body)
    except QuarantineReportRefused:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="junk review not found") from None
    await session.commit()
    return JunkQuarantineResultResponse(review_id=review_id, status=outcome.status, applied=outcome.applied, retired_files=outcome.retired_files)
