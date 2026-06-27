---
phase: 48-compute-agent-type
plan: 01
subsystem: data-model
tags: [schema, alembic, agents, compute-agent, kind, check-constraint]
requires: []
provides:
  - "agents.kind String(16) NOT NULL DEFAULT 'fileserver' column"
  - "ck_agents_kind_enum CHECK restricting kind to {'fileserver','compute'}"
  - "Alembic migration 024 (single head off 023, up/down round-trip)"
affects:
  - src/phaze/models/agent.py
  - alembic/versions/024_add_agents_kind.py
tech-stack:
  added: []
  patterns:
    - "SQLAlchemy ck_%(table_name)s_%(constraint_name)s naming convention auto-prefix"
    - "additive NOT NULL column via server_default backfill (no separate UPDATE)"
key-files:
  created:
    - alembic/versions/024_add_agents_kind.py
    - tests/test_migrations/test_024.py
  modified:
    - src/phaze/models/agent.py
    - tests/test_models/test_agent.py
decisions:
  - "kind lives on the agents row (DB-authoritative identity), not config alone — admin page reads it, CLI sets it"
  - "server_default='fileserver' backfills every existing row including legacy-application-server; no data migration step"
  - "downgrade passes bare 'kind_enum' to drop_constraint so the naming convention does not double-prefix to ck_agents_ck_agents_kind_enum"
metrics:
  duration: ~25min
  tasks: 2
  files: 4
  tests_added: 6
  completed: 2026-06-25
---

# Phase 48 Plan 01: Agent kind column + migration 024 Summary

Added the durable `kind` capability marker to the `Agent` model (`String(16)` NOT NULL, server_default `'fileserver'`) plus a `ck_agents_kind_enum` CHECK restricting values to `{'fileserver','compute'}`, and shipped additive Alembic migration 024 that adds the column to the live `agents` table, backfills existing rows via the server default, and chains cleanly off 023 as a single head.

## What Was Built

**Task 1 — Agent model (TDD):**
- `src/phaze/models/agent.py`: new `kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'fileserver'"))` placed among the scalar columns; appended `CheckConstraint("kind IN ('fileserver', 'compute')", name="kind_enum")` to the existing `__table_args__` (the `id_charset` constraint is untouched). Reused the already-imported `String`/`text` — no new imports.
- `tests/test_models/test_agent.py`: extended the required-columns set to include `kind`; added `test_kind_defaults_fileserver`, `test_kind_column_not_null_string16`, `test_kind_charset_constraint_declared` (asserts the auto-prefixed `ck_agents_kind_enum`).

**Task 2 — Migration 024 (TDD):**
- `alembic/versions/024_add_agents_kind.py`: `revision="024"`, `down_revision="023"`. `upgrade()` = `op.add_column("agents", sa.Column("kind", sa.String(16), nullable=False, server_default="fileserver"))` then `op.create_check_constraint("kind_enum", "agents", "kind IN ('fileserver', 'compute')")`. `downgrade()` drops the constraint (bare name `kind_enum`) then the column.
- `tests/test_migrations/test_024.py`: mirrors `test_023.py` (importlib module loader, bare-number revision assertions, saq_jobs non-reference guard, async round-trip via conftest helpers). Proves: upgrade adds NOT NULL kind defaulting `'fileserver'`; the migration-012-seeded `legacy-application-server` row backfills to `'fileserver'`; a `kind='compute'` insert is accepted; `kind='bogus'` raises `IntegrityError` (CHECK reject); downgrade to 023 drops the column.

## Verification

- `uv run pytest tests/test_models/test_agent.py tests/test_migrations/test_024.py` — 15 passed (12 model + 3 migration, including the live Postgres round-trip via the ephemeral `just test-db` container on port 5433).
- `uv run alembic heads` — single head `024`, no multiple-head error.
- `uv run ruff check` + `uv run mypy` on `src/phaze/models/agent.py` and `alembic/versions/024_add_agents_kind.py` — clean.

## Threat Model Coverage

- **T-48-02 (Tampering — `agents.kind`)** mitigated: `ck_agents_kind_enum` enforces the enum at the database (innermost of the planned 3-layer defense). Migration test asserts `kind='bogus'` is rejected.
- **T-48-SC (dependency install)**: no packages installed — only already-pinned SQLAlchemy/Alembic used. No supply-chain surface.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] downgrade `drop_constraint` double-prefixed the constraint name**
- **Found during:** Task 2 (round-trip test failed on downgrade)
- **Issue:** The `ck_%(table_name)s_%(constraint_name)s` naming convention is applied by Alembic to BOTH create and drop. The plan's literal instruction `op.drop_constraint("ck_agents_kind_enum", ...)` made the convention re-prefix it to `ck_agents_ck_agents_kind_enum`, which does not exist → `UndefinedObjectError` on downgrade.
- **Fix:** Pass the bare name `op.drop_constraint("kind_enum", "agents", type_="check")` so the convention resolves to the live `ck_agents_kind_enum`. Documented inline in the migration docstring.
- **Files modified:** alembic/versions/024_add_agents_kind.py
- **Commit:** 9808d5e

**2. [Rule 3 - Blocking] saq_jobs non-reference guard tripped on the docstring**
- **Found during:** Task 2 (static guard test)
- **Issue:** The mirrored `test_migration_never_references_saq_jobs` guard excludes only lines containing "never reference"; my CRITICAL banner split the `saq_jobs` mention onto a separate line without that phrase.
- **Fix:** Re-flowed the banner so the `saq_jobs` line contains "It must never reference" (matching the 023 pattern). No behavior change.
- **Files modified:** alembic/versions/024_add_agents_kind.py
- **Commit:** 9808d5e

Note: the test DB connects on port **5433** (the `just test-db` ephemeral container), not the conftest default 5432; the round-trip test was run with `MIGRATIONS_TEST_DATABASE_URL` pointed at 5433 per the project's integration-test convention.

## Commits

- 58974e1 — test(48-01): failing model tests (RED)
- db6b9eb — feat(48-01): Agent.kind column + CHECK (GREEN)
- f5e34b1 — test(48-01): failing migration 024 round-trip test (RED)
- 9808d5e — feat(48-01): migration 024 column + CHECK + backfill (GREEN)

## Known Stubs

None.

## Self-Check

- Files exist: src/phaze/models/agent.py, alembic/versions/024_add_agents_kind.py, tests/test_models/test_agent.py, tests/test_migrations/test_024.py — verified below.
- Commits exist: 58974e1, db6b9eb, f5e34b1, 9808d5e — verified below.

## Self-Check: PASSED

All 4 created/modified files present; all 4 task commits present in git history.
