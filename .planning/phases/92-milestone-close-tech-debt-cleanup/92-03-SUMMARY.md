---
phase: 92-milestone-close-tech-debt-cleanup
plan: 03
subsystem: testing
tags: [pytest, pytest-asyncio, sqlalchemy, create_savepoint, asyncpg, test-hermeticity, monkeypatch, fan-out]

# Dependency graph
requires:
  - phase: 92-01
    provides: CLEAN-01/03 verified green under the OLD conftest harness before rewiring
  - phase: 92-02
    provides: get_stage_progress asyncio.gather fan-out + _STATS_FANOUT patchable seam + phaze.database.async_session deferred-import seam
provides:
  - Session-scoped test engine (schema created ONCE) + per-test create_savepoint outer-transaction session with teardown rollback (D-06/D-07)
  - Shared `verify` fixture (independent read on the same per-test connection) — the migration target for plan 92-04's 21 verify-session sites
  - Production get_stage_progress fan-out routed through the per-test connection under the DB fixture (monkeypatch async_session + Semaphore(1))
  - Mutation-safe hermeticity contract test proving rollback isolation + commit-visibility + production-fan-out visibility
affects: [92-04, 92-05]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "create_savepoint join mode: AsyncSession(bind=_db_connection, join_transaction_mode='create_savepoint') + per-test outer transaction rolled back at teardown (SQLAlchemy 2.0 built-in; no hand-rolled after_transaction_end listener)"
    - "session-scoped engine + NullPool: schema created once; NullPool opens a fresh connection per connect() so per-test function-loop connections never reuse a session-loop-bound pooled asyncpg connection"
    - "single-connection funnel: session, verify, get_session override, AND the routed production fan-out all bind to one _db_connection so in-test commits are visible cross-session yet fully rolled back"
    - "production-fan-out routing under test: monkeypatch phaze.database.async_session (deferred-import seam) to a _db_connection-bound sessionmaker + _STATS_FANOUT = Semaphore(1) to serialize onto the one shared asyncpg connection"

key-files:
  created:
    - tests/shared/test_conftest_hermeticity.py
  modified:
    - tests/conftest.py

key-decisions:
  - "Used RESEARCH option a (monkeypatch async_session + Semaphore(1)) — smallest blast radius, preserves seed-then-read tests unchanged"
  - "Added loop_scope='session' + poolclass=NullPool to the session-scoped async_engine so per-test function-loop connections stay loop-safe under pytest-asyncio 1.4 asyncio_mode=auto"
  - "Routing lives in a dedicated _route_stats_fanout fixture that `session` depends on — scoped to DB tests via the fixture chain, NOT autouse=True global (would force a DB for DB-free tests)"

patterns-established:
  - "Pattern: hermetic transactional test fixtures via create_savepoint + outer-transaction rollback"
  - "Pattern: route a production self-opened-session fan-out through the per-test connection by patching the deferred-import source attribute"

requirements-completed: [CLEAN-02]

# Metrics
duration: 40min
completed: 2026-07-13
---

# Phase 92 Plan 03: Hermetic conftest (create_savepoint) + production fan-out routing Summary

**Session-scoped engine + per-test `create_savepoint` outer-transaction session with a shared `verify` read fixture, plus a per-test monkeypatch that routes `get_stage_progress`'s production fan-out through the same connection — making the suite hermetic by construction without blinding the seed-then-read stats path.**

## Performance

- **Duration:** ~40 min
- **Completed:** 2026-07-13
- **Tasks:** 3
- **Files modified:** 2 (1 modified, 1 created)

## Accomplishments
- Replaced the function-scoped `create_all`/`drop_all`+committed-seed `async_engine` (the 83-01/83-03 flake root) with a session-scoped engine that builds the schema and seeds `test-fileserver` exactly once (D-06/D-07).
- `session` now runs each test inside a per-test outer transaction on one `_db_connection` with `AsyncSession(join_transaction_mode="create_savepoint")`; teardown `outer.rollback()` discards every in-test commit — no surviving `pk_agents` collision.
- Added a shared `verify` fixture (independent read session on the same connection) — the migration target for plan 92-04's 21 verify-session sites.
- Routed the production `get_stage_progress` fan-out through the per-test connection (monkeypatch `phaze.database.async_session` + `_STATS_FANOUT = Semaphore(1)`), turning the 8 RED `tests/analyze/core/test_stage_progress.py` tests GREEN and keeping the `shared`-bucket `test_pipeline` seed-then-`/pipeline/stats` cases reading correct non-zero counts.
- Added a mutation-safe hermeticity contract test (rollback isolation + commit-visibility + production-fan-out visibility) with both mutation break-recipes documented; verified recipe (b) actually flips assertion 3 to reading zero.

## Task Commits

Each task was committed atomically:

