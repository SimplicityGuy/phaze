---
phase: 43-analyze-throughput-fix
plan: 01
subsystem: tasks/process-pool
tags: [pebble, process-pool, timeout, kill-on-timeout, config, agent-worker]
requires: []
provides:
  - "killable pebble ProcessPool with per-task timeout + kwargs passthrough (run_in_process_pool)"
  - "AgentSettings.analysis_inner_timeout_sec (6600) / analysis_fine_cap (60) / analysis_coarse_cap (30)"
  - "pebble>=5.2.0 dependency"
affects:
  - "src/phaze/tasks/functions.py (process_file calls run_in_process_pool — now timeout-capable)"
  - "Plan 02 analyze_file (consumes analysis_fine_cap/analysis_coarse_cap)"
  - "Plan 04 agent worker wiring (consumes analysis_inner_timeout_sec)"
tech-stack:
  added: ["pebble>=5.2.0"]
  patterns: ["asyncio.wrap_future bridge over pebble ProcessFuture", "max_tasks=1 worker recycling", "PHAZE_* AliasChoices config knobs"]
key-files:
  created: []
  modified:
    - pyproject.toml
    - uv.lock
    - src/phaze/config.py
    - src/phaze/tasks/pool.py
    - src/phaze/tasks/agent_worker.py
    - tests/test_tasks/test_pool.py
    - tests/test_tasks/test_agent_startup_banner.py
decisions:
  - "Build pebble schedule() call dynamically (list(args); omit timeout when None) to satisfy pebble's strict-but-loose inline annotations (args: list, timeout: float) under mypy strict — no type: ignore needed."
  - "Timeout-kills-child test uses a module-level sleeper that records its PID, then asserts os.kill(pid, 0) raises ProcessLookupError (child reaped) plus a fresh task succeeds on the recycled pool — no real essentia, deterministic 5s inner timeout."
metrics:
  duration_min: 11
  completed: 2026-06-18
  tasks_total: 3
  tasks_implemented: 2
---

# Phase 43 Plan 01: Killable pebble ProcessPool Summary

Replaced the un-killable `concurrent.futures.ProcessPoolExecutor` with a `pebble.ProcessPool` (`max_tasks=1`) so a runaway essentia child exceeding the inner per-task timeout is SIGKILLed and its pool slot reclaimed; added three `AgentSettings` knobs (inner timeout + window caps) for Plans 02/04 to consume.

## What Was Built

- **pebble dependency** (`pyproject.toml`, `uv.lock`): `pebble>=5.2.0`, placed alphabetically in `[project].dependencies`. Pure-Python wheel, ships `py.typed` (no mypy override required).
- **Three AgentSettings knobs** (`src/phaze/config.py`): `analysis_inner_timeout_sec` (default 6600, alias `PHAZE_ANALYSIS_INNER_TIMEOUT_SEC` — kept below the 7200s SAQ `process_file` net so the kill is deterministic), `analysis_fine_cap` (60, `PHAZE_ANALYSIS_FINE_CAP`), `analysis_coarse_cap` (30, `PHAZE_ANALYSIS_COARSE_CAP`). All mirror the existing `analysis_fine_window_sec` Field + AliasChoices pattern.
- **Killable pool** (`src/phaze/tasks/pool.py`): `create_process_pool()` → `ProcessPool(max_workers=settings.worker_process_pool_size, max_tasks=1)`. `run_in_process_pool(ctx, func, *args, timeout=None, **kwargs)` schedules on pebble and awaits `asyncio.wrap_future(future)`; a child exceeding `timeout` is SIGKILLed and the future raises `builtins.TimeoutError`.
- **Shutdown hook** (`src/phaze/tasks/agent_worker.py`): replaced `pool.shutdown(wait=True)` with `pool.stop()` + `pool.join()` (pebble API).
- **Tests** (`tests/test_tasks/test_pool.py`): pebble-import smoke, three config-default + env-alias tests, pool-is-pebble, kwargs passthrough, execute, and a real-pebble timeout-kills-child test that asserts the child PID is reaped and the recycled pool runs a fresh task. Updated `test_shutdown_closes_pool_engines_and_client` for the stop/join API.

## TDD Gate Compliance

Both implementation tasks followed RED → GREEN:
- Task 2: config-knob + import tests failed (ModuleNotFound + missing knobs) → `uv add` + knobs → green.
- Task 3: rewritten pool tests failed against the old `ProcessPoolExecutor` (`AttributeError: 'ProcessPoolExecutor' object has no attribute 'stop'`) → pool.py rewrite → green.

## Checkpoint: Task 1 (blocking-human legitimacy gate)

Task 1 was a `checkpoint:human-verify` `gate="blocking-human"` requiring human approval of `pebble`'s supply-chain provenance before `uv add`. **The orchestrator confirmed the human approved this before dispatch** (resume-signal "approved"). Verification evidence: Pebble 5.2.0, `requires_python >=3.8`, source github.com/noxdafox/pebble (Matteo Cafasso, LGPL), 100+ releases, pure-Python wheel with no compiled extension or post-install scripts — legitimate, not a typosquat. Install proceeded as approved; `pebble==5.2.0` resolved and installed.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated stale shutdown assertion in test_agent_startup_banner.py**
- **Found during:** Task 3 (broader test run)
- **Issue:** `test_shutdown_closes_pool_engines_and_client` asserted `pool.shutdown.assert_called_once_with(wait=True)`, which is the API this plan replaces.
- **Fix:** Asserted `pool.stop()` and `pool.join()` are each called once; updated the docstring.
- **Files modified:** tests/test_tasks/test_agent_startup_banner.py
- **Commit:** af266c9

**2. [Rule 3 - Blocking] pebble strict-stub mypy errors**
- **Found during:** Task 3
- **Issue:** pebble's inline annotations are loose-but-strict (`args: list = ()`, `timeout: float = None`); passing a tuple / `float | None` failed mypy strict.
- **Fix:** Build the `schedule()` call dynamically — `args=list(args)` and only include `timeout` when not None — instead of a `# type: ignore`.
- **Files modified:** src/phaze/tasks/pool.py
- **Commit:** af266c9

## Verification

- `uv run pytest tests/test_tasks/test_pool.py -q` → 7 passed (incl. timeout-kills-child).
- `uv run pytest tests/test_tasks/test_pool.py tests/test_tasks/test_agent_startup_banner.py tests/test_tasks/test_functions.py tests/test_phase04_gaps.py -q` → 34 passed.
- `uv run mypy src/phaze/tasks/pool.py src/phaze/tasks/agent_worker.py` → clean.
- `uv run ruff check` on all touched files → clean.
- `grep -c "pool.shutdown" src/phaze/tasks/agent_worker.py` → 0; `pool.stop()`/`pool.join()` present.
- pre-commit hooks ran on every commit (no `--no-verify`).

## Notes / Out of Scope

- `tests/test_tasks/test_recovery.py` and `tests/test_tasks/test_scan_reaper.py` error at setup with SQLAlchemy connection failures (no Postgres in this sandbox). Pre-existing and unrelated to this plan's changes (they require a live DB) — not addressed per the scope boundary.
- The two new caps (`analysis_fine_cap`/`analysis_coarse_cap`) and the inner timeout are config-only here; they are wired into behavior by Plans 02 (`analyze_file`) and 04 (worker). This is intentional per the plan, not a stub.

## Self-Check: PASSED
