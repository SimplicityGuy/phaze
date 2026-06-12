"""Tests for the HTTP-rewritten extract_file_metadata task (Phase 26 Plan 11)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import ValidationError
import pytest

from phaze.services.metadata import ExtractedTags
from phaze.tasks.metadata_extraction import extract_file_metadata


def _make_ctx(api_client: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with an api_client mock."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.put_metadata = AsyncMock(return_value=MagicMock())
    return {"api_client": api_client}


def _make_payload_kwargs(file_id: uuid.UUID | None = None, file_type: str = "mp3") -> dict[str, Any]:
    return {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/track.mp3",
        "file_type": file_type,
        "agent_id": "test-agent",
    }


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_extract_calls_put_metadata(mock_extract: MagicMock) -> None:
    """Music file: tags extracted and posted via api.put_metadata."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    file_id = uuid.uuid4()

    mock_extract.return_value = ExtractedTags(
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

    result = await extract_file_metadata(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "extracted"
    assert result["file_id"] == str(file_id)
    mock_extract.assert_called_once_with("/music/track.mp3")
    api.put_metadata.assert_awaited_once()
    awaited_call = api.put_metadata.await_args
    assert awaited_call.args[0] == file_id
    body = awaited_call.args[1]
    assert body.artist == "Test Artist"
    assert body.title == "Test Title"
    assert body.duration == 240.5


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_companion_file_skipped(mock_extract: MagicMock) -> None:
    """Companion files (.txt) are skipped per D-10 -- no extract, no HTTP."""
    api = AsyncMock()
    api.put_metadata = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await extract_file_metadata(ctx, **_make_payload_kwargs(file_type="txt"))

    assert result["status"] == "skipped"
    assert result["reason"] == "not_extractable"
    mock_extract.assert_not_called()
    api.put_metadata.assert_not_awaited()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_no_tags_still_posts_empty_metadata(mock_extract: MagicMock) -> None:
    """File with no tags still gets an empty metadata row (D-11)."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    mock_extract.return_value = ExtractedTags()  # all None

    result = await extract_file_metadata(ctx, **_make_payload_kwargs())

    assert result["status"] == "extracted"
    api.put_metadata.assert_awaited_once()
    body = api.put_metadata.await_args.args[1]
    assert body.artist is None
    assert body.title is None


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_extraction_failure_propagates(mock_extract: MagicMock) -> None:
    """Exception during tag extraction re-raises for SAQ retry handling."""
    api = AsyncMock()
    api.put_metadata = AsyncMock()
    ctx = _make_ctx(api_client=api)
    mock_extract.side_effect = RuntimeError("mutagen crashed")

    with pytest.raises(RuntimeError, match="mutagen crashed"):
        await extract_file_metadata(ctx, **_make_payload_kwargs())
    api.put_metadata.assert_not_awaited()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_rejects_extra_kwargs(mock_extract: MagicMock) -> None:
    """ExtractMetadataPayload.extra='forbid' rejects unknown fields."""
    api = AsyncMock()
    api.put_metadata = AsyncMock()
    ctx = _make_ctx(api_client=api)

    bad_kwargs = _make_payload_kwargs()
    bad_kwargs["bogus_field"] = "x"
    with pytest.raises(ValidationError):
        await extract_file_metadata(ctx, **bad_kwargs)
    mock_extract.assert_not_called()
    api.put_metadata.assert_not_awaited()


# NOTE (Phase 35 D-06): the former ``test_run_scan_auto_enqueues_extraction`` test (which
# asserted run_scan auto-enqueues the metadata-extraction task per the retired D-09) has been
# removed. Metadata extraction is now operator-triggered ONLY; the inverse regression guard
# (run_scan does NOT auto-enqueue) lives in tests/test_no_auto_metadata_enqueue.py.
