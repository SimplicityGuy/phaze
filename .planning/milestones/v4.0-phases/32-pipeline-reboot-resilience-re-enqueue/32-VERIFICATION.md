---
phase: 32-pipeline-reboot-resilience-re-enqueue
verified: 2026-06-11T17:51:25Z
status: human_needed
score: 10/10
overrides_applied: 0
human_verification:
  - test: "Real reboot self-heal on homelab"
    expected: "After rebooting the homelab host, DISCOVERED files re-enqueue automatically (no manual 'Run Analysis') and analysis resumes. Log line 'phaze.controller startup re-enqueue' should appear with reenqueued > 0."
    why_human: "Requires a real OS-level host reboot with the live corpus and agent worker running. Not reproducible in CI or local env."
  - test: "Real-Redis dedup under container restart"
    expected: "Restarting the controller container while jobs are queued results in no duplicate in-flight jobs. The startup re-enqueue should count all in-flight files as 'skipped' (deterministic-key dedup working against live Redis)."
    why_human: "Requires a live Redis instance retaining state across the controller container restart. Needs homelab with running agent worker."
---

# Phase 32: Pipeline Reboot Resilience & Re-enqueue — Verification Report

**Phase Goal:** Make the analysis pipeline self-healing across full host reboots and container restarts. Postgres `FileState` is the durable source of truth; Redis stays disposable. `FileState.DISCOVERED` files that lack an active job get `process_file` re-enqueued automatically — once on controller boot AND on a 5-minute cron. Resilience is idempotent: both producers emit the identical deterministic SAQ key `process_file:<file_id>`, so a re-enqueue of an already-in-flight file dedups to a no-op.

**Verified:** 2026-06-11T17:51:25Z
**Status:** human_needed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A single shared FastAPI-free helper (`analysis_enqueue.py`) produces the deterministic key `process_file:<file_id>` used by BOTH the dashboard path and the reboot re-enqueue path | VERIFIED | `src/phaze/services/analysis_enqueue.py:32-40` — `process_file_job_key` returns `f"process_file:{file_id}"`; imported at `pipeline.py:24` and `reenqueue.py:38`. No second key-construction site exists. |
| 2 | The shared enqueue module is FastAPI-free — does not import `fastapi` or `phaze.routers` | VERIFIED | Only imports: `__future__`, `typing`, `phaze.schemas.agent_tasks.ProcessFilePayload` (real import), `uuid` and `phaze.models.file.FileRecord` (TYPE_CHECKING only). Grep for `fastapi`/`phaze.routers` in the module returns nothing. |
| 3 | `reenqueue_discovered(ctx)` queries Postgres for `FileState.DISCOVERED`, selects the active agent, and re-enqueues each file via the shared helper onto the agent's queue (not the controller queue) | VERIFIED | `src/phaze/tasks/reenqueue.py:67-87` — uses `ctx["async_session"]`, `get_files_by_state(session, FileState.DISCOVERED)`, `select_active_agent(session)`, `ctx["task_router"].queue_for(agent.id)`, `enqueue_process_file(...)`. `grep ctx["queue"]` returns nothing in that file. |
| 4 | In-flight files dedup to a no-op (enqueue returns `None`, counted as `skipped`) | VERIFIED | `reenqueue.py:84-87` — `if job is None: skipped += 1`. `test_reenqueue_inflight_file_is_noop` PASSES: pre-enqueued key → task returns `{reenqueued:0, skipped:1}`, `len(queue.captured)==1` (no second payload landed). |
| 5 | Zero active agents (`NoActiveAgentError`) logs a WARNING and returns zeros — never raises | VERIFIED | `reenqueue.py:72-76` — `except NoActiveAgentError: logger.warning(...); return {"reenqueued":0,"skipped":0}`. `test_no_active_agent_skips` PASSES (caplog asserts "reenqueue skipped: no active agent", result == zeros, no exception). |
| 6 | Controller startup stashes `ctx["task_router"] = AgentTaskRouter(cfg.redis_url)` and closes it in shutdown | VERIFIED | `controller.py:97` (stash), `controller.py:126-128` (close in shutdown). `test_startup_stashes_router_and_calls_reenqueue_once` and `test_shutdown_closes_task_router` PASS. |
| 7 | Controller startup calls `reenqueue_discovered(ctx)` once on boot | VERIFIED | `controller.py:105-109` — `counts = await reenqueue_discovered(ctx)` wrapped in `try/except Exception`. `test_startup_stashes_router_and_calls_reenqueue_once` asserts `reenqueue_mock.assert_awaited_once_with(ctx)`. |
| 8 | A boot-time re-enqueue failure never aborts controller startup | VERIFIED | `controller.py:105-109` — broad `try/except Exception` catches all failures, logs `logger.exception("reenqueue on startup failed")` and continues. `test_startup_survives_raising_reenqueue` PASSES (RuntimeError("boom") does not propagate). |
| 9 | A periodic `CronJob(reenqueue_discovered, cron="*/5 * * * *")` is registered in controller settings alongside existing crons | VERIFIED | `controller.py:162` — `CronJob(reenqueue_discovered, cron="*/5 * * * *")`. `test_cron_registers_reenqueue_every_five_minutes` PASSES. `test_cron_does_not_regress_existing_jobs` PASSES (reap_stalled_scans + refresh_tracklists remain). |
| 10 | `reenqueue_discovered` is registered in `settings["functions"]` | VERIFIED | `controller.py:150` — `reenqueue_discovered` in functions list. `test_functions_list_includes_reenqueue_discovered` PASSES. |

