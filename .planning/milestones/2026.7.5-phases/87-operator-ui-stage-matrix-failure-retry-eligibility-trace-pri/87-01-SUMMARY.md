---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 01
subsystem: schema
tags: [migration, orm, sidecar, stage-skip, derive-dont-store]
requires: []
provides:
  - stage_skip table (file_id, stage) force-skip marker sidecar
  - StageSkip ORM model on Base.metadata + models.__all__
  - alembic migration 037 (head; down_revision 036)
affects:
  - Phase 87 seam-a derivation (skipped_clause, resolve_status skipped bucket) reads this marker
tech-stack:
  added: []
  patterns:
    - "sidecar marker table: existence = fact, undo = DELETE row (derive-don't-store)"
    - "ORM __table_args__ mirrors migration DDL byte-for-byte (empty-autogenerate-diff contract)"
    - "bare CheckConstraint name -> ck_%(table_name)s_%(constraint_name)s convention; op.f() verbatim in migration"
key-files:
  created:
    - src/phaze/models/stage_skip.py
    - alembic/versions/037_add_stage_skip.py
    - tests/integration/test_migrations/test_037_stage_skip.py
  modified:
    - src/phaze/models/__init__.py
decisions:
  - "CHECK constraint uses bare ORM name 'enrich_only' (not 'ck_stage_skip_enrich_only') to avoid double-prefix under the ck_ naming convention"
  - "Migration docstring avoids the literal saq_jobs token so both the inline grep gate and the test guard pass"
metrics:
  duration: ~35m
  completed: 2026-07-11
  tasks: 3
  files: 4
---

# Phase 87 Plan 01: stage_skip Force-Skip Marker Sidecar Summary

The D-13 non-UI foundation: a `(file_id, stage)` `stage_skip` sidecar table — the sole *stored*
force-skip fact for the three enrich stages (metadata/analyze/fingerprint) — delivered as an ORM model,
registry registration, additive migration 037, and a mutation-verified migration integration test.

## What Was Built

- **`StageSkip` ORM model** (`src/phaze/models/stage_skip.py`) mirroring the `dedup_resolution` sidecar:
  UUID PK, `file_id` FK→`files.id` (NOT unique alone), `stage` String, `reason` Text (D-09 required),
  `skipped_at` TIMESTAMPTZ default now, `created_at`/`updated_at` from `TimestampMixin`. `__table_args__`
  carries `UNIQUE(file_id, stage)` (`uq_stage_skip_file_stage`) + enrich-only `CHECK`
  (`ck_stage_skip_enrich_only`). Registered on `Base.metadata` and `models.__all__` (load-bearing for the
  autogenerate empty-diff contract).
- **Migration 037** (`alembic/versions/037_add_stage_skip.py`): `revision="037"`, `down_revision="036"`,
  additive `create_table("stage_skip", ...)` with `op.f()` bare constraint names; mirrored
  `downgrade()` = `drop_table`. No backfill (greenfield marker). No SAQ-owned jobs table reference.
- **Migration integration test** (`tests/integration/test_migrations/test_037_stage_skip.py`): bare-number
  revision assert, no-`saq_jobs` grep guard, and a DB body proving table existence, UNIQUE rejection,
  enrich-only CHECK rejection of `'propose'`, empty autogenerate diff, and mirrored downgrade drop.

## How to Verify

- `just test-db` (Postgres on :5433), then
  `MIGRATIONS_TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test uv run pytest tests/integration/test_migrations/test_037_stage_skip.py -q` → 3 passed.
- `uv run mypy src/phaze/models/stage_skip.py` and `uv run ruff check .` → clean.
- Rendered ORM constraint names equal the migration `op.f()` names:
  `pk_stage_skip`, `uq_stage_skip_file_stage`, `fk_stage_skip_file_id_files`, `ck_stage_skip_enrich_only`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] ORM CheckConstraint name would double-prefix**
- **Found during:** Task 1
- **Issue:** The plan instructed passing `name="ck_stage_skip_enrich_only"` to the ORM `CheckConstraint`.
  The `Base` naming convention is `ck_%(table_name)s_%(constraint_name)s`, which treats the passed name as
  the `%(constraint_name)s` token and prepends `ck_stage_skip_`, rendering
  `ck_stage_skip_ck_stage_skip_enrich_only` — a mismatch against the migration's verbatim
  `op.f("ck_stage_skip_enrich_only")`, which would break the empty-autogenerate-diff contract.
- **Fix:** Passed the bare suffix `name="enrich_only"` (mirroring the `analysis.py:56` bare-name
  CheckConstraint discipline), which renders exactly `ck_stage_skip_enrich_only`. Verified the rendered
  name set matches the migration and the empty-diff test is green.
- **Files modified:** `src/phaze/models/stage_skip.py`
- **Commit:** ddc49b32

**2. [Rule 3 - Blocking] Migration docstring tripped the inline saq_jobs grep gate**
- **Found during:** Task 2
- **Issue:** Task 2's inline `<verify>` gate strips only `#`-comment lines before counting `saq_jobs`
  occurrences; the CRITICAL banner in the module docstring (not a `#` comment) mentioned the literal token,
  producing a non-zero count and failing the gate.
- **Fix:** Reworded the banner to "the SAQ-owned jobs table", removing the literal token entirely so the
  file contains zero `saq_jobs` occurrences (satisfying both the inline gate and the test guard, which
  additionally has a "never reference" exemption).
- **Files modified:** `alembic/versions/037_add_stage_skip.py`
- **Commit:** d44fa707

## Notes

- **Mutation-tested guard teeth (project memory rule):** the enrich-only CHECK assertion was mutation-tested
  by widening the migration's *DDL* `CheckConstraint` to admit `'propose'` — the test then failed with
  `DID NOT RAISE IntegrityError`, and restoring returned it to green. An initial mutation attempt misfired
  by replacing the first (docstring) occurrence of the predicate string rather than the DDL line; corrected
  to target the `sa.CheckConstraint(...)` line specifically. The empty-diff helper is table-scoped
  (add/remove `stage_skip`), so it does not police constraint-name drift — the constraint enforcement teeth
  are proven by the real DB `IntegrityError` assertions (UNIQUE + CHECK), not by the diff helper.
- Full `tests/integration/test_migrations/` suite (78 tests) green in isolation — migration 037 at head does
  not disturb sibling per-revision migration tests.

## Threat Register Coverage

- **T-87-01** (no SAQ jobs table reference): mitigated — zero-occurrence grep guard + banner reword.
- **T-87-02** (non-enrich stage): mitigated — `ck_stage_skip_enrich_only` CHECK, asserted (rejects `'propose'`).
- **T-87-03** (duplicate skip rows): mitigated — `UNIQUE(file_id, stage)`, asserted (rejects duplicate).
- **T-87-04** (destructive downgrade): accepted — mirrored `drop_table` on an additive greenfield table.

No new threat surface introduced beyond the plan's register.

## Self-Check: PASSED

All 4 key files present on disk; all 3 task commits (ddc49b32, d44fa707, 048a5546) found in git history.
