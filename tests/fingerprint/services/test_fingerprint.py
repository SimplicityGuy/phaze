"""Tests for fingerprint service layer: Protocol, adapters, orchestrator, progress."""

import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from phaze.services.fingerprint import (
    AudfprintAdapter,
    CombinedMatch,
    EngineQueryError,
    FingerprintEngine,
    FingerprintOrchestrator,
    FingerprintQueryUnavailableError,
    IngestResult,
    PanakoAdapter,
    QueryMatch,
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

    async def test_query_returns_empty_on_4xx(self):
        """A 4xx is a FILE-level rejection by a healthy engine -- still an empty result (phaze-z7yw)."""
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

    async def test_query_raises_engine_error_on_exception(self):
        """A transport failure is an ENGINE-level fault: raise, never a silent empty (phaze-z7yw)."""

        def raise_error(request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(raise_error)
        adapter = PanakoAdapter(base_url="http://test:8002")
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test:8002")
        with pytest.raises(EngineQueryError):
            await adapter.query("/data/music/test.mp3")
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

    async def test_combined_query_carries_timestamp_from_single_engine_match(self):
        """phaze-nldg: a single-engine match's timestamp must survive into CombinedMatch.

        Before the fix, combined_query's accumulation kept ONLY confidence per (track_id,
        engine), discarding QueryMatch.timestamp entirely -- so tasks/scan.py's
        `timestamp=match.timestamp` pass-through was always None even when the engine
        reported a genuine match offset.
        """
        audfprint = self._make_mock_engine(
            "audfprint",
            0.6,
            query_results=[QueryMatch(track_id="track-1", confidence=95.0, timestamp="12.3")],
        )
        panako = self._make_mock_engine("panako", 0.4, query_results=[])
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/music/test.mp3")
        assert len(matches) == 1
        assert matches[0].timestamp == "12.3"

    async def test_combined_query_carries_best_scoring_engines_timestamp(self):
        """When both engines match the same track_id, the BEST-SCORING engine's timestamp wins.

        panako's raw per-engine confidence (90.0) beats audfprint's (80.0) here, so the
        combined match's timestamp must be panako's offset, not audfprint's, even though
        audfprint's own weight (0.6) is higher and dominates the weighted-average confidence.
        """
        audfprint = self._make_mock_engine(
            "audfprint",
            0.6,
            query_results=[QueryMatch(track_id="track-1", confidence=80.0, timestamp="5.0")],
        )
        panako = self._make_mock_engine(
            "panako",
            0.4,
            query_results=[QueryMatch(track_id="track-1", confidence=90.0, timestamp="7.5")],
        )
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/music/test.mp3")
        assert len(matches) == 1
        # 0.6 * 80 + 0.4 * 90 = 84.0 (weighted average still governs confidence)
        assert matches[0].confidence == pytest.approx(84.0)
        assert matches[0].timestamp == "7.5"

    async def test_combined_query_timestamp_none_when_engine_reports_none(self):
        """An engine that matches without a timestamp still yields a clean CombinedMatch."""
        audfprint = self._make_mock_engine(
            "audfprint",
            0.6,
            query_results=[QueryMatch(track_id="track-1", confidence=95.0, timestamp=None)],
        )
        panako = self._make_mock_engine("panako", 0.4, query_results=[])
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/music/test.mp3")
        assert len(matches) == 1
        assert matches[0].timestamp is None

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

    async def test_real_audfprint_adapter_enables_two_engine_agreement(self):
        """A NON-empty audfprint response (the phaze-uciu.4 fix) drives the weighted-average branch.

        Regression guard for phaze-uciu.4: while the audfprint sidecar's parser returned [] for
        every real query, AudfprintAdapter.query was always empty, combined_query never saw
        two-engine agreement, and every confidence was capped at 70.0 (panako-only). Here the
        REAL AudfprintAdapter deserializes a service /query payload that carries a match for the
        same track panako matched, so the orchestrator must take the weighted-average branch and
        exceed the 70.0 single-engine cap.
        """
        # The exact JSON shape services/audfprint/app.py emits once its parser produces a match.
        audfprint_payload = {"matches": [{"track_id": "/data/ref/track01.mp3", "confidence": 80.0}]}
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=audfprint_payload))
        audfprint = AudfprintAdapter(base_url="http://audfprint:8001", weight=0.6)
        audfprint._client = httpx.AsyncClient(transport=transport, base_url="http://audfprint:8001")

        panako = self._make_mock_engine(
            "panako",
            0.4,
            query_results=[QueryMatch(track_id="/data/ref/track01.mp3", confidence=90.0)],
        )

        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/query/song.wav")
        await audfprint.close()

        assert len(matches) == 1
        # Two-engine agreement: 0.6 * 80 + 0.4 * 90 = 84.0 -- above the 70.0 single-engine cap.
        assert matches[0].confidence == pytest.approx(84.0)
        assert matches[0].confidence > 70.0
        assert matches[0].engines == {"audfprint": 80.0, "panako": 90.0}


