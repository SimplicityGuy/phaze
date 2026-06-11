---
phase: 32-pipeline-reboot-resilience-re-enqueue
plan: 03
subsystem: tasks
tags: [saq, reboot-resilience, re-enqueue, controller, cron, wave-3]
requires:
  - "src/phaze/tasks/reenqueue.py::reenqueue_discovered (Wave-2 control-only recovery task)"
  - "src/phaze/services/agent_task_router.py::AgentTaskRouter (per-agent queue router + close())"
  - "src/phaze/config.py::get_settings().redis_url"
provides:
  - "src/phaze/tasks/controller.py::startup stashes ctx['task_router'] = AgentTaskRouter(cfg.redis_url) and calls reenqueue_discovered(ctx) once on boot (guarded)"
  - "src/phaze/tasks/controller.py::shutdown closes ctx['task_router']"
  - "src/phaze/tasks/controller.py::settings registers reenqueue_discovered in functions + CronJob(reenqueue_discovered, cron='*/5 * * * *')"
affects:
  - "Controller worker boot/cron now resumes DISCOVERED-file analysis automatically after a reboot or Redis flush, with no manual 'Run Analysis'"
tech-stack:
  added: []
  patterns:
    - "Mirror the ctx['discogs_client'] create-in-startup / close-in-shutdown lifecycle for the single reused AgentTaskRouter (RESEARCH Pitfall 4 -- never construct per cron tick)"
    - "Boot-time recovery call wrapped in a broad try/except + logger.exception so a re-enqueue failure can never abort controller boot (RESEARCH Pitfall 3)"
    - "5-min CronJob for mid-run stall recovery alongside the 1-min reaper; 5 min balances recovery latency vs DB load since re-enqueue scans all DISCOVERED rows"
key-files:
  created:
    - tests/test_tasks/test_controller_reenqueue.py
  modified:
    - src/phaze/tasks/controller.py
decisions:
  - "Reused the cfg already bound at the top of startup for AgentTaskRouter(cfg.redis_url) rather than re-calling get_settings()"
  - "Stash + boot call placed AFTER ctx['queue'] = queue so the task_router exists before reenqueue_discovered routes; router built once, closed in shutdown"
  - "Boot-time call guarded by try/except Exception even though the Wave-2 task already swallows NoActiveAgentError -- defense in depth for boot resilience (T-32-04)"
  - "Cron set to '*/5 * * * *' (every 5 min), distinct from the reaper's '* * * * *', per CONTEXT Claude's Discretion on recovery-latency vs DB-load balance"
requirements: [RESIL-01, RESIL-02]
metrics:
  duration: ~20m
  completed: 2026-06-11
  tasks: 2
  files: 2
---

# Phase 32 Plan 03: Controller Re-enqueue Wiring Summary

Wired the Wave-2 `reenqueue_discovered` recovery task into the controller worker so reboot recovery is automatic: a single `AgentTaskRouter` is stashed in `ctx` at startup (closed at shutdown), the re-enqueue runs once on boot (immediate post-reboot recovery), and a 5-minute `CronJob` catches mid-run stalls without a restart. Without this wiring the Wave-2 task was dead code.

## What Was Built

- **`src/phaze/tasks/controller.py`** (modified):
  - Added imports `from phaze.services.agent_task_router import AgentTaskRouter` and `from phaze.tasks.reenqueue import reenqueue_discovered` (isort-sorted into the first-party block).
  - In `startup(ctx)`, after `ctx["queue"] = queue`: construct `ctx["task_router"] = AgentTaskRouter(cfg.redis_url)` (reusing the `cfg` already bound), then `await reenqueue_discovered(ctx)` once, logging the returned `reenqueued`/`skipped` counts at INFO. The boot-time call is wrapped in a broad `try/except Exception` that logs `logger.exception("reenqueue on startup failed")` and continues — a re-enqueue failure can never abort controller boot.
  - In `shutdown(ctx)`, mirror the `discogs_client` cleanup: `task_router = ctx.get("task_router")` then `if task_router is not None: await task_router.close()`.
  - In the module-level `settings` dict: appended `reenqueue_discovered` to `functions`, and added `CronJob(reenqueue_discovered, cron="*/5 * * * *")` to `cron_jobs` (5-field standard form, matching the existing `# type: ignore[type-var]` style), with a comment explaining the 5-min cadence.
