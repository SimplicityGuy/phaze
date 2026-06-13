---
phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker
plan: 02
subsystem: queue-control-plane
tags: [saq, postgres, before-enqueue-hook, raw-sql, import-boundary, pipeline-control]

# Dependency graph
requires:
  - phase: 36-pipeline-queue-backend-migration
    provides: "build_pipeline_queue factory + PostgresQueue.pool (psycopg3) the hook reads through, and saq_jobs the helpers UPDATE"
  - phase: 37-01-substrate
    provides: "STAGE_TO_FUNCTION / _FUNCTION_TO_STAGE / SENTINEL constants + pipeline_stage_control table (migration 020)"
  - phase: 35-pipeline-determinism-idempotency
    provides: "deterministic <function>:<file_id> keys that make the key LIKE '<fn>:%' stage filter exact"
provides:
  - "apply_stage_control before-enqueue hook (stamps live priority + parks paused stage jobs) with a 5s TTL cache over job.queue.pool"
  - "set_stage_priority / pause_stage / resume_stage raw saq_jobs UPDATE helpers (allowlist-guarded, bound-param, sentinel-guarded resume)"
  - "the hook wired into all four queue construction sites via build_pipeline_queue (single seam)"
  - "test_stage_control_stays_postgres_free import-boundary guard for the new hook module"
