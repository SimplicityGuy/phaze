"""GET /api/internal/agent/whoami -- agent identity probe (Phase 26 D-15..D-17).

Returns the calling agent's identity (id, name, scan_roots, created_at) so the
agent worker can verify (a) its bearer token is valid and (b) the token-derived
agent_id matches the operator-supplied PHAZE_AGENT_QUEUE env var at startup
(Plan 10 anti-misconfiguration probe, RESEARCH Pitfall 1).

Consumed by `whoami_with_retry` (`tasks/_shared/agent_bootstrap.py`) at agent-worker /
watcher startup. The Phase 29 Agents admin page (`routers/admin_agents.py`) does NOT call
this endpoint -- it classifies liveness from `Agent.last_seen_at` / `last_status` instead.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_identity import AgentIdentity


router = APIRouter(prefix="/api/internal/agent/whoami", tags=["agent-internal"])


@router.get("", status_code=status.HTTP_200_OK, response_model=AgentIdentity)
async def whoami(
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
) -> AgentIdentity:
    """Return the calling agent's identity.

    Per D-15: response body is `{agent_id, name, scan_roots, created_at}`.
    No request body. agent_id is NEVER derived from request body (AUTH-01); it
    comes from the auth dep's token lookup.

    401: missing/malformed Authorization header (handled by HTTPBearer auto_error
         in get_authenticated_agent's dep chain).
    403: well-formed bearer whose hash is unknown OR whose row has
         revoked_at IS NOT NULL (handled inside get_authenticated_agent).
    200: success.
    """
    return AgentIdentity(
        agent_id=agent.id,
        name=agent.name,
        scan_roots=agent.scan_roots,
        created_at=agent.created_at,
    )
