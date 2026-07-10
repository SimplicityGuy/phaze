---
phase: 80-recovery-re-enqueue-cutover
plan: 04
subsystem: infra
tags: [recovery, scheduling-ledger, stage-status, cloud-job, saq, postgres, sqlalchemy]

# Dependency graph
requires:
  - phase: 80-01
    provides: "migration 036 (analysis.analysis_completed_at backfill) so done(analyze) reads a complete corpus"
  - phase: 80-02
    provides: "awaiting_candidate_clause() extracted into services/stage_status.py"
  - phase: 78
    provides: "done_clause / failed_clause / inflight_clause / domain_completed_clause predicate layer"
  - phase: 81
    provides: "domain_completed twins + FAILURE_IS_TERMINAL + the metadata failed_at writer (retry leaves it set)"
  - phase: 83
    provides: "hold_awaiting_cloud (the go-forward cloud_job status='awaiting' writer)"
provides:
  - "reenqueue.py recovery derives done/in-flight from the predicate layer + cloud_job sidecar with ZERO FileRecord.state reads"
  - "ledger-scoped = ANY(array) done-set derivation (O(|ledger|), asyncpg-safe)"
  - "the D-10 metadata in_flight-and-failed cell resolved at the recovery call site via SchedulingLedger.enqueued_at"
affects: [82-pending-counts-cutover, 90-filestate-write-retirement]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "= ANY(:ids) single Postgres array bind for ledger-scoped id filters (first use in the codebase)"
    - "call-site timestamp gate (ledger.enqueued_at vs marker.failed_at) to disambiguate an in_flight-and-failed cell"

key-files:
  created: []
  modified:
    - "src/phaze/tasks/reenqueue.py"
    - "tests/analyze/tasks/test_recovery.py"
    - "tests/analyze/core/test_reenqueue.py"
    - "tests/shared/core/test_analysis_progress_spike.py"

key-decisions:
  - "Metadata done-set derived via domain_completed_clause(METADATA) (done OR failed), refined by the D-10 enqueued_at gate at the call site — keeps the D-11 ~inflight trap catchable (a bare done_clause would make the D-11 lock toothless)"
  - "_get_awaiting_cloud_ids consumes awaiting_candidate_clause() (D-08); in the sidecar model its ~inflight conjunct means the held-routing branch is now provably empty (genuinely-parked long files carry NO process_file ledger row — the hold path parks without enqueuing), so kind-agnostic process_file routing stays CLOUDROUTE-02-safe"
  - "push_done via cloud_job.status='succeeded' OR domain_completed(analyze) (D-07) — no backend-kind resolution since a push_file ledger row implies compute"

patterns-established:
  - "Ledger-scoped derived-done: bind the ledger's fids as one uuid[] array, one targeted predicate probe per stage so each Phase-77 partial index drives its own scan"
  - "Mutation-named regression tests: each new guard names the source mutation that turns it RED (SC-2/SC-3/D-10/D-11 all hand-verified RED)"

requirements-completed: [READ-03]

# Metrics
duration: 95min
completed: 2026-07-10
---

# Phase 80 Plan 04: Recovery / Re-enqueue Cutover Summary

**`recover_orphaned_work` now derives every done/in-flight set from the Phase-78/81 stage_status predicate layer + the cloud_job sidecar — ledger-scoped via a single `= ANY(array)` bind — with ZERO `FileRecord.state` reads, closing the double-negation before Phase 82 and resolving the D-10 metadata cell at the call site.**

## Performance

- **Duration:** ~95 min (includes recovering an accidental `git checkout` revert of the implementation file mid-run)
- **Completed:** 2026-07-10
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Cut `reenqueue.py`'s four done-set helpers + `is_domain_completed` over to `done_clause` / `domain_completed_clause` / `awaiting_candidate_clause` — no `FileRecord.state`, no `FileState` import, pending-fn imports dropped (D-01/D-05).
- Every done-set query is ledger-scoped via a single `FileRecord.id == func.any(bindparam(..., ARRAY(PGUUID)))` bind (D-06) — the codebase's first `= ANY(array)` idiom, sidestepping the asyncpg 32767-param cap that the ~44.5K-row ledger would hit.
- `push_done = cloud_job.status='succeeded' OR domain_completed(analyze)` (D-07); `_get_awaiting_cloud_ids` consumes the shared `awaiting_candidate_clause()` (D-08).
- The metadata `in_flight ∧ failed` cell (D-10) is resolved at the call site via `SchedulingLedger.enqueued_at`; deriving the metadata set through `domain_completed_clause(METADATA)` keeps the D-11 `~inflight` trap catchable.
- Added SC-2 / SC-3 / D-10 Cell A / D-10 Cell B / D-11 regressions, each mutation-named and hand-verified RED under its named mutation.

## Task Commits

Each task was committed atomically:

1. **Task 1: Cut the four done-set helpers + is_domain_completed to the predicate layer** — `7c145ad7` (feat)
2. **Task 2: SC-2 / SC-3 / D-10 both-cells / D-11 regression cases** — `05f94b79` (test)

_(This SUMMARY + its metadata commit follow.)_

## Files Created/Modified
- `src/phaze/tasks/reenqueue.py` — state-free, ledger-scoped done-set derivation via the LOCKED predicate builders; `_DoneSets` dataclass; `_ledger_fids` / `_fids_scope` array-bind helpers; D-10 call-site metadata gate; three module-docstring reframes updated in place.
- `tests/analyze/tasks/test_recovery.py` — reseeded the FileState-based done tests onto the `analysis`/`metadata`/`fingerprint` output rows + cloud_job sidecar; reframed the Phase-49 held-routing tests to the D-08 `awaiting_candidate_clause` model; added the SC-2/SC-3/D-10/D-11 mutation-named regressions.
- `tests/analyze/core/test_reenqueue.py` — Phase-50 push tests reseeded onto the SUCCEEDED cloud_job sidecar + `analysis` rows; `_build_done_sets` fids signature; dataclass field access.
- `tests/shared/core/test_analysis_progress_spike.py` — `_analyze_is_done` passes fids to the now-scoped `_select_done_analyze_ids`.

