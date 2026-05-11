---
phase: 24-schema-foundation-agent-registry
plan: 04
subsystem: database
tags: [alembic, postgres, not-null, composite-unique-index, downgrade-guard, runtime-error, async, pytest, sqlalchemy]

# Dependency graph
requires:
  - phase: 24-01
    provides: migrated_engine fixture, upgrade_to/downgrade_to helpers wrapped in asyncio.to_thread, MIGRATIONS_TEST_DATABASE_URL
  - phase: 24-02
    provides: Composite UQ uq_files_agent_id_original_path declared in FileRecord __table_args__ (model-side); Agent model with kebab-case CHECK regex; ScanStatus.LIVE enum value
  - phase: 24-03
    provides: Migration 012 (agents table, FK columns, legacy backfill, LIVE sentinel, partial UQ); revised conftest with asyncio.to_thread wrapping
provides:
  - alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py (constraint-tightening migration)
  - files.agent_id NOT NULL + scan_batches.agent_id NOT NULL (DATA-02 + DATA-03 NOT NULL half)
  - uq_files_agent_id_original_path composite unique index (DATA-02 SC #2: same path allowed under different agents)
  - Drop of legacy uq_files_original_path single-column index
  - Downgrade D-16 dupe-detection guard - RuntimeError with "Cannot downgrade 013->012" before any DDL
  - tests/test_migrations/test_013_upgrade.py (5 integration tests covering VALIDATION rows #17-#21)
  - tests/test_migrations/test_downgrade.py (3 downgrade tests covering rows #22-#24, including D-16 error path)
affects:
  - 24-05 ingestion-service edits (composite UQ swap is now active; INSERT ON CONFLICT target must be (agent_id, original_path) not (original_path))
  - phase 25+ agent auth middleware (legacy agent already born revoked; 013 just locks the attribution invariant at the DB level)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Constraint-tightening migration shape: alter_column nullable=False on already-backfilled columns (op.alter_column with existing_type=sa.String(64) per asyncpg backend requirement)"
    - "Unique-index swap pattern: op.drop_index of old single-column UQ THEN op.create_index of new composite UQ (separate statements, atomic within the alembic transaction)"
    - "Downgrade dupe-detection guard: bind.execute(sa.text('SELECT ... GROUP BY ... HAVING COUNT(*) > 1 LIMIT 5')).scalars().all() BEFORE any DDL; raise RuntimeError if non-empty"
    - "Downgrade test sequence: downgrade_to(base) -> upgrade_to(target_rev) -> downgrade_to(target_rev - 1) -> assert state, wrapped in asyncio.to_thread for pytest-asyncio compatibility"
    - "ASCII arrows in error messages (013->012 not 013→012) - matches yamllint/ruff conventions and pytest.raises(match=...) regex literal"

key-files:
  created:
    - alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py
    - tests/test_migrations/test_013_upgrade.py
    - tests/test_migrations/test_downgrade.py
  modified: []

key-decisions:
  - "Reused asyncio.to_thread pattern from plan 24-03 in test_downgrade.py - same fixture-API constraint applies to self-driven downgrade tests (alembic env.py's inner asyncio.run cannot be called from the outer pytest-asyncio loop)"
  - "Downgrade fails BEFORE any DDL mutation - the SELECT runs first via op.get_bind(); the RuntimeError propagates and Alembic's transactional wrapper rolls back to the pre-call state. No partial-swap states possible."
  - "Error message format is exactly 'Cannot downgrade 013->012: original_path is no longer unique across agents. Example collisions: {dupes!r}. Resolve manually before retrying. Silent dedup is FORBIDDEN per phase-24 D-16.' - the substring 'Cannot downgrade 013->012' is the assertion-stable contract; the rest is operator-facing diagnostics"
  - "LIMIT 5 on the dupe-detection SELECT is a pragmatic bound - if there are thousands of dupes the operator only needs a handful of examples to start debugging; full enumeration is wasted work"

patterns-established:
  - "Two-step migration model now complete (012 additive + backfill from plan 24-03, 013 tightening from this plan). Each migration is independently revertable on a clean dataset."
  - "Downgrade tests live in their own file (test_downgrade.py) separate from per-revision upgrade tests so the test layout mirrors the asymmetric runtime: many upgrades on the happy path, fewer downgrades exercised only during operator interventions."

requirements-completed: [DATA-02, DATA-04]

# Metrics
duration: ~5 min
completed: 2026-05-11
tasks_completed: 3
files_created: 3
files_modified: 0
commits: 3
---

# Phase 24 Plan 04: Migration 013 - NOT NULL + Composite UQ Swap Summary

**Alembic migration 013 lands the constraint-tightening half of the agent_id rollout: enforces NOT NULL on `files.agent_id` and `scan_batches.agent_id` (both backfilled by plan 24-03's migration 012), drops the legacy single-column `uq_files_original_path` unique index, creates the composite `uq_files_agent_id_original_path` over `(agent_id, original_path)`, and implements the mandatory D-16 downgrade guard that raises `RuntimeError` if the same `original_path` lives under multiple agents.**

## Performance

- **Duration:** ~5 min (static-gate work only; the BLOCKING smoke gate is unmet-in-sandbox - see "User Setup Required")
- **Started:** 2026-05-11T20:18Z
- **Completed:** 2026-05-11T20:22Z
- **Tasks:** 3 implementation tasks + 1 BLOCKING checkpoint (deferred to operator)
- **Files created:** 3 (1 migration, 2 test modules)
- **Files modified:** 0
- **Commits:** 3 (Task 1 feat, Task 2 test, Task 3 test)

## Accomplishments

- Migration 013 implements the exact 4-step upgrade shape per RESEARCH Pattern 4 Revision 013 and PATTERNS.md target skeleton: alter files.agent_id NOT NULL, alter scan_batches.agent_id NOT NULL, drop uq_files_original_path, create uq_files_agent_id_original_path composite UQ.
- Downgrade reverses the four steps with the D-16 dupe-detection guard executed FIRST. The guard uses `op.get_bind().execute(sa.text("SELECT original_path FROM files GROUP BY original_path HAVING COUNT(*) > 1 LIMIT 5")).scalars().all()`; if non-empty, raises `RuntimeError` with the substring `Cannot downgrade 013->012` and a repr-formatted list of up to 5 colliding paths. No DDL runs after a non-empty result, so the downgrade aborts pre-mutation.
- Error message format is `Cannot downgrade 013->012: original_path is no longer unique across agents. Example collisions: {dupes!r}. Resolve manually before retrying. Silent dedup is FORBIDDEN per phase-24 D-16.` ASCII arrows throughout (no unicode arrows) per project ruff/yamllint conventions.
- Migration is exactly 60 lines (the plan's `min_lines: 60` floor); minimal imports (`collections.abc.Sequence`, `sqlalchemy`, `alembic.op`); no model imports per D-14 (decoupled from current model code).
- 5 upgrade integration tests cover VALIDATION rows #17-#21:
  - `test_files_agent_id_not_null` - information_schema.columns is_nullable = 'NO'
  - `test_scan_batches_agent_id_not_null` - same for scan_batches
  - `test_same_path_different_agent` - inserts a second agent `agent-b`, then two files with same `original_path` under different `agent_id` values; asserts both committed (SC #2 from ROADMAP)
  - `test_composite_unique_rejects_dup` - same `(agent_id, original_path)` pair twice in one transaction; second insert raises IntegrityError
  - `test_old_unique_dropped` - pg_indexes inventory: `uq_files_original_path` not present, `uq_files_agent_id_original_path` present
- 3 downgrade tests cover VALIDATION rows #22-#24:
  - `test_downgrade_013_clean` - clean 013 -> 012 path; restores uq_files_original_path, relaxes both agent_id columns back to nullable
  - `test_downgrade_013_fails_on_dupes` - inserts second agent + duplicate `original_path`, asserts `pytest.raises(RuntimeError, match="Cannot downgrade 013->012")`, verifies DB state unchanged (`uq_files_agent_id_original_path` still present), then cleans up the dupes so the finally `downgrade_to('base')` can succeed
  - `test_downgrade_012_clean` - 012 -> 011 in isolation; agents table dropped (`SELECT to_regclass('agents')` returns NULL), both agent_id columns removed, uq_files_original_path restored

## Task Commits

Each task was committed atomically:

1. **Task 1: Write migration 013 - NOT NULL + constraint swap + safe downgrade** - `be5d60b` (feat)
2. **Task 2: Integration tests for migration 013 - NOT NULL + composite UQ swap** - `ba944eb` (test)
3. **Task 3: Downgrade tests - clean roundtrip + D-16 dupe-detection error path** - `fda0b2b` (test)

The two `tdd="true"` tasks (Task 2 and Task 3) did not require a separate RED commit because the production behavior under test (migration 013) shipped in Task 1's commit. Per the plan's action blocks, both test tasks are verification-only: write tests against the existing migration; they pass against a correct migration and fail against a buggy one. A literal RED-GREEN split would have meant fabricating a no-op `feat:` commit between Task 1 and Task 2 - the same pattern plan 24-03 settled on for the same reason.

## Files Created/Modified

- `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` (created, 60 lines) - The 4-step upgrade + dupe-checked downgrade. Imports: `collections.abc.Sequence`, `sqlalchemy as sa`, `alembic.op` (no `os`, no `json`, no `uuid`, no `logging` - this migration neither reads env nor generates UUIDs nor logs anything). Two blank lines after imports per project ruff `lines-after-imports = 2`. revision/down_revision are zero-padded three-digit strings `"013"` / `"012"`.
- `tests/test_migrations/test_013_upgrade.py` (created, 106 lines) - 5 async tests, all using the `migrated_engine` fixture from plan 24-01. Imports: `uuid`, `pytest`, `sqlalchemy.text`, `sqlalchemy.exc.IntegrityError`. All test bodies use `async with engine.begin()` for inserts that must commit and `async with engine.connect()` for read-only queries.
- `tests/test_migrations/test_downgrade.py` (created, 147 lines) - 3 async tests, all self-driving (no `migrated_engine` fixture - each test calls `downgrade_to(base) -> upgrade_to(target) -> downgrade_to(target-1)`). Tests wrap every `upgrade_to`/`downgrade_to` call in `asyncio.to_thread` per the plan 24-01 pattern. Each test has a `finally:` block disposing the engine and calling `downgrade_to(cfg, "base")` to clean up.

## Decisions Made

- **No separate RED commit for tdd="true" tasks.** Plan flagged Task 2 and Task 3 as `tdd="true"` but the migration behavior they test shipped in Task 1. Tests are verification-only. Same precedent as plan 24-03 Task 2; single `test(24-04):` commit per task.
- **Downgrade tests use asyncio.to_thread for upgrade/downgrade calls.** Plan 24-01's pattern - alembic env.py's `asyncio.run(run_async_migrations())` cannot be nested inside pytest-asyncio's outer loop. Self-driving tests inherit this constraint; plan 24-03 already wrapped self-driving calls the same way (see `test_backfill_files` / `test_backfill_scan_batches`).
- **Error-match string uses ASCII arrow.** `Cannot downgrade 013->012` (hyphen + greater-than) not the unicode arrow. Justification: yamllint strict mode and project ruff config (`UP` rule set) treat unicode arrows in source strings as a smell; pytest.raises(match=...) is a regex literal so escaping issues are also avoided.
- **LIMIT 5 on dupe-detection SELECT.** PATTERNS.md target skeleton specifies `LIMIT 5`. Rationale: gives the operator a handful of examples to start debugging without enumerating thousands of rows when a corrupt scan attributes many files to the wrong agent. The `{dupes!r}` repr formatting safely quotes any control characters.
- **Test 2's pytest.raises wraps asyncio.to_thread.** The downgrade call is `await asyncio.to_thread(downgrade_to, cfg, "012")` inside `with pytest.raises(RuntimeError, match=...):`. asyncio.to_thread propagates the exception synchronously through the await, so pytest.raises catches it cleanly. Confirmed by inspecting the call shape - no special asyncio exception wrapping happens here.

## Deviations from Plan

None - plan executed exactly as written.

The plan's `<interfaces>` block, `<acceptance_criteria>` grep contracts, line-count floors, error-message contract, and test-name contracts were all satisfied verbatim. No auto-fixes, no architectural changes, no out-of-scope discoveries.

## Issues Encountered

- **Ruff format reflowed the dupe-detection SQL onto one line.** The plan's action block presents the `bind.execute(sa.text(...))` call as a multi-line string for readability, but ruff format (project line length 150) detected the SQL plus surrounding boilerplate fit on a single line and reflowed. The final formatted line is well under 150 chars; the grep contracts on `GROUP BY original_path HAVING COUNT(*) > 1` and `Cannot downgrade 013->012` remain satisfied. No behavior change.
- **Postgres not available locally.** Same documented operator pre-condition as plan 24-03 - this sandbox has no Postgres daemon on localhost:5432 and no Docker daemon (`unix:///var/run/docker.sock` does not exist). Static gates all pass (`ruff check`, `ruff format --check`, `mypy`, pre-commit, `pytest --collect-only`); the integration tests collect (8/8 new this plan, 21/21 across phase 24) but cannot reach a passing assertion without a running Postgres. The BLOCKING smoke gate (Task 4) requires a real operator workflow against a throwaway DB and is documented in "User Setup Required" below.

## User Setup Required

**Operator pre-condition for running the integration tests (inherited from plan 24-01 / 24-03):** the database `phaze_migrations_test` must exist on `localhost:5432` with the same credentials as `phaze_test`:

```sql
CREATE DATABASE phaze_migrations_test OWNER phaze;
```

**Task 4 BLOCKING smoke gate (unmet in this sandbox):** the operator-facing CLI roundtrip (`just db-upgrade`/`just db-downgrade`) was not executed because Docker Compose is not available here. The operator must run the 11-step sequence documented in `24-04-PLAN.md` Task 4 against a clean throwaway DB and confirm:

1. `docker compose up -d postgres`
2. `just db-current` (current revision)
3. `just db-upgrade` -> log shows `Running upgrade 011 -> 012` and `Running upgrade 012 -> 013`
4. `just db-current` -> revision is `013` (or `head`)
5. Log line `phaze-024: resolved legacy-application-server scan_roots=...` is present (D-05 audit trail)
6. `just db-downgrade` -> log shows `Running downgrade 013 -> 012`
7. `just db-downgrade` -> log shows `Running downgrade 012 -> 011`
8. `just db-current` -> revision is `011`
9. `just db-upgrade` -> both `Running upgrade ...` lines reappear
10. `just db-current` -> revision is back at head
11. `SCAN_PATH=/tmp/test-override uv run alembic upgrade 012` on a fresh test DB shows `scan_roots=['/tmp/test-override']` in the log

When the operator returns the actual log lines and exit codes, this SUMMARY.md should be updated in place with the captured output. Until then, the BLOCKING gate is recorded as "deferred to operator" rather than "passed".

## Verification

| Check | Result |
|-------|--------|
| `grep -c 'revision: str = "013"' alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` | 1 |
| `grep -c 'down_revision: str \| Sequence\[str\] \| None = "012"' migration` | 1 |
| `grep -c 'op.alter_column("files", "agent_id", nullable=False' migration` | 1 |
| `grep -c 'op.alter_column("scan_batches", "agent_id", nullable=False' migration` | 1 |
| `grep -c 'op.drop_index("uq_files_original_path"' migration` | 1 |
| `grep -F '"uq_files_agent_id_original_path"' migration` | 2 occurrences (create in upgrade, drop in downgrade) |
| `grep -F 'GROUP BY original_path HAVING COUNT(*) > 1' migration` | 1 |
| `grep -F 'Cannot downgrade 013->012' migration` | 1 (ASCII arrow) |
| `grep -c 'RuntimeError' migration` | 1 |
| `grep -v '^#' migration \| grep -c 'from phaze.models'` | 0 (no model imports per D-14) |
| `grep -c 'CONCURRENTLY' migration` | 0 (no CONCURRENTLY per D-13) |
| `grep -c 'op.drop_column' migration` | 0 (013 only tightens) |
| `grep -c '^async def test_' tests/test_migrations/test_013_upgrade.py` | 5 |
| `grep -c '^async def test_' tests/test_migrations/test_downgrade.py` | 3 |
| `grep -F 'pytest.raises(RuntimeError, match="Cannot downgrade 013->012")' test_downgrade.py` | 1 (line 102) |
| All 5 expected test_013 names present | yes |
| All 3 expected test_downgrade names present | yes |
| `uv run ruff check` (all 3 files) | All checks passed |
| `uv run ruff format --check` (all 3 files) | All formatted |
| `uv run mypy alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` | Success: no issues found |
| `pre-commit run --files` (all 3 files) | all hooks pass |
| `uv run pytest tests/test_migrations/ --collect-only -q` | 21 tests collected (13 from 24-03 + 5 + 3) |
| `uv run pytest tests/test_migrations/test_013_upgrade.py -x -v` | fails at TCP connect to localhost:5432 (documented operator pre-condition) |
| `uv run pytest tests/test_migrations/test_downgrade.py -x -v` | fails at TCP connect to localhost:5432 (same pre-condition) |
| `uv run pytest tests/test_models/ --no-cov -q` | 82 passed (no regression; 1 error is the same Postgres pre-condition on test_tables_created_in_database) |
| Migration line count >= 60 | 60 lines (exactly at the floor) |
| test_013_upgrade.py line count >= 80 | 106 lines |
| test_downgrade.py line count >= 100 | 147 lines |

All grep contracts and acceptance-criteria checks from the plan's `<acceptance_criteria>` blocks are green. The "tests pass" and "[BLOCKING] smoke" criteria require an operator-provisioned Postgres + Docker stack and are documented in "User Setup Required".

## Next Phase Readiness

- **Plan 24-05 (ingestion-service edits)** can proceed. Migration 013 is fully implemented and statically verified. Plan 24-05 will update `bulk_upsert_files` to use `(agent_id, original_path)` as the ON CONFLICT target instead of the old `(original_path)` - the schema is now ready to receive that change. The composite UQ in the model layer (plan 24-02) and the composite UQ in the DB (this plan) are now byte-aligned: both refer to `uq_files_agent_id_original_path`.
- **Pattern established for downgrade-test plans going forward:** self-driving alembic test files belong in their own module separate from per-revision upgrade tests, mirroring the asymmetric runtime (upgrade-heavy, downgrade-only-during-intervention).

## Known Stubs

None. Every column, constraint, FK, index, and seeded row in migration 013 is exercised by at least one test in `test_013_upgrade.py` or `test_downgrade.py`; no placeholder / TODO / "coming soon" data is wired anywhere.

## Threat Flags

None. The plan's `<threat_model>` enumerated four trust-boundary threats. Their dispositions:

- **T-24-04-T (Tampering: silent dedup on downgrade)** [mitigate]: implemented via `bind.execute(...).scalars().all()` SELECT before any DDL; raises RuntimeError if non-empty. Test `test_downgrade_013_fails_on_dupes` exercises the guard with the exact error-match contract.
- **T-24-04-T (Tampering: partial NOT NULL state)** [accept]: relies on Alembic's transactional wrapper. No additional mitigation in 013.
- **T-24-04-R (Repudiation: operator cannot reconstruct what migration did)** [mitigate]: deferred to Task 4's BLOCKING smoke gate (operator captures the actual log lines in this SUMMARY when they run the smoke).
- **T-24-04-S (Spoofing: attacker injects fake dupes to block downgrade)** [accept]: single-user local-only deployment.

No new surface introduced beyond the planned trust boundaries; no `threat_flag` entries needed.

## Self-Check: PASSED

- File `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` exists at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a9fd014fe88bc080f/alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py` (60 lines).
- File `tests/test_migrations/test_013_upgrade.py` exists at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a9fd014fe88bc080f/tests/test_migrations/test_013_upgrade.py` (106 lines, 5 async test functions).
- File `tests/test_migrations/test_downgrade.py` exists at `/Users/Robert/Code/public/phaze/.claude/worktrees/agent-a9fd014fe88bc080f/tests/test_migrations/test_downgrade.py` (147 lines, 3 async test functions).
- Commit `be5d60b` exists in git log (`feat(24-04): add migration 013 NOT NULL + composite UQ swap`).
- Commit `ba944eb` exists in git log (`test(24-04): add 5 integration tests for migration 013`).
- Commit `fda0b2b` exists in git log (`test(24-04): add 3 downgrade tests including D-16 dupe-detection error path`).

---
*Phase: 24-schema-foundation-agent-registry*
*Plan: 04*
*Completed: 2026-05-11*
