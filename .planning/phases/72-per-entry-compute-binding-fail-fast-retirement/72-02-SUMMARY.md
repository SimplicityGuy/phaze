---
phase: 72-per-entry-compute-binding-fail-fast-retirement
plan: 02
subsystem: analyze/backends
tags: [fail-fast-retirement, compute-backend, multi-compute, byte-identical, MCOMP-01]
requires:
  - phaze.services.backends.resolved_non_local_kind
  - phaze.config.ControlSettings.active_compute_scratch_dir
  - D-06 golden ≤1-compute characterization (Plan 01)
provides:
  - resolved_non_local_kind returns "compute" for N compute-only backends (no >1 raise)
  - active_compute_scratch_dir returns first compute entry's scratch_dir for N compute (no >1 raise), ≤1 byte-identical
  - a backends.toml with 2+ compute backends is accepted through both accessors (success criterion 3)
affects:
  - Plan 03 (per-entry compute binding rewire) — consumes the now-N-tolerant accessors
  - Phase 73 (MCOMP-03) — replaces active_compute_scratch_dir with per-agent scratch resolution
tech-stack:
  added: []
  patterns:
    - generalize-not-descope for fail-fasts (WR-01, Phase 70) — drop the >1 raise, keep ≤1 byte-identical
    - flip raise-asserting characterization tests to assert the generalized no-raise behavior
key-files:
  created: []
  modified:
    - src/phaze/services/backends.py
    - src/phaze/config.py
    - tests/analyze/services/test_backends.py
    - tests/shared/config/test_bucket_registry.py
decisions:
  - Compute-only branch of resolved_non_local_kind falls through to return non_local[0].kind, yielding "compute" for any N compute (per-agent dispatch attribution deferred to Phase 73).
  - active_compute_scratch_dir returns the FIRST compute entry's scratch_dir for N compute as a documented TRANSITIONAL reduction (D-07); ≤1 return is byte-identical; per-agent widening is Phase 73 (MCOMP-03), agent_push.py unchanged in Phase 72.
  - Task 3 made no source edits — it is behavior-preserving confirmation only (golden + backend + registry suites re-run green, only the two intentionally-flipped raise tests differ).
metrics:
  duration: ~8m
  completed: 2026-07-05
  tasks: 3
  files: 4
---

# Phase 72 Plan 02: Retire the Two ≤1-Compute Fail-Fasts (D-03) Summary

Both compute-only `>1` fail-fasts are retired: `resolved_non_local_kind` now returns `"compute"` for
N compute-only backends and `active_compute_scratch_dir` returns the first compute entry's `scratch_dir`
for N compute — the ≤1-compute return values stay byte-identical (D-07) and the Plan-01 golden stays
green. A `backends.toml` declaring 2+ compute backends is now accepted through both accessors where it
previously 500'd (success criterion 3).

## What Was Built

**Task 1 — `resolved_non_local_kind` (`src/phaze/services/backends.py`):**
- Deleted the `if len(non_local) > 1: raise ValueError(... PROV-01 ...)` block so the compute-only
  branch falls through to `return non_local[0].kind`, yielding `"compute"` for any N compute-only
  registry (the D-03 generalization / discretion confirmation).
- Left the `not cloud_enabled -> "local"` guard and the `any(kind == "kueue") -> "kueue"` branch
  UNCHANGED (they already tolerate N Kueue, WR-01).
- Updated the docstring: the compute-only branch now returns `"compute"` for N compute; per-agent
  dispatch attribution lands in Phase 73.
- Flipped `test_resolved_non_local_kind_raises_on_multiple_compute_only` →
  `test_resolved_non_local_kind_returns_compute_for_multiple_compute_only`: dropped `pytest.raises`,
  now asserts `backends.resolved_non_local_kind(settings) == "compute"` for the two-compute registry
  (kept the `cloud_enabled is True` assertion).

**Task 2 — `active_compute_scratch_dir` (`src/phaze/config.py`):**
- Deleted the `if len(compute) > 1: raise ValueError(... PROV-01 ...)` block; the accessor now falls
  through to `backend = compute[0]; return backend.scratch_dir if isinstance(...) else None`. For exactly
  one compute backend this is byte-identical (D-07); for N compute it returns the first entry's
  `scratch_dir` as a documented TRANSITIONAL reduction.
- Docstring note added: per-agent scratch resolution replaces this global accessor in Phase 73
  (MCOMP-03); `agent_push.py` stays byte-identical in Phase 72.
- Flipped `test_multiple_compute_backends_scratch_dir_raises` →
  `test_multiple_compute_backends_scratch_dir_no_longer_raises`: dropped `pytest.raises`, now asserts
  `settings.active_compute_scratch_dir == "/scratch/a"` (first entry) with no raise.

**Task 3 — behavior-preserving confirmation (no source edits):**
- Re-ran the Plan-01 golden + backend + registry suites and mypy. All green; only the two intentionally
  flipped raise tests differ from the pre-change assertions. The golden module was not touched.

## Verification Results

- `grep -n "len(non_local) > 1" src/phaze/services/backends.py` → nothing.
- `grep -n "len(compute) > 1" src/phaze/config.py` → nothing.
- `uv run pytest tests/analyze/services/test_compute_binding_golden.py tests/analyze/services/test_backends.py tests/shared/config/test_bucket_registry.py tests/shared/config/test_backend_registry.py -q` → **63 passed**.
- `uv run mypy src/phaze/config.py src/phaze/services/backends.py` → **Success: no issues found**.
- `uv run ruff check src/phaze/config.py src/phaze/services/backends.py` → **All checks passed**.

## must_haves Coverage

- **D-03 (resolved_non_local_kind no longer raises, returns "compute" for N compute):** met (Task 1, flipped test green).
- **D-03/D-07 (active_compute_scratch_dir no longer raises for N compute; ≤1 byte-identical):** met (Task 2; `local+2kueue+1compute` ≤1 case still returns `/srv/scratch`).
- **2+ compute backends accepted through both accessors:** met (both flipped tests assert the no-raise path).
- **D-06 Plan-01 golden stays green:** met (golden module unchanged, re-run green in Task 3).

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None. The N-compute `active_compute_scratch_dir` first-entry reduction is a documented transitional
behavior (T-72-02-01, disposition `accept`) whose per-agent widening is scheduled for Phase 73 (MCOMP-03),
not an unwired stub. The real target deploy is `local + N-Kueue + 1-compute` (≤1 compute), so N-compute
is groundwork not yet dispatched.

## Self-Check: PASSED
