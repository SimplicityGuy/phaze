---
phase: 08-safe-file-execution-audit
plan: 01
subsystem: execution
tags: [file-operations, safety, audit-logging, arq, redis]
dependency_graph:
  requires: [models/execution, models/file, models/proposal, tasks/worker]
  provides: [services/execution, tasks/execution, migration-004]
  affects: [tasks/worker]
tech_stack:
  added: []
  patterns: [write-ahead-logging, copy-verify-delete, redis-progress-tracking]
key_files:
  created:
    - src/phaze/services/execution.py
    - src/phaze/tasks/execution.py
    - alembic/versions/004_add_execution_log.py
    - tests/test_services/test_execution.py
    - tests/test_tasks/test_execution.py
  modified:
    - src/phaze/tasks/worker.py
decisions:
  - "Write-ahead logging pattern: log IN_PROGRESS before each file operation, update to COMPLETED/FAILED after"
  - "Sequential processing in batch job: safer for single-drive disk I/O than parallel"
  - "Delete failure still marks file as EXECUTED since the copy is verified good"
  - "FileRecord type annotation via TYPE_CHECKING for proper mypy support"
requirements-completed: [EXE-01, EXE-02]
metrics:
  duration: 8min
  completed: "2026-03-29"
  tasks: 2
  files: 6
  coverage: 94.64%
---

# Phase 8 Plan 1: Execution Service with Copy-Verify-Delete Summary

Copy-verify-delete execution service with write-ahead audit logging to ExecutionLog, plus arq batch job with Redis progress tracking for SSE consumption.

## What Was Built

### Task 1: Execution Service (src/phaze/services/execution.py)

Core safety layer for the irreplaceable 200K file collection:

- **compute_sha256**: Chunked (8192 bytes) SHA256 hash computation for file verification
- **log_operation**: Creates write-ahead ExecutionLog entry with IN_PROGRESS status before each file operation (EXE-02)
- **complete_operation**: Updates log entry to COMPLETED or FAILED with error details
- **get_approved_proposals**: Queries approved proposals with eagerly loaded FileRecord via selectinload
- **execute_single_file**: Full copy-verify-delete flow with all error paths:
  - Guards against destination already existing
  - Copies with shutil.copy2 (preserves metadata)
  - Verifies SHA256 hash matches original
  - On mismatch: deletes bad copy, preserves original (D-05)
  - On delete failure: still updates FileRecord since copy is verified
  - Updates FileRecord.current_path and state after success

### Task 2: arq Batch Job (src/phaze/tasks/execution.py)

- **execute_approved_batch**: Processes all approved proposals sequentially
- Redis progress hash (`exec:{batch_id}`) updated after each file with total/completed/failed/status
- Status transitions: running -> complete
- 1-hour TTL on Redis keys for cleanup
- Partial failures don't stop the batch (D-07)
- Registered in WorkerSettings.functions

### Alembic Migration (004_add_execution_log.py)

Creates execution_log table with:
- proposal_id FK to proposals
- operation, source_path, destination_path, sha256_verified, status, error_message
- executed_at, created_at, updated_at timestamps
- Indexes on proposal_id and status

## Test Coverage

15 tests total, 94.64% coverage:

- **Service tests (10)**: compute_sha256, copy-verify-delete success, hash mismatch cleanup, destination exists, copy OS error, delete failure with good copy, file record update, write-ahead audit pattern, log/complete operations, approved proposals query
- **Task tests (5)**: batch success, partial failure, empty batch, Redis progress updates, UUID generation

## Deviations from Plan

None - plan executed exactly as written.

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 (RED) | 52332c9 | Failing tests for execution service |
| 1 (GREEN) | c562c47 | Execution service implementation + migration |
| 2 (RED) | a048c14 | Failing tests for arq batch job |
| 2 (GREEN) | 817cdf1 | arq batch job implementation |

## Verification Results

- All 15 tests pass
- ruff check clean (lint + format)
- mypy clean (strict mode)
- Coverage: 94.64% (above 85% minimum)
