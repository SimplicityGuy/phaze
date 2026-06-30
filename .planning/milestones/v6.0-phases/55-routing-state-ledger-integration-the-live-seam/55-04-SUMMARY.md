---
phase: 55-routing-state-ledger-integration-the-live-seam
plan: 04
subsystem: api
tags: [backfill, scheduling-ledger, sqlalchemy, exists, cloud-routing, k8s, ast-guard]

# Dependency graph
requires:
  - phase: 55-01
    provides: "cloud_target settings gate + routers/pipeline.py rekeys (settings.cloud_target == 'local' backfill gate)"
  - phase: 50
    provides: "trigger_backfill_cloud endpoint + held-file AWAITING_CLOUD reshape + insert_ledger_if_absent seed"
  - phase: 45
    provides: "report_analysis_failed clears process_file:<id> ledger row (terminal-failure poison-case)"
provides:
  - "Ledger-scoped backfill candidate query (EXISTS scheduling_ledger keyed process_file:<id>)"
  - "cloud_target fork in trigger_backfill_cloud: k8s skips the held-file ledger seed (L3)"
  - "AST/static guard extension asserting routed k8s enqueue + no-whole-backlog backfill property (KROUTE-04)"
affects: [55-03, kubernetes-burst, recover_orphaned_work, cloud_staging]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "EXISTS-ledger scoping of a re-drive candidate query (previously-scheduled work only) mirroring the v5.0 recover-over-enqueue fix"
    - "cloud_target fork at the ledger-seed boundary (k8s in-flight registry = cloud_job row, NOT scheduling_ledger)"
    - "Static AST source-guard over a query-builder function to lock a bounded-filter invariant"

key-files:
  created:
    - .planning/phases/55-routing-state-ledger-integration-the-live-seam/deferred-items.md
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - tests/test_routers/test_pipeline.py
    - tests/test_no_default_queue_producers.py

key-decisions:
  - "EXISTS predicate distinguishes timed-out (ledger row persists) from cleanly-reported-failed (row cleared by report_analysis_failed) — backfill re-drives only the recoverable timed-out set"
  - "k8s backfill skips insert_ledger_if_absent entirely; a process_file ledger row would let recover_orphaned_work replay the held file onto a LOCAL agent queue (CLOUDROUTE-02)"
  - "Branched the existing /pipeline/backfill-cloud endpoint on cloud_target (one surface) rather than a distinct endpoint (Open Q2)"
  - "L3 fork is asserted at the insert_ledger_if_absent call boundary via a spy — both forks converge on the prior candidacy row, so the seed call is the only observable difference"

patterns-established:
  - "Test helper _persist_failed_with_duration seeds a process_file ledger row by default (with_ledger param) to model previously-scheduled timed-out work"

requirements-completed: [KROUTE-04, KROUTE-05]

# Metrics
duration: 45min
completed: 2026-06-28
---

# Phase 55 Plan 04: Ledger-scoped K8s backfill + AST guard Summary

**Backfill now re-drives ONLY previously-scheduled timed-out long files (EXISTS scheduling_ledger predicate), the k8s fork holds them in AWAITING_CLOUD with NO process_file ledger seed (L3), and a static AST guard locks the no-whole-backlog over-enqueue invariant (KROUTE-04).**

## Performance

- **Duration:** ~45 min
- **Completed:** 2026-06-28
- **Tasks:** 3
- **Files modified:** 4 (+1 created)

## Accomplishments
- **L4 — ledger-scoped candidate query:** `_backfill_candidates_stmt` gained an `EXISTS (scheduling_ledger WHERE key = 'process_file:' || cast(file.id))` predicate, so never-scheduled (or cleanly report-failed, row-cleared) failures are excluded — closing the v4.0.6 / v5.0 whole-backlog over-enqueue class. ORM/bound params only (no f-string SQL, T-55-BF-04).
- **L3 — k8s ledger-seed skip:** `trigger_backfill_cloud` forks on `settings.cloud_target`; the k8s branch resets candidates to DISCOVERED and lets the duration router hold them in AWAITING_CLOUD but seeds NO `process_file:<id>` ledger row (which would let `recover_orphaned_work` replay the held file onto a LOCAL agent queue). The a1 path is unchanged (still seeds, D-09).
- **KROUTE-04 — static guard:** extended `test_no_default_queue_producers.py` to assert `submit_cloud_job` is a routed CONTROLLER_TASK and that the backfill candidate query is the bounded `ANALYSIS_FAILED ∧ duration ∧ EXISTS-ledger` filter, not a bare `state == ANALYSIS_FAILED` sweep.

## Task Commits

