"""Scraper service for 1001Tracklists.com SEARCH -- the httpx half of the drain's two requests.

SCOPE, AND WHY IT IS ONLY THE SEARCH PAGE (phaze-2akf)
------------------------------------------------------
This module once also fetched and parsed DETAIL pages (``scrape_tracklist`` /
``_parse_track_item``). That path is gone. It was httpx-only, so it had no browser and could never
clear the Turnstile interstitial the detail pages sit behind -- meaning even perfect selectors
would have parsed the pre-JS page. Measured against the committed capture
``tests/identify/fixtures/tracklist_render/25fhn7c9-ok.html``, every per-track selector it used
(``.tp a`` / ``.tN`` / ``.tL`` / ``.cueTime``) matched ZERO nodes while the container ``.tlpItem``
matched 52 -- i.e. it found 52 track rows and extracted nothing from each, producing a zero-track
result that looked like a successfully scraped empty tracklist.

Detail pages are now fetched by :mod:`phaze.services.tracklist_render` (a real browser) and parsed
by :mod:`phaze.services.tracklist_parser` (selectors re-derived against two real captures, verified
52/52 and 12/12). There is exactly ONE per-track selector set in the tree, and it is that one.

What stays here is the search POST, its result parsing, and
:func:`reserve_host_request_slot` -- the process-wide whole-host schedule that BOTH this module and
the renderer draw from, so the published ``Crawl-delay: 8`` is a single budget rather than two.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.metadata
import random
import re
import time
from typing import ClassVar
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
import httpx
import structlog

from phaze.config import get_settings


logger = structlog.get_logger(__name__)


def honest_user_agent_token() -> str:
    """Return phaze's identifying User-Agent token, ``phaze/<version> (+<contact url>)``.

    Read from settings on every call (not baked into a module-level constant) so tests can
    exercise a fresh `get_settings()` value.

    phaze-fq9h.1: shared with the Patchright renderer, which APPENDS this to the real browser's
    own Chrome UA rather than replacing it. The httpx scraper has no browser to contradict, so it
    sends this alone; a browser that claimed not to be a browser would be exactly the kind of
    inconsistency Turnstile scores. Same identity, two honest presentations of it.
    """
    settings = get_settings()
    version = importlib.metadata.version("phaze")
    return f"phaze/{version} (+{settings.scraper_contact_url})"


class DisallowedScrapeHostError(ValueError):
    """Raised when a URL's scheme or host falls outside the 1001Tracklists allow-list.

    The SSRF guard (phaze-k5zz): a compromised/malicious search-results response, or a DB-stored
    ``source_url`` pointing at an internal address (e.g. cloud metadata, an internal Docker
    service), must never reach an outbound client. Raised by :meth:`TracklistScraper.search` when a
    followed redirect leaves the allow-list, and by
    :mod:`phaze.services.tracklist_render` before it navigates a detail page.
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
    """A single result from a 1001Tracklists search.

    ``artist`` / ``event`` / ``date`` are the three signals the result scorer
    (``services.tracklist_result_scorer``, phaze-fq9h.6) weighs against the file's derived query.
    All three are optional because a real results page mixes genuine set rows with rows that have
    no artist at all -- promo/aftermovie video entries such as "Sunburn Festival - Official
    Aftermovie 2019" carry no ``" @ "`` separator, and 32 of the 180 captured rows are that shape.
    Those are exactly the rows a blind "take the top result" would render into an empty tracklist,
    so they are represented honestly (artist/event ``None``) rather than being coerced into a
    plausible-looking string the scorer would then score against.
    """

    external_id: str
    title: str
    url: str
    artist: str | None = None
    date: str | None = None
    event: str | None = None


