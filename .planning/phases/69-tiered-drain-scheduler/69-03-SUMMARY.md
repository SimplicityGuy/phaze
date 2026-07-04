---
phase: 69-tiered-drain-scheduler
plan: 03
subsystem: scheduler
tags: [reconcile, advisory-lock, per-backend-dispatch, spill-back, cap-safe, SCHED-02, SCHED-03, SCHED-05, D-04]
requires:
  - "src/phaze/tasks/release_awaiting_cloud._STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY (5_000_504) — the drain's per-tick advisory lock (Plan 02)"
  - "src/phaze/services/backends.KueueBackend backend_id-scoped reconcile substrate (Phase 68 / Plan 02)"
  - "src/phaze/services/backends.resolve_backends N-backend registry resolution (Plan 02, SCHED-01)"
provides:
  - "src/phaze/services/backends.KueueBackend.reconcile sharing the drain's advisory lock per-row (cap-safe) + returning its per-backend tally"
  - "src/phaze/tasks/reconcile_cloud_jobs.reconcile_cloud_jobs per-backend dispatch (no global un-scoped cloud_job query)"
  - "src/phaze/tasks/reconcile_cloud_jobs at-cap spill-back to AWAITING_CLOUD (cloud flakiness never hard-fails)"
affects:
  - "Later 69 waves / recover_orphaned_work: compute cloud_job rows are now single-owner (only their /pushed callback touches them)"
tech-stack:
  added: []
  patterns:
    - "per-row pg_advisory_xact_lock(5_000_504) at the TOP of each reconcile row unit (per-row granularity — the commit auto-releases the xact lock, preserving delete-after-record ordering)"
    - "per-backend reconcile dispatch (for b in resolve_backends(cfg): await b.reconcile(session, ctx)) via a function-local deferred import to break the backends<->reconcile_cloud_jobs cycle"
    - "at-cap spill-back: terminalize cloud_job (FAILED decrements in-flight) + flip FileRecord to AWAITING_CLOUD so the next drain tick routes to local via attempts>=cap"
key-files:
  created: []
  modified:
    - "src/phaze/services/backends.py"
    - "src/phaze/tasks/reconcile_cloud_jobs.py"
    - "tests/analyze/services/test_backends.py"
    - "tests/analyze/tasks/test_reconcile_cloud_jobs.py"
decisions:
  - "SCHED-02: reconcile shares the drain's pg_advisory_xact_lock(5_000_504) per-row (not whole-tick — Pitfall 2); reconcile only ever DECREMENTS in-flight (never claims), so the single shared drain lock is provably cap-safe"
  - "SCHED-05: reconcile is dispatched per-backend; the global un-scoped select(CloudJob WHERE status IN {SUBMITTED,RUNNING}) query is removed so a compute row is owned only by its /pushed callback"
  - "SCHED-03/D-04: at the cloud cap the file spills back to AWAITING_CLOUD (not ANALYSIS_FAILED); local failure is the only terminal into ANALYSIS_FAILED"
  - "reconcile return type widened to dict[str,int] | None: Kueue returns its per-backend tally for the cron to aggregate; Local/Compute stay None (Rule 3 — required to preserve the cron's return-tally contract the existing tests assert on)"
metrics:
  duration: "~1h"
  completed: "2026-07-04"
  tasks: 2
  files_created: 0
  files_modified: 4
---

# Phase 69 Plan 03: Cap-Safe Backend-Scoped Reconcile + Spill-Back Summary

The reconcile cron becomes cap-safe and single-owner. `KueueBackend.reconcile` now acquires the drain's advisory lock `5_000_504` at the top of each per-row unit of work so a reconcile row-mutation and a `stage_cloud_window` snapshot are mutually exclusive (SCHED-02). The cron's monolithic global `cloud_job` query is replaced by per-backend dispatch `for b in resolve_backends(cfg): await b.reconcile(session, ctx)` (via a function-local deferred import — no circular import), so a compute row is owned only by its `/pushed` callback (SCHED-05). At the cloud cap a failed cloud_job now spills the file back to `AWAITING_CLOUD` instead of `ANALYSIS_FAILED` — the next drain tick routes it to the guaranteed local safety net via `attempts >= cap`, so cloud flakiness never hard-fails a processable file (SCHED-03/D-04).

## What Was Built

