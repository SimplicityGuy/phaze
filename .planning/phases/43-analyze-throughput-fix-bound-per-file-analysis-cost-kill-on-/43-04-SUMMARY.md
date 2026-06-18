---
phase: 43-analyze-throughput-fix
plan: 04
subsystem: agent-worker
tags: [saq, pebble, timeout, retry-policy, terminal-classification, coverage-forwarding]
requires:
  - "43-01: run_in_process_pool(timeout, **kwargs) killable pebble pool + AgentSettings.analysis_inner_timeout_sec/fine_cap/coarse_cap"
  - "43-02: analyze_file(fine_cap, coarse_cap) emits the five coverage fields"
  - "43-03: PhazeAgentClient.report_analysis_failed + AnalysisFailurePayload + the five coverage fields on AnalysisWritePayload"
provides:
  - "enqueue policy timeout=7200 + retries=2 (one real retry) for process_file"
  - "process_file terminal classification: TimeoutError/ProcessExpired -> report + COMPLETE (no retry)"
  - "process_file threads inner timeout + 60/30 caps from AgentSettings into the pool"
  - "process_file forwards the five coverage fields to put_analysis"
affects:
  - "src/phaze/services/analysis_enqueue.py (both process_file producers funnel through it)"
  - "src/phaze/tasks/functions.py (agent worker process_file body)"
tech-stack:
  added: []
  patterns:
    - "agent-only settings (analysis_*) resolved via get_settings() + AgentSettings narrowing, NOT the ControlSettings-typed module singleton"
    - "pebble TimeoutError/ProcessExpired classified terminal -> normal return so SAQ marks the job COMPLETE"
key-files:
  created: []
  modified:
    - "src/phaze/services/analysis_enqueue.py"
    - "src/phaze/tasks/functions.py"
    - "tests/test_services/test_analysis_enqueue.py"
    - "tests/test_tasks/test_functions.py"
decisions:
  - "Resolve agent-only analysis_* settings via get_settings()+AgentSettings narrowing (the module-level settings singleton is ControlSettings-typed and lacks them) — deviation from the plan's literal 'settings.analysis_inner_timeout_sec' hint, which would AttributeError at runtime."
metrics:
  duration: "~10m"
  completed: "2026-06-18"
  tasks: 2
  files: 4
---

# Phase 43 Plan 04: Wire the Worker Side (Terminal Timeout + Coverage Forwarding) Summary

Lowered the SAQ `process_file` outer timeout to 7200s (keeping `retries=2`), and made `process_file` treat an inner pebble `TimeoutError`/`ProcessExpired` as terminal — reporting `ANALYSIS_FAILED` via the Plan 03 endpoint and returning normally so SAQ marks the job COMPLETE (no wasteful re-run of a deterministically-too-long file). It threads the inner timeout + 60/30 caps from `AgentSettings` into the killable pool and forwards the five coverage fields to `put_analysis`.

## What Was Built

### Task 1 — Enqueue policy: timeout 14400 -> 7200, keep retries=2 (commit `9deecaf`)
- `src/phaze/services/analysis_enqueue.py`: `timeout=7200` (outer SAQ safety net; the inner pebble `analysis_inner_timeout_sec`=6600 does the deterministic kill first — RESEARCH Pitfall 2). `retries=2` preserved verbatim (NOT 1 — Pitfall 1: SAQ default 1 gets clobbered to `worker_max_retries`=4 by `apply_project_job_defaults`).
- Tests assert the emitted job carries `timeout==7200`/`retries==2`, and a real `saq.Job(timeout=7200, retries=2)` survives `apply_project_job_defaults` unchanged.

### Task 2 — Terminal classification + coverage forwarding (commit `dd828a8`)
- `src/phaze/tasks/functions.py`: wrapped `run_in_process_pool(...)` in try/except:
  - `except TimeoutError:` -> `report_analysis_failed(reason="timeout")`, return `{"status": "analysis_failed"}` (normal return -> SAQ COMPLETE -> no retry).
  - `except ProcessExpired:` (imported from `pebble`) -> same, `reason="crashed"`.
  - `except Exception:` -> report `reason="error"` (truncated to 2000 chars) ONLY when `ctx["job"]` exists and is not retryable, then re-raise; a retryable attempt (and the no-job case) re-raises silently so the one real retry runs.
