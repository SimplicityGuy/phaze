# Phase 19: Discogs Cross-Service Linking - Research

**Researched:** 2026-04-02
**Domain:** Cross-service data linking, fuzzy matching, HTMX inline UI, search extension
**Confidence:** HIGH

## Summary

Phase 19 links phaze tracklist tracks to Discogs releases via the discogsography HTTP API. The work touches four layers: a new SQLAlchemy model (DiscogsLink), a new service (discogs_matcher) with httpx adapter + rapidfuzz scoring, a SAQ background task for batch matching, and UI extensions to both the tracklist detail page (inline candidates) and the search page (Discogs release entity type).

All required libraries are already installed (httpx, rapidfuzz, SAQ, SQLAlchemy). The discogsography `/api/search` endpoint accepts `q`, `types`, `genres`, `year_min`, `year_max`, `limit`, `offset` and returns JSON with `results` (each having `type`, `id`, `name`, `highlight`, `relevance`, `metadata`). The adapter only needs to query with `types=release` since we are matching tracks to releases. Fuzzy scoring reuses the established `rapidfuzz.fuzz.token_set_ratio` pattern from `tracklist_matcher.py`.

The UI follows established HTMX partial patterns: the tracklist detail page already supports expandable track rows; candidate rows render below each track. The search page already has entity type pills (blue=files, green=tracklists); adding purple=Discogs releases requires a third UNION ALL branch in `search_queries.py` and a new pill color in the results row template.

**Primary recommendation:** Follow the fingerprint service adapter pattern (httpx.AsyncClient with base_url + timeout), the SAQ task pattern from `tracklist.py` (one job per tracklist, iterate tracks), and the search UNION ALL pattern from `search_queries.py`. No new dependencies needed.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Per-tracklist "Match to Discogs" button on the tracklist detail page triggers matching. No batch-all-tracklists endpoint.
- **D-02:** Only tracks with both artist AND title populated are eligible for matching. Tracks with missing data are skipped.
- **D-03:** Matching runs as a SAQ background task (one job per tracklist). Each track fires an HTTP request to discogsography `/api/search`. Matches the existing fingerprint scan pattern.
- **D-04:** Candidates appear inline on the tracklist page -- expand each track row to show candidate Discogs matches below it. No separate dedicated linking page.
- **D-05:** Each candidate row shows: artist, title, label, year, confidence score. Compact table row format.
- **D-06:** Store top 3 highest-confidence matches per track. Can re-match for more.
- **D-07:** Actions per candidate: Accept (links track to release, auto-dismisses other candidates) or Dismiss (removes candidate). One accepted link per track.
- **D-08:** Extend the existing Phase 18 search page with a "Discogs releases" entity type. Reuse established search patterns.
- **D-09:** Search queries stored DiscogsLink data only -- no live calls to discogsography during search. Fast, consistent with human-in-the-loop model.
- **D-10:** Discogs results shown as purple pill badges in the unified results table (blue = files, green = tracklists, purple = Discogs releases). Same dense row format.
- **D-11:** "Bulk-link" button accepts the highest-confidence candidate for every track in the tracklist that has candidates. One-click action.
- **D-12:** Bulk-link requires matches to exist first -- user must trigger "Match to Discogs" before bulk-linking. Two-step flow: match -> review (optional) -> bulk-link.

### Claude's Discretion
- DiscogsLink model schema details (columns, indexes, relationships)
- Fuzzy matching strategy (rapidfuzz algorithm choice, scoring normalization)
- discogsography API adapter implementation (retry logic, timeout handling)
- SAQ task structure (job naming, progress reporting)
- HTMX partial structure for inline candidate display
- Search integration implementation details (FTS config for Discogs data)

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| DISC-01 | System fuzzy-matches live set tracks to Discogs releases via discogsography HTTP API | httpx adapter to `/api/search`, rapidfuzz `token_set_ratio` scoring, SAQ background task |
| DISC-02 | Candidate matches stored with confidence scores in DiscogsLink table, displayed in admin UI | DiscogsLink SQLAlchemy model, HTMX inline candidate partials on tracklist page |
| DISC-03 | User can query "find all sets containing track X" across phaze and discogsography data | Extend `search_queries.py` UNION ALL with DiscogsLink join, add purple pill entity type |
| DISC-04 | User can bulk-link an entire tracklist's tracks to Discogs releases in one action | Bulk-link endpoint accepts top candidate per track, single POST with HTMX swap |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Python 3.13 exclusively; `uv run` prefix for all commands
- Pre-commit hooks must pass; frozen SHAs on all hooks
- 85% code coverage minimum
- All functions must have type hints
- 150-char line length, double quotes, ruff + mypy strict
- SAQ for task queue (not arq, not Celery)
- Every feature gets its own git worktree and PR
- Never push directly to main

