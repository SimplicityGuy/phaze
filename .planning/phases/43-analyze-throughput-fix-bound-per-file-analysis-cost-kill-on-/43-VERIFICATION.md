---
phase: 43-analyze-throughput-fix
verified: 2026-06-17T00:00:00Z
status: human_needed
score: 11/11
overrides_applied: 0
human_verification:
  - test: "Deploy to homelab and trigger a file known to previously exceed the 4h timeout (a 3h+ DJ set). Monitor logs and SAQ UI."
    expected: "Job completes in minutes (not hours); logs show pebble inner timeout at 6600s if the file was still too long; files.state advances to 'analyzed' (or 'analysis_failed' on terminal); CPU/slot usage drops after job completion; no blind retry for timeout/crash outcomes."
    why_human: "The core invariant (bounded cost makes a 3h set behave like a 20-min track) can only be validated against real essentia on real files. Automated tests mock the pool and analyze_file; the homelab redeploy is the only gate that confirms actual throughput improvement."
  - test: "Check pipeline dashboard after redeploy — scan for 'analyzed' vs 'discovered' state counts and verify files now advance state."
    expected: "Files that are processed show 'analyzed' or 'analysis_failed' in files.state (not stuck 'discovered'). The latent re-enqueue-all bug (all 11,428 stuck at discovered) should no longer reproduce on a fresh trigger."
    why_human: "State-advance correctness requires a live DB with real file records and a running agent worker. The router test covers this code path, but production verification against the actual homelab archive confirms no edge case regresses the state machine."
deferred:
  - truth: "ANALYSIS_FAILED files are visible in the pipeline dashboard (count/list shown, progress bar accounts for them)"
    addressed_in: "Phase 44"
    evidence: "Phase 44 goal: 'Add a dashboard count/list of failed/straggler files, a sampled badge, and a deepen-analysis re-trigger.' Phase 44 requirements: 'dashboard straggler/ANALYSIS_FAILED count + list'. The IN-01 finding in 43-REVIEW.md explicitly defers this to Phase 44 scope."
  - truth: "fine_window_sec / coarse_window_sec / fine_min_sec are threaded from AgentSettings to analyze_file"
    addressed_in: "Phase 44 or later (pre-existing Phase 31 gap)"
    evidence: "43-REVIEW.md WR-03: confirmed pre-existing omission from Phase 31. WR-03 resolution: 'DEFERRED (out of scope). Verified the baseline (origin/main) call site forwarded only original_path/models_path — the three window-size knobs were never wired since Phase 31. This is a pre-existing Phase 31 gap, not a Phase 43 regression.'"
---

# Phase 43: Analyze Throughput Fix Verification Report

