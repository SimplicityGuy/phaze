# Phase 18: Unified Search - Research

**Researched:** 2026-04-02
**Domain:** PostgreSQL full-text search, SQLAlchemy async queries, HTMX search UI
**Confidence:** HIGH

## Summary

Phase 18 adds a unified search page to the admin UI that queries across files and tracklists using PostgreSQL full-text search (FTS) with GIN indexes. The existing codebase already has all the infrastructure needed: FastAPI router pattern, Jinja2 templates with HTMX partials, Alpine.js for collapsible panels, and Pagination dataclass. No new pip dependencies are required.

The core technical work is: (1) an Alembic migration to add `tsvector` columns and GIN indexes to the `files`, `metadata`, and `tracklists` tables, plus enable `pg_trgm` extension for ILIKE fallback on short queries; (2) a search query service that builds a UNION ALL query across file and tracklist entities; (3) a FastAPI router + templates following the established proposals page pattern.

**Primary recommendation:** Use stored `tsvector` columns with database triggers for automatic updates, `simple` text search config (not `english`) because music metadata contains artist names, remix info, and non-English content that should not be stemmed. Use offset-based pagination (consistent with all other pages). Target sub-100ms response at 200K files with GIN indexes.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Table rows (like Proposals page) -- dense, sortable, compact. Not cards.
- **D-02:** Color-coded pill badges to distinguish entity types -- blue for files, green for tracklists.
- **D-03:** Form submit (Enter or Search button) triggers search -- not live search-as-you-type. Standard HTMX swap pattern.
- **D-04:** Facet filters in a collapsible "Advanced filters" panel above results -- collapsed by default, Alpine.js toggle. Facets: artist, genre, date range, BPM range, file state.
- **D-05:** Search is the first (leftmost) tab in the nav bar -- primary entry point for the app.
- **D-06:** Before any query: search box + summary counts (e.g., "200K files, 45 tracklists") as a quick overview.
- **D-07:** No-results state: "No results found for [query]" with suggestion to broaden filters.

### Claude's Discretion
- Pagination approach (offset-based vs cursor) -- choose based on PostgreSQL FTS performance characteristics
- Exact FTS configuration (`simple` vs `english` text search config) -- research recommends `simple` for music metadata
- Which columns to index with GIN -- choose based on query patterns
- Result row detail level -- what columns to show in the table

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.

</user_constraints>

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SRCH-01 | User can search across files, tracklists, and metadata from a single search page | UNION ALL query pattern across files+metadata and tracklists; single search router |
| SRCH-02 | Search results are faceted by artist, genre, date range, BPM range, and file state | WHERE clause filters on joined metadata/analysis columns; collapsible filter panel |
| SRCH-03 | Results show unified cross-entity hits (files and tracklists together with type indicators) | Entity type column in UNION query; color-coded pill badges per D-02 |
| SRCH-04 | Search uses PostgreSQL full-text search with GIN indexes for sub-second response at 200K files | Stored tsvector columns + GIN indexes; `simple` config; Alembic migration |

</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.13** exclusively, **uv only** for all commands
- **Pre-commit hooks** must pass on all commits (frozen SHAs)
- **85% minimum code coverage** required
- **Type hints** on all functions, mypy strict mode (excluding tests)
- **Ruff** linting with 150-char line length
- **Double quotes** for strings
- **Every feature gets its own git worktree and PR**
- **Never push directly to main**
- All new routers must be registered in `src/phaze/main.py` via `app.include_router()`

## Standard Stack

No new dependencies required. Everything is already in pyproject.toml.

### Core (Already Installed)
| Library | Purpose | How Used in This Phase |
|---------|---------|----------------------|
| SQLAlchemy (async) | ORM + query builder | `func.to_tsvector`, `func.plainto_tsquery`, GIN index definitions |
| asyncpg | PostgreSQL async driver | Underlying driver for async session queries |
| Alembic | Database migrations | Migration to add tsvector columns, GIN indexes, triggers, pg_trgm extension |
| FastAPI | Router/endpoint | New `/search/` router with query parameters |
| Jinja2 | Templates | Search page and HTMX partials |
| HTMX 2.x (CDN) | Dynamic UI | Form submit swap, pagination swap |
| Alpine.js 3.x (CDN) | Client-side state | Collapsible filter panel toggle |
| Tailwind CSS (CDN) | Styling | Table, badges, filter panel |

