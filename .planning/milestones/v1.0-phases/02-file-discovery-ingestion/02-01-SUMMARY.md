---
phase: 02-file-discovery-ingestion
plan: 01
subsystem: ingestion-foundation
tags: [constants, models, config, migration, docker]
dependency_graph:
  requires: []
  provides: [FileCategory, EXTENSION_MAP, ScanBatch, scan_path_config, scan_batches_table]
  affects: [src/phaze/constants.py, src/phaze/models/scan_batch.py, src/phaze/config.py, docker-compose.yml]
tech_stack:
  added: []
  patterns: [StrEnum-for-constants, TimestampMixin-model-pattern]
key_files:
  created:
    - src/phaze/constants.py
    - src/phaze/models/scan_batch.py
    - alembic/versions/002_add_scan_batches_and_unique_path.py
    - tests/test_constants.py
  modified:
    - src/phaze/models/__init__.py
    - src/phaze/config.py
    - .env.example
    - docker-compose.yml
decisions:
  - Used StrEnum for FileCategory and ScanStatus to match existing FileState pattern
  - Set HASH_CHUNK_SIZE to 64KB per design decision D-07
  - Read-only Docker volume mount for scan directory safety
requirements-completed: [ING-05]

metrics:
  duration: 4min
  completed: 2026-03-28T03:30:23Z
  tasks: 3
  files: 8
---

# Phase 02 Plan 01: Ingestion Foundation Summary

FileCategory enum, EXTENSION_MAP (27 extensions), ScanBatch model with status tracking, Alembic migration for scan_batches table with unique path index and FK, scan_path config setting, Docker volume mounts for api and worker services.

## Tasks Completed

### Task 1: Create constants module, ScanBatch model, and config update

- Created `src/phaze/constants.py` with FileCategory enum (MUSIC, VIDEO, COMPANION, UNKNOWN), EXTENSION_MAP (27 entries), HASH_CHUNK_SIZE (65536), BULK_INSERT_BATCH_SIZE (1000)
- Created `src/phaze/models/scan_batch.py` with ScanStatus enum and ScanBatch model using TimestampMixin
- Updated `src/phaze/models/__init__.py` to export ScanBatch and ScanStatus
- Added `scan_path: str = "/data/music"` to Settings
- Added SCAN_PATH to `.env.example`
- **Commit:** 83242cf

### Task 2: Alembic migration and Docker volume mount

- Created migration 002: scan_batches table, unique index on files.original_path, FK from files.batch_id to scan_batches.id
- Added read-only volume mount `${SCAN_PATH:-/data/music}:/data/music:ro` to api and worker services
- **Commit:** 17a4fba

### Task 3: Unit tests for constants and models

- Created `tests/test_constants.py` with 9 tests covering all acceptance criteria
- All 9 tests pass: enum values, extension completeness, category mapping, format validation, constant values
- **Commit:** 5c88957

## Deviations from Plan

None - plan executed exactly as written.

## Verification Results

- `from phaze.constants import FileCategory, EXTENSION_MAP` -- 27 extensions confirmed
- `ScanBatch.__tablename__` returns "scan_batches"
- `settings.scan_path` returns "/data/music"
- `uv run pytest tests/test_constants.py -x -v` -- 9/9 pass
- `uv run ruff check` -- no errors
- `uv run mypy` -- no errors
- Docker volume mounts present on api and worker services

## Known Stubs

None -- all functionality is fully wired.
