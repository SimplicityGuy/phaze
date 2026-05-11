---
phase: 24-schema-foundation-agent-registry
plan: 01
subsystem: testing
tags: [alembic, pytest, postgres, async, migrations, fixtures]

# Dependency graph
requires:
  - phase: 23-v3-polish-wiring-fixes
    provides: alembic revision 011 (tag_write_log) is the current migration head; tests/conftest.py async_engine fixture pattern
provides:
  - tests/test_migrations/ pytest package marker
  - tests/test_migrations/conftest.py exporting migrated_engine fixture, upgrade_to/downgrade_to helpers, _build_alembic_config builder, MIGRATIONS_TEST_DATABASE_URL constant, ALEMBIC_INI_PATH constant
  - _patched_settings_database_url contextmanager that survives alembic/env.py's sqlalchemy.url override
affects:
  - 24-02 (Agent model + ScanStatus.LIVE + agent_id columns)
  - 24-03 (migration 012 + 13 integration tests use migrated_engine)
  - 24-04 (migration 013 + downgrade tests use upgrade_to / downgrade_to to step revisions)
  - 24-05 (ingestion service composite conflict target tests)

# Tech tracking
tech-stack:
  added: []   # No new dependencies; uses already-installed alembic + pytest-asyncio
  patterns:
    - "alembic-driven test DB fixture (vs. Base.metadata.create_all) for migration validation"
    - "contextmanager-scoped monkey-patching of phaze.config.settings.database_url to redirect alembic/env.py"
    - "Step-helper wrappers (upgrade_to / downgrade_to) so future tests can assert on intermediate revisions without rebuilding cfg"

key-files:
  created:
    - tests/test_migrations/__init__.py
    - tests/test_migrations/conftest.py
  modified: []

key-decisions:
  - "Dedicated phaze_migrations_test DB rather than schema-isolation: matches the per-DB convention of tests/conftest.py (phaze_test); no shared mutable state with the parent async_engine fixture."
  - "Added _patched_settings_database_url contextmanager (NOT in PATTERNS.md skeleton) because alembic/env.py:21 unconditionally overrides sqlalchemy.url with settings.database_url -- without the patch, the fixture would silently run upgrades against the production-targeted DB. Smallest-blast-radius fix: mutate the in-memory singleton attribute only for the upgrade/downgrade duration, restore in finally."
  - "upgrade_to / downgrade_to are thin wrappers that bundle the settings patch with the alembic command call, so per-revision step tests in Plans 03/04 do not need to remember the workaround."
  - "Defined __all__ to make exports explicit for the success_criteria's enumeration of public names."

patterns-established:
  - "tests/test_migrations/ subpackage as the canonical location for alembic-driven integration tests"
  - "Use `MIGRATIONS_TEST_DATABASE_URL` (phaze_migrations_test DB) to isolate migration tests from model-driven tests"
  - "Always wrap alembic.command.upgrade / .downgrade in _patched_settings_database_url (or use upgrade_to/downgrade_to) when targeting the test DB -- direct command.upgrade(cfg, rev) calls will silently hit settings.database_url instead"

requirements-completed: [DATA-04]

# Metrics
duration: 10min
completed: 2026-05-11
---

# Phase 24 Plan 01: Alembic-Driven Test Substrate Summary

**New tests/test_migrations/ subpackage with migrated_engine fixture + upgrade_to/downgrade_to step helpers that actually run `alembic.command.upgrade()` against a dedicated phaze_migrations_test DB, enabling Phase 24 Waves 1-3 to validate migration files (not just ORM models).**

## Performance

- **Duration:** ~10 min
- **Started:** 2026-05-11T19:46Z
- **Completed:** 2026-05-11T19:56Z
- **Tasks:** 2
- **Files created:** 2
- **Files modified:** 0

## Accomplishments

- Empty `tests/test_migrations/__init__.py` so pytest discovers the new test package.
- `tests/test_migrations/conftest.py` exporting:
  - `migrated_engine` async fixture (upgrades to head, yields engine, downgrades to base on teardown).
  - `_build_alembic_config(database_url) -> Config` builder.
  - `upgrade_to(cfg, revision)` / `downgrade_to(cfg, revision)` step helpers.
  - `MIGRATIONS_TEST_DATABASE_URL` + `ALEMBIC_INI_PATH` module constants.