# NOTE: get_fingerprint_progress is exercised by the real-DB integration test
# tests/integration/test_fingerprint_progress.py (D-15). The former mock stub here
# (side_effect list + assert-your-own-dict) stayed green through any rewrite -- including a
# wrong one -- so it was deleted rather than adapted.


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


class TestSidecarHttpTimeout:
    """App-side client timeouts must exceed the sidecars' subprocess budget (phaze-mv1f).

    The sidecars now allow SUBPROCESS_TIMEOUT (default 3600s) per engine run, sized for
    multi-hour concert sets. The adapters previously capped every call at 120.0s, so the
    app side timed out first and the sidecar budget was meaningless.
    """

    def test_default_exceeds_sidecar_subprocess_budget(self):
        import phaze.services.fingerprint as fp

        assert fp.SIDECAR_HTTP_TIMEOUT_SEC > 3600

    def test_adapters_use_the_long_timeout(self):
        import phaze.services.fingerprint as fp

        for adapter in (AudfprintAdapter(), PanakoAdapter()):
            assert adapter._client.timeout.read == fp.SIDECAR_HTTP_TIMEOUT_SEC
            # A down sidecar must still fail fast; only the request budget is long.
            assert adapter._client.timeout.connect == 10.0

    async def test_health_uses_short_per_request_timeout(self):
        # A wedged sidecar must surface as unhealthy quickly, not pin health_all for the
        # full ingest budget.
        seen: dict[str, object] = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            seen["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200, json={"status": "ok"})

        adapter = AudfprintAdapter(base_url="http://test:8001")
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(_capture), base_url="http://test:8001")
        assert await adapter.health() is True
        await adapter.close()
        timeout = seen["timeout"]
        assert isinstance(timeout, dict)
        assert timeout["read"] == 35.0


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


