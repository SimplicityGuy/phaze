---
quick_id: 260520-bcl
title: Dedicated local integration-test database on a non-colliding port
date: 2026-05-20
status: ready
mode: quick
---

# Quick Task 260520-bcl: Dedicated local integration-test database

## Problem

`tests/conftest.py:18` hardcodes `TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test"`
and `tests/test_migrations/conftest.py:34` hardcodes
`MIGRATIONS_TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test"`,
with no environment override. Integration tests therefore only work when a phaze Postgres is reachable
at `localhost:5432`. On a developer machine where port 5432 is taken by another project's Postgres,
the suite fails with `asyncpg.exceptions.InvalidPasswordError`. CI provisions its own
`postgres:18-alpine` service (user/pass `phaze`/`phaze`, DB `phaze_test`, plus a manually-created
`phaze_migrations_test`) on 5432 and relies on the current hardcoded defaults.

## Goal

Give phaze a self-contained, non-colliding local integration-test database so a developer can run the
full suite with one command, without disturbing other projects on port 5432 — while keeping CI behavior
byte-for-byte unchanged (defaults stay at 5432).

## Constraints (from CLAUDE.md / project memory)

- Python 3.14 + `uv` only — always `uv run`.
- 150-char line length; `uv run ruff check .`, `uv run ruff format .`, and `uv run mypy .` must pass.
- Pre-commit must pass; **never** use `--no-verify`.
- GitHub Actions / automation delegate to `just` recipes (don't inline shell that belongs in justfile).
- Keep docs (README) current alongside code.
- Commit atomically per task.

## Tasks

### Task 1 — Make both test DB URLs environment-configurable

**Files:** `tests/conftest.py`, `tests/test_migrations/conftest.py`

- In `tests/conftest.py`: `import os` (alphabetical, top of stdlib imports) and change line 18 to:
  ```python
  TEST_DATABASE_URL = os.environ.get(
      "TEST_DATABASE_URL", "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test"
  )
  ```
- In `tests/test_migrations/conftest.py`: ensure `import os` is present and change line 34 to:
  ```python
  MIGRATIONS_TEST_DATABASE_URL = os.environ.get(
      "MIGRATIONS_TEST_DATABASE_URL",
      "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test",
  )
  ```
  The migration test modules import this symbol from the conftest (`__all__` already exports it), so a
  single edit covers all of `tests/test_migrations/test_*.py`.

**Verify:** Defaults are unchanged (CI keeps working). `uv run ruff check tests/` and `uv run mypy .` pass.

**Done:** Both URLs honor an env override; with no env set, the resolved value equals the previous
hardcoded literal.

### Task 2 — Add justfile recipes for an ephemeral test Postgres

**Files:** `justfile`

Add to the `[group('test')]` (or a new `[group('db')]`-adjacent) section. Mirror CI's setup
(`postgres:18-alpine`, user/pass `phaze`/`phaze`, DB `phaze_test`, plus `phaze_migrations_test`), but
publish on a **non-colliding** host port. Make the port overridable.

- A justfile variable: `test_db_port := env_var_or_default("PHAZE_TEST_DB_PORT", "5433")` and a fixed
  container name (e.g. `phaze-test-db`).
- **`test-db`** — start the container idempotently (`docker rm -f` any stale one first, or skip if
  already running), wait until `pg_isready` succeeds (bounded retry loop, fail loud on timeout), then
  create `phaze_migrations_test` (`CREATE DATABASE ... OWNER phaze`, guarded so re-runs don't error).
- **`test-db-down`** — `docker rm -f` the container (no error if absent).
- **`integration-test`** — bash recipe: run `just test-db`, `export` both
  `TEST_DATABASE_URL` and `MIGRATIONS_TEST_DATABASE_URL` pointed at `localhost:{{test_db_port}}`,
  `trap 'just test-db-down' EXIT` so the DB is always torn down, then `uv run pytest tests/ -q`.

Use `#!/usr/bin/env bash` + `set -euo pipefail` for multi-line recipes; keep bash shellcheck-clean.
Each recipe gets a `[doc(...)]` annotation consistent with the existing style.

**Verify:** `just --list` shows the new recipes; `just integration-test` brings up the DB, runs the
full suite green (including `tests/test_health.py::test_health_endpoint_returns_ok` and
`tests/test_migrations/`), and tears the DB down. The recipe must not touch port 5432.

**Done:** A clean `git`-state machine with Docker can run the entire suite with `just integration-test`
and nothing binds 5432.

### Task 3 — Document local integration testing

**Files:** `README.md` (and `docs/database.md` only if it already documents test setup)

- Under the existing Testing section (~line 178, near `just test` / `just test-cov`), add a short
  "Running integration tests locally" subsection: `just integration-test` for a one-shot self-contained
  run; `just test-db` / `just test-db-down` for iterative work; note the ephemeral DB listens on
  **5433** (override with `PHAZE_TEST_DB_PORT`) to avoid colliding with the dev DB on 5432; note the
  `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` env overrides.
- Keep README badges/line-style conventions intact (no new badges, no reflowing unrelated lines).

**Verify:** Docs accurately describe the new recipes and env vars; pre-commit (markdown/EOF/whitespace)
passes.

**Done:** A new contributor can run integration tests locally from the README alone.

## Out of scope

- Changing `just test` / `just test-ci` defaults or CI workflow (CI already provisions its own DB on 5432).
- Touching the dev `docker-compose.yml` Postgres service.

## Final verification (whole task)

1. `just integration-test` → full suite green, DB on 5433, torn down afterward.
2. `uv run ruff check .` && `uv run ruff format --check .` && `uv run mypy .` → clean.
3. `pre-commit run --all-files` → all hooks pass (no `--no-verify`).
4. Confirm CI defaults unchanged: with no env vars set, `TEST_DATABASE_URL` resolves to the original
   `...@localhost:5432/phaze_test` literal.
