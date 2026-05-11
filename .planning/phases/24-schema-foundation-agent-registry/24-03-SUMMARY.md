---
phase: 24-schema-foundation-agent-registry
plan: 03
subsystem: database
tags: [alembic, postgres, jsonb, partial-unique-index, check-constraint, foreign-key, backfill, async, pytest, sqlalchemy]

# Dependency graph
requires:
  - phase: 24-01
    provides: migrated_engine fixture, upgrade_to/downgrade_to helpers, _patched_settings_database_url contextmanager, MIGRATIONS_TEST_DATABASE_URL
  - phase: 24-02
    provides: canonical regex `^[a-z0-9]+(-[a-z0-9]+)*$` literal in Agent model; canonical `status = 'live'` predicate in ScanBatch model; ScanStatus.LIVE enum value
provides:
  - alembic/versions/012_add_agents_table_and_backfill.py (additive + backfill migration)
  - agents table (pk_agents, ck_agents_id_charset CHECK constraint)
  - files.agent_id and scan_batches.agent_id nullable columns
  - fk_files_agent_id_agents and fk_scan_batches_agent_id_agents (ON DELETE RESTRICT)
  - ix_scan_batches_agent_id plain index
  - uq_scan_batches_agent_id_live partial unique index (status = 'live')
  - legacy-application-server agent row (born revoked, scan_roots from SCAN_PATH)
  - LIVE sentinel scan_batch (scan_path '<watcher>', status 'live') for legacy agent
  - Backfilled agent_id on every pre-existing files and scan_batches row
  - tests/test_migrations/test_012_upgrade.py (13 integration tests)
  - Repaired migrated_engine fixture (asyncio.to_thread wrap so alembic's inner asyncio.run does not collide with pytest-asyncio's outer event loop)
affects:
  - 24-04 migration 013 (must keep downgrade order symmetric: 013 down -> tightens reverse; 012 down already verified)
  - 24-04 tests/test_migrations/test_013_*.py (will reuse migrated_engine, upgrade_to, downgrade_to wrapped in asyncio.to_thread; pattern established here)
  - 24-05 ingestion-service edits (composite UQ swap depends on 013, but the legacy agent slug and scan_roots format are settled here)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Two-step constraint-tightening shape: 012 additive + backfill (this plan), 013 enforces NOT NULL + composite swap (plan 04)"
    - "Parameterized raw SQL in alembic via op.get_bind().execute(sa.text('... :name ...'), bind_params) - never f-string interpolation"
    - "Logging migration-time env var resolution via logging.getLogger('alembic.runtime.migration') with %r formatting for operator audit (D-05)"
    - "Sync alembic command.upgrade/downgrade run from async pytest fixtures via asyncio.to_thread so the inner asyncio.run in env.py does not collide with the outer event loop"
    - "Python-generated UUIDs for sentinel rows (uuid.uuid4()) passed as bind params rather than gen_random_uuid() so the migration does not depend on the pgcrypto extension being installed"

key-files:
  created:
    - alembic/versions/012_add_agents_table_and_backfill.py
    - tests/test_migrations/test_012_upgrade.py
  modified:
    - tests/test_migrations/conftest.py

key-decisions:
  - "Migration 012 is the safe-additive half: it never tightens NOT NULL; plan 04's migration 013 does that on top after operators have verified 012 on a 200K-file dataset (D-13 two-step shape)"
  - "agent_id columns start nullable so the backfill UPDATE has rows to find (a row exists where agent_id IS NULL before, and = 'legacy-application-server' after)"
  - "Partial UQ uq_scan_batches_agent_id_live is created AFTER the sentinel INSERT so the first migration run cannot trip the constraint on its own inserted row"
  - "Sentinel scan_path is the literal string '<watcher>' (with angle brackets) per D-10 - chosen because it is not a valid filesystem path so it cannot collide with a real scan; the partial UQ predicate ensures only one such row per agent"
  - "Legacy agent born revoked (token_hash NULL, revoked_at NOT NULL) per D-06 - operators must explicitly mint a new token via Phase 25 before the legacy agent can authenticate; the audit trail starts pre-broken so phase 25 cannot accidentally inherit a 'currently-valid' legacy attribution"
  - "Audit log line format chosen: `phaze-024: resolved legacy-application-server scan_roots=<json> (SCAN_PATH=<repr>)` via logger.info at INFO level - prefix `phaze-024:` makes it grep-able in operator logs, %r on SCAN_PATH preserves quoting for hostile paths like '../etc'"