## Standard Stack

### Core (all already installed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| httpx | existing | Async HTTP client for discogsography API | Already used in fingerprint.py and tracklist_scraper.py. Established adapter pattern. |
| rapidfuzz | >=3.14.3 | Fuzzy string matching for confidence scoring | Already used in tracklist_matcher.py. `fuzz.token_set_ratio` handles word reordering. |
| SAQ | >=0.26.3 | Background task queue for batch matching | Already configured in worker.py. Follow scan_live_set/search_tracklist pattern. |
| SQLAlchemy | >=2.0.48 | DiscogsLink model with async session | Already configured. Follow existing model patterns (UUID pk, TimestampMixin, Base). |
| Alembic | >=1.18.4 | Database migration for new table | Already configured with async template. Autogenerate from model. |

### No New Dependencies
Zero new pip packages required (STATE.md decision). Everything needed is already installed.

## Architecture Patterns

### New Files
```
src/phaze/
  models/
    discogs_link.py          # DiscogsLink SQLAlchemy model
  services/
    discogs_matcher.py       # httpx adapter + rapidfuzz scoring + match orchestration
  tasks/
    discogs.py               # SAQ task: match_tracklist_to_discogs
  templates/
    tracklists/partials/
      discogs_candidates.html     # Inline candidate rows under each track
      discogs_match_button.html   # "Match to Discogs" button + progress
      discogs_bulk_link.html      # Bulk-link button partial
    search/partials/
      results_row.html            # Updated with purple Discogs pill
  routers/
    tracklists.py            # Extended with match/accept/dismiss/bulk-link endpoints
    search.py                # Extended to pass Discogs results
  services/
    search_queries.py        # Extended UNION ALL with Discogs branch
alembic/versions/
    xxx_add_discogs_links.py # Migration
```

### Pattern 1: DiscogsLink Model
**What:** New table storing candidate matches between TracklistTrack and Discogs releases.
**When to use:** Every match/accept/dismiss/bulk-link operation.
**Schema recommendation:**

```python
# Source: established project patterns from models/tracklist.py, models/base.py
class DiscogsLink(TimestampMixin, Base):
    __tablename__ = "discogs_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    track_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklist_tracks.id"), nullable=False)
    discogs_release_id: Mapped[str] = mapped_column(String(50), nullable=False)  # discogsography data_id
    discogs_artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="candidate")
    # status values: "candidate", "accepted", "dismissed"

    track: Mapped[TracklistTrack] = relationship("TracklistTrack", lazy="noload")

    __table_args__ = (
        Index("ix_discogs_links_track_id", "track_id"),
        Index("ix_discogs_links_status", "status"),
        Index("ix_discogs_links_discogs_release_id", "discogs_release_id"),
    )
```

**Key design decisions:**
- Store denormalized Discogs metadata (artist, title, label, year) so search works without live API calls (D-09).
- `status` column: "candidate" (fresh match), "accepted" (user approved), "dismissed" (user rejected).
- One accepted link per track enforced at application level (D-07): accepting auto-dismisses other candidates.
- Top 3 candidates stored per track (D-06): query with ORDER BY confidence DESC LIMIT 3 during match.
- FTS tsvector on `discogs_artist` + `discogs_title` for search integration (D-08).

### Pattern 2: Discogsography API Adapter
**What:** httpx.AsyncClient wrapper calling discogsography `/api/search`.
**When to use:** During SAQ match task execution.

```python
# Source: fingerprint.py AudfprintAdapter pattern
class DiscogsographyClient:
    def __init__(self, base_url: str = "http://discogsography:8000", timeout: float = 30.0) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def search_releases(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search discogsography for releases matching query."""
        resp = await self._client.get(
            "/api/search",
            params={"q": query, "types": "release", "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])

    async def close(self) -> None:
        await self._client.aclose()
```

**Discogsography response shape** (from reading `search_queries.py`):
```json
{
  "query": "deadmau5 strobe",
  "total": 42,
  "facets": { "type": {"release": 42}, "genre": {...}, "decade": {...} },
  "results": [
    {
      "type": "release",
      "id": "r12345",
      "name": "Strobe",
      "highlight": "<b>Strobe</b>",
      "relevance": 0.8542,
      "metadata": { "year": 2009, "genres": ["Electronic", "House"] }
    }
  ],
  "pagination": { "limit": 10, "offset": 0, "has_more": true }
}
```