1. **Task 1: Rewire conftest to session-scoped engine + create_savepoint session + shared verify fixture** - `71f41ff9` (test)
2. **Task 2: Route get_stage_progress's production fan-out through the per-test connection** - `b4b7ce57` (test)
3. **Task 3: Add mutation-safe hermeticity contract test** - `db209e85` (test)

## Files Created/Modified
- `tests/conftest.py` - session-scoped `async_engine` (NullPool + loop_scope=session, create/seed once), new `_db_connection` fixture, `session` rewired to per-test outer-transaction + create_savepoint, new shared `verify` read fixture, new `_route_stats_fanout` fan-out-routing fixture, `_db_connection`/`verify` added to `DB_FIXTURES` auto-marking.
- `tests/shared/test_conftest_hermeticity.py` - 3 async tests: two order-independent probe tests (commit-visible-to-sibling + rollback isolation) reusing one agent id, and a seed-then-`get_stage_progress` test asserting `analyze.done == 1` (production-fan-out visibility). Module docstring carries both mutation recipes.

## Decisions Made
- **RESEARCH option a over option b:** monkeypatch the deferred-import source `phaze.database.async_session` to a `_db_connection`-bound `create_savepoint` sessionmaker and serialize with `Semaphore(1)`, rather than adding a new session-factory seam to `get_stage_progress` or migrating the seed-then-read tests to a durable seed. No `pipeline.py` edit needed; seed-then-read tests unchanged.
- **NullPool + `loop_scope="session"` on the session-scoped engine:** pytest-asyncio 1.4 (`asyncio_mode=auto`) runs each test in its own function-scoped loop while the engine lives in the session loop. NullPool opens a fresh asyncpg connection per `connect()` in the caller's own loop, so a per-test `_db_connection` never reuses a session-loop-bound pooled connection ("attached to a different loop").
- **Routing scoped via the fixture chain, not `autouse=True`:** `_route_stats_fanout` depends on `_db_connection`+`monkeypatch` and `session` depends on it, so only DB-fixture tests get the routing — DB-free tests are never forced to open a DB.

## Deviations from Plan

None - plan executed exactly as written. (The Task-1 `async_engine` fixture no longer disposes the production module engine on teardown; that dispose dance was a 92-02 stopgap the plan explicitly says 92-03 supersedes via the fan-out monkeypatch, so it was removed rather than deviated.)

## Issues Encountered
- **pytest-asyncio loop-scope hazard for a session-scoped async engine:** a session-scoped async fixture consumed by function-loop tests risks cross-loop asyncpg connection reuse. Resolved by `loop_scope="session"` on `async_engine` + `poolclass=NullPool` (fresh connection per `connect()`), which the plan's RESEARCH shape implied and which the passing DB buckets confirm.
- **ruff pre-commit auto-edits:** the initial Task-1 commit tripped ruff's unused-import removal (`import asyncio` was not yet used until Task 2) and ruff-format reflowed the multi-line `async_sessionmaker(...)` call in Task 2. Re-staged the hook-fixed files and re-committed; no logic change.

## Intermediate RED (expected, closed by later plans)
- The 13 test files / 21 sites that build INDEPENDENT verify sessions via `async_sessionmaker(async_engine)` (a DIFFERENT connection) go RED after this rewrite and are migrated in plan **92-04** (e.g. `tests/review/routers/test_duplicates.py::test_resolve_endpoint_commits_marker` — confirmed RED for exactly this reason, a non-`shared` bucket). This is the documented intermediate-red class, NOT a regression. The phase does not merge until the plan **92-05** full-suite gate is green (D-08).

## Verification
- `uv run pytest tests/shared -q -k "conftest or dsn"` → 5 passed (Task 1 infra gate).
- `uv run pytest tests/analyze/core/test_stage_progress.py -q` → **10 passed** (all 8 previously-RED seed-then-read + degrade + fake-session tests green).
- `just test-bucket shared` → **1084 passed** (1081 pre-existing + 3 new hermeticity tests), incl. the seed-then-`GET /pipeline/stats` cases reading correct non-zero counts; no `another operation is in progress` / `IllegalStateChangeError`.
- Mutation recipe (b) exercised live: disabling the `phaze.database.async_session` monkeypatch flips the contract test's assertion 3 to `assert 0 == 1` (RED), confirming the guard has teeth; conftest restored via `git checkout`.
- Integration/migration suites grep-verified as NOT consuming the rewired top-level fixtures — they build their own `create_async_engine` against `MIGRATIONS_TEST_DATABASE_URL` / a real broker.

## Next Phase Readiness
- `verify` fixture is in place for plan **92-04** to migrate the 21 independent verify-session call sites onto the shared per-test connection.
- Plan **92-05** full-suite gate remains the D-08 merge gate that must go green across all buckets.

---
*Phase: 92-milestone-close-tech-debt-cleanup*
*Completed: 2026-07-13*
