"""Unit tests for src/phaze/schemas/agent_exec_batches.py (Phase 28 D-06).

Mirrors the schema-validation patterns in tests/test_schemas/test_agent_scan_batches.py
and tests/test_schemas/test_agent_proposals.py. Targets test IDs 28-V-17.

Cross-field invariant under test (D-06):
- ``failed_at_step`` is required iff ``terminal_step == "failed"``.
- ``terminal_step != "failed"`` MUST have ``failed_at_step is None``.
"""

from __future__ import annotations

import uuid

from pydantic import ValidationError
import pytest

from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload


def _base_kwargs(**overrides: object) -> dict[str, object]:
    """Return a valid baseline payload dict; tests override individual fields."""
    defaults: dict[str, object] = {
        "request_id": uuid.uuid4(),
        "batch_id": uuid.uuid4(),
        "agent_id": "test-agent-01",
        "sub_batch_index": 0,
        "proposal_id": uuid.uuid4(),
        "terminal_step": "deleted",
    }
    defaults.update(overrides)
    return defaults


def test_valid_payload_with_all_fields_constructs() -> None:
    """Happy path: a valid payload with deleted terminal_step constructs cleanly."""
    payload = ExecBatchProgressPayload(**_base_kwargs())  # type: ignore[arg-type]
    assert payload.terminal_step == "deleted"
    assert payload.failed_at_step is None
    assert payload.sub_batch_terminal is False
    assert payload.sub_batch_index == 0


def test_terminal_step_copied_no_failed_at_step_succeeds() -> None:
    """terminal_step='copied' (or any non-failed) MUST allow failed_at_step=None."""
    payload = ExecBatchProgressPayload(**_base_kwargs(terminal_step="copied"))  # type: ignore[arg-type]
    assert payload.terminal_step == "copied"
    assert payload.failed_at_step is None


def test_terminal_step_verified_no_failed_at_step_succeeds() -> None:
    """terminal_step='verified' MUST allow failed_at_step=None."""
    payload = ExecBatchProgressPayload(**_base_kwargs(terminal_step="verified"))  # type: ignore[arg-type]
    assert payload.terminal_step == "verified"
    assert payload.failed_at_step is None


def test_terminal_step_failed_with_failed_at_step_copy_succeeds() -> None:
    """terminal_step='failed' + failed_at_step='copy' constructs cleanly."""
    payload = ExecBatchProgressPayload(**_base_kwargs(terminal_step="failed", failed_at_step="copy"))  # type: ignore[arg-type]
    assert payload.terminal_step == "failed"
    assert payload.failed_at_step == "copy"


def test_terminal_step_failed_with_failed_at_step_verify_succeeds() -> None:
    """terminal_step='failed' + failed_at_step='verify' constructs cleanly."""
    payload = ExecBatchProgressPayload(**_base_kwargs(terminal_step="failed", failed_at_step="verify"))  # type: ignore[arg-type]
    assert payload.failed_at_step == "verify"


def test_terminal_step_failed_with_failed_at_step_delete_succeeds() -> None:
    """terminal_step='failed' + failed_at_step='delete' constructs cleanly."""
    payload = ExecBatchProgressPayload(**_base_kwargs(terminal_step="failed", failed_at_step="delete"))  # type: ignore[arg-type]
    assert payload.failed_at_step == "delete"


def test_terminal_step_failed_without_failed_at_step_rejected() -> None:
    """D-06 invariant: terminal_step='failed' + failed_at_step=None raises ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        ExecBatchProgressPayload(**_base_kwargs(terminal_step="failed"))  # type: ignore[arg-type]
    assert "failed_at_step" in str(excinfo.value)


def test_terminal_step_deleted_with_failed_at_step_rejected() -> None:
    """D-06 invariant: non-failed terminal_step + non-null failed_at_step raises."""
    with pytest.raises(ValidationError) as excinfo:
        ExecBatchProgressPayload(**_base_kwargs(terminal_step="deleted", failed_at_step="verify"))  # type: ignore[arg-type]
    assert "failed_at_step" in str(excinfo.value)


def test_terminal_step_copied_with_failed_at_step_rejected() -> None:
    """D-06 invariant: terminal_step='copied' + failed_at_step set raises."""
    with pytest.raises(ValidationError):
        ExecBatchProgressPayload(**_base_kwargs(terminal_step="copied", failed_at_step="copy"))  # type: ignore[arg-type]


def test_extra_field_forbid_rejects_unknown() -> None:
    """extra='forbid' MUST reject any unknown field (AUTH-01 -- no spoof leakage)."""
    with pytest.raises(ValidationError):
        ExecBatchProgressPayload(**_base_kwargs(unknown_field="x"))  # type: ignore[arg-type]


def test_terminal_step_invalid_literal_rejected() -> None:
    """terminal_step must be in {copied, verified, deleted, failed} (Literal layer)."""
    with pytest.raises(ValidationError):
        ExecBatchProgressPayload(**_base_kwargs(terminal_step="invalid_step"))  # type: ignore[arg-type]


def test_failed_at_step_invalid_literal_rejected() -> None:
    """failed_at_step must be in {copy, verify, delete} (Literal layer)."""
    with pytest.raises(ValidationError):
        ExecBatchProgressPayload(**_base_kwargs(terminal_step="failed", failed_at_step="bogus"))  # type: ignore[arg-type]


def test_sub_batch_terminal_defaults_to_false() -> None:
    """sub_batch_terminal defaults to False when omitted from input."""
    payload = ExecBatchProgressPayload(**_base_kwargs())  # type: ignore[arg-type]
    assert payload.sub_batch_terminal is False


def test_sub_batch_terminal_can_be_true() -> None:
    """sub_batch_terminal can be set explicitly to True."""
    payload = ExecBatchProgressPayload(**_base_kwargs(sub_batch_terminal=True))  # type: ignore[arg-type]
    assert payload.sub_batch_terminal is True


def test_agent_id_is_string_slug_not_uuid() -> None:
    """agent_id is a kebab-case slug str (matches Phase 24 D-01)."""
    payload = ExecBatchProgressPayload(**_base_kwargs(agent_id="fileserver-02"))  # type: ignore[arg-type]
    assert payload.agent_id == "fileserver-02"
    assert isinstance(payload.agent_id, str)


def test_model_dump_json_round_trip() -> None:
    """Payload survives a model_dump(mode='json') -> model_validate_json round trip."""
    original = ExecBatchProgressPayload(
        **_base_kwargs(terminal_step="failed", failed_at_step="verify", sub_batch_terminal=True),  # type: ignore[arg-type]
    )
    rebuilt = ExecBatchProgressPayload.model_validate(original.model_dump(mode="json"))
    assert rebuilt == original
