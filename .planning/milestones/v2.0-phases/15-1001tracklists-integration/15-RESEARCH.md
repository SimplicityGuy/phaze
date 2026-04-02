# Phase 15: 1001Tracklists Integration - Research

**Researched:** 2026-04-01
**Domain:** Web scraping, fuzzy string matching, async task scheduling, HTMX admin UI
**Confidence:** MEDIUM

## Summary

This phase integrates 1001tracklists.com data into the phaze pipeline: searching for matching tracklists by artist/event, scraping tracklist detail pages, storing versioned tracklist data in PostgreSQL, fuzzy-matching tracklists to files, and periodically refreshing stale entries. The existing project has well-established patterns for models, services, tasks, and HTMX-based UI that this phase extends.

The primary technical challenge is reliable scraping of 1001tracklists.com. The site has no official API -- all existing community scrapers use HTML parsing with BeautifulSoup. The site employs CAPTCHA protection (403 errors) and blocks non-browser user agents. Rate limiting and realistic headers are essential. The scraping layer must be built as a service abstraction so parsing logic can be updated when the site changes its HTML structure.

The fuzzy matching component is well-supported by rapidfuzz (C-extension, actively maintained, MIT licensed). The data model, task queue integration, and UI patterns all follow established project conventions with minimal novelty.

**Primary recommendation:** Build a `services/tracklist.py` service with three responsibilities (search, scrape, match), backed by `models/tracklist.py` (Tracklist + TracklistTrack), exposed via `routers/tracklists.py` with HTMX card layout following the duplicates page pattern. Use httpx (already installed) for async HTTP, beautifulsoup4 + lxml for HTML parsing, and rapidfuzz for string similarity scoring.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: One-to-one relationship between tracklists and files. Simple foreign key on tracklist pointing to file_id.
- D-02: Versioned snapshots for tracklist data. Each scrape creates a new version row. UI shows latest version; history is DB-level only.
- D-03: Store source URL and 1001tracklists external ID on every tracklist record.
- D-04: Store all available track fields: position, artist, title, label, timestamp/cue time, mashup/remix metadata.
- D-05: New `models/tracklist.py` file with Tracklist and TracklistTrack models.
- D-06: Both manual and automatic triggers. Tag extraction completion auto-enqueues search. Manual search button per file in UI.
- D-07: Fixed delay rate limiting (2-5 seconds between requests).
- D-08: httpx as HTTP client.
- D-09: Multiple search results ranked by relevance. User picks or dismisses.
- D-10: Periodic refresh targets stale (90+ days) and unresolved tracklists. Monthly minimum cadence with randomized jitter.
- D-11: Scraping failures logged, auto-retried on next refresh cycle. No UI error visibility.
- D-12: Match signals: artist (primary), event/venue (secondary), date proximity (tertiary).
- D-13: Numeric confidence score 0-100, weighted, displayed as percentage, sortable/filterable.
- D-14: Auto-link above 90% confidence with 10-second undo toast. Below 90% requires human approval.
- D-15: rapidfuzz library for string similarity.
- D-16: Parse v1.0 naming format filenames as primary matching signal. Fall back to tags.
- D-17: Multiple similar-confidence matches presented ranked. User picks; date is key differentiator.
- D-18: Dedicated Tracklists page plus badge/link on file cards in proposals/duplicates.
- D-19: Nav position after Duplicates, before Audit Log.
- D-20: Card-per-tracklist layout with inline expand (HTMX) for full track listing.
- D-21: Four actions per tracklist: Unlink, Re-scrape, View on 1001tracklists, Search for better match.
- D-22: Tabs/filter: Matched / Unmatched / All. Unmatched files with "Search" button.
- D-23: Auto-linked matches use 10-second undo toast.

### Claude's Discretion
- Alembic migration details for new tracklist/track tables
- httpx client wrapper implementation (session management, headers, retries)
- arq task functions for search, scrape, and refresh jobs
- Exact weight distribution for fuzzy matching signals
- HTMX partial structure for tracklist card expand/collapse
- Pagination approach on Tracklists page (follow existing proposals pattern)
- Tracklist badge design on file cards in other pages
- 1001tracklists endpoint details and response parsing

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| TL-01 | System searches 1001tracklists by artist and event to find matching tracklists | httpx async client with browser-like headers; search via `search/result.php` with form data; BeautifulSoup HTML parsing |
| TL-02 | Tracklist data (tracks, positions, timestamps) scraped and stored in PostgreSQL | Tracklist + TracklistTrack models with versioned snapshots; BeautifulSoup extraction of `.tlpItem` divs; Alembic migration |
| TL-03 | Scraped tracklists fuzzy-matched to files using artist/event/date similarity | rapidfuzz token_set_ratio for strings, date proximity scoring, weighted composite score 0-100 |
| TL-04 | Background job periodically re-checks tracklists with unresolved IDs (monthly minimum, randomized) | arq cron job with monthly schedule + random jitter; re-scrape unresolved + stale (90+ days) entries |
</phase_requirements>