### Pattern 3: Fuzzy Matching Confidence Score
**What:** Compute confidence score for each Discogs result against a track.
**Recommendation:** Use `rapidfuzz.fuzz.token_set_ratio` (same as tracklist_matcher.py).

```python
from rapidfuzz import fuzz

def compute_discogs_confidence(
    track_artist: str,
    track_title: str,
    discogs_name: str,
    discogs_relevance: float,
) -> float:
    """Score a Discogs result against a tracklist track.

    Weight: 0.6 artist+title similarity, 0.4 discogsography relevance.
    """
    # Combine track artist+title for matching against Discogs "name" field
    track_query = f"{track_artist} {track_title}".lower().strip()
    discogs_lower = discogs_name.lower().strip()

    string_sim = fuzz.token_set_ratio(track_query, discogs_lower) / 100.0
    # Blend local fuzzy score with server-side relevance
    confidence = (string_sim * 0.6 + discogs_relevance * 0.4) * 100.0
    return round(confidence, 1)
```

**Why this approach:**
- `token_set_ratio` handles word reordering and partial matches ("deadmau5 Strobe" vs "Strobe - deadmau5").
- Blending with discogsography's own relevance score (which uses PostgreSQL FTS ranking) gives a richer signal.
- Returns 0-100 scale consistent with existing `compute_match_confidence` in tracklist_matcher.py.

### Pattern 4: SAQ Task
**What:** Background job that matches all eligible tracks in a tracklist to Discogs.

```python
# Source: tasks/tracklist.py search_tracklist pattern
async def match_tracklist_to_discogs(ctx: dict[str, Any], *, tracklist_id: str) -> dict[str, Any]:
    """Match all eligible tracks in a tracklist to Discogs releases via discogsography."""
    async with ctx["async_session"]() as session:
        # Load tracklist + latest version tracks
        # Filter to tracks with both artist AND title (D-02)
        # For each eligible track:
        #   1. Build search query from artist + title
        #   2. Call discogsography /api/search
        #   3. Score results with rapidfuzz
        #   4. Store top 3 as DiscogsLink candidates (D-06)
        # Return summary: {tracklist_id, tracks_matched, tracks_skipped, candidates_created}
```

**Registration:** Add to `tasks/worker.py` settings["functions"] list and import.

### Pattern 5: HTMX Inline Candidates
**What:** Expandable candidate rows beneath each track on the tracklist detail page.
**Follow:** Existing track_detail.html + Alpine.js expand/collapse pattern.

The track row gets an expand button. Clicking it fetches candidates via `hx-get="/tracklists/{tracklist_id}/tracks/{track_id}/discogs"` and swaps content into a nested row.

Each candidate row shows: artist, title, label, year, confidence badge, Accept/Dismiss buttons.
- Accept: `hx-post="/tracklists/discogs-links/{link_id}/accept"` -- sets status="accepted", dismisses others, swaps row.
- Dismiss: `hx-delete="/tracklists/discogs-links/{link_id}"` -- sets status="dismissed", removes row.

### Pattern 6: Search Extension
**What:** Add "discogs_release" entity type to unified search.
**Follow:** Existing UNION ALL pattern in `search_queries.py`.

```python
# Third branch in the UNION ALL
discogs_tsvector = func.to_tsvector(
    "simple",
    func.concat_ws(" ", DiscogsLink.discogs_artist, DiscogsLink.discogs_title),
)
discogs_q = (
    select(
        cast(DiscogsLink.id, String).label("id"),
        literal_column("'discogs_release'").label("result_type"),
        DiscogsLink.discogs_title.label("title"),
        DiscogsLink.discogs_artist.label("artist"),
        literal_column("NULL").label("genre"),
        DiscogsLink.status.label("state"),
        cast(DiscogsLink.discogs_year, String).label("date"),
        func.ts_rank(discogs_tsvector, ts_query).label("rank"),
    )
    .where(discogs_tsvector.op("@@")(ts_query))
    .where(DiscogsLink.status == "accepted")  # Only show accepted links in search
)
```

**SearchResult dataclass:** `result_type` gains a third value `"discogs_release"`.
**Template:** Purple pill in `results_row.html`:
```html
{% elif result.result_type == "discogs_release" %}
    <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-purple-100 text-purple-700">Discogs</span>
```