- Threaded `timeout=cfg.analysis_inner_timeout_sec`, `fine_cap=cfg.analysis_fine_cap`, `coarse_cap=cfg.analysis_coarse_cap` into the pool call.
- Forwarded the five coverage fields (`fine_windows_analyzed`/`fine_windows_total`/`coarse_windows_analyzed`/`coarse_windows_total`/`sampled`) into `AnalysisWritePayload` on the success path.
- Added tests for every branch (timeout terminal, ProcessExpired terminal, non-retryable-reports-then-raises, retryable-raises-silently, no-job-raises-silently, caps/timeout threading, coverage forwarded, coverage default-None).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Agent-only `analysis_*` settings must be read via `get_settings()`, not the module-level `settings` singleton**
- **Found during:** Task 2 (RED run surfaced `AttributeError: 'ControlSettings' object has no attribute 'analysis_inner_timeout_sec'`).
- **Issue:** The plan's `<interfaces>` hint said `settings.analysis_inner_timeout_sec`. But `analysis_inner_timeout_sec`/`analysis_fine_cap`/`analysis_coarse_cap` are defined on `AgentSettings`, while the module-level `from phaze.config import settings` singleton is `ControlSettings`-typed (config.py docstring is explicit: agent workers must use `get_settings()`/`AgentSettings()` directly). Following the literal hint would have raised `AttributeError` at runtime inside the agent worker. (Plan 01's `pool.py` only reads `settings.worker_process_pool_size`, which lives on the shared `BaseSettings`, so its `from phaze.config import settings` is fine — the agent-only fields are the difference.)
- **Fix:** Added a small `_agent_settings()` helper in `functions.py` that calls `get_settings()` and narrows via `isinstance(cfg, AgentSettings)` (mirroring the `agent_worker.startup` invariant), and read the three knobs off that instance. `process_file` is registered only on the agent worker (`PHAZE_ROLE=agent`), so `get_settings()` returns `AgentSettings` in production.
- **Files modified:** `src/phaze/tasks/functions.py`, `tests/test_tasks/test_functions.py` (autouse fixture patches `phaze.tasks.functions.get_settings` to a `MagicMock(spec=AgentSettings)`).
- **Commit:** `dd828a8`

## Authentication Gates

None.

## Verification

- `uv run pytest tests/test_tasks/test_functions.py tests/test_services/test_analysis_enqueue.py -q` -> 26 passed.
- `uv run mypy .` -> Success, 156 source files. `uv run ruff check .` -> All checks passed. All pre-commit hooks passed on both commits (no `--no-verify`).
- Coverage of the two modified source modules: `functions.py` 100%, `analysis_enqueue.py` 100%.
- DB-backed `tests/test_tasks/{test_scan_reaper,test_recovery}.py` errored only without the test-DB env (pre-existing OSError on asyncpg connect); they pass with `TEST_DATABASE_URL`/`PHAZE_REDIS_URL` set — unrelated to this change.

## Acceptance Criteria

- `grep -c "14400" src/phaze/services/analysis_enqueue.py` == 0; `grep "7200"` and `grep "retries=2"` present.
- `grep -nE "report_analysis_failed|ProcessExpired|analysis_inner_timeout_sec|fine_cap" src/phaze/tasks/functions.py` shows the terminal handling + cap/timeout threading; `grep "fine_windows_total"` shows coverage forwarded.
- `uv run mypy src/phaze/tasks/functions.py` clean.

## Threat Model Coverage

- **T-43-08** (blind-retry of a too-long file): mitigated — `TimeoutError`/`ProcessExpired` return normally (COMPLETE, no retry); transient errors retry once via `retries=2`.
- **T-43-09** (unbounded error string): mitigated — `str(exc)` truncated to 2000 chars before send (`_ERROR_DETAIL_MAX`); control-side `max_length=2000` is the authoritative bound.
- **T-43-10** (unbounded outer timeout): mitigated — SAQ `process_file` timeout lowered to 7200; inner pebble timeout (6600) kills first.

## Known Stubs

None.

## Self-Check: PASSED
