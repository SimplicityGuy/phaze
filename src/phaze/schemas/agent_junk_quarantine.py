"""Wire contract for the agent -> control junk-quarantine result callback (phaze-lwuf6).

``PATCH /api/internal/agent/junk-quarantine/{review_id}`` is how the agent reports what the
``quarantine_companion`` job did with one approved ``companion_junk_review`` row. The agent never
touches Postgres (D-25), so the row's terminal status and the retirement of the moved file's
``files`` row both land through this endpoint. ``extra="forbid"`` per D-16.
"""

from __future__ import annotations

from typing import Literal
import uuid  # noqa: TC003 -- pydantic resolves annotations at runtime

from pydantic import BaseModel, ConfigDict, Field, model_validator


_ERROR_MESSAGE_MAX = 2000
"""The DoS bound on a failure reason; ``error_message`` is an unbounded ``Text`` column."""


class JunkQuarantineResultPayload(BaseModel):
    """The terminal outcome of one quarantine move: ``quarantined`` with its destination, or ``failed`` with a reason."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["quarantined", "failed"]
    destination_path: str | None = Field(default=None, min_length=1)
    # True when the source was already gone and the destination corroborated an earlier move.
    replayed: bool = False
    error_message: str | None = Field(default=None, min_length=1, max_length=_ERROR_MESSAGE_MAX)

    @model_validator(mode="after")
    def _outcome_carries_its_detail(self) -> JunkQuarantineResultPayload:
        """A success names where the file went; a failure says why, and nothing else."""
        if self.status == "quarantined" and (self.destination_path is None or self.error_message is not None):
            msg = "a quarantined outcome carries destination_path and no error_message"
            raise ValueError(msg)
        if self.status == "failed" and (self.error_message is None or self.destination_path is not None or self.replayed):
            msg = "a failed outcome carries error_message only"
            raise ValueError(msg)
        return self


class JunkQuarantineResultResponse(BaseModel):
    """Ack for a quarantine result callback."""

    model_config = ConfigDict(extra="forbid")

    review_id: uuid.UUID
    status: str
    # False when the row had already left ``executing``: a replayed report is a 200 no-op.
    applied: bool
    # ``files`` rows retired with the move (0 on a failure or a replay).
    retired_files: int = 0