### Anti-Patterns to Avoid
- **Live API calls during search:** D-09 explicitly forbids this. Store denormalized data in DiscogsLink.
- **Auto-accepting matches:** Human-in-the-loop is a core project value. All matches are candidates until user accepts.
- **Direct Discogs API calls:** Route everything through discogsography HTTP API (Out of Scope table in REQUIREMENTS.md).
- **Storing only Discogs IDs without metadata:** Search needs artist/title text locally for FTS.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| String similarity scoring | Levenshtein from scratch | `rapidfuzz.fuzz.token_set_ratio` | C-extension, handles word reordering, 10x faster |
| HTTP retry logic | Manual retry loops | httpx built-in + SAQ task retries | SAQ already provides exponential backoff via queue config |
| Full-text search | LIKE queries or app-level filtering | PostgreSQL `to_tsvector` + `ts_rank` | Already established in search_queries.py, GIN indexes |
| Inline expand/collapse UI | Custom JavaScript | Alpine.js `x-data`/`x-show` + HTMX `hx-get` | Already used throughout the tracklist page |

## Common Pitfalls

### Pitfall 1: Missing Artist/Title on Tracks
**What goes wrong:** Sending empty queries to discogsography returns garbage results.
**Why it happens:** Some tracklist tracks have NULL artist or title (e.g., ID-only tracks from 1001Tracklists).
**How to avoid:** D-02 mandates filtering: only match tracks where BOTH artist AND title are non-null and non-empty.
**Warning signs:** High candidate counts with low confidence scores across the board.

### Pitfall 2: N+1 API Calls to Discogsography
**What goes wrong:** A tracklist with 30 tracks fires 30 sequential HTTP requests, taking minutes.
**Why it happens:** Naive loop without any concurrency.
**How to avoid:** Use `asyncio.gather` with a bounded semaphore (e.g., 5 concurrent requests) to parallelize within a single SAQ task. Respect discogsography's 30/minute rate limit.
**Warning signs:** Match jobs timing out or taking >60s for typical tracklists.

### Pitfall 3: Duplicate DiscogsLink Rows on Re-match
**What goes wrong:** Triggering "Match to Discogs" twice creates duplicate candidates.
**Why it happens:** No upsert logic on (track_id, discogs_release_id).
**How to avoid:** Before inserting, delete existing "candidate" status links for the track. Or use an upsert pattern. Keep "accepted" links untouched.
**Warning signs:** UI showing duplicate candidate rows for the same release.

### Pitfall 4: Search FTS Without GIN Index
**What goes wrong:** Search on Discogs data does full table scan, slow at scale.
**Why it happens:** Forgetting to add a GIN index on the tsvector expression.
**How to avoid:** Add a GIN index in the Alembic migration: `CREATE INDEX ix_discogs_links_fts ON discogs_links USING GIN (to_tsvector('simple', coalesce(discogs_artist, '') || ' ' || coalesce(discogs_title, '')))`.
**Warning signs:** Search page load time increasing as more links are created.

### Pitfall 5: Bulk-Link Without Existing Candidates
**What goes wrong:** User clicks "Bulk-link" before running "Match to Discogs", nothing happens.
**Why it happens:** D-12 requires two-step flow but UI doesn't enforce it.
**How to avoid:** Disable or hide "Bulk-link" button when no candidates exist. Check candidate count before showing the button.
**Warning signs:** User confusion, empty bulk-link operations.

### Pitfall 6: Discogsography Service Unavailable
**What goes wrong:** SAQ task fails hard when discogsography container is down.
**Why it happens:** No graceful error handling on connection refused.
**How to avoid:** Catch `httpx.ConnectError` and `httpx.TimeoutException` in the adapter. Return empty results with a logged warning. SAQ retry handles transient failures.
**Warning signs:** All match jobs failing simultaneously.

## Code Examples

### Discogsography API Call (verified from source)
```python
# Source: discogsography/api/routers/search.py (read directly)
# GET /api/search?q=deadmau5+strobe&types=release&limit=10
# Response:
# {
#   "query": "deadmau5 strobe",
#   "total": N,
#   "results": [
#     {"type": "release", "id": "r12345", "name": "Strobe",
#      "highlight": "<b>Strobe</b>", "relevance": 0.85,
#      "metadata": {"year": 2009, "genres": ["Electronic"]}}
#   ],
#   "pagination": {"limit": 10, "offset": 0, "has_more": false}
# }
```

### SAQ Task Registration (verified from worker.py)
```python
# In tasks/worker.py, add to settings["functions"]:
from phaze.tasks.discogs import match_tracklist_to_discogs

settings = {
    "functions": [
        # ... existing functions ...
        match_tracklist_to_discogs,
    ],
}
```

