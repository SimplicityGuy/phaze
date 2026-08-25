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
import unicodedata
import uuid

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ProposalStatePatch(BaseModel):
    """PATCH body for /proposals/{id}/state."""

    model_config = ConfigDict(extra="forbid")  # D-28 -- strict body parsing

    proposal_state: Literal["executed", "failed"]
    file_state: Literal["moved", "unchanged"] | None = None
    current_path: str | None = None
    error_message: str | None = None

    @field_validator("current_path", mode="after")
    @classmethod
    def _nfc_normalize_current_path(cls, v: str | None) -> str | None:
        """phaze-sy8z3: NFC-normalize the post-move path, the way every OTHER path writer does.

        ``current_path`` sinks into ``FileRecord.current_path`` -- the same column
        ``tasks/scan.py`` ("Pitfall 3: NFC-normalize EVERY path field"),
        ``agent_watcher/poster.py``, ``routers/agent_files.py`` ("Pitfall 7: NFC-normalize
        defensively") and ``routers/scan.py`` all normalize before writing. The execute path was
        the one writer that did not: ``tasks/execution.py``'s ``_report_success`` sends
        ``str(proposed)``, built from ``proposed_filename``, which arrives from LLM JSON via
        ``sanitize_pg_text`` -- and sanitize_pg_text strips NULs and lone surrogates without
        touching normalization form. So an NFD emission landed verbatim in a column every other
        writer keeps NFC.

        WHY THE SCHEMA AND NOT THE ROUTER OR THE TASK. This model is shared by both ends of the
        wire: ``services/agent_client.py::patch_proposal_state`` constructs it on the AGENT, and
        the FastAPI route parses the body back into it on the CONTROLLER. One validator therefore
        covers the producer and the receiver at once -- the same double coverage the ingest path
        gets from two separate call sites, without the second site to drift. It also covers any
        future client of this endpoint, which a fix at ``tasks/execution.py`` alone would not.
        Sanitize-on-write in a wire schema is the established idiom here
        (``schemas/wire_mixins.py::SanitizedErrorMessageMixin``).

        Normalizing rather than REJECTING is deliberate and is the narrower of the two options: an
        NFC and an NFD spelling of a filename denote the same name, so folding them loses nothing,
        whereas a 4xx here would strand a file whose move has ALREADY committed on disk (see
        ``_report_success``'s step 6b comment) with no path back to a consistent row.

        WHOSE CHOICE THIS IS. The operator decision of 2026-08-24 recorded on phaze-i6lzt selected
        the label "Split the defect-shaped rows now (C5, D2, E3+E4)" and settled nothing about how
        to fix any of them; phaze-sy8z3 carries the mechanism as an open question with the four
        candidates named above. The choice made here is the implementing seat's, argued on its
        merits and open to review.
        """
        return unicodedata.normalize("NFC", v) if v is not None else v

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
