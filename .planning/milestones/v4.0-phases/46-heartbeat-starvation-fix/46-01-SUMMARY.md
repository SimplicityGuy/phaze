---
phase: 46-heartbeat-starvation-fix
plan: 01
subsystem: infra
tags: [saq, asyncio, heartbeat, liveness, agent-worker, postgres-broker, background-task]

# Dependency graph
requires:
  - phase: 29-distributed-agents
    provides: heartbeat_tick cron handler, agent liveness classifier, AGENT_LIVENESS_* thresholds
  - phase: 43-analyze-throughput
    provides: pebble ProcessPool for essentia (frees the event loop so a background task can tick)
  - phase: 36-saq-postgres-broker
    provides: Postgres saq_jobs broker (where the orphaned cron:heartbeat_tick row lives)
provides:
  - "send_heartbeat(ctx): reusable single-POST heartbeat coroutine"
  - "_heartbeat_loop(ctx): starvation-proof asyncio background loop ticking every AGENT_HEARTBEAT_INTERVAL_SECONDS"
  - "agent worker launches ctx['heartbeat_task'] in startup, cancels it in shutdown"
  - "heartbeat decoupled from the SAQ worker_max_jobs dispatch pool (no more CronJob)"
  - "AGENT_HEARTBEAT_INTERVAL_SECONDS=30 constant (single source of cadence)"
affects: [agent-liveness, agent-worker, deployment-runbook]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Liveness signals run as in-process asyncio background tasks, NOT SAQ CronJobs, so they cannot be starved by the job-dispatch semaphore"
    - "Per-iteration broad try/except that re-raises CancelledError but logs+continues on Exception (a dead loop = a silently DEAD agent)"

key-files:
  created:
    - tests/test_tasks/test_heartbeat_loop.py
    - tests/test_tasks/test_agent_worker_heartbeat.py
  modified:
    - src/phaze/constants.py
    - src/phaze/tasks/heartbeat.py
    - src/phaze/tasks/agent_worker.py
    - docs/architecture.md
    - docs/deployment.md
    - docs/configuration.md

key-decisions:
  - "Heartbeat runs as an asyncio background task in startup (cancelled in shutdown), not a SAQ CronJob — a CronJob competes for worker_max_jobs slots and is starved by multi-hour process_file jobs (Phase 46 incident)"
  - "heartbeat_tick retained as a thin back-compat shim delegating to send_heartbeat (keeps existing direct-call tests + documented public surface)"
  - "send_heartbeat reads ctx['worker'] lazily INSIDE the queue try/except so a not-yet-attached worker degrades to queue_depth=0 and still POSTs"
  - "cron_jobs key dropped entirely from settings (SAQ treats it as optional); heartbeat_tick removed from functions (no longer SAQ-dispatched)"
  - "Documented a one-time operator DELETE of the orphaned cron:heartbeat_tick row from saq_jobs after redeploy"

patterns-established:
  - "Background liveness loop pattern: while True -> try send/except CancelledError re-raise/except Exception log+continue -> sleep(interval)"

requirements-completed: []

# Metrics
duration: ~20min
completed: 2026-06-23
---

# Phase 46 Plan 01: Heartbeat Starvation Fix Summary

**The agent liveness heartbeat now runs as an in-process asyncio background task (launched in the worker startup hook, cancelled on shutdown) instead of a SAQ CronJob, so a worker with all `worker_max_jobs` dispatch slots saturated by multi-hour `process_file` jobs still POSTs a heartbeat every 30s and is never wrongly marked DEAD.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-06-23T18:23:00Z
- **Completed:** 2026-06-23T18:43:00Z
- **Tasks:** 3 (two TDD)
- **Files modified:** 6 modified, 2 test files created

## Accomplishments
- Extracted the heartbeat body into a reusable `send_heartbeat(ctx)` coroutine and added `_heartbeat_loop(ctx)`, a starvation-proof background loop that ticks every `AGENT_HEARTBEAT_INTERVAL_SECONDS` (30s) entirely on the event loop — never acquiring a SAQ dispatch slot.
- Wired the loop into the agent worker: `startup` launches `ctx["heartbeat_task"] = asyncio.create_task(_heartbeat_loop(ctx))` after `api_client`/`agent_identity` are set; `shutdown` cancels + awaits it first (CancelledError suppressed) before closing the api_client. Removed the heartbeat `CronJob` and the `heartbeat_tick` function registration from `settings`, plus the now-unused `saq.CronJob` import and its `type: ignore`.
- Preserved every pre-existing defensive branch verbatim (ctx-not-init guard, `queue.info()`->0 degrade, AgentApiError->WARNING+continue, DEBUG "heartbeat sent"), and hardened the queue read so a missing `ctx["worker"]` also degrades to `queue_depth=0`.
- Proved starvation-independence, loop-survives-exception, clean CancelledError propagation, and the no-`worker`-key degrade with new tests; kept the Phase 26 D-25 import boundary intact (verified by `tests/test_task_split.py`).
- Updated architecture/deployment/configuration docs to describe the background-task mechanism + the one-time `cron:heartbeat_tick` orphaned-row operator cleanup.