**Score:** 10/10 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/analysis_enqueue.py` | FastAPI-free shared producer with `process_file_job_key` + `enqueue_process_file` | VERIFIED | Exists, substantive (80 lines), wired by both `pipeline.py` and `reenqueue.py` |
| `src/phaze/tasks/reenqueue.py` | `reenqueue_discovered(ctx)` control-only recovery task | VERIFIED | Exists, substantive (98 lines), wired in `controller.py` |
| `src/phaze/tasks/controller.py` | task_router lifecycle + startup re-enqueue + CronJob + functions registration | VERIFIED | Modified; all 4 wiring points confirmed at lines 97, 105-109, 150, 162 |
| `tests/_queue_fakes.py` | `DedupFakeQueue` + `DedupFakeTaskRouter` additive test doubles | VERIFIED | `DedupFakeQueue(FakeQueue)` at line 176, `DedupFakeTaskRouter(FakeTaskRouter)` at line 223; 6 existing FakeQueue consumers unperturbed |
| `tests/test_queue_fakes_dedup.py` | 4 self-tests for dedup contract | VERIFIED | 4 async tests, all PASS |
| `tests/test_services/test_analysis_enqueue.py` | Key format + complete payload + policy tests | VERIFIED | 3 tests, all PASS |
| `tests/test_tasks/test_reenqueue.py` | 6 unit tests + 1 integration test | VERIFIED | 6 PASS, 1 SKIPPED (Redis integration — expected, no local Redis) |
| `tests/test_tasks/test_controller_reenqueue.py` | Registration + startup behavior tests | VERIFIED | 6 tests, all PASS |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `pipeline.py::_enqueue_analysis_jobs` | `analysis_enqueue.py::enqueue_process_file` | `import + per-file call` | VERIFIED | `pipeline.py:24` imports, `pipeline.py:64` calls; `ProcessFilePayload` no longer constructed inline (grep returns nothing) |
| `reenqueue.py::reenqueue_discovered` | `analysis_enqueue.py::enqueue_process_file` | `per-file shared-helper enqueue` | VERIFIED | `reenqueue.py:38` imports, `reenqueue.py:83` calls |
| `reenqueue.py::reenqueue_discovered` | `services/enqueue_router.py::select_active_agent` | `inside ctx["async_session"]()` | VERIFIED | `reenqueue.py:39` imports `select_active_agent + NoActiveAgentError`, `reenqueue.py:73` calls |
| `reenqueue.py::reenqueue_discovered` | `ctx["task_router"].queue_for(agent.id)` | per-agent queue | VERIFIED | `reenqueue.py:78` — `queue = ctx["task_router"].queue_for(agent.id)`. Never touches `ctx["queue"]` (grep confirms). |
| `controller.py::startup` | `reenqueue_discovered` | `ctx["task_router"] stash + one boot call` | VERIFIED | `controller.py:97` stash, `controller.py:106` boot call, both confirmed by tests |
| `controller.py::settings` | `CronJob(reenqueue_discovered, cron="*/5 * * * *")` | cron_jobs list | VERIFIED | `controller.py:162` |

---

### Data-Flow Trace (Level 4)

Not applicable — the phase delivers task queue infrastructure (enqueueing), not data-rendering components. The data source is Postgres (`get_files_by_state` → real DB query), verified substantive in prior phases.

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| `process_file_job_key` format | `uv run pytest tests/test_services/test_analysis_enqueue.py::test_process_file_job_key_format -q` | PASSED | PASS |
| Dashboard emits deterministic key | `uv run pytest tests/test_routers/test_pipeline.py::test_analyze_enqueues_deterministic_key_per_file -q` | PASSED | PASS |
| Dedup fake: repeat live key returns None | `uv run pytest tests/test_queue_fakes_dedup.py::test_dedup_repeat_live_key_returns_none -q` | PASSED | PASS |
| Startup re-enqueue call once | `uv run pytest tests/test_tasks/test_controller_reenqueue.py::test_startup_stashes_router_and_calls_reenqueue_once -q` | PASSED | PASS |
| Boot survives raising re-enqueue | `uv run pytest tests/test_tasks/test_controller_reenqueue.py::test_startup_survives_raising_reenqueue -q` | PASSED | PASS |

**Full targeted suite result:** 56 passed, 1 skipped (Redis integration — environmental, expected)

---

### Probe Execution

No phase-specific probe scripts found or declared.

---

### Requirements Coverage

| Requirement | Source Plan | Status | Evidence |
|-------------|------------|--------|---------|
| RESIL-01 (reboot re-enqueue — startup + cron) | 32-02, 32-03 | SATISFIED | Startup call at `controller.py:106`, CronJob at `controller.py:162` |
| RESIL-02 (idempotent dedup via deterministic key) | 32-00, 32-02, 32-03 | SATISFIED | `process_file_job_key` in shared helper; dedup tests pass |
| RESIL-03 (FastAPI-free shared helper) | 32-01 | SATISFIED | Import check confirms no fastapi/phaze.routers in `analysis_enqueue.py` |
| RESIL-04 (zero-agent graceful skip) | 32-02 | SATISFIED | `try/except NoActiveAgentError` → warn + zeros; `test_no_active_agent_skips` passes |
| RESIL-05 (dashboard path emits deterministic key) | 32-01 | SATISFIED | `pipeline.py` delegates to shared helper; `test_analyze_enqueues_deterministic_key_per_file` passes |

---

### Anti-Patterns Found

No TBD, FIXME, or XXX markers found in any implementation file. No stub patterns. No empty implementations. All three implementation files (`analysis_enqueue.py`, `reenqueue.py`, `controller.py`) are substantive with working logic.

---

### Static Analysis

| Check | Result |
|-------|--------|
| `uv run ruff check .` | All checks passed (0 violations) |
| `uv run mypy .` | Success: no issues found in 147 source files |

---

### Human Verification Required

#### 1. Real Reboot Self-Heal (Homelab)

**Test:** Redeploy the latest image to the homelab. Reboot the host (or restart the controller container with Redis container restarted first so Redis is empty). Watch the controller logs.

**Expected:** The log line `phaze.controller startup re-enqueue` appears with `reenqueued > 0` (all 11,428+ DISCOVERED files re-enqueue automatically). Analysis jobs begin running on the agent worker without any manual "Run Analysis" click.

**Why human:** Requires a real OS-level host reboot or container restart sequence with the live corpus and running agent worker. Not reproducible in CI or local dev environment. The unit tests prove the mechanism; only the homelab proves end-to-end recovery.

---

#### 2. Real-Redis Dedup Under Container Restart (Homelab)

**Test:** With the controller and agent worker running and some `process_file` jobs in flight (visible in SAQ dashboard), restart ONLY the controller container (leave Redis running). Check the controller startup logs.

**Expected:** The startup re-enqueue logs all in-flight files as `skipped` (not `reenqueued`). No duplicate jobs appear in the SAQ queue. The `test_real_redis_dedup_returns_none` integration test (which skips locally due to no Redis) can optionally be run on the homelab with `PHAZE_REDIS_URL` set.

**Why human:** Requires live Redis with real job state (incomplete sorted set populated by prior runs). The `DedupFakeQueue` unit test and the guarded integration test prove the mechanism, but the full container-restart scenario requires the homelab deployment.

---

### Gaps Summary

No gaps found. All 10 observable truths are VERIFIED with direct code and test evidence. The 2 human verification items are homelab deployment tests that are inherently untestable programmatically — they validate the end-to-end recovery behavior in production, not the mechanism (which is fully tested).

---

_Verified: 2026-06-11T17:51:25Z_
_Verifier: Claude (gsd-verifier)_
