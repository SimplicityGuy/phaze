"""GET /api/internal/agent/config -- the DB-override layer, agent-authenticated (phaze-mvq8z.9).

ADR-0019 (runtime config hot-reload) §14: a remote agent has no Postgres reachability of its own (the import-boundary
invariant ``phaze.tasks.heartbeat`` enforces -- see that module's docstring), so it cannot install the
DB-override layer directly the way the api process and control worker do
(``phaze.runtime_config_notify.install_runtime_config_overrides``, wired via a shared asyncpg LISTEN
connection). This endpoint is that layer's HTTP mirror: the SAME reader
(:func:`~phaze.services.runtime_config_overrides.get_runtime_config_overrides`, phaze-mvq8z.6),
restricted to :data:`~phaze.runtime_config.RELOADABLE_KEYS` defensively -- the DB should never hold a
non-reloadable key (``set_runtime_config_override``'s only caller, the admin router, already runs
``RuntimeConfigStore.preview()`` before writing), but an agent trusts nothing about its own upstream
that is not proven true right here rather than inherited from a caller it cannot see -- served under
the SAME ``get_authenticated_agent`` bearer-token dependency every other ``/api/internal/agent/*``
route uses (401 missing/malformed header, 403 unknown/revoked token).

``phaze.tasks.heartbeat``'s heartbeat loop polls this on the SAME cadence as the liveness heartbeat
(the operator's ~30s-latency decision, ADR-0019 (runtime config hot-reload) §14) and reloads its LOCAL
``RuntimeConfigStore`` only when the returned ``digest`` changes -- see that module for the client
side of this round trip.

A failed override read is a 5xx, never an empty override set (phaze-mvq8z.19): ``{}`` carries a
NEW digest, so every polling agent would reload and drop every override it holds. The reader
raises; the error surfaces as a 500, which ``PhazeAgentClient`` retries and then raises, and the
heartbeat's poll treats as "local config unchanged this tick" -- each agent keeps last-good.

The response is NOT per-agent: every agent reading this endpoint sees the SAME override layer (the
DB table has no agent-scoped rows). The auth dependency exists to keep this off the open internet,
not to partition the response.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.runtime_config import RELOADABLE_KEYS
from phaze.schemas.agent_config import AgentConfigResponse, compute_overrides_digest
from phaze.services.runtime_config_overrides import get_runtime_config_overrides


# NOTE: no `from __future__ import annotations` here, and `Agent`/`AsyncSession` are imported at
# module level rather than under `TYPE_CHECKING` -- deliberately mirroring `agent_identity.py` /
# `agent_heartbeat.py` (the sibling `Annotated[Agent, Depends(get_authenticated_agent)]` routes)
# rather than `admin_runtime_config.py`'s `TYPE_CHECKING`-deferred style. FastAPI must resolve the
# REAL `Agent` class from this module's globals when building the dependant for an
# `Annotated[Agent, Depends(...)]` parameter; deferred string annotations broke that resolution at
# runtime here (every request 422'd, including requests with no Authorization header at all) even
# though the identical-looking pattern works for `AsyncSession` in `admin_runtime_config.py`, which
# uses the older `session: AsyncSession = Depends(get_session)` default-style dependency instead.

router = APIRouter(prefix="/api/internal/agent/config", tags=["agent-internal"])


@router.get("", response_model=AgentConfigResponse)
async def get_agent_config(
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AgentConfigResponse:
    """Return the current DB-override layer, restricted to reloadable keys.

    401/403 per :func:`~phaze.routers.agent_auth.get_authenticated_agent` (missing/malformed
    bearer, or a well-formed one whose hash is unknown or revoked) -- acceptance's "rejects
    unauthenticated/revoked agents".
    """
    del agent  # authentication only -- the override layer is not per-agent, see module docstring.
    overrides = await get_runtime_config_overrides(session)
    overrides = {key: value for key, value in overrides.items() if key in RELOADABLE_KEYS}
    return AgentConfigResponse(overrides=overrides, digest=compute_overrides_digest(overrides))
