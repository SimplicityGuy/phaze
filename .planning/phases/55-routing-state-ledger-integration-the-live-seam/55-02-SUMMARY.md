---
phase: 55-routing-state-ledger-integration-the-live-seam
plan: 02
subsystem: database
tags: [alembic, sqlalchemy, postgres, kueue, cloud_job, saq, k8s]

# Dependency graph
requires:
  - phase: 54
    provides: "cloud_job sidecar table + CloudJobStatus lifecycle + submit_cloud_job / reconcile_cloud_jobs Kube submit+reconcile tasks"
provides:
  - "cloud_job.cloud_phase admission-progression column (nullable, CHECK-constrained) via additive/reversible migration 027"
  - "CloudPhase StrEnum (queued_behind_quota / admitted / running / finished) on the CloudJob model"
  - "submit_cloud_job seeds cloud_phase=queued_behind_quota (reset on re-submit)"
  - "reconcile_cloud_jobs co-writes the admission progression per Kueue Job/Workload condition branch"
affects: [55-05-dashboard-cards, KROUTE-06, cloud_phase counts]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-phase scoped migration: each new cloud_job column lands in its own additive/reversible migration (027) referencing cloud_job ONLY (never saq_jobs)"
    - "Orthogonal axis co-write: cloud_phase (admission progression) written alongside status, kept independent of the inadmissible fault flag"
    - "Bare CHECK-constraint name (cloud_phase_enum) so the ck_%(table_name)s_%(constraint_name)s convention re-applies the ck_cloud_job_ prefix"

key-files:
  created:
    - alembic/versions/027_add_cloud_job_cloud_phase.py
    - tests/test_migrations/test_migration_027_cloud_phase.py
  modified:
    - src/phaze/models/cloud_job.py
    - src/phaze/tasks/submit_cloud_job.py
    - src/phaze/tasks/reconcile_cloud_jobs.py
    - tests/test_tasks/test_submit_cloud_job.py
    - tests/test_tasks/test_reconcile_cloud_jobs.py

key-decisions:
  - "Mapped QuotaReserved-only (quota granted, pod not yet un-suspended) -> cloud_phase=admitted, and Admitted=True (pod un-gated/running) -> cloud_phase=running, realizing the admitted->running progression the plan describes while keeping the cloud_job status advance to RUNNING unchanged in both cases"
  - "cloud_phase kept ORTHOGONAL to the inadmissible fault flag: the Inadmissible branch never writes cloud_phase (asserted by test)"

patterns-established:
  - "cloud_phase co-write: each reconcile condition branch stamps the admission phase before its existing commit; the Pending branch uses a dirty-flag so a no-op stays a no-op"

requirements-completed: [KROUTE-03]

# Metrics
duration: 12min
completed: 2026-06-28
---

# Phase 55 Plan 02: cloud_job.cloud_phase Admission Progression Summary

**Added the nullable, CHECK-constrained `cloud_phase` column (CloudPhase StrEnum: queued_behind_quota → admitted → running → finished) to the `cloud_job` sidecar via additive/reversible migration 027, seeded it in `submit_cloud_job`, and co-wrote it per Kueue Job/Workload condition branch in the `reconcile_cloud_jobs` cron — kept orthogonal to the existing `inadmissible` fault flag.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-06-28T19:33:00Z
- **Completed:** 2026-06-28T19:41:00Z
- **Tasks:** 2 (both TDD)
- **Files modified:** 7 (2 created, 5 modified)

## Accomplishments
- Migration 027: additive/reversible `cloud_phase` String(20) column + `cloud_phase_enum` CHECK on `cloud_job` only (never `saq_jobs`); round-trip verified against the migrations test DB.
- `CloudPhase` StrEnum + `cloud_phase` Mapped column + parallel CheckConstraint on the `CloudJob` model.
- `submit_cloud_job` seeds `cloud_phase=queued_behind_quota` in both the upsert `.values(...)` and the `on_conflict_do_update` `set_` (re-submit resets the progression).
- `reconcile_cloud_jobs` co-writes the admission progression: Pending → queued_behind_quota, QuotaReserved-only → admitted, Admitted → running, success → finished; the Inadmissible branch leaves `cloud_phase` untouched.

## Task Commits

Each task was committed atomically, following the TDD RED → GREEN gate:

