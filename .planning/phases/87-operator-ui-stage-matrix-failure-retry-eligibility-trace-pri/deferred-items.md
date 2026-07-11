# Phase 87 — Deferred Items

Out-of-scope discoveries logged during execution (SCOPE BOUNDARY). Not fixed by the discovering plan.

## From Plan 02 (Wave 2)

- **RESOLVED (orchestrator, mid-phase): `tests/integration/test_drain_double_dispatch.py` — 3 setup errors: `ModuleNotFoundError: No module named 'psycopg2'`.**
  - Discovered while running clause-consumer no-regression tests (Plan 02, Task 2).
  - Corrected root cause: NOT a SAQ `scoped_runner` fixture. The three tests consume the shared
    `async_engine` fixture (`tests/conftest.py`), which feeds `TEST_DATABASE_URL` straight to
    `create_async_engine`. When an operator exports a **bare** `postgresql://` DSN (the natural form —
    it matches `PHAZE_QUEUE_URL`), SQLAlchemy resolves its default **psycopg2** sync dialect, which the
    async-only stack does not install → every DB-fixture test dies at setup. Reproduced deterministically
    with `TEST_DATABASE_URL=postgresql://…`.
  - NOT caused by Plan 02: changes were purely additive ORM `ColumnElement` builders + a new `Status`
    member (asyncpg path only). Latent footgun in shared test infra.
  - Fix applied: `_coerce_async_dsn()` in `tests/conftest.py` normalizes bare `postgresql://`,
    `postgresql+psycopg2://`, and `postgresql+psycopg://` DSNs to `postgresql+asyncpg://` before the
    engine is built (only the leading driver token is rewritten). Rejected the "add psycopg2-binary"
    option — it violates the project's async-only driver rule (CLAUDE.md: psycopg2 is a sync driver to
    avoid). Regression guard: `tests/shared/test_conftest_dsn_coercion.py`. Verified the drain suite now
    passes under a bare `postgresql://` DSN.
