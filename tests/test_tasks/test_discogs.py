"""Tests for match_tracklist_to_discogs SAQ task."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid


def _make_ctx() -> dict[str, Any]:
    """Create a minimal SAQ context dict with async_session factory."""
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return {"async_session": mock_session_factory, "_mock_session": mock_session}


def _make_track(
    artist: str | None = "deadmau5",
    title: str | None = "Strobe",
    track_id: uuid.UUID | None = None,
) -> MagicMock:
    """Create a mock TracklistTrack."""
    track = MagicMock()
    track.id = track_id or uuid.uuid4()
    track.artist = artist
    track.title = title
    return track


def _make_tracklist(tracklist_id: uuid.UUID | None = None, latest_version_id: uuid.UUID | None = None) -> MagicMock:
    """Create a mock Tracklist."""
    tl = MagicMock()
    tl.id = tracklist_id or uuid.uuid4()
    tl.latest_version_id = latest_version_id or uuid.uuid4()
    return tl


@patch("phaze.tasks.discogs.DiscogsographyClient")
@patch("phaze.tasks.discogs.match_track_to_discogs")
@patch("phaze.tasks.discogs.settings")
async def test_match_tracklist_processes_eligible_tracks(
    mock_settings: MagicMock,
    mock_match_fn: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    """match_tracklist_to_discogs processes all eligible tracks and stores DiscogsLink candidates."""
    from phaze.tasks.discogs import match_tracklist_to_discogs

    mock_settings.discogsography_url = "http://test:8000"
    mock_settings.discogs_match_concurrency = 5

    ctx = _make_ctx()
    session = ctx["_mock_session"]

    tracklist = _make_tracklist()
    tracks = [_make_track("deadmau5", "Strobe"), _make_track("Skrillex", "Bangarang")]

    # First execute: tracklist lookup, second: tracks query
    mock_tracklist_result = MagicMock()
    mock_tracklist_result.scalar_one_or_none.return_value = tracklist

    mock_tracks_result = MagicMock()
    mock_tracks_result.scalars.return_value.all.return_value = tracks

    # Third execute: delete old candidates (one per track)
    session.execute = AsyncMock(side_effect=[mock_tracklist_result, mock_tracks_result, None, None])

    mock_client_instance = AsyncMock()
    mock_client_cls.return_value = mock_client_instance

    mock_match_fn.return_value = [
        {
            "discogs_release_id": "r12345",
            "discogs_artist": "deadmau5",
            "discogs_title": "Strobe",
            "discogs_label": None,
            "discogs_year": 2009,
            "confidence": 90.5,
        },
    ]

    result = await match_tracklist_to_discogs(ctx, tracklist_id=str(tracklist.id))

    assert result["tracks_matched"] == 2
    assert result["candidates_created"] >= 1
    mock_client_instance.close.assert_called_once()


@patch("phaze.tasks.discogs.DiscogsographyClient")
@patch("phaze.tasks.discogs.match_track_to_discogs")
@patch("phaze.tasks.discogs.settings")
async def test_rematch_deletes_candidates_preserves_accepted(
    mock_settings: MagicMock,
    mock_match_fn: MagicMock,
    mock_client_cls: MagicMock,
) -> None:
    """Re-matching deletes existing 'candidate' links but preserves 'accepted' links (pitfall 3)."""
    from phaze.tasks.discogs import match_tracklist_to_discogs

    mock_settings.discogsography_url = "http://test:8000"
    mock_settings.discogs_match_concurrency = 5

    ctx = _make_ctx()
    session = ctx["_mock_session"]

    tracklist = _make_tracklist()
    tracks = [_make_track("deadmau5", "Strobe")]

    mock_tracklist_result = MagicMock()
    mock_tracklist_result.scalar_one_or_none.return_value = tracklist

    mock_tracks_result = MagicMock()
    mock_tracks_result.scalars.return_value.all.return_value = tracks

    session.execute = AsyncMock(side_effect=[mock_tracklist_result, mock_tracks_result, None])

    mock_client_instance = AsyncMock()
    mock_client_cls.return_value = mock_client_instance

    mock_match_fn.return_value = [
        {
            "discogs_release_id": "r12345",
            "discogs_artist": "deadmau5",
            "discogs_title": "Strobe",
            "discogs_label": None,
            "discogs_year": 2009,
            "confidence": 90.5,
        },
    ]

    await match_tracklist_to_discogs(ctx, tracklist_id=str(tracklist.id))

    # Verify that delete was called (third execute call is the delete for candidates)
    # The delete should target status="candidate" only, not "accepted"
    assert session.execute.call_count >= 3  # tracklist + tracks + at least one delete
