"""Pydantic schema for GET /api/internal/agent/whoami response (D-15)."""

from datetime import datetime

from pydantic import BaseModel


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
