"""Tests for the metadata extraction SAQ task and auto-enqueue wiring."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.models.file import FileState
from phaze.services.metadata import ExtractedTags
from phaze.tasks.metadata_extraction import extract_file_metadata


def _make_ctx() -> dict[str, Any]:
    """Create a minimal SAQ context dict with async_session factory."""
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return {"async_session": mock_session_factory, "_mock_session": mock_session}


def _make_file_record(
    file_id: uuid.UUID | None = None,
    file_type: str = "mp3",
    state: str = "discovered",
    current_path: str = "/music/track.mp3",
) -> MagicMock:
    """Create a mock FileRecord."""
    record = MagicMock()
    record.id = file_id or uuid.uuid4()
    record.file_type = file_type
    record.state = state
    record.current_path = current_path
    return record


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_file_not_found_returns_not_found(mock_extract: MagicMock) -> None:
    """Task returns not_found when file ID doesn't exist in DB."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    result = await extract_file_metadata(ctx, file_id=str(uuid.uuid4()))
    assert result["status"] == "not_found"
    mock_extract.assert_not_called()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_companion_file_skipped(mock_extract: MagicMock) -> None:
    """Companion files (e.g., .txt) are skipped per D-10."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record(file_type="txt")

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_result

    result = await extract_file_metadata(ctx, file_id=str(file_record.id))
    assert result["status"] == "skipped"
    assert result["reason"] == "not_extractable"
    mock_extract.assert_not_called()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_music_file_tags_extracted(mock_extract: MagicMock) -> None:
    """Music file gets tags extracted, FileMetadata upserted, state set to METADATA_EXTRACTED."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record(file_type="mp3")

    mock_tags = ExtractedTags(
        artist="Test Artist",
        title="Test Title",
        album="Test Album",
        year=2024,
        genre="Electronic",
        track_number=3,
        duration=240.5,
        bitrate=320000,
        raw_tags={"TPE1": "Test Artist"},
    )
    mock_extract.return_value = mock_tags

    # First execute: file record lookup; second: metadata lookup (none found)
    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_meta = MagicMock()
    mock_result_meta.scalar_one_or_none.return_value = None
    session.execute.side_effect = [mock_result_file, mock_result_meta]

    result = await extract_file_metadata(ctx, file_id=str(file_record.id))
    assert result["status"] == "extracted"
    assert file_record.state == FileState.METADATA_EXTRACTED
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_no_tags_creates_empty_metadata_row(mock_extract: MagicMock) -> None:
    """File with no tags gets empty FileMetadata row (per D-11)."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record(file_type="flac")

    mock_tags = ExtractedTags()  # All None, empty raw_tags
    mock_extract.return_value = mock_tags

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_meta = MagicMock()
    mock_result_meta.scalar_one_or_none.return_value = None
    session.execute.side_effect = [mock_result_file, mock_result_meta]

    result = await extract_file_metadata(ctx, file_id=str(file_record.id))
    assert result["status"] == "extracted"
    session.add.assert_called_once()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_exception_triggers_retry(_mock_extract: MagicMock) -> None:
    """Exception during processing re-raises for SAQ retry handling."""
    ctx = _make_ctx()
    session = ctx["_mock_session"]
    session.execute.side_effect = RuntimeError("DB connection lost")

    with pytest.raises(RuntimeError, match="DB connection lost"):
        await extract_file_metadata(ctx, file_id=str(uuid.uuid4()))


async def test_run_scan_auto_enqueues_extraction() -> None:
    """run_scan with queue enqueues extract_file_metadata for music/video files (D-09)."""
    from phaze.services.ingestion import run_scan

    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_queue = AsyncMock()

    file_id_1 = uuid.uuid4()
    file_id_2 = uuid.uuid4()
    file_id_3 = uuid.uuid4()

    mock_records = [
        {
            "id": file_id_1,
            "file_type": "mp3",
            "sha256_hash": "a" * 64,
            "original_path": "/a.mp3",
            "original_filename": "a.mp3",
            "current_path": "/a.mp3",
            "file_size": 100,
            "state": "discovered",
            "batch_id": None,
        },
        {
            "id": file_id_2,
            "file_type": "mp4",
            "sha256_hash": "b" * 64,
            "original_path": "/b.mp4",
            "original_filename": "b.mp4",
            "current_path": "/b.mp4",
            "file_size": 200,
            "state": "discovered",
            "batch_id": None,
        },
        {
            "id": file_id_3,
            "file_type": "txt",
            "sha256_hash": "c" * 64,
            "original_path": "/c.txt",
            "original_filename": "c.txt",
            "current_path": "/c.txt",
            "file_size": 50,
            "state": "discovered",
            "batch_id": None,
        },
    ]

    batch_id = uuid.uuid4()

    with (
        patch("phaze.services.ingestion.discover_and_hash_files", return_value=mock_records),
        patch("phaze.services.ingestion.bulk_upsert_files", new_callable=AsyncMock, return_value=3),
    ):
        await run_scan("/fake/path", batch_id, mock_session_factory, queue=mock_queue)

    # Should enqueue for mp3 and mp4 (music + video), but NOT for txt (companion)
    enqueue_calls = mock_queue.enqueue.call_args_list
    enqueued_ids = [call.kwargs["file_id"] for call in enqueue_calls]
    assert str(file_id_1) in enqueued_ids
    assert str(file_id_2) in enqueued_ids
    assert str(file_id_3) not in enqueued_ids