## Standard Stack

### Core (New Dependencies)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| rapidfuzz | >=3.14.3 | Fuzzy string matching | C-extension, 10x faster than fuzzywuzzy, MIT, actively maintained. Token set ratio ideal for artist/event matching with word reordering. |
| beautifulsoup4 | >=4.14.3 | HTML parsing | Industry standard for scraping. All community 1001tracklists scrapers use it. Handles malformed HTML gracefully. |
| lxml | >=5.0 | HTML parser backend | Fastest parser for BeautifulSoup. C-based, handles large pages. |

### Already Available
| Library | Version | Purpose |
|---------|---------|---------|
| httpx | 0.28.1 | Async HTTP client (D-08, already installed as dev dep) |
| arq | >=0.27.0 | Task queue for search/scrape/refresh jobs |
| SQLAlchemy | >=2.0.48 | ORM for Tracklist/TracklistTrack models |
| Alembic | >=1.18.4 | Database migration for new tables |

### Installation
```bash
uv add rapidfuzz beautifulsoup4 lxml
```

**Note:** httpx is currently a dev dependency only (used for test client). It must be moved to production dependencies since this phase uses it for runtime HTTP scraping.

```bash
uv add httpx
```

## Architecture Patterns

### Recommended Project Structure (New Files)
```
src/phaze/
  models/
    tracklist.py           # Tracklist + TracklistTrack + TracklistVersion models
  services/
    tracklist_scraper.py   # HTTP client, search, scrape logic (isolated for testability)
    tracklist_matcher.py   # Fuzzy matching logic with rapidfuzz
  tasks/
    tracklist.py           # arq task functions: search, scrape, refresh
  routers/
    tracklists.py          # HTMX UI endpoints
  templates/
    tracklists/
      list.html            # Main tracklists page
      partials/
        tracklist_card.html     # Single tracklist card
        tracklist_list.html     # Card list (paginated)
        track_detail.html       # Expanded track listing
        stats_header.html       # Stats bar
        search_results.html     # Search results modal/panel
        toast.html              # Undo toast (reuse duplicates pattern)
        pagination.html         # Pagination controls
```

### Pattern 1: Scraper Service with httpx
**What:** Isolated async service class wrapping all 1001tracklists HTTP interactions
**When to use:** All search and scrape operations
**Example:**
```python
import asyncio
import random
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup


@dataclass
class TracklistSearchResult:
    """A single search result from 1001tracklists."""
    external_id: str
    title: str
    url: str
    artist: str | None
    date: str | None


class TracklistScraper:
    """Async scraper for 1001tracklists.com."""

    BASE_URL = "https://www.1001tracklists.com"
    SEARCH_URL = f"{BASE_URL}/search/result.php"
    MIN_DELAY = 2.0
    MAX_DELAY = 5.0

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.5",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    async def _rate_limit(self) -> None:
        await asyncio.sleep(random.uniform(self.MIN_DELAY, self.MAX_DELAY))

    async def search(self, query: str) -> list[TracklistSearchResult]:
        await self._rate_limit()
        # Search via GET with query params or POST form data
        # Parse HTML response with BeautifulSoup
        ...

    async def scrape_tracklist(self, url: str) -> dict:
        await self._rate_limit()
        response = await self._client.get(url)
        soup = BeautifulSoup(response.text, "lxml")
        # Extract tracks from .tlpItem divs
        ...
```

### Pattern 2: Versioned Tracklist Storage (D-02)
**What:** Each scrape creates a new TracklistVersion row; Tracklist points to latest version
**When to use:** Every scrape/re-scrape operation
**Example:**
```python
class Tracklist(TimestampMixin, Base):
    __tablename__ = "tracklists"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)
    match_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-100
    auto_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

class TracklistVersion(TimestampMixin, Base):
    __tablename__ = "tracklist_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tracklist_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklists.id"))
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(server_default=func.now())

class TracklistTrack(TimestampMixin, Base):
    __tablename__ = "tracklist_tracks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklist_versions.id"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_mashup: Mapped[bool] = mapped_column(Boolean, default=False)
    remix_info: Mapped[str | None] = mapped_column(Text, nullable=True)
```

