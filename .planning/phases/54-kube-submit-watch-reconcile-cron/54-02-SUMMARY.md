---
phase: 54-kube-submit-watch-reconcile-cron
plan: 02
subsystem: database
tags: [sqlalchemy, alembic, postgres, cloud_job, kueue, strenum]

# Dependency graph
requires:
  - phase: 53-s3-object-staging-leg
    provides: cloud_job per-file_id staging sidecar (status StrEnum + CHECK, unique FK to files.id) and migration 025
provides:
  - "CloudJobStatus extended with SUBMITTED/RUNNING/SUCCEEDED submit/reconcile lifecycle members"
  - "cloud_job.kueue_workload (String(255), nullable) — the Kueue/Job name stamped at submit"
  - "cloud_job.attempts (int, NOT NULL, default 0) — bounded re-drive counter (D-08)"
  - "cloud_job.inadmissible (bool, NOT NULL, default false) — drives the D-06 operator alert"
  - "Additive reversible migration 026 (chains off 025, scoped to cloud_job only)"
affects: [submit_cloud_job, reconcile_cloud_jobs, kube_staging, inadmissible-alert, 55-routing-state-ledger]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "string-backed StrEnum extension: new members need only the CHECK membership list updated, no Postgres enum-type migration"
    - "additive reversible migration with bare-name CHECK swap (drop_constraint + create_check_constraint, name re-prefixed by naming convention)"

key-files:
  created:
    - alembic/versions/026_add_cloud_job_kube_columns.py
    - tests/test_migrations/test_migration_026_kube_columns.py
  modified:
    - src/phaze/models/cloud_job.py
    - tests/test_models/test_cloud_job.py

key-decisions:
  - "cloud_phase deliberately left untouched (reserved for Phase 55's own migration); only kueue_workload/attempts/inadmissible land here"
  - "status stays String(16) — 'succeeded' (9 chars) fits; no column widening needed"
  - "migration touches ONLY cloud_job, never saq_jobs (SAQ owns that table); grep-style test enforces it"

patterns-established:
  - "StrEnum membership widening via CHECK-only swap: drop status_enum + recreate with the 6-member list, both upgrade and downgrade"
  - "migration test clears rows carrying new statuses before exercising downgrade (narrowed CHECK would reject in-flight data — operator drains first)"

requirements-completed: [KSUBMIT-04, KSUBMIT-05, KSUBMIT-06]

# Metrics
duration: 22min
completed: 2026-06-28
---

# Phase 54 Plan 02: cloud_job Kube Lifecycle Schema Summary

**Extended the Phase 53 `cloud_job` sidecar with the Kube submit/reconcile lifecycle — SUBMITTED/RUNNING/SUCCEEDED status members plus `kueue_workload`/`attempts`/`inadmissible` columns — delivered via additive reversible migration 026 (D-09), with `cloud_phase` deferred to Phase 55.**

## Performance

- **Duration:** ~22 min
- **Started:** 2026-06-28T14:08Z
- **Completed:** 2026-06-28T14:30Z
- **Tasks:** 2
- **Files modified:** 4 (2 created, 2 modified)

## Accomplishments
- `CloudJobStatus` StrEnum gained SUBMITTED/RUNNING/SUCCEEDED (existing UPLOADING/UPLOADED/FAILED retained); only the CHECK membership list changed — no Postgres enum-type migration.
- `cloud_job` gained `kueue_workload` (the Kueue/Job name, nullable), `attempts` (bounded re-drive counter, default 0) and `inadmissible` (operator-alert flag, default false) columns.
- Migration 026 chains off 025, is additive + reversible, swaps the status CHECK to the 6-member list, and touches ONLY `cloud_job` (never `saq_jobs`).
- `cloud_phase` left untouched, reserved for Phase 55.

## Task Commits

Each task was committed atomically (Task 1 was TDD: test → feat):

1. **Task 1 (RED): failing model tests** - `7c5a2ad` (test)
2. **Task 1 (GREEN): extend CloudJobStatus + kube columns** - `d4b3b97` (feat)
3. **Task 2: migration 026 + migration test** - `5a36341` (feat)

## Files Created/Modified
- `src/phaze/models/cloud_job.py` - extended `CloudJobStatus` with 3 lifecycle members; added `kueue_workload`/`attempts`/`inadmissible` columns; swapped CHECK to the 6-member list; updated module docstring (cloud_phase still deferred).
- `alembic/versions/026_add_cloud_job_kube_columns.py` - additive reversible migration: adds the 3 columns + CHECK swap on upgrade, reverses both on downgrade; CRITICAL "ONLY cloud_job" banner.
- `tests/test_models/test_cloud_job.py` - asserts the 3 new enum members, the 3 column shapes/defaults, and that `cloud_phase` stays absent.
- `tests/test_migrations/test_migration_026_kube_columns.py` - static revision assertions + never-references-saq_jobs grep + integration upgrade/downgrade round-trip proving column presence, widened CHECK membership, and clean reversal.

## Decisions Made
- `cloud_phase` deferred to Phase 55 (its own migration) — keeps each migration scoped to its phase.
- `status` column kept at `String(16)` — `succeeded` (9 chars) fits, no widening.
- `attempts`/`inadmissible` carry both `server_default` (DB) and `default` (ORM) so existing rows backfill and new ORM inserts get the value without a DB round-trip.

## Deviations from Plan

None - plan executed exactly as written. Both new files and both modified files match the planned shapes; no auto-fixes (Rules 1-3) were needed against the codebase.

## Issues Encountered
- **Migration test downgrade CheckViolation (test-only bug, self-fixed before commit):** Two issues in the first draft of `test_migration_026_kube_columns.py` — (1) the `running`/`succeeded` loop built file UUIDs from `status[:2]` (`"ru"`/`"su"`), which are non-hex and aborted `_seed_file` mid-loop, leaving `'submitted'` rows uncleaned so the downgrade's narrowed CHECK violated; (2) the downgrade step needed an explicit `DELETE FROM cloud_job` first (the narrowed 3-member CHECK rejects in-flight new-status rows — mirrors the real operator "drain before downgrade" requirement). Fixed by using valid-hex UUIDs and clearing rows before downgrade. No production code affected.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The `cloud_job` sidecar now carries the full submit/reconcile lifecycle that Phase 54 Plan 03+ (`submit_cloud_job`, `reconcile_cloud_jobs`, `kube_staging`) will read/write.
- `inadmissible` column is ready for the D-06 operator-alert count reader and pipeline card.
- `cloud_phase` remains the only deferred column, reserved for Phase 55's routing/state/ledger integration.

## Verification
- `uv run pytest tests/test_models/test_cloud_job.py tests/test_migrations/test_migration_026_kube_columns.py` → 19 passed (against ephemeral Postgres on 5433 via `just test-db`).
- `uv run pytest tests/test_migrations/ -k cloud_job` → green; migration 025 regression → 3 passed.
- `uv run ruff check .` → all checks passed; `uv run mypy src/phaze/models/cloud_job.py` → success, no issues.

## Self-Check: PASSED
- `src/phaze/models/cloud_job.py` — FOUND
- `alembic/versions/026_add_cloud_job_kube_columns.py` — FOUND
- `tests/test_models/test_cloud_job.py` — FOUND
- `tests/test_migrations/test_migration_026_kube_columns.py` — FOUND
- Commit `7c5a2ad` — FOUND
- Commit `d4b3b97` — FOUND
- Commit `5a36341` — FOUND

---
*Phase: 54-kube-submit-watch-reconcile-cron*
*Completed: 2026-06-28*
