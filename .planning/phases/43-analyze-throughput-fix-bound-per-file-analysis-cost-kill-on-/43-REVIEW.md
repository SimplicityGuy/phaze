---
phase: 43-analyze-throughput-fix-bound-per-file-analysis-cost-kill-on-
reviewed: 2026-06-17T00:00:00Z
depth: standard
files_reviewed: 13
files_reviewed_list:
  - alembic/versions/021_add_analysis_coverage_columns.py
  - src/phaze/config.py
  - src/phaze/models/analysis.py
  - src/phaze/models/file.py
  - src/phaze/routers/agent_analysis.py
  - src/phaze/routers/pipeline.py
  - src/phaze/schemas/agent_analysis.py
  - src/phaze/services/agent_client.py
  - src/phaze/services/analysis_enqueue.py
  - src/phaze/services/analysis.py
  - src/phaze/tasks/agent_worker.py
  - src/phaze/tasks/functions.py
  - src/phaze/tasks/pool.py
findings:
  critical: 1
  warning: 3
  info: 1
  total: 5
status: resolved
resolution:
  fixed: [CR-01, WR-02]
  deferred: [WR-03, WR-01, IN-01]
  fixed_commit: d516bf2
---

# Phase 43: Code Review Report

**Reviewed:** 2026-06-17
**Depth:** standard
**Files Reviewed:** 13
**Status:** resolved (2 in-scope findings fixed in d516bf2; 3 deferred — see Resolution)

## Resolution (orchestrator triage)

Each finding was verified against the live code before acting:

