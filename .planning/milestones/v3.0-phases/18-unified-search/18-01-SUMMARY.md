---
phase: 18-unified-search
plan: 01
subsystem: database
tags: [postgresql, fts, tsvector, gin, pg_trgm, sqlalchemy, union-all]

requires:
  - phase: 15-tracklists
    provides: Tracklist model and table
  - phase: 12-metadata
    provides: FileMetadata model and table

provides:
  - Alembic migration 009 for tsvector GENERATED columns and GIN indexes
  - Search query service with cross-entity UNION ALL, facet filters, pagination
  - SearchResult dataclass with result_type discriminator

affects: [18-02-search-ui]

tech-stack:
  added: [pg_trgm]
  patterns: [expression-based-tsvector, union-all-cross-entity, plainto_tsquery]

key-files:
  created:
    - alembic/versions/009_add_search_vectors.py
    - src/phaze/services/search_queries.py
    - tests/test_services/test_search_queries.py

key-decisions:
  - "Expression-based tsvector in queries (not stored column references) for test compatibility"
  - "plainto_tsquery('simple', ...) for safe user input and language-agnostic search"
  - "file_state filter excludes tracklists entirely when active"

patterns-established:
  - "Expression-based FTS: use func.to_tsvector inline rather than referencing GENERATED columns for portability"
  - "Cross-entity search: UNION ALL with literal_column result_type discriminator"

requirements-completed: [SRCH-01, SRCH-03, SRCH-04]

duration: 4min
completed: 2026-04-02
---

# Phase 18 Plan 01: Search Data Layer Summary

**PostgreSQL full-text search with tsvector GENERATED columns, GIN indexes, and cross-entity UNION ALL search service returning ranked, paginated results from files and tracklists**

## Performance

- **Duration:** 4 min
- **Started:** 2026-04-02T23:36:16Z
- **Completed:** 2026-04-02T23:40:30Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments

- Alembic migration 009 adds GENERATED STORED tsvector columns and 6 GIN indexes (3 FTS + 3 trigram) to files, metadata, and tracklists tables
- Search query service returns ranked, paginated SearchResult objects via UNION ALL with result_type discriminator
- Full facet filtering: artist, genre, BPM range, file state, date range
- 14 tests passing covering all search behaviors, ruff clean, mypy clean

## Task Commits

Each task was committed atomically:

1. **Task 1: Alembic migration for FTS infrastructure** - `1e8e950` (feat)
2. **Task 2: Search query service (RED)** - `335a303` (test)
3. **Task 2: Search query service (GREEN)** - `03478f1` (feat)

## Files Created/Modified

- `alembic/versions/009_add_search_vectors.py` - Migration adding tsvector GENERATED columns, GIN indexes, pg_trgm extension
- `src/phaze/services/search_queries.py` - Unified search service with cross-entity FTS, facet filters, pagination
- `tests/test_services/test_search_queries.py` - 14 tests covering search, filters, pagination, summary counts

## Decisions Made

- Used expression-based tsvector (func.to_tsvector inline) rather than referencing stored search_vector columns -- this works identically with or without GENERATED columns, enabling tests to run without migration
- Used plainto_tsquery with 'simple' config for language-agnostic search and safe user input handling
- When file_state filter is active, tracklist results are excluded entirely (tracklists have status, not state)
- concat_ws used for combining multiple text columns into single tsvector expression

## Deviations from Plan

None -- plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None -- no external service configuration required.

## Known Stubs

None -- all data paths are fully wired.

## Next Phase Readiness

- Search service ready for Plan 02 to wire up the HTMX search UI
- SearchResult dataclass, search(), and get_summary_counts() are the public API for the UI layer

---
*Phase: 18-unified-search*
*Completed: 2026-04-02*
