---
quick_id: 260520-bcl
title: Dedicated local integration-test database on a non-colliding port
date: 2026-05-20
status: complete
mode: quick
branch: quick/local-integration-test-db
---

# Quick Task 260520-bcl Summary

Gave phaze a self-contained, non-colliding local integration-test environment so the full suite runs
with one command (`just integration-test`) without disturbing other projects on port 5432 — while
keeping CI behavior byte-for-byte unchanged (test DB URL defaults stay at `localhost:5432`).

## What changed

| File | Change | Commit |
| ---- | ------ | ------ |
| `tests/conftest.py` | `import os`; `TEST_DATABASE_URL` now reads `os.environ.get("TEST_DATABASE_URL", <original literal>)` | `f0a9103` |
| `tests/test_migrations/conftest.py` | added `import os`; `MIGRATIONS_TEST_DATABASE_URL` now reads `os.environ.get("MIGRATIONS_TEST_DATABASE_URL", <original literal>)` (covers all `tests/test_migrations/test_*.py` via the existing `__all__` export) | `f0a9103` |
| `justfile` | new vars `test_db_port`/`test_db_container` + `test_redis_port`/`test_redis_container`; new `[group('test')]` recipes `test-db`, `test-db-down`, `integration-test` | `5034e4e` |
| `README.md` | new "🧪 Running integration tests locally" subsection under Development | `adc2970` |

## Commits (atomic, code only)

- `f0a9103` — `test: make integration-test database URLs environment-configurable`
- `5034e4e` — `chore(just): add ephemeral test-db / integration-test recipes on non-colliding ports`
- `adc2970` — `docs: document running integration tests locally against the ephemeral DB`

All commits ran the full pre-commit hook set (no `--no-verify`). SUMMARY.md / PLAN.md / STATE.md / ROADMAP.md
were intentionally NOT committed (orchestrator handles docs).

## Recipe behavior

- **`test-db`** — idempotently starts `postgres:18-alpine` (host `5433`→5432) and `redis:7-alpine`
  (host `6380`→6379) under fixed container names `phaze-test-db` / `phaze-test-redis`; `docker rm -f`
  any stale ones first; waits via `pg_isready` and `redis-cli ping` (bounded 30s retry loop, fails loud
  on timeout); creates `phaze_migrations_test` (`CREATE DATABASE ... OWNER phaze`, guarded so re-runs
  don't error).
- **`test-db-down`** — `docker rm -f` both containers (no error if absent).
- **`integration-test`** — `just test-db`, `export` `TEST_DATABASE_URL` /
  `MIGRATIONS_TEST_DATABASE_URL` (→ `localhost:5433`) and `PHAZE_REDIS_URL` (→ `localhost:6380`),
  `trap 'just test-db-down' EXIT` so services are always torn down, then `uv run pytest tests/ -q`.
- Multi-line recipes use `#!/usr/bin/env bash` + `set -euo pipefail`; each has a `[doc(...)]` annotation;
  ports overridable via `PHAZE_TEST_DB_PORT` / `PHAZE_TEST_REDIS_PORT`. **Never binds host port 5432.**

## Verification (actual captured results)

### `just integration-test`

```
1366 passed, 36 warnings in 149.98s (0:02:29)
🧹 Removed phaze-test-db + phaze-test-redis
```

- Full suite GREEN: **1366 passed, 0 failed**.
- Required tests confirmed passing (run against the ephemeral DB):
  `tests/test_health.py::test_health_endpoint_returns_ok` + all `tests/test_migrations/` →
  `22 passed in 8.70s`.
- Ephemeral Postgres came up on **5433**, Redis on **6380**; both torn down after the run.
- **No phaze Postgres bound host port 5432** during the run — `lsof -iTCP:5432` showed only an
  unrelated `ssh` process (PID 4472); the recipe publishes 5433/6380 only.
- After the run, `docker ps -a | grep -E 'phaze-test-db|phaze-test-redis'` returns nothing
  (containers gone).

### Quality gate

- `uv run ruff check .` → **All checks passed!**
- `uv run ruff format --check .` → **257 files already formatted**
- `uv run mypy .` → **Success: no issues found in 134 source files**
- `pre-commit run --all-files` → **all hooks passed** (ruff, ruff-format, bandit, mypy, shellcheck,
  yamllint, actionlint, jsonschema, EOF/whitespace, etc.)
- Rendered justfile recipe bodies pass `shellcheck --shell=bash --severity=warning` (clean).

### CI defaults unchanged (no env vars set)

```
TEST_DATABASE_URL = postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test
MIGRATIONS_TEST_DATABASE_URL = postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test
```

Both resolve byte-for-byte to the original hardcoded literals → CI (which provisions its own services
on 5432/6379) is unaffected.

## Deviations from plan

1. **Ephemeral Redis added alongside the ephemeral Postgres (Rule 2 / Rule 3).**
   The plan text described only an ephemeral Postgres, but the plan's *goal* and the task's verification
   requirement both demand the **full suite GREEN** with one command. The full suite includes
   Redis-backed (SAQ) tests in `tests/test_routers/test_agent_tracklists.py`,
   `tests/test_routers/test_execution_dispatch.py`, `tests/test_routers/test_agent_exec_batches.py`, and
   `tests/test_services/test_agent_task_router.py`. A Postgres-only environment left 7 failures + 39
   errors (`redis.exceptions.ConnectionError` at `localhost:6379`). CI itself provisions BOTH a
   `postgres:18-alpine` and a `redis:7-alpine` service, so mirroring CI required adding Redis. The
   recipe publishes Redis on a non-colliding **6380** (overridable via `PHAZE_TEST_REDIS_PORT`) and
   exports `PHAZE_REDIS_URL`, which the redis tests already honor (`os.environ.get("PHAZE_REDIS_URL",
   "redis://localhost:6379/0")`). With Redis added the suite went fully green (1366 passed).
   The `test-db` / `test-db-down` / `integration-test` recipe names from the plan were kept; they simply
   manage both services now.

2. **`tests/conftest.py` env override rendered on a single line (formatter-driven).**
   The plan showed a 3-line `os.environ.get(...)` call, but at the project's 150-char line length
   `ruff format` collapses it to one line (it fits in 113 chars). The collapsed form is what the
   pre-commit `ruff format` hook produces, so it was committed as a single line to keep hooks green.
   The longer `MIGRATIONS_TEST_DATABASE_URL` call (two explicit args) stays multi-line as in the plan.

3. **`docs/database.md` left untouched.** The plan said update it "only if it already documents test
   setup" — it does not (only mentions a `TagWriteStatus` enum), so only `README.md` was updated.

## Out of scope (left unchanged, per plan)

- `just test` / `just test-ci` defaults and the CI workflow — CI provisions its own services on 5432/6379.
- The dev `docker-compose.yml` Postgres service.
- Pre-existing pytest deprecation/runtime warnings (e.g. SAQ `asyncio.iscoroutinefunction`
  deprecation, `tracklist.py` mock RuntimeWarnings) — unrelated to this task.

## Self-Check: PASSED

- `tests/conftest.py`, `tests/test_migrations/conftest.py`, `justfile`, `README.md` — all present and modified.
- Commits `f0a9103`, `5034e4e`, `adc2970` all present in `git log` on `quick/local-integration-test-db`.
- No ephemeral containers remain; port 5432 untouched by phaze.