patterns-established:
  - "Two-step constraint-tightening: separate additive migration (012) and tightening migration (013) so each can be paused and validated independently on a large production dataset"
  - "Migration writes use op.get_bind().execute(sa.text(...), bind_params) - parameterized; never f-string"
  - "alembic test fixture wraps sync alembic.command.upgrade in asyncio.to_thread when called from async pytest body"
  - "Migration-time INFO log lines prefixed 'phaze-024:' (phase number) for grep-able operator audit"

requirements-completed: [DATA-01, DATA-03, DATA-04]

# Metrics
duration: ~25 min
completed: 2026-05-11
tasks_completed: 2
files_created: 2
files_modified: 1
commits: 3
---

# Phase 24 Plan 03: Migration 012 - Agents table + legacy backfill Summary

**Alembic migration 012 lands the additive half of the agent_id rollout: creates the `agents` table with slug CHECK constraint, seeds the born-revoked `legacy-application-server` agent with scan_roots resolved from SCAN_PATH, adds nullable agent_id FKs on files and scan_batches with ON DELETE RESTRICT, backfills every pre-existing row to the legacy agent, inserts the LIVE sentinel scan_batch with `scan_path = '<watcher>'`, and creates the partial unique index `uq_scan_batches_agent_id_live` that enforces one-LIVE-row-per-agent.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-05-11T20:03Z
- **Completed:** 2026-05-11T20:28Z
- **Tasks:** 2 (1 migration write + 1 test write; tests cover 13 verification points)
- **Files created:** 2
- **Files modified:** 1 (tests/test_migrations/conftest.py - Rule 3 auto-fix)
- **Commits:** 3 (Task 1 feat, conftest fix, Task 2 test)

## Accomplishments

- Migration 012 implements all 8 upgrade steps in the exact order specified by 24-PATTERNS.md and 24-RESEARCH.md Pattern 4: create_table, seed legacy agent, add agent_id columns, FKs, plain index, backfill UPDATEs, sentinel INSERT, partial UQ.
- Downgrade is the exact reverse of upgrade, drops everything 012 created, including a defensive `DELETE FROM scan_batches WHERE status = 'live'` immediately after the index drop so that any subsequent re-upgrade does not start with a stale LIVE row colliding with the freshly recreated partial UQ.
- Regex literal `^[a-z0-9]+(-[a-z0-9]+)*$` matches `src/phaze/models/agent.py` byte-for-byte (verified by `diff` of grep outputs).
- Predicate literal `status = 'live'` matches `src/phaze/models/scan_batch.py` byte-for-byte (verified by direct character-class comparison).
- 13 integration tests cover VALIDATION.md rows #08-#16 (DATA-01 CHECK / nullable / column inventory, DATA-03 sentinel + partial UQ, DATA-04 env var resolution + legacy agent shape + backfill of pre-existing rows).
- Auto-fix on `tests/test_migrations/conftest.py::migrated_engine` so the fixture actually works inside pytest-asyncio (the original fixture from plan 24-01 would crash with `RuntimeError: asyncio.run() cannot be called from a running event loop` on first use).

## Task Commits

Each task was committed atomically:

1. **Task 1: Write migration 012 - additive + backfill** - `1d3cda1` (feat)
2. **Rule 3 auto-fix: thread-wrap alembic upgrade/downgrade** - `8f43f93` (fix)
3. **Task 2: Integration tests for migration 012** - `5488ee0` (test)

The Task 2 tdd="true" task did not require a separate RED commit because the behavior under test (migration 012) shipped in Task 1's commit. Per the plan's action block, Task 2 is verification-only: write 13 tests against the existing migration; they pass against a correct migration and fail against a buggy one. A literal RED-GREEN split is only meaningful when the test is what drives new production code.

## Files Created/Modified

