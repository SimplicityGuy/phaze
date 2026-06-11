---
phase: 32-pipeline-reboot-resilience-re-enqueue
plan: 02
subsystem: tasks
tags: [saq, reboot-resilience, re-enqueue, dedup, control-only, wave-2]
requires:
  - "src/phaze/services/analysis_enqueue.py::enqueue_process_file (Wave-1 shared producer)"
  - "src/phaze/services/enqueue_router.py::select_active_agent + NoActiveAgentError"
  - "src/phaze/services/agent_task_router.py::AgentTaskRouter.queue_for"
  - "src/phaze/services/pipeline.py::get_files_by_state"
  - "tests/_queue_fakes.py::DedupFakeTaskRouter + seed_active_agent (Wave-0)"
provides:
  - "src/phaze/tasks/reenqueue.py::reenqueue_discovered — control-only task that re-enqueues every DISCOVERED file onto the active agent's queue via the shared helper, returns {reenqueued, skipped}"
affects:
  - "Wave 3 controller startup/cron registers reenqueue_discovered and stashes ctx['task_router'] so this task can route per-agent"
tech-stack:
  added: []
  patterns:
    - "Control-only SAQ task mirroring scan_reaper.py: ctx['async_session'] for Postgres, structlog logger, dict-count return, hard import boundary away from agent_worker"
    - "Reboot recovery layered on the Wave-1 deterministic-key seam: in-flight files dedup to a no-op (enqueue returns None -> counted as skipped), making the task idempotent on every boot AND every cron tick"
    - "Graceful degradation: NoActiveAgentError -> logged WARNING + zero count, never raises (safe right after a cold reboot)"
key-files:
  created:
    - src/phaze/tasks/reenqueue.py
    - tests/test_tasks/test_reenqueue.py
  modified: []
decisions:
  - "Reused ctx['task_router'] (Wave-3 stash) instead of constructing a new AgentTaskRouter per call (RESEARCH Pitfall 4) — connection pool stays cached"
  - "Routed ONLY via select_active_agent + task_router.queue_for(agent.id); the controller queue ctx also carries is never touched (RESEARCH Pitfall 1, grep-gated)"
  - "Monkeypatched get_settings in tests for a deterministic models_path, mirroring test_scan_reaper.py's _patch_threshold rather than depending on ambient role/env"
  - "Integration test probes Redis with an up-front ping and skips BEFORE building the router, so a skip is never overridden by close() re-raising the same ConnectionError"
requirements: [RESIL-01, RESIL-02, RESIL-04]
metrics:
  duration: ~25m
  completed: 2026-06-11
  tasks: 2
  files: 2
---

# Phase 32 Plan 02: Reboot Re-enqueue Controller Task Summary

Built `reenqueue_discovered(ctx)` — the control-only recovery task that, after a reboot or Redis flush, queries Postgres for `FileState.DISCOVERED` files and re-enqueues `process_file` for each onto the active agent's per-agent queue through the Wave-1 shared helper, so the resumed jobs carry the identical deterministic key/payload/policy as the dashboard and any in-flight file dedups to a clean no-op.

## What Was Built

- **`src/phaze/tasks/reenqueue.py::reenqueue_discovered(ctx)`** — opens its own session via `ctx["async_session"]`, loads DISCOVERED files via `get_files_by_state`, and:
  - returns `{"reenqueued": 0, "skipped": 0}` immediately when there are no DISCOVERED files (never selects an agent / touches the router);
  - selects the active agent via `select_active_agent`, catching `NoActiveAgentError` → logs a WARNING with the discovered count and returns zeros (never raises);
  - obtains the per-agent queue from `ctx["task_router"].queue_for(agent.id)` (NOT the controller queue) and loops `enqueue_process_file(queue, file, agent.id, cfg.models_path)`, counting `None` returns as `skipped` and the rest as `reenqueued`;
  - emits an INFO log with the agent id, queue name, and final counts, then returns `{"reenqueued": N, "skipped": M}`.
  - Module docstring carries the CONTROL-ONLY boundary wording copied from `scan_reaper.py` (needs `async_session` + `task_router`; MUST NEVER be imported/registered by `agent_worker` or `_shared`).
- **`tests/test_tasks/test_reenqueue.py`** — 6 unit tests + 1 integration test:
  - `test_startup_reenqueues_all_discovered` (`-k discovered`): N DISCOVERED + active agent → `{reenqueued: N, skipped: 0}`, N `process_file` payloads on `phaze-agent-<id>` with the right deterministic keys.
  - `test_cron_reenqueues_stragglers`: a pre-enqueued (live) subset counts as `skipped`, the rest `reenqueued`.
  - `test_reenqueue_inflight_file_is_noop` (`-k dedup`): a live key dedups → `skipped`, NO second payload lands.
  - `test_no_active_agent_skips` (`-k no_agent`): DISCOVERED files but no live agent → zeros, a `caplog` WARNING, no raise, `queue_for` never called.
  - `test_payload_is_complete` (`-k payload`): exactly the 5 `ProcessFilePayload` fields + `timeout=14400` / `retries=2` + the deterministic key.
  - `test_empty_discovered_returns_zero`: no DISCOVERED rows → zeros, `select_active_agent` never reached (`queue_for_calls == []`).
  - `test_real_redis_dedup_returns_none` (`@pytest.mark.integration`): real `AgentTaskRouter`, enqueues the same `process_file` key twice, asserts the second returns `None`; skips cleanly when Redis is down and aborts the test key on teardown.

