---
phase: 19-discogs-cross-service-linking
plan: 01
subsystem: database, api
tags: [sqlalchemy, discogs, rapidfuzz, httpx, saq, fuzzy-matching]

# Dependency graph
requires:
  - phase: 15-tracklist-integration
    provides: TracklistTrack model, tracklist tables
  - phase: 18-unified-search
    provides: GIN index patterns, search infrastructure
provides:
  - DiscogsLink SQLAlchemy model with denormalized Discogs metadata
  - DiscogsographyClient HTTP adapter for discogsography /api/search
  - compute_discogs_confidence scoring function (rapidfuzz + relevance blending)
  - match_tracklist_to_discogs SAQ task with bounded concurrency
  - Alembic migration 010 for discogs_links table with GIN FTS index
affects: [19-02, 19-03, discogs-ui, discogs-api-endpoints]

# Tech tracking
tech-stack:
  added: []
  patterns: [discogsography-http-adapter, candidate-status-lifecycle, confidence-blending]

key-files:
  created:
    - src/phaze/models/discogs_link.py
    - src/phaze/services/discogs_matcher.py
    - src/phaze/tasks/discogs.py
    - alembic/versions/010_add_discogs_links.py
    - tests/test_models/test_discogs_link.py
    - tests/test_services/test_discogs_matcher.py
    - tests/test_tasks/test_discogs.py
  modified:
    - src/phaze/models/__init__.py
    - src/phaze/config.py
    - src/phaze/tasks/worker.py

key-decisions:
  - "Denormalized Discogs metadata in DiscogsLink avoids live API calls during search (D-09)"
  - "Top 3 candidates enforced at query time not schema level (D-06)"
  - "Confidence blending: 0.6 token_set_ratio + 0.4 API relevance score"
  - "Unicode dash separators for artist-title parsing from Discogs release names"

patterns-established:
  - "Discogsography HTTP adapter: same pattern as AudfprintAdapter/PanakoAdapter"
  - "Candidate/accepted status lifecycle: delete candidates on re-match, preserve accepted"
  - "Bounded concurrency via asyncio.Semaphore for external API calls"

requirements-completed: [DISC-01, DISC-02]

# Metrics
duration: 11min
completed: 2026-04-03
---

# Phase 19 Plan 01: Discogs Cross-Service Linking Data Layer Summary

**DiscogsLink model, discogsography HTTP adapter with rapidfuzz confidence scoring, and SAQ background task for batch matching tracklist tracks to Discogs releases**

## Performance

- **Duration:** 11 min
- **Started:** 2026-04-03T03:38:15Z
- **Completed:** 2026-04-03T03:49:30Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- DiscogsLink model with full denormalized Discogs metadata, 3 B-tree indexes and GIN FTS index
- DiscogsographyClient HTTP adapter with graceful ConnectError/TimeoutException handling
- Confidence scoring blending rapidfuzz token_set_ratio (60%) and discogsography relevance (40%)
- SAQ task processes all eligible tracks with bounded concurrency, stores top 3 candidates per track
- Re-matching preserves accepted links while replacing candidate links

## Task Commits

Each task was committed atomically:

1. **Task 1: DiscogsLink model, migration, and config settings** - `c20af2b` (feat)
2. **Task 2: Discogsography adapter, fuzzy matcher, SAQ task, worker registration** - `089fb60` (feat)

_Note: TDD tasks -- tests written first (RED), then implementation (GREEN), committed together._

## Files Created/Modified
- `src/phaze/models/discogs_link.py` - DiscogsLink SQLAlchemy model with TimestampMixin
- `src/phaze/services/discogs_matcher.py` - DiscogsographyClient, compute_discogs_confidence, match_track_to_discogs
- `src/phaze/tasks/discogs.py` - match_tracklist_to_discogs SAQ task
- `alembic/versions/010_add_discogs_links.py` - Migration creating discogs_links table with GIN FTS
- `src/phaze/models/__init__.py` - Added DiscogsLink export
- `src/phaze/config.py` - Added discogsography_url and discogs_match_concurrency settings
- `src/phaze/tasks/worker.py` - Registered SAQ task, added client lifecycle management
- `tests/test_models/test_discogs_link.py` - 11 model tests
- `tests/test_services/test_discogs_matcher.py` - 13 service tests
- `tests/test_tasks/test_discogs.py` - 2 task tests

## Decisions Made
- Denormalized Discogs metadata stored in DiscogsLink to avoid live API calls during search
- Top 3 candidates per track enforced at query time, not schema constraint
- Confidence formula: (token_set_ratio * 0.6 + relevance * 0.4) * 100, clamped 0-100
- Best-effort artist/title parsing from Discogs "name" field using dash separators (hyphen, en dash, em dash)
- Worker manages DiscogsographyClient lifecycle in startup/shutdown hooks

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- `.gitignore` `models/` pattern matches `src/phaze/models/` -- used `git add -f` for new model files (pre-existing issue, not new)

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- DiscogsLink model and matching infrastructure ready for Plan 02 (API endpoints) and Plan 03 (UI)
- Worker registered and client lifecycle managed -- task can be enqueued immediately
- 26 tests passing, mypy clean, ruff clean

---
*Phase: 19-discogs-cross-service-linking*
*Completed: 2026-04-03*