## Task Commits

Each task was committed atomically (TDD tasks use test -> feat):

1. **Task 1: Refactor into send_heartbeat + starvation-proof loop**
   - `1409822` (test — RED)
   - `90fa3f1` (feat — GREEN)
2. **Task 2: Launch heartbeat in startup, cancel in shutdown, remove CronJob**
   - `e754742` (test — RED)
   - `3ecdfdb` (feat — GREEN)
3. **Task 3: Document the background-heartbeat mechanism + cron-row cleanup**
   - `ec28b1f` (docs)

## Files Created/Modified
- `src/phaze/constants.py` - Added `AGENT_HEARTBEAT_INTERVAL_SECONDS = 30` (single source of cadence); updated `AGENT_LIVENESS_ALIVE_SECONDS` docstring to reference it (3x cadence).
- `src/phaze/tasks/heartbeat.py` - `send_heartbeat` + `_heartbeat_loop`; `heartbeat_tick` now a thin shim; lazy `ctx["worker"]` read degrades to 0.
- `src/phaze/tasks/agent_worker.py` - startup launches `ctx["heartbeat_task"]`; shutdown cancels it; CronJob + `heartbeat_tick` registration removed; unused imports dropped.
- `tests/test_tasks/test_heartbeat_loop.py` - starvation-independence, loop-survives-exception, CancelledError, no-worker-key tests.
- `tests/test_tasks/test_agent_worker_heartbeat.py` - no cron / not-in-functions / startup-launches / shutdown-cancels tests.
- `docs/architecture.md`, `docs/deployment.md`, `docs/configuration.md` - background-task mechanism + Phase 46 rationale + `cron:heartbeat_tick` cleanup runbook; thresholds unchanged.

## Decisions Made
See `key-decisions` frontmatter. Summary: background task over CronJob (decisive root-cause fix), shim retained for back-compat, lazy worker read for early-tick safety, cron_jobs key dropped entirely, operator cron-row DELETE documented.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Two pre-commit ruff TC002/TC003 nudges on the new test files (move `Callable`/`pytest` into `TYPE_CHECKING` blocks) — applied and re-committed. Normal lint-loop, not a logic issue.
- `ruff` SIM105 on the shutdown cancel block — switched the `try/except CancelledError: pass` to `contextlib.suppress(asyncio.CancelledError)` (added `import contextlib`). Functionally identical.
- Full `tests/test_tasks/` coverage run surfaced 23 pre-existing OSError errors in `test_recovery.py` / `test_scan_reaper.py` (require a live Postgres broker) — out of scope for this phase, logged here, not modified. The plan's targeted verification set (heartbeat + agent_worker + import-boundary) is fully green.

## Verification

- `uv run pytest tests/test_tasks/test_heartbeat_loop.py tests/test_tasks/test_heartbeat_cron.py tests/test_tasks/test_heartbeat_failure.py tests/test_tasks/test_agent_worker_heartbeat.py tests/test_task_split.py -x` → 21 passed.
- Coverage on changed modules: `heartbeat.py` 97.5%, `agent_worker.py` 96.15% (project gate 85%). Only uncovered line is the defensive `raise` in the loop's `except CancelledError` branch (propagation proven end-to-end by `test_heartbeat_loop_reraises_cancelled`).
- `uv run ruff check .` clean; `uv run ruff format --check .` clean (350 files); `uv run mypy .` clean (160 files); `pre-commit run --all-files` all hooks pass.
- `grep -v '^#' src/phaze/tasks/agent_worker.py | grep -c "CronJob(heartbeat_tick"` → 0; `grep -c "create_task(_heartbeat_loop" src/phaze/tasks/agent_worker.py` → 1.

## User Setup Required
None - no external service configuration required.

**Operator note (post-redeploy, one-time):** after deploying the Phase 46 agent image, run `DELETE FROM saq_jobs WHERE key = 'cron:heartbeat_tick';` against `PHAZE_QUEUE_URL` to purge the orphaned `unique=True` cron row left by the prior build. Harmless if absent. Documented in `docs/deployment.md`.

## Next Phase Readiness
- Liveness is now guaranteed independent of job cost; the busy-agent-DEAD incident class is closed in code. Needs a release + nox/lux redeploy to take effect live, then the one-time cron-row cleanup.
- This is a homelab-reliability fix on a phase branch; open a PR per project workflow (no direct main commits).

## Self-Check: PASSED

All 6 key files exist on disk and all 5 task commits (1409822, 90fa3f1, e754742, 3ecdfdb, ec28b1f) are present in git history.

---
*Phase: 46-heartbeat-starvation-fix*
*Completed: 2026-06-23*