### Pattern 3: Fuzzy Matching with Weighted Scores (D-12, D-13)
**What:** Composite confidence score from artist, event, date signals
**When to use:** After scraping a tracklist, match it to files
**Example:**
```python
from rapidfuzz import fuzz

def compute_match_confidence(
    tracklist_artist: str,
    tracklist_event: str | None,
    tracklist_date: date | None,
    file_artist: str | None,
    file_event: str | None,
    file_date: date | None,
) -> int:
    """Compute weighted match confidence 0-100."""
    score = 0.0
    weights_used = 0.0

    # Primary: artist similarity (weight 0.5)
    if tracklist_artist and file_artist:
        artist_sim = fuzz.token_set_ratio(tracklist_artist.lower(), file_artist.lower())
        score += artist_sim * 0.5
        weights_used += 0.5

    # Secondary: event/venue similarity (weight 0.3)
    if tracklist_event and file_event:
        event_sim = fuzz.token_set_ratio(tracklist_event.lower(), file_event.lower())
        score += event_sim * 0.3
        weights_used += 0.3

    # Tertiary: date proximity (weight 0.2)
    if tracklist_date and file_date:
        days_diff = abs((tracklist_date - file_date).days)
        if days_diff == 0:
            date_score = 100.0
        elif days_diff <= 3:
            date_score = 80.0
        elif days_diff <= 30:
            date_score = 50.0
        else:
            date_score = 0.0
        score += date_score * 0.2
        weights_used += 0.2

    if weights_used == 0:
        return 0
    return round(score / weights_used)
```

### Pattern 4: Filename Parsing (D-16)
**What:** Extract artist, event, date from v1.0 naming format
**When to use:** As primary matching signal before falling back to tags
**Example:**
```python
import re
from datetime import date

_LIVE_SET_PATTERN = re.compile(
    r"^(?P<artist>.+?) - Live @ (?P<event>.+?) (?P<year>\d{4})\.(?P<month>\d{2})\.(?P<day>\d{2})\.\w+$"
)

def parse_live_set_filename(filename: str) -> tuple[str, str, date] | None:
    """Parse v1.0 live set filename format. Returns (artist, event, date) or None."""
    match = _LIVE_SET_PATTERN.match(filename)
    if not match:
        return None
    return (
        match.group("artist"),
        match.group("event"),
        date(int(match.group("year")), int(match.group("month")), int(match.group("day"))),
    )
```

### Anti-Patterns to Avoid
- **Direct URL construction without validation:** Always validate external IDs and URLs before storing. Sanitize to prevent injection.
- **Blocking scrape in request handler:** Never scrape 1001tracklists synchronously in an HTTP endpoint. Always enqueue via arq.
- **Storing raw HTML:** Store extracted structured data, not raw HTML. The HTML structure will change.
- **Hardcoding CSS selectors inline:** Keep all selector strings as class constants in the scraper so they are easy to update.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| String similarity | Custom Levenshtein | rapidfuzz `fuzz.token_set_ratio` | C-extension, handles word reordering, 10x faster than Python impl |
| HTML parsing | Regex on HTML | BeautifulSoup + lxml | Handles malformed HTML, nested tags, encoding issues |
| HTTP client | urllib/aiohttp | httpx (already installed) | Async-native, follows redirects, timeout handling, cookie support |
| Rate limiting | Custom asyncio.sleep wrapper | Simple delay in scraper class | Fixed delay per D-07; no need for token bucket complexity |
| Periodic scheduling | Custom cron | arq `cron_jobs` | Built into arq worker, supports intervals with timezone |

**Key insight:** The scraping domain is the only novel complexity. Everything else (models, tasks, UI) follows established project patterns exactly.

## Common Pitfalls

### Pitfall 1: 1001tracklists CAPTCHA / 403 Blocking
**What goes wrong:** Site returns 403 or CAPTCHA page instead of content
**Why it happens:** Non-browser user agents detected, too-rapid requests, or IP reputation
**How to avoid:** Use realistic browser headers (User-Agent, Accept, Accept-Language), respect D-07 rate limit (2-5s delays), rotate user agent strings, detect 403 responses and back off
**Warning signs:** Empty parse results, "Error 403" in response title

### Pitfall 2: HTML Structure Changes Breaking Parser
**What goes wrong:** BeautifulSoup selectors return None after site update
**Why it happens:** 1001tracklists updates their HTML class names or structure
**How to avoid:** Isolate all CSS selectors as constants in the scraper class. Log warnings when expected elements are missing. Fail gracefully -- return partial data rather than crashing. Version the scraper logic.
**Warning signs:** Scrape jobs suddenly returning zero tracks, logging warnings about missing elements