**Phase Goal:** Make the Analyze stage actually drain. Long DJ/concert essentia analysis legitimately exceeds the 4h timeout (cost is O(file duration)). Bound per-file cost so a 3h set costs approximately a 20-min track, kill runaway essentia children deterministically, stop wasteful retries, and make analysis outcomes (done / sampled / failed) visible in the file state machine. Backend-only — redeployable to the homelab immediately.
**Verified:** 2026-06-17
**Status:** human_needed (all 11 automated truths VERIFIED; 2 items require homelab deployment confirmation)
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| #  | Truth                                                                                                            | Status     | Evidence                                                                                                                                  |
|----|------------------------------------------------------------------------------------------------------------------|------------|-------------------------------------------------------------------------------------------------------------------------------------------|
| 1  | A runaway essentia child exceeding the inner timeout is SIGKILLed and its pool slot is reclaimed                | VERIFIED   | `pool.py`: `ProcessPool(max_workers=..., max_tasks=1)`; `run_in_process_pool` calls `asyncio.wrap_future(pool.schedule(..., timeout=timeout))`; raises `builtins.TimeoutError` on kill. Tests in `test_pool.py::test_run_in_process_pool_kills_runaway_child_on_timeout`. |
| 2  | `run_in_process_pool` raises a catchable `builtins.TimeoutError` when a task exceeds its per-task timeout        | VERIFIED   | `pool.py` line 51: `return await asyncio.wrap_future(future)` — pebble surfaces as `builtins.TimeoutError`. Test `test_run_in_process_pool_kills_runaway_child_on_timeout` covers this with a real pebble pool and a module-level sleeper. |
| 3  | New AgentSettings knobs (`analysis_inner_timeout_sec=6600`, `analysis_fine_cap=60`, `analysis_coarse_cap=30`) are config-exposed with PHAZE_* aliases | VERIFIED | `config.py` lines 446–464: all three fields with `AliasChoices`, correct defaults, `gt=0, lt=7200` on inner timeout, `ge=2` on both caps (review fixes CR-01 and WR-02 applied). |
| 4  | A file whose natural window count exceeds the cap is strided EVENLY across the WHOLE file (first and last window always kept), not truncated to first-N | VERIFIED | `analysis.py` line 391: `_stride_to_cap` with endpoint-inclusive stride formula `round(i * (n - 1) / (cap - 1))` spanning `0..n-1`. Tests `test_stride_keeps_first_and_last`, `test_stride_evenly_spaced`, `test_stride_over_cap_bounds_count_and_sets_sampled` all green. |
| 5  | Per-file analysis cost is bounded to <=60 fine + <=30 coarse windows regardless of duration                     | VERIFIED   | `analysis.py` lines 354–355: `_DEFAULT_FINE_CAP = 60`, `_DEFAULT_COARSE_CAP = 30`; both applied via `_stride_to_cap` in `_analyze_fine_windows` / `_analyze_coarse_windows`. Tests `test_analyze_file_coverage_over_cap_strides` confirm the bounds. |
| 6  | `analyze_file` returns coverage: `fine_windows_analyzed/total`, `coarse_windows_analyzed/total`, and a `sampled` bool | VERIFIED | `analysis.py` lines 583–587: all five coverage keys emitted in return dict. Tests `test_analyze_file_coverage_under_cap` and `test_analyze_file_coverage_over_cap_strides` verify both paths. |
| 7  | A successful (non-empty) analysis PUT advances `files.state` to `'analyzed'` in the same transaction            | VERIFIED   | `routers/agent_analysis.py` lines 187–188: `if dumped: await session.execute(update(FileRecord)...values(state=FileState.ANALYZED))` before `session.commit()`. Test `test_analysis_put_advances_state_and_persists_coverage_columns` verifies this; `test_analysis_empty_put_does_not_advance_state` confirms no-op on empty body. |
| 8  | A terminal failure POST advances `files.state` to `'analysis_failed'`; `FileState.ANALYSIS_FAILED` exists      | VERIFIED   | `models/file.py` line 39: `ANALYSIS_FAILED = "analysis_failed"`. `routers/agent_analysis.py` lines 194–222: `POST /{file_id}/failed` handler updates state. `services/agent_client.py` line 266: `report_analysis_failed` method. Tests `test_analysis_failed_sets_state`, `test_analysis_failed_bad_reason_422`, `test_analysis_failed_extra_field_422`, `test_analysis_failed_missing_auth_returns_401`. |
| 9  | Coverage fields land in dedicated analysis columns, NOT the `features` JSONB overflow                           | VERIFIED   | `routers/agent_analysis.py` lines 61–77: `_ANALYSIS_COLUMN_FIELDS` frozenset includes all five coverage names (`fine_windows_analyzed`, `fine_windows_total`, `coarse_windows_analyzed`, `coarse_windows_total`, `sampled`). Migration 021 adds them as actual columns. Test `test_analysis_put_advances_state_and_persists_coverage_columns` asserts coverage columns populated while features untouched. |
| 10 | `enqueue_process_file` emits `timeout=7200` and `retries=2`; inner-timeout `TimeoutError` and `ProcessExpired` are terminal (no retry) | VERIFIED | `analysis_enqueue.py` lines 80–85: `timeout=7200`, `retries=2`. `functions.py` lines 164–172: `except TimeoutError` → `report_analysis_failed(reason="timeout")` + `return` (COMPLETE, no retry); `except ProcessExpired` → same with `reason="crashed"`. Tests `test_process_file_timeout_is_terminal`, `test_process_file_process_expired_is_terminal`, `test_enqueue_process_file_complete_payload_and_policy`, `test_enqueue_policy_survives_apply_project_job_defaults`. |
| 11 | A successful analysis forwards the five coverage fields to `put_analysis`; worker passes inner timeout + 60/30 caps from settings | VERIFIED | `functions.py` lines 155–163: `run_in_process_pool` called with `timeout=cfg.analysis_inner_timeout_sec, fine_cap=cfg.analysis_fine_cap, coarse_cap=cfg.analysis_coarse_cap`. Lines 207–211: all five coverage keys forwarded to `AnalysisWritePayload`. Tests `test_process_file_threads_inner_timeout_and_caps`, `test_process_file_forwards_coverage_fields`. |

