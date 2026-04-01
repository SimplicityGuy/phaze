---
phase: 15-1001tracklists-integration
plan: 01
subsystem: database, api
tags: [sqlalchemy, beautifulsoup, rapidfuzz, httpx, scraping, fuzzy-matching]

requires:
  - phase: 01-foundation
    provides: Base, TimestampMixin, FileRecord model, Alembic migration chain
provides:
  - Tracklist, TracklistVersion, TracklistTrack ORM models
  - Alembic migration 006 for 3 tracklist tables
  - TracklistScraper service (search + scrape with rate limiting)
  - TracklistMatcher service (weighted confidence scoring + filename parser)
  - AUTO_LINK_THRESHOLD constant and should_auto_link helper
affects: [15-02, tracklist-ui, tracklist-tasks]

tech-stack:
  added: [rapidfuzz, beautifulsoup4, lxml, httpx (promoted to prod)]
  patterns: [dataclass-based scraper results, weighted multi-signal confidence scoring, CSS selector constants for maintainability]

key-files:
  created:
    - src/phaze/models/tracklist.py
    - src/phaze/services/tracklist_scraper.py
    - src/phaze/services/tracklist_matcher.py
    - alembic/versions/006_add_tracklist_tables.py
    - tests/test_models/test_tracklist.py
    - tests/test_services/test_tracklist_scraper.py
    - tests/test_services/test_tracklist_matcher.py
  modified:
    - pyproject.toml
    - src/phaze/models/__init__.py
    - tests/test_models/test_core_models.py

key-decisions:
  - "CSS selectors stored as class constants on TracklistScraper for easy updating when 1001tracklists.com changes layout"
  - "Dataclass-based return types (TracklistSearchResult, ScrapedTrack, ScrapedTracklist) rather than dicts for type safety"
  - "Date cap at 89 prevents false auto-links when artist+event match but date diverges >3 days (Pitfall 3)"

patterns-established:
  - "Weighted multi-signal confidence scoring: artist 0.5, event 0.3, date 0.2 with normalization by signals used"
  - "Rate limiting via asyncio.sleep with random uniform delay between MIN_DELAY and MAX_DELAY"
  - "test_models/ directory for model tests (moved from flat test_models.py)"

requirements-completed: [TL-01, TL-02, TL-03]

duration: 11min
completed: 2026-04-01
---

# Phase 15 Plan 01: 1001Tracklists Data Model and Services Summary

**Three-table tracklist data model with async scraper (rate-limited) and weighted fuzzy matcher using rapidfuzz token_set_ratio**

## Performance

- **Duration:** 11 min
- **Started:** 2026-04-01T19:30:41Z
- **Completed:** 2026-04-01T19:42:06Z
- **Tasks:** 2
- **Files modified:** 12

## Accomplishments
- Tracklist, TracklistVersion, TracklistTrack ORM models with full FK chain to FileRecord
- Alembic migration 006 creates 3 tables with indexes and constraints
- TracklistScraper with async search/scrape, browser-like headers, and 2-5s rate limiting
- TracklistMatcher with weighted confidence scoring, date proximity, and auto-link threshold at 90
- parse_live_set_filename extracts artist/event/date from v1.0 naming format
- 60 new tests, 427 total passing, mypy clean

## Task Commits

Each task was committed atomically:

1. **Task 1: Install dependencies, create Tracklist models, and Alembic migration** - `e23e96f` (feat)
2. **Task 2: Create scraper service and matcher service with unit tests** - `c127e87` (feat)

## Files Created/Modified
- `src/phaze/models/tracklist.py` - Tracklist, TracklistVersion, TracklistTrack ORM models
- `src/phaze/services/tracklist_scraper.py` - Async scraper with search/scrape and rate limiting
- `src/phaze/services/tracklist_matcher.py` - Weighted confidence scoring and filename parser
- `alembic/versions/006_add_tracklist_tables.py` - Migration for 3 new tables
- `pyproject.toml` - Added rapidfuzz, beautifulsoup4, lxml; promoted httpx to prod deps
- `src/phaze/models/__init__.py` - Registered 3 new models
- `tests/test_models/test_tracklist.py` - 35 model tests
- `tests/test_services/test_tracklist_scraper.py` - 7 scraper tests with mocked HTML
- `tests/test_services/test_tracklist_matcher.py` - 18 matcher tests (pure logic)
- `tests/test_models/test_core_models.py` - Updated table count from 7 to 10

## Decisions Made
- CSS selectors stored as class constants on TracklistScraper for easy updating when site layout changes
- Dataclass-based return types rather than dicts for type safety throughout scraper service
- Date cap at 89 prevents false auto-links when artist+event match but date diverges >3 days

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Moved test_models.py to test_models/ directory**
- **Found during:** Task 2 (full test suite run)
- **Issue:** Creating tests/test_models/ directory conflicted with existing tests/test_models.py -- pytest import collision
- **Fix:** Moved test_models.py to test_models/test_core_models.py, updated table count test from 7 to 10 tables
- **Files modified:** tests/test_models.py (deleted), tests/test_models/test_core_models.py (created)
- **Verification:** Full test suite (427 tests) passes
- **Committed in:** c127e87 (Task 2 commit)

**2. [Rule 1 - Bug] Adjusted default value test assertions for SQLAlchemy column defaults**
- **Found during:** Task 1 (TDD GREEN phase)
- **Issue:** SQLAlchemy `default=False` is an insert-time default, not applied to Python-side instantiation
- **Fix:** Changed tests to inspect column.default.arg instead of checking instance attribute value
- **Files modified:** tests/test_models/test_tracklist.py
- **Verification:** All 35 model tests pass
- **Committed in:** e23e96f (Task 1 commit)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 bug)
**Impact on plan:** Both auto-fixes necessary for test correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## Known Stubs
None - all models are fully wired, scraper parses real HTML structure, matcher computes real scores.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Models, scraper, and matcher ready for Plan 02 (arq task integration and UI)
- TracklistScraper.search() and scrape_tracklist() ready to be called from task queue workers
- compute_match_confidence() and should_auto_link() ready for linking workflow

---
*Phase: 15-1001tracklists-integration*
*Completed: 2026-04-01*
