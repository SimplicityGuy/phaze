"""Tests for TracklistScraper service."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from phaze.config import get_settings
from phaze.services.tracklist_scraper import (
    DisallowedScrapeHostError,
    ScrapedTracklist,
    SearchParseFailureError,
    TracklistScraper,
    TracklistSearchResult,
)


# --- Fixtures ---
#
# phaze-mk6y: SAMPLE_SEARCH_HTML mirrors the CURRENT live markup (verified against a real fetched
# search-results page 2026-07-18), not the stale `.bItmT` / `.bItmArtist` / `.bItmDate` shape the
# scraper used to assume. Rows are `.bItm` (plus other classes like `action oItm`); the result
# link is reachable via `a[href*='/tracklist/']`, its text is the full
# "Artist @ Event, Venue, City, Country" string, and its href carries the external id plus a
# trailing YYYY-MM-DD date. No live request is made anywhere in this module -- every case is a
# saved/synthetic HTML fixture.

SAMPLE_SEARCH_HTML = """
<html><body>
<div class="bItm action oItm">
  <div class="bTitle">
    <a href="/tracklist/abc123/skrillex-coachella-empire-polo-club-indio-united-states-2025-04-12">
      Skrillex @ Coachella, Empire Polo Club, Indio, United States
    </a>
  </div>
  <div class="bCont">
    <span class="artM">Skrillex</span>
  </div>
</div>
<div class="bItm action oItm">
  <div class="bTitle">
    <a href="/tracklist/def456/deadmau5-edc-las-vegas-nv-united-states-2025-06-20">
      deadmau5 @ EDC, Las Vegas Motor Speedway, Las Vegas, United States
    </a>
  </div>
  <div class="bCont">
    <span class="artM">deadmau5</span>
  </div>
</div>
</body></html>
"""

SAMPLE_EMPTY_SEARCH_HTML = "<html><body></body></html>"

# A page whose row selector (.bItm) still matches but whose result-link selector matches
# nothing -- the shape phaze-mk6y actually hit live: 30 real rows, 0 parsed links, no signal.
SAMPLE_STALE_SEARCH_HTML = """
<html><body>
<div class="bItm action oItm">
  <div class="bItmT"><span>Skrillex @ Coachella 2025</span></div>
</div>
<div class="bItm action oItm">
  <div class="bItmT"><span>deadmau5 @ EDC 2025</span></div>
