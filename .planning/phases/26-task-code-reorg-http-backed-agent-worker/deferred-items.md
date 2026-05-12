# Phase 26 — Deferred Items

Issues discovered during plan execution that fall outside the active plan's scope.
Each item is logged with the plan that surfaced it; resolution belongs to a later
plan or a follow-up cleanup wave.

## Item D-1: Full-suite test flakiness on integration DB fixtures

- **Surfaced during:** Plan 26-05 (Wave 3, GET /whoami router); reconfirmed by 26-06, 26-07, 26-08
- **Symptom:** Running `uv run pytest -q --no-cov` against the full suite (842
  tests) produces ~56-131 errors and 2 failures, all of the form:
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
  fixtures, leaving Postgres connections in a half-closed state. The legacy
  agent seed lacks `ON CONFLICT DO NOTHING` and collides on shared-DB re-use.
- **Pre-existing:** Yes — present on the inherited `gsd/phase-26-…` branch tip
  BEFORE any Wave 3 changes were applied. Verified by checking that the
  same fixtures + ordering predate the Wave-3 work.
- **Scope:** Out of scope for the Wave 3 router plans (each plan's
  `files_modified` is router + its tests only).
- **Recommendation:** Open a dedicated cleanup plan to (a) scope `async_engine`
  to `session=session` (or use a per-test transactional rollback fixture in
  place of full create_all/drop_all), and (b) gate the `legacy-application-server`
  seed behind a `merge_into_session`/upsert so duplicate-seed contention
  cannot fire.
- **Verification of plan deliverables:** Each Wave 3 plan's tests exit 0
  when run in isolation (e.g. `uv run pytest
  tests/test_routers/test_agent_identity.py -x -q --no-cov`).