### No New Dependencies
Zero new pip dependencies per STATE.md accumulated decision. PostgreSQL extensions (`pg_trgm`) are enabled via migration SQL, not pip packages.

## Architecture Patterns

### Recommended Project Structure
```
src/phaze/
  routers/
    search.py                  # New router
  services/
    search_queries.py          # Query logic + pagination
  templates/
    search/
      page.html                # Full page (extends base.html)
      partials/
        search_form.html       # Search box + filter panel
        results_content.html   # HTMX swap target (table + pagination)
        results_table.html     # Table rows
        results_row.html       # Single result row
        pagination.html        # Pagination controls
        summary_counts.html    # Initial state counts
```

### Pattern 1: Stored tsvector Column with Trigger

**What:** Add a `search_vector` column of type `TSVECTOR` to the `files` and `tracklists` tables. Use a database trigger to auto-populate it on INSERT/UPDATE.

**When to use:** When you have 200K+ rows and need sub-second FTS. Expression-based indexes (indexing `to_tsvector(col)` without a stored column) work but stored columns are faster at query time because the tsvector is pre-computed.

**Example (Alembic migration):**
```python
from alembic import op

def upgrade() -> None:
    # Enable pg_trgm for ILIKE fallback on short queries
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Add search_vector column to files
    op.execute("""
        ALTER TABLE files
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(original_filename, ''))
        ) STORED
    """)

    # Add search_vector column to metadata
    op.execute("""
        ALTER TABLE metadata
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple',
                coalesce(artist, '') || ' ' ||
                coalesce(title, '') || ' ' ||
                coalesce(album, '') || ' ' ||
                coalesce(genre, '')
            )
        ) STORED
    """)

    # Add search_vector column to tracklists
    op.execute("""
        ALTER TABLE tracklists
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple',
                coalesce(artist, '') || ' ' ||
                coalesce(event, '')
            )
        ) STORED
    """)

    # GIN indexes on search_vector columns
    op.execute("CREATE INDEX ix_files_search_vector ON files USING gin(search_vector)")
    op.execute("CREATE INDEX ix_metadata_search_vector ON metadata USING gin(search_vector)")
    op.execute("CREATE INDEX ix_tracklists_search_vector ON tracklists USING gin(search_vector)")

    # GIN trigram indexes for ILIKE partial matching
    op.execute("CREATE INDEX ix_files_filename_trgm ON files USING gin(original_filename gin_trgm_ops)")
    op.execute("CREATE INDEX ix_metadata_artist_trgm ON metadata USING gin(artist gin_trgm_ops)")
    op.execute("CREATE INDEX ix_tracklists_artist_trgm ON tracklists USING gin(artist gin_trgm_ops)")
```

**Note:** PostgreSQL 12+ supports `GENERATED ALWAYS AS ... STORED` for tsvector columns, eliminating the need for custom triggers. This is cleaner and automatically maintained.

### Pattern 2: UNION ALL Cross-Entity Query

**What:** A single query that returns both file and tracklist results with a `result_type` discriminator column, ordered by relevance.

**When to use:** When the search page must show mixed entity types in one table.

**Example (SQLAlchemy):**
```python
from sqlalchemy import func, literal_column, select, union_all

def build_search_query(query_text: str) -> Select:
    ts_query = func.plainto_tsquery("simple", query_text)

    file_q = (
        select(
            FileRecord.id,
            literal_column("'file'").label("result_type"),
            FileRecord.original_filename.label("title"),
            FileMetadata.artist,
            FileMetadata.genre,
            FileRecord.state,
            func.ts_rank(FileRecord.search_vector, ts_query).label("rank"),
        )
        .outerjoin(FileMetadata, FileRecord.id == FileMetadata.file_id)
        .where(
            FileRecord.search_vector.bool_op("@@")(ts_query)
            | FileMetadata.search_vector.bool_op("@@")(ts_query)
        )
    )

    tracklist_q = (
        select(
            Tracklist.id,
            literal_column("'tracklist'").label("result_type"),
            Tracklist.event.label("title"),
            Tracklist.artist,
            literal_column("NULL").label("genre"),
            Tracklist.status.label("state"),
            func.ts_rank(Tracklist.search_vector, ts_query).label("rank"),
        )
        .where(Tracklist.search_vector.bool_op("@@")(ts_query))
    )

    combined = union_all(file_q, tracklist_q).subquery()
    return select(combined).order_by(combined.c.rank.desc())
```