</div>
</body></html>
"""

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


@pytest.fixture(autouse=True)
def _clear_scraper_caches():
    """Reset the process-wide TTL caches (phaze-hu8v) so tests never leak state via shared keys."""
    TracklistScraper._search_cache.clear()
    TracklistScraper._tracklist_cache.clear()
    yield
    TracklistScraper._search_cache.clear()
    TracklistScraper._tracklist_cache.clear()


@pytest.fixture(autouse=True)
def _no_real_rate_limit_sleep(request):
    """Stub asyncio.sleep for every test except the one that specifically asserts its bounds.

    MIN_DELAY/MAX_DELAY are now 8.0/12.0s (phaze-hu8v, robots.txt Crawl-delay compliance) --
    without this, every test exercising search()/scrape_tracklist() would burn 8-12 real wall
    seconds. `test_rate_limit_delay` installs its own nested patch to inspect the sampled delay.
    """
    if request.node.name == "test_rate_limit_delay":
        yield
        return
    with patch("phaze.services.tracklist_scraper.asyncio.sleep", new_callable=AsyncMock):
        yield


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
        assert results[0].title == "Skrillex @ Coachella, Empire Polo Club, Indio, United States"
        assert "/tracklist/abc123/" in results[0].url
        assert results[0].artist == "Skrillex"
        assert results[0].date == "2025-04-12"

        assert results[1].external_id == "def456"
        assert results[1].artist == "deadmau5"
        assert results[1].date == "2025-06-20"

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

    def test_min_delay_meets_robots_txt_crawl_delay_floor(self):
        """robots.txt publishes Crawl-delay: 8 (verified live 2026-07-18); the sampled delay must
        never be able to go below that, and jitter only ever adds time above the floor (phaze-hu8v)."""
        assert TracklistScraper.MIN_DELAY >= 8.0
        assert TracklistScraper.MAX_DELAY >= TracklistScraper.MIN_DELAY


class TestTracklistScraperSearchParseFailure:
    """phaze-mk6y: a stale-selector defect must be LOUD, not collapsed into the same [] as a
    genuine no-match search."""

    @pytest.mark.asyncio
    async def test_search_raises_when_all_candidate_rows_are_unparseable(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_STALE_SEARCH_HTML))

        scraper = TracklistScraper(client=client)
        with pytest.raises(SearchParseFailureError) as exc_info:
            await scraper.search("Skrillex Coachella")
        assert exc_info.value.candidate_count == 2

    def test_parse_search_results_raises_when_every_row_lacks_a_link(self):
        """A single `.bItm` present but no `a[href*='/tracklist/']` inside it -- the selector
        itself is stale, this must not be silently treated as zero results."""
        html = '<html><body><div class="bItm"><div class="bItmT"><span>No link here</span></div></div></body></html>'
        scraper = TracklistScraper(client=AsyncMock(spec=httpx.AsyncClient))
        with pytest.raises(SearchParseFailureError) as exc_info:
            scraper._parse_search_results(html)
        assert exc_info.value.candidate_count == 1

    def test_parse_search_results_mixed_rows_returns_only_the_parseable_ones(self):
        """Some rows parsing and some not is a normal partial page, NOT a stale-selector signal --
        only raise when EVERY candidate row fails to yield a link."""
        html = (
            "<html><body>"
            '<div class="bItm action oItm"><div class="bTitle">'
            '<a href="/tracklist/abc123/skrillex-coachella-2025-04-12">Skrillex @ Coachella, Indio, United States</a>'
            "</div></div>"
            '<div class="bItm action oItm"><div class="bItmT"><span>No link here</span></div></div>'
            "</body></html>"
        )
        scraper = TracklistScraper(client=AsyncMock(spec=httpx.AsyncClient))
        results = scraper._parse_search_results(html)
        assert len(results) == 1
        assert results[0].external_id == "abc123"

    def test_parse_search_results_skips_item_without_result_link(self):
        """Historical name kept for continuity with phaze-k5zz coverage below: with the new
        selector, an item lacking a `/tracklist/`-containing anchor is exactly the stale-selector
        case, so a lone such item raises rather than being silently skipped (phaze-mk6y)."""
        html = '<html><body><div class="bItm"><div class="bItmM"></div></div></body></html>'
        scraper = TracklistScraper(client=AsyncMock(spec=httpx.AsyncClient))
        with pytest.raises(SearchParseFailureError):
            scraper._parse_search_results(html)


class TestTracklistScraperHrefDateExtraction:
    """phaze-mk6y: the date lives in the href slug, not a separate `.bItmDate` element."""

    def test_extract_date_from_href_parses_trailing_date(self):
        href = "/tracklist/25fhn7c9/sven-vath-time-warp-maimarkthalle-mannheim-germany-2024-10-25"
        assert TracklistScraper._extract_date_from_href(href) == "2024-10-25"

    def test_extract_date_from_href_returns_none_without_a_trailing_date(self):
        assert TracklistScraper._extract_date_from_href("/tracklist/abc123/no-date-here") is None

    def test_extract_date_from_href_zero_pads_single_digit_month_and_day(self):
        href = "/tracklist/xyz/some-event-2024-3-5"
        assert TracklistScraper._extract_date_from_href(href) == "2024-03-05"


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
    async def test_scrape_non_200_raises_status_error(self):
        """A 403/blocked page must RAISE so SAQ retries rather than parsing an empty tracklist (phaze-o8sy)."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(403, "<html><body>Access denied</body></html>"))

        scraper = TracklistScraper(client=client)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await scraper.scrape_tracklist("https://www.1001tracklists.com/tracklist/abc123/test.html")
        assert exc_info.value.response.status_code == 403

    @pytest.mark.asyncio
    async def test_scrape_429_raises_status_error(self):
        """A 429 rate-limit page also raises rather than silently returning zero tracks (phaze-o8sy)."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(429, "Too Many Requests"))

        scraper = TracklistScraper(client=client)
        with pytest.raises(httpx.HTTPStatusError):
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

    @pytest.mark.asyncio
    async def test_scrape_title_falls_back_to_title_tag_when_no_h1(self):
        """With no <h1>, the title is taken from <title> with the site suffix stripped."""
        html = "<html><head><title>Zeds Dead @ EDC 2025 | 1001Tracklists</title></head><body></body></html>"
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(200, html))

        scraper = TracklistScraper(client=client)
        result = await scraper.scrape_tracklist("https://www.1001tracklists.com/tracklist/zd99/zeds-dead.html")

        assert result.title == "Zeds Dead @ EDC 2025"
        assert result.external_id == "zd99"


class TestTracklistScraperSearchEdgeCases:
    """Search error/parse branches and result-item skip paths."""

    @pytest.mark.asyncio
    async def test_search_http_error_returns_empty(self):
        """A transport error during the search POST is logged and yields []."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

        scraper = TracklistScraper(client=client)
        assert await scraper.search("skrillex") == []

    @pytest.mark.asyncio
    async def test_search_parse_failure_returns_empty(self):
        """An unexpected (non-stale-selector) parser exception on a 200 body is still swallowed to []."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_SEARCH_HTML))

        scraper = TracklistScraper(client=client)
        with patch.object(scraper, "_parse_search_results", side_effect=ValueError("bad parse")):
            assert await scraper.search("skrillex") == []

    @pytest.mark.asyncio
    async def test_search_parse_failure_error_is_not_swallowed(self):
        """SearchParseFailureError specifically must propagate out of search(), not collapse to []."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_SEARCH_HTML))

        scraper = TracklistScraper(client=client)
        with patch.object(scraper, "_parse_search_results", side_effect=SearchParseFailureError(5)), pytest.raises(SearchParseFailureError):
            await scraper.search("skrillex")


