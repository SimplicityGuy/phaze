---
phase: 14-duplicate-resolution-ui
plan: 01
subsystem: database, api
tags: [sqlalchemy, dedup, scoring, duplicate-resolution, postgres]

requires:
  - phase: 03-duplicate-detection
    provides: SHA256 grouping, find_duplicate_groups, count_duplicate_groups
provides:
  - DUPLICATE_RESOLVED enum state on FileState
  - file_metadata relationship on FileRecord
  - tag_completeness scoring function
  - score_group auto-selection (bitrate > tags > path)
  - find_duplicate_groups_with_metadata enriched query
  - get_duplicate_stats aggregation
  - resolve_group and undo_resolve state transitions
affects: [14-02-duplicate-resolution-ui, duplicate-router, templates]

tech-stack:
  added: []
  patterns: [outerjoin-metadata-enrichment, rationale-differentiation-scoring]

key-files:
  created: []
  modified:
    - src/phaze/models/file.py
    - src/phaze/services/dedup.py
    - tests/test_services/test_dedup.py

key-decisions:
  - "Used file_metadata instead of metadata for relationship name (metadata is reserved by SQLAlchemy DeclarativeBase)"
  - "Scoring rationale reflects the actual differentiator between winner and runner-up, not just the winner's best attribute"

patterns-established:
  - "Outerjoin pattern: join FileMetadata via outerjoin in service queries rather than selectinload, since dict-based grouping is used"
  - "Rationale differentiation: compare winner vs runner-up to determine which criterion actually distinguished the canonical file"

requirements-completed: [DEDUP-04, DEDUP-01]

duration: 10min
completed: 2026-04-01
---

# Phase 14 Plan 01: Dedup Backend Foundation Summary

**Duplicate resolution backend with auto-selection scoring (bitrate > tags > path), metadata-enriched queries, resolve/undo state machine, and stats aggregation**

## Performance

- **Duration:** 10 min
- **Started:** 2026-04-01T01:49:47Z
- **Completed:** 2026-04-01T02:00:12Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 3

## Accomplishments
- Extended FileState enum with DUPLICATE_RESOLVED and added file_metadata relationship on FileRecord
- Implemented score_group with three-tier ranking (bitrate > tag completeness > shortest path) and human-readable rationale
- Added find_duplicate_groups_with_metadata using outerjoin to include all tag fields
- Added get_duplicate_stats returning groups, total_files, recoverable_bytes
- Added resolve_group and undo_resolve for state transitions with undo tracking
- Filtered DUPLICATE_RESOLVED files from all duplicate group queries
- All 17 tests passing (12 new + 5 existing), full suite 357 passing

## Task Commits

Each task was committed atomically:

1. **Task 1 (RED): Failing tests** - `a8d945c` (test)
2. **Task 1 (GREEN): Implementation** - `3d42e22` (feat)

## Files Created/Modified
- `src/phaze/models/file.py` - Added DUPLICATE_RESOLVED state, file_metadata relationship
- `src/phaze/services/dedup.py` - Added 6 new functions: tag_completeness, score_group, find_duplicate_groups_with_metadata, get_duplicate_stats, resolve_group, undo_resolve; updated existing queries to exclude resolved files
- `tests/test_services/test_dedup.py` - Added 12 new tests covering scoring, tag completeness, exclusion, metadata enrichment, stats, resolve/undo

## Decisions Made
- Used `file_metadata` instead of `metadata` for relationship name because `metadata` is reserved by SQLAlchemy's DeclarativeBase
- Scoring rationale reflects the actual differentiator (compares winner vs runner-up) rather than just the winner's best attribute -- ensures "most complete tags" appears when bitrate is tied

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] SQLAlchemy reserved attribute name `metadata`**
- **Found during:** Task 1 (GREEN phase)
- **Issue:** `metadata` is reserved by SQLAlchemy DeclarativeBase, causing `InvalidRequestError`
- **Fix:** Renamed relationship from `metadata` to `file_metadata`
- **Files modified:** src/phaze/models/file.py
- **Verification:** All tests pass, mypy clean
- **Committed in:** 3d42e22

**2. [Rule 1 - Bug] Nested aggregate in get_duplicate_stats**
- **Found during:** Task 1 (GREEN phase)
- **Issue:** `SUM(MAX(...))` not allowed in PostgreSQL -- "aggregate function calls cannot be nested"
- **Fix:** Used subquery to compute MAX per group first, then SUM over the subquery
- **Files modified:** src/phaze/services/dedup.py
- **Verification:** test_get_duplicate_stats passes
- **Committed in:** 3d42e22

**3. [Rule 1 - Bug] Scoring rationale always showing "highest bitrate" on tiebreak**
- **Found during:** Task 1 (GREEN phase)
- **Issue:** When all files had the same bitrate, rationale still said "highest bitrate" because it checked winner's bitrate > 0 without comparing to runner-up
- **Fix:** Compare winner's bitrate/tags against runner-up to determine the actual differentiating criterion
- **Files modified:** src/phaze/services/dedup.py
- **Verification:** test_score_group_tag_tiebreak and test_score_group_path_tiebreak pass
- **Committed in:** 3d42e22

---

**Total deviations:** 3 auto-fixed (2 bugs, 1 blocking)
**Impact on plan:** All fixes necessary for correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## Known Stubs
None -- all functions are fully implemented with real logic.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Backend foundation complete for Plan 02 (router + templates)
- score_group, find_duplicate_groups_with_metadata, resolve_group, undo_resolve all ready for router wiring
- get_duplicate_stats ready for dashboard integration

---
*Phase: 14-duplicate-resolution-ui*
*Completed: 2026-04-01*
