---
phase: 81-per-stage-failure-persistence-retry-paths
plan: 04
subsystem: testing
tags: [fingerprint, regression-test, eligibility, stage-status, agent-api, docstrings]

# Dependency graph
requires:
  - phase: 78-derivation-layer
    provides: "enums/stage.py eligible()/resolve_status() + Stage/Status enums (ELIG-04 failed-fingerprint-stays-eligible)"
  - phase: 45-scheduling-ledger
    provides: "fingerprint_file:<file_id> ledger + clear_ledger_entry (report_fingerprint_failed clears it)"
provides:
  - "Regression lock: report_fingerprint_failed persists NO fingerprint_results row (D-18)"
  - "Regression lock: no synthetic engine='_task' sentinel row is ever written (T-81-04-01)"
  - "Regression lock: a status='failed' per-engine row keeps eligible(FINGERPRINT) True (ELIG-04)"
  - "Docstrings documenting the no-row asymmetry + the durable put_fingerprint failed-row marker"
affects: [82-counts-pending-cutover, 84-dedup-fingerprint-progress, 87-operator-ui-failure-retry]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Characterization/regression test over pre-existing behavior (no new writer — D-18 'reused, not re-invented')"
    - "End-to-end eligibility assertion: put_fingerprint DB row -> resolve_status -> eligible()"

key-files:
  created:
    - "tests/fingerprint/routers/test_agent_fingerprint_failure.py"
  modified:
    - "src/phaze/routers/agent_fingerprint.py"

key-decisions:
  - "FAIL-04 is a test+docs deliverable, not a new writer: the durable fingerprint failure marker is the existing per-engine fingerprint_results.status='failed' row (D-18)"
  - "report_fingerprint_failed writes no row by design — a synthetic engine='_task' sentinel would poison the two aliased per-engine joins at pipeline.py:939-940 and _trackid_engine_badge at :864"
  - "A FAILED fingerprint stays auto-retryable (FAILURE_IS_TERMINAL[fingerprint] = False / ELIG-04), unlike terminal FAILED analyze"

patterns-established:
  - "Regression lock for asymmetric no-write endpoints: assert row-count invariance + sentinel-absence"

requirements-completed: [FAIL-04]

# Metrics
duration: ~20min
completed: 2026-07-09
---

# Phase 81 Plan 04: Fingerprint Failure No-Row Asymmetry Summary

**Regression tests + docstrings proving `report_fingerprint_failed` persists no `fingerprint_results` row (only clears the ledger), the durable failure marker is the existing per-engine `status='failed'` row, and a failed fingerprint stays auto-retryable — with no synthetic `engine='_task'` sentinel ever written to poison the aliased per-engine joins.**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-07-09
- **Tasks:** 2
- **Files modified:** 2 (1 created, 1 modified)

## Accomplishments
- Added `tests/fingerprint/routers/test_agent_fingerprint_failure.py` (5 tests) locking the FAIL-04 / D-18 asymmetry: (a) `fingerprint_results` row count identical before/after `report_fingerprint_failed` + ledger cleared; (b) a `status='failed'` per-engine row keeps `eligible(FINGERPRINT)` True end-to-end (put_fingerprint → `resolve_status` → `eligible`) plus a DB-free ELIG-04 predicate lock; (c) no `engine='_task'` synthetic sentinel row exists after the ack, so the two aliased per-engine joins (`pipeline.py:939-940`) stay unpoisoned (T-81-04-01).
- Documented the asymmetry in `agent_fingerprint.py` docstrings: `report_fingerprint_failed` explains it persists no row by design and why (join poisoning); `put_fingerprint` notes its `status='failed'` row is the auto-retryable durable marker (`FAILURE_IS_TERMINAL[fingerprint] = False`).
- `just test-bucket fingerprint` green in isolation (83 passed).

## Task Commits

Each task was committed atomically:

1. **Task 1: Add fingerprint no-row + eligible + unpoisoned-join regression tests** - `9f8f441e` (test)
2. **Task 2: Document the fingerprint failure asymmetry in docstrings** - `443cc27b` (docs)

_Note: Task 1 is `tdd="true"` but the deliverable is characterization/regression tests over behavior that already ships from prior phases (D-18 "reused, not re-invented"). There is no new writer to implement, so it is a single `test(...)` commit — the tests pass against existing code by design._

## Files Created/Modified
- `tests/fingerprint/routers/test_agent_fingerprint_failure.py` - 5 regression tests for the no-row asymmetry, per-engine eligibility, and unpoisoned joins (authed-agent smoke-app pattern)
- `src/phaze/routers/agent_fingerprint.py` - docstring-only additions (20 insertions, no logic/import change) documenting the asymmetry

## Decisions Made
- None beyond the plan — executed exactly as specified. The plan's acceptance snippet `eligible(Stage.FINGERPRINT, {...})` was mapped to the real signature `eligible(status_map, stage)` (positional order is `status_map` first); asserted as `eligible({Stage.FINGERPRINT: Status.FAILED}, Stage.FINGERPRINT) is True`.

## Deviations from Plan

None - plan executed exactly as written.

Note: one in-development test iteration was corrected before its first commit (test (a) initially seeded the ledger before `put_fingerprint`, but a successful engine PUT clears the `fingerprint_file` ledger row, so the pre-POST `_ledger_present` assertion failed). Reordered to seed the ledger AFTER `put_fingerprint` so `report_fingerprint_failed` is unambiguously the clearer. This was fixed prior to the Task 1 commit — not a post-commit deviation.

## Issues Encountered
- The `just test-bucket fingerprint` recipe does not export `TEST_DATABASE_URL` (CI/the `test` recipe do). Ran with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL` pointed at the ephemeral test DB (`localhost:5433`) started via `just test-db`. Not a code issue.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- FAIL-04 locked. The fingerprint failure marker is now regression-guarded as the existing per-engine row, so the Phase 82 counts/pending cutover and Phase 84 fingerprint-progress readers can rely on `eligible(FINGERPRINT)` staying True for failed engines without a hidden sentinel row.
- No blockers.

## Self-Check: PASSED

- FOUND: tests/fingerprint/routers/test_agent_fingerprint_failure.py
- FOUND: src/phaze/routers/agent_fingerprint.py
- FOUND: .planning/phases/81-per-stage-failure-persistence-retry-paths/81-04-SUMMARY.md
- FOUND commit 9f8f441e (Task 1 test)
- FOUND commit 443cc27b (Task 2 docs)
- FOUND commit 6b4e023b (SUMMARY)

---
*Phase: 81-per-stage-failure-persistence-retry-paths*
*Completed: 2026-07-09*
