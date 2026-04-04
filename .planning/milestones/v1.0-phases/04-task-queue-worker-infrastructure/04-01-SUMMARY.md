---
phase: 04-task-queue-worker-infrastructure
plan: 01
subsystem: infra
tags: [arq, redis, task-queue, process-pool, asyncio]

# Dependency graph
requires:
  - phase: 01-project-setup
    provides: project structure, pyproject.toml, config.py with pydantic-settings
provides:
  - arq task queue dependency installed
  - Worker configuration settings (max_jobs, job_timeout, retries, pool size)
  - WorkerSettings class discoverable by arq CLI
  - Skeleton process_file task with retry logic
  - ProcessPoolExecutor lifecycle management
affects: [04-02-redis-docker-enqueue-api, 05-audio-analysis]

# Tech tracking
tech-stack:
  added: [arq>=0.27.0, redis (transitive)]
  patterns: [arq WorkerSettings class, ProcessPoolExecutor startup/shutdown hooks, Retry with exponential backoff]

key-files:
  created:
    - src/phaze/tasks/__init__.py
    - src/phaze/tasks/worker.py
    - src/phaze/tasks/functions.py
    - src/phaze/tasks/pool.py
    - tests/test_tasks/__init__.py
    - tests/test_tasks/test_functions.py
    - tests/test_tasks/test_worker.py
    - tests/test_tasks/test_pool.py
    - tests/test_config_worker.py
  modified:
    - pyproject.toml
    - src/phaze/config.py

key-decisions:
  - "Used ClassVar annotation on WorkerSettings.functions to satisfy ruff RUF012 mutable class attribute rule"
  - "arq Retry stores defer as defer_score in milliseconds -- tests assert against millisecond values"

patterns-established:
  - "arq WorkerSettings pattern: functions list, on_startup/on_shutdown hooks, settings-driven config"
  - "ProcessPoolExecutor lifecycle: created in startup hook, shutdown(wait=True) in shutdown hook"
  - "CPU-bound work via asyncio.get_running_loop().run_in_executor(pool, func, *args)"

requirements-completed: [INF-02, ANL-03]

# Metrics
duration: 7min
completed: 2026-03-28
---

# Phase 4 Plan 1: arq Task Queue Infrastructure Summary

**arq task queue with WorkerSettings, skeleton process_file with exponential retry backoff, and ProcessPoolExecutor for CPU-bound audio analysis**

## Performance

- **Duration:** 7 min
- **Started:** 2026-03-28T17:03:48Z
- **Completed:** 2026-03-28T17:10:24Z
- **Tasks:** 2
- **Files modified:** 11

## Accomplishments
- Installed arq>=0.27.0 with 6 configurable worker settings (max_jobs, job_timeout, retries, pool size, health check, keep result)
- Created `src/phaze/tasks/` package with worker.py (WorkerSettings), functions.py (process_file with retry), pool.py (ProcessPoolExecutor helper)
- 21 tests covering all configuration defaults, task functions, worker settings, and pool lifecycle

## Task Commits

Each task was committed atomically:

1. **Task 1: Add arq dependency and extend Settings with worker configuration** - `eef250d` (feat)
2. **Task 2: Create tasks package with worker, functions, and pool modules** - `e3fba20` (feat)

_Both tasks followed TDD: tests written first (RED), then implementation (GREEN)._

## Files Created/Modified
- `pyproject.toml` - Added arq>=0.27.0 dependency
- `src/phaze/config.py` - Added 6 worker_* settings fields
- `src/phaze/tasks/__init__.py` - Package init
- `src/phaze/tasks/worker.py` - WorkerSettings class with startup/shutdown hooks
- `src/phaze/tasks/functions.py` - Skeleton process_file with arq Retry backoff
- `src/phaze/tasks/pool.py` - ProcessPoolExecutor create/run_in helpers
- `tests/test_config_worker.py` - 6 tests for worker config defaults
- `tests/test_tasks/test_functions.py` - 5 tests for task function behavior
- `tests/test_tasks/test_worker.py` - 6 tests for WorkerSettings attributes
- `tests/test_tasks/test_pool.py` - 4 tests for pool lifecycle

## Decisions Made
- Used `ClassVar[list[Any]]` annotation on `WorkerSettings.functions` to satisfy ruff RUF012 (mutable class attribute)
- arq `Retry(defer=N)` stores value as `defer_score` in milliseconds -- tests assert against ms values (5000, 10000, 15000)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed Retry.defer attribute access in tests**
- **Found during:** Task 2 (test execution)
- **Issue:** Plan referenced `Retry.defer` but arq stores it as `Retry.defer_score` in milliseconds
- **Fix:** Updated test assertions to use `defer_score` with millisecond values
- **Files modified:** tests/test_tasks/test_functions.py
- **Committed in:** e3fba20

**2. [Rule 1 - Bug] Fixed ruff B904 and RUF012 lint violations**
- **Found during:** Task 2 (lint verification)
- **Issue:** `raise Retry` in except block needed `from exc`; mutable class attribute needed ClassVar
- **Fix:** Added `from exc` chain, annotated functions with ClassVar
- **Files modified:** src/phaze/tasks/functions.py, src/phaze/tasks/worker.py
- **Committed in:** e3fba20

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes required for correctness and lint compliance. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Worker infrastructure complete, ready for 04-02 (Redis Docker, enqueue API)
- Phase 5 audio analysis can plug into process_file skeleton
- ProcessPoolExecutor pattern established for CPU-bound work

---
*Phase: 04-task-queue-worker-infrastructure*
*Completed: 2026-03-28*
