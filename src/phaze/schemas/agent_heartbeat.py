"""Pydantic schema for POST /api/internal/agent/heartbeat (phase-25 D-17, D-19)."""

from pydantic import BaseModel, ConfigDict


class HeartbeatRequest(BaseModel):
    """Heartbeat payload. The original three fields are required per CONTEXT.md D-17.

    Persisted to `agents.last_status` JSONB by the handler (per-lane when `lane` is set --
    see :mod:`phaze.routers.agent_heartbeat`).
    """

    model_config = ConfigDict(extra="forbid")

    agent_version: str
    worker_pid: int
    queue_depth: int
    lane: str | None = None
    """Which lane worker sent this beat (phaze-30fo): analyze|fingerprint|meta|io.

    OPTIONAL and defaulting to None on purpose. Every lane now heartbeats, but an agent
    running an older image (or in all-mode, where there is no lane split) posts without
    this field, and a required field would 422 every one of those beats -- turning a
    liveness fix into a liveness outage during a rolling deploy. `None` means
    "unlaned beat", which the handler stores exactly the way it always did.
    """
