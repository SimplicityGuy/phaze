"""Pydantic schema for POST /api/internal/agent/heartbeat (phase-25 D-17, D-19)."""

from pydantic import BaseModel, ConfigDict


class HeartbeatRequest(BaseModel):
    """Heartbeat payload. All three fields required per CONTEXT.md D-17.

    Persisted verbatim to `agents.last_status` JSONB by the handler.
    """

    model_config = ConfigDict(extra="forbid")

    agent_version: str
    worker_pid: int
    queue_depth: int
