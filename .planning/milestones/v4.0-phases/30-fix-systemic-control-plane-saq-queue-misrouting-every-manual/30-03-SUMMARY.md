---
phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual
plan: 03
subsystem: api
tags: [saq, redis, task-queue, fastapi, htmx, routing, tracklists]

# Dependency graph
requires:
  - phase: 30-01 (control-plane SAQ routing foundation)
    provides: "resolve_queue_for_task, NoActiveAgentError, RoutedQueue, AgentTaskRouter.queue_for, app.state.controller_queue"
provides:
  - "tracklists router: rescrape/search/match route to the controller queue via resolve_queue_for_task"
  - "tracklists router: scan_live_set routes to the active agent's phaze-agent-<id> queue; zero-agent path is a visible empty-state"
  - "scan_status polls the same per-agent queue via task_router.queue_for(agent_id); agent_id threaded through scan_progress.html"
affects: [30-04]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-agent enqueue + queue-scoped poll must share the same agent_id: trigger_scan -> scan_progress.html poll URL -> scan_status queue_for(agent_id)"
    - "NoActiveAgentError renders a visible empty-state fragment instead of silently enqueuing nothing"

key-files:
  created: []
  modified:
    - src/phaze/routers/tracklists.py
    - src/phaze/templates/tracklists/partials/scan_progress.html
    - tests/test_routers/test_tracklists.py

key-decisions:
  - "scan_status agent_id is a required Query(...) param echoed from trigger_scan's selected agent; a garbage value resolves to a queue with no matching jobs (poll returns nothing) and cannot enqueue (T-30-03 residual accepted)"
  - "Task 1 imported only resolve_queue_for_task to stay lint-clean; NoActiveAgentError added in Task 2 where it is used"

requirements-completed: [QR-01, QR-02]

# Metrics
duration: ~25min
completed: 2026-06-09
---

# Phase 30 Plan 03: Tracklists router queue-routing fix Summary

**The four misrouted tracklist enqueues (`scrape_and_store_tracklist`, `search_tracklist`, `match_tracklist_to_discogs` -> controller queue; `scan_live_set` -> per-agent queue) now route through Plan 01's `resolve_queue_for_task`, and the scan-status poll follows `scan_live_set` onto the same `phaze-agent-<id>` queue by threading the selected `agent_id` through the progress partial.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-06-09
- **Completed:** 2026-06-09
- **Tasks:** 2
- **Files modified:** 3 (0 created, 3 modified)

## Accomplishments
- `rescrape_tracklist`, `manual_search`, and `match_discogs` now resolve the named `controller` queue via `resolve_queue_for_task(...)` instead of the removed consumer-less `app.state.queue`. `manual_search` gained a `session` dependency the helper requires.
- `trigger_scan` resolves the active agent's `phaze-agent-<id>` queue, enqueues `scan_live_set` per file there, and passes `routed.agent_id` into the progress partial. `NoActiveAgentError` short-circuits to a visible no-active-agent fragment and enqueues nothing.
- `scan_status` takes a required `agent_id` query param and polls `task_router.queue_for(agent_id)`, so the queue-scoped `queue.job(job_key)` lookups hit the same per-agent queue the jobs were enqueued onto. `agent_id` is re-emitted on every poll iteration so the round-trip survives the 3s HTMX re-render.
- `scan_progress.html` threads `agent_id` into the `hx-get` poll URL and renders the no-active-agent copy in the `done` branch.
- Threat register realized: T-30-01 (no default-queue producers remain — `grep` gate returns 0), T-30-03 (agent_id only builds `phaze-agent-<id>`; garbage resolves to an empty poll, never an enqueue), T-30-04 (zero-agent path is a visible empty-state).

## Task Commits

1. **Task 1: route 3 controller tracklist enqueues through resolve_queue_for_task** - `818f154` (fix)
2. **Task 2: route scan_live_set per-agent + re-target scan-status poll + tests** - `b986a01` (fix)

## Files Created/Modified
- `src/phaze/routers/tracklists.py` (modified) - 4 enqueue sites rerouted; `trigger_scan`/`scan_status` gain session+agent_id; `app.state.queue` fully removed.
- `src/phaze/templates/tracklists/partials/scan_progress.html` (modified) - poll URL carries `&agent_id=...`; no-active-agent empty-state added to the `done` branch.
- `tests/test_routers/test_tracklists.py` (modified) - migrated off `app.state.queue` to `_FakeQueue` + `_FakeTaskRouter`; new no-active-agent test and per-agent poll-targeting assertions.

## Decisions Made
- **`scan_status.agent_id` is a required `Query(...)` param** echoed from `trigger_scan`'s selected agent. A mismatched value resolves to a per-agent queue with no matching jobs (poll returns nothing) and can never enqueue — T-30-03's residual is accepted for this single-user LAN tool.
- **Split the `enqueue_router` import across tasks** — Task 1 imported only `resolve_queue_for_task` so its commit stays ruff-clean; `NoActiveAgentError` was added in Task 2 where it is first used.

## Deviations from Plan

None - plan executed exactly as written. Both tasks' acceptance criteria and the plan-level verification block pass.

## Issues Encountered
None. Postgres was available in the worktree, so the full `tests/test_routers/test_tracklists.py` suite (63 tests) ran green locally — these router tests use fake in-memory queues and do not require Redis.

## Verification
- `grep -c "app.state.queue" src/phaze/routers/tracklists.py` -> `0`.
- `grep -n "queue_for" src/phaze/routers/tracklists.py` -> `scan_status` uses `task_router.queue_for(agent_id)`.
- `grep -n "agent_id" src/phaze/templates/tracklists/partials/scan_progress.html` -> poll URL carries `agent_id`.
- `uv run pytest tests/test_routers/test_tracklists.py -q` -> 63 passed.
- `uv run mypy src/phaze/routers/tracklists.py` -> Success. `uv run ruff check src/phaze/routers/tracklists.py` (and the test file) -> All checks passed.

## Next Phase Readiness
- Tracklists router is fully off the default queue. Plan 04 (scan.py / ingestion.py) remains to complete the phase's per-site sweep using the same `resolve_queue_for_task` chokepoint.

## Self-Check: PASSED

All modified files exist on disk; both task commits (`818f154`, `b986a01`) are present in git history.

---
*Phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual*
*Completed: 2026-06-09*