### Task 1 — Per-row advisory lock + per-backend reconcile dispatch (SCHED-02/05) — commit `b3d8f25`
- `src/phaze/services/backends.py`: `KueueBackend.reconcile` executes `SELECT pg_advisory_xact_lock(:key)` (key `5_000_504`, imported from `release_awaiting_cloud`) as the FIRST statement inside each `for cloud_job_id in cloud_job_ids:` iteration — per-row, not whole-tick (Pitfall 2: a whole-tick lock breaks the delete-after-record ordering which commits mid-tick; `_reconcile_one`'s per-row commit auto-releases the xact lock). The existing capture-ids-then-get-fresh structure and the per-row `except -> session.rollback()` guard are preserved. `backends.py` already imports `push_file_job_key` from `release_awaiting_cloud`, so the new `_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY` import adds no cycle; `text` was added to the `sqlalchemy` import.
- `src/phaze/tasks/reconcile_cloud_jobs.py`: `reconcile_cloud_jobs(ctx)` replaced its global un-scoped `select(CloudJob WHERE status IN {SUBMITTED,RUNNING})` loop with `for backend in resolve_backends(cfg): backend_tally = await backend.reconcile(session, ctx)` and aggregates each backend's tally. `resolve_backends` is imported **function-locally** (`# noqa: PLC0415`, mirroring `release_awaiting_cloud.py:102`) because `backends.py:54` already does a module-top `from phaze.tasks.reconcile_cloud_jobs import _reconcile_one` — a module-top import here would be a `backends -> reconcile_cloud_jobs -> backends` collection-time ImportError. Kueue does the real backend_id-scoped work; Compute/Local reconcile are no-ops → a compute row is single-owner (SCHED-05). The unused `select` import was dropped.
- Tests: `test_backends.py` gained `test_kueue_reconcile_scope_ignores_other_backend_rows` (`-k reconcile_scope`) proving a kueue reconcile pass reconciles only its own `backend_id` rows and leaves a sibling compute row byte-untouched; `test_kueue_reconcile_reads_own_backend_rows` updated to assert the returned tally (reconcile now returns a dict, not `None`). The cron harness `_patch_cap` now patches BOTH `reconcile_cloud_jobs.get_settings` and `phaze.services.backends.get_settings` (the cap is read inside `KueueBackend.reconcile`) with a one-entry kueue registry, and `_seed` stamps `backend_id=kueue-x64` so the backend_id-scoped query owns the row.

### Task 2 — At-cap spill-back to AWAITING_CLOUD (SCHED-03/D-04) — commit `f657ae9`
- `_handle_no_callback_terminal` at-cap branch (`next_attempt > cap`) now sets `FileRecord.state = AWAITING_CLOUD` instead of `ANALYSIS_FAILED`. Everything else in the branch is unchanged: `cloud_job.status = FAILED` (decrements in-flight — the reconcile-only-decrements invariant), `inadmissible=False`, `cloud_phase=None`, the record+commit BEFORE `delete_staged_object`/`delete_job` (delete-after-record ordering, D-04). `cloud_job.attempts` already equals `cap` at this branch, so the next drain tick's `select_backend` excludes every cloud backend (`attempts >= cap`) and routes the file to local — no extra increment. The warning log was reworded to "cap reached -> spill back to AWAITING_CLOUD"; module + function docstrings updated.
- Tests: `test_max_attempts_cap_then_analysis_failed` → renamed `test_max_attempts_cap_then_spill_back_to_awaiting_cloud` (asserts `AWAITING_CLOUD`, cloud_job `FAILED`, staged object + Job cleaned up). `test_vanished_job_at_cap_marks_analysis_failed` → `..._spills_back_to_awaiting_cloud`. `test_delete_after_record_ordering`'s snapshot `file_state` expectation flipped to `AWAITING_CLOUD`. Added `test_cap_safe_reconcile_decrement_never_overshoots_drain_snapshot` (`-k cap_safe`): a kueue backend starting exactly at cap (2 in-flight, cap 2) reconciles one failed row and drops to 1 in-flight — proving a reconcile decrement keeps `in_flight <= cap` (overshoot is impossible from the reconcile side).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking issue] `reconcile` return type widened to `dict[str, int] | None`**
- **Found during:** Task 1 (per-backend dispatch).
- **Issue:** The cron's existing tests assert on the returned tally (`tally["failed"]`, `tally["reconciled"]`, `tally["redriven"]`, etc.). With the cron delegating the actual reconcile work to `KueueBackend.reconcile`, the only source of those counts is inside the backend — but the `Backend.reconcile` protocol returned `None`, so the cron could not aggregate real values. The plan's own action ("aggregating each backend's tally") requires the backend to surface its tally.
- **Fix:** Widened `Backend.reconcile` (protocol + all three impls) to `dict[str, int] | None`. `KueueBackend.reconcile` returns its per-backend `tally`; `LocalBackend`/`ComputeAgentBackend` keep returning `None` (callback-driven no-ops). The cron aggregates non-`None` returns into its return dict, preserving the return-tally shape. Updated `test_kueue_reconcile_reads_own_backend_rows` (was asserting `is None`).
- **Files modified:** `src/phaze/services/backends.py`, `src/phaze/tasks/reconcile_cloud_jobs.py`, `tests/analyze/services/test_backends.py`.
- **Commit:** `b3d8f25`.

**2. [Rule 3 — Blocking issue] mypy `arg-type` on `resolve_backends(get_settings())`**
- **Found during:** Task 1 mypy gate.
- **Issue:** `get_settings()` is typed `BaseSettings`, but `resolve_backends` expects `ControlSettings`; the old cron only touched `cfg.cloud_submit_max_attempts` with a `# type: ignore`.
- **Fix:** `cfg = cast("ControlSettings", get_settings())` (mirrors `KueueBackend.reconcile`), with `ControlSettings` added under the `TYPE_CHECKING` block.
- **Files modified:** `src/phaze/tasks/reconcile_cloud_jobs.py`.
- **Commit:** `b3d8f25`.

No architectural changes; no authentication gates; no new dependencies; no migrations.

## Threat Model Compliance

- **T-69-03-01 (cap overshoot via race):** per-row `pg_advisory_xact_lock(5_000_504)` makes each reconcile row-mutation mutually exclusive with a drain tick; reconcile only ever decrements in-flight — proven cap-safe by `test_cap_safe_reconcile_decrement_never_overshoots_drain_snapshot`.
- **T-69-03-02 (unbounded retry storm):** spill-back keys off the bounded `cloud_submit_max_attempts`; at cap the file is forced to local via the attempts filter — no infinite cloud thrash (asserted by the spill-back tests: `attempts == cap`, no re-drive enqueued).
- **T-69-03-03 (SQLi):** `pg_advisory_xact_lock(:key)` bound param; `CloudJob.backend_id == self.id` bound param; no f-string SQL.
- **T-69-03-04 (info disclosure — accept):** reconcile logs only `file_id`/`cloud_job_id`/attempt/cap; no secrets.

## Verification

- `uv run pytest tests/analyze/tasks/test_reconcile_cloud_jobs.py tests/analyze/services/test_backends.py -q` — **41 passed**.
- `-k reconcile_scope`, `-k spill_back`, `-k cap_safe` each bind ≥1 test and pass.
- Full analyze bucket — **405 passed** on the full run; the 6 setup ERRORs (`test_staging_cron`, `test_cloud_staging`, `test_scheduling_ledger`) are the known colima VM-pressure DB-connection flake — re-run in isolation: **37 passed**.
- `uv run python -c "import phaze.services.backends, phaze.tasks.reconcile_cloud_jobs"` exits 0 (no circular import).
- `grep "state=FileState.ANALYSIS_FAILED" src/phaze/tasks/reconcile_cloud_jobs.py` → NONE (the at-cap branch no longer hard-fails).
- `grep -n "from phaze.services.backends import" src/phaze/tasks/reconcile_cloud_jobs.py` → only the function-local deferred import (line 298, `# noqa: PLC0415`), no module-top import.
- `uv run mypy src/phaze` — clean (157 files). All pre-commit hooks pass (ruff/ruff-format/bandit/mypy); never `--no-verify`.

## Known Stubs

None. The reconcile path is fully wired: per-backend dispatch, per-row advisory lock, and at-cap spill-back are all exercised by the target suite. Local/Compute `reconcile` are intentional no-ops (their terminalization is the synchronous local path / the `/pushed` callback respectively) — not stubs.

## Self-Check: PASSED

- SUMMARY.md present at `.planning/phases/69-tiered-drain-scheduler/69-03-SUMMARY.md`.
- All four modified source/test files present on disk.
- Both task commits present: `b3d8f25` (Task 1), `f657ae9` (Task 2).
