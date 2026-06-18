---
phase: 43-analyze-throughput-fix
plan: 02
subsystem: analysis
tags: [essentia, windowing, cost-bound, coverage, sampling]
requires:
  - "src/phaze/services/analysis.py::_iter_windows (full natural window list, contiguous idx)"
provides:
  - "_stride_to_cap pure even-stride downsampler (preserves original window_index)"
  - "cap-aware _analyze_fine_windows/_analyze_coarse_windows (return windows, total, sampled)"
  - "analyze_file five-field coverage contract: fine_windows_analyzed/total, coarse_windows_analyzed/total, sampled"
  - "fine_cap/coarse_cap keyword args on analyze_file (defaults 60/30)"
affects:
  - "Plan 03 (persists the five coverage keys as columns)"
  - "Plan 04 (forwards coverage through the task payload)"
tech-stack:
  added: []
  patterns:
    - "Post-generation pure downsample preserving original idx (keeps _iter_windows side-effect-free)"
    - "Endpoint-inclusive even stride round(i*(n-1)/(cap-1)) with set-dedup of rounding collisions"
key-files:
  created: []
  modified:
    - "src/phaze/services/analysis.py"
    - "tests/test_services/test_analysis.py"
decisions:
  - "Cap defaults 60 fine / 30 coarse as module constants (_DEFAULT_FINE_CAP/_DEFAULT_COARSE_CAP), overridable via analyze_file kwargs — config-source wiring deferred to a later plan per task scope."
  - "*_total = natural pre-stride window count; *_analyzed = successful appends (post-stride, minus per-window skips), matching RESEARCH §Q2."
  - "sampled = fine_sampled OR coarse_sampled."
metrics:
  duration: ~25m
  tasks: 2
  files: 2
  completed: 2026-06-17
---

# Phase 43 Plan 02: Bound Per-File Analysis Cost (Cap + Even Stride) Summary

Bounded per-file essentia cost to a constant (<=60 fine / <=30 coarse windows) by striding evenly across the WHOLE file when a file's natural window count exceeds the cap, and emit a five-field coverage contract so sampled files can be re-deepened later (Phase 44).

## What Was Built

**Task 1 — `_stride_to_cap` pure even-stride downsampler** (`src/phaze/services/analysis.py`)
- Module-level pure function `_stride_to_cap(windows, cap) -> (kept, sampled)`.
- `cap <= 0` or `len(windows) <= cap` → `(windows, False)` (no-op).
- Over cap → endpoint-inclusive even stride `picks = {round(i*(n-1)/(cap-1)) for i in range(cap)}`, `kept = [windows[p] for p in sorted(picks)]`, `sampled=True`.
- First and last original windows always retained (whole-file span); kept tuples keep their ORIGINAL idx (no renumbering); `set` dedups rounding collisions so `len(kept) <= cap`.

**Task 2 — Cap-aware iterators + coverage emit** (`src/phaze/services/analysis.py`)
- Added module constants `_DEFAULT_FINE_CAP = 60`, `_DEFAULT_COARSE_CAP = 30`.
- `_analyze_fine_windows` / `_analyze_coarse_windows` now take a `cap` param, apply `_stride_to_cap` to the `_iter_windows` result BEFORE the per-window decode loop, and return `(windows, total, sampled)` (total = natural pre-stride count).
- `analyze_file` gained keyword-only `fine_cap`/`coarse_cap` (default to the constants) and now returns the five coverage keys (`fine_windows_analyzed`, `fine_windows_total`, `coarse_windows_analyzed`, `coarse_windows_total`, `sampled`) alongside the unchanged aggregate keys. `sampled = fine_sampled or coarse_sampled`.
- Under cap, behavior is byte-for-byte unchanged (every window analyzed; sampled False).

## How It Was Verified

- `uv run pytest tests/test_services/test_analysis.py -q` → 43 passed (8 stride + 3 coverage/cap + 1 strided-aggregate added this plan, plus the 31 pre-existing).
- `uv run mypy src/phaze/services/analysis.py` clean; `uv run ruff check src/phaze/services/analysis.py` clean.
- `uv run pytest tests/test_tasks/ -q` → 136 passed (the `tasks/functions.py::analyze_file` caller path is green; analyze_file change is additive only). 10 setup ERRORS in `test_recovery.py`/`test_scan_reaper.py` are pre-existing asyncpg/DB-connection failures unrelated to this plan (out of scope).
- All commits passed pre-commit hooks (ruff, ruff-format, bandit, mypy) without `--no-verify`.

## TDD Gate Compliance

Both tasks followed RED → GREEN:
- Task 1: `test(43-02)` 3295cc1 (RED, ImportError) → `feat(43-02)` 6752016 (GREEN).
- Task 2: `test(43-02)` df63eab (RED, TypeError/KeyError) → `feat(43-02)` 3caf4a0 (GREEN).

## Threat Model Outcomes

- **T-43-03 (DoS — unbounded O(duration) compute):** mitigated. Cap + even stride bound per-file cost to <=60 fine / <=30 coarse windows regardless of length.
- **T-43-04 (Tampering — sampled aggregates silently reported as complete):** mitigated. `sampled` flag + the four coverage counts are emitted so downstream (Plan 03 columns, Phase 44 badge) can distinguish partial from full.

No new trust boundary crossed (pure compute on a path already in the payload).

## Deviations from Plan

None — plan executed exactly as written.

## Commits

- `3295cc1` test(43-02): add failing tests for _stride_to_cap even-stride downsampler
- `6752016` feat(43-02): add _stride_to_cap even-stride downsampler
- `df63eab` test(43-02): add failing tests for cap-bounded coverage emit
- `3caf4a0` feat(43-02): cap-bound fine/coarse passes + emit coverage contract

## Notes for Downstream Plans

- Plan 03 should persist the exact key names `fine_windows_analyzed` / `fine_windows_total` / `coarse_windows_analyzed` / `coarse_windows_total` / `sampled`.
- Plan 04 forwards these through the task payload.
- Cap values are currently module constants overridable per-call; wiring `AgentSettings.analysis_fine_cap` / `analysis_coarse_cap` (RESEARCH §Q2 suggested config fields) into the agent worker's `analyze_file` call is NOT done here and remains available for a config-source plan if desired.

## Known Stubs

None.

## Self-Check: PASSED
- FOUND: src/phaze/services/analysis.py (_stride_to_cap + caps + coverage keys present)
- FOUND: tests/test_services/test_analysis.py (stride + coverage tests; 43 passed)
- FOUND commits: 3295cc1, 6752016, df63eab, 3caf4a0
