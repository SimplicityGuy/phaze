"""Pydantic schemas for PATCH /api/internal/agent/proposals/{id}/state (Phase 26 D-28).

Per D-28: joint Proposal + FileRecord state transition in one transaction
with server-side state-machine validation. Allowed transitions:
- ProposalStatus.APPROVED -> EXECUTED  (file_state is optional; typically MOVED)
- ProposalStatus.APPROVED -> FAILED    (file_state is optional; typically UNCHANGED or omitted)
- Same-state PATCH (e.g., EXECUTED -> EXECUTED) is 200 idempotent no-op.
- Any other transition (e.g., EXECUTED -> FAILED, REJECTED -> EXECUTED) is 409.

file_state is never required by proposal_state: the schema only requires current_path
when file_state == "moved" (see the validator below), regardless of proposal_state.

The `_require_path_when_moved` validator enforces the conditional that
CONTEXT.md flags as Claude's discretion: current_path MUST be set when
file_state == "moved" (the new file location); not required when "unchanged".
"""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, model_validator


class ProposalStatePatch(BaseModel):
    """PATCH body for /proposals/{id}/state."""

    model_config = ConfigDict(extra="forbid")  # D-28 -- strict body parsing

    proposal_state: Literal["executed", "failed"]
    file_state: Literal["moved", "unchanged"] | None = None
    current_path: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _require_path_when_moved(self) -> "ProposalStatePatch":
        """Per CONTEXT.md discretion: current_path is required when file_state=='moved'.

        Logically: a "moved" file MUST have a new path. An "unchanged" file
        has no new path (it stayed put). Caller that omits this gets a
        clear ValidationError before any DB work begins.
        """
        if self.file_state == "moved" and self.current_path is None:
            raise ValueError("current_path is required when file_state='moved'")
        return self


class ProposalStateResponse(BaseModel):
    """Success body of PATCH /proposals/{id}/state (D-28)."""

    proposal_id: uuid.UUID
    proposal_state: str
    file_state: str | None = None
    current_path: str | None = None
