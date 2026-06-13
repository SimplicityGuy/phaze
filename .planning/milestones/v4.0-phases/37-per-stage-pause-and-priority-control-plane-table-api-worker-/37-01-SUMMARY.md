---
phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker
plan: 01
subsystem: database
tags: [postgres, alembic, sqlalchemy, saq, pipeline-control, migration]

# Dependency graph
requires:
  - phase: 36-pipeline-queue-backend-migration
    provides: "Postgres-backed saq_jobs table + build_pipeline_queue factory (the substrate the control plane operates on)"
  - phase: 35-pipeline-determinism-idempotency
    provides: "deterministic <function>:<file_id> keys (_KEY_BUILDERS) that make STAGE_TO_FUNCTION key-prefix filtering exact"
provides:
  - "pipeline_stage_control app table (model + seeded migration 020) — durable per-stage pause/priority intent"
  - "DB CHECK priority BETWEEN 0 AND 100 enforcing the dequeueable-window guarantee at the schema layer"
  - "Canonical STAGE_TO_FUNCTION / _FUNCTION_TO_STAGE / SENTINEL constants (DB-free, agent-boundary-safe)"
affects: [37-02-hook, 37-03-services, 37-04-endpoints, 38-pipeline-dag-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Standalone app table separate from SAQ-owned saq_jobs (no FK, no migration touching saq_jobs)"
    - "Interface-first constants module imported by all downstream plans (single source of truth)"
    - "DB CHECK as a correctness guard mirroring endpoint clamping (defense in depth)"

key-files:
  created:
    - src/phaze/models/pipeline_stage_control.py
    - alembic/versions/020_add_pipeline_stage_control.py
    - src/phaze/tasks/_shared/stage_control.py
    - tests/test_migrations/test_020.py
  modified:
    - src/phaze/models/__init__.py
    - .gitignore

key-decisions:
  - "priority typed SmallInteger to match saq_jobs.priority (SMALLINT); DB CHECK 0-100 keeps it inside SAQ's 0-32767 dequeue window"
  - "CHECK named via convention (ck_pipeline_stage_control_priority_range) using bare CheckConstraint name='priority_range' in the model; explicit op.f() in the migration"
  - "Seed via per-stage bound-param INSERT (no f-string interpolation) per threat T-37-01"
  - "_FUNCTION_TO_STAGE included in __all__ despite underscore prefix — it is a deliberate public constant for the Plan-02 hook"

patterns-established:
  - "Pattern 1: per-stage control row as a normal ORM/Alembic app table, never co-mingled with SAQ's auto-managed schema"
  - "Pattern 2: DB-free pure-constants module (no sqlalchemy.ext.asyncio / phaze.database) so the agent worker can import it"

requirements-completed: [REQ-37-1, REQ-37-2, REQ-37-3]

# Metrics
duration: 3min
completed: 2026-06-13
---

# Phase 37 Plan 01: Per-Stage Control-Plane Substrate Summary

**pipeline_stage_control app table (model + seeded migration 020 with a priority 0-100 CHECK) plus the canonical DB-free STAGE_TO_FUNCTION / _FUNCTION_TO_STAGE / SENTINEL constants every later plan imports**

## Performance

- **Duration:** ~3 min (execution); commits 09:47–09:50 -07:00
- **Started:** 2026-06-13T09:47:00-07:00
- **Completed:** 2026-06-13T09:49:51-07:00
- **Tasks:** 3
- **Files modified:** 6 (4 created, 2 modified)

## Accomplishments
- `PipelineStageControl` ORM model: `stage` PK, `paused`, `priority` (SmallInteger), free timestamps via `TimestampMixin`, DB CHECK `priority BETWEEN 0 AND 100`; registered in `models/__init__.py` for Alembic autogenerate.
- Migration 020 (revises 019): creates `pipeline_stage_control`, seeds exactly 3 rows (metadata/analyze/fingerprint) at `paused=false, priority=50`, touches ONLY the new table (never `saq_jobs`); proven by a real-PG upgrade/downgrade + seed-count + CHECK-rejection test.
- DB-free `stage_control.py` constants module: `STAGE_TO_FUNCTION`, exact inverse `_FUNCTION_TO_STAGE`, and the single shared `SENTINEL = 9999999999`, with verified no `sqlalchemy.ext.asyncio` / `phaze.database` import (agent-boundary-safe).

## Task Commits

Each task was committed atomically:

1. **Task 1: PipelineStageControl model + registry registration** - `8c23e95` (feat)
2. **Task 2: Migration 020 create+seed + real-PG migration test** - `327ffdd` (feat)
3. **Task 3: Canonical DB-free stage-control constants module** - `24f2f2e` (feat)

## Files Created/Modified
- `src/phaze/models/pipeline_stage_control.py` - Standalone ORM model for the control table with the priority CHECK.
- `alembic/versions/020_add_pipeline_stage_control.py` - Migration creating + seeding the table; reversible downgrade.
- `src/phaze/tasks/_shared/stage_control.py` - Canonical stage→function map, inverse, and SENTINEL park constant.
- `tests/test_migrations/test_020.py` - Real-PG migration proof (revision chain, seed, CHECK, downgrade).
- `src/phaze/models/__init__.py` - Registered `PipelineStageControl` (import + `__all__`).
- `.gitignore` - Negation so `src/phaze/models/` source package is no longer shadowed by the ML `models/` rule.

## Decisions Made
- `priority` is `SmallInteger` to match `saq_jobs.priority` (SMALLINT); the DB CHECK `0..100` keeps a stage inside SAQ's `0..32767` dequeue window so it can never be driven silently un-dequeueable at the schema layer (threat T-37-02).
- Seeded the 3 rows via one bound-param `INSERT` per stage (no f-string interpolation), mirroring the 012/019 idiom (threat T-37-01).
- `_FUNCTION_TO_STAGE` is listed in `__all__` even though underscore-prefixed — it is a deliberate public constant the Plan-02 enqueue hook resolves `job.function` against.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Un-ignored the `src/phaze/models/` source package in .gitignore**
- **Found during:** Task 1 (committing the new model file)
- **Issue:** `git add src/phaze/models/pipeline_stage_control.py` was rejected — `.gitignore:218` had an unanchored `models/` rule (intended for ML model artifacts downloaded via `scripts/download-models.sh`) that also matches the source package `src/phaze/models/`. Existing model source files were tracked only because they predated/escaped the rule; the new untracked file was ignored and could not be committed.
- **Fix:** Added a negation `!src/phaze/models/` immediately after the `models/` rule with an explanatory comment, re-including the real source package while keeping ML-model artifact dirs ignored.
- **Files modified:** .gitignore
- **Verification:** `git check-ignore -v src/phaze/models/pipeline_stage_control.py` now reports "no longer ignored"; the file committed successfully.
- **Committed in:** `8c23e95` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** The fix was required to commit the planned model file. No scope creep — narrow `.gitignore` negation; ML-artifact ignoring is preserved.

## Issues Encountered
- ruff-format collapsed the multi-line seed `sa.text(...)` string onto one line (within the 150-char limit) on the Task 2 commit; re-staged the formatted file and the commit succeeded. No logic change.

## User Setup Required
None - no external service configuration required. (Migration 020 applies via the normal `alembic upgrade` path on deploy.)

## Next Phase Readiness
- The control table, the priority CHECK, and the canonical constants are in place. Plan 37-02 can now author the `apply_stage_control` before-enqueue hook + 5s TTL cache against `STAGE_TO_FUNCTION` / `SENTINEL` and register it in `build_pipeline_queue`; Plans 37-03/04 build the raw-UPDATE helpers and the pause/priority/resume endpoints against the same constants.
- Migration test requires the ephemeral PG (`just test-db` / `just integration-test`, ports 5433/6380); it is not run by the default unit-test path.

## Self-Check: PASSED
- Created files present: `src/phaze/models/pipeline_stage_control.py`, `alembic/versions/020_add_pipeline_stage_control.py`, `src/phaze/tasks/_shared/stage_control.py`, `tests/test_migrations/test_020.py` — all FOUND.
- Commits present: `8c23e95`, `327ffdd`, `24f2f2e` — all FOUND in git log.

---
*Phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker*
*Completed: 2026-06-13*