affects: [37-03-integration-tests, 37-04-endpoints, 38-pipeline-dag-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "before-enqueue hook reads its own queue's psycopg3 pool (NOT SQLAlchemy) to stay agent-import-boundary-clean"
    - "module-level TTL cache (single monotonic expiry window) collapses bulk-enqueue control reads"
    - "raw saq_jobs UPDATEs as static bound-param text() constants with an allowlist-validated key prefix"

key-files:
  created:
    - src/phaze/services/stage_control.py
    - tests/test_stage_control.py
  modified:
    - src/phaze/tasks/_shared/stage_control.py
    - src/phaze/tasks/_shared/queue_factory.py
    - tests/test_task_split.py
    - tests/test_queue_factory.py

key-decisions:
  - "Hook reads the control table via job.queue.pool (psycopg3 %(name)s param), never SQLAlchemy — keeps the agent boundary intact (T-37-04)"
  - "5s TTL cache with a single monotonic expiry window; whole cache dropped on window expiry (37-RESEARCH Open-Q2, tunable)"
  - "Service helpers raise ValueError on unknown stage BEFORE building the key LIKE prefix (allowlist guard, T-37-01)"
  - "resume keeps the AND scheduled = :SENTINEL guard so retry backoffs (now+delay) are never clobbered (REQ-37-3)"
  - "Helpers do NOT commit (caller owns the txn) and do NOT scope by queue (global key PK disambiguates)"

requirements-completed: [REQ-37-1, REQ-37-2, REQ-37-3]

# Metrics
duration: ~12min
completed: 2026-06-13
---

# Phase 37 Plan 02: Per-Stage Control-Plane Hook, Service Helpers & Factory Wiring Summary

**The `apply_stage_control` before-enqueue hook (live priority stamp + pause park via a 5s-TTL psycopg3-pool read), the raw `saq_jobs` priority/pause/resume UPDATE helpers, and the single-seam factory registration that activates the hook on all four queues — with the agent import boundary proven intact.**

## Performance

- **Duration:** ~12 min
- **Completed:** 2026-06-13
- **Tasks:** 3
- **Files modified:** 6 (2 created, 4 modified)

## Accomplishments

- **`apply_stage_control` hook** (`tasks/_shared/stage_control.py`): resolves `job.function` to a stage via `_FUNCTION_TO_STAGE`; non-stage jobs return untouched. For a stage job it reads `(paused, priority)` from `pipeline_stage_control` through `job.queue.pool` (psycopg3, bound `%(stage)s` param), stamps `job.priority`, and parks the job (`job.scheduled = SENTINEL`) when paused. Mirrors `apply_deterministic_key`'s best-effort discipline: any read failure logs a warning and returns without mutating, so an enqueue is never blocked (T-37-02). A module-level TTL cache (`_CACHE_TTL_SECONDS = 5.0`, single monotonic expiry) collapses bulk-enqueue reads to ~1 SELECT per stage per window. The module stays free of `sqlalchemy` / `phaze.database` imports.
- **Service helpers** (`services/stage_control.py`): `set_stage_priority` / `pause_stage` / `resume_stage` issue the three exact RESEARCH-contract UPDATEs as static bound-param `text()` constants. Each validates `stage` against `STAGE_TO_FUNCTION` before building the `key LIKE '<fn>:%'` prefix (T-37-01); resume carries the `AND scheduled = :SENTINEL` guard (REQ-37-3); all are `status='queued'`-guarded (drain + no-double-pickup) and never commit / never scope by `queue`.
- **Factory wiring** (`queue_factory.py`): `apply_stage_control` registered as the THIRD `before_enqueue` hook, after `apply_deterministic_key`. The single `build_pipeline_queue` seam means the API per-agent queues, the controller queue, the agent worker, and `main.py`'s controller queue all inherit it (verified: no queue constructed outside the factory).
- **Boundary guard**: new `test_stage_control_stays_postgres_free` subprocess test imports the hook module under `PHAZE_ROLE=agent` and asserts `phaze.database` / `phaze.tasks.session` / `sqlalchemy.ext.asyncio` are absent from `sys.modules` (T-37-04).

## Task Commits

Each task was committed atomically (Task 1 followed RED → GREEN per `tdd="true"`):

1. **Task 1 (RED): failing hook tests** — `c880459` (test)
2. **Task 1 (GREEN): apply_stage_control hook + 5s TTL cache** — `524861e` (feat)
3. **Task 2: raw saq_jobs UPDATE service helpers** — `0241364` (feat)
4. **Task 3: register hook in build_pipeline_queue + import-boundary guard** — `e0ba3e3` (feat)

## Files Created/Modified

- `src/phaze/tasks/_shared/stage_control.py` *(modified)* — extended the Plan-01 constants module with `apply_stage_control` + TTL-cached `_read_stage_control`; updated `__all__` and the boundary-rule docstring.
- `src/phaze/services/stage_control.py` *(created)* — the three raw `saq_jobs` UPDATE helpers (allowlist-guarded, bound-param, sentinel-guarded resume).
- `src/phaze/tasks/_shared/queue_factory.py` *(modified)* — third `register_before_enqueue(apply_stage_control)` + three-hook docstrings.
- `tests/test_stage_control.py` *(created)* — 5 hook unit tests (stamp/park/passthrough/best-effort/TTL-cache) with a fake psycopg3 pool.
- `tests/test_task_split.py` *(modified)* — added `test_stage_control_stays_postgres_free`.
- `tests/test_queue_factory.py` *(modified)* — renamed/extended the hook-registration test to assert the third hook.

## Decisions Made

- **psycopg3-pool read, not SQLAlchemy** — the hook reads `pipeline_stage_control` through the queue's own open pool (`job.queue.pool`), the same pool SAQ's `_enqueue` uses. This is the load-bearing choice that keeps the agent worker (which registers the hook but never enqueues stage jobs) free of the ORM/DB layer.
- **TTL cache with a single window** — keyed by stage, dropped wholesale on expiry; 5s default per RESEARCH Open-Q2, documented as tunable. Bounded staleness is acceptable because the endpoints separately UPDATE the existing backlog; the hook only stamps NEW jobs.
- **`ValueError` on unknown stage** — raised before the prefix is built so no unvalidated value reaches SQL; the Plan-04 router will convert it to a 422.
- **No commit in the helpers** — the calling endpoint owns the transaction so the control-row ORM update and the backlog UPDATE land atomically.

## Deviations from Plan

None — plan executed exactly as written. (`test_queue_factory.py`'s hook check was already additive, as the plan anticipated; it was extended to assert the third hook and the test renamed `test_both_*` → `test_all_*` for accuracy.)

## Known Stubs

None — no placeholder/empty-data stubs introduced. Real-PG UPDATE/dequeue behavior of the helpers is intentionally proven by the integration tests in Plan 37-03 (per the plan's acceptance note); the unit layer here proves shape, allowlist, async, and bound-param contracts.

## Issues Encountered

None. All pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on every commit without `--no-verify`.

## User Setup Required

None — no new dependencies, env vars, or external services. The hook activates automatically once the code deploys (it is registered in the existing `build_pipeline_queue` seam).

## Next Phase Readiness

- Plan 37-03 can now write the real-PG integration tests (`tests/integration/test_stage_{pause,priority,resume,concurrency}.py`) against `pause_stage`/`resume_stage`/`set_stage_priority` and the live dequeue semantics.
- Plan 37-04 can wire the `POST /pipeline/stages/{stage}/{priority,pause,resume}` endpoints, calling the service helpers + updating the `PipelineStageControl` ORM row in one transaction.

## Verification

- `uv run pytest tests/test_stage_control.py tests/test_task_split.py tests/test_queue_factory.py` — 16 passed.
- `uv run python -c "from phaze.services.stage_control import set_stage_priority, pause_stage, resume_stage"` — succeeds; all three are coroutine functions.
- `apply_stage_control` registered after `apply_deterministic_key` in `queue_factory.py` (lines 68 → 74).
- `uv run ruff check` (touched files) clean; `uv run mypy src/phaze` clean (133 files).
- No `PostgresQueue.from_url` outside `build_pipeline_queue` (single seam).

## Self-Check: PASSED

- Created/modified files present: `src/phaze/tasks/_shared/stage_control.py`, `src/phaze/services/stage_control.py`, `tests/test_stage_control.py` — all FOUND.
- Commits present: `c880459`, `524861e`, `0241364`, `e0ba3e3` — all FOUND in git log.

---
*Phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker*
*Completed: 2026-06-13*
