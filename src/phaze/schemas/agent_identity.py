"""Pydantic schema for GET /api/internal/agent/whoami response (D-15)."""

from datetime import datetime
from typing import Final

from pydantic import BaseModel, Field


#: The control plane accepts ``subwindows`` and ``beat_grid`` on the analysis completion PUT (phaze-ek9zf). An agent
#: sends them ONLY to a server whose whoami lists this (phaze-7hcgn.5 finding D): a server that predates them forbids
#: unknown fields, so a grid sent to it is a 422 that ``FAILURE_IS_TERMINAL`` would make a lost analysis.
ANALYSIS_GRIDS_CAPABILITY: Final = "analysis_grids"

#: Everything this control plane advertises. Additive: an agent ignores a capability it does not know.
SERVER_CAPABILITIES: Final[tuple[str, ...]] = (ANALYSIS_GRIDS_CAPABILITY,)


class AgentIdentity(BaseModel):
    """Response body for /whoami. RESPONSE-only model -- no extra='forbid'.

     convention: only REQUEST schemas are strict. Response schemas
    stay loose so the server can add fields non-breakingly (the agent's
    Pydantic-parsing will discard unknown keys).
    """

    agent_id: str
    name: str
    scan_roots: list[str]
    created_at: datetime
    # Defaults to none, so an agent reading an older server's body (which has no such key) sees no capabilities.
    capabilities: list[str] = Field(default_factory=list)
