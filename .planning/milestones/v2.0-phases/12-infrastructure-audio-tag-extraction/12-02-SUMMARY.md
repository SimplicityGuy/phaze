---
phase: 12-infrastructure-audio-tag-extraction
plan: 02
subsystem: metadata-extraction
tags: [mutagen, tag-extraction, arq-task, ingestion-wiring]
dependency_graph:
  requires: [12-01]
  provides: [extract_tags, extract_file_metadata, auto-enqueue-on-scan]
  affects: [pipeline-endpoints, worker-functions, ingestion-service, scan-router]
tech_stack:
  added: [mutagen]
  patterns: [dataclass-service, arq-task-ctx-session, background-enqueue]
key_files:
  created:
    - src/phaze/services/metadata.py
    - src/phaze/tasks/metadata_extraction.py
    - tests/test_services/test_metadata.py
    - tests/test_tasks/test_metadata_extraction.py
  modified:
    - pyproject.toml
    - src/phaze/models/metadata.py
    - src/phaze/tasks/worker.py
    - src/phaze/routers/pipeline.py
    - src/phaze/services/ingestion.py
    - src/phaze/routers/scan.py
    - tests/test_routers/test_scan.py
decisions:
  - "Used dataclass (not Pydantic) for ExtractedTags to keep service layer dependency-free"
  - "Added track_number/duration/bitrate columns to FileMetadata model (Plan 01 dependency not yet merged)"
  - "Added mutagen mypy override since mutagen lacks type stubs"
metrics:
  duration: "14m 25s"
  completed: "2026-03-31"
  tasks_completed: 2
  tasks_total: 2
  tests_added: 33
  files_changed: 11
---

# Phase 12 Plan 02: Tag Extraction Service & Task Summary

Mutagen-based tag extraction service with ID3/Vorbis/MP4 dispatch, arq task for DB persistence, manual API endpoint, HTMX UI trigger, and auto-enqueue wiring into scan pipeline.

## Task Completion

| Task | Name | Commit | Key Files |
|------|------|--------|-----------|
| 1 | Tag extraction service + tests (TDD) | e5fb32f | src/phaze/services/metadata.py, tests/test_services/test_metadata.py |
| 2 | arq task + worker + API + scan auto-enqueue | 25b916c | src/phaze/tasks/metadata_extraction.py, src/phaze/routers/pipeline.py, src/phaze/services/ingestion.py |

## What Was Built

### Tag Extraction Service (`src/phaze/services/metadata.py`)
- `ExtractedTags` dataclass with artist, title, album, year, genre, track_number, duration, bitrate, raw_tags
- `extract_tags(file_path)` pure function: opens file with mutagen, dispatches based on tag type (ID3/Vorbis/MP4)
- Helper functions: `_parse_year`, `_parse_track`, `_first_str`, `_serialize_tags`
- Tag key mappings: `_VORBIS_MAP`, `_ID3_MAP`, `_MP4_MAP`
- Graceful handling: nonexistent files, corrupt files, files with no tags all return empty ExtractedTags

### arq Task (`src/phaze/tasks/metadata_extraction.py`)
- `extract_file_metadata(ctx, file_id)` async task function
- Uses `ctx["async_session"]` pattern for DB access
- Skips companion files (per D-10), creates empty rows for tagless files (per D-11)
- Upserts FileMetadata and transitions state to METADATA_EXTRACTED (per D-03)
- Retry with exponential backoff on failure

### API & UI Endpoints (`src/phaze/routers/pipeline.py`)
- `POST /api/v1/extract-metadata` -- manual trigger for all music/video files
- `POST /pipeline/extract-metadata` -- HTMX UI trigger with response fragment

### Scan Auto-Enqueue (`src/phaze/services/ingestion.py`)
- `run_scan` now accepts optional `arq_pool` parameter
- After bulk_upsert_files, auto-enqueues `extract_file_metadata` for music/video files (per D-09)
- Companion files (.txt, .nfo, etc.) are filtered out

### Worker Registration (`src/phaze/tasks/worker.py`)
- `extract_file_metadata` added to WorkerSettings.functions list

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added track_number/duration/bitrate to FileMetadata model**
- **Found during:** Task 1
- **Issue:** Plan 01 (which adds these columns) was running in parallel and not yet merged to this worktree
- **Fix:** Added the three columns directly in this plan to unblock implementation
- **Files modified:** src/phaze/models/metadata.py

**2. [Rule 1 - Bug] Fixed scan test regression from arq_pool wiring**
- **Found during:** Task 2 (full test suite run)
- **Issue:** Adding `http_request` parameter to `trigger_scan` broke existing scan tests that didn't set `app.state.arq_pool`
- **Fix:** Added `client._transport.app.state.arq_pool = AsyncMock()` to affected scan tests
- **Files modified:** tests/test_routers/test_scan.py

**3. [Rule 1 - Bug] Added mutagen mypy override**
- **Found during:** Task 1
- **Issue:** mutagen package has no type stubs, causing mypy import errors
- **Fix:** Added `[[tool.mypy.overrides]]` for `mutagen.*` with `ignore_missing_imports = true`
- **Files modified:** pyproject.toml

## Verification Results

- 33 new tests (27 service + 6 task) all pass
- Full test suite: 315 tests pass, 0 failures
- `uv run ruff check` passes on all modified files
- `uv run mypy` passes on metadata service and task files
- `grep -r "enqueue_job.*extract_file_metadata" src/phaze/services/ingestion.py` confirms D-09 wiring

## Known Stubs

None -- all data paths are wired end-to-end.

## Self-Check: PASSED
