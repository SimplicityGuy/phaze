"""Tests for the scan_live_set arq task function."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from arq import Retry
import pytest

from phaze.services.fingerprint import CombinedMatch


def _make_ctx(job_try: int = 1) -> dict[str, Any]:
    """Create a minimal arq context dict with async_session factory and orchestrator."""
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_orchestrator = AsyncMock()

    return {
        "job_try": job_try,
        "async_session": mock_session_factory,
        "_mock_session": mock_session,
        "fingerprint_orchestrator": mock_orchestrator,
    }


def _make_file_record(
    file_id: uuid.UUID | None = None,
    current_path: str = "/music/liveset.mp3",
) -> MagicMock:
    """Create a mock FileRecord."""
    record = MagicMock()
    record.id = file_id or uuid.uuid4()
    record.current_path = current_path
    return record


def _make_metadata(artist: str | None = None, title: str | None = None) -> MagicMock:
    """Create a mock FileMetadata row."""
    meta = MagicMock()
    meta.artist = artist
    meta.title = title
    return meta


@pytest.mark.asyncio
async def test_scan_live_set_not_found() -> None:
    """scan_live_set returns not_found when file_id does not exist."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    result = await scan_live_set(ctx, str(uuid.uuid4()))
    assert result["status"] == "not_found"
    ctx["fingerprint_orchestrator"].combined_query.assert_not_called()


@pytest.mark.asyncio
async def test_scan_live_set_no_matches() -> None:
    """scan_live_set returns no_matches when combined_query returns empty list."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = file_record
    session.execute.return_value = mock_result
    ctx["fingerprint_orchestrator"].combined_query.return_value = []

    result = await scan_live_set(ctx, str(file_record.id))
    assert result["status"] == "no_matches"


@pytest.mark.asyncio
async def test_scan_live_set_creates_tracklist_with_fingerprint_source() -> None:
    """scan_live_set creates Tracklist with source='fingerprint', status='proposed' when matches found."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    # File record lookup
    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record

    # Metadata lookup for resolved track (returns None -- no metadata)
    mock_result_meta = MagicMock()
    mock_result_meta.scalar_one_or_none.return_value = None

    # No existing tracklist
    mock_result_existing = MagicMock()
    mock_result_existing.scalar_one_or_none.return_value = None

    track_id = str(uuid.uuid4())
    session.execute = AsyncMock(side_effect=[mock_result_file, mock_result_meta, mock_result_existing])
    ctx["fingerprint_orchestrator"].combined_query.return_value = [
        CombinedMatch(track_id=track_id, confidence=84.0, timestamp="04:32"),
    ]

    result = await scan_live_set(ctx, str(file_record.id))
    assert result["status"] == "scanned"
    assert "tracklist_id" in result

    # Verify Tracklist, TracklistVersion, TracklistTrack were added
    assert session.add.call_count >= 3  # tracklist + version + at least 1 track


@pytest.mark.asyncio
async def test_scan_live_set_creates_version_with_number_1() -> None:
    """scan_live_set creates TracklistVersion with version_number=1 for new tracklist."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_meta = MagicMock()
    mock_result_meta.scalar_one_or_none.return_value = None
    mock_result_existing = MagicMock()
    mock_result_existing.scalar_one_or_none.return_value = None

    track_id = str(uuid.uuid4())
    session.execute = AsyncMock(side_effect=[mock_result_file, mock_result_meta, mock_result_existing])
    ctx["fingerprint_orchestrator"].combined_query.return_value = [
        CombinedMatch(track_id=track_id, confidence=84.0),
    ]

    await scan_live_set(ctx, str(file_record.id))

    # Find TracklistVersion in add calls
    added_objects = [call.args[0] for call in session.add.call_args_list]
    from phaze.models.tracklist import TracklistVersion

    versions = [obj for obj in added_objects if isinstance(obj, TracklistVersion)]
    assert len(versions) == 1
    assert versions[0].version_number == 1


@pytest.mark.asyncio
async def test_scan_live_set_tracks_have_confidence_and_timestamp() -> None:
    """scan_live_set creates TracklistTrack rows with confidence and timestamp from CombinedMatch."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_meta1 = MagicMock()
    mock_result_meta1.scalar_one_or_none.return_value = None
    mock_result_meta2 = MagicMock()
    mock_result_meta2.scalar_one_or_none.return_value = None
    mock_result_existing = MagicMock()
    mock_result_existing.scalar_one_or_none.return_value = None

    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())
    session.execute = AsyncMock(side_effect=[mock_result_file, mock_result_meta1, mock_result_meta2, mock_result_existing])
    ctx["fingerprint_orchestrator"].combined_query.return_value = [
        CombinedMatch(track_id=t1, confidence=84.0, timestamp="04:32"),
        CombinedMatch(track_id=t2, confidence=72.0, timestamp="12:15"),
    ]

    await scan_live_set(ctx, str(file_record.id))

    added_objects = [call.args[0] for call in session.add.call_args_list]
    from phaze.models.tracklist import TracklistTrack

    tracks = [obj for obj in added_objects if isinstance(obj, TracklistTrack)]
    assert len(tracks) == 2
    assert tracks[0].confidence == 84.0
    assert tracks[0].timestamp == "04:32"
    assert tracks[0].position == 1
    assert tracks[1].confidence == 72.0
    assert tracks[1].timestamp == "12:15"
    assert tracks[1].position == 2