class _TTLCache[T]:
    """Minimal in-process TTL cache, shared across `TracklistScraper` instances (phaze-hu8v).

    1001Tracklists' own robots.txt asks for an 8s crawl-delay per request, and a tracklist for a
    past event essentially never changes -- the cheapest polite request is the one never made.
    This simple in-memory cache covers the common case of the same query being looked up
    repeatedly within one running process (e.g. a long drain slice), without adding a storage
    dependency.

    The PERSISTENT half of "cached and never re-fetched" lives one layer up, in
    :mod:`phaze.services.tracklist_lookup_cache`: one row per unique set, consulted before the
    drain spends anything, so a set answered in a PRIOR process or run costs zero requests. This
    class stays deliberately DB-oblivious (no session, unit-testable without one). Conditional
    requests (ETag / If-Modified-Since) remain unimplemented on purpose: since a published
    tracklist does not change, "never re-fetch what we already have" is strictly stronger than a
    cheaper 304 -- zero requests beats one small one against a per-host budget counted in requests,
    not bytes.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        """Return the cached value for key, or None if absent/expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            del self._entries[key]
            return None
        return value

    def set(self, key: str, value: T) -> None:
        """Cache value under key for this cache's configured TTL, evicting expired entries first.

        phaze-a18s4: ``get()`` only ever evicted the ONE key it was asked to read, so an entry
        nobody happens to re-query again just sits in ``_entries`` forever -- in the long-lived
        drain worker, which populates this cache once per unique query and mostly never repeats
        one, that is an unbounded, monotonically-growing dict for the process lifetime. Sweeping
        every already-expired entry here (on the write path, which the drain hits once per
        unique lookup regardless of repeats) bounds the cache to roughly one TTL window's worth
        of live entries instead of the whole run's history.
        """
        now = time.monotonic()
        expired = [k for k, (expires_at, _) in self._entries.items() if now >= expires_at]
        for k in expired:
            del self._entries[k]
        self._entries[key] = (now + self._ttl_seconds, value)

    def clear(self) -> None:
        """Drop every cached entry (test isolation)."""
        self._entries.clear()


