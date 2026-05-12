"""Pydantic schemas for /api/internal/agent/execution-log (phase-25 D-13, D-15).

Two request schemas use `extra="forbid"` (D-16) to prevent agents from sneaking
extra fields onto the wire. Response schemas remain loose so we can extend them
non-breakingly. Status fields are typed `ExecutionStatus` so Pydantic validates
the four allowed lifecycle values (`pending`, `in_progress`, `completed`, `failed`).

Per D-13 the agent supplies `id` on POST so retries can be coalesced with
`INSERT ... ON CONFLICT (id) DO NOTHING`. Per D-15 status transitions are
monotonic; the router enforces the ladder + terminal-state guard at PATCH time.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from phaze.enums.execution import ExecutionStatus


class ExecutionLogCreate(BaseModel):
    """Agent-supplied ExecutionLog row to insert.

    Per D-13, the agent generates `id` (uuid.uuid4 on the agent) and persists
    it in SAQ job state. Server does `INSERT ... ON CONFLICT (id) DO NOTHING`
    so retries are silent no-ops. `agent_id` is NEVER part of the body --
    handlers source it from `Depends(get_authenticated_agent)` only (AUTH-01).
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    proposal_id: uuid.UUID
    operation: str = Field(min_length=1, max_length=20)
    source_path: str = Field(min_length=1)
    destination_path: str = Field(min_length=1)
    sha256_verified: bool
    status: ExecutionStatus
    error_message: str | None = None


class ExecutionLogPatch(BaseModel):
    """Partial-update body for PATCH /execution-log/{id}.

    Status transitions enforced monotonic per D-15: PENDING(0) < IN_PROGRESS(1)
    < COMPLETED(2) < FAILED(3). Same-status PATCH is allowed (idempotent retry);
    backward transitions return 409 with detail `"execution-log status would
    regress"`; PATCH against a terminal row returns 409 with detail
    `"execution-log status is terminal"`.
    """

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    error_message: str | None = None
    sha256_verified: bool | None = None


class ExecutionLogCreateResponse(BaseModel):
    """Minimal echo response confirming the create (or replay no-op) (D-19)."""

    agent_id: str
    execution_log_id: uuid.UUID


class ExecutionLogPatchResponse(BaseModel):
    """Minimal echo response confirming the patch (D-19)."""

    agent_id: str
    execution_log_id: uuid.UUID
    status: ExecutionStatus