class TestTracklistScraperSsrfGuard:
    """SSRF regression coverage for phaze-k5zz.

    A compromised/malicious upstream search response can embed an absolute href pointing at an
    internal address, and _EXTERNAL_ID_PATTERN's substring match on "/tracklist/([^/?#]+)" is
    satisfied by paths like "http://169.254.169.254/tracklist/x/". Both _parse_search_results and
    scrape_tracklist must reject anything off the 1001Tracklists host allow-list.
    """

    def test_parse_search_results_drops_internal_ip_absolute_href(self):
        """An absolute href pointing at a cloud-metadata-style internal IP is dropped, not forwarded."""
        html = (
            '<html><body><div class="bItm"><div class="bItmT">'
            '<a href="http://169.254.169.254/tracklist/x/evil.html">Metadata</a>'
            "</div></div></body></html>"
        )
        scraper = TracklistScraper(client=AsyncMock(spec=httpx.AsyncClient))
        assert scraper._parse_search_results(html) == []

    def test_parse_search_results_drops_lookalike_domain_absolute_href(self):
        """A lookalike domain (real domain as a subdomain of an attacker-controlled one) is dropped."""
        html = (
            '<html><body><div class="bItm"><div class="bItmT">'
            '<a href="https://1001tracklists.com.evil.com/tracklist/x/evil.html">Fake</a>'
            "</div></div></body></html>"
        )
        scraper = TracklistScraper(client=AsyncMock(spec=httpx.AsyncClient))
        assert scraper._parse_search_results(html) == []

    def test_parse_search_results_keeps_legitimate_relative_href(self):
        """A normal relative href from the real site still resolves and is kept."""
        html = '<html><body><div class="bItm"><div class="bItmT"><a href="/tracklist/abc123/skrillex.html">Skrillex</a></div></div></body></html>'
        scraper = TracklistScraper(client=AsyncMock(spec=httpx.AsyncClient))
        results = scraper._parse_search_results(html)
        assert len(results) == 1
        assert results[0].url == "https://www.1001tracklists.com/tracklist/abc123/skrillex.html"

    def test_parse_search_results_keeps_legitimate_absolute_href(self):
        """A legitimate absolute href on the allow-listed host is kept unchanged."""
        html = (
            '<html><body><div class="bItm"><div class="bItmT">'
            '<a href="https://www.1001tracklists.com/tracklist/abc123/skrillex.html">Skrillex</a>'
            "</div></div></body></html>"
        )
        scraper = TracklistScraper(client=AsyncMock(spec=httpx.AsyncClient))
        results = scraper._parse_search_results(html)
        assert len(results) == 1
        assert results[0].url == "https://www.1001tracklists.com/tracklist/abc123/skrillex.html"

    @pytest.mark.asyncio
    async def test_scrape_tracklist_rejects_internal_ip_url(self):
        """scrape_tracklist refuses a cloud-metadata-style internal IP URL before any request."""
        client = AsyncMock(spec=httpx.AsyncClient)
        scraper = TracklistScraper(client=client)
        with pytest.raises(DisallowedScrapeHostError):
            await scraper.scrape_tracklist("http://169.254.169.254/tracklist/x/evil.html")
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_scrape_tracklist_rejects_off_allowlist_https_host(self):
        """scrape_tracklist refuses an https URL whose host is not on the allow-list."""
        client = AsyncMock(spec=httpx.AsyncClient)
        scraper = TracklistScraper(client=client)
        with pytest.raises(DisallowedScrapeHostError):
            await scraper.scrape_tracklist("https://evil.com/tracklist/x/evil.html")
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_scrape_tracklist_rejects_lookalike_domain(self):
        """scrape_tracklist refuses a lookalike domain that merely contains the real one as a substring."""
        client = AsyncMock(spec=httpx.AsyncClient)
        scraper = TracklistScraper(client=client)
        with pytest.raises(DisallowedScrapeHostError):
            await scraper.scrape_tracklist("https://1001tracklists.com.evil.com/tracklist/x/evil.html")
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_scrape_tracklist_rejects_userinfo_trick(self):
        """A userinfo trick (https://evil@1001tracklists.com/...) must not smuggle a disallowed host past hostname checks."""
        client = AsyncMock(spec=httpx.AsyncClient)
        scraper = TracklistScraper(client=client)
        # hostname here IS 1001tracklists.com (userinfo "evil@" is stripped by urlsplit), so this
        # one is actually ALLOWED -- included to document that .hostname, not .netloc, is what
        # gates the request.
        client.get = AsyncMock(return_value=_mock_response(200, SAMPLE_EMPTY_SEARCH_HTML))
        result = await scraper.scrape_tracklist("https://evil@1001tracklists.com/tracklist/abc123/test.html")
        assert result.external_id == "abc123"

    @pytest.mark.asyncio
    async def test_scrape_tracklist_still_works_for_legitimate_url(self):
        """The allow-list guard does not break the normal, legitimate scrape path."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(200, SAMPLE_TRACKLIST_HTML))
        scraper = TracklistScraper(client=client)
        result = await scraper.scrape_tracklist("https://www.1001tracklists.com/tracklist/abc123/skrillex.html")
        assert isinstance(result, ScrapedTracklist)
        assert result.external_id == "abc123"


class TestTracklistScraperLifecycle:
    """__init__ client ownership + close()."""

    def test_constructs_own_client_when_none_supplied(self):
        """With no client, the scraper owns a real httpx.AsyncClient it must close."""
        scraper = TracklistScraper()
        assert scraper._owns_client is True
        assert isinstance(scraper._client, httpx.AsyncClient)

    def test_does_not_own_injected_client(self):
        injected = AsyncMock(spec=httpx.AsyncClient)
        scraper = TracklistScraper(client=injected)
        assert scraper._owns_client is False
        assert scraper._client is injected

    @pytest.mark.asyncio
    async def test_close_closes_owned_client(self):
        """close() aclose()s a self-owned client exactly once."""
        scraper = TracklistScraper()
        with patch.object(scraper._client, "aclose", new_callable=AsyncMock) as mock_aclose:
            await scraper.close()
            mock_aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_leaves_injected_client_open(self):
        """close() must NOT aclose a caller-owned client."""
        injected = AsyncMock(spec=httpx.AsyncClient)
        scraper = TracklistScraper(client=injected)
        await scraper.close()
        injected.aclose.assert_not_called()


class TestTracklistScraperHonestUserAgent:
    """phaze-hu8v: replace the spoofed Chrome UA with an honest, identifying one."""

    def test_owned_client_uses_honest_identifying_user_agent(self):
        """A self-constructed client's User-Agent identifies phaze and a contact URL, and no
        longer pretends to be a browser."""
        scraper = TracklistScraper()
        user_agent = scraper._client.headers["User-Agent"]

        assert user_agent.startswith("phaze/")
        assert "Chrome" not in user_agent
        assert "Mozilla" not in user_agent

        settings = get_settings()
        assert settings.scraper_contact_url in user_agent

    def test_build_headers_includes_accept_and_referer(self):
        """The rest of the header set (Accept/Accept-Language/Referer) is unchanged by the UA fix."""
        headers = TracklistScraper._build_headers()
        assert headers["Referer"] == "https://www.1001tracklists.com/"
        assert "text/html" in headers["Accept"]

    def test_injected_client_headers_are_not_overridden(self):
        """Passing an explicit client (as every test and every real call site does) bypasses
        _build_headers entirely -- the caller owns that client's headers."""
        injected = AsyncMock(spec=httpx.AsyncClient)
        scraper = TracklistScraper(client=injected)
        assert scraper._client is injected