### Pattern 3: HTMX Form Submit (D-03 Compliance)

**What:** A form with `hx-get` that submits the search query and filters, swapping the results area.

**When to use:** Per D-03 -- form submit, not live search-as-you-type.

**Example:**
```html
<form hx-get="/search/"
      hx-target="#search-results"
      hx-swap="innerHTML"
      hx-push-url="true"
      hx-indicator="#search-spinner">
    <input type="text" name="q" value="{{ query }}"
           placeholder="Search files and tracklists..."
           class="w-full rounded-md border border-gray-300 px-4 py-2 text-sm">
    <button type="submit">Search</button>
</form>
<div id="search-results">
    <!-- Results swapped in here -->
</div>
```

### Pattern 4: Collapsible Filter Panel (D-04)

**What:** Alpine.js `x-data` with `x-show` for a filter panel that is collapsed by default.

**Example:**
```html
<div x-data="{ showFilters: false }">
    <button @click="showFilters = !showFilters"
            class="text-sm text-blue-600">
        <span x-text="showFilters ? 'Hide filters' : 'Advanced filters'"></span>
    </button>
    <div x-show="showFilters" x-transition class="mt-2 p-4 border rounded">
        <!-- Filter inputs: artist, genre, date range, BPM range, file state -->
    </div>
</div>
```

### Anti-Patterns to Avoid
- **Expression-based FTS in WHERE clause:** `WHERE to_tsvector('simple', col) @@ ...` without a stored column or expression index. PostgreSQL must recompute the tsvector for every row. Use stored generated columns instead.
- **Separate queries per entity type:** Running one query for files, another for tracklists, then merging in Python. This makes pagination inaccurate and doubles round trips. Use UNION ALL in SQL.
- **`to_tsquery` with raw user input:** `to_tsquery` requires proper tsquery syntax (operators like `&`, `|`). Use `plainto_tsquery` or `websearch_to_tsquery` for raw user text.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Full-text search | Custom LIKE/ILIKE queries | PostgreSQL FTS with `tsvector` + GIN | LIKE doesn't rank results, doesn't handle word boundaries, O(n) without trigram index |
| Pagination dataclass | New pagination class | Existing `Pagination` from `phaze.services.proposal_queries` | Already battle-tested, used by proposals, duplicates, tracklists |
| Query parameter parsing | Manual string parsing | FastAPI `Query()` parameters | Type-safe, auto-documented in OpenAPI |
| Result ranking | Custom scoring algorithm | `ts_rank()` built into PostgreSQL | Handles term frequency, position weighting, normalization |

**Key insight:** PostgreSQL FTS at 200K rows is trivially fast with GIN indexes. No external search engine (Elasticsearch, Meilisearch) is needed or wanted (per REQUIREMENTS.md out-of-scope).

## Discretion Recommendations

### Pagination: Offset-based
**Recommendation:** Use offset-based pagination (LIMIT/OFFSET), same as every other page in the app.

**Rationale:** Cursor-based pagination is theoretically better for large datasets, but: (1) offset pagination is consistent with the existing Pagination dataclass and all other UI pages; (2) at 200K files, OFFSET is fast enough with proper indexes; (3) UNION ALL queries make cursor-based pagination complex (cursor must encode both entity types). The Pagination dataclass from `proposal_queries.py` should be reused directly.

### FTS Configuration: `simple`
**Recommendation:** Use `simple` text search config, not `english`.

**Rationale:** Music metadata contains artist names (proper nouns), remix info, event names, and non-English content. The `english` config stems words (`refusing` -> `refus`) and removes stop words (`the`, `and`). This would: (1) mangle artist names like "The Prodigy" by removing "The"; (2) stem "Remix" and "Remixed" to the same root when they mean different things in music context; (3) fail on non-English artist names. The `simple` config lowercases and tokenizes but does not stem, which is correct for music search.

### GIN Index Columns
**Recommendation:** Index these columns:

| Table | Columns in tsvector | Why |
|-------|-------------------|-----|
| `files` | `original_filename` | Primary search target -- filename contains artist, track title, event info |
| `metadata` | `artist`, `title`, `album`, `genre` | Structured metadata fields users will search |
| `tracklists` | `artist`, `event` | Tracklist identification fields |

Also add `pg_trgm` GIN indexes on `files.original_filename`, `metadata.artist`, and `tracklists.artist` for ILIKE partial matching fallback when FTS returns no results for very short queries (1-2 chars).

### Result Row Columns
**Recommendation:** Show these columns in the search results table:

| Column | Source | Notes |
|--------|--------|-------|
| Type | Computed (`file`/`tracklist`) | Color-coded pill badge per D-02 |
| Name/Title | `original_filename` or `event` | Primary identifier |
| Artist | `metadata.artist` or `tracklist.artist` | Key search field |
| Genre | `metadata.genre` or null | File metadata only |
| State | `files.state` or `tracklist.status` | Current pipeline/review state |
| Date | `files.created_at` or `tracklist.date` | When discovered/performed |

## Common Pitfalls

### Pitfall 1: Empty tsquery Crashes
**What goes wrong:** `plainto_tsquery('simple', '')` returns an empty tsquery. Using it in `@@` produces no results but is valid SQL. However, `to_tsquery('simple', '')` raises an error.
**Why it happens:** Empty search string with no validation.
**How to avoid:** Always validate query is non-empty before building FTS query. Return the initial state (summary counts) for empty queries.
**Warning signs:** 500 errors on empty form submit.

### Pitfall 2: UNION Column Mismatch
**What goes wrong:** SQLAlchemy `union_all()` requires all SELECT statements to have the same number of columns with compatible types.
**Why it happens:** File query has different columns than tracklist query.
**How to avoid:** Use `literal_column("NULL")` or `cast(None, Text)` for columns that don't exist in one entity. Label all columns explicitly.
**Warning signs:** SQLAlchemy compilation errors or PostgreSQL type mismatch errors.

### Pitfall 3: N+1 Queries on Result Rendering
**What goes wrong:** Template iterates over results and loads related objects per-row.
**Why it happens:** Lazy loading on relationships.
**How to avoid:** The UNION query should select all needed columns directly. Don't load full ORM objects from UNION results -- use raw rows or named tuples.
**Warning signs:** Slow page load, many SQL queries in logs.

### Pitfall 4: Filter Parameters Lost on Pagination
**What goes wrong:** Clicking "Next page" loses the search query and filter state.
**Why it happens:** Pagination links don't include all query parameters.
**How to avoid:** Pass all filter state (q, artist, genre, date_from, date_to, bpm_min, bpm_max, file_state) through pagination links. The existing pagination partial pattern does this correctly -- follow it.
**Warning signs:** Results change when navigating pages.

### Pitfall 5: Generated Column on Existing Table Migration
**What goes wrong:** Adding a `GENERATED ALWAYS AS ... STORED` column to a table with 200K rows locks the table during ALTER.
**Why it happens:** PostgreSQL must compute the value for every existing row.
**How to avoid:** For development, this is fine (data volume is small in dev). In production with real 200K rows, the ALTER will take seconds, not minutes -- tsvector computation is fast. If concerned, add the column as nullable first, backfill, then add the GENERATED clause. For this project, direct ALTER is acceptable.
**Warning signs:** Migration timeout in production.

## Code Examples

### Search Query Service Pattern
```python
# src/phaze/services/search_queries.py
from dataclasses import dataclass
from sqlalchemy import func, literal_column, select, union_all, Text, cast
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.proposal_queries import Pagination


@dataclass
class SearchResult:
    """A single search result from the unified query."""
    id: str
    result_type: str  # "file" or "tracklist"
    title: str
    artist: str | None
    genre: str | None
    state: str
    rank: float


async def search(
    session: AsyncSession,
    query: str,
    *,
    artist: str | None = None,
    genre: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    file_state: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[SearchResult], Pagination]:
    """Execute unified search across files and tracklists."""
    ts_query = func.plainto_tsquery("simple", query)
    # ... build UNION ALL query with filters ...
```