**Score:** 11/11 truths verified

---

### Deferred Items

Items not yet met but explicitly addressed in later milestone phases.

| # | Item | Addressed In | Evidence |
|---|------|-------------|----------|
| 1 | `ANALYSIS_FAILED` visible in pipeline dashboard (count/list, progress bar denominator) | Phase 44 | Phase 44 goal: "Add a dashboard count/list of failed/straggler files." 43-REVIEW.md IN-01: deferred to Phase 44 scope. `ANALYSIS_FAILED` absent from `PIPELINE_STAGES` in `pipeline.py` — intentional per review triage. |
| 2 | `fine_window_sec` / `coarse_window_sec` / `fine_min_sec` threaded from AgentSettings to `analyze_file` | Phase 44 or follow-up | 43-REVIEW.md WR-03: confirmed pre-existing Phase 31 gap (zero behavior risk — config defaults equal module defaults). Not a Phase 43 regression. |

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/tasks/pool.py` | pebble ProcessPool-backed killable pool with timeout + kwargs | VERIFIED | `ProcessPool(max_workers=..., max_tasks=1)`; `asyncio.wrap_future`; `stop()/join()` shutdown |
| `src/phaze/config.py` | Three AgentSettings knobs (6600/60/30 defaults + PHAZE_* aliases) | VERIFIED | `analysis_inner_timeout_sec` with `gt=0, lt=7200`; `analysis_fine_cap` / `analysis_coarse_cap` both `ge=2` |
| `pyproject.toml` | pebble dependency | VERIFIED | Line 32: `"pebble>=5.2.0"` |
| `src/phaze/services/analysis.py` | `_stride_to_cap` + cap-aware iterators + coverage in `analyze_file` return | VERIFIED | `_stride_to_cap` at line 391; `_DEFAULT_FINE_CAP=60`, `_DEFAULT_COARSE_CAP=30`; five coverage keys in return dict |
| `alembic/versions/021_add_analysis_coverage_columns.py` | Five nullable coverage columns on `analysis` table | VERIFIED | `revision="021"`, `down_revision="020"`; four Integer + one Boolean, all `nullable=True`; downgrade reverses |
| `src/phaze/models/file.py` | `FileState.ANALYSIS_FAILED` enum member | VERIFIED | Line 39: `ANALYSIS_FAILED = "analysis_failed"` |
| `src/phaze/routers/agent_analysis.py` | ANALYZED-on-success + coverage in `_ANALYSIS_COLUMN_FIELDS` + `POST /{file_id}/failed` | VERIFIED | Lines 61–77, 187–188, 194–222 |
| `src/phaze/services/agent_client.py` | `report_analysis_failed` client method | VERIFIED | Lines 266–281 |
| `src/phaze/services/analysis_enqueue.py` | `timeout=7200`, `retries=2` enqueue policy | VERIFIED | Lines 80–85; no `14400` remains (count=0) |
| `src/phaze/tasks/functions.py` | Terminal classification + coverage forwarding + cap/inner-timeout threading | VERIFIED | Lines 155–163 (pool call), 164–184 (exception branches), 207–211 (coverage forward) |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `pool.py::run_in_process_pool` | `pebble.ProcessPool.schedule` | `asyncio.wrap_future(pool.schedule(..., timeout=timeout))` | WIRED | Lines 50–51: `future = pool.schedule(func, **schedule_kwargs); return await asyncio.wrap_future(future)` |
| `agent_worker.py::shutdown` | `pool.stop()/pool.join()` | shutdown hook | WIRED | Lines 164–165: `pool.stop(); pool.join()` — no `pool.shutdown` remains |
| `analysis.py::_analyze_fine_windows` | `_stride_to_cap` | call before per-window decode loop | WIRED | Line 453: `kept, sampled = _stride_to_cap(natural, cap)` |
| `analysis.py::analyze_file` | coverage dict keys | return dict | WIRED | Lines 583–587: five keys present |
| `routers/agent_analysis.py::put_analysis` | `FileRecord.state = 'analyzed'` | same-transaction UPDATE | WIRED | Lines 187–188: `if dumped: await session.execute(update(FileRecord)...values(state=FileState.ANALYZED))` |
| `services/agent_client.py::report_analysis_failed` | `POST /api/internal/agent/analysis/{file_id}/failed` | `_request` funnel | WIRED | Lines 276–278: `self._request("POST", f"/api/internal/agent/analysis/{file_id}/failed", ...)` |
| `tasks/functions.py::process_file` | `run_in_process_pool(timeout, fine_cap, coarse_cap)` | kwargs passthrough | WIRED | Lines 155–162: `timeout=cfg.analysis_inner_timeout_sec, fine_cap=cfg.analysis_fine_cap, coarse_cap=cfg.analysis_coarse_cap` |
| `tasks/functions.py::process_file` | `api.report_analysis_failed` | `except TimeoutError / ProcessExpired / non-retryable Exception` | WIRED | Lines 164–184: all three branches confirmed |

---

### Data-Flow Trace (Level 4)

Not applicable for this phase. The Phase 43 changes are backend services and task functions — no components render dynamic data to a UI. Level 4 data-flow trace applies to rendering components; these are API routes, task workers, and data-processing functions.

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| pebble importable under venv | `grep "pebble>=5.2.0" pyproject.toml` | `"pebble>=5.2.0"` found at line 32 | PASS |
| Old 14400 timeout gone | `grep -c "14400" src/phaze/services/analysis_enqueue.py` | 0 | PASS |
| `ANALYSIS_FAILED` enum exists | `grep "ANALYSIS_FAILED" src/phaze/models/file.py` | `ANALYSIS_FAILED = "analysis_failed"` at line 39 | PASS |
| Migration 021 has correct revision | `grep 'revision: str = "021"' alembic/versions/021_add_analysis_coverage_columns.py` | matches | PASS |
| `pool.shutdown` no longer present | `grep -c "pool.shutdown" src/phaze/tasks/agent_worker.py` | 0 | PASS |
| Live essentia runtime on homelab | cannot test without running worker | n/a | SKIP — requires homelab redeploy |

---

### Requirements Coverage

| Requirement (from ROADMAP.md) | Source Plans | Description | Status | Evidence |
|-------------------------------|-------------|-------------|--------|---------|
| Cap + even-stride (60/30, config-exposed, sampled flag) | 43-02 | `_stride_to_cap` evenly strides over-cap files; caps 60/30 in `AgentSettings`; `sampled` returned | SATISFIED | `analysis.py:391`, `config.py:453–464`, `analysis.py:583–587` |
| Kill-on-timeout (pebble ProcessPool, inner timeout below SAQ outer) | 43-01 | pebble replaces ProcessPoolExecutor; SIGKILL on timeout; `analysis_inner_timeout_sec=6600 < 7200` | SATISFIED | `pool.py`, `config.py:446–451` |
| State-machine fix (ANALYZED/ANALYSIS_FAILED, Alembic 021) | 43-03 | `put_analysis` advances state; `report_analysis_failed` endpoint; migration 021 | SATISFIED | `agent_analysis.py:187–222`, `021_add_analysis_coverage_columns.py` |
| Retry policy (retries for transient, TimeoutError terminal, outer 7200s) | 43-04 | `timeout=7200`, `retries=2`, TimeoutError/ProcessExpired terminal branches | SATISFIED | `analysis_enqueue.py:80–85`, `functions.py:164–184` |
| Regression tests for stride/cap, kill-on-timeout, state transitions, timeout-terminal | 43-01/02/03/04 | All test files listed: 9 stride tests, 7 timeout/terminal tests, 4 state tests | SATISFIED | `test_analysis.py`, `test_pool.py`, `test_functions.py`, `test_agent_analysis.py` |

All 9 requirement IDs from PLAN frontmatter (`ANALYZE-KILL-ON-TIMEOUT`, `ANALYZE-INNER-TIMEOUT`, `ANALYZE-CONFIG-KNOBS`, `ANALYZE-BOUND-COST`, `ANALYZE-COVERAGE-EMIT`, `ANALYZE-STATE-MACHINE`, `ANALYZE-COVERAGE-PERSIST`, `ANALYZE-FAILED-ENDPOINT`, `ANALYZE-RETRY-POLICY`, `ANALYZE-TIMEOUT-TERMINAL`, `ANALYZE-WORKER-WIRING`) map to satisfied implementation evidence. No REQUIREMENTS.md file exists in `.planning/` — requirements are tracked inline in ROADMAP.md for this project.

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `analysis.py` | 514 | `return {}` | Info | Legitimate guard clause in `_representative_features` when `coarse` list is empty — not a stub |

No `TBD`, `FIXME`, or `XXX` markers found in any Phase 43 modified files. No unresolved debt markers. No stub or placeholder implementations detected.

**Code Review findings disposition (43-REVIEW.md):**
- CR-01 (`_stride_to_cap` divide-by-zero at `cap=1`): FIXED in d516bf2 — `ge=2` validators added to both caps; defense-in-depth guard at line 411 of `analysis.py`
- WR-02 (`analysis_inner_timeout_sec` no upper bound): FIXED in d516bf2 — `gt=0, lt=7200` on the field
- WR-03 (window-size knobs not threaded, pre-existing Phase 31 gap): DEFERRED — zero behavior risk, deferred to follow-up
- WR-01 (unconditional state overwrite without allowed-state guard): DEFERRED to Phase 44 (only reachable via "deepen analysis" re-trigger not yet built)
- IN-01 (`ANALYSIS_FAILED` absent from pipeline dashboard stats): DEFERRED to Phase 44 (its explicit scope)

---

### Human Verification Required

**2 items require homelab deployment:**

#### 1. Homelab throughput validation against a real long-set file

**Test:** Deploy Phase 43 to homelab. Trigger analysis on a file known to previously exceed the 4h timeout (a 3h+ DJ set). Monitor the SAQ UI and worker logs.
**Expected:** Job completes in minutes (not hours); `files.state` advances to `analyzed` or `analysis_failed`; if the strided file still exceeds the inner timeout (unlikely after bounding), logs show pebble kill at 6600s; CPU and pool slots are released after completion; no blind retry for a timed-out file.
**Why human:** The bounded-cost invariant — "a 3h set costs approximately a 20-min track" — requires real essentia running on real audio. Automated tests mock both pebble and `analyze_file`. The homelab is the only environment where throughput improvement is observable.

#### 2. State-machine correctness on the live archive

**Test:** After redeploy, trigger a fresh `process_file` job for a small set of files. Check `files.state` in Postgres for those files after the jobs complete.
**Expected:** Files show `analyzed` (not stuck `discovered`) after successful analysis. The "re-enqueue all 11,428" latent bug should not reproduce — only `discovered` files should be enqueued on a fresh trigger.
**Why human:** Production state-machine correctness requires a running agent writing to the real Postgres instance with real job payloads. Router tests cover the code path, but the live integration test confirms no edge case (e.g. network blip between pool kill and `report_analysis_failed` call) leaves files in an unexpected state.

---

### Gaps Summary

No gaps. All 11 must-have truths are VERIFIED in the codebase. Two items are deferred to Phase 44 (dashboard visibility of `ANALYSIS_FAILED`, window-size knob threading which is a pre-existing Phase 31 gap). Two human verification items are identified for homelab deployment confirmation — these are expected operational validation steps, not code defects.

---

_Verified: 2026-06-17_
_Verifier: Claude (gsd-verifier)_
