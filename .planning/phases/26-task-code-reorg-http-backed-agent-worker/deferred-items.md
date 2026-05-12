# Phase 26 — Deferred Items

Issues discovered during plan execution that fall outside the active plan's scope.
Each item is logged with the plan that surfaced it; resolution belongs to a later
plan or a follow-up cleanup wave.

## Item D-1: Full-suite test flakiness on integration DB fixtures

- **Surfaced during:** Plan 26-05 (Wave 3, GET /whoami router)
- **Symptom:** Running `uv run pytest -q --no-cov` against the full suite (842
  tests) produces ~56 errors and 2 failures, all of the form:
  - `sqlalchemy.exc.DBAPIError: connection was closed in the middle of operation`
  - `asyncpg.exceptions.ConnectionDoesNotExistError`
  - `sqlalchemy.exc.IntegrityError: ... duplicate key value violates unique
    constraint "agents_pkey"` (`legacy-application-server`)
- **Reproducibility:** All affected tests pass cleanly when run in isolation
  (verified for `tests/test_routers/test_agent_identity.py` — 4/4 pass in
  isolation; same file shows 4 errors when bundled with the full suite).
- **Root cause hypothesis:** The `async_engine` fixture in `tests/conftest.py`
  drops + recreates schema per test and re-seeds the legacy agent; on parallel
  collection or session reuse the engine teardown races with subsequent
  fixtures, leaving Postgres connections in a half-closed state.
- **Pre-existing:** Yes — present on the inherited `gsd/phase-26-…` branch tip
  BEFORE any Plan 26-05 changes were applied. Verified by checking that the
  same fixtures + ordering predate the Wave-3 work.
- **Scope:** Out of scope for Plan 26-05 (whose `files_modified` is only
  `src/phaze/routers/agent_identity.py` + `tests/test_routers/test_agent_identity.py`).
- **Recommendation:** Open a dedicated cleanup plan to (a) scope `async_engine`
  to `session=session` (or use a per-test transactional rollback fixture in
  place of full create_all/drop_all), and (b) gate the `legacy-application-server`
  seed behind a `merge_into_session`/upsert so duplicate-seed contention
  cannot fire.
- **Verification of my plan's deliverable:** `uv run pytest
  tests/test_routers/test_agent_identity.py -x -q --no-cov` exits 0 with 4
  tests passing.