### Pitfall 3: Fuzzy Match False Positives
**What goes wrong:** Wrong tracklist auto-linked to a file at >90% confidence
**Why it happens:** Different year, same artist + same festival name = high string similarity
**How to avoid:** Date proximity is critical as tiebreaker (D-12 tertiary signal). When artist+event match but date differs by >3 days, cap confidence below 90% regardless of string scores. Undo toast (D-14, D-23) provides safety net.
**Warning signs:** Multiple tracklists matching same file with similar scores

### Pitfall 4: httpx as Dev-Only Dependency
**What goes wrong:** Import error in production container
**Why it happens:** httpx is currently only in dev dependencies (used for test client). Phase 15 uses it at runtime.
**How to avoid:** Move httpx to production dependencies: `uv add httpx`
**Warning signs:** ModuleNotFoundError on first runtime scrape attempt

### Pitfall 5: Search Query Construction
**What goes wrong:** Search returns no results or irrelevant results
**Why it happens:** Query too specific (full filename) or too generic (just artist name)
**How to avoid:** Build search query from artist + event/venue. If no results, retry with artist only. Strip date from query (search by text, validate date in results).
**Warning signs:** Consistently zero search results for files that clearly have tracklists

## Code Examples

### arq Cron Job for Periodic Refresh (D-10)
```python
# In tasks/tracklist.py
from arq import cron

async def refresh_tracklists(ctx: dict) -> dict:
    """Re-scrape stale and unresolved tracklists."""
    async with ctx["async_session"]() as session:
        # Find unresolved (file_id IS NULL) and stale (updated_at < 90 days ago)
        stale_cutoff = datetime.utcnow() - timedelta(days=90)
        stmt = select(Tracklist).where(
            or_(
                Tracklist.file_id.is_(None),
                Tracklist.updated_at < stale_cutoff,
            )
        )
        tracklists = (await session.execute(stmt)).scalars().all()
        # Re-scrape each with rate limiting
        ...

# In tasks/worker.py - add to WorkerSettings
class WorkerSettings:
    cron_jobs = [
        cron(refresh_tracklists, month={1,2,3,4,5,6,7,8,9,10,11,12},
             day=None, hour=3, minute=None,  # Run monthly at 3am + jitter
             run_at_startup=False),
    ]
```

### HTMX Card Expand/Collapse (D-20)
```html
<!-- tracklist_card.html -->
<div id="tracklist-{{ tracklist.id }}" class="border rounded-lg p-4 mb-3">
    <div class="flex justify-between items-center">
        <div>
            <span class="font-semibold">{{ tracklist.artist }}</span>
            <span class="text-gray-500">@ {{ tracklist.event }}</span>
            <span class="text-gray-400 text-sm">{{ tracklist.date }}</span>
        </div>
        <div class="flex items-center gap-2">
            <span class="text-sm {% if tracklist.match_confidence >= 90 %}text-green-600{% elif tracklist.match_confidence >= 70 %}text-yellow-600{% else %}text-red-600{% endif %}">
                {{ tracklist.match_confidence }}%
            </span>
            <button hx-get="/tracklists/{{ tracklist.id }}/tracks"
                    hx-target="#tracks-{{ tracklist.id }}"
                    hx-swap="innerHTML"
                    class="text-blue-600 text-sm hover:underline">
                {{ tracklist.track_count }} tracks
            </button>
        </div>
    </div>
    <div id="tracks-{{ tracklist.id }}"></div>
    <!-- Actions: D-21 -->
    <div class="flex gap-2 mt-2 text-sm">
        <button hx-post="/tracklists/{{ tracklist.id }}/unlink" ...>Unlink</button>
        <button hx-post="/tracklists/{{ tracklist.id }}/rescrape" ...>Re-scrape</button>
        <a href="{{ tracklist.source_url }}" target="_blank">View Source</a>
        <button hx-get="/tracklists/{{ tracklist.id }}/search" ...>Find Better Match</button>
    </div>
</div>
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| fuzzywuzzy (python-Levenshtein) | rapidfuzz | 2020+ | 10x faster, no GPL dependency, drop-in API replacement |
| requests (sync) | httpx (async) | 2020+ | Fits async stack, native asyncio support |
| Selenium/headless browser | Direct HTTP + BeautifulSoup | Always for 1001tracklists | Site serves full HTML without JS rendering; no browser needed (confirmed in PROJECT.md) |

## Open Questions

1. **1001tracklists Search Endpoint Details**
   - What we know: Search page at `/search/result.php` with `#sBoxInput` text field, `#sBoxSel` type dropdown. CLIENT-SIDE JavaScript handles form submission via AJAX. PROJECT.md confirms "POST endpoints for search and detail pages."
   - What's unclear: Exact POST parameters, response format (HTML fragment vs JSON), whether search_selection values map to tracklist types
   - Recommendation: During implementation, use browser DevTools Network tab to capture exact request/response cycle. Build scraper defensively with parameter discovery. This is LOW confidence but manageable -- the scraper service is isolated and testable.

