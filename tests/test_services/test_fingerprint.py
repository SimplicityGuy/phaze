"""Tests for fingerprint service layer: Protocol, adapters, orchestrator, progress."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from phaze.services.fingerprint import (
    AudfprintAdapter,
    CombinedMatch,
    FingerprintEngine,
    FingerprintOrchestrator,
    IngestResult,
    PanakoAdapter,
    QueryMatch,
    get_fingerprint_progress,
)


class TestFingerprintEngineProtocol:
    """Verify Protocol is runtime-checkable."""

    def test_audfprint_adapter_implements_protocol(self):
        adapter = AudfprintAdapter()
        assert isinstance(adapter, FingerprintEngine)

    def test_panako_adapter_implements_protocol(self):
        adapter = PanakoAdapter()
        assert isinstance(adapter, FingerprintEngine)


class TestAudfprintAdapter:
    """Tests for AudfprintAdapter HTTP client."""

    def test_adapter_name(self):
        adapter = AudfprintAdapter()
        assert adapter.name == "audfprint"

    def test_adapter_weight(self):
        adapter = AudfprintAdapter()
        assert adapter.weight == 0.6

    def test_adapter_custom_url(self):
        adapter = AudfprintAdapter(base_url="http://custom:9999")
        assert adapter.base_url == "http://custom:9999"

    async def test_health_returns_true_on_200(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
        adapter = AudfprintAdapter(base_url="http://test:8001")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8001")
        result = await adapter.health()
        assert result is True
        await adapter.close()

    async def test_health_returns_false_on_error(self):
        def raise_error(request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(raise_error)
        adapter = AudfprintAdapter(base_url="http://test:8001")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8001")
        result = await adapter.health()
        assert result is False
        await adapter.close()

    async def test_ingest_returns_success(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
        adapter = AudfprintAdapter(base_url="http://test:8001")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8001")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "success"
        assert result.error is None
        await adapter.close()

    async def test_ingest_returns_failed_on_error(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(500, json={"error": "internal"}))
        adapter = AudfprintAdapter(base_url="http://test:8001")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8001")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "failed"
        assert result.error is not None
        await adapter.close()

    async def test_query_returns_matches(self):
        response_data = {"matches": [{"track_id": "abc123", "confidence": 85.0}]}
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=response_data))
        adapter = AudfprintAdapter(base_url="http://test:8001")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8001")
        result = await adapter.query("/data/music/test.mp3")
        assert len(result) == 1
        assert result[0].track_id == "abc123"
        assert result[0].confidence == 85.0
        await adapter.close()


class TestPanakoAdapter:
    """Tests for PanakoAdapter HTTP client."""

    def test_adapter_name(self):
        adapter = PanakoAdapter()
        assert adapter.name == "panako"

    def test_adapter_weight(self):
        adapter = PanakoAdapter()
        assert adapter.weight == 0.4

    async def test_health_returns_true_on_200(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.health()
        assert result is True
        await adapter.close()

    async def test_health_returns_false_on_error(self):
        def raise_error(request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(raise_error)
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.health()
        assert result is False
        await adapter.close()

    async def test_ingest_returns_success(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "success"
        await adapter.close()

    async def test_ingest_returns_failed_on_error(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(500, json={"error": "internal"}))
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "failed"
        assert result.error is not None
        await adapter.close()

    async def test_query_returns_matches(self):
        response_data = {"matches": [{"track_id": "xyz789", "confidence": 72.0}]}
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=response_data))
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.query("/data/music/test.mp3")
        assert len(result) == 1
        assert result[0].track_id == "xyz789"
        assert result[0].confidence == 72.0
        await adapter.close()

    async def test_query_returns_empty_on_non_200(self):
        transport = httpx.MockTransport(lambda _request: httpx.Response(404))
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.query("/data/music/test.mp3")
        assert result == []
        await adapter.close()

    async def test_ingest_returns_failed_on_exception(self):
        def raise_error(request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(raise_error)
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "failed"
        assert result.error is not None
        await adapter.close()

    async def test_query_returns_empty_on_exception(self):
        def raise_error(request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(raise_error)
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        result = await adapter.query("/data/music/test.mp3")
        assert result == []
        await adapter.close()


class TestFingerprintOrchestrator:
    """Tests for FingerprintOrchestrator scoring and coordination."""

    def _make_mock_engine(self, name: str, weight: float, query_results: list[QueryMatch] | None = None, ingest_result: IngestResult | None = None):
        engine = MagicMock()
        engine.name = name
        engine.weight = weight
        engine.ingest = AsyncMock(return_value=ingest_result or IngestResult(status="success"))
        engine.query = AsyncMock(return_value=query_results or [])
        engine.health = AsyncMock(return_value=True)
        return engine

    async def test_combined_query_weighted_average_both_engines(self):
        """Both engines match same track: weighted average (60% audfprint + 40% panako)."""
        audfprint = self._make_mock_engine(
            "audfprint",
            0.6,
            query_results=[QueryMatch(track_id="track-1", confidence=80.0)],
        )
        panako = self._make_mock_engine(
            "panako",
            0.4,
            query_results=[QueryMatch(track_id="track-1", confidence=90.0)],
        )
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/music/test.mp3")
        assert len(matches) == 1
        # 0.6 * 80 + 0.4 * 90 = 48 + 36 = 84.0
        assert matches[0].confidence == pytest.approx(84.0)
        assert matches[0].track_id == "track-1"
        assert matches[0].engines == {"audfprint": 80.0, "panako": 90.0}

    async def test_single_engine_match_capped_at_70(self):
        """Single-engine match is capped at 70% confidence (D-12)."""
        audfprint = self._make_mock_engine(
            "audfprint",
            0.6,
            query_results=[QueryMatch(track_id="track-1", confidence=95.0)],
        )
        panako = self._make_mock_engine("panako", 0.4, query_results=[])
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/music/test.mp3")
        assert len(matches) == 1
        assert matches[0].confidence == 70.0

    async def test_combined_query_sorted_by_confidence_descending(self):
        """Matches sorted by confidence descending."""
        audfprint = self._make_mock_engine(
            "audfprint",
            0.6,
            query_results=[
                QueryMatch(track_id="track-low", confidence=50.0),
                QueryMatch(track_id="track-high", confidence=90.0),
            ],
        )
        panako = self._make_mock_engine(
            "panako",
            0.4,
            query_results=[
                QueryMatch(track_id="track-low", confidence=60.0),
                QueryMatch(track_id="track-high", confidence=80.0),
            ],
        )
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/music/test.mp3")
        assert len(matches) == 2
        assert matches[0].track_id == "track-high"
        assert matches[1].track_id == "track-low"
        # track-high: 0.6*90 + 0.4*80 = 54 + 32 = 86.0
        assert matches[0].confidence == pytest.approx(86.0)
        # track-low: 0.6*50 + 0.4*60 = 30 + 24 = 54.0
        assert matches[1].confidence == pytest.approx(54.0)

    async def test_ingest_all_calls_both_engines(self):
        """ingest_all calls both engines and returns per-engine results."""
        audfprint = self._make_mock_engine("audfprint", 0.6)
        panako = self._make_mock_engine("panako", 0.4)
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        results = await orchestrator.ingest_all("/data/music/test.mp3")
        assert "audfprint" in results
        assert "panako" in results
        assert results["audfprint"].status == "success"
        assert results["panako"].status == "success"

    async def test_ingest_engine_exception_returns_error_result(self):
        """Engine that raises during ingest returns error result, does not crash."""
        audfprint = self._make_mock_engine("audfprint", 0.6)
        audfprint.ingest = AsyncMock(side_effect=Exception("Container down"))
        panako = self._make_mock_engine("panako", 0.4)
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        results = await orchestrator.ingest_all("/data/music/test.mp3")
        assert results["audfprint"].status == "failed"
        assert "Container down" in results["audfprint"].error
        assert results["panako"].status == "success"

    async def test_health_all(self):
        """health_all checks all engines."""
        audfprint = self._make_mock_engine("audfprint", 0.6)
        panako = self._make_mock_engine("panako", 0.4)
        panako.health = AsyncMock(return_value=False)
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        result = await orchestrator.health_all()
        assert result == {"audfprint": True, "panako": False}


class TestGetFingerprintProgress:
    """Tests for progress tracking function."""

    async def test_get_progress_returns_counts(self):
        """get_fingerprint_progress returns total/completed/failed counts."""
        mock_session = AsyncMock()

        # Mock execute calls: total=100, completed=50, failed=5
        mock_result_total = MagicMock()
        mock_result_total.scalar_one.return_value = 100
        mock_result_completed = MagicMock()
        mock_result_completed.scalar_one.return_value = 50
        mock_result_failed = MagicMock()
        mock_result_failed.scalar_one.return_value = 5

        mock_session.execute = AsyncMock(side_effect=[mock_result_total, mock_result_completed, mock_result_failed])

        result = await get_fingerprint_progress(mock_session)
        assert result == {"total": 100, "completed": 50, "failed": 5}


class TestConfigSettings:
    """Tests for fingerprint config settings."""

    def test_config_has_audfprint_url(self):
        from phaze.config import Settings

        s = Settings(database_url="postgresql+asyncpg://x:x@localhost/x", redis_url="redis://localhost")
        assert s.audfprint_url == "http://audfprint:8001"

    def test_config_has_panako_url(self):
        from phaze.config import Settings

        s = Settings(database_url="postgresql+asyncpg://x:x@localhost/x", redis_url="redis://localhost")
        assert s.panako_url == "http://panako:8002"


class TestQueryMatchTimestamp:
    """Tests for QueryMatch timestamp field."""

    def test_query_match_timestamp_default_none(self):
        """QueryMatch timestamp defaults to None."""
        match = QueryMatch(track_id="track-1", confidence=85.0)
        assert match.timestamp is None

    def test_query_match_timestamp_set(self):
        """QueryMatch timestamp can be set."""
        match = QueryMatch(track_id="track-1", confidence=85.0, timestamp="04:32")
        assert match.timestamp == "04:32"


class TestCombinedMatchExtended:
    """Tests for CombinedMatch timestamp, resolved_artist, resolved_title fields."""

    def test_combined_match_timestamp_default_none(self):
        """CombinedMatch timestamp defaults to None."""
        match = CombinedMatch(track_id="track-1", confidence=84.0)
        assert match.timestamp is None

    def test_combined_match_timestamp_set(self):
        """CombinedMatch timestamp can be set."""
        match = CombinedMatch(track_id="track-1", confidence=84.0, timestamp="04:32")
        assert match.timestamp == "04:32"

    def test_combined_match_resolved_artist_default_none(self):
        """CombinedMatch resolved_artist defaults to None."""
        match = CombinedMatch(track_id="track-1", confidence=84.0)
        assert match.resolved_artist is None

    def test_combined_match_resolved_title_default_none(self):
        """CombinedMatch resolved_title defaults to None."""
        match = CombinedMatch(track_id="track-1", confidence=84.0)
        assert match.resolved_title is None

    def test_combined_match_all_new_fields(self):
        """CombinedMatch with all new fields set."""
        match = CombinedMatch(
            track_id="track-1",
            confidence=84.0,
            timestamp="04:32",
            resolved_artist="Deadmau5",
            resolved_title="Strobe",
        )
        assert match.timestamp == "04:32"
        assert match.resolved_artist == "Deadmau5"
        assert match.resolved_title == "Strobe"
