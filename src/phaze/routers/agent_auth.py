"""Bearer-token authentication dependency for /api/internal/agent/* routes.

NOT a router module -- exports `get_authenticated_agent` as a FastAPI dependency
for use in every agent-internal route handler:

    agent: Annotated[Agent, Depends(get_authenticated_agent)]

Status codes (per phase-25 D-06):
  - 401 Unauthorized -- missing or malformed `Authorization` header (emitted by
    `HTTPBearer(auto_error=True)`; includes `WWW-Authenticate: Bearer` per RFC 6750).
  - 403 Forbidden -- well-formed bearer whose hash is unknown OR whose row has
    `revoked_at IS NOT NULL`. Both surface as 403 (intentionally indistinguishable
    to avoid an oracle for "does this agent_id exist").

Token format (phase-25 D-01..D-04):
  - Wire string: `phaze_agent_<32 urlsafe-base64 random bytes>` (~55 chars).
  - Stored as `sha256(wire_string).hexdigest()` in `agents.token_hash` (64 chars).
  - SHA-256 is sufficient because the input is already uniform-random; no KDF
    needed (would break the indexed-equality-lookup model).
  - Per-request verification is a single indexed SELECT on the partial index
    `ix_agents_token_hash_active (token_hash) WHERE revoked_at IS NULL`
    (migration 014). Revocation = set `revoked_at = NOW()`; the next request
    misses the predicate and returns 403, NO server restart required (AUTH-04).
    Do NOT add an in-process cache -- caching would defeat the immediate-revoke
    contract proven by `test_revoke_blocks_next_call`.
"""

import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent


bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    description="Per-agent bearer token. Format: phaze_agent_<32 urlsafe-base64 bytes>.",
)
"""The HTTPBearer instance that drives OpenAPI security-scheme emission.

`scheme_name="bearerAuth"` lands the auto-generated `components.securitySchemes.bearerAuth`
entry on `/openapi.json` (FastAPI introspection). Every route depending on
`get_authenticated_agent` inherits the lock icon in /docs.
"""


def hash_token(token: str) -> str:
    """Return `sha256(token).hex()` of the entire wire token (prefix included).

    Per phase-25 D-02: the server NEVER strips the `phaze_agent_` prefix before
    hashing. A future prefix change is therefore a versioning event that
    invalidates all existing tokens -- the right behaviour.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_authenticated_agent(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Agent:
    """Resolve the calling agent from the bearer token.

    Raises:
        HTTPException(403): well-formed bearer whose hash is unknown OR whose
            row has `revoked_at IS NOT NULL`. Both cases are indistinguishable
            to the client by design (phase-25 D-06).

    The missing/malformed-header 401 case is raised by `HTTPBearer` BEFORE this
    function runs; that response includes `WWW-Authenticate: Bearer` per RFC 6750.
    """
    token_hash = hash_token(credentials.credentials)
    # Predicate must be `Agent.revoked_at.is_(None)` so SQLAlchemy renders
    # `revoked_at IS NULL`, matching migration 014's partial index predicate
    # (`ix_agents_token_hash_active ... WHERE revoked_at IS NULL`).
    stmt = select(Agent).where(Agent.token_hash == token_hash, Agent.revoked_at.is_(None))
    agent = (await session.execute(stmt)).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return agent
