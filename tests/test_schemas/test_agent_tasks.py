"""Unit tests for phaze.schemas.agent_tasks (Phase 26 Plan 03 — D-22..D-24).

These payloads are validated at SAQ task entry via `<Payload>.model_validate(kwargs)`
so the same strictness rules as HTTP request bodies apply (extra='forbid', D-16).
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_tasks import (
    ExecuteApprovedBatchPayload,
    ExecuteBatchProposalItem,
    ExtractMetadataPayload,
    FingerprintFilePayload,
    ProcessFilePayload,
    ScanLiveSetPayload,
)


# -----------------------
# ProcessFilePayload
# -----------------------


def test_process_file_payload_minimal_valid() -> None:
    """ProcessFilePayload is the only task carrying models_path (essentia .pb files)."""
    p = ProcessFilePayload(
        file_id=uuid.uuid4(),
        original_path="/music/a.mp3",
        file_type="mp3",
        agent_id="agent-a",
        models_path="/opt/essentia/models",
    )
    assert p.file_type == "mp3"
    assert p.models_path == "/opt/essentia/models"


def test_process_file_payload_requires_models_path() -> None:
    """models_path is required ONLY for ProcessFilePayload (essentia needs the .pb files)."""
    with pytest.raises(pydantic.ValidationError):
        ProcessFilePayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "original_path": "/x",
                "file_type": "mp3",
                "agent_id": "a",
            },
        )


def test_process_file_payload_rejects_unknown_field() -> None:
    """extra='forbid' on every SAQ payload."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ProcessFilePayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "original_path": "/x",
                "file_type": "mp3",
                "agent_id": "a",
                "models_path": "/m",
                "rogue": "x",
            },
        )

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_process_file_payload_round_trip() -> None:
    """model_dump_json() -> model_validate_json() round-trip preserves equality."""
    payload = ProcessFilePayload(
        file_id=uuid.uuid4(),
        original_path="/music/a.mp3",
        file_type="mp3",
        agent_id="agent-a",
        models_path="/opt/essentia/models",
    )
    rt = ProcessFilePayload.model_validate_json(payload.model_dump_json())
    assert rt == payload


# -----------------------
# ExtractMetadataPayload
# -----------------------


def test_extract_metadata_payload_minimal_valid() -> None:
    """ExtractMetadataPayload has NO models_path (mutagen doesn't need it)."""
    p = ExtractMetadataPayload(
        file_id=uuid.uuid4(),
        original_path="/x",
        file_type="m4a",
        agent_id="a",
    )
    assert p.file_type == "m4a"


def test_extract_metadata_payload_has_no_models_path() -> None:
    """models_path MUST NOT exist on ExtractMetadataPayload (D-22)."""
    fields = ExtractMetadataPayload.model_fields
    assert "models_path" not in fields


def test_extract_metadata_payload_rejects_unknown_field() -> None:
    with pytest.raises(pydantic.ValidationError):
        ExtractMetadataPayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "original_path": "/x",
                "file_type": "mp3",
                "agent_id": "a",
                "models_path": "/m",  # not allowed on this payload
            },
        )


# -----------------------
# FingerprintFilePayload
# -----------------------


def test_fingerprint_file_payload_minimal_valid() -> None:
    """FingerprintFilePayload has NO file_type or models_path (sidecar adapter handles)."""
    p = FingerprintFilePayload(
        file_id=uuid.uuid4(),
        original_path="/x",
        agent_id="a",
    )
    assert p.agent_id == "a"


def test_fingerprint_file_payload_field_set() -> None:
    """Confirm minimal field set per D-22."""
    fields = FingerprintFilePayload.model_fields
    assert set(fields.keys()) == {"file_id", "original_path", "agent_id"}


# -----------------------
# ScanLiveSetPayload
# -----------------------


def test_scan_live_set_payload_minimal_valid() -> None:
    p = ScanLiveSetPayload(
        file_id=uuid.uuid4(),
        original_path="/x",
        agent_id="a",
    )
    assert p.original_path == "/x"


def test_scan_live_set_payload_field_set() -> None:
    fields = ScanLiveSetPayload.model_fields
    assert set(fields.keys()) == {"file_id", "original_path", "agent_id"}


# -----------------------
# ExecuteBatchProposalItem + ExecuteApprovedBatchPayload (B2 Option A)
# -----------------------


def _proposal_item() -> ExecuteBatchProposalItem:
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        original_path="/orig/a.mp3",
        proposed_path="/new/a.mp3",
    )


def test_execute_batch_proposal_item_minimal() -> None:
    """sha256_hash is optional; everything else required."""
    item = _proposal_item()
    assert item.sha256_hash is None
    assert item.proposed_path == "/new/a.mp3"


