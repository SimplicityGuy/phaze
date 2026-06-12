---
phase: 35-pipeline-determinism-idempotency-per-job-type-observability
plan: 02
subsystem: database
tags: [postgres, alembic, sqlalchemy, upsert, idempotency, partial-index, proposals, asyncpg]

# Dependency graph
requires:
  - phase: 35-01
    provides: schedule-safe deterministic enqueue keys (first half of the determinism guarantee)
provides:
  - "D-04: one active (PENDING) proposal per file, enforced at the DB level via a partial unique index"
  - "alembic revision 019: dedupe-then-index migration safe to apply to the live 11,428-file archive"
  - "store_proposals converted to a partial-index upsert (overwrite-in-place; approvals structurally protected)"
  - "audit confirming execution_log already idempotent and tag_write_log intentionally append-only"
  - "pipeline DB-write idempotency is now complete (proposals was the last non-idempotent task write)"
affects: [35-observability, proposals, generate_proposals, pipeline-determinism]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Partial unique index as an ON CONFLICT target: pg_insert(...).on_conflict_do_update(index_elements=[col], index_where=<partial predicate>)"
    - "Dedupe-before-index migration: collapse duplicates (row_number window) in op 1 so the unique-index build in op 2 cannot abort on live data"
    - "Explicit PK stamp on pg_insert rows (pg_insert bypasses the Python-side uuid4 default)"

key-files:
  created:
    - alembic/versions/019_add_proposals_pending_unique_index.py
    - tests/test_migration_019_dedupe.py
    - tests/test_proposals_upsert.py
  modified:
    - src/phaze/models/proposal.py
    - src/phaze/services/proposal.py
    - tests/test_services/test_proposal.py

key-decisions:
  - "Partial unique index uq_proposals_file_id_pending (WHERE status='pending') is the DB-level guard AND the upsert conflict target"
  - "Migration runs two ordered ops (dedupe keep-newest, then create unique index) so it cannot abort on the live archive's duplicate pending rows"
  - "tag_write_log left append-only on purpose — adding an upsert would erase its audit trail"

patterns-established:
  - "Partial-index upsert: index_where on the conflict matches a partial unique index so only the intended subset (PENDING) is a conflict target"
  - "Dedupe-then-constrain migration shape for adding a uniqueness constraint to a table that already has duplicates in production"

requirements-completed: [IDEMP]

# Metrics
duration: ~45min
completed: 2026-06-11
---

# Phase 35 Plan 02: Proposals Idempotency (D-04) Summary

**generate_proposals is now idempotent — a partial unique index (one PENDING proposal per file) backs a store_proposals upsert that overwrites the pending row in place while structurally protecting human-approved proposals, plus a dedupe-then-index migration safe for the live 11,428-file archive.**

## Performance

- **Duration:** ~45 min
- **Completed:** 2026-06-11
- **Tasks:** 2
- **Files modified:** 6 (3 created, 3 modified)

## Accomplishments

- Alembic revision 019: collapses pre-existing duplicate PENDING proposals (keep most-recent `created_at`) THEN creates the partial unique index `uq_proposals_file_id_pending` — two ordered ops so the index build cannot abort on the live archive.
- `RenameProposal.__table_args__` mirrors the partial unique index so the ORM / autogenerate stays in sync with the migration.
- `store_proposals` rewritten from per-proposal `session.add(RenameProposal(...))` to `pg_insert(...).on_conflict_do_update(index_elements=["file_id"], index_where=status=='pending', ...)` with explicit PK stamping. Re-runs overwrite the PENDING row in place; APPROVED/EXECUTED/REJECTED/FAILED rows fall outside the partial index and are never a conflict target.
- Audited the other two write targets in scope: `execution_log` already idempotent (`on_conflict_do_nothing` on agent-supplied id), `tag_write_log` intentionally append-only — documented both as correct-as-is in a module comment; neither changed.
- Proposals was the last non-idempotent task write; pipeline DB-write idempotency is now complete.

## Task Commits

