"""POST /api/internal/agent/scratch/live -- compute-scratch janitor liveness probe (phaze-5cvbz).

The compute agent's startup janitor (``phaze.tasks.agent_worker._maybe_sweep_scratch``) cannot
tell "is a durable job still claiming this scratch entry?" on its own -- the module MUST NOT
import ``phaze.database`` / SQLAlchemy (D-25 import boundary), so it previously relied SOLELY on
a fixed ``min_age_sec`` ceiling as a time-since-liveness proxy. That proxy is unsafe: a durable
``process_file`` job can sit ``queued`` (never dequeued under a concurrency-1 clamp, or parked at
the far-future ``SENTINEL`` by a stage pause) far longer than any finite ceiling while its scratch
copy's mtime stays frozen at push-completion. This endpoint answers the liveness question
directly off ``saq_jobs`` (via :func:`phaze.services.pipeline.get_live_job_keys`, the SAME
degrade-safe live-broker read recovery already uses) so the agent-side sweep only deletes an
age-eligible entry once the control plane confirms nothing durable still needs it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_scratch import ScratchLivenessRequest, ScratchLivenessResponse
from phaze.services.pipeline import get_live_job_keys


router = APIRouter(prefix="/api/internal/agent/scratch", tags=["agent-internal"])


@router.post("/live", response_model=ScratchLivenessResponse)
async def get_scratch_liveness(
    body: ScratchLivenessRequest,
    _agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ScratchLivenessResponse:
    """Return which of ``body.file_ids`` have a live ``process_file`` job, plus a ``push_file`` flag.

    Reads the WHOLE live-key set once (degrade-safe: an unreadable ``saq_jobs`` -- pre-migration
    env or a transient DB hiccup -- returns an empty set rather than raising, mirroring every
    other ``get_live_job_keys`` consumer) and filters in Python, rather than issuing a second
    SQL shape against ``saq_jobs`` -- this endpoint is a startup-only, low-QPS probe (one call
    per compute-agent restart), so reuse over a bespoke IN-list query keeps this module's
    liveness read in permanent lockstep with recovery's (``phaze.tasks.reenqueue``).

    ``live_file_ids`` is the exact ``body.file_ids`` subset with a ``queued``/``active``
    ``process_file:<file_id>`` key -- this INCLUDES a job parked at the pause ``SENTINEL``
    (status stays ``queued`` while parked), closing the indefinite-park gap a fixed age ceiling
    cannot cover on its own.

    ``push_file_active`` is coarser and IGNORES ``body.file_ids``: True when ANY ``push_file``
    job is currently queued/active, gating the shared ``.rsync-partial`` staging directory
    whose mtime freezes at transfer START rather than tracking any one file.
    """
    live_keys = await get_live_job_keys(session)
    live_file_ids = [file_id for file_id in body.file_ids if f"process_file:{file_id}" in live_keys]
    push_file_active = any(key.startswith("push_file:") for key in live_keys)
    return ScratchLivenessResponse(live_file_ids=live_file_ids, push_file_active=push_file_active)
