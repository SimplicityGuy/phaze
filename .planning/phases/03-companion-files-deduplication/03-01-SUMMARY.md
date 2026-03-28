---
phase: 03-companion-files-deduplication
plan: 01
subsystem: database, api
tags: [sqlalchemy, alembic, postgresql, companion-files, deduplication, sha256]

# Dependency graph
requires:
  - phase: 02-file-ingestion-pipeline
    provides: FileRecord model, ingestion pipeline, file discovery
provides:
  - FileCompanion join table model linking companions to media files
  - associate_companions() service for directory-based companion linking
  - find_duplicate_groups() and count_duplicate_groups() dedup services
  - Alembic migration 003 for file_companions table
affects: [03-02-api-endpoints, future-dedup-ui, future-companion-management]

# Tech tracking
tech-stack:
  added: []
  patterns: [directory-based companion association, SHA256-grouped dedup queries, idempotent link creation]

key-files:
  created:
    - src/phaze/models/file_companion.py
    - src/phaze/services/companion.py
    - src/phaze/services/dedup.py
    - alembic/versions/003_add_file_companions_table.py
    - tests/test_services/test_companion.py
    - tests/test_services/test_dedup.py
  modified:
    - src/phaze/models/__init__.py

key-decisions:
  - "Used PurePosixPath for directory grouping to match POSIX paths stored in DB"
  - "Companion types and media types derived from EXTENSION_MAP at module level for consistency"
  - "Idempotency via NOT IN subquery on already-linked companion IDs"

patterns-established:
  - "Service functions accept AsyncSession, return typed results"
  - "Module-level type sets derived from EXTENSION_MAP for file classification"
  - "Paginated queries via limit/offset on subquery"

requirements-completed: [ING-04, ING-06]

# Metrics
duration: 4min
completed: 2026-03-28
---

# Phase 03 Plan 01: Companion Files and Dedup Data Layer Summary

**FileCompanion join table with directory-based companion association and SHA256 duplicate group detection services**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-28T05:23:04Z
- **Completed:** 2026-03-28T05:27:16Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments
- FileCompanion join table model with companion_id/media_id FKs, unique constraint, and indexes
- associate_companions() links unlinked companion files to media in the same directory, idempotent
- find_duplicate_groups() returns paginated SHA256-grouped duplicates with file details
- count_duplicate_groups() returns total number of duplicate hash groups
- 10 tests covering all behaviors (5 companion, 5 dedup), all passing

## Task Commits

Each task was committed atomically:

1. **Task 1: FileCompanion model, migration, and model registration** - `6c72309` (feat)
2. **Task 2 RED: Failing tests for companion and dedup services** - `9f973c4` (test)
3. **Task 2 GREEN: Implement companion and dedup services** - `8759bd1` (feat)

## Files Created/Modified
- `src/phaze/models/file_companion.py` - FileCompanion join table model with UUID PK, companion_id/media_id FKs, unique constraint
- `src/phaze/models/__init__.py` - Added FileCompanion to model registry for Alembic autogenerate
- `alembic/versions/003_add_file_companions_table.py` - Migration creating file_companions table with CASCADE FKs and indexes
- `src/phaze/services/companion.py` - associate_companions() with directory-based linking and idempotency
- `src/phaze/services/dedup.py` - find_duplicate_groups() and count_duplicate_groups() with paginated queries
- `tests/test_services/test_companion.py` - 5 tests for companion association logic
- `tests/test_services/test_dedup.py` - 5 tests for duplicate detection logic

## Decisions Made
- Used PurePosixPath for directory grouping since DB stores POSIX-style paths
- Module-level COMPANION_TYPES and MEDIA_TYPES sets derived from EXTENSION_MAP for single source of truth
- Idempotency achieved by filtering out already-linked companions via NOT IN subquery

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- PostgreSQL test database needed to be created before tests could run (docker compose up postgres + CREATE DATABASE phaze_test)

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- FileCompanion model and services ready for API endpoint consumption in Plan 02
- Both associate_companions() and find_duplicate_groups()/count_duplicate_groups() are fully tested and typed

---
*Phase: 03-companion-files-deduplication*
*Completed: 2026-03-28*
