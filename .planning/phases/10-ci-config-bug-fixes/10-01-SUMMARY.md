---
phase: 10-ci-config-bug-fixes
plan: 01
subsystem: models
tags: [sqlalchemy, orm, foreignkey]

requires:
  - phase: 01-infrastructure
    provides: FileRecord model, migration 002
provides:
  - FileRecord.batch_id ForeignKey annotation matching DB constraint
affects: []

tech-stack:
  added: []
  patterns: []

key-files:
  created: []
  modified:
    - src/phaze/models/file.py
    - tests/test_models.py
    - tests/test_services/test_ingestion.py

key-decisions:
  - "Added ScanBatch setup to integration tests that insert file records with batch_id"

patterns-established: []

requirements-completed: [INF-03]

duration: 3min
completed: 2026-03-30
---

# Phase 10 Plan 1: Add ForeignKey to FileRecord.batch_id

**ORM model fix to match DB-level constraint from migration 002**

## Performance

- **Duration:** 3 min
- **Tasks:** 1
- **Files modified:** 3

## Accomplishments
- Added `ForeignKey("scan_batches.id")` to `FileRecord.batch_id` ORM column
- Updated test to assert FK exists and targets `scan_batches.id`
- Fixed 2 integration tests that needed `ScanBatch` setup to satisfy FK constraint

## Task Commits

1. **Task 1: Add ForeignKey and fix tests** - `b8a8555` (fix)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Integration tests needed ScanBatch setup**
- **Found during:** Test suite run after adding ForeignKey
- **Issue:** `test_bulk_upsert_stores_paths` and `test_bulk_upsert_handles_duplicates` insert file records with `batch_id` but no corresponding `ScanBatch` row — FK violation
- **Fix:** Added `ScanBatch` creation before file insertion in both tests
- **Verification:** 269 tests pass

## Issues Encountered
None beyond the auto-fixed test deviations.

## Known Stubs
None.

---
*Phase: 10-ci-config-bug-fixes*
*Completed: 2026-03-30*