- `_patched_settings_database_url` contextmanager that mutates the in-memory `phaze.config.settings.database_url` for the duration of an alembic call, working around `alembic/env.py`'s unconditional `sqlalchemy.url` override.
- All acceptance criteria green: pytest collects 0 tests (no collection errors), mypy clean, pre-commit (ruff + ruff-format + bandit + mypy) all pass.

## Task Commits

Each task was committed atomically:

1. **Task 1: Create test_migrations package marker** - `c655d54` (test)
2. **Task 2: Create alembic-driven test DB fixture and step helpers** - `53f49e2` (test)

## Files Created/Modified

- `tests/test_migrations/__init__.py` (created, 0 bytes) - pytest package marker; mirrors `tests/test_models/__init__.py` shape.
- `tests/test_migrations/conftest.py` (created, 109 lines) - alembic-driven fixture, step helpers, settings-patch contextmanager, module constants, `__all__` export list.

## Decisions Made

- **Dedicated DB over schema isolation:** Used `postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_migrations_test` (a separate DB) rather than carving a schema inside `phaze_test`, matching the per-DB convention already used by the parent conftest. Documented as the standing operator pre-condition in the module docstring.
- **`__all__` listed explicitly:** Even though all names are top-level functions/fixtures (so `from ... import *` would pick them up regardless), an explicit `__all__` satisfies the plan's success-criteria enumeration (`migrated_engine`, `upgrade_to`, `downgrade_to`, `_build_alembic_config`, `MIGRATIONS_TEST_DATABASE_URL`, `ALEMBIC_INI_PATH`) and makes the export surface obvious to readers of Plans 03/04.
- **Wrapper functions rather than re-exporting alembic.command:** `upgrade_to` / `downgrade_to` thinly wrap `command.upgrade` / `command.downgrade` but bundle the `settings.database_url` patch. Tests in 03/04 will use the wrappers; if a test ever calls `command.upgrade(cfg, rev)` directly, it would silently target the production DB. The wrapper closes that footgun.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added `_patched_settings_database_url` contextmanager + wrapper helpers**

- **Found during:** Task 2 (creating the fixture). On reading `alembic/env.py:21`, line 21 calls `config.set_main_option("sqlalchemy.url", settings.database_url)` *after* the cfg passed by `command.upgrade(cfg, rev)` is obtained. This unconditionally overwrites the `sqlalchemy.url` that `_build_alembic_config` set, so the fixture as spec'd in PATTERNS.md would silently migrate the production-pointed DB (`settings.database_url` default is `postgresql+asyncpg://phaze:phaze@postgres:5432/phaze`).
- **Issue:** PLAN.md `<action>` step 4 specifies `cfg.set_main_option("sqlalchemy.url", database_url)` as the URL-redirection mechanism, which is the Alembic-canonical pattern -- but the project's `alembic/env.py` defeats it. Without a fix, `migrated_engine` would have torn through whatever Postgres `settings.database_url` resolved to at test time. Cannot complete the plan's "alembic-driven test DB fixture" objective without addressing this.
- **Fix:** Added `_patched_settings_database_url(database_url)` contextmanager that mutates `phaze.config.settings.database_url` (a pydantic-settings singleton attribute) to `database_url` for the body, restores in `finally`. `upgrade_to` / `downgrade_to` (and therefore `migrated_engine`) wrap their `command.upgrade` / `command.downgrade` calls in this contextmanager. The cfg's `set_main_option` is still called so consumers reading the cfg directly see the correct URL; the patch is the belt to the cfg's suspenders.
- **Files modified:** `tests/test_migrations/conftest.py` (the new file -- no existing files touched).
- **Verification:** `uv run python -c "from tests.test_migrations.conftest import _build_alembic_config, MIGRATIONS_TEST_DATABASE_URL; cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL); print(cfg.get_main_option('sqlalchemy.url'))"` prints the test URL. End-to-end migration run is not executed in this plan (Plan 03 will be the first to assert `migrated_engine` actually runs against the test DB) but the URL-redirection contract is in place.
- **Committed in:** `53f49e2` (Task 2 commit).
- **Rationale for Rule 3 (not Rule 4):** No architectural change. Production `alembic/env.py` is untouched. Only test-side code adapts to env.py's existing behavior. If the project later decides to make env.py honor the cfg URL (architectural decision), this workaround becomes a no-op and can be deleted without behavior changes.

