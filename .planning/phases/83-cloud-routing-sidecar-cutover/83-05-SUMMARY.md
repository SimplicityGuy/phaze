---
phase: 83-cloud-routing-sidecar-cutover
plan: 05
subsystem: cloud-routing
tags: [trigger_analysis, hold-path, cloud_job, awaiting, shadow-compare, sidecar]
requires:
  - "hold_awaiting_cloud() shared writer (83-01)"
  - "migration 034 existing-corpus backfill (83-02)"
provides:
  - "trigger_analysis hold path cut over to the shared awaiting writer (go-forward half of D-01)"
  - "shadow-compare integration coverage proving the awaiting_cloud HARD invariant is green on the held-file fixture"
affects:
  - "83-06 (drain reader now sees every go-forward hold carrying its awaiting sidecar row)"
tech-stack:
  added: []
  patterns:
    - "call-site swap to the shared services/ writer; the writer never commits, the caller's post-loop commit stays"
    - "hermetic shadow-compare fixture: GREEN consistent-cell paired with the existing parametrized RED divergent-cell (non-vacuous)"
key-files:
  created: []
  modified:
    - "src/phaze/routers/pipeline.py"
    - "tests/integration/test_shadow_compare.py"
decisions:
  - "D-01: trigger_analysis's long-file hold calls hold_awaiting_cloud(session, file) (attempts=0) instead of a bare file.state = FileState.AWAITING_CLOUD, so every go-forward hold carries its cloud_job(status='awaiting') sidecar row"
  - "D-00d: the Phase-79 shadow gate stays green with implication-not-equality preserved; no converse invariant added, no shadow_compare.py source change"
metrics:
  duration: "~20m"
  completed: "2026-07-09"
  tasks: 2
  files: 2
---

# Phase 83 Plan 05: Trigger-Analysis Hold-Path Cutover Summary

Cut `trigger_analysis`'s go-forward long-file hold over to the shared writer `hold_awaiting_cloud`
(D-01) so every new `AWAITING_CLOUD` hold now carries its `cloud_job(status='awaiting', attempts=0)`
sidecar row, and proved the Phase-79 shadow gate stays green on a held-file fixture carrying that row
(D-00d). This is the go-forward half of closing D-01; migration `034` (83-02) is the existing-corpus
half. Without it, new holds would keep re-opening the hard-invariant violation `034` just repaired.

## What Was Built

- **`routers/pipeline.py` — `_route_discovered_by_duration` hold loop** (the seam shared by "Run
  Analysis" and the backfill producer): replaced the bare `file.state = FileState.AWAITING_CLOUD`
  (was `:346`) with `await hold_awaiting_cloud(session, file)` (attempts defaults to 0 — a fresh hold
  has spent no budget). Added `hold_awaiting_cloud` to the existing
  `from phaze.services.backends import ...` line. The existing post-loop `await session.commit()`
  (the hold's own commit boundary — the helper never commits) is retained unchanged; the long-file
  threshold logic and the non-long branches are untouched.

- **`tests/integration/test_shadow_compare.py` — two new hermetic cells** (no change to
  `services/shadow_compare.py`):
  - `test_awaiting_cloud_green_on_held_file_carrying_awaiting_row` — seeds an `AWAITING_CLOUD` file
    WITH its `cloud_job(status='awaiting')` row (the exact shape the writer + `034` guarantee) and
    asserts zero HARD divergence on the `awaiting_cloud` invariant and `hard_fail_total == 0`. Its
    non-vacuity is provided by the existing parametrized `test_divergent_hard_invariant_flags` RED
    cell (an `AWAITING_CLOUD` file with NO row flags), which is violated at HEAD without the writer.
  - `test_local_analyzing_carrying_awaiting_row_does_not_violate_hard` — seeds a `LOCAL_ANALYZING`
    file carrying an inert `awaiting` row (post-D-13 local dispatch, pre-D-14 reap) and asserts it
    drives NO hard-invariant flag (`hard_fail_total == 0`), `awaiting_cloud.count == 0` (outside that
    invariant's scope), and `local_analyzing` stays soft-counted only — the implication-not-equality
    contract (79 D-04) in action.

## Verification

- `uv run ruff check` + `uv run mypy src/phaze/routers/pipeline.py`: clean.
- Grep audit: `trigger_analysis`/hold loop calls `hold_awaiting_cloud`; no bare
  `file.state = FileState.AWAITING_CLOUD` remains in the hold loop (the surviving `FileState.AWAITING_CLOUD`
  references are the held-file read at `:860`, not a write); `services/shadow_compare.py` unmodified
  (`git diff` empty).
- `just test-bucket shared` in isolation: **997 passed, 0 failed** (covers `test_pipeline.py`,
  `test_routing_seam.py`, and the trigger_analysis route — existing hold-state assertions stay green
  because `hold_awaiting_cloud` dual-writes `file.state`).
- `just test-bucket integration` in isolation: **161 passed, 0 failed**, including the two new cells.

## Deviations from Plan

None — plan executed exactly as written. No code deviations (Rules 1–4) were required.

**TDD note:** Task 1's `<files>` is source-only (`pipeline.py`) and Task 2's is test-only
(`test_shadow_compare.py`), so the RED/GREEN split maps across the two tasks rather than within one
file: Task 1 swaps the call site (existing shared-bucket behavior tests confirm no regression), and
Task 2's GREEN consistent-cell is paired with the pre-existing parametrized RED divergent-cell that
proves the assertion is non-vacuous. The declared `files_modified` scope was honored strictly; no
other test file (e.g. `tests/shared/core/test_routing_seam.py`) was touched, and the sibling-owned
`routers/agent_s3.py` / `routers/agent_push.py` were not touched.

## Notes for Downstream Plans

- **83-06 (drain reader):** every go-forward hold now carries its `cloud_job(status='awaiting')` row,
  so the sidecar-based drain-candidate query sees holds without needing a `FileRecord.state` read.
- The D-14 reaper (analyze-terminal `DELETE ... WHERE status='awaiting'`) is still required to bound
  `ix_cloud_job_awaiting` growth; the LOCAL_ANALYZING-with-inert-row case is now covered by the new
  shadow test, confirming the pre-reap window is safe under implication-not-equality.

## Self-Check: PASSED

- `src/phaze/routers/pipeline.py` — FOUND (imports and calls `hold_awaiting_cloud`; no bare
  `file.state = FileState.AWAITING_CLOUD` in the hold loop).
- `tests/integration/test_shadow_compare.py` — FOUND (two new cells present, both passing).
- Commit `d68e6c09` (feat) — FOUND.
- Commit `90900083` (test) — FOUND.
