"""Tests for DiscogsographyClient, compute_discogs_confidence, and match_track_to_discogs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

from phaze.services.discogs_matcher import (
    DiscogsographyClient,
    compute_discogs_confidence,
    match_track_to_discogs,
)


class TestDiscogsographyClient:
    """Tests for the DiscogsographyClient HTTP adapter."""

    async def test_search_releases_sends_correct_request(self) -> None:
        """search_releases sends GET to /api/search with q and types=release params."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "query": "deadmau5 strobe",
            "total": 1,
            "results": [{"type": "release", "id": "r12345", "name": "Strobe", "relevance": 0.85, "metadata": {}}],
        }
        mock_response.raise_for_status = MagicMock()

        client = DiscogsographyClient(base_url="http://test:8000")
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=mock_response)

        await client.search_releases("deadmau5 strobe")

        client._client.get.assert_called_once()
        call_args = client._client.get.call_args
        assert call_args[0][0] == "/api/search"
        params = call_args[1].get("params", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert params["q"] == "deadmau5 strobe"
        assert params["types"] == "release"

    async def test_search_releases_returns_results(self) -> None:
        """search_releases returns list of dicts from results key."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"type": "release", "id": "r12345", "name": "Strobe", "relevance": 0.85, "metadata": {}},
                {"type": "release", "id": "r67890", "name": "Ghosts", "relevance": 0.70, "metadata": {}},
            ],
        }
        mock_response.raise_for_status = MagicMock()

        client = DiscogsographyClient(base_url="http://test:8000")
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=mock_response)

        results = await client.search_releases("deadmau5")
        assert len(results) == 2
        assert results[0]["id"] == "r12345"

    async def test_search_releases_handles_connect_error(self) -> None:
        """search_releases returns empty list on ConnectError."""
        client = DiscogsographyClient(base_url="http://test:8000")
        client._client = AsyncMock()
        client._client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        results = await client.search_releases("deadmau5")
        assert results == []

    async def test_search_releases_handles_timeout(self) -> None:
        """search_releases returns empty list on TimeoutException."""
        client = DiscogsographyClient(base_url="http://test:8000")
        client._client = AsyncMock()
        client._client.get = AsyncMock(side_effect=httpx.TimeoutException("Timed out"))

        results = await client.search_releases("deadmau5")
        assert results == []


class TestComputeDiscogsConfidence:
    """Tests for compute_discogs_confidence scoring function."""

    def test_returns_float_between_0_and_100(self) -> None:
        """Confidence is a float in 0-100 range."""
        score = compute_discogs_confidence("deadmau5", "Strobe", "Strobe", 0.85)
        assert 0 <= score <= 100
        assert isinstance(score, float)

    def test_blends_string_sim_and_relevance(self) -> None:
        """Score combines token_set_ratio (0.6 weight) and relevance (0.4 weight)."""
        # Exact string match should give high string_sim (~1.0)
        # With relevance=1.0, score should be near 100
        score = compute_discogs_confidence("deadmau5", "Strobe", "deadmau5 Strobe", 1.0)
        assert score > 90

        # With relevance=0.0, only string_sim contributes (0.6 weight)
        score_low_rel = compute_discogs_confidence("deadmau5", "Strobe", "deadmau5 Strobe", 0.0)
        assert score_low_rel < score

    def test_handles_word_reordering(self) -> None:
        """token_set_ratio handles word reordering well."""
        score1 = compute_discogs_confidence("deadmau5", "Strobe", "deadmau5 Strobe", 0.85)
        score2 = compute_discogs_confidence("deadmau5", "Strobe", "Strobe - deadmau5", 0.85)
        # Both should be high since token_set_ratio is order-insensitive
        assert score1 > 70
        assert score2 > 70
        # Scores should be similar (within 15 points)
        assert abs(score1 - score2) < 15

    def test_low_match_returns_low_score(self) -> None:
        """Completely different strings produce low score."""
        score = compute_discogs_confidence("deadmau5", "Strobe", "Justin Bieber Baby", 0.1)
        assert score < 50


class TestMatchTrackToDiscogs:
    """Tests for match_track_to_discogs."""

    async def test_returns_top_3_scored_results(self) -> None:
        """match_track_to_discogs returns at most top 3 results."""
        mock_client = AsyncMock()
        mock_client.search_releases.return_value = [
            {"id": f"r{i}", "name": f"Result {i}", "relevance": 0.9 - i * 0.1, "metadata": {}} for i in range(5)
        ]

        track = MagicMock()
        track.artist = "deadmau5"
        track.title = "Strobe"

        results = await match_track_to_discogs(mock_client, track)
        assert len(results) <= 3

    async def test_skips_tracks_with_none_artist(self) -> None:
        """Tracks with None artist are skipped (D-02)."""
        mock_client = AsyncMock()
        track = MagicMock()
        track.artist = None
        track.title = "Strobe"

        results = await match_track_to_discogs(mock_client, track)
        assert results == []

    async def test_skips_tracks_with_none_title(self) -> None:
        """Tracks with None title are skipped (D-02)."""
        mock_client = AsyncMock()
        track = MagicMock()
        track.artist = "deadmau5"
        track.title = None

        results = await match_track_to_discogs(mock_client, track)
        assert results == []

    async def test_skips_tracks_with_empty_artist(self) -> None:
        """Tracks with empty string artist are skipped."""
        mock_client = AsyncMock()
        track = MagicMock()
        track.artist = ""
        track.title = "Strobe"

        results = await match_track_to_discogs(mock_client, track)
        assert results == []

    async def test_results_contain_required_fields(self) -> None:
        """Each result contains discogs_release_id, confidence, and metadata fields."""
        mock_client = AsyncMock()
        mock_client.search_releases.return_value = [
            {"id": "r12345", "name": "deadmau5 - Strobe", "relevance": 0.85, "metadata": {"year": 2009}},
        ]

        track = MagicMock()
        track.artist = "deadmau5"
        track.title = "Strobe"

        results = await match_track_to_discogs(mock_client, track)
        assert len(results) >= 1
        result = results[0]
        assert "discogs_release_id" in result
        assert "confidence" in result
        assert "discogs_title" in result
