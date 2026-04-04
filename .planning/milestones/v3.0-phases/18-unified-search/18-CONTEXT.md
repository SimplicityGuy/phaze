# Phase 18: Unified Search - Context

**Gathered:** 2026-04-02
**Status:** Ready for planning

<domain>
## Phase Boundary

A single search page in the admin UI that queries across files, tracklists, and metadata using PostgreSQL full-text search with GIN indexes. Users type a query, see cross-entity results in a dense table, and narrow results with faceted filters. This is read-only over existing data — no new data models, no writes.

</domain>

<decisions>
## Implementation Decisions

### Results Layout
- **D-01:** Table rows (like Proposals page) — dense, sortable, compact. Not cards.
- **D-02:** Color-coded pill badges to distinguish entity types — blue for files, green for tracklists.

### Search Interaction
- **D-03:** Form submit (Enter or Search button) triggers search — not live search-as-you-type. Standard HTMX swap pattern.
- **D-04:** Facet filters in a collapsible "Advanced filters" panel above results — collapsed by default, Alpine.js toggle. Facets: artist, genre, date range, BPM range, file state.

### Navigation
- **D-05:** Search is the first (leftmost) tab in the nav bar — primary entry point for the app.

### Initial State
- **D-06:** Before any query: search box + summary counts (e.g., "200K files, 45 tracklists") as a quick overview.
- **D-07:** No-results state: "No results found for [query]" with suggestion to broaden filters.

### Claude's Discretion
- Pagination approach (offset-based vs cursor) — choose based on PostgreSQL FTS performance characteristics
- Exact FTS configuration (`simple` vs `english` text search config) — research recommends `simple` for music metadata
- Which columns to index with GIN — choose based on query patterns
- Result row detail level — what columns to show in the table

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Existing UI Patterns
- `src/phaze/templates/base.html` — Nav bar structure, must add Search as first tab
- `src/phaze/templates/proposals/list.html` — Table row pattern to follow for search results
- `src/phaze/templates/proposals/partials/filter_tabs.html` — Existing filter pattern (search uses collapsible panel instead)

### Research
- `.planning/research/SUMMARY.md` — Stack recommendations (PostgreSQL FTS, GIN indexes, no external search engine)
- `.planning/research/STACK.md` — Detailed search implementation guidance
- `.planning/research/ARCHITECTURE.md` — Integration patterns and build order

### Requirements
- `.planning/REQUIREMENTS.md` — SRCH-01 through SRCH-04

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `base.html` nav bar — add Search tab as first item
- `proposals/list.html` table pattern — reuse for search results table
- `filter_tabs.html` partial — reference for filter interaction (search uses collapsible panel instead)
- `proposals/partials/pagination.html` — reusable pagination partial
- Alpine.js toggle pattern — already used for tracklists scan panel, reuse for collapsible filters

### Established Patterns
- FastAPI router + Jinja2 templates + HTMX partials for all pages
- `current_page` context variable for nav highlighting
- HTMX `hx-get` / `hx-target` / `hx-swap` for dynamic content updates
- Alpine.js `x-data` for client-side state (filter toggles, form state)

### Integration Points
- New router: `src/phaze/routers/search.py`
- New templates: `src/phaze/templates/search/`
- Nav update: `src/phaze/templates/base.html` — add Search tab first
- Alembic migration: enable `pg_trgm` extension, create GIN indexes on relevant columns
- Models queried: FileRecord, FileMetadata, Tracklist, TracklistTrack

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches. Follow existing Proposals page density and interaction patterns.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 18-unified-search*
*Context gathered: 2026-04-02*