class TestIngestErrorClassification:
    """phaze-ds1z: ingest failures must distinguish ENGINE-level from FILE-level faults.

    ``fingerprint_file`` keys its refuse-to-complete decision off ``engine_error``, so this
    classification is load-bearing: mislabel a 5xx as file-level and a total outage once
    again drains the backlog into fabricated FAILED rows; mislabel a 4xx as engine-level and
    one corrupt file stalls the whole lane behind retries.
    """

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_5xx_is_engine_level(self, adapter_cls):
        transport = httpx.MockTransport(lambda _request: httpx.Response(500, json={"error": "internal"}))
        adapter = adapter_cls()
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "failed"
        assert result.engine_error is True
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_4xx_is_file_level(self, adapter_cls):
        transport = httpx.MockTransport(lambda _request: httpx.Response(422, json={"error": "undecodable"}))
        adapter = adapter_cls()
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "failed"
        assert result.engine_error is False
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_transport_error_is_engine_level(self, adapter_cls):
        def raise_error(_request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(raise_error)
        adapter = adapter_cls()
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "failed"
        assert result.engine_error is True
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_success_is_not_an_error(self, adapter_cls):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
        adapter = adapter_cls()
        adapter._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        result = await adapter.ingest("/data/music/test.mp3")
        assert result.status == "success"
        assert result.engine_error is False
        await adapter.close()

    async def test_orchestrator_marks_raising_adapter_as_engine_level(self):
        """An adapter that RAISES is an engine fault by construction (adapters never raise per-file)."""
        audfprint = AsyncMock()
        audfprint.name = "audfprint"
        audfprint.ingest = AsyncMock(side_effect=Exception("Container down"))
        orchestrator = FingerprintOrchestrator(engines=[audfprint])
        results = await orchestrator.ingest_all("/data/music/test.mp3")
        assert results["audfprint"].engine_error is True


class TestWithinEngineRepeatReduction:
    """phaze-dxkz: one engine returning several matches for the SAME track must reduce by rule.

    The accumulation was a plain dict assignment, so the survivor was whichever occurrence the
    engine listed LAST -- an unwritten tie-break sitting directly above the deliberate,
    documented cross-engine reduction. Panako genuinely produces this shape: `query` returns
    one result per occurrence, so a track played twice in a set arrives as several results for
    the same reference path at different alignments and scores.

    The rule pinned here is: highest confidence wins; ties break on the EARLIEST reference
    offset; a missing/unparseable offset loses a tie.
    """

    def _engine(self, name: str, weight: float, matches: list[QueryMatch]):
        engine = MagicMock()
        engine.name = name
        engine.weight = weight
        engine.query = AsyncMock(return_value=matches)
        return engine

    async def test_best_confidence_survives_regardless_of_result_order(self):
        engine = self._engine(
            "panako",
            1.0,
            [
                QueryMatch(track_id="t", confidence=85.0, timestamp="0.0"),
                QueryMatch(track_id="t", confidence=8.0, timestamp="260.0"),
            ],
        )
        combined = await FingerprintOrchestrator([engine]).combined_query("/data/music/set.mp3")
        assert combined[0].engines["panako"] == 85.0
        assert combined[0].timestamp == "0.0"

    async def test_best_confidence_survives_when_the_weak_repeat_is_first(self):
        """The mirror case: the old code was only ever right by accident of ordering."""
        engine = self._engine(
            "panako",
            1.0,
            [
                QueryMatch(track_id="t", confidence=8.0, timestamp="260.0"),
                QueryMatch(track_id="t", confidence=85.0, timestamp="0.0"),
            ],
        )
        combined = await FingerprintOrchestrator([engine]).combined_query("/data/music/set.mp3")
        assert combined[0].engines["panako"] == 85.0
        assert combined[0].timestamp == "0.0"

    async def test_equal_confidence_breaks_on_the_earliest_reference_offset(self):
        """A track played twice at the same score: the FIRST occurrence is the track start."""
        engine = self._engine(
            "panako",
            1.0,
            [
                QueryMatch(track_id="t", confidence=60.0, timestamp="2400.0"),
                QueryMatch(track_id="t", confidence=60.0, timestamp="180.0"),
            ],
        )
        combined = await FingerprintOrchestrator([engine]).combined_query("/data/music/set.mp3")
        assert combined[0].timestamp == "180.0"

    async def test_a_hit_without_an_offset_loses_a_tie(self):
        engine = self._engine(
            "panako",
            1.0,
            [
                QueryMatch(track_id="t", confidence=60.0, timestamp="180.0"),
                QueryMatch(track_id="t", confidence=60.0, timestamp=None),
            ],
        )
        combined = await FingerprintOrchestrator([engine]).combined_query("/data/music/set.mp3")
        assert combined[0].timestamp == "180.0"

    async def test_repeat_reduction_does_not_disturb_the_cross_engine_reduction(self):
        """Reducing within an engine must still leave BOTH engines counted for the 2-engine average."""
        audfprint = self._engine("audfprint", 0.6, [QueryMatch(track_id="t", confidence=90.0, timestamp="5.0")])
        panako = self._engine(
            "panako",
            0.4,
            [
                QueryMatch(track_id="t", confidence=20.0, timestamp="700.0"),
                QueryMatch(track_id="t", confidence=80.0, timestamp="5.0"),
            ],
        )
        combined = await FingerprintOrchestrator([audfprint, panako]).combined_query("/data/music/set.mp3")
        assert len(combined) == 1
        assert combined[0].engines == {"audfprint": 90.0, "panako": 80.0}
        # 0.6*90 + 0.4*80 == 86.0 -- not capped, because both engines matched.
        assert combined[0].confidence == pytest.approx(86.0)

    async def test_distinct_tracks_are_still_kept_separately(self):
        engine = self._engine(
            "panako",
            1.0,
            [
                QueryMatch(track_id="a", confidence=70.0, timestamp="0.0"),
                QueryMatch(track_id="b", confidence=40.0, timestamp="10.0"),
            ],
        )
        combined = await FingerprintOrchestrator([engine]).combined_query("/data/music/set.mp3")
        assert {m.track_id for m in combined} == {"a", "b"}


class TestQueryErrorClassification:
    """phaze-z7yw: the query path must distinguish an ENGINE outage from a genuine no-match.

    ``scan_live_set`` writes a TERMINAL 'no_matches' verdict (and clears the recovery ledger
    row) on an empty ``combined_query`` result, so this classification is load-bearing: let a
    5xx/transport failure collapse to ``[]`` and a total sidecar outage silently converts the
    whole scanned backlog into permanent, success-looking no-match verdicts -- the query-path
    twin of the phaze-ds1z ingest drain.
    """

    def _adapter(self, adapter_cls, handler):
        adapter = adapter_cls(base_url="http://test:9000")
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test:9000")
        return adapter

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_5xx_raises_engine_query_error(self, adapter_cls):
        adapter = self._adapter(adapter_cls, lambda _request: httpx.Response(500, json={"error": "internal"}))
        with pytest.raises(EngineQueryError, match="HTTP 500"):
            await adapter.query("/data/music/test.mp3")
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_transport_error_raises_engine_query_error(self, adapter_cls):
        def raise_error(_request):
            raise httpx.ConnectError("Connection refused")

        adapter = self._adapter(adapter_cls, raise_error)
        with pytest.raises(EngineQueryError, match="Connection refused"):
            await adapter.query("/data/music/test.mp3")
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_5xx_carries_the_response_body_not_just_the_status(self, adapter_cls):
        """phaze-cf0z: the body IS the engine's stderr -- the only text naming the failure.

        ``_post_ingest`` has always preserved it; ``_post_query`` discarded it, so a
        query-path engine failure reached the hub log as a bare 'HTTP 500'. Applies to BOTH
        engines: the discard was in the shared helper, not in either adapter.
        """
        detail = "EOFError: Ran out of input"
        adapter = self._adapter(adapter_cls, lambda _request: httpx.Response(500, json={"detail": detail}))
        with pytest.raises(EngineQueryError, match=re.escape(detail)):
            await adapter.query("/data/music/test.mp3")
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_5xx_body_is_truncated_not_flooded(self, adapter_cls):
        """A per-file stack-trace flood in the hub log buries the signal it exists to surface."""
        # 'Z' appears nowhere in the surrounding "<engine>: query engine failure: HTTP 500: "
        # envelope, so the count is exactly the carried body length.
        adapter = self._adapter(adapter_cls, lambda _request: httpx.Response(500, text="Z" * 10_000))
        with pytest.raises(EngineQueryError) as excinfo:
            await adapter.query("/data/music/test.mp3")
        assert excinfo.value.args[0].count("Z") == 2000
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_5xx_with_empty_body_is_reported_as_such(self, adapter_cls):
        adapter = self._adapter(adapter_cls, lambda _request: httpx.Response(500, text=""))
        with pytest.raises(EngineQueryError, match="<no body>"):
            await adapter.query("/data/music/test.mp3")
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_4xx_is_file_level_empty(self, adapter_cls):
        adapter = self._adapter(adapter_cls, lambda _request: httpx.Response(422, json={"error": "undecodable"}))
        assert await adapter.query("/data/music/test.mp3") == []
        await adapter.close()

    @pytest.mark.parametrize("adapter_cls", [AudfprintAdapter, PanakoAdapter])
    async def test_200_empty_matches_is_a_genuine_no_match(self, adapter_cls):
        adapter = self._adapter(adapter_cls, lambda _request: httpx.Response(200, json={"matches": []}))
        assert await adapter.query("/data/music/test.mp3") == []
        await adapter.close()

    def _mock_engine(self, name: str, weight: float, query: AsyncMock) -> MagicMock:
        engine = MagicMock()
        engine.name = name
        engine.weight = weight
        engine.query = query
        return engine

    async def test_combined_query_raises_when_all_engines_error(self):
        audfprint = self._mock_engine("audfprint", 0.6, AsyncMock(side_effect=EngineQueryError("audfprint", "down")))
        panako = self._mock_engine("panako", 0.4, AsyncMock(side_effect=EngineQueryError("panako", "down")))
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        with pytest.raises(FingerprintQueryUnavailableError, match="all fingerprint engines"):
            await orchestrator.combined_query("/data/music/test.mp3")

    async def test_combined_query_survives_partial_outage_with_matches(self):
        """One engine down + one healthy match -> single-engine result (capped), no raise."""
        audfprint = self._mock_engine("audfprint", 0.6, AsyncMock(side_effect=EngineQueryError("audfprint", "down")))
        panako = self._mock_engine("panako", 0.4, AsyncMock(return_value=[QueryMatch(track_id="t1", confidence=95.0)]))
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        matches = await orchestrator.combined_query("/data/music/test.mp3")
        assert len(matches) == 1
        assert matches[0].track_id == "t1"
        assert matches[0].confidence == 70.0  # single-engine cap (D-12)

    async def test_combined_query_partial_outage_empty_is_genuine_no_match(self):
        """One engine down + one healthy empty answer -> [], NOT an outage raise."""
        audfprint = self._mock_engine("audfprint", 0.6, AsyncMock(side_effect=EngineQueryError("audfprint", "down")))
        panako = self._mock_engine("panako", 0.4, AsyncMock(return_value=[]))
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        assert await orchestrator.combined_query("/data/music/test.mp3") == []

    async def test_combined_query_treats_any_raise_as_engine_error(self):
        """A non-EngineQueryError raise out of query() still counts toward the all-errored floor."""
        audfprint = self._mock_engine("audfprint", 0.6, AsyncMock(side_effect=RuntimeError("adapter bug")))
        panako = self._mock_engine("panako", 0.4, AsyncMock(side_effect=httpx.ConnectError("refused")))
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])
        with pytest.raises(FingerprintQueryUnavailableError):
            await orchestrator.combined_query("/data/music/test.mp3")


