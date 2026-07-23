"""Tests for fingerprint service layer: Protocol, adapters, orchestrator, progress."""

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
