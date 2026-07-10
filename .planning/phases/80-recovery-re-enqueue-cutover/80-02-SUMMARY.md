---
phase: 80-recovery-re-enqueue-cutover
plan: 02
subsystem: database
tags: [sqlalchemy, cloud_job, stage_status, predicate-builder, drain, recovery]

# Dependency graph
requires:
  - phase: 78-single-source-predicate-layer
    provides: "LOCKED inflight_clause / domain_completed_clause / done_clause builders + DERIV-04 equivalence lock"
  - phase: 83-cloud-routing-sidecar
    provides: "get_awaiting_cloud_count + get_cloud_staging_candidates cut over onto the cloud_job sidecar + derived in_flight layer (the two inline call sites this plan extracts)"
provides:
  - "awaiting_candidate_clause() — the single-source awaiting-cloud candidate predicate in services/stage_status.py"
  - "D-11 ~inflight_clause prohibition rationale recorded in domain_completed_clause's docstring"
  - "both pipeline.py drain/count call sites repointed at the shared builder (third consumer = Plan 80-04's _get_awaiting_cloud_ids)"
affects: [80-04-recovery-reenqueue, 80-05-equivalence-scope, cloud-drain, recovery]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Out-of-ladder clause builder (no stage arg, like dedup_resolved_clause) kept OUT of the Stage-dispatch equivalence ladder so DERIV-04 does not pick it up"
    - "Single-source predicate: card + drain + recovery derive one .where() conjunct from ONE named builder so they can never disagree"

key-files:
  created:
    - tests/shared/test_awaiting_candidate_clause.py
  modified:
    - src/phaze/services/stage_status.py
    - src/phaze/services/pipeline.py

key-decisions:
  - "awaiting_candidate_clause() composes the LOCKED inflight_clause + domain_completed_clause verbatim (no re-spelled predicate) so DERIV-04 equivalence still holds"
  - "Builder takes no stage argument and is out-of-ladder (dedup_resolved_clause placement template) so the equivalence test that raises on unknown stages ignores it (D-09/D-13)"
  - "Removing the two inline call sites left Stage / inflight_clause / domain_completed_clause unused in pipeline.py — dropped those three imports (F401)"

patterns-established:
  - "Single-source awaiting-cloud predicate: get_awaiting_cloud_count, get_cloud_staging_candidates, and (Plan 80-04) _get_awaiting_cloud_ids all consume awaiting_candidate_clause()"
  - "D-11 trap documented in code: ~inflight_clause MUST NEVER be conjoined into domain_completed_clause (would silently disable the secondary over-enqueue net)"

requirements-completed: [READ-03]

# Metrics
duration: 20min
completed: 2026-07-10
---

# Phase 80 Plan 02: Awaiting-Cloud Candidate Clause Extraction Summary

**Extracted the byte-identical awaiting-cloud candidate `.where(...)` conjunct into ONE named `awaiting_candidate_clause()` builder composing the LOCKED `inflight_clause` + `domain_completed_clause` verbatim, repointed both pipeline drain/count call sites at it, and landed the D-11 `~inflight_clause`-prohibition docstring trap.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-10T16:07:39Z
- **Completed:** 2026-07-10T16:28:09Z
- **Tasks:** 2
- **Files modified:** 3 (1 created, 2 modified)

## Accomplishments
- `awaiting_candidate_clause()` in `services/stage_status.py`: `and_(CloudJob.status == AWAITING, ~inflight_clause(ANALYZE), ~domain_completed_clause(ANALYZE))` — same three conjuncts, same order, as the two inline spellings it replaces (D-08/D-09). Composed only from the LOCKED builders, so the DERIV-04 equivalence guarantee holds.
- Both `pipeline.py` consumers (`get_awaiting_cloud_count` and `get_cloud_staging_candidates`) now call the single builder; neither retains the inline three-conjunct spelling. All chaining preserved unchanged: both inner `FileRecord` joins, the `_safe_count(...)` wrapper, `with_for_update(of=CloudJob, skip_locked=True)`, and `order_by(FileRecord.created_at.asc())`.
- D-11 trap recorded: `domain_completed_clause`'s docstring now states `~inflight_clause(stage)` MUST NEVER be added (every recovery candidate is a ledger row by construction, so the disjunct would make `domain_completed` False for every candidate — silently disabling the secondary over-enqueue net, the 44.5K incident class).
- CloudJob/CloudJobStatus imported into `stage_status.py` with no import cycle (`cloud_job` imports only SQLAlchemy + `models.base`).

