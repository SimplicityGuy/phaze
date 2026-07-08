---
phase: quick-260707-ryn
plan: 01
subsystem: config / db-engine / task-dispatch
tags: [pgbouncer, connection-pool, pool-hygiene, config-knobs, dispatch-queue]
requires: []
provides:
  - "Seven PHAZE_-prefixed pool/dispatch knobs on BaseSettings (db_pool_size, db_max_overflow, db_pool_timeout, db_pool_recycle, db_pool_pre_ping, dispatch_queue_min_size, dispatch_queue_max_size)"
  - "api engine + control worker task_engine with pool hygiene sourced from config"
  - "control-side per-(agent,lane) dispatch queues sized 0/2 from config"
affects:
  - src/phaze/config.py
  - src/phaze/database.py
  - src/phaze/tasks/controller.py
  - src/phaze/services/agent_task_router.py
tech-stack:
  added: []
  patterns:
    - "Field + AliasChoices(PHAZE_<NAME>, <name>) knob, mirroring the lane-concurrency knobs"
    - "SQLAlchemy engine pool_pre_ping / pool_recycle / pool_timeout hygiene"
    - "SAQ PostgresQueue sizing read off queue.min_size/queue.max_size pre-connect"
key-files:
  created: []
  modified:
    - src/phaze/config.py
    - src/phaze/database.py
    - src/phaze/tasks/controller.py
    - src/phaze/services/agent_task_router.py
    - tests/shared/core/test_config_worker.py
    - tests/shared/core/test_database.py
    - tests/shared/tasks/test_controller_startup_banner.py
    - tests/agents/services/test_agent_task_router.py
    - tests/agents/tasks/test_agent_worker_lanes.py
decisions:
  - "Assert SAQ queue-level min_size/max_size (not queue.pool.*) at construction time — the psycopg pool only takes the configured sizing at connect() via pool.resize()"
metrics:
  duration: ~15m
  completed: 2026-07-07
  tasks: 3
  files: 9
---

# Phase quick-260707-ryn Plan 01: Lean DB Connection Footprint / Pool Hygiene Summary

Leaned phaze's PgBouncer session-mode server-connection footprint by reducing SQLAlchemy engine
pool sizes, adding liveness hygiene (pre_ping / recycle / bounded acquire timeout), and trimming
the control-side per-(agent,lane) dispatch-queue pools — every value sourced from a new
PHAZE_-prefixed config knob so operators can re-tune without a code change.

## Objective

Stop the shared (phaze,phaze) session-mode pool (cap ~55) from deadlocking under normal
multi-worker load (which hangs `/health` behind the exhausted pool). In session mode every
app→pooler connection pins one upstream server connection for its whole lifetime, so the fix
reduces how many server connections phaze holds AND frees stale/idle slots instead of pinning
them. Homelab is raising the pooler cap to ~80 in parallel — these app-side reductions are
HEADROOM, not a hard fit.

## What Was Built

### Task 1 — Seven pool/dispatch knobs on BaseSettings (commit `3cf612eb`)
Added to `BaseSettings` (the shared base, reachable by both the module-level `settings`
singleton and `get_settings()`), mirroring the existing `scan_stall_seconds` /
lane-concurrency `Field + AliasChoices` pattern:

| Knob | Default | Alias |
|------|---------|-------|
| `db_pool_size` | 5 | `PHAZE_DB_POOL_SIZE` |
| `db_max_overflow` | 5 | `PHAZE_DB_MAX_OVERFLOW` |
| `db_pool_timeout` | 10 | `PHAZE_DB_POOL_TIMEOUT` |
| `db_pool_recycle` | 1800 | `PHAZE_DB_POOL_RECYCLE` |
| `db_pool_pre_ping` | True | `PHAZE_DB_POOL_PRE_PING` |
| `dispatch_queue_min_size` | 0 | `PHAZE_DISPATCH_QUEUE_MIN_SIZE` |
| `dispatch_queue_max_size` | 2 | `PHAZE_DISPATCH_QUEUE_MAX_SIZE` |

The block carries the incident comment (PgBouncer session-mode 55-cap deadlock; homelab raising
the cap to ~80 in parallel = headroom). Two tests: a defaults assertion + an env-alias
assertion (incl. the `PHAZE_DB_POOL_PRE_PING=false` bool case).

