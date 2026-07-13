"""Unit tests for phaze.schemas.agent_proposals (Phase 26 Plan 03 — D-28)."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.models.proposal import ProposalStatus
from phaze.schemas.agent_proposals import ProposalStatePatch, ProposalStateResponse


def test_proposal_state_patch_minimal_executed() -> None:
    """proposal_state='executed' with no other fields must validate (terminal-state-only patch)."""
    patch = ProposalStatePatch(proposal_state="executed")
    assert patch.proposal_state == "executed"
    assert patch.file_state is None
    assert patch.current_path is None
    assert patch.error_message is None


def test_proposal_state_patch_minimal_failed() -> None:
    """proposal_state='failed' with no other fields is valid."""
    patch = ProposalStatePatch(proposal_state="failed")
    assert patch.proposal_state == "failed"


def test_proposal_state_patch_moved_with_current_path() -> None:
    """Happy path: executed + moved + current_path supplied."""
    patch = ProposalStatePatch(
        proposal_state="executed",
        file_state="moved",
        current_path="/music/Artist/Album/01-Track.mp3",
    )
    assert patch.file_state == "moved"
    assert patch.current_path == "/music/Artist/Album/01-Track.mp3"


def test_proposal_state_patch_unchanged_without_path() -> None:
    """Unchanged file requires NO current_path."""
    patch = ProposalStatePatch(
        proposal_state="failed",
        file_state="unchanged",
        error_message="copy verify failed",
    )
    assert patch.file_state == "unchanged"
    assert patch.current_path is None


def test_proposal_state_patch_moved_without_path_raises() -> None:
    """CONTEXT.md discretion: file_state='moved' without current_path must raise ValidationError.

    Validator name: _require_path_when_moved.
    """
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ProposalStatePatch(proposal_state="executed", file_state="moved")

    # The validator message should mention `current_path`.
    err_messages = " ".join(str(e.get("msg", "")) for e in exc_info.value.errors())
    assert "current_path" in err_messages.lower()


def test_proposal_state_patch_rejects_unknown_field() -> None:
    """extra='forbid' on the patch body."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ProposalStatePatch.model_validate(
            {"proposal_state": "executed", "agent_id": "rogue-agent"},
        )

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_proposal_state_patch_rejects_invalid_proposal_state() -> None:
    """Literal['executed', 'failed'] — values like 'approved' / 'pending' rejected (only terminal states)."""
    with pytest.raises(pydantic.ValidationError):
        ProposalStatePatch.model_validate({"proposal_state": "approved"})

    with pytest.raises(pydantic.ValidationError):
        ProposalStatePatch.model_validate({"proposal_state": "pending"})


def test_proposal_state_patch_rejects_invalid_file_state() -> None:
    """Literal['moved', 'unchanged'] — disallows other values."""
    with pytest.raises(pydantic.ValidationError):
        ProposalStatePatch.model_validate(
            {"proposal_state": "executed", "file_state": "deleted"},
        )


def test_proposal_state_patch_literal_values_match_enum_values() -> None:
    """Literal strings must match the lowercase enum values from Plan 01."""
    # Match Plan-01 ProposalStatus enum lowercase values
    assert ProposalStatus.EXECUTED.value == "executed"
    assert ProposalStatus.FAILED.value == "failed"

    # Round-trip both literal values
    for ps in ("executed", "failed"):
        ProposalStatePatch(proposal_state=ps)  # type: ignore[arg-type]


def test_proposal_state_response_shape() -> None:
    """Response carries proposal_id + the resolved states + optional path."""
    proposal_id = uuid.uuid4()
    resp = ProposalStateResponse(
        proposal_id=proposal_id,
        proposal_state="executed",
        file_state="moved",
        current_path="/x/y.mp3",
    )

    assert resp.proposal_id == proposal_id
    assert resp.proposal_state == "executed"
    assert resp.file_state == "moved"
    assert resp.current_path == "/x/y.mp3"


def test_proposal_state_response_optional_fields() -> None:
    """file_state and current_path may be omitted."""
    resp = ProposalStateResponse(
        proposal_id=uuid.uuid4(),
        proposal_state="failed",
    )

    assert resp.file_state is None
    assert resp.current_path is None