@pytest.mark.asyncio
async def test_scan_live_set_resolves_artist_title_from_metadata() -> None:
    """scan_live_set resolves artist/title from FileMetadata via track_id lookup."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_meta = MagicMock()
    mock_result_meta.scalar_one_or_none.return_value = _make_metadata(artist="Deadmau5", title="Strobe")
    mock_result_existing = MagicMock()
    mock_result_existing.scalar_one_or_none.return_value = None

    track_id = str(uuid.uuid4())
    session.execute = AsyncMock(side_effect=[mock_result_file, mock_result_meta, mock_result_existing])
    ctx["fingerprint_orchestrator"].combined_query.return_value = [
        CombinedMatch(track_id=track_id, confidence=84.0, timestamp="04:32"),
    ]

    await scan_live_set(ctx, str(file_record.id))

    added_objects = [call.args[0] for call in session.add.call_args_list]
    from phaze.models.tracklist import TracklistTrack

    tracks = [obj for obj in added_objects if isinstance(obj, TracklistTrack)]
    assert len(tracks) == 1
    assert tracks[0].artist == "Deadmau5"
    assert tracks[0].title == "Strobe"


@pytest.mark.asyncio
async def test_scan_live_set_external_id_format() -> None:
    """scan_live_set uses external_id format 'fp-{file_id_hex[:12]}'."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_meta = MagicMock()
    mock_result_meta.scalar_one_or_none.return_value = None
    mock_result_existing = MagicMock()
    mock_result_existing.scalar_one_or_none.return_value = None

    track_id = str(uuid.uuid4())
    session.execute = AsyncMock(side_effect=[mock_result_file, mock_result_meta, mock_result_existing])
    ctx["fingerprint_orchestrator"].combined_query.return_value = [
        CombinedMatch(track_id=track_id, confidence=84.0),
    ]

    await scan_live_set(ctx, str(file_id))

    added_objects = [call.args[0] for call in session.add.call_args_list]
    from phaze.models.tracklist import Tracklist

    tracklists = [obj for obj in added_objects if isinstance(obj, Tracklist)]
    assert len(tracklists) == 1
    expected_external_id = f"fp-{file_id.hex[:12]}"
    assert tracklists[0].external_id == expected_external_id


@pytest.mark.asyncio
async def test_scan_live_set_rescan_creates_new_version() -> None:
    """Re-scanning same file creates new TracklistVersion (version_number=2), not duplicate Tracklist."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_meta = MagicMock()
    mock_result_meta.scalar_one_or_none.return_value = None

    # Existing tracklist from previous scan
    existing_tracklist = MagicMock()
    existing_tracklist.id = uuid.uuid4()
    existing_tracklist.latest_version_id = uuid.uuid4()

    mock_result_existing = MagicMock()
    mock_result_existing.scalar_one_or_none.return_value = existing_tracklist

    # Max version query
    mock_result_max_version = MagicMock()
    mock_result_max_version.scalar_one_or_none.return_value = 1

    track_id = str(uuid.uuid4())
    session.execute = AsyncMock(side_effect=[mock_result_file, mock_result_meta, mock_result_existing, mock_result_max_version])
    ctx["fingerprint_orchestrator"].combined_query.return_value = [
        CombinedMatch(track_id=track_id, confidence=84.0),
    ]

    result = await scan_live_set(ctx, str(file_record.id))
    assert result["status"] == "scanned"

    added_objects = [call.args[0] for call in session.add.call_args_list]
    from phaze.models.tracklist import Tracklist, TracklistVersion

    # No new Tracklist created (reusing existing)
    tracklists = [obj for obj in added_objects if isinstance(obj, Tracklist)]
    assert len(tracklists) == 0

    # New version created with version_number=2
    versions = [obj for obj in added_objects if isinstance(obj, TracklistVersion)]
    assert len(versions) == 1
    assert versions[0].version_number == 2


@pytest.mark.asyncio
async def test_scan_live_set_invalid_track_id_skipped() -> None:
    """scan_live_set gracefully handles non-UUID track_ids from fingerprint matches."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_existing = MagicMock()
    mock_result_existing.scalar_one_or_none.return_value = None

    # Use a non-UUID track_id — should trigger the ValueError/AttributeError handler (lines 57-59)
    session.execute = AsyncMock(side_effect=[mock_result_file, mock_result_existing])
    ctx["fingerprint_orchestrator"].combined_query.return_value = [
        CombinedMatch(track_id="not-a-uuid", confidence=84.0, timestamp="04:32"),
    ]

    result = await scan_live_set(ctx, str(file_record.id))
    assert result["status"] == "scanned"
    assert "tracklist_id" in result

    # Track should still be created, just without resolved artist/title
    added_objects = [call.args[0] for call in session.add.call_args_list]
    from phaze.models.tracklist import TracklistTrack

    tracks = [obj for obj in added_objects if isinstance(obj, TracklistTrack)]
    assert len(tracks) == 1
    # resolved_artist/resolved_title not set because UUID parse failed
    assert tracks[0].artist is None


@pytest.mark.asyncio
async def test_scan_live_set_retry_on_exception() -> None:
    """scan_live_set retries on exception via Retry with exponential defer."""
    from phaze.tasks.scan import scan_live_set

    ctx = _make_ctx(job_try=2)
    session = ctx["_mock_session"]
    session.execute.side_effect = RuntimeError("DB connection lost")

    with pytest.raises(Retry):
        await scan_live_set(ctx, str(uuid.uuid4()))