**2. [Style] Ruff reordered `from alembic import command` into the first-party section**

- **Found during:** Pre-commit on Task 2. Ruff's isort grouping treats `alembic` as a first-party section (last group), matching existing migrations (`alembic/versions/011_*.py` uses the same ordering: `from collections.abc ...` → `import sqlalchemy as sa` → `from alembic import op`).
- **Issue:** My initial import block grouped `from alembic.config import Config` and `from alembic import command` together at the top of the third-party block; ruff split them, hoisting `Config` into the third-party group and demoting `command` next to `phaze.config`.
- **Fix:** Accepted ruff's reordering -- it matches the project-wide convention established in existing migrations. No semantic change.
- **Files modified:** `tests/test_migrations/conftest.py`.
- **Verification:** `pre-commit run --files tests/test_migrations/conftest.py` passes cleanly after ruff's edit.
- **Committed in:** `53f49e2` (part of Task 2 commit).

---

**Total deviations:** 2 (1 blocking auto-fix, 1 style auto-fix).
**Impact on plan:** Deviation 1 closes a silent-correctness footgun that PATTERNS.md / PLAN.md did not anticipate; without it Plans 03/04 would have appeared green while never actually migrating the test DB. Deviation 2 is a routine import-ordering normalization. No scope creep; both fixes are necessary for the fixture to behave as the plan's `<objective>` describes.

## Issues Encountered

- The plan and PATTERNS.md skeleton both prescribed `cfg.set_main_option("sqlalchemy.url", database_url)` as the URL-redirection mechanism. That pattern is alembic-canonical but breaks against this project's `env.py` (line 21 hard-overrides). Caught by careful pre-implementation reading of `env.py` (which the plan's `<read_first>` explicitly required). See Deviation 1.
- mypy initially flagged `# type: ignore[no-untyped-def]` on `migrated_engine` as unused (because the fixture has an explicit `-> AsyncGenerator` return type, unlike the parent conftest's bare `async def async_engine():`). Removed the comment; mypy now clean.

## Operator Pre-condition

The test database `phaze_migrations_test` must exist on `localhost:5432` with the same credentials as `phaze_test`:

```sql
CREATE DATABASE phaze_migrations_test OWNER phaze;
```

This is documented in the conftest module docstring (lines 3-6). It matches the standing convention that operators provision `phaze_test` before integration runs (`tests/conftest.py:15`). No automation in this plan -- Plan 03/04 tests will fail with a Postgres "database does not exist" error if the operator skips this step.

## User Setup Required

None - no external service configuration required. The new fixture uses an additional local Postgres database (`phaze_migrations_test`) that the operator creates once, same pattern as the existing `phaze_test` database.

## Next Phase Readiness

- **Wave 1 (Plan 24-02)** can proceed immediately: it creates the `Agent` model, `ScanStatus.LIVE` value, and `agent_id` columns -- it does not need `migrated_engine` for its unit-level model tests (which use the parent `async_engine` fixture).
- **Wave 2 (Plan 24-03)** is the first consumer of `migrated_engine`: it writes 13 integration tests asserting migration 012 behavior. The fixture / step helpers are ready.
- **Wave 3 (Plans 24-04 / 24-05)** use `upgrade_to` and `downgrade_to` to step between 011/012/013/head/base for the constraint-tightening migration and the [BLOCKING] roundtrip smoke. The wrapper API is in place.

## Self-Check: PASSED

- File `tests/test_migrations/__init__.py` exists at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a25a16e446910803d/tests/test_migrations/__init__.py` (0 bytes).
- File `tests/test_migrations/conftest.py` exists at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a25a16e446910803d/tests/test_migrations/conftest.py` (109 lines).
- Commit `c655d54` exists in git log (`test(24-01): add tests/test_migrations package marker`).
- Commit `53f49e2` exists in git log (`test(24-01): add alembic-driven test DB fixture and step helpers`).
- `uv run pytest tests/test_migrations/ --collect-only` exits 0.
- `uv run mypy tests/test_migrations/conftest.py` returns "Success: no issues found".
- `pre-commit run --files tests/test_migrations/__init__.py tests/test_migrations/conftest.py` passes all hooks.

---
*Phase: 24-schema-foundation-agent-registry*
*Plan: 01*
*Completed: 2026-05-11*
