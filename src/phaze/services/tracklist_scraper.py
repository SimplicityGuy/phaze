"""Scraper service for 1001Tracklists.com search and tracklist extraction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import random
import re
from typing import ClassVar
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
import httpx
import structlog


logger = structlog.get_logger(__name__)


class DisallowedScrapeHostError(ValueError):
    """Raised when a URL's scheme or host falls outside the 1001Tracklists allow-list.

    Guards scrape_tracklist() against SSRF: a compromised/malicious search-results response or a
    DB-stored source_url pointing at an internal address (e.g. cloud metadata, an internal Docker
    service) must never reach the outbound HTTP client (phaze-k5zz).
    """

    def __init__(self, url: str) -> None:
        super().__init__(f"Refusing to scrape disallowed URL: {url!r}")
        self.url = url


class SearchParseFailureError(RuntimeError):
    """Raised when search result rows are present but NONE of them could be parsed.

    ``search()``'s docstring used to say it "returns empty list on 403, parse failure, or no
    results" -- three completely different outcomes collapsing into the same ``[]`` with no
    distinguishing signal. A page that genuinely has no matches and a page whose markup has
    drifted out from under our selectors (a site redesign) looked identical to every caller and
    to the logs (phaze-mk6y, same failure class as phaze-ds1z). This exception is raised only
    when the item selector matched at least one candidate row AND the result-link selector
    matched none of them -- i.e. the selectors themselves are stale, not merely that this
    particular query has zero hits.
    """

    def __init__(self, candidate_count: int) -> None:
        super().__init__(
            f"Search returned {candidate_count} candidate result row(s) but could not parse a link from any of them -- selectors are likely stale"
        )
        self.candidate_count = candidate_count


@dataclass
class TracklistSearchResult:
    """A single result from a 1001Tracklists search."""

    external_id: str
    title: str
    url: str
    artist: str | None = None
    date: str | None = None


@dataclass
class ScrapedTrack:
    """A single track extracted from a tracklist page."""

    position: int
    artist: str | None = None
    title: str | None = None
    label: str | None = None
    timestamp: str | None = None
    is_mashup: bool = False
    remix_info: str | None = None


@dataclass
class ScrapedTracklist:
    """Scraped tracklist data from a 1001Tracklists detail page."""

    external_id: str
    title: str
    artist: str | None = None
    event: str | None = None
    date: str | None = None
    tracks: list[ScrapedTrack] = field(default_factory=list)
    source_url: str = ""


class TracklistScraper:
    """Async scraper for 1001Tracklists.com with rate limiting."""

    BASE_URL = "https://www.1001tracklists.com"
    SEARCH_URL = f"{BASE_URL}/search/result.php"
    MIN_DELAY = 2.0
    MAX_DELAY = 5.0

    # Hosts a scrape is ever allowed to target (phaze-k5zz). Exact-match only -- comparing against
    # urlsplit().hostname (never .netloc, which can carry userinfo like "evil@1001tracklists.com")
    # and lower-cased, so this can't be bypassed by case tricks or a lookalike subdomain such as
    # "www.1001tracklists.com.evil.com" (a suffix/substring check would wrongly allow that). The
    # bare apex is included alongside "www" because 1001tracklists.com redirects there and both
    # are legitimate hosts the scraper can encounter in a source_url.
    _ALLOWED_HOSTS: ClassVar[frozenset[str]] = frozenset({"1001tracklists.com", "www.1001tracklists.com"})

    # CSS selectors as class constants for easy updating.
    #
    # phaze-mk6y: re-derived against a real fetched search-results page 2026-07-18. The row
    # selector (.bItm) was still valid, but every field selector INSIDE the row was stale
    # (.bItmT a / .bItmArtist / .bItmDate all matched 0 nodes against 30 real result rows), which
    # search() silently swallowed to []. The current markup exposes the result link via
    # `a[href*='/tracklist/']`; its text is the full "Artist @ Event, Venue, City, Country"
    # string and its href carries the external id plus a trailing date.
    _SEARCH_ITEM_SELECTOR = ".bItm"
    _SEARCH_RESULT_LINK_SELECTOR = "a[href*='/tracklist/']"
    _SEARCH_LINK_TEXT_ARTIST_SEPARATOR = " @ "

    # phaze-mk6y SCOPE NOTE: only the SEARCH page selectors above were verified against live
    # markup. The DETAIL-page selectors below were NOT exercised live -- the bead explicitly
    # calls them "likely stale too" since the site clearly redesigned, but re-deriving them
    # requires a real fetched detail page, which is out of scope here (no live requests are made
    # by this scraper's tests or CI). Treat these as UNVERIFIED, not confirmed-working, until a
    # real detail page is captured and checked against them.
    _TRACK_ITEM_SELECTOR = ".tlpItem"
    _TRACK_ARTIST_SELECTOR = ".tp a"
    _TRACK_NAME_SELECTOR = ".tN"
    _TRACK_LABEL_SELECTOR = ".tL"
    _TRACK_TIME_SELECTOR = ".cueTime"
    _META_ARTIST_SELECTOR = ".artName"
    _META_EVENT_SELECTOR = ".evtName"
    _META_DATE_SELECTOR = ".evtDate"

    _DEFAULT_HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.1001tracklists.com/",
    }

    _EXTERNAL_ID_PATTERN = re.compile(r"/tracklist/([^/?#]+)")
    # phaze-mk6y: the href-embedded date trails the slug, e.g.
    # ".../sven-vath-time-warp-maimarkthalle-mannheim-germany-2024-10-25". Anchored so it only
    # matches a trailing date, not an incidental "-1-2-3"-shaped substring earlier in the slug.
    _HREF_DATE_PATTERN = re.compile(r"-(\d{4})-(\d{1,2})-(\d{1,2})(?:[/?#]|$)")

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Initialize scraper with optional httpx client."""
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(headers=self._DEFAULT_HEADERS, timeout=30.0)
            self._owns_client = True

    @classmethod
    def _is_allowed_url(cls, url: str) -> bool:
        """Return True iff url is https and its host is on the 1001Tracklists allow-list.

        Uses ``.hostname`` (not ``.netloc``) so userinfo tricks like ``https://evil@
        1001tracklists.com`` can't smuggle a different apparent netloc past the check, and
        lower-cases the comparison so the allow-list match is case-insensitive (phaze-k5zz).
        """
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.hostname is not None and parts.hostname.lower() in cls._ALLOWED_HOSTS

    async def _rate_limit(self) -> None:
        """Apply rate limiting delay between requests."""
        delay = random.uniform(self.MIN_DELAY, self.MAX_DELAY)  # noqa: S311  # nosec B311
        await asyncio.sleep(delay)

    async def search(self, query: str) -> list[TracklistSearchResult]:
        """Search 1001Tracklists for tracklists matching query.

        Returns empty list on 403, HTTP error, or genuinely no results. Raises
        SearchParseFailureError (NOT swallowed to []) when the results page contains candidate
        rows that none of them could be parsed from -- that is a stale-selector defect, and must
        be distinguishable from a normal empty result (phaze-mk6y).
        """
        await self._rate_limit()

        try:
            response = await self._client.post(
                self.SEARCH_URL,
                data={"main_search": query, "search_selection": "9"},
            )
        except httpx.HTTPError:
            logger.warning("HTTP error during search for: %s", query)
            return []

        if response.status_code != 200:
            logger.info("Search returned status %d for: %s", response.status_code, query)
            return []

        try:
            return self._parse_search_results(response.text)
        except SearchParseFailureError:
            # Already logged at ERROR inside _parse_search_results with the candidate count.
            # Propagate rather than collapsing to [] -- that collapse is exactly phaze-mk6y.
            raise
        except Exception:
            logger.warning("Failed to parse search results for: %s", query, exc_info=True)
            return []

    def _parse_search_results(self, html: str) -> list[TracklistSearchResult]:
        """Parse search result HTML into TracklistSearchResult objects.

        Raises:
            SearchParseFailureError: the item selector matched one or more candidate rows but the
                result-link selector matched none of them -- the selectors are stale, this is not
                "no results" (phaze-mk6y).
        """
        soup = BeautifulSoup(html, "lxml")
        items = soup.select(self._SEARCH_ITEM_SELECTOR)
        results: list[TracklistSearchResult] = []
        unparseable_rows = 0

        for item in items:
            link = item.select_one(self._SEARCH_RESULT_LINK_SELECTOR)
            if link is None:
                unparseable_rows += 1
                continue

            href_str = str(link.get("href", ""))
            title = link.get_text(strip=True)

            # Extract external_id from URL path
            match = self._EXTERNAL_ID_PATTERN.search(href_str)
            if match is None:
                continue
            external_id = match.group(1)

            if href_str.startswith("/"):
                url = f"{self.BASE_URL}{href_str}"
            else:
                # Absolute href from the response body -- e.g. a compromised/malicious upstream
                # response embedding "http://169.254.169.254/tracklist/x/", which the external_id
                # pattern above matches as a substring. Only forward it if scheme+host clear the
                # allow-list; otherwise drop this result rather than let a poisoned search page
                # hand the caller an SSRF target (phaze-k5zz).
                if not self._is_allowed_url(href_str):
                    logger.warning("Skipping search result with disallowed href host: %s", href_str)
                    continue
                url = href_str

            # phaze-mk6y: the current markup carries no separate artist/date elements -- the
            # link text is the full "Artist @ Event, Venue, City, Country" string and the date is
            # embedded at the end of the href slug.
            artist_part, separator, _rest = title.partition(self._SEARCH_LINK_TEXT_ARTIST_SEPARATOR)
            artist = artist_part.strip() if separator else None
            date = self._extract_date_from_href(href_str)

            results.append(
                TracklistSearchResult(
                    external_id=external_id,
                    title=title,
                    url=url,
                    artist=artist,
                    date=date,
                )
            )

        if items and unparseable_rows == len(items):
            logger.error(
                "Search result parsing found %d candidate row(s) but the result-link selector matched none of them -- selectors are likely stale",
                len(items),
            )
            raise SearchParseFailureError(len(items))

        return results

    @classmethod
    def _extract_date_from_href(cls, href: str) -> str | None:
        """Extract a trailing YYYY-MM-DD date from a tracklist href's slug, if present."""
        match = cls._HREF_DATE_PATTERN.search(href)
        if match is None:
            return None
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    async def scrape_tracklist(self, url: str) -> ScrapedTracklist:
        """Scrape a tracklist detail page and extract track data.

        Extracts title, artist, event, date, and individual track entries.

        Raises:
            DisallowedScrapeHostError: url's scheme is not https or its host is not on the
                1001Tracklists allow-list. Checked BEFORE any network I/O so a malicious/poisoned
                source_url (attacker-controlled DB row or search-result href) never reaches the
                outbound HTTP client -- the SSRF surface this method previously had (phaze-k5zz).
        """
        if not self._is_allowed_url(url):
            logger.warning("Refusing to scrape disallowed URL: %s", url)
            raise DisallowedScrapeHostError(url)

        await self._rate_limit()

        try:
            response = await self._client.get(url)
        except httpx.HTTPError:
            logger.warning("HTTP error scraping tracklist: %s", url)
            raise

        # A blocked/challenge page (403/429/5xx) is served as HTML that parses to an empty
        # tracklist; without this guard the caller would treat that as a valid zero-track scrape
        # and clobber good data. Mirror search()'s status handling, but RAISE instead of returning
        # empty so SAQ retries the job rather than persisting the degraded result (phaze-o8sy).
        if response.status_code != 200:
            logger.warning("Scrape returned status %d for: %s", response.status_code, url)
            raise httpx.HTTPStatusError(
                f"Unexpected status {response.status_code} while scraping tracklist",
                request=response.request,
                response=response,
            )

        # Extract external_id from URL
        match = self._EXTERNAL_ID_PATTERN.search(url)
        external_id = match.group(1) if match else ""

        soup = BeautifulSoup(response.text, "lxml")

        # Extract title from h1 or <title>
        h1 = soup.find("h1")
        title_tag = soup.find("title")
        title = ""
        if h1:
            title = h1.get_text(strip=True)
        elif title_tag:
            title = title_tag.get_text(strip=True).replace(" | 1001Tracklists", "")

        # Extract metadata
        artist_el = soup.select_one(self._META_ARTIST_SELECTOR)
        event_el = soup.select_one(self._META_EVENT_SELECTOR)
        date_el = soup.select_one(self._META_DATE_SELECTOR)

        artist = artist_el.get_text(strip=True) if artist_el else None
        event = event_el.get_text(strip=True) if event_el else None
        tracklist_date = date_el.get_text(strip=True) if date_el else None

        # Extract tracks
        tracks: list[ScrapedTrack] = []
        for idx, track_item in enumerate(soup.select(self._TRACK_ITEM_SELECTOR), start=1):
            track = self._parse_track_item(track_item, idx)
            tracks.append(track)

        return ScrapedTracklist(
            external_id=external_id,
            title=title,
            artist=artist,
            event=event,
            date=tracklist_date,
            tracks=tracks,
            source_url=url,
        )

    def _parse_track_item(self, item: Tag, position: int) -> ScrapedTrack:
        """Parse a single track item from the tracklist page."""
        # Artist: join text from all .tp a links
        artist_links = item.select(self._TRACK_ARTIST_SELECTOR)
        artist = " & ".join(a.get_text(strip=True) for a in artist_links) if artist_links else None

        # Title
        title_el = item.select_one(self._TRACK_NAME_SELECTOR)
        title = title_el.get_text(strip=True) if title_el else None

        # Label
        label_el = item.select_one(self._TRACK_LABEL_SELECTOR)
        label = label_el.get_text(strip=True) if label_el else None

        # Timestamp
        time_el = item.select_one(self._TRACK_TIME_SELECTOR)
        timestamp = time_el.get_text(strip=True) if time_el else None

        # Mashup detection
        classes = item.get("class")
        is_mashup = "mashup" in classes if isinstance(classes, list) else "mashup" in str(classes or "")

        # Remix info: extract from title if present
        remix_info = None
        if title:
            remix_match = re.search(r"\(([^)]*(?:remix|mix|edit|bootleg|vip)[^)]*)\)", title, re.IGNORECASE)
            if remix_match:
                remix_info = remix_match.group(1)

        return ScrapedTrack(
            position=position,
            artist=artist,
            title=title,
            label=label,
            timestamp=timestamp,
            is_mashup=is_mashup,
            remix_info=remix_info,
        )

    async def close(self) -> None:
        """Close the httpx client if we own it."""
        if self._owns_client:
            await self._client.aclose()
