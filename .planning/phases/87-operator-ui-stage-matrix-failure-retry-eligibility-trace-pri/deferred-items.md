# Phase 87 — Deferred Items

Out-of-scope discoveries logged during execution (SCOPE BOUNDARY). Not fixed by the discovering plan.

## From Plan 02 (Wave 2)

- **`tests/integration/test_drain_double_dispatch.py` — 3 setup errors: `ModuleNotFoundError: No module named 'psycopg2'`.**
  - Discovered while running clause-consumer no-regression tests (Plan 02, Task 2).
  - Root cause: the SAQ Postgres-broker `scoped_runner` fixture resolves a sync `postgresql://` DSN
    (SQLAlchemy's psycopg2 dialect) during SETUP; `psycopg2` is not installed in this worktree venv.
    The failure occurs before any test body / any Plan-02 code runs.
  - NOT caused by Plan 02: changes were purely additive ORM `ColumnElement` builders + a new `Status`
    member (asyncpg path only). File last touched by Phase 83 (`6855cfe2`). The direct consumers of
    `eligible_clause` / `domain_completed_clause` (test_domain_completed_contract,
    test_awaiting_candidate_clause, test_pending_set_source_scan, test_pending_set_divergence) all pass.
  - Suggested fix (separate task): add `psycopg2-binary` to the dev/test dependency group, OR point the
    SAQ broker test fixture at the asyncpg driver, so the drain double-dispatch suite can set up locally.