- **`tests/test_tasks/test_controller_reenqueue.py`** (created): 6 tests in two groups:
  - Registration: `reenqueue_discovered` is in `settings["functions"]`; exactly one `CronJob` whose `function is reenqueue_discovered` with `cron == "*/5 * * * *"`; and the existing `reap_stalled_scans` + `refresh_tracklists` crons remain (no regression).
  - Startup behavior: monkeypatching the heavyweight constructors plus `controller.AgentTaskRouter` (router stub with async `close()` + `queue_for`) and `controller.reenqueue_discovered` (`AsyncMock` returning `{"reenqueued": 3, "skipped": 1}`), assert `startup` stashes the stub in `ctx["task_router"]` and awaits `reenqueue_discovered(ctx)` exactly once; `shutdown` awaits `task_router.close()`; and a raising `reenqueue_discovered` does NOT abort `startup`.

## Why It Matters

This is the wiring that makes Phase 32 real: the startup call is the "no manual Run Analysis after reboot" guarantee (Redis is empty after a reboot, so every DISCOVERED file re-enqueues), and the 5-min cron is the "catch mid-run stalls without a restart" guarantee. Building the `AgentTaskRouter` once and closing it at shutdown avoids the per-tick Redis connection-pool leak (T-32-06), and the guarded boot call guarantees a re-enqueue exception can never put the controller into a boot/restart loop (T-32-04).

## Deviations from Plan

None — plan executed exactly as written.

## TDD Gate Compliance

These tasks are not marked `tdd="true"`. The plan deliberately separates the implementation (Task 1, `feat`) from the behavioral/registration test module (Task 2, `test`); they were committed in plan order. All Task-2 tests were authored against the Task-1 wiring and pass.

## Verification

- `uv run pytest tests/test_tasks/test_controller_reenqueue.py tests/test_tasks/test_controller_startup_banner.py -q` → 9 passed (no startup-banner regression).
- `uv run mypy src/phaze/tasks/controller.py` → clean; `uv run ruff check src/phaze/tasks/controller.py` → clean.
- Grep gates: `reenqueue_discovered` matches import + startup call + functions entry + CronJob; `task_router` matches startup stash + shutdown close; `cron="*/5 * * * *"` matches the new CronJob.
- Full suite (`uv run pytest --cov`): the 9 failures + 42 errors are all in Redis/Postgres-dependent suites (`test_agent_task_router`, `test_execution_dispatch`, `test_agent_tracklists`) and are environmental — `redis.exceptions.ConnectionError` at `localhost:6379` (no live Redis/DB in this worktree). They are pre-existing and unrelated to this plan's files (controller.py + the new test module). 1620 tests passed.

## Deferred Issues

- The Redis/Postgres-dependent suites error/fail when no broker is reachable rather than skipping cleanly (unlike Plan 02's integration test). This is an environmental harness gap outside this plan's scope (logged here, not fixed) — those suites do not touch controller.py.

## Threat Surface

No new trust boundary: the controller boot/cron performs internal enqueues onto per-agent Redis queues (no new external input/auth/endpoint).
- **T-32-04 (re-enqueue exception aborting boot → restart loop)** mitigated: boot-time call wrapped in `try/except Exception` + `logger.exception`; `test_startup_survives_raising_reenqueue` asserts startup survives a raising re-enqueue.
- **T-32-06 (per-tick AgentTaskRouter leaking Redis pools)** mitigated: single router built in startup, stashed in ctx, `close()`d in shutdown; `test_startup_stashes_router_and_calls_reenqueue_once` + `test_shutdown_closes_task_router` pin the lifecycle.
- **T-32-SC**: no package installs.

## Commits

- `92f9048` feat(32-03): wire reenqueue_discovered into controller startup + cron
- `6e4bcab` test(32-03): controller re-enqueue wiring + cron/functions registration

## Self-Check: PASSED
- FOUND: src/phaze/tasks/controller.py
- FOUND: tests/test_tasks/test_controller_reenqueue.py
- FOUND: commit 92f9048
- FOUND: commit 6e4bcab