### Router Pattern
```python
# src/phaze/routers/search.py
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/search", tags=["search"])

@router.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str | None = Query(None),
    artist: str | None = Query(None),
    genre: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    bpm_min: float | None = Query(None),
    bpm_max: float | None = Query(None),
    file_state: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # If no query, show initial state with counts
    if not q:
        counts = await get_summary_counts(session)
        return templates.TemplateResponse(...)

    results, pagination = await search(session, q, ...)
    # HTMX partial vs full page
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            name="search/partials/results_content.html", ...
        )
    return templates.TemplateResponse(name="search/page.html", ...)
```

### Nav Bar Update (D-05)
```html
<!-- In base.html, add Search as FIRST tab -->
<a href="/search/"
   class="text-sm font-semibold px-3 py-2 {% if current_page == 'search' %}text-blue-600{% else %}text-gray-600 hover:text-gray-900{% endif %}">
    Search
</a>
<!-- Then existing tabs: Pipeline, Proposals, etc. -->
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_routers/test_search.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SRCH-01 | Search page returns file + tracklist results | integration | `uv run pytest tests/test_routers/test_search.py::test_search_returns_files_and_tracklists -x` | No -- Wave 0 |
| SRCH-01 | Search router registered at /search/ | smoke | `uv run pytest tests/test_routers/test_search.py::test_search_page_loads -x` | No -- Wave 0 |
| SRCH-02 | Artist filter narrows results | integration | `uv run pytest tests/test_routers/test_search.py::test_artist_filter -x` | No -- Wave 0 |
| SRCH-02 | BPM range filter narrows results | integration | `uv run pytest tests/test_routers/test_search.py::test_bpm_range_filter -x` | No -- Wave 0 |
| SRCH-02 | File state filter narrows results | integration | `uv run pytest tests/test_routers/test_search.py::test_file_state_filter -x` | No -- Wave 0 |
| SRCH-03 | Results include result_type discriminator | unit | `uv run pytest tests/test_services/test_search_queries.py::test_union_has_result_type -x` | No -- Wave 0 |
| SRCH-04 | GIN index migration applies cleanly | integration | `uv run pytest tests/test_services/test_search_queries.py::test_fts_query_uses_index -x` | No -- Wave 0 |
| SRCH-04 | Search returns results sub-second on test data | integration | `uv run pytest tests/test_routers/test_search.py::test_search_performance -x` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_routers/test_search.py tests/test_services/test_search_queries.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_routers/test_search.py` -- covers SRCH-01, SRCH-02, SRCH-04
- [ ] `tests/test_services/test_search_queries.py` -- covers SRCH-03, SRCH-04
- [ ] Test fixtures: file records + metadata + tracklists with known searchable content

## Sources

### Primary (HIGH confidence)
- [SQLAlchemy PostgreSQL dialect docs](https://docs.sqlalchemy.org/en/21/dialects/postgresql.html) -- TSVECTOR type, GIN index creation, `match()` operator, `func.to_tsvector`
- [PostgreSQL 18 FTS documentation](https://www.postgresql.org/docs/current/textsearch-tables.html) -- tsvector columns, GIN indexes, text search configs
- [PostgreSQL pg_trgm documentation](https://www.postgresql.org/docs/current/pgtrgm.html) -- trigram matching, GIN operator class

### Secondary (MEDIUM confidence)
- [Multi-table FTS patterns](https://thoughtbot.com/blog/implementing-multi-table-full-text-search-with-postgres) -- UNION approach for cross-entity search, verified against PostgreSQL docs
- [PostgreSQL FTS at 200M rows case study](https://medium.com/@yogeshsherawat/using-full-text-search-fts-in-postgresql-for-over-200-million-rows-a-case-study-e0a347df14d0) -- confirms GIN index performance at scale far exceeding our 200K target
- [PostgreSQL text search configuration docs](https://www.postgresql.org/docs/current/textsearch-configuration.html) -- `simple` vs `english` config differences

### Tertiary (LOW confidence)
- None -- all findings verified against official documentation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- zero new dependencies, all patterns verified in existing codebase
- Architecture: HIGH -- UNION ALL + stored tsvector is well-documented PostgreSQL pattern; UI follows established project conventions
- Pitfalls: HIGH -- all pitfalls derived from official PostgreSQL docs and verified project patterns

**Research date:** 2026-04-02
**Valid until:** 2026-05-02 (stable domain, no fast-moving dependencies)