1. **Task 1 (RED): ledger-scoped candidate tests** - `90e1981` (test)
2. **Task 1 (GREEN): EXISTS scheduling_ledger predicate** - `e46b436` (feat)
3. **Task 2 (RED): cloud_target fork spy tests** - `02a0cb9` (test)
4. **Task 2 (GREEN): fork trigger_backfill_cloud on cloud_target** - `6a3741f` (feat)
5. **Task 3: AST guard extension (KROUTE-04)** - `ad1a9a3` (test)

## Files Created/Modified
- `src/phaze/services/pipeline.py` - `_backfill_candidates_stmt` EXISTS-ledger predicate; added `String`, `cast`, `SchedulingLedger` imports.
- `src/phaze/routers/pipeline.py` - `trigger_backfill_cloud` k8s fork: early-return after routing for k8s, skipping the held-file ledger seed.
- `tests/test_routers/test_pipeline.py` - `with_ledger` param on `_persist_failed_with_duration` (seeds process_file ledger rows by default); 2 candidate-query unit tests; 3 cloud_target fork tests (a1/k8s/local) with an `insert_ledger_if_absent` spy.
- `tests/test_no_default_queue_producers.py` - `submit_cloud_job` routing asserts + static `_backfill_candidates_stmt` ledger-scope guard.
- `.planning/.../deferred-items.md` - logged pre-existing test-harness flakiness (out of scope).

## Decisions Made
- The EXISTS predicate is semantically load-bearing: because `report_analysis_failed` clears the `process_file:<id>` ledger row (Phase 45 poison-case), a cleanly-failed file has NO row and is correctly excluded, while a SAQ-timed-out file (worker killed, no callback) keeps its orphaned row and IS re-driven. This is exactly the "timed-out long files" target set.
- L3 is observed at the `insert_ledger_if_absent` call boundary (monkeypatch spy), because with the prior candidacy row present both forks converge on exactly one ledger row — the seed *call* is the only distinguishable difference.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated test helper + one existing test to satisfy the new ledger-scope predicate**
- **Found during:** Task 1 (GREEN)
- **Issue:** The existing Phase-49/50 backfill tests seed `ANALYSIS_FAILED` long files with NO scheduling-ledger row and expect them to be selected. After adding the EXISTS predicate those files would be excluded, breaking ~6 regression tests.
- **Fix:** Added a `with_ledger: bool = True` param to `_persist_failed_with_duration` that seeds a `process_file:<id>` ledger row per file (modelling previously-scheduled timed-out work — the legitimate backfill target). Switched `test_backfill_disabled_when_cloud_local` to `with_ledger=False` so its `rows == []` no-seed assertion stays exact.
- **Files modified:** tests/test_routers/test_pipeline.py
- **Verification:** All 13 `backfill` tests pass; full `test_pipeline.py` = 92 passed.
- **Committed in:** `90e1981` (Task 1 RED — the test-infra change ships with the failing test)

---

**Total deviations:** 1 auto-fixed (1 bug — test fixture realignment). No production-scope creep.
**Impact on plan:** The helper change is required for the new predicate to coexist with the prior backfill regression suite; it makes the fixtures model reality (failed-then-timed-out files carry a ledger row).

## Issues Encountered
- **Pre-existing test-harness flakiness (out of scope):** `tests/test_routers/test_pipeline.py` intermittently errors at SETUP with an `agents` `IntegrityError` (`pk_agents` / duplicate `legacy-application-server`). The failing test is non-deterministic and moves across runs with pytest-randomly ordering; every failing test passes in isolation. Root cause is the function-scoped `async_engine` `create_all`/`drop_all` + legacy-agent seed racing against the shared test Postgres. Unrelated to this plan (all `backfill` + guard tests pass deterministically; `mypy .` clean across 182 files). Logged in `deferred-items.md`; not fixed per the executor scope boundary. Workaround used during verification: reset the test schema between file-level runs.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The k8s backfill seam is complete and bounded; ready for Plan 03 (`stage_cloud_window` k8s branch / `submit_cloud_job` wiring) to consume the AWAITING_CLOUD held files.
- The AST guard will fail loudly if a future edit drops the ledger EXISTS predicate or introduces a raw/default-queue enqueue in `routers/`.

## Self-Check: PASSED

- All created/modified files verified present (`55-04-SUMMARY.md`, `deferred-items.md`, both source + both test files committed).
- All task commits verified in history: `90e1981`, `e46b436`, `02a0cb9`, `6a3741f`, `ad1a9a3`, `69eb8d9`.
- Verification suite green: `tests/test_routers/test_pipeline.py` (92), `tests/test_no_default_queue_producers.py` (10), `tests/test_routing_seam.py` (5), `tests/test_task_split.py` (12); `mypy .` clean (182 files).

---
*Phase: 55-routing-state-ledger-integration-the-live-seam*
*Completed: 2026-06-28*
