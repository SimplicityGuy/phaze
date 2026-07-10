---
phase: 80-recovery-re-enqueue-cutover
plan: 03
subsystem: infra
tags: [reconcile, cloud_job, kueue, shadow-compare, hold_awaiting_cloud, sqlalchemy, spill-cas]

# Dependency graph
requires:
  - phase: 83-single-awaiting-writer
    provides: "hold_awaiting_cloud spill-mode CAS (services/backends.py:86) — the single go-forward 'awaiting' writer"
  - phase: 77-cloud-job-awaiting
    provides: "CloudJobStatus.AWAITING member + ix_cloud_job_awaiting"
provides:
  - "reconcile_cloud_jobs.py fully state-free: the at-cap spill writes only the cloud_job sidecar (status='awaiting'), never FileRecord.state"
  - "fixes a HARD shadow-gate violation live on main (state=AWAITING_CLOUD + cloud_job.status=FAILED)"
  - "reconcile is now the FOURTH caller of hold_awaiting_cloud spill mode (alongside agent_s3, agent_push)"
affects: [80-05-source-guard, 90-drop-filestate-column, shadow-compare]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Spill-mode CAS via the single awaiting writer: hold_awaiting_cloud(attempts=cap, expect_status=(SUBMITTED,RUNNING), clear_cloud_phase=True) owns the status write; caller keeps inadmissible/staging_bucket inline"
    - "No pre-mutation of cloud_job.status before the CAS (autoflush-race guard, RESEARCH Landmine 3)"

key-files:
  created: []
  modified:
    - "src/phaze/tasks/reconcile_cloud_jobs.py — at-cap spill block rewritten; FileState import dropped; update->select"
    - "tests/analyze/tasks/test_reconcile_cloud_jobs.py — new parametrized spill regression + existing at-cap assertions updated"

key-decisions:
  - "Kept the FileRecord import (dropped only FileState): CloudJob has no .file relationship, so the spill-mode helper's file arg is loaded via select(FileRecord) — diverges from the plan's 'drop both imports' instruction"
  - "Seed FileRecord at PUSHED (the realistic in-flight state) so the at-cap tests prove reconcile writes NO state; reconcile leaving it unchanged is the D-04 assertion"

patterns-established:
  - "Reconcile at-cap terminal re-stamps the sidecar to 'awaiting' (out of IN_FLIGHT), never FAILED; the file stays PUSHED and select_backend routes it to local via attempts>=cap"

requirements-completed: [READ-03]

# Metrics
duration: ~20min
completed: 2026-07-10
---

# Phase 80 Plan 03: Reconcile Re-enqueue Cutover Summary

**Retired the last `FileRecord.state` write in `reconcile_cloud_jobs.py` — the at-cap spill now routes through `hold_awaiting_cloud` spill-mode CAS (status='awaiting', no state write), fixing a HARD shadow-gate violation live on main.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-10 (worktree agent)
- **Completed:** 2026-07-10
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Rewrote the at-cap terminal block in `_handle_no_callback_terminal`: removed the `cloud_job.status = FAILED` pre-mutation (autoflush-race guard, Landmine 3) and the `update(FileRecord).values(state=AWAITING_CLOUD)` write (D-04), swapping in the spill-mode CAS `hold_awaiting_cloud(session, file, attempts=cap, expect_status=(SUBMITTED,RUNNING), clear_cloud_phase=True)`.
- Preserved MKUE-04 clean-before-flip byte-for-byte: `delete_staged_object` under the still-held `pg_advisory_xact_lock(5_000_504)` BEFORE the commit; `cloud_job.attempts` NOT incremented; `delete_job` POST-commit. Verified by the existing advisory-lock/ordering tests.
- Added `test_at_cap_spill_restamps_cloud_job_awaiting_not_failed` (parametrized over SUBMITTED/RUNNING), asserting from an INDEPENDENT session: `status=='awaiting'` (not FAILED), `FileRecord.state` unchanged (PUSHED), attempts not incremented, `inadmissible`/`staging_bucket`/`cloud_phase` cleared, and `s3_delete < commit < delete_job` ordering.
- Mutation-verified the regression has teeth: reintroducing `status=FAILED` (VALIDATION SC-1 mutation b) and moving `delete_staged_object` after `session.commit()` (SC-1 mutation c) each turn it RED, then restored.

## Task Commits

1. **Task 1: Rewrite the at-cap spill block via hold_awaiting_cloud** - `cfadd351` (feat)
2. **Task 2: Spill regression test (status='awaiting', ordering, attempts)** - `aa5066b0` (test)

_Task 1 is `tdd="true"` but its plan-defined verify is ruff+mypy only (the behavioral test lives in Task 2); committed as feat → test to match the plan's task split._

## Files Created/Modified
- `src/phaze/tasks/reconcile_cloud_jobs.py` - At-cap spill re-stamps the cloud_job sidecar to 'awaiting' via the single spill writer; zero FileRecord.state coupling; docstrings/comments reframed; `FileState` import dropped, `update`→`select`.
- `tests/analyze/tasks/test_reconcile_cloud_jobs.py` - New parametrized spill regression; existing at-cap assertions updated (`FAILED`→`AWAITING`, seed/assert `PUSHED` instead of `AWAITING_CLOUD`); `_make_file` default state → `PUSHED`.