## Why It Matters

After a reboot or Redis flush, DISCOVERED files are stranded: their `process_file` jobs are gone but Postgres still records them as discovered (the state does not advance until a worker finishes). This task is the recovery layer on top of Phase 31's bounded timeout/retries — it resumes the analysis stage automatically. Because it funnels through the Wave-1 deterministic-key seam, running it on every boot and every cron tick is safe: already in-flight files become no-ops instead of duplicate jobs (the v4.0.6 stranded-jobs class of bug), and it can never mis-route onto the consumer-less controller queue.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Reworded a docstring to clear the `ctx["queue"]` grep gate**
- **Found during:** Task 1
- **Issue:** The acceptance gate `grep -n "ctx\[.queue.\]" src/phaze/tasks/reenqueue.py` must return nothing, but the docstring originally spelled out the forbidden `ctx["queue"]` literal while explaining what NOT to use.
- **Fix:** Reworded to "the consumer-less controller queue that ctx also carries" — same meaning, no forbidden literal. The code never references the controller queue.
- **Files modified:** src/phaze/tasks/reenqueue.py
- **Commit:** 71e0c18

**2. [Rule 1 - Test correctness] Up-front Redis ping gate so the integration skip is not overridden by teardown**
- **Found during:** Task 2
- **Issue:** Catching the connection error at the first `enqueue` and calling `pytest.skip` still FAILED the test: the `finally` block's `router.close()` re-raised the same `redis.exceptions.ConnectionError`, overriding the `Skipped` outcome.
- **Fix:** Probe Redis with a dedicated `redis.asyncio` `ping()` BEFORE constructing the router; skip there so the skip path builds no SAQ connection to clean up. (`redis.exceptions.ConnectionError` is a `RedisError`, not a builtin `ConnectionError` — the gate catches `redis_exc.RedisError` + `OSError`.)
- **Files modified:** tests/test_tasks/test_reenqueue.py
- **Commit:** 03fe5e9

**3. [Rule 3 - Blocking] `contextlib.suppress` for best-effort teardown (ruff SIM105/S110)**
- **Found during:** Task 2
- **Issue:** The `try/except Exception: pass` cleanup tripped ruff `SIM105` + `S110` and a stale `BLE001` noqa.
- **Fix:** Replaced with `with contextlib.suppress(Exception):` around `queue.abort(...)`.
- **Files modified:** tests/test_tasks/test_reenqueue.py
- **Commit:** 03fe5e9

## TDD Gate Compliance

Task 1 is `tdd="true"` and its sole automated verification is `mypy` (a structural gate); the plan deliberately separates the implementation (Task 1) from the full behavioral test suite (Task 2). The two were committed in plan order — `feat` (Task 1) then `test` (Task 2) — rather than a strict RED→GREEN interleave, because Task 2 owns the behavioral tests as a distinct deliverable. All Task-2 tests were authored against the Task-1 module and pass.

## Verification

- `uv run pytest tests/test_tasks/test_reenqueue.py -q` → 6 passed, 1 skipped (integration skips cleanly, Redis absent in this env).
- `uv run mypy src/phaze/tasks/reenqueue.py` → clean (and the repo-wide pre-commit `uv run mypy .` hook passed on both commits).
- `grep -n "ctx\[.queue.\]\|AgentTaskRouter(\|fastapi\|phaze.routers" src/phaze/tasks/reenqueue.py` → nothing (queue-routing + import-boundary hygiene).
- Pre-commit hooks (ruff, ruff-format, bandit, mypy, ...) passed on both code commits.

## Threat Surface

No new trust boundary: the task READS server-owned Postgres rows and enqueues onto an internal per-agent Redis queue (no new external input/auth/endpoint).
- **T-32-02 (mis-route → stranded jobs)** mitigated: routes only via `select_active_agent` + `task_router.queue_for(agent.id)`; the `ctx["queue"]` grep gate is clean.
- **T-32-03 (runaway duplicate jobs)** mitigated: the shared deterministic key dedups in-flight files to no-ops — pinned by `test_reenqueue_inflight_file_is_noop` (fake) and `test_real_redis_dedup_returns_none` (real SAQ).
- **T-32-04 (unhandled NoActiveAgentError)** mitigated: `try/except` → warn + zero; `test_no_active_agent_skips` asserts no raise.
- **T-32-SC**: no package installs.

## Commits

- `71e0c18` feat(32-02): add reenqueue_discovered control task
- `03fe5e9` test(32-02): unit + integration tests for reenqueue_discovered

## Self-Check: PASSED
- FOUND: src/phaze/tasks/reenqueue.py
- FOUND: tests/test_tasks/test_reenqueue.py
- FOUND: commit 71e0c18
- FOUND: commit 03fe5e9
