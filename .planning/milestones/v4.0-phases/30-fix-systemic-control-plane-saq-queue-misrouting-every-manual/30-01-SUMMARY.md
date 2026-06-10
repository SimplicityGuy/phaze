---
phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual
plan: 01
subsystem: infra
tags: [saq, redis, task-queue, sqlalchemy, fastapi-lifespan, routing]

# Dependency graph
requires:
  - phase: 26-distributed-agents (AgentTaskRouter, per-agent queues)
    provides: "AgentTaskRouter._queue_for cached phaze-agent-<id> queues + apply_project_job_defaults hook"
  - phase: 24-distributed-agents (Agent model)
    provides: "Agent table with revoked_at / last_seen_at columns + LEGACY_AGENT_ID"
provides:
  - "src/phaze/services/enqueue_router.py: CONTROLLER_TASKS/AGENT_TASKS sets, select_active_agent, resolve_queue_for_task, NoActiveAgentError, RoutedQueue"
  - "Named 'controller' SAQ queue wired in the API lifespan (app.state.controller_queue) with the policy before_enqueue hook"
  - "Public AgentTaskRouter.queue_for(agent_id) accessor"
  - "Removal of the consumer-less unnamed default queue (app.state.queue) from the API process"
affects: [30-02, 30-03, 30-04, pipeline.py, tracklists.py, scan.py, ingestion.py]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Single source of truth: task_name -> consumed-queue map; unknown names raise (fail loud, never default)"
    - "Active-agent selection: most-recently-seen non-revoked agent (deterministic ORDER BY last_seen_at DESC LIMIT 1)"

key-files:
  created:
    - src/phaze/services/enqueue_router.py
    - tests/test_services/test_enqueue_router.py
  modified:
    - src/phaze/main.py
    - src/phaze/services/agent_task_router.py
    - src/phaze/routers/agent_exec_batches.py
    - tests/test_main_lifespan.py
    - tests/test_phase04_gaps.py

key-decisions:
  - "Active-agent policy = most-recently-seen non-revoked agent (simplest deterministic rule; round-robin/least-loaded deferred)"
  - "reap_stalled_scans and heartbeat_tick are cron-only and intentionally excluded from the operator-routable task sets"
  - "Unknown task names raise ValueError rather than falling back to any queue (fail loud)"

patterns-established:
  - "Pattern: resolve_queue_for_task() is the single routing chokepoint every control-plane enqueue (Plans 02-04) will call"
  - "Pattern: per-agent dispatch always routes via AgentTaskRouter.queue_for so the apply_project_job_defaults hook is guaranteed applied"

requirements-completed: [QR-01, QR-02]

# Metrics
duration: ~20min
completed: 2026-06-09
---

# Phase 30 Plan 01: Control-plane SAQ routing foundation Summary

**Shared task->queue routing helper (`resolve_queue_for_task` + `select_active_agent`) plus a named `controller` SAQ queue in the API lifespan, eliminating the consumer-less unnamed default queue that stranded 11,428 jobs in the v4.0.6 incident.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-06-09
- **Completed:** 2026-06-09
- **Tasks:** 2
- **Files modified:** 7 (2 created, 5 modified)

## Accomplishments
- New `enqueue_router.py`: `CONTROLLER_TASKS`/`AGENT_TASKS` frozensets mirroring the worker `functions` lists, `select_active_agent` (non-revoked + recently-seen, deterministic), `resolve_queue_for_task` mapping every known task to a consumed queue and raising `ValueError` on unknown names.
- API lifespan now wires `app.state.controller_queue` (named `"controller"`, with `apply_project_job_defaults` before_enqueue hook) and no longer constructs the unnamed default queue that had no consumer.
- `AgentTaskRouter.queue_for(agent_id)` is now public so the routing helper and future poll sites reuse the same hook-applied cached Queue.
- Threat register T-30-01..T-30-04 mitigations realized: fail-loud on unknown tasks, revoked-agent exclusion, queue names derived only from `Agent.id` + a fixed allowlist, and `NoActiveAgentError` instead of a silent success.

## Task Commits

1. **Task 1 (RED): failing tests for enqueue-routing helper** - `43dadcb` (test)
2. **Task 1 (GREEN): shared enqueue-routing helper** - `13c4d31` (feat)
3. **Task 2: wire named controller queue, drop default queue, public queue_for** - `97b0940` (feat)

