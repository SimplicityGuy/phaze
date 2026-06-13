---
phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker
plan: 03
subsystem: queue-control-plane-integration-tests
tags: [saq, postgres, integration-tests, saq_jobs, dequeue-semantics, concurrency, drain-pause]

# Dependency graph
requires:
  - phase: 36-pipeline-queue-backend-migration
    provides: "build_pipeline_queue factory + PostgresQueue (psycopg3 pool) + the auto-managed saq_jobs table with the ORDER BY priority, scheduled / FOR UPDATE SKIP LOCKED dequeue contract"
  - phase: 37-01-substrate
    provides: "STAGE_TO_FUNCTION / SENTINEL constants (the tests import the real SENTINEL, never a literal)"
  - phase: 37-02-helpers
    provides: "set_stage_priority / pause_stage / resume_stage raw saq_jobs UPDATE helpers + apply_stage_control before-enqueue hook (the code under test)"
provides:
  - "tests/integration/conftest.py: shared stage_env harness (a real build_pipeline_queue PostgresQueue + a SQLAlchemy AsyncSession on the same DB + a seeded pipeline_stage_control + hook-cache reset)"
  - "four real-PG integration modules proving REQ-37-1/2/3/4 against the live saq_jobs dequeue + count + row-locking contract"
  - "the Pitfall-1 paused count(queued)->0 / count(incomplete)-unchanged semantic pinned as a regression assertion"