- **CR-01 (Critical) — FIXED (d516bf2).** Confirmed real: `_stride_to_cap` divides by `cap-1`. Phase 43's own field. Added `ge=2` validators on `analysis_fine_cap`/`analysis_coarse_cap` (even-stride always keeps first+last, so <2 is invalid) plus a defense-in-depth guard in `_stride_to_cap`. Tests added.
- **WR-02 (Warning) — FIXED (d516bf2).** Confirmed real: `analysis_inner_timeout_sec` had no upper bound. Phase 43's own field. Added `gt=0, lt=7200` so a misconfig can't disable the deterministic inner<outer kill. Test added.
- **WR-03 (Warning) — DEFERRED (out of scope).** Verified the baseline (`origin/main`) call site forwarded only `original_path`/`models_path` — the three window-size knobs were **never** wired since Phase 31. This is a pre-existing Phase 31 gap, not a Phase 43 regression, and is zero-behavior-risk (config defaults equal `analyze_file`'s module defaults). Track as Phase 31 follow-up.
- **WR-01 (Warning) — DEFERRED to Phase 44.** State-regression on re-PUT is only reachable via Phase 44's "deepen analysis" re-trigger (the current analyze enqueue path only targets `discovered` files). The guard's allowed-source-state semantics should be decided alongside that feature.
- **IN-01 (Info) — DEFERRED to Phase 44.** Adding `ANALYSIS_FAILED` to the dashboard pipeline stages is Phase 44's explicit scope (surface analysis outcomes).

## Summary

Phase 43 lands four key changes: (1) the even-stride downsampler that bounds per-file essentia cost to O(1) window count, (2) the pebble `ProcessPool` replacement for deterministic child-kill on timeout, (3) the `retries=2` policy that stops wasteful re-runs of deterministically-failing long files, and (4) the `put_analysis` state-advance fix and the new `report_analysis_failed` endpoint that finally move files out of `DISCOVERED` after analysis.

The core logic is sound and the design invariants (inner timeout < outer, retries=2 semantics, TERMINAL classification) are correctly implemented. Two defects stand out: a `ZeroDivisionError` crash in `_stride_to_cap` when any cap is set to exactly 1 (triggerable via env var with no validation guard), and an unconditional state overwrite in `put_analysis` / `report_analysis_failed` that can regress a file from a late-pipeline state back to `ANALYZED` or `ANALYSIS_FAILED`. Three additional warnings follow.

---

## Critical Issues

### CR-01: `_stride_to_cap` crashes with `ZeroDivisionError` when `cap=1`

**File:** `src/phaze/services/analysis.py:411`
**Issue:** The stride formula `round(i * (n - 1) / (cap - 1))` divides by `cap - 1`. When `cap=1` and `n > 1` (more windows than the cap), this raises an unhandled `ZeroDivisionError` inside the pebble child process. Pebble surfaces child crashes as `ProcessExpired`, which the caller classifies as a deterministic terminal failure and calls `report_analysis_failed`. Every file on a deployment where `PHAZE_ANALYSIS_FINE_CAP=1` or `PHAZE_ANALYSIS_COARSE_CAP=1` is set would be permanently marked `ANALYSIS_FAILED` with no diagnostic clue.

The `AgentSettings.analysis_fine_cap` / `analysis_coarse_cap` fields carry no `ge=2` validator, so a value of `1` passes pydantic construction and silently arms the crash.

**Fix:**
```python
# In _stride_to_cap, add a guard for cap == 1 BEFORE the set comprehension:
def _stride_to_cap(windows, cap):
    n = len(windows)
    if cap <= 0 or n <= cap:
        return windows, False
    if cap == 1:
        # Keep only the first window (or the only window when n==1, already handled above).
        return [windows[0]], True
    picks = {round(i * (n - 1) / (cap - 1)) for i in range(cap)}
    kept = [windows[p] for p in sorted(picks)]
    return kept, True
```

Also add `ge=2` validators in `AgentSettings`:
```python
analysis_fine_cap: int = Field(default=60, ge=2, ...)
analysis_coarse_cap: int = Field(default=30, ge=2, ...)
```

---

## Warnings

### WR-01: `put_analysis` and `report_analysis_failed` unconditionally overwrite state without a guard clause

**File:** `src/phaze/routers/agent_analysis.py:188,213`
**Issue:** Both state-advancing `UPDATE` statements have no `WHERE state IN (...)` guard. A non-empty `PUT /analysis/{file_id}` against a file that is already `APPROVED`, `EXECUTED`, or `MOVED` rewrites its state to `ANALYZED`, silently regressing it through the pipeline. Similarly, `POST /analysis/{file_id}/failed` can demote any file — including one currently `APPROVED` — to `ANALYSIS_FAILED`. In a misbehaving agent scenario (e.g. the agent retries a stale job for a file that has already been approved), this silently corrupts durable pipeline state.

The system is single-user and the endpoints require agent auth, so severity is lower than a public API, but the regressability is a real data-loss risk.

**Fix:**
```python
# put_analysis: restrict state advance to pre-analysis states only
if dumped:
    await session.execute(
        update(FileRecord)
        .where(FileRecord.id == file_id)
        .where(FileRecord.state.in_([
            FileState.DISCOVERED,
            FileState.METADATA_EXTRACTED,
            FileState.FINGERPRINTED,
        ]))
        .values(state=FileState.ANALYZED)
    )

# report_analysis_failed: restrict to pre-terminal states only
await session.execute(
    update(FileRecord)
    .where(FileRecord.id == file_id)
    .where(FileRecord.state.in_([
        FileState.DISCOVERED,
        FileState.METADATA_EXTRACTED,
        FileState.FINGERPRINTED,
        FileState.ANALYZED,  # allow re-failing a previously-analyzed file
    ]))
    .values(state=FileState.ANALYSIS_FAILED)
)
```

### WR-02: `analysis_inner_timeout_sec` has no validator enforcing it stays strictly below the 7200s outer SAQ timeout

**File:** `src/phaze/config.py:446-450`
**Issue:** The description states "MUST stay below the 7200s SAQ `process_file` net so the kill is deterministic" but there is no `le=7199` (or similar) validator. If an operator sets `PHAZE_ANALYSIS_INNER_TIMEOUT_SEC=7200` (or higher), the inner pebble timeout fires simultaneously with (or after) the outer SAQ timeout. SAQ's `asyncio.wait_for` fires first and cancels the coroutine; the pebble kill never runs. The child process leaks, and the job is retried by SAQ rather than classified terminal — exactly the regression this feature was designed to prevent.

**Fix:**
```python
analysis_inner_timeout_sec: int = Field(
    default=6600,
    ge=1,
    le=7100,  # strictly below the hardcoded 7200s outer SAQ timeout with margin
    validation_alias=AliasChoices("PHAZE_ANALYSIS_INNER_TIMEOUT_SEC", "analysis_inner_timeout_sec"),
    description="Inner pebble per-task analysis timeout; must stay strictly below the 7200s SAQ process_file net (Phase 43).",
)
```

### WR-03: `fine_window_sec`, `coarse_window_sec`, and `fine_min_sec` are never plumbed from `AgentSettings` to `analyze_file`

**File:** `src/phaze/tasks/functions.py:155-163`
**Issue:** `process_file` passes `fine_cap` and `coarse_cap` from `AgentSettings` to `analyze_file` via `run_in_process_pool`, but does NOT pass `fine_window_sec`, `coarse_window_sec`, or `fine_min_sec`. Those three parameters are configurable on `AgentSettings` (Phase 31), but `analyze_file` always uses its hardcoded module-level defaults (`30s`, `180s`, `15s`). An operator who overrides `PHAZE_ANALYSIS_FINE_WINDOW_SEC` or `PHAZE_ANALYSIS_COARSE_WINDOW_SEC` will see no effect.

This is a pre-existing Phase 31 omission that Phase 43 touched the same call site and had an opportunity to fix but did not.

**Fix:**
```python
analysis = await run_in_process_pool(
    ctx,
    _load_analyze_file(),
    payload.original_path,
    payload.models_path,
    timeout=cfg.analysis_inner_timeout_sec,
    fine_window_sec=cfg.analysis_fine_window_sec,
    coarse_window_sec=cfg.analysis_coarse_window_sec,
    fine_min_sec=cfg.analysis_fine_min_sec,
    fine_cap=cfg.analysis_fine_cap,
    coarse_cap=cfg.analysis_coarse_cap,
)
```

---

## Info

### IN-01: `ANALYSIS_FAILED` is absent from `PIPELINE_STAGES` — failed files inflate `analyzeTotal` without contributing to `analyzeDone`

**File:** `src/phaze/services/pipeline.py:40-48` (context; not a Phase 43 file)
**Issue:** `ANALYSIS_FAILED` is not in the `PIPELINE_STAGES` list used by `get_pipeline_stats`. Files permanently marked `ANALYSIS_FAILED` count toward the music/video total (the `analyze.total` denominator in `get_stage_progress`) but not toward `analyze.done` (which counts rows in the `analysis` table). As the archive accumulates permanently-failed files the analyze stage progress bar will never reach 100%.

This is cosmetic (the data is accurate, just incomplete in display), but an operator may be confused by a stuck progress bar. Consider adding `ANALYSIS_FAILED` to the pipeline stats or adjusting the `analyze.total` denominator to exclude `ANALYSIS_FAILED` files.

**Fix:** Either exclude `ANALYSIS_FAILED` files from `analyze.total`, or add a dedicated `analysis_failed` key to the stats dict so the dashboard can surface failed-file counts separately.

---

_Reviewed: 2026-06-17_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