## Decisions Made
- **Metadata via `domain_completed_clause` (not `done_clause`) + a call-site gate.** The plan's D-10 formula is `done OR (failed AND enqueued_at <= failed_at)`. Deriving the metadata done-set through `domain_completed_clause(METADATA)` (done OR failed) and refining the failed subset at the call site keeps the D-11 lock meaningful — a `~inflight_clause` wrongly added to `domain_completed_clause` turns the D-11/Cell-B regressions RED. A `done_clause`-only split would have made D-11 toothless (mutation confirmed).
- **The held-routing branch is now provably empty (documented, not deleted).** `awaiting_candidate_clause()`'s `~inflight_clause(ANALYZE)` excludes any file carrying a `process_file` ledger row; genuinely-parked long files carry NO such row (the Phase-83 hold path parks without enqueuing), and compute-dispatched files enqueue `push_file` (not `process_file`) + a SUBMITTED cloud_job (SCHED-05-excluded). So a `process_file` orphan is always a short/local analysis and kind-agnostic routing stays CLOUDROUTE-02-safe. The branch structure is preserved but is a no-op in the sidecar model.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed `_select_done_analyze_ids` callers outside the plan's named test file**
- **Found during:** Task 1 (signature change added a required `fids` argument)
- **Issue:** `tests/shared/core/test_analysis_progress_spike.py` and `tests/analyze/core/test_reenqueue.py` call the changed helpers, breaking the `shared` + `analyze` buckets (the plan listed only `test_recovery.py`).
- **Fix:** Passed `fids` to `_analyze_is_done`; reseeded the Phase-50 push tests onto the SUCCEEDED cloud_job sidecar + `analysis` rows and the renamed `_DoneSets` fields.
- **Files modified:** `tests/shared/core/test_analysis_progress_spike.py`, `tests/analyze/core/test_reenqueue.py`
- **Verification:** `just test-bucket shared` (1015 passed) + `just test-bucket analyze` (537 passed)
- **Committed in:** `7c145ad7` (spike test, Task 1 shared-bucket verify) and `05f94b79` (push tests, Task 2)

**2. [Rule 3 - Blocking] Reframed the Phase-49 CR-01 held-routing tests to the sidecar model**
- **Found during:** Task 2
- **Issue:** The existing held-routing tests encoded the retired backfill model (an `AWAITING_CLOUD` FileRecord + a `process_file` ledger row = a held long file needing compute-only routing). Under the D-08 `awaiting_candidate_clause` cutover that combination is reinterpreted as mid-local-analysis (excluded by `~inflight`), so the old compute-only expectations no longer hold.
- **Fix:** Replaced them with unit tests of `_get_awaiting_cloud_ids` (the D-08 `~inflight` exclusion + genuinely-parked inclusion) and a routing test proving a mid-local-analysis `process_file` orphan recovers kind-agnostically. Corrected the now-inaccurate SCHED-05 "held path" comments.
- **Files modified:** `tests/analyze/tasks/test_recovery.py`
- **Verification:** `just test-bucket analyze` (537 passed)
- **Committed in:** `05f94b79`

---

**Total deviations:** 2 auto-fixed (both Rule 3 - blocking, caused by the source cutover's API/semantic change rippling into test callers the plan did not enumerate).
**Impact on plan:** Necessary to keep the `shared` + `analyze` buckets green in isolation (the acceptance gate). No behavior change beyond the intended cutover; the held-routing reframe documents a model consequence rather than adding scope.

## Issues Encountered
- **Self-inflicted revert (recovered).** A mutation-testing cleanup step ran `git checkout -- src/phaze/tasks/reenqueue.py` to restore a temporary mutation, which reverted the (still-uncommitted) Task 1 implementation to HEAD. Detected immediately via a marker grep and fully re-applied. All four named mutations (SC-3, D-10 Cell A/B, D-11) were confirmed RED before the revert, and the restored file passes ruff + mypy + all 61 recovery/reenqueue/spike tests. Lesson: never `git checkout` a file with uncommitted work during mutation testing — use a scratch backup.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Recovery is fully state-free and ledger-scoped; Phase 82 can redefine `pending` as `NOT done ∧ NOT in_flight` without silently changing recovery's done semantics (the double-negation is closed).
- **Note for Phase 82:** the same read-before-write inversion (D-02/D-03 class) may affect READ-01/READ-02 (pending sets + `get_pipeline_stats` counts) — flagged in 80-CONTEXT, not acted on here.
- **Note for Phase 90:** `reconcile_cloud_jobs.py`'s residual `FileRecord.state` write (D-04) is a SEPARATE plan in this phase, not this one; PROV-01 (single-active-compute selection at `reenqueue.py:382`) remains deferred and untouched.

## Self-Check: PASSED

- `src/phaze/tasks/reenqueue.py` — FOUND
- `.planning/phases/80-recovery-re-enqueue-cutover/80-04-SUMMARY.md` — FOUND
- Commit `7c145ad7` (Task 1) — FOUND
- Commit `05f94b79` (Task 2) — FOUND
- `just test-bucket analyze` (537 passed) + `just test-bucket shared` (1015 passed) — green in isolation

---
*Phase: 80-recovery-re-enqueue-cutover*
*Completed: 2026-07-10*