### Task 2 — Pool hygiene into the api engine + control worker task_engine (commit `f8a8aad3`)
- `database.py`: the module-level `engine` now sources all five pool kwargs from `settings`
  (`pool_size=5`, `max_overflow=5` — was a hardcoded 10, `pool_timeout=10`, `pool_recycle=1800`,
  `pool_pre_ping=True`). The `from phaze.config import settings` import is unchanged.
- `controller.py`: `startup(ctx)`'s `task_engine` sources the same five kwargs from `cfg`
  (`pool_size` 10→5, `max_overflow=5`, plus the three hygiene kwargs). Same incident comment.
- Tests: a live-engine assertion on `engine.pool.size()/._max_overflow/._timeout/._recycle/._pre_ping`,
  and a capturing-fake `create_async_engine` in the controller banner test asserting the
  recorded kwargs equal the config values.

### Task 3 — Dispatch-queue sizing from config + agent-worker regression guard (commit `20f82c9b`)
- `agent_task_router.py`: added `from phaze.config import get_settings`; `_queue_for` reads
  `dispatch_queue_min_size` (0) / `dispatch_queue_max_size` (2) from `get_settings()` (lru_cached)
  in place of the hardcoded 1/4. Covers all callers (`queue_for`, `all_lane_queues`,
  `legacy_base_queue`). The 1/4 comment block was updated to the producer-pool rationale + the
  incident reference (RESEARCH Pitfall 4 max_connections note retained).
- REGRESSION test proves the agent worker's OWN drain queue (`agent_worker.py:354`) stays 1/4 —
  `agent_worker.py` was NOT touched.
- The `controller_queue` (controller.py:265 and main.py:117-118) stays 2/8 — untouched.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Incorrect assertion target] Assert SAQ queue-level min_size/max_size, not queue.pool.***
- **Found during:** Task 3
- **Issue:** The plan's `<behavior>` and `<interfaces>` claimed a constructed (open=False) queue
  reports `queue.pool.min_size==0` / `queue.pool.max_size==2`. Empirically, SAQ's
  `PostgresQueue.__init__` stores the configured sizing on `self.min_size`/`self.max_size` and
  builds the psycopg `AsyncConnectionPool` WITHOUT those values; the pool is only resized to the
  configured sizing inside `connect()` (`pool.resize(...)`). So an unopened queue's
  `pool.min_size` reports psycopg's default (4), which made the plan's assertion fail (observed
  4/4).
- **Fix:** Both new tests assert on the SAQ queue-level attributes (`queue.min_size` /
  `queue.max_size`) that carry the configured sizing pre-connect — keeping the check socket-free
  while still proving the config values flowed through. The runtime wiring (passing
  `min_size`/`max_size` into `build_pipeline_queue`) is exactly as the plan specified; only the
  test-assertion attribute changed.
- **Files modified:** tests/agents/services/test_agent_task_router.py, tests/agents/tasks/test_agent_worker_lanes.py
- **Commit:** `20f82c9b`

## Verification

- `uv run ruff check` + `uv run ruff format --check` on all touched files: clean.
- `uv run mypy` on the four touched src modules (config.py, database.py, controller.py,
  agent_task_router.py): `Success: no issues found in 4 source files` (agent_task_router.py's
  strict override is now active).
- Targeted pytest (`-m "not integration"`) across all five test modules: **34 passed, 7 deselected**.
- Grep confirms sourcing (not hardcoding): `settings.db_pool_size` (database.py:38),
  `cfg.db_pool_size` (controller.py:99), `cfg.dispatch_queue_min/max_size` (agent_task_router.py:154-155).
- Grep confirms `min_size=1, max_size=4` still matches `agent_worker.py:354` (unchanged).
- Grep confirms `controller_queue` stays 2/8 (controller.py:265, main.py:117-118 unchanged).
- pre-commit ran on every task's files (no `--no-verify`); all hooks passed.

No live test DB was needed — every touched test is DB-free: config parsing, lazy
`create_async_engine` (no connect), mocked alembic/engine, and `open=False` SAQ queue
construction.

## Known Stubs

None.

## Self-Check: PASSED
- Files: config.py, database.py, controller.py, agent_task_router.py all FOUND.
- Commits: `3cf612eb`, `f8a8aad3`, `20f82c9b` all FOUND.