class TracklistScraper:
    """Async scraper for 1001Tracklists.com with rate limiting.

    Compliance posture (phaze-hu8v, robots.txt fetched live 2026-07-18, re-confirmed 2026-07-24):
    ``User-agent: *`` gets ``Allow: /`` at ``Crawl-delay: 8``, with ``Disallow: /js/``,
    ``/user/``, ``/action/``, ``/projects/``. A separate block blanket-disallows ~30 NAMED
    commercial/AI crawlers (GPTBot, AhrefsBot, SemrushBot, Amazonbot, PetalBot, DotBot, MJ12bot,
    Sogou, omgili, and others) that phaze is not one of. The site publishes no Terms of Service
    (confirmed by the operator 2026-07-18). MIN_DELAY/MAX_DELAY below honor the Crawl-delay,
    ``_build_headers`` sends an honest identifying User-Agent instead of a spoofed browser UA, and
    the Disallow list is honored BY CONSTRUCTION rather than a runtime robots.txt fetch/parse: this
    class only ever requests ``SEARCH_URL`` and hrefs matching ``_SEARCH_RESULT_LINK_SELECTOR``
    (``a[href*='/tracklist/']``) pulled from a search-results page, so it never follows a
    ``/user/`` profile link (search rows do link to them) or any other disallowed path. A runtime
    parser (e.g. the ``protego`` library) would additionally re-derive Crawl-delay/Disallow if the
    site's policy ever changes rather than trusting these hardcoded values indefinitely -- noted
    as a deliberate, not-yet-implemented enhancement rather than a compliance gap, since the
    ">= 8s" and "honor the Disallow list" requirements are both independently satisfied above.
    """

    BASE_URL = "https://www.1001tracklists.com"
    SEARCH_URL = f"{BASE_URL}/search/result.php"

    # phaze-hu8v: robots.txt publishes "Crawl-delay: 8" for general agents (verified live
    # 2026-07-18). We previously crawled at 2-5s -- 1.6x to 4x FASTER than permitted, out of
    # compliance on every request. 8.0 is a hard floor; MAX_DELAY only adds jitter ABOVE it, it
    # must never bring the sampled delay back under the published minimum.
    MIN_DELAY = 8.0
    MAX_DELAY = 12.0

    # phaze-hu8v: tracklists don't change once published -- cache search results in-process for a
    # while so a long slice re-touching the same query doesn't re-hit the site at all.
    _CACHE_TTL_SECONDS: ClassVar[float] = 6 * 60 * 60  # 6 hours

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

    # phaze-2akf: the DETAIL-page selector block that used to sit here is GONE, along with
    # `scrape_tracklist` / `_parse_track_item`. It was never verified against live markup, and when
    # it finally was (against tests/identify/fixtures/tracklist_render/25fhn7c9-ok.html) every
    # per-track selector matched zero nodes. Detail-page parsing now lives in
    # `services/tracklist_parser.py` -- one selector set, fixture-verified, exercised by the drain.

    _EXTERNAL_ID_PATTERN = re.compile(r"/tracklist/([^/?#]+)")
    # phaze-7zoh: Tracklist.external_id is String(50) (models/tracklist.py) and is also the
    # ON CONFLICT idempotency key. The captured slug is the short id today (e.g.
    # "25fhn7c9"), but a URL-shape change that moves the human-readable slug into this first
    # path segment would blow the column width and abort the store transaction. Bound it here,
    # at the scraper boundary, rather than let an oversized value reach Postgres.
    _EXTERNAL_ID_MAX_LEN = 50
    # phaze-mk6y: the href-embedded date trails the slug, e.g.
    # ".../sven-vath-time-warp-maimarkthalle-mannheim-germany-2024-10-25". Anchored so it only
    # matches a trailing date, not an incidental "-1-2-3"-shaped substring earlier in the slug.
    #
    # phaze-fq9h.6 STALENESS FIX: the site now appends a ".html" extension to that slug, so the
    # original `(?:[/?#]|$)` tail could never match a live href and this pattern scored **0 hits
    # across all 180 real result rows** in tests/identify/fixtures/tracklist_search/ -- i.e.
    # `TracklistSearchResult.date` was silently ALWAYS None. That is the same stale-selector class
    # as the dead detail selectors (phaze-2akf), and it is far more dangerous here: date is the
    # only signal that separates a recurring festival's editions from each other, so a scorer
    # trusting this field would have ranked date-blind and picked whichever year the site listed
    # first. The optional extension is now consumed before the tail anchor.
    _HREF_DATE_PATTERN = re.compile(r"-(\d{4})-(\d{1,2})-(\d{1,2})(?:\.html?)?(?:[/?#]|$)")

    # phaze-fq9h.6: the row's own displayed date cell, verified live at 180/180 rows. Preferred
    # over the href slug because it is a real rendered field rather than a URL-formatting artifact
    # -- a slug can be renamed without the tracklist changing, and the two disagreeing is itself a
    # signal worth catching (the fixture test asserts they agree on every captured row).
    _SEARCH_ROW_DATE_SELECTOR = 'div[title="tracklist date"]'
    _ISO_DATE_IN_TEXT_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

    # phaze-hu8v: shared across every TracklistScraper instance in this process, since each call
    # site (``tasks/tracklist_drain.py``) constructs a fresh scraper per job. A process-lifetime,
    # in-memory TTL cache is the simplest way to make "repeat lookups don't re-hit the site" true
    # across those short-lived instances without adding a storage dependency; see `_TTLCache`
    # docstring for why this isn't the full persistent cache.
    _search_cache: ClassVar[_TTLCache[list[TracklistSearchResult]]] = _TTLCache(_CACHE_TTL_SECONDS)

    # phaze-wb1o: process-wide shared rate-limiter state. `_rate_limit` used to be a private
    # per-coroutine sleep with no shared lock/timestamp -- since each search call site
    # (tasks/tracklist_drain.py) constructs a fresh TracklistScraper() per SAQ job, and the controller
    # worker runs `worker_max_jobs` (default 8) jobs concurrently in ONE process, N concurrent
    # jobs each slept their own independent 8-12s and then fired immediately, so the AGGREGATE
    # request rate scaled ~Nx past the MIN_DELAY floor the module exists to honor. A `asyncio.Lock`
    # + shared monotonic "next allowed slot" is sufficient (and correct) ONLY because every
    # docker-compose*.yml in this repo runs a single `worker` container / single process for the
    # controller role (no `replicas:`/`scale:`) -- this is a process-wide, not cross-host,
    # serializer. If the controller is ever scaled to >1 replica this stops being sufficient and
    # needs a Redis-backed limiter instead (the `cache_redis` handle already wired onto the
    # controller queue -- see `check_rate_limit` in services/proposal.py for the existing
    # atomic-Lua-script pattern this would follow).
    _rate_limit_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _next_request_at: ClassVar[float | None] = None

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Initialize scraper with optional httpx client."""
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            # phaze-8ib8: httpx does NOT follow redirects by default. The bare apex host
            # ("1001tracklists.com") is deliberately on _ALLOWED_HOSTS (phaze-k5zz) BECAUSE it
            # redirects to "www" -- so every request to an apex URL, and any server-side slug move,
            # previously came back as an un-followed 301/302 that the non-200 guard (phaze-o8sy)
            # turned into a permanently-retried HTTPStatusError. Follow redirects here;
            # _is_allowed_url is re-checked against the FINAL response.url after the request (see
            # search()) so this can't be used to smuggle a request off-host through a
            # malicious/compromised redirect chain.
            self._client = httpx.AsyncClient(headers=self._build_headers(), timeout=30.0, follow_redirects=True)
            self._owns_client = True

    @staticmethod
    def _build_headers() -> dict[str, str]:
        """Build request headers, including an honest, identifying User-Agent (phaze-hu8v).

        Previously spoofed a desktop Chrome UA with no way for 1001tracklists.com to identify us,
        contact us, or apply a phaze-specific policy. Read at construction time (not baked into a
        module-level constant) so tests can exercise a fresh `get_settings()` value.
        """
        return {
            "User-Agent": honest_user_agent_token(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://www.1001tracklists.com/",
        }

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
        """Serialize this scraper's outbound requests onto the shared whole-host schedule.

        Thin delegate to :func:`reserve_host_request_slot` -- see that function for the
        reservation semantics and for why the schedule is module-level rather than per-instance.
        """
        await reserve_host_request_slot()

    async def search(self, query: str) -> list[TracklistSearchResult]:
        """Search 1001Tracklists for tracklists matching query.

        Returns empty list on 403, HTTP error, or genuinely no results. Raises
        SearchParseFailureError (NOT swallowed to []) when the results page contains candidate
        rows that none of them could be parsed from -- that is a stale-selector defect, and must
        be distinguishable from a normal empty result (phaze-mk6y). Also raises
        DisallowedScrapeHostError if the client's followed redirects land the request off the
        1001Tracklists host allow-list (phaze-8ib8/phaze-k5zz).
        """
        cached = self._search_cache.get(query)
        if cached is not None:
            logger.debug("Search cache hit for: %s", query)
            return cached

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

        # phaze-8ib8: the client now follows redirects (see __init__), so a redirected search
        # POST could otherwise land on an off-allow-list host and have its (attacker-controlled)
        # HTML parsed as if it were a legitimate 1001Tracklists results page. Re-check the FINAL
        # response.url the same way scrape_tracklist() does, so this can't be used to smuggle a
        # search off-host through a malicious/compromised redirect chain (phaze-k5zz).
        if not self._is_allowed_url(str(response.url)):
            logger.warning("Refusing search result redirected to disallowed URL: %s", response.url)
            raise DisallowedScrapeHostError(str(response.url))

        try:
            results = self._parse_search_results(response.text)
        except SearchParseFailureError:
            # Already logged at ERROR inside _parse_search_results with the candidate count.
            # Propagate rather than collapsing to [] -- that collapse is exactly phaze-mk6y.
            raise
        except Exception:
            logger.warning("Failed to parse search results for: %s", query, exc_info=True)
            return []

        self._search_cache.set(query, results)
        return results

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
            # phaze-7zoh: bound to the column width -- see _EXTERNAL_ID_MAX_LEN.
            external_id = match.group(1)[: self._EXTERNAL_ID_MAX_LEN]

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
            artist_part, separator, rest = title.partition(self._SEARCH_LINK_TEXT_ARTIST_SEPARATOR)
            artist = artist_part.strip() if separator else None
            # phaze-fq9h.6: the remainder after " @ " is the event/venue/city text -- previously
            # discarded as `_rest`, which left the scorer with no event term at all (0.3 of the
            # weighting) and forced it to match on artist+date alone.
            event = rest.strip() or None if separator else None
            date = self._extract_row_date(item) or self._extract_date_from_href(href_str)

            results.append(
                TracklistSearchResult(
                    external_id=external_id,
                    title=title,
                    url=url,
                    artist=artist,
                    date=date,
                    event=event,
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
    def _extract_row_date(cls, item: Tag) -> str | None:
        """Extract a search row's displayed ``YYYY-MM-DD`` tracklist date (phaze-fq9h.6).

        The primary date source, with :meth:`_extract_date_from_href` as fallback. The cell holds
        an icon element followed by the bare date text, so the value is read from the cell's full
        text rather than a child selector -- that keeps it working if the icon markup changes,
        which is the kind of drift that killed the href pattern above.
        """
        cell = item.select_one(cls._SEARCH_ROW_DATE_SELECTOR)
        if cell is None:
            return None
        match = cls._ISO_DATE_IN_TEXT_PATTERN.search(cell.get_text(" ", strip=True))
        return match.group(0) if match else None

    @classmethod
    def _extract_date_from_href(cls, href: str) -> str | None:
        """Extract a trailing YYYY-MM-DD date from a tracklist href's slug, if present."""
        match = cls._HREF_DATE_PATTERN.search(href)
        if match is None:
            return None
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"

    async def close(self) -> None:
        """Close the httpx client if we own it."""
        if self._owns_client:
            await self._client.aclose()


