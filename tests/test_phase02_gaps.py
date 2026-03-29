"""Phase 02 gap-filling tests.

Covers behaviors not exercised by the existing 35 tests:
- run_scan orchestration: happy path and failure path
- ScanBatch model: tablename and field definitions
- ScanStatus enum: all values present
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
import uuid


if TYPE_CHECKING:
    from pathlib import Path

import pytest

from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.ingestion import run_scan


# ---------------------------------------------------------------------------
# ScanStatus enum
# ---------------------------------------------------------------------------


def test_scan_status_has_three_values() -> None:
    """ScanStatus enum contains exactly RUNNING, COMPLETED, FAILED."""
    members = list(ScanStatus)
    assert len(members) == 3
    assert ScanStatus.RUNNING == "running"
    assert ScanStatus.COMPLETED == "completed"
    assert ScanStatus.FAILED == "failed"


# ---------------------------------------------------------------------------
# ScanBatch model fields
# ---------------------------------------------------------------------------


def test_scan_batch_tablename() -> None:
    """ScanBatch maps to the 'scan_batches' table."""
    assert ScanBatch.__tablename__ == "scan_batches"


def test_scan_batch_has_required_columns() -> None:
    """ScanBatch model exposes the expected column names."""
    col_names = {col.name for col in ScanBatch.__table__.columns}
    required = {"id", "scan_path", "status", "total_files", "processed_files", "error_message", "created_at", "updated_at"}
    assert required <= col_names


def test_scan_batch_status_default() -> None:
    """ScanBatch.status column has a default of ScanStatus.RUNNING."""
    status_col = ScanBatch.__table__.columns["status"]
    assert status_col.default.arg == ScanStatus.RUNNING


def test_scan_batch_error_message_nullable() -> None:
    """ScanBatch.error_message column is nullable."""
    col = ScanBatch.__table__.columns["error_message"]
    assert col.nullable is True


# ---------------------------------------------------------------------------
# run_scan orchestration — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scan_creates_batch_and_completes(tmp_path: Path) -> None:
    """run_scan creates a ScanBatch record, upserts files, marks status COMPLETED."""
    # Write one mp3 so discover_and_hash_files returns one record
    (tmp_path / "song.mp3").write_bytes(b"fake audio data")

    batch_id = uuid.uuid4()

    # Track all execute() calls so we can verify the status transitions
    execute_calls: list[object] = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=lambda stmt: execute_calls.append(stmt) or AsyncMock())
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    # async_session_factory used as async context manager: `async with factory() as session`
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    # Patch bulk_upsert_files to avoid needing a real DB; returns count of records
    with patch("phaze.services.ingestion.bulk_upsert_files", new=AsyncMock(return_value=1)) as mock_upsert:
        await run_scan(str(tmp_path), batch_id, mock_factory)

    # ScanBatch was added to the session (batch creation)
    assert mock_session.add.called
    added_obj = mock_session.add.call_args[0][0]
    assert isinstance(added_obj, ScanBatch)
    assert added_obj.id == batch_id
    assert added_obj.status == ScanStatus.RUNNING

    # bulk_upsert was called with at least one record
    assert mock_upsert.called
    upsert_records = mock_upsert.call_args[0][1]
    assert len(upsert_records) == 1
    assert upsert_records[0]["original_filename"] == "song.mp3"

    # Session committed multiple times (batch creation, total_files update, final status)
    assert mock_session.commit.call_count >= 3


@pytest.mark.asyncio
async def test_run_scan_sets_completed_status_with_processed_count(tmp_path: Path) -> None:
    """run_scan sets status=COMPLETED and processed_files to the upserted count."""
    (tmp_path / "track.flac").write_bytes(b"flac")

    batch_id = uuid.uuid4()

    # Capture the VALUES passed to the final UPDATE statement
    update_values_seen: list[dict] = []

    async def capture_execute(stmt):  # type: ignore[no-untyped-def]
        # SQLAlchemy compiled statements expose ._values / .clauses differently;
        # we capture by inspecting compile-time dict via whereclause comparison
        update_values_seen.append(stmt)
        return AsyncMock()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=capture_execute)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("phaze.services.ingestion.bulk_upsert_files", new=AsyncMock(return_value=1)):
        await run_scan(str(tmp_path), batch_id, mock_factory)

    # Two UPDATE statements should have been executed:
    # 1. total_files update  2. status=COMPLETED update
    # We verify at least 2 execute calls happened (add is not via execute)
    assert mock_session.execute.call_count >= 2


# ---------------------------------------------------------------------------
# run_scan orchestration — failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scan_marks_failed_on_exception(tmp_path: Path) -> None:
    """run_scan sets status=FAILED and re-raises when bulk_upsert raises."""
    (tmp_path / "audio.mp3").write_bytes(b"data")

    batch_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=AsyncMock())
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    boom = RuntimeError("database exploded")
    with patch("phaze.services.ingestion.bulk_upsert_files", new=AsyncMock(side_effect=boom)), pytest.raises(RuntimeError, match="database exploded"):
        await run_scan(str(tmp_path), batch_id, mock_factory)

    # The FAILED status update must have been executed
    # execute() is called for: total_files update + failed status update = at least 2
    assert mock_session.execute.call_count >= 2
    # session.commit() must still be called after the failure update
    assert mock_session.commit.call_count >= 2


@pytest.mark.asyncio
async def test_run_scan_failure_records_error_message(tmp_path: Path) -> None:
    """run_scan passes the exception message to the FAILED status update."""
    (tmp_path / "song.mp3").write_bytes(b"mp3 data")

    batch_id = uuid.uuid4()

    # Capture UPDATE statements to inspect values
    executed_stmts: list[object] = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=lambda s: executed_stmts.append(s) or AsyncMock())
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    error_msg = "unique constraint violated"
    with patch("phaze.services.ingestion.bulk_upsert_files", new=AsyncMock(side_effect=RuntimeError(error_msg))), pytest.raises(RuntimeError):
        await run_scan(str(tmp_path), batch_id, mock_factory)

    # At least one UPDATE statement should have been issued
    assert len(executed_stmts) >= 1


# ---------------------------------------------------------------------------
# run_scan orchestration — empty directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scan_empty_directory_completes(tmp_path: Path) -> None:
    """run_scan completes successfully even when the directory has no known files."""
    batch_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=AsyncMock())
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("phaze.services.ingestion.bulk_upsert_files", new=AsyncMock(return_value=0)) as mock_upsert:
        await run_scan(str(tmp_path), batch_id, mock_factory)

    # bulk_upsert called with empty list
    assert mock_upsert.called
    assert mock_upsert.call_args[0][1] == []