1. **Task 1: cloud_phase column — migration 027 + CloudPhase enum + model column**
   - `c37a55e` (test — RED: round-trip migration test)
   - `de1f39e` (feat — GREEN: migration 027 + CloudPhase enum + model column)
2. **Task 2: seed cloud_phase in submit + co-write it in reconcile**
   - `3c10391` (test — RED: submit seed/reset + reconcile per-branch + orthogonality tests)
   - `107b589` (feat — GREEN: submit seed + reconcile co-writes)

_TDD gate sequence satisfied for both tasks: a `test(...)` commit precedes each `feat(...)` commit._

## Files Created/Modified
- `alembic/versions/027_add_cloud_job_cloud_phase.py` - Additive/reversible migration adding the nullable `cloud_phase` column + `cloud_phase_enum` CHECK (cloud_job-only).
- `tests/test_migrations/test_migration_027_cloud_phase.py` - Round-trip test: bare-number revision asserts, upgrade adds the column, the 4 members + NULL are accepted, a bogus value is rejected, downgrade drops it cleanly.
- `src/phaze/models/cloud_job.py` - `CloudPhase` StrEnum + `cloud_phase` Mapped column + parallel CheckConstraint in `__table_args__`.
- `src/phaze/tasks/submit_cloud_job.py` - Seeds `cloud_phase=queued_behind_quota` in the upsert values + set_.
- `src/phaze/tasks/reconcile_cloud_jobs.py` - Co-writes `cloud_phase` in `_record_success` (finished), the Pending branch (queued_behind_quota), and the Admitted/QuotaReserved branch (admitted/running).
- `tests/test_tasks/test_submit_cloud_job.py` - Seed + re-submit-reset tests.
- `tests/test_tasks/test_reconcile_cloud_jobs.py` - Per-branch cloud_phase + orthogonality (Inadmissible never touches cloud_phase) tests.

## Decisions Made
- **Admitted vs running split in the reconcile admitted branch:** RESEARCH/PLAN describe the progression "admitted then running" within the Admitted/QuotaReserved branch. Realized this by distinguishing the two Kueue signals already available: `QuotaReserved=True` alone (quota reserved, pod not yet un-suspended) → `cloud_phase=admitted`; `Admitted=True` (workload fully admitted, pod un-gated/running) → `cloud_phase=running`. The `cloud_job.status` axis still advances SUBMITTED→RUNNING in both cases (unchanged), preserving the orthogonality of the two axes. This makes both members independently testable via the existing `QUOTA_RESERVED` and `ADMITTED` kube fakes.
- **cloud_phase is orthogonal to inadmissible:** the Inadmissible branch writes only the fault flag; a test asserts `cloud_phase` stays NULL through an Inadmissible tick.

## Deviations from Plan

None - plan executed exactly as written. (The "admitted then running" mapping in the Admitted/QuotaReserved branch was implemented as a QuotaReserved-only → admitted / Admitted=True → running split — an interpretation of the plan's stated progression, not a deviation; all acceptance criteria and behaviors are met.)

## Issues Encountered
- The migration round-trip integration test requires the ephemeral `phaze_migrations_test` Postgres (started via `just test-db` on host port 5433, with `MIGRATIONS_TEST_DATABASE_URL` overridden). A first failing run left stale `status='submitted'` rows that tripped the 026→025 status-CHECK on the teardown downgrade chain; resolved by (a) adding a `DELETE FROM cloud_job` before the downgrade in the test (mirroring the 026 test) and (b) resetting the migrations test schema. Both are test-harness mechanics, not product behavior.

## Note on the saq_jobs acceptance grep
The plan's literal acceptance `grep -c saq_jobs ... == 0` is non-zero (1) because migration 027 carries the same CRITICAL "must never reference `saq_jobs`" banner docstring that migrations 020/025/026 use. The enforced invariant — no non-comment `saq_jobs` reference — passes (the `test_migration_never_references_saq_jobs` test is green), matching the established 026 precedent.

## User Setup Required
None - no external service configuration required. (cloud_phase is NULL for a1/local rows; admission is k8s-only.)

## Next Phase Readiness
- The `cloud_phase` column + writers are in place; Plan 05 (KROUTE-06 dashboard cards) can now read the column via `get_cloud_phase_counts`.
- Migration 027 is the new head (chains 026 → 027).

---
*Phase: 55-routing-state-ledger-integration-the-live-seam*
*Completed: 2026-06-28*