1. **Task 1: Migration 019 (dedupe → partial unique index) + model `__table_args__` sync** - `4d674b4` (feat)
2. **Task 2: store_proposals → partial-index upsert + execution_log/tag_write_log audit** - `79d1d1c` (feat)

## Files Created/Modified

- `alembic/versions/019_add_proposals_pending_unique_index.py` - dedupe-then-index migration, down_revision=018, downgrade drops the index
- `src/phaze/models/proposal.py` - added the partial unique `Index` to `__table_args__` (imports `text`)
- `src/phaze/services/proposal.py` - `store_proposals` partial-index upsert + idempotency audit comment block
- `tests/test_migration_019_dedupe.py` - proves dedupe keeps newest PENDING, preserves APPROVED, index rejects a 2nd pending insert, allows a 2nd approved; upgrade/downgrade round-trip
- `tests/test_proposals_upsert.py` - double-run yields one pending row (second content wins), approved row preserved, fresh-insert PK stamp succeeds
- `tests/test_services/test_proposal.py` - updated 7 unit tests to inspect the upsert `.values()` instead of the removed constructor

## Decisions Made

- Used a partial unique index (`WHERE status = 'pending'`) as both the DB-level uniqueness guard and the `on_conflict_do_update` target — a single structure satisfies D-04 and the upsert.
- Migration op order is load-bearing: dedupe MUST precede the unique-index build on the live archive (35-RESEARCH Q3). Downgrade only drops the index; the dedupe is intentionally non-reversible (documented in the migration).
- Left `tag_write_log` append-only (RESEARCH Q4) — an upsert there would destroy the audit trail.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated 7 existing store_proposals unit tests broken by the upsert refactor**
- **Found during:** Task 2 (store_proposals conversion)
- **Issue:** `tests/test_services/test_proposal.py` had 7 tests that mocked `RenameProposal` as a constructor and asserted `MockProposal.call_args` / `session.add.assert_called_once()`. The refactor removes the constructor call and `session.add`, so these tests would fail — but the plan's acceptance criteria requires `pytest tests/ -k proposal` green.
- **Fix:** Rewrote the assertions to patch `phaze.services.proposal.pg_insert` and inspect `mock_pg_insert.return_value.values.call_args.kwargs` (the row dict), preserving identical behavioral coverage (path normalization, confidence clamping, context_used assembly, state transition). The plan's `files` for Task 2 did not list this test file, but it is the same proposal test module the criteria require to stay green.
- **Files modified:** tests/test_services/test_proposal.py
- **Verification:** `uv run pytest tests/ -k proposal` → 172 passed
- **Committed in:** `79d1d1c` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug — test fixup required by the refactor)
**Impact on plan:** Necessary to keep the proposal test suite green per the plan's own acceptance criteria. No scope creep; the production change is exactly as specified.

## Issues Encountered

- The first migration-test run failed because asyncpg rejects string literals for `created_at` (`expected a datetime.date or datetime.datetime instance`). Fixed by seeding with `datetime(...)` objects rather than ISO strings.
- A `noqa: PT011` directive tripped RUF100 (PT rules are not enabled in this repo). Removed the unneeded code, kept `B017`.

## User Setup Required

None - no external service configuration required. (Operator must apply `alembic upgrade head` on deploy to run revision 019 against the live archive; the migration is self-dedupe-ing and safe.)

## Next Phase Readiness

- D-04 idempotency is complete; re-running Generate Proposals no longer accumulates duplicate pending rows. The remaining Phase 35 items (centralized deterministic keys, auto-enqueue removal, per-job-type observability, DAG canvas) are independent of this plan.
- Verification: full suite `1662 passed`; `ruff check .` clean; `mypy .` clean (148 files); alembic upgrade/downgrade round-trips on the migrations test DB.

## Self-Check: PASSED

All created/modified files exist on disk; all task + metadata commits (`4d674b4`, `79d1d1c`, `119438f`) are present in git history.

---
*Phase: 35-pipeline-determinism-idempotency-per-job-type-observability*
*Completed: 2026-06-11*