def test_execute_batch_proposal_item_with_sha256() -> None:
    """sha256_hash optional pre-copy integrity check."""
    item = ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        original_path="/o",
        proposed_path="/n",
        sha256_hash="a" * 64,
    )
    assert item.sha256_hash == "a" * 64


def test_execute_batch_proposal_item_rejects_unknown_field() -> None:
    """Nested item also has extra='forbid' (per-class ConfigDict)."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ExecuteBatchProposalItem.model_validate(
            {
                "proposal_id": str(uuid.uuid4()),
                "file_id": str(uuid.uuid4()),
                "original_path": "/o",
                "proposed_path": "/n",
                "current_path": "/x",  # explicitly forbidden by D-24
            },
        )

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_execute_approved_batch_payload_valid() -> None:
    """Full batch payload with 2 proposals validates."""
    batch = ExecuteApprovedBatchPayload(
        batch_id=uuid.uuid4(),
        agent_id="agent-a",
        proposals=[_proposal_item(), _proposal_item()],
    )

    assert len(batch.proposals) == 2
    assert batch.agent_id == "agent-a"


def test_execute_approved_batch_payload_requires_nonempty_proposals() -> None:
    """min_length=1 — empty batch is invalid."""
    with pytest.raises(pydantic.ValidationError):
        ExecuteApprovedBatchPayload(
            batch_id=uuid.uuid4(),
            agent_id="a",
            proposals=[],
        )


def test_execute_approved_batch_payload_rejects_over_500() -> None:
    """max_length=500 cap on per-job batch size."""
    items = [_proposal_item() for _ in range(501)]
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ExecuteApprovedBatchPayload(
            batch_id=uuid.uuid4(),
            agent_id="a",
            proposals=items,
        )

    assert any("too_long" in str(e.get("type", "")) for e in exc_info.value.errors())


def test_execute_approved_batch_payload_accepts_max_boundary() -> None:
    """Exactly 500 proposals must pass (boundary)."""
    items = [_proposal_item() for _ in range(500)]
    batch = ExecuteApprovedBatchPayload(
        batch_id=uuid.uuid4(),
        agent_id="a",
        proposals=items,
    )
    assert len(batch.proposals) == 500


def test_execute_approved_batch_payload_round_trip() -> None:
    """JSON round-trip preserves equality (D-23: agent serializes/deserializes via SAQ Redis)."""
    batch = ExecuteApprovedBatchPayload(
        batch_id=uuid.uuid4(),
        agent_id="agent-a",
        proposals=[
            ExecuteBatchProposalItem(
                proposal_id=uuid.uuid4(),
                file_id=uuid.uuid4(),
                original_path="/orig/a.mp3",
                proposed_path="/new/a.mp3",
                sha256_hash="b" * 64,
            ),
        ],
    )

    rt = ExecuteApprovedBatchPayload.model_validate_json(batch.model_dump_json())
    assert rt == batch


def test_execute_approved_batch_payload_rejects_unknown_field() -> None:
    """extra='forbid' on the batch."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ExecuteApprovedBatchPayload.model_validate(
            {
                "batch_id": str(uuid.uuid4()),
                "agent_id": "a",
                "proposals": [
                    {
                        "proposal_id": str(uuid.uuid4()),
                        "file_id": str(uuid.uuid4()),
                        "original_path": "/o",
                        "proposed_path": "/n",
                    },
                ],
                "rogue": "no",
            },
        )

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_no_current_path_field_anywhere() -> None:
    """D-24 invariant: NO payload carries `current_path` — agents work off original_path.

    `current_path` is set on the FileRecord only AFTER execute_approved_batch
    flips state via PATCH /proposals/{id}/state.
    """
    payload_classes = (
        ProcessFilePayload,
        ExtractMetadataPayload,
        FingerprintFilePayload,
        ScanLiveSetPayload,
        ExecuteApprovedBatchPayload,
        ExecuteBatchProposalItem,
    )
    for cls in payload_classes:
        assert "current_path" not in cls.model_fields, f"{cls.__name__} unexpectedly has current_path"


def test_only_process_file_payload_has_models_path() -> None:
    """D-22 invariant: models_path is unique to ProcessFilePayload."""
    assert "models_path" in ProcessFilePayload.model_fields
    assert "models_path" not in ExtractMetadataPayload.model_fields
    assert "models_path" not in FingerprintFilePayload.model_fields
    assert "models_path" not in ScanLiveSetPayload.model_fields
    assert "models_path" not in ExecuteApprovedBatchPayload.model_fields
    assert "models_path" not in ExecuteBatchProposalItem.model_fields