2. **Search Result Pagination**
   - What we know: Search returns multiple results
   - What's unclear: Whether results are paginated, how many results per page
   - Recommendation: Start with first page of results (likely sufficient for artist+event queries). Add pagination if needed.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL | Data storage | Yes (Docker) | 16+ | -- |
| Redis | arq task queue | Yes (Docker) | 7+ | -- |
| httpx | HTTP scraping | Yes (dev dep) | 0.28.1 | Must move to prod deps |
| rapidfuzz | Fuzzy matching | No (new) | -- | Install via uv add |
| beautifulsoup4 | HTML parsing | No (new) | -- | Install via uv add |
| lxml | BS4 parser backend | No (new) | -- | html.parser (slower fallback) |

**Missing dependencies with no fallback:**
- rapidfuzz, beautifulsoup4: Must be installed. Core functionality depends on them.

**Missing dependencies with fallback:**
- lxml: html.parser works as fallback but is slower. Prefer lxml.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x --no-header -q` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TL-01 | Search 1001tracklists by artist/event | unit (mock httpx) | `uv run pytest tests/test_services/test_tracklist_scraper.py -x` | No -- Wave 0 |
| TL-02 | Scrape and store tracklist data | unit (mock httpx + DB) | `uv run pytest tests/test_services/test_tracklist_scraper.py tests/test_models/test_tracklist.py -x` | No -- Wave 0 |
| TL-03 | Fuzzy match tracklists to files | unit (pure logic) | `uv run pytest tests/test_services/test_tracklist_matcher.py -x` | No -- Wave 0 |
| TL-04 | Periodic refresh of stale/unresolved | unit (mock arq ctx) | `uv run pytest tests/test_tasks/test_tracklist.py -x` | No -- Wave 0 |
| UI | Tracklists page renders, actions work | integration (httpx client) | `uv run pytest tests/test_routers/test_tracklists.py -x` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x --no-header -q`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_tracklist_scraper.py` -- covers TL-01, TL-02
- [ ] `tests/test_services/test_tracklist_matcher.py` -- covers TL-03
- [ ] `tests/test_tasks/test_tracklist.py` -- covers TL-04
- [ ] `tests/test_routers/test_tracklists.py` -- covers UI endpoints
- [ ] `tests/test_models/test_tracklist.py` -- covers model creation, relationships

## Sources

### Primary (HIGH confidence)
- [rapidfuzz PyPI](https://pypi.org/project/RapidFuzz/) -- version 3.14.3 verified, actively maintained
- [beautifulsoup4 PyPI](https://pypi.org/project/beautifulsoup4/) -- version 4.14.3 verified
- Project codebase: existing models, services, routers, templates, tasks patterns

### Secondary (MEDIUM confidence)
- [leandertolksdorf/1001-tracklists-api](https://github.com/leandertolksdorf/1001-tracklists-api) -- scraper.py uses requests + fake_headers + BeautifulSoup, tracklists.py parses `.tlpItem` divs for track data
- [jamescamagong/DJ-set-analysis](https://github.com/jamescamagong/DJ-set-analysis) -- confirms GET-based page fetching, artist pages at `/dj/{artist}/index.html`, tracklist pages contain meta tags with track URLs
- [Tel0sity/1001-tracklist-scraper](https://github.com/Tel0sity/1001-tracklist-scraper) -- BeautifulSoup-based Python scraper

### Tertiary (LOW confidence)
- 1001tracklists search endpoint parameters -- not fully documented in any source. PROJECT.md states POST endpoints confirmed but exact parameters need runtime discovery during implementation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- rapidfuzz, beautifulsoup4, httpx all well-proven, versions verified
- Architecture: HIGH -- follows established project patterns (models, services, tasks, routers, templates)
- Scraping specifics: MEDIUM -- community scrapers confirm approach works, but exact search endpoint params unverified
- Fuzzy matching: HIGH -- rapidfuzz API well-documented, matching logic is straightforward weighted scoring
- Pitfalls: MEDIUM -- based on community scraper experience and general scraping knowledge

**Research date:** 2026-04-01
**Valid until:** 2026-04-15 (scraping targets are fragile; HTML structure may change)