### HTMX Expand Pattern (verified from tracklist router)
```python
# Router endpoint for fetching Discogs candidates
@router.get("/{tracklist_id}/tracks/{track_id}/discogs", response_class=HTMLResponse)
async def get_discogs_candidates(
    request: Request,
    tracklist_id: uuid.UUID,
    track_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return inline Discogs candidate rows for a track."""
    # Query DiscogsLink where track_id matches, status != "dismissed"
    # Order by confidence DESC
    # Render partial template
```

### Accept/Dismiss Pattern (verified from approve/reject in tracklists.py)
```python
@router.post("/discogs-links/{link_id}/accept", response_class=HTMLResponse)
async def accept_discogs_link(
    request: Request,
    link_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Accept a Discogs link candidate. Auto-dismisses other candidates for same track (D-07)."""
    # Set this link status = "accepted"
    # Set all other links for same track_id to status = "dismissed"
    # Return updated candidate partial
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x --tb=short` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DISC-01 | Fuzzy match tracks to Discogs via API | unit + integration | `uv run pytest tests/test_services/test_discogs_matcher.py -x` | No -- Wave 0 |
| DISC-01 | SAQ task runs matching for tracklist | unit | `uv run pytest tests/test_tasks/test_discogs.py -x` | No -- Wave 0 |
| DISC-02 | DiscogsLink model CRUD + migration | unit | `uv run pytest tests/test_models/test_discogs_link.py -x` | No -- Wave 0 |
| DISC-02 | Candidate display on tracklist page | integration | `uv run pytest tests/test_routers/test_tracklists.py -x -k discogs` | No -- Wave 0 |
| DISC-03 | Search includes Discogs release results | integration | `uv run pytest tests/test_routers/test_search.py -x -k discogs` | No -- Wave 0 |
| DISC-03 | search_queries includes Discogs UNION branch | unit | `uv run pytest tests/test_services/test_search_queries.py -x -k discogs` | No -- Wave 0 |
| DISC-04 | Bulk-link accepts top candidates | integration | `uv run pytest tests/test_routers/test_tracklists.py -x -k bulk_link` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x --tb=short`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_models/test_discogs_link.py` -- DiscogsLink model creation, constraints, indexes
- [ ] `tests/test_services/test_discogs_matcher.py` -- API adapter (mocked httpx), confidence scoring, match orchestration
- [ ] `tests/test_tasks/test_discogs.py` -- SAQ task execution with mocked adapter
- [ ] `tests/test_routers/test_tracklists.py` -- extend with Discogs candidate endpoints, accept/dismiss, bulk-link
- [ ] `tests/test_routers/test_search.py` -- extend with Discogs release search results
- [ ] `tests/test_services/test_search_queries.py` -- extend with Discogs UNION ALL branch

## Configuration

### New Settings (in config.py)
```python
# Discogsography service URL
discogsography_url: str = "http://discogsography:8000"
# Max concurrent requests to discogsography per match job
discogs_match_concurrency: int = 5
```

### Alembic Migration
Create migration for `discogs_links` table with:
- UUID primary key
- Foreign key to `tracklist_tracks.id`
- String columns for denormalized Discogs metadata
- Float confidence column
- String status column with server_default "candidate"
- GIN index on tsvector for FTS
- B-tree indexes on track_id, status, discogs_release_id

## Sources

### Primary (HIGH confidence)
- `src/phaze/services/fingerprint.py` -- httpx adapter pattern (AudfprintAdapter)
- `src/phaze/services/tracklist_matcher.py` -- rapidfuzz scoring pattern
- `src/phaze/tasks/tracklist.py` -- SAQ task pattern for external service calls
- `src/phaze/tasks/worker.py` -- SAQ worker configuration and task registration
- `src/phaze/services/search_queries.py` -- UNION ALL FTS pattern
- `src/phaze/routers/search.py` -- HTMX partial detection pattern
- `src/phaze/routers/tracklists.py` -- inline expand, approve/reject, HTMX swap patterns
- `src/phaze/models/tracklist.py` -- TracklistTrack model (match source)
- `src/phaze/models/base.py` -- Base, TimestampMixin patterns
- `/Users/Robert/Code/public/discogsography/api/routers/search.py` -- discogsography endpoint signature
- `/Users/Robert/Code/public/discogsography/api/queries/search_queries.py` -- response shape, ALL_TYPES constant

### Secondary (MEDIUM confidence)
- rapidfuzz PyPI -- version 3.14.3 verified installed locally

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries already installed and in use in this project
- Architecture: HIGH -- all patterns directly follow existing codebase patterns
- Pitfalls: HIGH -- based on direct code reading and established project patterns
- Discogs API shape: HIGH -- read directly from discogsography source code

**Research date:** 2026-04-02
**Valid until:** 2026-05-02 (stable -- no external API changes expected)