class TestTracklistScraperCaching:
    """phaze-hu8v: repeat lookups must not re-hit the site."""

    @pytest.mark.asyncio
    async def test_search_cache_hit_skips_second_network_call(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_SEARCH_HTML))
        scraper = TracklistScraper(client=client)

        first = await scraper.search("Skrillex Coachella")
        second = await scraper.search("Skrillex Coachella")

        assert client.post.await_count == 1
        assert second == first

    @pytest.mark.asyncio
    async def test_search_cache_is_keyed_per_query(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(200, SAMPLE_SEARCH_HTML))
        scraper = TracklistScraper(client=client)

        await scraper.search("Skrillex Coachella")
        await scraper.search("deadmau5 EDC")

        assert client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_search_does_not_cache_a_403(self):
        """A blocked/transient response must not poison the cache with a permanent []."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.post = AsyncMock(return_value=_mock_response(403, "Forbidden"))
        scraper = TracklistScraper(client=client)

        await scraper.search("Skrillex")
        await scraper.search("Skrillex")

        assert client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_scrape_cache_hit_skips_second_network_call(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(200, SAMPLE_TRACKLIST_HTML))
        scraper = TracklistScraper(client=client)
        url = "https://www.1001tracklists.com/tracklist/cache1/test.html"

        first = await scraper.scrape_tracklist(url)
        second = await scraper.scrape_tracklist(url)

        assert client.get.await_count == 1
        assert second is first

    @pytest.mark.asyncio
    async def test_scrape_does_not_cache_a_403(self):
        """A blocked/challenge page must never be cached as if it were a valid scrape (phaze-o8sy)."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(return_value=_mock_response(403, "<html><body>Access denied</body></html>"))
        scraper = TracklistScraper(client=client)
        url = "https://www.1001tracklists.com/tracklist/cache2/test.html"

        with pytest.raises(httpx.HTTPStatusError):
            await scraper.scrape_tracklist(url)
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.scrape_tracklist(url)

        assert client.get.await_count == 2