class TestSystematicSingleEngineOutageIsDetected:
    """phaze-p3hj.3: a dead engine must fail LOUDLY, not silently degrade matching.

    phaze-p3hj.1 diagnosed the shape this class pins: audfprint's ``/query`` and ``/ingest``
    500'd on EVERY call for ~10 days (a zero-byte ``fprint.pklz``) while panako kept
    succeeding. ``combined_query`` weights both engines, so the outage never surfaced as an
    error -- it surfaced only as worse match quality, and the ONLY trace was the per-row
    ``fingerprint_results.error_message`` column an operator had to go looking for
    (phaze-p3hj.1 SS4). ``_post_query``/``_post_ingest``/``combined_query`` already emit a
    log at WARNING/ERROR on every single engine-level failure -- not merely the first one, and
    not merely when the whole request ultimately fails. These tests use the REAL
    ``AudfprintAdapter``/``PanakoAdapter`` HTTP classification path (not mocked-away engines)
    so a regression that widens an except clause, downgrades the log level, or stops logging
    once the sibling engine "covers" the failure would turn a test here RED.

    This is deliberately about DETECTION, not about changing the graceful-degradation
    behavior itself: a single dead engine correctly must NOT stall the pipeline (D-18), so the
    combined result keeps succeeding throughout. What must NOT happen is that success silently
    absorbing the failure erases every trace of it.
    """

    def _systematically_dead_adapter(
        self, adapter_cls: type[AudfprintAdapter] | type[PanakoAdapter], calls: list[str]
    ) -> AudfprintAdapter | PanakoAdapter:
        """A real adapter whose sidecar 500s on EVERY request -- the exact zero-byte-pklz shape."""

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(500, text="EOFError: Ran out of input")

        adapter = adapter_cls(base_url="http://dead:9999")
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://dead:9999")
        return adapter

    def _healthy_adapter(
        self, adapter_cls: type[AudfprintAdapter] | type[PanakoAdapter], track_id: str, confidence: float
    ) -> AudfprintAdapter | PanakoAdapter:
        """A real adapter whose sidecar answers every request normally."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/query":
                return httpx.Response(200, json={"matches": [{"track_id": track_id, "confidence": confidence}]})
            return httpx.Response(200, json={"status": "ok"})

        adapter = adapter_cls(base_url="http://healthy:9999")
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://healthy:9999")
        return adapter

    async def test_systematic_query_failure_is_logged_on_every_call(self, caplog: pytest.LogCaptureFixture) -> None:
        """A dead engine must NOT be silently absorbed into a degraded-but-successful result.

        Regression shape: audfprint 500s on every one of N files while panako answers every
        one. The combined result keeps succeeding (graceful degradation, unchanged), but that
        success must not erase the failure -- an ERROR-level log naming the dead engine fires
        on EVERY call, not just the first, so an operator tailing logs (or an alert rule
        counting ERROR-level fingerprint log lines) sees a sustained pattern rather than a
        single blip indistinguishable from a transient hiccup.
        """
        dead_calls: list[str] = []
        audfprint = self._systematically_dead_adapter(AudfprintAdapter, dead_calls)
        panako = self._healthy_adapter(PanakoAdapter, track_id="track-1", confidence=95.0)
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])

        n_files = 5
        for i in range(n_files):
            matches = await orchestrator.combined_query(f"/data/music/track-{i}.mp3")
            # D-18 unchanged: the dead sibling never stalls the lane -- panako alone answers.
            assert len(matches) == 1
            # The dead engine must never be silently credited into full two-engine agreement:
            # a single-engine match stays capped at 70.0 (D-12), never inflated as if BOTH
            # engines had agreed.
            assert matches[0].confidence == 70.0
            assert matches[0].engines == {"panako": 95.0}

        await audfprint.close()
        await panako.close()

        assert len(dead_calls) == n_files  # sanity: audfprint really was queried every time
        error_records = [r for r in caplog.records if r.levelname == "ERROR" and "audfprint" in r.getMessage()]
        # DETECTION, not merely correct output: if a future change stopped logging once the
        # sibling engine started "covering" the failure (e.g. de-duplicating repeat errors),
        # this count would drop below n_files while every assertion above still passed.
        assert len(error_records) == n_files

    async def test_systematic_ingest_failure_is_logged_on_every_call(self, caplog: pytest.LogCaptureFixture) -> None:
        """The ingest-path twin: a systematically 500ing engine logs every single attempt.

        Mirrors the query-path test above for ``ingest_all`` / ``_post_ingest``, the path
        phaze-p3hj.1 actually traced the outage through (the ingest response body is what
        carried the ``EOFError`` traceback into ``fingerprint_results.error_message``).
        """
        dead_calls: list[str] = []
        audfprint = self._systematically_dead_adapter(AudfprintAdapter, dead_calls)
        panako = self._healthy_adapter(PanakoAdapter, track_id="track-1", confidence=95.0)
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])

        n_files = 5
        for i in range(n_files):
            results = await orchestrator.ingest_all(f"/data/music/track-{i}.mp3")
            assert results["audfprint"].status == "failed"
            assert results["audfprint"].engine_error is True
            assert results["panako"].status == "success"

        await audfprint.close()
        await panako.close()

        assert len(dead_calls) == n_files
        warning_records = [r for r in caplog.records if r.levelname == "WARNING" and "audfprint" in r.getMessage()]
        assert len(warning_records) == n_files

    async def test_genuine_no_match_never_logs_at_error_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """phaze-z7yw contrast case: a HEALTHY engine's genuine no-match logs NOTHING.

        Pins the outage-vs-no-match distinction as an observable signal, not just a return
        value: a 200 with an empty match list is silent (nothing to alert on), so a future
        regression that started logging every no-match at ERROR would make outage alerts
        meaningless noise, and a regression that stopped logging every real 500 would make the
        two tests in this class collapse into the same (silent) behavior. Contrast this
        against ``test_systematic_query_failure_is_logged_on_every_call`` above, which is
        identical except for the sidecar's response code.
        """

        def empty_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"matches": []})

        audfprint = AudfprintAdapter(base_url="http://healthy:9999")
        audfprint._client = httpx.AsyncClient(transport=httpx.MockTransport(empty_handler), base_url="http://healthy:9999")
        panako = self._healthy_adapter(PanakoAdapter, track_id="track-1", confidence=95.0)
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])

        matches = await orchestrator.combined_query("/data/music/track.mp3")

        await audfprint.close()
        await panako.close()

        # Both engines answered; this is a genuine two-engine agreement, not a degraded result.
        assert len(matches) == 1
        assert matches[0].engines == {"panako": 95.0}
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    async def test_health_all_distinguishes_the_systematically_dead_engine(self) -> None:
        """The health signal bullet: ``health_all`` correctly reports the dead engine unhealthy.

        phaze-p3hj.1 SS3 found the audfprint health signal was not merely wrong but ABSENT --
        no production caller, no Docker healthcheck. phaze-p3hj.2 fixed the sidecar's own
        ``/health`` to key off loadability rather than existence; this pins the ORCHESTRATOR
        side of that signal against a systematically-dead engine (real adapter, real HTTP
        classification), so a future change that made ``AudfprintAdapter.health()`` swallow a
        5xx into ``True`` -- exactly the shape that made the outage invisible -- fails here.
        """
        dead_calls: list[str] = []
        audfprint = self._systematically_dead_adapter(AudfprintAdapter, dead_calls)
        panako = self._healthy_adapter(PanakoAdapter, track_id="track-1", confidence=95.0)
        orchestrator = FingerprintOrchestrator(engines=[audfprint, panako])

        for _ in range(3):
            result = await orchestrator.health_all()
            assert result == {"audfprint": False, "panako": True}

        await audfprint.close()
        await panako.close()