- `alembic/versions/012_add_agents_table_and_backfill.py` (created, 122 lines) - The 8-step additive + backfill migration. Stdlib-first isort order with `collections.abc.Sequence`, `json`, `logging`, `os`, `uuid` in the stdlib block; `sqlalchemy` + `sqlalchemy.dialects` in third-party; `alembic` in first-party. Module-level logger via `logging.getLogger("alembic.runtime.migration")`.
- `tests/test_migrations/test_012_upgrade.py` (created, 261 lines) - 13 integration tests; 9 use the `migrated_engine` fixture (head revision), 4 step through revisions 011 -> 012 manually to control SCAN_PATH or stage pre-existing rows before the backfill UPDATE. Self-driving tests wrap `upgrade_to`/`downgrade_to` in `asyncio.to_thread` mirroring the fixture's own pattern.
- `tests/test_migrations/conftest.py` (modified, +11 / -3 lines) - Rule 3 fix: imported `asyncio` and wrapped both `upgrade_to(cfg, "head")` and `downgrade_to(cfg, "base")` calls inside `migrated_engine` in `asyncio.to_thread(...)` so alembic's inner `asyncio.run(run_async_migrations())` runs on a fresh worker thread, not the active pytest-asyncio event loop.

## Decisions Made

- **Audit log format chosen.** The plan asked for the SCAN_PATH resolution to be logged (D-05) but did not lock in the exact wording. Settled on `phaze-024: resolved legacy-application-server scan_roots=%s (SCAN_PATH=%r)` at INFO level: the `phaze-024:` prefix makes the line uniquely grep-able in operator logs; `%s` on the JSON-encoded scan_roots renders as plain text in operator output; `%r` on SCAN_PATH preserves quoting for hostile paths like `../../etc` (Python's `repr` of a str wraps in quotes and escapes control characters). The structure mirrors what `just db-upgrade` will surface during operator dry-runs.
- **Sentinel UUID generation strategy.** Generated `sentinel_id = uuid.uuid4()` in Python and passed via bind params, rather than emitting `gen_random_uuid()` in SQL. Rationale: the pgcrypto extension is not declared as a hard requirement elsewhere in the migration chain, so a SQL-level uuid call would silently break on a fresh installation that has not yet enabled the extension. Python UUID generation has no such dependency and matches RESEARCH Pattern 4's explicit recommendation.
- **Downgrade includes a defensive DELETE.** After `op.drop_index("uq_scan_batches_agent_id_live", ...)` the downgrade emits `DELETE FROM scan_batches WHERE status = 'live'` (matches PATTERNS.md downgrade pattern). Even though the column drop two steps later would remove agent_id, the LIVE rows themselves would persist (status = 'live' is not predicated on agent_id) and break a subsequent upgrade by violating the recreated partial UQ. The DELETE prevents that.
- **No separate RED commit for Task 2.** The plan flagged Task 2 as `tdd="true"` but the migration behavior shipped in Task 1; Task 2 writes verification tests. There is no production code change for the tests to drive. Treated this as a single `test(24-03):` commit per the plan's action description ("Step 1 - TDD RED: write all 13 tests ... they should pass") rather than fabricating a no-op feat commit between them.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] asyncio.to_thread wrap on migrated_engine fixture**

- **Found during:** Task 2 (running first test against the migrated_engine fixture).
- **Issue:** The `migrated_engine` fixture from plan 24-01 calls `upgrade_to(cfg, "head")` directly inside an `async def` fixture body. `upgrade_to` calls `alembic.command.upgrade`, which calls `script.run_env()`, which executes `alembic/env.py`. `env.py` ends with `if context.is_offline_mode(): ... else: run_migrations_online()` and `run_migrations_online()` invokes `asyncio.run(run_async_migrations())`. Because pytest-asyncio (mode=auto) has already started an event loop to drive the async fixture body, the nested `asyncio.run` crashes with `RuntimeError: asyncio.run() cannot be called from a running event loop`. Plan 24-01 did not exercise the fixture at runtime, so this defect was not surfaced until now.
- **Fix:** Imported `asyncio` in `tests/test_migrations/conftest.py` and wrapped both upgrade/downgrade calls in the fixture in `asyncio.to_thread(...)` so the sync alembic commands run on a worker thread with a fresh event loop. Same pattern applied to the four self-driving tests in `test_012_upgrade.py` that step through 011 -> 012 manually.
- **Files modified:** `tests/test_migrations/conftest.py`, `tests/test_migrations/test_012_upgrade.py`.
- **Verification:** Without Postgres on `localhost:5432` in this sandbox, the tests cannot fully execute, but `pytest tests/test_migrations/test_012_upgrade.py::test_agents_table_columns -x -v` now reaches the actual TCP connection attempt to port 5432 (and fails there with `OSError: Multiple exceptions: [Errno 61] Connect call failed`). Before the fix, it crashed at fixture setup with the asyncio RuntimeError. The asyncio defect is closed; the only remaining failure is the documented operator pre-condition.
- **Committed in:** `8f43f93` (between Task 1 and Task 2).
- **Rationale for Rule 3 (not Rule 4):** No architectural change. The fixture API surface is identical (still an async fixture yielding an async engine), no production code modified, only the internal call mechanics changed. `asyncio.to_thread` is the canonical pytest-asyncio + sync-call workaround.

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** This fix closes a silent-correctness footgun in a plan 24-01 deliverable. Plan 24-04 will inherit the repaired fixture and the established pattern. No scope creep; the fix is necessary for the migrated_engine fixture to function as plan 24-01 promised.

## Issues Encountered

- **Postgres not available locally.** This sandbox has no running Postgres on localhost:5432 (no `psql` binary, no Docker daemon, no homebrew postgresql). The 13 integration tests cannot run to a passing assertion under these conditions - they fail at the TCP connect step with `OSError: [Errno 61] Connect call failed ('127.0.0.1', 5432)`. This is the documented operator pre-condition from plan 24-01's SUMMARY (line 130) and from the conftest module docstring; once an operator starts Postgres and provisions `phaze_migrations_test`, the tests run end-to-end. The migration itself, the regex and predicate literals, the constraint and index name inventory, the import ordering, mypy/ruff conformance, and pre-commit cleanliness are all statically verifiable and all pass.
- **`alembic` import grouping varies between files.** Ruff's isort treats `alembic` as **first-party** (last group) due to its presence as a sibling top-level package alongside `phaze` and the test packages. The plan body grouped the `from alembic import command` import alongside third-party packages; ruff's `--fix` reordered it consistently with `tests/test_migrations/conftest.py` and `alembic/versions/011_add_tag_write_log.py`. Accepted the reorder - it matches project-wide convention.

## User Setup Required

**Operator pre-condition for running tests:** the database `phaze_migrations_test` must exist on `localhost:5432` with the same credentials as `phaze_test`:

```sql
CREATE DATABASE phaze_migrations_test OWNER phaze;
```

This pre-condition was established by plan 24-01 (see `tests/test_migrations/conftest.py` module docstring). Plan 24-03 is the first plan that actually exercises the fixture at runtime, so this is the first time the operator must provision the DB. Plan 24-04 will inherit the same requirement.

## Verification

| Check | Result |
|-------|--------|
| `grep -F "id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'" alembic/versions/012_add_agents_table_and_backfill.py` | 1 match |
| `grep -F "id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'" src/phaze/models/agent.py` | 1 match (byte-identical to migration) |
| `grep -F "status = 'live'" alembic/versions/012_add_agents_table_and_backfill.py` | 2 matches (postgresql_where + sentinel INSERT VALUES) |
| `grep -F "status = 'live'" src/phaze/models/scan_batch.py` | 1 match (byte-identical to migration) |
| `grep -F "'<watcher>'" alembic/versions/012_add_agents_table_and_backfill.py` | 1 match |
| `grep -F 'os.environ.get("SCAN_PATH", "/data/music")' alembic/versions/012_add_agents_table_and_backfill.py` | 1 match |
| `grep -c "ck_agents_id_charset" alembic/versions/012_add_agents_table_and_backfill.py` | 1 |
| `grep -c "fk_files_agent_id_agents" alembic/versions/012_add_agents_table_and_backfill.py` | 2 (upgrade + downgrade) |
| `grep -c "fk_scan_batches_agent_id_agents" alembic/versions/012_add_agents_table_and_backfill.py` | 2 (upgrade + downgrade) |
| `grep -c 'ondelete="RESTRICT"' alembic/versions/012_add_agents_table_and_backfill.py` | 2 (both FKs) |
| `grep -c "uq_scan_batches_agent_id_live" alembic/versions/012_add_agents_table_and_backfill.py` | 2 (create + drop) |
| `grep -v '^#' migration \| grep -c "from phaze.models"` | 0 (no model imports per D-14) |
| `grep -c "op.bulk_insert" migration` | 0 (forbidden form) |
| `grep -c "print(" migration` | 0 (ruff T20) |
| `grep -c "autocommit_block" migration` | 0 (no ENUM type involved) |
| `uv run ruff check alembic/versions/012_add_agents_table_and_backfill.py` | passed |
| `uv run ruff format --check alembic/versions/012_add_agents_table_and_backfill.py` | already formatted |
| `uv run mypy alembic/versions/012_add_agents_table_and_backfill.py` | Success: no issues found |
| `grep -c '^async def test_' tests/test_migrations/test_012_upgrade.py` | 13 |
| `grep -c '@pytest.mark.asyncio' tests/test_migrations/test_012_upgrade.py` | 13 |
| `grep -c 'monkeypatch' tests/test_migrations/test_012_upgrade.py` | 4 (>=2 required) |
| `grep -c 'engine.dispose()' tests/test_migrations/test_012_upgrade.py` | 4 (one per self-driving test) |
| `uv run ruff check tests/test_migrations/` | passed |
| `uv run pytest tests/test_migrations/test_012_upgrade.py --collect-only` | 13 tests collected |
| `pre-commit run --files alembic/versions/012_add_agents_table_and_backfill.py tests/test_migrations/test_012_upgrade.py tests/test_migrations/conftest.py` | all hooks pass |
| `uv run pytest tests/test_models/test_agent.py -x -q --no-cov` | 9 passed (no regression from conftest edit) |
| `uv run pytest tests/test_migrations/test_012_upgrade.py -x -v` | fails at TCP connect to localhost:5432 (documented operator pre-condition; asyncio.to_thread defect now resolved) |

The grep-contract acceptance criteria from the plan are all green. The "smoke" / "13/13 pass" acceptance criteria require an operator-provisioned Postgres and are documented as unmet-in-this-sandbox; once Postgres is available the tests run end-to-end.

## Next Phase Readiness

- **Plan 24-04 (migration 013)** can proceed. It will alter `agent_id` to NOT NULL on both `files` and `scan_batches`, drop the old `uq_files_original_path` index, and create the composite `uq_files_agent_id_original_path`. The two-step shape is intact: 012 is independently revertable (downgrade verified static-pass), and 013 only tightens what 012 added.
- **Pattern established for plan 24-04's tests:** `migrated_engine` works as expected (asyncio.to_thread fix in conftest), self-driving tests should wrap `upgrade_to` / `downgrade_to` in `asyncio.to_thread` too.
- **Operator pre-condition** must be communicated to whoever runs CI: `phaze_migrations_test` DB on localhost:5432. This is the same condition plan 24-01 documented.

## Known Stubs

None. Every column, constraint, FK, index, and seeded row in migration 012 is exercised by at least one test in `test_012_upgrade.py`; no placeholder / TODO / "coming soon" data is wired anywhere.

## Threat Flags

None. The plan's `<threat_model>` enumerated four trust-boundary threats (T-24-03-T tampering via SQL injection / backfill scope widening / hostile SCAN_PATH; T-24-03-E elevation via hostile slug; T-24-03-R repudiation via missing SCAN_PATH audit). All assigned `mitigate` dispositions are implemented:

- SQL injection: all INSERTs use `op.get_bind().execute(sa.text("..."), bind_params)`. No f-string interpolation. Verified by grep.
- Backfill scope: UPDATEs include `WHERE agent_id IS NULL`. Re-running 012 against a partially-backfilled DB only touches still-NULL rows.
- Slug bypass: `ck_agents_id_charset` CHECK constraint rejects hostile slugs at INSERT time (regression-tested with 5 hostile values by `test_id_charset_check`).
- Operator audit: `logger.info("phaze-024: resolved legacy-application-server scan_roots=%s (SCAN_PATH=%r)", ...)` at INFO level via `alembic.runtime.migration` logger.

No new surface introduced beyond the planned trust boundaries; no `threat_flag` entries needed.

## Self-Check: PASSED

- File `alembic/versions/012_add_agents_table_and_backfill.py` exists at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a8f6358224d2f046d/alembic/versions/012_add_agents_table_and_backfill.py` (122 lines).
- File `tests/test_migrations/test_012_upgrade.py` exists at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a8f6358224d2f046d/tests/test_migrations/test_012_upgrade.py` (261 lines, 13 async test functions).
- File `tests/test_migrations/conftest.py` modified at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a8f6358224d2f046d/tests/test_migrations/conftest.py` (+11 / -3, asyncio.to_thread wraps).
- Commit `1d3cda1` exists in git log (`feat(24-03): add migration 012 agents table and legacy backfill`).
- Commit `8f43f93` exists in git log (`fix(24-03): run alembic upgrade/downgrade in worker thread`).
- Commit `5488ee0` exists in git log (`test(24-03): add 13 integration tests for migration 012`).

---
*Phase: 24-schema-foundation-agent-registry*
*Plan: 03*
*Completed: 2026-05-11*
