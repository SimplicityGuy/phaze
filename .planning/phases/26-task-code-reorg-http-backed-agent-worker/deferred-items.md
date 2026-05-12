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

## D-3: Live-Redis integration tests fail without a running Redis instance

- **Surfaced during:** Plan 26-13 (full test-suite run after legacy deletion)
- **Affected tests:**
  - `tests/test_services/test_agent_task_router.py` (4 tests; added by Plan 26-04)
  - `tests/test_routers/test_agent_tracklists.py` (7 tests; added by Plan 26-07)
- **Error:** `redis.exceptions.ConnectionError: ... Connect call failed
  ('127.0.0.1', 6379)`
- **Pre-existing:** Yes — these tests have required a live Redis since they
  were introduced. Independent of Plan 26-13's deletions.
- **Root cause:** Tests construct real SAQ `Queue.from_url(redis://localhost:6379/0)`
  and exercise enqueue paths against a live broker; the worktree CI sandbox has
  no Redis sidecar.
- **Reproducer in worktree:**
  ```bash
  uv run pytest tests/test_services/test_agent_task_router.py -x --no-cov
  ```
- **Suggested resolution:** Either gate these tests behind a `@pytest.mark.integration`
  marker and skip when `REDIS_URL` is unset, or stand up a Redis sidecar in the
  CI matrix. Pure unit-test variants (mock `Queue.enqueue`) would also satisfy
  the contract; the live-Redis variants are belt-and-suspenders.
