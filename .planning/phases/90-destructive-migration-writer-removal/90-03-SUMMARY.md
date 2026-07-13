---
phase: 90-destructive-migration-writer-removal
plan: 03
status: partial
requirements: [MIG-04]
depends_on: [90-01, 90-02]
completed_tasks: [1, 2]
deferred_tasks: [3]
deferred_to: 90-04
---

# 90-03 SUMMARY — PR-C, MIG-04 (PARTIAL — migration landed, model/enum retirement split to 90-04)

## Status: PARTIAL (executor checkpoint → user decision to split)

Tasks 1–2 (the irreversible-critical migration `039`) are **complete, merged, and tested**.
Task 3 (delete the `FileState` enum + `state` column, remove the `shadow_compare` subsystem,
migrate the ~90 dependent test files, add the D-08 mutation-tested guard) was **split into a
new, properly-scoped plan `90-04`** at a decision checkpoint. The plan's `files_modified`
budgeted ~26 SRC files; the real blast radius is **90 test files (718 `FileState` refs, 328
`state=` seed sites) plus a 14-file `shadow_compare` service/CLI/divergence-test subsystem** —
a Rule-4 structural change the plan never budgeted. Per the checkpoint protocol (auto-mode off,
sole irreversible plan), the executor stopped at the clean boundary and the user chose to
re-plan the remainder rather than power-drive ~90 files through a continuation.

## What shipped (Tasks 1–2)

- **`alembic/versions/039_drop_files_state_column.py`** (265 lines) — the destructive migration,
  in ONE transaction:
  - Archives `files.state` verbatim into **`files_state_archive`** (D-10 lossless primary).
  - Idempotent **delta top-up** for anything changed since 032.
  - **Guard-first, data-only self-guard (D-06):** RAISEs/aborts on mid-flight rows
    (`state IN ('pushing','uploading')` or non-terminal `cloud_job`) and on shadow-compare
    implication violations; runs clean on an empty/fresh DB (avoids the Phase-89 038 CR-02
    fresh-DB-abort footgun).
  - **D-07:** the shadow-compare precondition is INLINE sync SQL transcribed from
    `shadow_compare.INVARIANTS` (hard invariants only) — does **not** import
    `services/shadow_compare.py`, never touches `saq_jobs` or the scheduling ledger.
  - Drops `ix_files_state` + `files.state` under a **`lock_timeout` + `begin_nested`
    savepoint-retry** wrapper so the `ACCESS EXCLUSIVE` lock aborts-and-retries instead of
    queuing behind the 5s `/pipeline/stats` poll (ROADMAP success-criterion 1).
  - **`downgrade()`** restores `files.state` verbatim from `files_state_archive` (D-10), with a
    derived furthest-along + marker-override reconstruction as the documented FALLBACK for rows
    created after 039; round-trips up→down→up.
- **`alembic/env.py`** — supporting change for the migration test's autogenerate comparison.
- **`tests/integration/test_migrations/test_migration_039_drop_files_state_column.py`** (462 lines)
  — chained from the 038 fileserver-seeded fixture. **13/14 tests GREEN.**

Commits (merged to `SimplicityGuy/phase-90`):
- `af880f1f` — test(90-03): failing migration 039 integration test (RED)
- `70ccff33` — feat(90-03): guarded reversible migration 039 dropping files.state (GREEN)
- merged via `chore(90-03): merge migration 039 (PR-C Tasks 1-2) — Task 3 split to 90-04`

## Known expected-RED (closed by 90-04)

`test_039_autogenerate_diff_is_empty_for_dropped_objects` is **RED by design**: after 039 the DB
no longer has `files.state`/`ix_files_state`, but `models/file.py` still declares them, so alembic
autogenerate reports `[('add_column','files.state'), ('add_index','ix_files_state')]`. This is the
model↔DB drift sentinel — it goes GREEN the moment 90-04 removes the model column/index. It is
**tracked, not a defect**. Everything else is green (ruff clean, mypy clean on 210 files, the other
13 migration-039 tests pass).

## Deferred to 90-04 (Task 3)

- Delete `FileState` enum + `state` mapped column + `Index('ix_files_state','state')` from
  `models/file.py`; drop the `FileState` re-export from `models/__init__.py`; tidy `config.py`.
- Remove the now-dead `shadow_compare` subsystem (service + CLI + its divergence/mutation tests
  that deliberately mutate `FileRecord.state`) — ~14 files.
- Migrate the ~90 dependent test files off `FileState`/`state=` seeds to the derived authority
  (central `make_file` lever + import swaps).
- Add the **D-08 mutation-tested** source-grep anti-drift guard (`tests/shared/test_no_filestate_guard.py`)
  forbidding `FileState` / `files.state` / `.state =` / `.values(state=` in `src/` — with a planted-match
  self-test that goes RED (handle multi-line `.values(\n state=...)` and `.values(**splat)` forms).
- Update `tests/buckets.json` for the new guard test.
- Flip `test_039_autogenerate_diff_is_empty_for_dropped_objects` to GREEN.

## Self-Check: PARTIAL (migration delivered; remainder tracked in 90-04)
