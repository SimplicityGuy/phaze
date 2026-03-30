---
phase: 09-pipeline-orchestration
plan: 01
subsystem: api
tags: [fastapi, arq, htmx, pipeline, jinja2, sqlalchemy]

# Dependency graph
requires:
  - phase: 04-task-queue
    provides: arq worker infrastructure, process pool
  - phase: 05-audio-analysis
    provides: process_file task function
  - phase: 06-ai-proposals
    provides: generate_proposals task function
  - phase: 07-approval-ui
    provides: HTMX/Jinja2 UI patterns, base template, nav bar
  - phase: 08-safe-execution
    provides: execution router, SSE patterns
provides:
  - Pipeline trigger API endpoints (POST /api/v1/analyze, POST /api/v1/proposals/generate)
  - Pipeline dashboard page with stage counts and trigger buttons
  - Shared task session module (get_task_session)
  - Pipeline service with stage count queries
  - Docker OUTPUT_PATH volume mount for worker
  - output_path config setting
affects: [10-integration]

# Tech tracking
tech-stack:
  added: []
  patterns: [background-enqueue-for-large-batches, session-dedup-across-task-modules]

key-files:
  created:
    - src/phaze/tasks/session.py
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/dashboard.html
    - src/phaze/templates/pipeline/partials/stats_bar.html
    - src/phaze/templates/pipeline/partials/stage_cards.html
    - src/phaze/templates/pipeline/partials/trigger_response.html
    - tests/test_routers/test_pipeline.py
    - tests/test_tasks/test_session.py
    - tests/test_services/test_pipeline.py
  modified:
    - src/phaze/tasks/functions.py
    - src/phaze/tasks/proposal.py
    - src/phaze/tasks/execution.py
    - src/phaze/config.py
    - src/phaze/main.py
    - src/phaze/templates/base.html
    - docker-compose.yml
    - tests/test_tasks/test_execution.py
    - tests/test_tasks/test_functions.py
    - tests/test_tasks/test_proposal.py

key-decisions:
  - "Background enqueue via asyncio.create_task to avoid HTTP timeout on 200K+ file batches"
  - "HTMX polling every 5s for pipeline stats refresh on dashboard"

patterns-established:
  - "Session dedup: shared get_task_session in tasks/session.py imported by all task modules"
  - "Background enqueue pattern: create_task with GC-safe task set for large job submissions"

requirements-completed: [ANL-01, ANL-02, AIP-01]

# Metrics
duration: 15min
completed: 2026-03-30
---

# Phase 9 Plan 1: Pipeline Orchestration Summary

**Pipeline trigger endpoints and dashboard wiring scan->analyze->propose flow via API with background enqueue for 200K+ file scale**

## Performance

- **Duration:** 15 min
- **Started:** 2026-03-30T06:02:43Z
- **Completed:** 2026-03-30T06:18:26Z
- **Tasks:** 2
- **Files modified:** 20

## Accomplishments
- Pipeline trigger API endpoints (POST /api/v1/analyze, POST /api/v1/proposals/generate) with background enqueue
- Pipeline dashboard page at /pipeline/ with HTMX-polled stage counts and trigger buttons
- Session dedup: extracted _get_session to shared tasks/session.py module, eliminating 3 copies
- Docker worker OUTPUT_PATH volume mount and config.py output_path setting

## Task Commits

Each task was committed atomically:

1. **Task 1: Session dedup, config, Docker volume, pipeline service** - `54d23da` (feat)
2. **Task 2: Pipeline router with trigger endpoints, dashboard page, tests** - `4e70db1` (feat)

## Files Created/Modified
- `src/phaze/tasks/session.py` - Shared async session factory for arq tasks
- `src/phaze/services/pipeline.py` - Pipeline stage count queries and file-by-state retrieval
- `src/phaze/routers/pipeline.py` - API trigger endpoints and dashboard page routes
- `src/phaze/templates/pipeline/dashboard.html` - Dashboard page with stats and trigger cards
- `src/phaze/templates/pipeline/partials/stats_bar.html` - HTMX-polled stage count bar
- `src/phaze/templates/pipeline/partials/stage_cards.html` - Action cards with trigger buttons
- `src/phaze/templates/pipeline/partials/trigger_response.html` - Enqueue confirmation fragment
- `src/phaze/main.py` - Added pipeline router registration
- `src/phaze/templates/base.html` - Added Pipeline nav link
- `src/phaze/config.py` - Added output_path setting
- `docker-compose.yml` - Added OUTPUT_PATH :rw volume to worker
- `src/phaze/tasks/functions.py` - Replaced _get_session with get_task_session import
- `src/phaze/tasks/proposal.py` - Replaced _get_session with get_task_session import
- `src/phaze/tasks/execution.py` - Replaced _get_session with get_task_session import
- `tests/test_routers/test_pipeline.py` - 6 tests for pipeline router endpoints
- `tests/test_tasks/test_session.py` - 1 test for session factory
- `tests/test_services/test_pipeline.py` - 3 tests for pipeline service queries

## Decisions Made
- Background enqueue via asyncio.create_task to avoid HTTP timeout on 200K+ file batches
- HTMX polling every 5s for pipeline stats refresh on dashboard

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Updated existing test mocks after session dedup refactor**
- **Found during:** Task 2 (full test suite run)
- **Issue:** Existing tests in test_execution.py, test_functions.py, test_proposal.py patched `_get_session` at the old module paths which no longer existed after session dedup
- **Fix:** Updated all `@patch("phaze.tasks.*.\_get_session")` to `@patch("phaze.tasks.*.get_task_session")` in 3 test files
- **Files modified:** tests/test_tasks/test_execution.py, tests/test_tasks/test_functions.py, tests/test_tasks/test_proposal.py
- **Verification:** Full test suite (269 tests) passes
- **Committed in:** 4e70db1 (Task 2 commit)

**2. [Rule 1 - Bug] Fixed mypy type annotation in pipeline service**
- **Found during:** Task 2 (mypy check)
- **Issue:** `dict(result.all())` had incompatible type for mypy -- needed explicit type annotation
- **Fix:** Changed to explicit dict comprehension with type annotation: `counts: dict[str, int] = {row[0]: row[1] for row in result.all()}`
- **Files modified:** src/phaze/services/pipeline.py
- **Verification:** `uv run mypy .` passes with 0 errors
- **Committed in:** 4e70db1 (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 bug)
**Impact on plan:** Both fixes necessary for correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## Known Stubs
None -- all endpoints are fully wired to real service functions and database queries.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Pipeline orchestration endpoints are operational
- E2E flow scan->analyze->propose is triggerable via API
- Ready for Phase 10 integration testing and final wiring

---
*Phase: 09-pipeline-orchestration*
*Completed: 2026-03-30*
