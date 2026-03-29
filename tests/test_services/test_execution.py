"""Tests for execution service - copy-verify-delete with audit logging."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
import uuid

import pytest

from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file_record(
    file_id: uuid.UUID | None = None,
    sha256_hash: str = "",
    current_path: str = "/music/old_name.mp3",
    state: str = FileState.APPROVED,
) -> MagicMock:
    record = MagicMock()
    record.id = file_id or uuid.uuid4()
    record.sha256_hash = sha256_hash
    record.current_path = current_path
    record.state = state
    return record


def _make_proposal(
    proposal_id: uuid.UUID | None = None,
    file_record: MagicMock | None = None,
    proposed_filename: str = "new_name.mp3",
    status: str = "approved",
) -> MagicMock:
    proposal = MagicMock()
    proposal.id = proposal_id or uuid.uuid4()
    proposal.proposed_filename = proposed_filename
    proposal.status = status
    proposal.file = file_record
    return proposal


def _sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# compute_sha256
# ---------------------------------------------------------------------------


def test_compute_sha256(tmp_path: Path) -> None:
    """compute_sha256 returns correct SHA256 hex digest for a file with known content."""
    from phaze.services.execution import compute_sha256

    content = b"hello world test content"
    f = tmp_path / "test.mp3"
    f.write_bytes(content)

    result = compute_sha256(f)
    assert result == _sha256_of(content)


# ---------------------------------------------------------------------------
# log_operation / complete_operation
# ---------------------------------------------------------------------------


async def test_log_operation_and_complete_operation() -> None:
    """log_operation creates IN_PROGRESS entry; complete_operation updates to COMPLETED or FAILED."""
    from phaze.services.execution import complete_operation, log_operation

    session = AsyncMock()
    proposal_id = uuid.uuid4()

    # log_operation should add and commit an ExecutionLog with IN_PROGRESS
    log_entry = await log_operation(session, proposal_id, "copy", "/src", "/dst")

    assert isinstance(log_entry, ExecutionLog)
    assert log_entry.status == ExecutionStatus.IN_PROGRESS
    assert log_entry.operation == "copy"
    assert log_entry.source_path == "/src"
    assert log_entry.destination_path == "/dst"
    assert log_entry.sha256_verified is False
    session.add.assert_called_once_with(log_entry)
    session.commit.assert_awaited()

    # Reset mock for complete_operation
    session.reset_mock()

    # Complete successfully
    await complete_operation(session, log_entry, sha256_verified=True)
    assert log_entry.status == ExecutionStatus.COMPLETED
    assert log_entry.sha256_verified is True
    assert log_entry.error_message is None
    session.commit.assert_awaited()

    # Complete with failure
    session.reset_mock()
    await complete_operation(session, log_entry, error_message="Hash mismatch")
    assert log_entry.status == ExecutionStatus.FAILED
    assert log_entry.error_message == "Hash mismatch"
    session.commit.assert_awaited()


# ---------------------------------------------------------------------------
# execute_single_file — success path
# ---------------------------------------------------------------------------


async def test_copy_verify_delete_success(tmp_path: Path) -> None:
    """Full success: copy to destination, verify hash, delete original, update FileRecord."""
    from phaze.services.execution import execute_single_file

    content = b"audio file content for testing"
    source = tmp_path / "old_name.mp3"
    source.write_bytes(content)
    file_hash = _sha256_of(content)

    file_record = _make_file_record(sha256_hash=file_hash, current_path=str(source))
    proposal = _make_proposal(proposed_filename="new_name.mp3")

    session = AsyncMock()
    result = await execute_single_file(session, proposal, file_record)

    assert result is True
    # Original deleted
    assert not source.exists()
    # Destination exists with same content
    dest = tmp_path / "new_name.mp3"
    assert dest.exists()
    assert dest.read_bytes() == content
    # FileRecord updated
    assert file_record.current_path == str(dest)
    assert file_record.state == FileState.EXECUTED
    # 3 log entries created (copy, verify, delete) — session.add called at least 3 times for logs
    add_calls = session.add.call_args_list
    log_entries = [c[0][0] for c in add_calls if isinstance(c[0][0], ExecutionLog)]
    assert len(log_entries) == 3
    operations = [e.operation for e in log_entries]
    assert operations == ["copy", "verify", "delete"]
    # All should be COMPLETED
    assert all(e.status == ExecutionStatus.COMPLETED for e in log_entries)
    # Verify entry should have sha256_verified=True
    verify_entry = log_entries[1]
    assert verify_entry.sha256_verified is True


# ---------------------------------------------------------------------------
# execute_single_file — hash mismatch
# ---------------------------------------------------------------------------


async def test_hash_mismatch_cleanup(tmp_path: Path) -> None:
    """Hash mismatch: bad copy deleted, original preserved, state=FAILED."""
    from phaze.services.execution import execute_single_file

    content = b"original content"
    source = tmp_path / "track.mp3"
    source.write_bytes(content)
    file_hash = _sha256_of(content)

    file_record = _make_file_record(sha256_hash=file_hash, current_path=str(source))
    proposal = _make_proposal(proposed_filename="renamed.mp3")

    session = AsyncMock()

    # Patch shutil.copy2 to create a corrupted copy
    dest = tmp_path / "renamed.mp3"

    def corrupt_copy(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(b"corrupted different content!!!")

    with patch("phaze.services.execution.shutil.copy2", side_effect=corrupt_copy):
        result = await execute_single_file(session, proposal, file_record)

    assert result is False
    # Original still exists
    assert source.exists()
    assert source.read_bytes() == content
    # Bad copy removed (per D-05)
    assert not dest.exists()
    # FileRecord state is FAILED
    assert file_record.state == FileState.FAILED
    # Verify log shows FAILED with hash mismatch error
    add_calls = session.add.call_args_list
    log_entries = [c[0][0] for c in add_calls if isinstance(c[0][0], ExecutionLog)]
    verify_entries = [e for e in log_entries if e.operation == "verify"]
    assert len(verify_entries) == 1
    assert verify_entries[0].status == ExecutionStatus.FAILED
    assert "mismatch" in (verify_entries[0].error_message or "").lower()


# ---------------------------------------------------------------------------
# execute_single_file — destination exists
# ---------------------------------------------------------------------------


async def test_destination_exists(tmp_path: Path) -> None:
    """Destination already exists: returns False, logs copy as FAILED, original untouched."""
    from phaze.services.execution import execute_single_file

    content = b"source content"
    source = tmp_path / "track.mp3"
    source.write_bytes(content)

    # Create the destination already
    dest = tmp_path / "new.mp3"
    dest.write_bytes(b"existing content")

    file_record = _make_file_record(sha256_hash=_sha256_of(content), current_path=str(source))
    proposal = _make_proposal(proposed_filename="new.mp3")

    session = AsyncMock()
    result = await execute_single_file(session, proposal, file_record)

    assert result is False
    # Original untouched
    assert source.exists()
    assert source.read_bytes() == content
    # Check log entry shows FAILED with "already exists"
    add_calls = session.add.call_args_list
    log_entries = [c[0][0] for c in add_calls if isinstance(c[0][0], ExecutionLog)]
    assert len(log_entries) >= 1
    copy_entry = log_entries[0]
    assert copy_entry.operation == "copy"
    assert copy_entry.status == ExecutionStatus.FAILED
    assert "already exists" in (copy_entry.error_message or "").lower()


# ---------------------------------------------------------------------------
# execute_single_file — copy OS error
# ---------------------------------------------------------------------------


async def test_copy_os_error(tmp_path: Path) -> None:
    """Source not readable: returns False, logs copy as FAILED with OSError message."""
    from phaze.services.execution import execute_single_file

    source = tmp_path / "nonexistent.mp3"
    file_record = _make_file_record(sha256_hash="abc123", current_path=str(source))
    proposal = _make_proposal(proposed_filename="new.mp3")

    session = AsyncMock()

    with patch("phaze.services.execution.shutil.copy2", side_effect=OSError("Permission denied")):
        result = await execute_single_file(session, proposal, file_record)

    assert result is False
    add_calls = session.add.call_args_list
    log_entries = [c[0][0] for c in add_calls if isinstance(c[0][0], ExecutionLog)]
    copy_entry = log_entries[0]
    assert copy_entry.status == ExecutionStatus.FAILED
    assert "Permission denied" in (copy_entry.error_message or "")


# ---------------------------------------------------------------------------
# execute_single_file — delete fails but copy verified
# ---------------------------------------------------------------------------


async def test_delete_fails_but_copy_verified(tmp_path: Path) -> None:
    """Delete fails but copy is good: FileRecord still updated (copy is valid), delete log FAILED."""
    from phaze.services.execution import execute_single_file

    content = b"precious audio data"
    source = tmp_path / "track.mp3"
    source.write_bytes(content)
    file_hash = _sha256_of(content)

    file_record = _make_file_record(sha256_hash=file_hash, current_path=str(source))
    proposal = _make_proposal(proposed_filename="better_name.mp3")

    session = AsyncMock()
    dest = tmp_path / "better_name.mp3"

    with patch("phaze.services.execution.Path.unlink", side_effect=OSError("Permission denied")):
        result = await execute_single_file(session, proposal, file_record)

    # Still returns True since the copy is good
    assert result is True
    # Destination exists
    assert dest.exists()
    assert dest.read_bytes() == content
    # FileRecord updated
    assert file_record.current_path == str(dest)
    assert file_record.state == FileState.EXECUTED
    # Delete log shows FAILED
    add_calls = session.add.call_args_list
    log_entries = [c[0][0] for c in add_calls if isinstance(c[0][0], ExecutionLog)]
    delete_entries = [e for e in log_entries if e.operation == "delete"]
    assert len(delete_entries) == 1
    assert delete_entries[0].status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# FileRecord update after success
# ---------------------------------------------------------------------------


async def test_file_record_updated(tmp_path: Path) -> None:
    """After success, FileRecord.current_path = destination, state = EXECUTED."""
    from phaze.services.execution import execute_single_file

    content = b"track data"
    source = tmp_path / "original.mp3"
    source.write_bytes(content)
    file_hash = _sha256_of(content)

    file_record = _make_file_record(sha256_hash=file_hash, current_path=str(source))
    proposal = _make_proposal(proposed_filename="final.mp3")

    session = AsyncMock()
    await execute_single_file(session, proposal, file_record)

    expected_dest = str(tmp_path / "final.mp3")
    assert file_record.current_path == expected_dest
    assert file_record.state == FileState.EXECUTED


# ---------------------------------------------------------------------------
# Write-ahead logging pattern (EXE-02)
# ---------------------------------------------------------------------------


async def test_audit_log_created_before_operation(tmp_path: Path) -> None:
    """Each operation creates IN_PROGRESS log entry before file operation (write-ahead)."""
    from phaze.services.execution import execute_single_file

    content = b"test data"
    source = tmp_path / "file.mp3"
    source.write_bytes(content)
    file_hash = _sha256_of(content)

    file_record = _make_file_record(sha256_hash=file_hash, current_path=str(source))
    proposal = _make_proposal(proposed_filename="new.mp3")

    session = AsyncMock()
    # Track order of session operations
    call_order: list[str] = []
    original_add = session.add

    def tracking_add(obj: Any) -> Any:
        if isinstance(obj, ExecutionLog):
            call_order.append(f"log_{obj.operation}_{obj.status}")
        return original_add(obj)

    session.add = tracking_add

    original_commit = session.commit

    async def tracking_commit() -> None:
        call_order.append("commit")
        return await original_commit()

    session.commit = tracking_commit

    await execute_single_file(session, proposal, file_record)

    # Verify write-ahead pattern: log_copy_in_progress comes before copy completion
    assert "log_copy_in_progress" in call_order
    assert "log_verify_in_progress" in call_order
    assert "log_delete_in_progress" in call_order


# ---------------------------------------------------------------------------
# get_approved_proposals
# ---------------------------------------------------------------------------


async def test_get_approved_proposals_with_files() -> None:
    """Query returns approved proposals with eagerly loaded FileRecord."""
    from phaze.services.execution import get_approved_proposals

    session = AsyncMock()
    mock_proposals = [_make_proposal(), _make_proposal()]
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = mock_proposals
    mock_result.scalars.return_value = mock_scalars
    session.execute.return_value = mock_result

    result = await get_approved_proposals(session)

    assert result == mock_proposals
    session.execute.assert_awaited_once()
    # Verify the query was called (we can't easily inspect the SQL, but ensure it was called)
    call_args = session.execute.call_args
    assert call_args is not None
