# Phase 26 — Deferred Items (out-of-scope discoveries during execution)

Issues discovered during Phase 26 plan execution that fall outside the active
plan's scope. Each item is logged with the plan that surfaced it; resolution
belongs to a later plan or a follow-up cleanup wave.

## D-1: Full-suite test flakiness on integration DB fixtures

- **Surfaced during:** Plans 26-05, 26-06, 26-07 (Wave 3 routers)
- **Symptom:** Running `uv run pytest -q --no-cov` against the full suite
  produces 56-131 errors and 2 failures of the form:
  - `sqlalchemy.exc.DBAPIError: connection was closed in the middle of operation`
  - `asyncpg.exceptions.ConnectionDoesNotExistError`
  - `sqlalchemy.exc.IntegrityError: duplicate key value violates unique
    constraint "agents_pkey"` (`legacy-application-server`)
- **Reproducibility:** Affected tests pass cleanly when run in isolation.
- **Root cause hypothesis:** The `async_engine` fixture in `tests/conftest.py`
  drops + recreates schema per test and re-seeds the legacy agent without
  `ON CONFLICT DO NOTHING`. On parallel collection or session reuse, engine
  teardown races leave Postgres connections half-closed and seeds collide.
- **Pre-existing:** Yes — present on the inherited `gsd/phase-26-…` branch
  before any Wave 3 changes.
- **Recommendation:** Dedicated cleanup plan: (a) scope `async_engine` to
  `scope="session"` or use per-test transactional rollback fixture in place
  of full create_all/drop_all; (b) gate the `legacy-application-server` seed
  behind an upsert (`INSERT ... ON CONFLICT DO NOTHING`).

## D-2: `test_tags.py` UniqueViolation race in fixture setup

- **Surfaced during:** Plan 26-08 (full test-suite run)
- **Reproducer:** `uv run pytest tests/test_routers/test_tags.py -x --no-cov`
- **Error:** `asyncpg.exceptions.UniqueViolationError: duplicate key value
  violates unique constraint "pg_type_typname_nsp_index"`
- **Root cause hypothesis:** `Base.metadata.create_all` can race against
  leftover enum types from a previous interrupted run. Independent of
  Phase 26 router work — reproduces on a clean tree.
- **Suggested resolution:** Investigate whether `Base.metadata.drop_all`
  properly drops custom enum types in `async_engine` teardown; consider
  running each test module against a uniquely-named test schema; or wrap
  `create_all` in a `DROP TYPE IF EXISTS … CASCADE` preamble.
