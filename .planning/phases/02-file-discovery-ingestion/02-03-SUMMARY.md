---
phase: 02-file-discovery-ingestion
plan: 03
subsystem: api
tags: [fastapi, pydantic, asyncio, scan-api]

requires:
  - phase: 02-02
    provides: "Ingestion service with run_scan, discover_and_hash_files, bulk_upsert_files"
provides:
  - "POST /api/v1/scan endpoint for triggering file discovery scans"
  - "GET /api/v1/scan/{batch_id} endpoint for checking scan status"
  - "Pydantic schemas for scan request/response models"
  - "Scan-related just commands"
affects: [phase-03, phase-04, ui]

tech-stack:
  added: [greenlet]
  patterns: [background-task-with-reference-tracking, path-validation-pattern]

key-files:
  created:
    - src/phaze/schemas/__init__.py
    - src/phaze/schemas/scan.py
    - src/phaze/routers/scan.py
    - tests/test_routers/__init__.py
    - tests/test_routers/test_scan.py
  modified:
    - src/phaze/main.py
    - README.md
    - justfile
    - tests/test_models.py
    - pyproject.toml
    - uv.lock

key-decisions:
  - "Background tasks stored in module-level set to prevent GC (RUF006 pattern)"
  - "Path traversal check rejects any path containing '..' for simplicity"
  - "Pydantic schemas use runtime imports for uuid/datetime (not TYPE_CHECKING) due to model resolution needs"

patterns-established:
  - "Router pattern: APIRouter with prefix=/api/v1, tags for grouping"
  - "Background scan: asyncio.create_task with reference tracking set"
  - "Path validation: traversal check then is_dir() before processing"

requirements-completed: [ING-01, ING-02, ING-03, ING-05]

duration: 9min
completed: 2026-03-28
---

# Phase 02 Plan 03: Scan API Endpoints Summary

**REST API endpoints for triggering file discovery scans and querying status, with Pydantic schemas, background task management, and path validation**

## Performance

- **Duration:** 9 min
- **Started:** 2026-03-28T03:45:35Z
- **Completed:** 2026-03-28T03:54:48Z
- **Tasks:** 3/3
- **Files modified:** 11

## Accomplishments

### Task 1: Scan schemas, router, and app wiring
- Created `ScanRequest`, `ScanResponse`, `ScanStatusResponse` Pydantic models in `src/phaze/schemas/scan.py`
- Created scan router with `POST /api/v1/scan` and `GET /api/v1/scan/{batch_id}` endpoints
- POST endpoint validates path (no traversal, must be real directory), generates batch_id, launches background scan via `asyncio.create_task`
- GET endpoint queries ScanBatch model and returns status or 404
- Wired scan router into FastAPI app in `main.py`
- **Commit:** `42ed5b2`

### Task 2: Endpoint tests
- Created 6 async endpoint tests covering success and error paths
- Tests use monkeypatching to mock `run_scan` and `settings.scan_path`
- Tests cover: default scan, path override, invalid path (400), path traversal (400), status not found (404), status found (200)
- Fixed Pydantic schema to use runtime imports for uuid/datetime (not TYPE_CHECKING)
- Added greenlet dependency for async SQLAlchemy test support
- **Commit:** `9ad803c`

### Task 3: README, justfile, and quality checks
- Added "Scanning Files" section to README with configuration, usage examples, and supported file types table
- Updated Project Structure in README to include all new modules
- Added `scan` and `scan-status` just commands
- Fixed pre-existing `test_all_tables_defined` to include `scan_batches`
- All quality checks pass: ruff, mypy, 45 tests, 91.56% coverage
- **Commit:** `0f41bd2`

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Pydantic schema runtime imports**
- **Found during:** Task 2
- **Issue:** `uuid` and `datetime` were in `TYPE_CHECKING` block but Pydantic needs them at runtime for model field resolution
- **Fix:** Moved imports out of `TYPE_CHECKING` into regular imports
- **Files modified:** `src/phaze/schemas/scan.py`
- **Commit:** `9ad803c`

**2. [Rule 1 - Bug] Missing scan_batches in test_all_tables_defined**
- **Found during:** Task 3
- **Issue:** Pre-existing test expected only 5 tables but scan_batches was added in Plan 01
- **Fix:** Updated expected table set to include `scan_batches`
- **Files modified:** `tests/test_models.py`
- **Commit:** `0f41bd2`

**3. [Rule 3 - Blocking] greenlet dependency missing**
- **Found during:** Task 2
- **Issue:** async SQLAlchemy tests required greenlet library which was not installed
- **Fix:** Added greenlet as dev dependency via `uv add --dev greenlet`
- **Files modified:** `pyproject.toml`, `uv.lock`
- **Commit:** `9ad803c`

## Test Results

```
45 passed in 1.12s
Coverage: 91.56% (above 85% threshold)
```

| Module | Coverage |
|--------|----------|
| schemas/scan.py | 100% |
| routers/scan.py | 87.88% |
| models/scan_batch.py | 100% |

## Known Stubs

None -- all endpoints are fully wired to the ingestion service.

## Self-Check: PASSED

All files verified present. All commit hashes verified in git log.
