# Quick 260609 PR2: Scan completed_at / elapsed-timer fix — Summary

Closes the "elapsed time keeps climbing on COMPLETED scans" defect by sealing all
three NULL-`completed_at` sources for terminal `ScanBatch` rows: a data backfill
(migration 016), a legacy-writer fix (`run_scan`), and a defensive read
(`elapsed_seconds` freezes terminal+NULL rows at `updated_at`).

## Tasks & Commits

| Task | Commit | Summary |
| ---- | ------ | ------- |
| 1 — Migration 016 backfill | `ded3cb6` | feat(quick-260609-01): backfill completed_at on terminal NULL scan_batches (migration 016) |
| 2 — Stamp completed_at in run_scan (TDD) | `a3a3f04` | feat(quick-260609-01): stamp completed_at on run_scan terminal transitions |
| 3 — Defensive elapsed_seconds (TDD) | `69d0cae` | feat(quick-260609-01): freeze elapsed_seconds at updated_at for terminal NULL rows |

Branch: `fix/scan-completed-at-elapsed` (worktree `/Users/Robert/Code/public/phaze-pr2-scan-elapsed`). Not pushed; no PR opened (per instructions).

## What Changed

### Task 1 — alembic/versions/016_backfill_scan_batches_completed_at.py (+ test)
- Data-only migration. revision="016", down_revision="015", single Alembic head.
- upgrade(): one op.execute — UPDATE scan_batches SET completed_at = updated_at WHERE status IN ('completed','failed') AND completed_at IS NULL. Raw SQL, static literals (no injection surface — threat T-PR2-02 accept).
- downgrade(): intentional no-op with docstring explaining irreversibility (cannot distinguish originally-NULL from legitimately-stamped rows).
- tests/test_migrations/test_016_upgrade.py: drives base->015->insert 4 rows->016; asserts COMPLETED-null and FAILED-null backfill to updated_at, RUNNING-null stays NULL, already-stamped COMPLETED keeps its original timestamp.

### Task 2 — src/phaze/services/ingestion.py::run_scan (+ tests)
- Added `from datetime import UTC, datetime` (isort force-sort-within-sections placement).
- completed_at=datetime.now(UTC) added to BOTH .values(...) calls (COMPLETED and FAILED). raise in except untouched.
- Docstring records the two-terminal-writer audit: run_scan (legacy path) + agent PATCH in agent_scan_batches.py; watcher writes only non-terminal LIVE.
- New success + failure tests via async_sessionmaker(async_engine, ...) factory.

### Task 3 — src/phaze/routers/pipeline_scans.py::elapsed_seconds (+ tests)
- Added module constant _TERMINAL_STATUSES = frozenset({ScanStatus.COMPLETED, ScanStatus.FAILED}).
- Branching freeze-point precedence: (1) completed_at set -> freeze at it; (2) terminal + NULL completed_at -> freeze at updated_at (fallback to now if updated_at is None); (3) running -> now. tz-naive->UTC guard extended to updated_at.
- Docstring superseded to cover incident 260609.
- New tests: COMPLETED-null freeze, FAILED-null freeze, tz-naive updated_at. Existing running / completed_at-set tests unchanged and still green.

## Verification (run from worktree)

1. Plan tests — pytest test_pipeline_scans.py test_ingestion.py test_016_upgrade.py -q -> 57 passed.
   Note: this sandbox has Postgres (localhost:5432) and the phaze_migrations_test / phaze_test DBs provisioned, so migration + DB-backed run_scan tests actually ran (not skipped). Redis is down, but the router-contract tests use a self-contained smoke-app fixture with an AsyncMock task_router, so they need no Redis and passed.
2. run_scan tests — success + failure both green; failure path re-raises RuntimeError and persists FAILED + completed_at + error_message. TDD RED captured first (both failed on completed_at is None); GREEN after fix.
3. elapsed_seconds tests — TDD RED captured (got 100, want ~60); GREEN after branching. All 8 elapsed unit tests pass.
4. Repo-wide quality — ruff check . -> All checks passed; ruff format --check . -> 265 files formatted; mypy . -> no issues in 137 source files.
5. Coverage on changed code — pipeline_scans.py 100%, ingestion.py 91.03% (combined 95.45%) — both >=85%. Missing ingestion.py lines 169-175 are the pre-existing queue-enqueue branch (tests pass queue=None), out of scope.
6. pre-commit run --all-files — all hooks Passed (no --no-verify).
7. Migration head — alembic heads -> 016 (head); diff confirms down_revision: str | Sequence[str] | None = "015".

## Deviations from Plan

- Migration test agent insert removed (vs. plan's "reuse the agent-insert style from test_downgrade.py"). The legacy-application-server FK target is already created by migration 012's backfill, so an explicit INSERT INTO agents tripped UniqueViolationError on pk_agents. Removed the insert and reused the migration-seeded legacy agent. Logic-only test-harness adjustment (Rule 3 — blocking issue); no production code affected.

No other deviations. No authentication gates. No architectural changes. Scope held: no stall-heartbeat column, reaper, UI indicator (PR4), or structlog (PR3).

## Sandbox Infra Note
The prompt anticipated DB/Redis connection errors. In practice both Postgres and the migrations/test DBs were reachable here, so the migration and run_scan integration tests executed for real and passed. No test errored purely on connection during the plan-scoped runs.

## Self-Check: PASSED
- Files exist: alembic/versions/016_backfill_scan_batches_completed_at.py, tests/test_migrations/test_016_upgrade.py, modified src/phaze/services/ingestion.py, tests/test_services/test_ingestion.py, src/phaze/routers/pipeline_scans.py, tests/test_routers/test_pipeline_scans.py.
- Commits exist on fix/scan-completed-at-elapsed: ded3cb6, a3a3f04, 69d0cae.