## Decisions Made
- **Kept `FileRecord` imported (dropped only `FileState`).** The plan's action and acceptance said to drop `FileRecord, FileState`, but `CloudJob` has no `.file` relationship, and the plan's own action text sanctions `SELECT FileRecord WHERE id == file_id` to load the helper's `file` arg. That select requires the import (ruff would NOT flag F401 since it's used). The Plan 80-05 source guard flags only `.attr == "state"` reads and `FileState.<member>` occurrences — bare `FileRecord.id` in a `select()` is not flagged, so this is safe against the guard.
- **Seed FileRecord at `PUSHED`** (the realistic state when a cloud_job is in-flight SUBMITTED/RUNNING) so the at-cap tests prove reconcile leaves state untouched.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Kept `FileRecord` import; dropped only `FileState`; swapped `update`→`select`**
- **Found during:** Task 1
- **Issue:** The plan instructed dropping both `FileRecord` and `FileState` from the `phaze.models.file` import (acceptance: "FileRecord, FileState import removed"). But `CloudJob` defines no `.file` relationship, so the spill-mode helper's `file: FileRecord` argument must be loaded via `select(FileRecord).where(FileRecord.id == file_id)` — which requires `FileRecord`. Removing it would break the load the plan's own action text prescribes. Separately, `sqlalchemy.update` became unused (the `update(FileRecord)` write was removed) while `select` was newly needed.
- **Fix:** Changed the import to `from phaze.models.file import FileRecord` (dropped `FileState` only) and `from sqlalchemy import select` (was `update`). `FileState.<member>` and `FileRecord.state` no longer appear anywhere in the file, satisfying the D-04 state-free acceptance and the Plan 80-05 guard's actual scan surface.
- **Files modified:** src/phaze/tasks/reconcile_cloud_jobs.py
- **Verification:** `uv run ruff check` + `uv run mypy` pass; no `FileState`/`FileRecord.state`/`update(FileRecord)` occurrences remain.
- **Committed in:** cfadd351 (Task 1 commit)

**2. [Rule 1 - Correctness] Updated existing at-cap tests to the new awaiting/no-state-write behavior**
- **Found during:** Task 2
- **Issue:** Several existing at-cap tests asserted the OLD behavior (`cloud_job.status == FAILED`, `file.state == AWAITING_CLOUD`), which the D-04/D-12 change intentionally supersedes. Left unchanged they would (and did) go RED under `just test-bucket analyze`.
- **Fix:** Updated the six at-cap tests' assertions (`FAILED`→`AWAITING`, `AWAITING_CLOUD`→`PUSHED`) and changed `_make_file`'s default state to `PUSHED` so the "no state write" property is meaningfully tested (distinct seed state that must remain unchanged).
- **Files modified:** tests/analyze/tasks/test_reconcile_cloud_jobs.py
- **Verification:** `test_reconcile_cloud_jobs.py` 30 passed deterministically; targeted at-cap subset green 3/3.
- **Committed in:** aa5066b0 (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 correctness)
**Impact on plan:** Both are necessary consequences of the behavior change and the `CloudJob`-has-no-`.file`-relationship reality. No scope creep; the D-04/D-12 goals are fully met.

## Issues Encountered
- **Local colima full-suite flake (infra, NOT a regression).** `just test-bucket analyze` reported `469 passed, 61 errors`; the errors are fixture-setup `IntegrityError`s on the shared `agents` table under VM pressure, spread across files I never touched (`test_scheduling_ledger`, `test_compute_binding_golden`, `test_cloud_staging`, etc.) and non-deterministic run-to-run (a different subset errors each run). Confirmed infra-not-regression per the documented flake: the reconcile file passes 30/30 deterministically (`-p no:randomly`), each errored file (`test_recovery`, `test_cloud_staging`, `test_scheduling_ledger`) passes cleanly in isolation, and the targeted at-cap subset is green 3/3. Do NOT set `PHAZE_QUEUE_URL=redis`.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- `reconcile_cloud_jobs.py` is fully state-free — ready for Plan 80-05's AST source guard (which targets `reenqueue.py` + `reconcile_cloud_jobs.py` at clean absence of `FileRecord.state`/`FileState.<member>`). Note for 80-05: `FileRecord` is still imported (used only via `FileRecord.id` in a `select`), which the `.attr == "state"` guard does not flag.
- The `036` migration + shadow-compare gate (SC-4) are owned by other plans in this phase; this plan's contribution (retiring the reconcile state write) is the second half of the `awaiting_cloud`-invariant fix.

## Self-Check: PASSED

- `80-03-SUMMARY.md` exists.
- Commits `cfadd351` (Task 1) and `aa5066b0` (Task 2) exist.
- `reconcile_cloud_jobs.py` is state-free at the code level: no `FileState.<member>`, no `FileRecord.state`, no `update(FileRecord)` in code. (A `grep` matches 5 prose occurrences of the string "FileRecord.state" in docstrings/comments that document the invariant; Plan 80-05's guard is AST-based — `ast.Attribute` with `.attr == "state"` — so prose is not flagged. Confirmation that no code reference survives: ruff + mypy pass after the `FileState` import was dropped, which would fail F821 on any code use.)
- `hold_awaiting_cloud` referenced in the source (4 occurrences: the deferred import + call + 2 doc mentions).

---
*Phase: 80-recovery-re-enqueue-cutover*
*Completed: 2026-07-10*