## Task Commits

Each task was committed atomically:

1. **Task 1 (RED): failing contract test for awaiting_candidate_clause** - `ff2f744d` (test)
2. **Task 1 (GREEN): add awaiting_candidate_clause + D-11 note** - `9409683b` (feat)
3. **Task 2: repoint pipeline.py's two inline call sites at the builder** - `7c779714` (refactor)

_TDD Task 1 produced test → feat commits (no refactor needed)._

## Files Created/Modified
- `tests/shared/test_awaiting_candidate_clause.py` - DB-free contract test: byte-identical composition of the LOCKED builders (compiled-SQL equality mutation guard), 3-conjunct AND order, AWAITING literal presence, D-11 docstring prohibition.
- `src/phaze/services/stage_status.py` - Added `CloudJob`/`CloudJobStatus` import, `awaiting_candidate_clause()` builder (out-of-ladder), and the D-11 `~inflight_clause` prohibition note on `domain_completed_clause`.
- `src/phaze/services/pipeline.py` - Repointed both call sites at `awaiting_candidate_clause()`; dropped the now-unused `Stage` / `inflight_clause` / `domain_completed_clause` imports.

## Decisions Made
- Dropped `Stage`, `inflight_clause`, and `domain_completed_clause` imports from `pipeline.py` after the last inline uses were replaced (they only survive in docstrings now). Kept as part of the refactor to keep ruff green (no F401).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Removed three now-unused imports from pipeline.py**
- **Found during:** Task 2 (repoint call sites)
- **Issue:** Replacing the two inline three-conjunct `.where(...)` spellings with `awaiting_candidate_clause()` removed the last code uses of `Stage`, `inflight_clause`, and `domain_completed_clause` in `pipeline.py`, so ruff F401 flagged all three as unused imports (the plan only mentioned adding `awaiting_candidate_clause` to the import).
- **Fix:** Removed the three unused imports; kept `awaiting_candidate_clause`.
- **Files modified:** src/phaze/services/pipeline.py
- **Verification:** `uv run ruff check src/phaze/services/pipeline.py` passes (no F401); `uv run mypy` clean.
- **Committed in:** 7c779714 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Necessary consequence of the extraction — the whole point was to move these builders behind the single-source clause. No scope creep.

## Issues Encountered
- The local test buckets surfaced DB-infra flakiness, NOT regressions. First `just test-bucket analyze` run reported 181 "errors" because the ephemeral test Postgres was not running (connection refused on 5432). After `just test-db` (Postgres on 5433 + Redis on 6380) with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` exported, `analyze` = **528 passed, 0 errors**.
- Batched multi-file integration runs threw scattered `duplicate key value violates unique constraint "pg_type_typname_nsp_index"` (a `CREATE TYPE` DDL race between self-contained-schema integration files co-scheduled in one process) and colima VM-pressure connection errors. Every errored test PASSES in isolation. The two behavior-critical files pass fully alone: `test_stage_status_equivalence.py` (36/36, DERIV-04 lock) and `test_drain_double_dispatch.py` (3/3, incl. SC#3 case B rolled-back-tick-not-repicked and case C terminally-failed-never-dispatched — the exact tests a divergent conjunct or dropped `~domain_completed_clause` would flip RED). New `test_awaiting_candidate_clause.py` = 5/5.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- `awaiting_candidate_clause()` is ready for Plan 80-04's `_get_awaiting_cloud_ids` to become the third consumer (D-08 single-source guarantee now extends to recovery).
- D-11 prohibition is documented in code; Plan 80-04's recovery regression and Plan 80-05's equivalence SCOPE comment lock it further.

## Self-Check: PASSED

All 4 files present (1 created, 2 modified, 1 summary); all 3 task commits (`ff2f744d`, `9409683b`, `7c779714`) present in git history.

---
*Phase: 80-recovery-re-enqueue-cutover*
*Completed: 2026-07-10*