async def reserve_host_request_slot() -> None:
    """Serialize outbound 1001Tracklists requests process-wide, honoring MIN_DELAY (phaze-wb1o).

    Reserves the next allowed request "slot" under a shared lock -- advancing the shared
    ``TracklistScraper._next_request_at`` timestamp and releasing the lock immediately, BEFORE
    sleeping -- so concurrent callers never race each other into the same slot, but the actual
    request/sleep still happens outside the lock (holding a lock across a 30s-timeout HTTP call
    would turn the scraper into a hard serial bottleneck rather than merely floor its rate). Each
    successive request's slot is at least MIN_DELAY-MAX_DELAY after the previous one; a process
    that falls behind (e.g. after a slow request) catches up from "now" rather than compounding a
    backlog of reservations into the future.

    phaze-fq9h.1: lifted out of ``TracklistScraper._rate_limit`` (which now delegates here) so the
    Patchright renderer in ``services/tracklist_render.py`` draws from the SAME schedule. The
    crawl-delay published in robots.txt is a WHOLE-HOST budget, so an httpx search and a browser
    navigation are both one request against one ceiling. Two independent limiters -- one per
    module -- would each honor 8s while the host saw 4s, which is the exact defect phaze-wb1o
    fixed for concurrent scraper instances, re-introduced across module boundaries. The state
    deliberately stays on ``TracklistScraper`` rather than moving to a module-level global: it is
    the documented reset point for test isolation and for the phaze-wb1o regression tests.
    """
    delay = random.uniform(TracklistScraper.MIN_DELAY, TracklistScraper.MAX_DELAY)  # noqa: S311  # nosec B311
    async with TracklistScraper._rate_limit_lock:
        now = time.monotonic()
        previous = TracklistScraper._next_request_at
        floor = now if previous is None else max(previous, now)
        slot = floor + delay
        # Mutate the CLASS attribute (never an instance attribute, which would be invisible to
        # every other short-lived TracklistScraper instance and to the renderer).
        TracklistScraper._next_request_at = slot

    wait = slot - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
