"""Tests for TracklistScraper service."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from phaze.services.tracklist_scraper import ScrapedTracklist, TracklistScraper, TracklistSearchResult


# --- Fixtures ---

SAMPLE_SEARCH_HTML = """
<html><body>
<div class="bItm">
  <div class="bItmT">
    <a href="/tracklist/abc123/skrillex-at-coachella-2025.html">
      Skrillex @ Coachella 2025
    </a>
  </div>
  <div class="bItmM">
    <span class="bItmArtist">Skrillex</span>
    <span class="bItmDate">2025-04-12</span>
  </div>
</div>
<div class="bItm">
  <div class="bItmT">
    <a href="/tracklist/def456/deadmau5-at-edc-2025.html">
      deadmau5 @ EDC Las Vegas 2025
    </a>
  </div>
  <div class="bItmM">
    <span class="bItmArtist">deadmau5</span>
    <span class="bItmDate">2025-06-20</span>
  </div>
</div>
</body></html>
"""

SAMPLE_EMPTY_SEARCH_HTML = "<html><body></body></html>"

SAMPLE_TRACKLIST_HTML = """
<html>
<head><title>Skrillex @ Coachella 2025 | 1001Tracklists</title></head>
<body>
<div id="tlMeta">
  <h1>Skrillex @ Coachella 2025</h1>
  <div class="meta">
    <span class="artName">Skrillex</span>
    <span class="evtName">Coachella</span>
    <span class="evtDate">2025-04-12</span>
  </div>
</div>
<div class="tlpTog">
  <div class="tlpItem">
    <span class="trackFormat">
      <span class="tp"><a>Skrillex</a></span>
      <span class="tN">Bangarang</span>
      <span class="tL">OWSLA</span>
    </span>
    <span class="cueTime">00:05:30</span>
  </div>
  <div class="tlpItem">
    <span class="trackFormat">
      <span class="tp"><a>Skrillex</a> &amp; <a>Diplo</a></span>
      <span class="tN">Where Are U Now (VIP Mix)</span>
      <span class="tL">Atlantic</span>
    </span>
    <span class="cueTime">00:10:15</span>
  </div>
  <div class="tlpItem mashup">
    <span class="trackFormat">
      <span class="tp"><a>Skrillex</a></span>
      <span class="tN">Scary Monsters</span>
      <span class="tL">mau5trap</span>
    </span>
    <span class="cueTime">00:15:00</span>
  </div>
</div>
</body></html>
"""


def _mock_response(status_code: int, text: str) -> httpx.Response:
    """Create a mock httpx.Response."""
    return httpx.Response(status_code=status_code, text=text, request=httpx.Request("GET", "https://example.com"))


class TestTracklistScraperSearch:
    """Tests for TracklistScraper.search()."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_SEARCH_HTML))

        scraper = TracklistScraper(client=client)
        results = await scraper.search("Skrillex Coachella")

        assert len(results) == 2
        assert isinstance(results[0], TracklistSearchResult)
        assert results[0].external_id == "abc123"
        assert results[0].title == "Skrillex @ Coachella 2025"
        assert "/tracklist/abc123/" in results[0].url
        assert results[0].artist == "Skrillex"
        assert results[0].date == "2025-04-12"

    @pytest.mark.asyncio
    async def test_search_empty_results(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_EMPTY_SEARCH_HTML))

        scraper = TracklistScraper(client=client)
        results = await scraper.search("nonexistent")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_403_returns_empty(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(403, "Forbidden"))

        scraper = TracklistScraper(client=client)
        results = await scraper.search("Skrillex")

        assert results == []

    @pytest.mark.asyncio
    async def test_rate_limit_delay(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_EMPTY_SEARCH_HTML))

        scraper = TracklistScraper(client=client)

        with patch("phaze.services.tracklist_scraper.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await scraper.search("test")
            mock_sleep.assert_called_once()
            delay = mock_sleep.call_args[0][0]
            assert scraper.MIN_DELAY <= delay <= scraper.MAX_DELAY


class TestTracklistScraperScrape:
    """Tests for TracklistScraper.scrape_tracklist()."""

    @pytest.mark.asyncio
    async def test_scrape_returns_tracklist(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(200, SAMPLE_TRACKLIST_HTML))

        scraper = TracklistScraper(client=client)
        result = await scraper.scrape_tracklist("https://www.1001tracklists.com/tracklist/abc123/skrillex.html")

        assert isinstance(result, ScrapedTracklist)
        assert result.external_id == "abc123"
        assert "Skrillex" in result.title
        assert len(result.tracks) == 3
        assert result.tracks[0].position == 1
        assert result.tracks[0].artist == "Skrillex"
        assert result.tracks[0].title == "Bangarang"
        assert result.tracks[0].label == "OWSLA"
        assert result.tracks[0].timestamp == "00:05:30"

    @pytest.mark.asyncio
    async def test_scrape_empty_tracks(self):
        empty_html = "<html><head><title>Test | 1001Tracklists</title></head><body><div id='tlMeta'><h1>Test</h1></div></body></html>"
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(200, empty_html))

        scraper = TracklistScraper(client=client)
        result = await scraper.scrape_tracklist("https://www.1001tracklists.com/tracklist/xyz789/test.html")

        assert isinstance(result, ScrapedTracklist)
        assert result.tracks == []

    @pytest.mark.asyncio
    async def test_scrape_http_error_raises(self):
        """HTTP errors during scrape are logged and re-raised."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        scraper = TracklistScraper(client=client)
        with pytest.raises(httpx.ConnectError):
            await scraper.scrape_tracklist("https://www.1001tracklists.com/tracklist/abc123/test.html")

    @pytest.mark.asyncio
    async def test_scrape_mashup_detection(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(200, SAMPLE_TRACKLIST_HTML))

        scraper = TracklistScraper(client=client)
        result = await scraper.scrape_tracklist("https://www.1001tracklists.com/tracklist/abc123/skrillex.html")

        # Third track has mashup class
        assert result.tracks[2].is_mashup is True
        # First track is not a mashup
        assert result.tracks[0].is_mashup is False
