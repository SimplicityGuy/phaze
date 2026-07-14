---
phase: 83-cloud-routing-sidecar-cutover
plan: 02
subsystem: database
tags: [alembic, migration, postgres, cloud_job, sidecar, backfill]

# Dependency graph
requires:
  - phase: 77-additive-schema-rescan-wipe-fix-migration-032
    provides: "cloud_job 'awaiting' status CHECK value + ix_cloud_job_awaiting partial index + 032's _BACKFILL_CLOUD_AWAITING statement (the verbatim donor)"
  - phase: 81-per-stage-failure-persistence-retry-paths
    provides: "migration 033 (the alembic head 034 chains off)"
provides:
  - "Migration 034: one-shot data-only repair backfilling cloud_job(status='awaiting') for every file parked at state='awaiting_cloud' with no sidecar row since 032 (D-04)"
  - "Idempotent-backfill + empty-autogenerate migration test proving the repair, the ON CONFLICT skip, and no ORM schema drift"
affects: [83-06 drain-candidate cutover, 90 destructive migration renumber, shadow_compare awaiting_cloud invariant]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Repair migration: re-run a prior migration's backfill statement verbatim with ON CONFLICT DO NOTHING to fix a go-forward-writer gap, touching no ORM-mapped schema so autogenerate stays empty"

key-files:
  created:
    - alembic/versions/034_backfill_cloud_awaiting.py
    - tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py
  modified: []

key-decisions:
  - "downgrade() is a documented-lossy DELETE FROM cloud_job WHERE status='awaiting' — it cannot distinguish repaired rows from live go-forward holds, so it removes both (016/032/033 best-effort precedent)"
  - "034 touches no ORM __table_args__ — the 'awaiting' CHECK value and ix_cloud_job_awaiting already shipped in 032, so autogenerate stays trivially empty (77 D-01 contract)"

patterns-established:
  - "Data-only repair migration: sync def upgrade(), single static parameter-free op.execute of a prior migration's INSERT…SELECT…ON CONFLICT DO NOTHING; no schema DDL, empty autogenerate diff"

requirements-completed: [SIDECAR-01]

# Metrics
duration: 18min
completed: 2026-07-09
---

# Phase 83 Plan 02: Cloud-Awaiting Repair Migration Summary

**Migration 034 — a sync, data-only repair that re-runs 032's `INSERT…SELECT 'awaiting' … ON CONFLICT (file_id) DO NOTHING` to backfill the missing `cloud_job(status='awaiting')` sidecar row for every file held in `AWAITING_CLOUD` since 032, closing the D-04 corpus gap before the 83-06 reader cutover.**

## Performance

- **Duration:** ~18 min
- **Started:** 2026-07-09
- **Completed:** 2026-07-09
- **Tasks:** 2
- **Files modified:** 2 (both created)

## Accomplishments
- Added migration `034` chaining `down_revision="033"` (verified alembic head), applying cleanly on a fresh migrations test DB and landing as the new head.
- Repair backfill re-runs 032's `_BACKFILL_CLOUD_AWAITING` verbatim: static, parameter-free SQL, sync, no ORM schema change — `alembic revision --autogenerate` stays empty (asserted via `compare_metadata` in the test).
- Migration test proves: one `awaiting` row per held row-less file, zero for a non-awaiting control, an unchanged pre-existing `submitted` row (`ON CONFLICT DO NOTHING`), idempotency on re-run (no duplicate rows, `uq_cloud_job_file_id`), empty autogenerate diff, `files.state` byte-unchanged, and a working documented-lossy downgrade.
- Full `tests/integration/test_migrations/` suite green in isolation (67 passed) with both DB URLs exported on port 5433.

## Task Commits

Each task was committed atomically:

1. **Task 1: Write migration 034 — repair backfill** - `32b44445` (feat)
2. **Task 2: Migration test — idempotent backfill + empty autogenerate diff** - `6865ccdd` (test)

## Files Created/Modified
- `alembic/versions/034_backfill_cloud_awaiting.py` - Sync data-only repair migration; `upgrade()` runs 032's verbatim `INSERT…SELECT 'awaiting' … ON CONFLICT (file_id) DO NOTHING`; `downgrade()` is a documented-lossy `DELETE FROM cloud_job WHERE status='awaiting'`.
- `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py` - Static-SQL + saq-banner + revision-id unit assertions; integration body seeding row-less awaiting files, a control, and a pre-existing row, then asserting the repair, ON CONFLICT skip, idempotency, empty autogenerate diff, and downgrade.

## Decisions Made
- Chose the documented-lossy `DELETE FROM cloud_job WHERE status='awaiting'` downgrade over a bare no-op, following the plan's option and the 016/032/033 best-effort-reversal precedent (it cannot distinguish repaired rows from live go-forward holds).
- Scoped the test's empty-diff assertion to `{ix_cloud_job_awaiting}` (the only cloud-awaiting object the repair concerns), mirroring 032's `_diffs_touching_032` scoping so unrelated pre-existing ORM↔DB drift is not falsely attributed to 034.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None. The alembic env resolves the URL from `PHAZE_DATABASE_URL` (validation_alias), so the fresh-DB `alembic upgrade head` verification (Task 1 acceptance criterion) required exporting `PHAZE_DATABASE_URL` alongside `MIGRATIONS_TEST_DATABASE_URL`/`DATABASE_URL` — all pointed at port 5433 (the `just test-db` provisioning port, per the documented footgun).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The already-held `AWAITING_CLOUD` corpus now gains its `awaiting` sidecar row, so the hard shadow invariant `AWAITING_CLOUD ⇒ cloud_job(status='awaiting')` holds for existing files.
- Sequenced in Wave 1: `034` lands before the 83-06 drain-candidate reader cutover, so repaired files are visible to the drain query instead of stranding.
- Phase 90's destructive migration will renumber `034 → 035` (doc-only churn already accepted by 81 D-08); this plan only ADDS `034`.

## Self-Check: PASSED

- Files verified present: `alembic/versions/034_backfill_cloud_awaiting.py`, `tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py`, `.planning/phases/83-cloud-routing-sidecar-cutover/83-02-SUMMARY.md`
- Commits verified in history: `32b44445` (Task 1), `6865ccdd` (Task 2)

---
*Phase: 83-cloud-routing-sidecar-cutover*
*Completed: 2026-07-09*