_Note: Task 1 is TDD (test -> feat). No refactor commit was needed._

## Files Created/Modified
- `src/phaze/services/enqueue_router.py` (created) - Routing source of truth: task sets, `select_active_agent`, `resolve_queue_for_task`, `NoActiveAgentError`, `RoutedQueue`.
- `tests/test_services/test_enqueue_router.py` (created) - 11 tests covering every routing/selection branch.
- `src/phaze/main.py` - Lifespan wires `app.state.controller_queue` (name="controller") + policy hook; unnamed default queue removed; reverse-order shutdown disconnects it last.
- `src/phaze/services/agent_task_router.py` - Public `queue_for` accessor delegating to the cached `_queue_for`; class docstring updated.
- `src/phaze/routers/agent_exec_batches.py` - Stale `app.state.queue.redis` doc comment refreshed to `controller_queue`.
- `tests/test_main_lifespan.py` - Asserts `controller_queue` exists, `app.state.queue` is gone, queue named "controller", before_enqueue hook registered.
- `tests/test_phase04_gaps.py` - Lifespan startup test updated to the new `controller_queue` shape.

## Decisions Made
- **Active-agent policy:** most-recently-seen non-revoked agent (`revoked_at IS NULL AND last_seen_at IS NOT NULL`, `ORDER BY last_seen_at DESC LIMIT 1`). Documented as the simplest deterministic rule; round-robin/least-loaded deferred.
- **Routable task sets exclude cron-only functions** (`reap_stalled_scans`, `heartbeat_tick`) — operators never enqueue these.
- **Unknown task names fail loud** (`ValueError`), never fall back to any queue.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated `test_phase04_gaps.py` lifespan assertion to the new queue shape**
- **Found during:** Task 2 (lifespan rewire)
- **Issue:** `test_lifespan_creates_queue_on_startup` asserted `app.state.queue is mock_queue`, which is the exact behavior this plan removes — the test would fail against the new lifespan. Not listed in the plan's `files_modified`, but directly broken by the Task 2 change (Rule 1 / scope: the test exercises the code I modified).
- **Fix:** Updated the assertion to `app.state.controller_queue is mock_queue`, added `assert not hasattr(app.state, "queue")`, and asserted `from_url` was called with `name="controller"`. Refreshed the test name/docstring.
- **Files modified:** tests/test_phase04_gaps.py
- **Verification:** `uv run pytest tests/test_phase04_gaps.py -q` passes (the companion shutdown test still passes unchanged, since the controller queue is the same mock).
- **Committed in:** `97b0940` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug — a stale test directly invalidated by the planned lifespan change).
**Impact on plan:** Necessary for the test suite to reflect the new behavior. No scope creep — only the test of the modified code was touched.

## Issues Encountered
- **Local Redis unavailable (environmental, not a code issue):** `tests/test_services/test_agent_task_router.py` (5 failures + 7 teardown errors) require a live Redis at `localhost:6379` for real SAQ enqueues; only Postgres is running in this worktree environment. All failures are `redis.exceptions.ConnectionError`. My change to `agent_task_router.py` is purely additive (new public `queue_for` delegating to the unchanged `_queue_for`) and cannot affect these. They pass in CI where Redis runs. Out of scope per the scope-boundary rule; not logged to deferred-items.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Routing foundation is in place: Plans 02-04 can now replace each misrouted `app.state.queue` enqueue (pipeline.py, tracklists.py, scan.py/ingestion.py) with `resolve_queue_for_task(...)`.
- **Note for the wave:** the per-site router code (pipeline.py, tracklists.py, scan.py) still references the now-removed `app.state.queue`. Those endpoints are runtime-broken until Plans 02-04 land — this is the intended phased sequence (this plan is the foundation; the per-site fixes follow). Router unit tests are unaffected because they set `app.state.queue` themselves on non-lifespan apps.

## Verification
- `uv run pytest tests/test_services/test_enqueue_router.py tests/test_main_lifespan.py tests/test_phase04_gaps.py -q` -> 18 passed.
- `grep "Queue.from_url" src/phaze/main.py` -> only the `name="controller"` construction.
- `uv run mypy .` -> Success (142 source files). `uv run ruff check .` -> All checks passed.

---
*Phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual*
*Completed: 2026-06-09*