affects: [37-04-endpoints, 38-pipeline-dag-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "dual-connection real-PG harness: a PostgresQueue (psycopg3) for enqueue/dequeue/count + a SQLAlchemy AsyncSession (asyncpg) on the SAME DB for the service-helper UPDATEs, both derived from TEST_DATABASE_URL"
    - "shared tests/integration/conftest.py fixture (Phase 36 duplicated its per-file fixture; Phase 37 shares one)"
    - "reset the apply_stage_control module-level TTL cache in fixture setup/teardown so per-test seeded control rows are read fresh"

key-files:
  created:
    - tests/integration/conftest.py
    - tests/integration/test_stage_pause.py
    - tests/integration/test_stage_priority.py
    - tests/integration/test_stage_resume.py
    - tests/integration/test_stage_concurrency.py
  modified:
    - tests/integration/__init__.py

key-decisions:
  - "Built the queue via build_pipeline_queue (per the plan key_links pattern) so the real apply_stage_control hook stamps each enqueued analyze job from a seeded pipeline_stage_control row -- the tests exercise the production enqueue path, not a bare PostgresQueue"
  - "Added tests/integration/conftest.py (shared stage_env fixture) instead of duplicating the harness in four files -- a Rule-3 reduce-duplication deviation; tests/integration/__init__.py already existed from Phase 36 so it was docstring-updated, not created"
  - "Assert dequeue ORDER + DB-column priority, NOT the deserialized Job.priority -- a raw saq_jobs priority UPDATE changes the ORDER BY column (what dequeue reads) but does NOT rewrite the serialized job BYTEA, so a dequeued Job.priority still mirrors the original stamp"
  - "Mirror pipeline_stage_control in the fixture via CREATE TABLE IF NOT EXISTS + ON CONFLICT seed so the harness does not depend on migration 020 having been applied to the ephemeral broker DB"
  - "Kept the explicit pytestmark = pytest.mark.integration in every file even though tests/conftest.py:126 already auto-marks tests/integration/ (belt-and-suspenders + the Plan 37-03 artifact contract)"

requirements-completed: [REQ-37-1, REQ-37-2, REQ-37-3, REQ-37-4]

# Metrics
duration: ~20min
completed: 2026-06-13
---

# Phase 37 Plan 03: Real-PG Per-Stage Control-Plane Integration Tests Summary

**Four real-Postgres integration modules (on a shared `stage_env` harness) that prove the Plan-02 service helpers against SAQ's live `saq_jobs` dequeue/count/row-lock contract: drain-style pause + the Pitfall-1 count semantic (REQ-37-1), live backlog reprioritization + the priority-0 floor (REQ-37-2), sentinel-guarded resume preserving retry backoffs (REQ-37-3), and no double-pickup / no deadlock under a concurrent admin-UPDATE-vs-dequeue race (REQ-37-4).**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-06-13
- **Tasks:** 2
- **Files modified:** 6 (5 created, 1 modified)

## Accomplishments

- **Shared `stage_env` harness** (`tests/integration/conftest.py`): probes broker connectivity (skips if Postgres is down), builds a real `PostgresQueue` through `build_pipeline_queue` (so all three before-enqueue hooks, including `apply_stage_control`, are wired exactly as in prod), runs `connect()`/`init_db()` to materialize `saq_jobs`, creates + seeds `pipeline_stage_control` (3 unpaused/priority-50 rows) idempotently, resets the hook's module-level TTL cache, and yields `(queue, session_factory)`. Teardown deletes this queue's `saq_jobs` rows, disconnects the pool, disposes the engine, and clears the cache. The SQLAlchemy DSN and the libpq broker DSN are both derived from `TEST_DATABASE_URL` (dialect-stripped for psycopg3), pointing at the same DB.
- **REQ-37-1 (`test_stage_pause.py`):** enqueues three analyze jobs, flips one to `status='active'` (in-flight), then `pause_stage("analyze")` parks every `status='queued'` analyze row at `scheduled = SENTINEL` while the active row's status + scheduled are untouched (drains). Pins the **Pitfall-1** semantic as a regression assertion: after pause `count("queued") == 0` (parked rows fail `now >= scheduled`) while `count("incomplete")` is unchanged (parked rows are still `status='queued'`).
- **REQ-37-2 (`test_stage_priority.py`):** `set_stage_priority("analyze", 5)` reprioritizes the live queued backlog so the analyze job dequeues BEFORE a priority-30 comparison job; a second test proves the helper writes the literal value (no clamp) and that priority `-1` is un-dequeueable (outside SAQ's `priority BETWEEN 0 AND 32767` window -- Pitfall 2) while `0` is the dequeueable floor.
- **REQ-37-3 (`test_stage_resume.py`):** parks the backlog at SENTINEL, mutates ONE row to a retry-backoff `scheduled = now + 3600`, then `resume_stage("analyze")` resets the SENTINEL rows to `0` and leaves the retry-backoff row untouched -- proving the `scheduled = SENTINEL` resume guard structurally preserves retry backoffs.
- **REQ-37-4 (`test_stage_concurrency.py`):** races a real `set_stage_priority` UPDATE (own session/connection + commit) against a real `queue.dequeue(...)` under `asyncio.gather`; asserts the gather completes (no deadlock/timeout), at most one row went `active` and it is exactly the dequeued job (no double-pickup), the row count is conserved, and every still-queued row carries the mutation -- tolerant of both safe interleavings (worker-locks-first / admin-locks-first).

## Task Commits

1. **Task 1: pause-drain + live-reprioritization real-PG tests** — `4a657b3` (test)
2. **Task 2: sentinel-guarded resume + concurrent no-double-pickup tests** — `cc1e4e8` (test)

## Files Created/Modified

- `tests/integration/conftest.py` *(created)* — shared `stage_env` fixture + DSN derivation + `pipeline_stage_control` DDL/seed + hook-cache reset.
- `tests/integration/test_stage_pause.py` *(created)* — REQ-37-1 drain-pause + Pitfall-1 count semantic.
- `tests/integration/test_stage_priority.py` *(created)* — REQ-37-2 live reorder + priority lower bound (2 tests).
- `tests/integration/test_stage_resume.py` *(created)* — REQ-37-3 sentinel-guarded resume preserving retry backoff.
- `tests/integration/test_stage_concurrency.py` *(created)* — REQ-37-4 no-double-pickup / no-deadlock race.
- `tests/integration/__init__.py` *(modified)* — extended the package docstring to describe the four Phase 37 modules.

## Decisions Made

- **Production enqueue path, not a bare queue** — using `build_pipeline_queue` means the `apply_stage_control` hook reads the seeded `pipeline_stage_control` through the queue's psycopg3 pool and stamps each enqueued analyze job at priority 50, so the helpers act on a realistically-stamped backlog (and the hook's real read path is exercised end-to-end).
- **Assert the COLUMN + dequeue behavior, never the deserialized `Job.priority`** — a raw `saq_jobs` priority UPDATE mutates the ordering column SAQ's `ORDER BY priority` reads, but NOT the serialized `job` BYTEA, so a dequeued `Job.priority` still mirrors the enqueue-time stamp. The tests therefore assert the DB column value + the resulting dequeue order/eligibility, which is what the feature actually depends on.
- **Self-contained control-table mirror** — the fixture `CREATE TABLE IF NOT EXISTS pipeline_stage_control ... ON CONFLICT` so the harness works whether or not migration 020 ran on the ephemeral broker DB, and never collides with a pre-migrated table.
- **Reset the hook TTL cache per test** — `apply_stage_control` caches `(paused, priority)` per stage for 5s in module globals; clearing it in fixture setup/teardown stops a stale window from serving a prior test's seeded priority.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - reduce duplication] Added `tests/integration/conftest.py` instead of duplicating the harness per file**
- **Found during:** Task 1
- **Issue:** The plan's `files_modified` listed `tests/integration/__init__.py` (to "create the package") and four test files, with Task 2 told to "reuse the Task-1 harness". `__init__.py` already existed (Phase 36), and pytest does not share fixtures across sibling test modules by import.
- **Fix:** Put the shared `stage_env` fixture in a new `tests/integration/conftest.py` (idiomatic pytest fixture sharing) and updated the existing `__init__.py` docstring rather than recreating it. No behavior change to source code; test-only addition.
- **Files modified:** `tests/integration/conftest.py` (created), `tests/integration/__init__.py` (docstring).
- **Commit:** `4a657b3`

**2. [Rule 1 - corrected a false assertion] Dropped `Job.priority == <new>` assertions on dequeued jobs**
- **Found during:** Task 1 (priority test failed: `assert 50 == 5`)
- **Issue:** The first draft asserted the dequeued `Job.priority` equalled the helper-set value. A raw column UPDATE does not rewrite the serialized job blob, so the deserialized `Job.priority` is the stale enqueue-time stamp (50), even though the column (and thus dequeue ORDER + eligibility) is the new value.
- **Fix:** Assert the DB column via a direct `SELECT priority` + the dequeue ORDER / eligibility instead of the blob attribute; documented the column-vs-blob distinction inline.
- **Files modified:** `tests/integration/test_stage_priority.py`
- **Commit:** `4a657b3`

### Plan note (not auto-marked vs explicit marker)
The plan/RESEARCH said a new `tests/integration/` path is NOT auto-marked and each file MUST declare `pytestmark`. Confirmed against `tests/conftest.py:126`: the collection hook DOES auto-mark anything under `tests/integration/`. The explicit `pytestmark = pytest.mark.integration` was kept in all four files anyway (belt-and-suspenders + the documented Plan 37-03 artifact contract). No functional impact.

## Known Stubs

None — test-only plan; no source stubs introduced.

## Issues Encountered

- One iteration on the priority test (the `Job.priority`-vs-column distinction above), fixed within Task 1 before commit. All pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on every commit without `--no-verify`.

## Verification

- Real-PG run (ephemeral Postgres + Redis on 5433/6380):
  `TEST_DATABASE_URL=postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test PHAZE_REDIS_URL=redis://localhost:6380/0 uv run pytest tests/integration/test_stage_{pause,priority,resume,concurrency}.py -q` → **5 passed**, stable across 5 consecutive runs (concurrency test not flaky).
- Coverage on the stage-control modules (integration + Plan-02 unit tests):
  `services/stage_control.py` 95.24%, `tasks/_shared/stage_control.py` 100.00%, combined **98.28%** (≥85% gate met).
- `uv run ruff check` clean on all touched files; mypy excludes `tests/`.
- Full suite via `just integration-test` exercises these alongside the rest (each file auto-marked `integration`, so `pytest -m 'not integration'` excludes them offline).

## Next Phase Readiness

- Plan 37-04 can now wire `POST /pipeline/stages/{stage}/{priority,pause,resume}`, calling the now-integration-proven helpers + updating the `PipelineStageControl` ORM row in one transaction. The endpoint owns the clamp `[0,100]`; the integration tests prove WHY (negative priority is un-dequeueable).
- Phase 38 can surface the paused backlog separately in the UI — the Pitfall-1 regression assertion documents that a paused stage reads `count("queued") == 0` while remaining `incomplete`.

## Self-Check: PASSED

- Created/modified files present: `tests/integration/conftest.py`, `test_stage_pause.py`, `test_stage_priority.py`, `test_stage_resume.py`, `test_stage_concurrency.py`, `__init__.py` — all FOUND.
- Commits present: `4a657b3`, `cc1e4e8` — both FOUND in git log.

---
*Phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker*
*Completed: 2026-06-13*
