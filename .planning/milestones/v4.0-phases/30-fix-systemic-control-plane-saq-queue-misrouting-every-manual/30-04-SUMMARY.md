---
phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual
plan: 04
subsystem: api
tags: [saq, redis, task-queue, fastapi, scan, ingestion, routing]

# Dependency graph
requires:
  - phase: 30-01 (routing foundation)
    provides: "resolve_queue_for_task, NoActiveAgentError, RoutedQueue, public AgentTaskRouter.queue_for"
provides:
  - "POST /api/v1/scan resolves an active agent and feeds run_scan the per-agent phaze-agent-<id> queue"
  - "Zero-active-agent scan returns HTTP 503 instead of silently stranding extraction jobs"
  - "ingestion.run_scan queue contract documented (consumed per-agent queue, never default)"
affects: [scan.py, ingestion.py]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Control-plane auto-enqueue resolves its target queue via resolve_queue_for_task before dispatching background work"
    - "No-active-agent on an auto-enqueue path is a visible 503, not a silent 200"

key-files:
  created: []
  modified:
    - src/phaze/routers/scan.py
    - src/phaze/services/ingestion.py
    - tests/test_routers/test_scan.py

key-decisions:
  - "Zero active agents -> HTTP 503 (discovery's extraction step has no consumer; a 200 would silently strand jobs)"
  - "Test points phaze.routers.scan.async_session at the test engine so the real run_scan enqueue loop runs against the seeded DB"

patterns-established:
  - "Pattern: the legacy /scan path now joins pipeline/tracklists in routing every enqueue through resolve_queue_for_task"

requirements-completed: [QR-01, QR-02]

# Metrics
duration: ~15min
completed: 2026-06-09
---

# Phase 30 Plan 04: Legacy scan auto-enqueue routing Summary

**`POST /api/v1/scan` now selects an active agent and hands `ingestion.run_scan` that agent's consumed `phaze-agent-<id>` queue, so the legacy discovery path's `extract_file_metadata` jobs stop rotting on the removed default queue; with no active agent the endpoint fails loud with a 503.**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-06-09
- **Completed:** 2026-06-09
- **Tasks:** 2
- **Files modified:** 3 (0 created, 3 modified)

## Accomplishments
- `trigger_scan` gained a `session: AsyncSession = Depends(get_session)` and now resolves `resolve_queue_for_task("extract_file_metadata", app.state, session)` after path validation, passing `routed.queue` into `run_scan` exactly where `app.state.queue` used to flow. The dead `app.state.queue` reference is gone (grep count 0).
- The zero-agent case (`NoActiveAgentError`) is translated to `HTTPException(503, ...)` — discovery has no consumer to feed, so the endpoint refuses rather than persisting files whose extraction step would silently strand.
- `ingestion.run_scan`'s `queue` param docstring now states the contract: a consumed per-agent `phaze-agent-<id>` queue resolved by the caller via the enqueue router, never the removed default; `queue is None` remains honored for discovery-only callers/tests.
- Regression coverage: a real `.mp3` in a temp dir drives the actual `run_scan` enqueue loop through a capture-queue fake and asserts `phaze-agent-nox` / `extract_file_metadata` with nothing on `default`; a second test proves the zero-agent path is a 503 with no enqueue.

## Task Commits

1. **Task 1: route /api/v1/scan auto-enqueue to an active agent queue** - `5e7ef89` (fix)
2. **Task 2: regression tests — per-agent enqueue + 503 path** - `0ec50db` (test)

## Files Created/Modified
- `src/phaze/routers/scan.py` (modified) - `trigger_scan` takes a session dependency, resolves the per-agent queue via `resolve_queue_for_task`, raises 503 on `NoActiveAgentError`, and passes `queue=routed.queue` into `run_scan`. Removed `queue = http_request.app.state.queue`.
- `src/phaze/services/ingestion.py` (modified) - documented `run_scan`'s `queue` contract (consumed per-agent queue, never default); no logic change.
- `tests/test_routers/test_scan.py` (modified) - added `_CaptureQueue`/`_CaptureRouter`/`_seed_active_agent`/`_drain_background_scans` helpers; two new behavior tests (named-queue capture + 503); migrated the two `app.state.queue =` tests to the `task_router` capture fake + seeded active agent.

## Decisions Made
- **Zero active agents -> 503:** an explicit failure is correct because discovery's auto-enqueued extraction step has no consumer; a 200 would silently strand work (mitigates T-30-04).
- **Test drives the real `run_scan`:** rather than mocking the enqueue, the capture test monkeypatches `phaze.routers.scan.async_session` to a factory bound to the test `async_engine`, so the genuine discovery + enqueue loop fires against the seeded DB and proves named-queue targeting end to end.

## Deviations from Plan

None - plan executed exactly as written. Rules 1-4 were not triggered; no auth gates occurred.

## Threat Mitigations Realized
- **T-30-01 (DoS / data-integrity, default-queue enqueue loop):** `scan.py` carries zero `app.state.queue` references (grep gate) and the test asserts the enqueue lands on `phaze-agent-nox`, never `default`.
- **T-30-04 (DoS, silent strand on zero agents):** `NoActiveAgentError` -> HTTP 503, asserted by `test_trigger_scan_no_active_agent_returns_503`.
- **T-30-SC (package installs):** accepted — no new packages introduced.

## Issues Encountered
None. All scan-router tests run against the live Postgres present in this worktree (8 passed). This plan touches no SAQ/Redis-dependent path directly — the capture-queue fake substitutes for the real per-agent Queue, so no live Redis was required.

## Verification
- `uv run pytest tests/test_routers/test_scan.py -q` -> 8 passed.
- `grep -c "app.state.queue" src/phaze/routers/scan.py` -> 0.
- `grep -c "app.state.queue =" tests/test_routers/test_scan.py` -> 0.
- `uv run mypy src/phaze/routers/scan.py src/phaze/services/ingestion.py` -> Success.
- `uv run ruff check src/phaze/routers/scan.py src/phaze/services/ingestion.py tests/test_routers/test_scan.py` -> All checks passed.

## Self-Check: PASSED

All three modified files exist on disk; both task commits (`5e7ef89`, `0ec50db`) are present in git history on the worktree branch. Working tree clean apart from this SUMMARY.

---
*Phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual*
*Completed: 2026-06-09*
