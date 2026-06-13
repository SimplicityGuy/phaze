---
phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker
verified: 2026-06-13T12:00:00Z
status: human_needed
score: 21/21 must-haves verified
overrides_applied: 0
human_verification:
  - test: "Live backlog reprioritization observed end-to-end on homelab"
    expected: "After deploying and enqueueing a stage backlog, POST a priority delta to /pipeline/stages/{stage}/priority and observe that lower-priority jobs dequeue sooner via /saq"
    why_human: "Requires a live running backlog on the homelab Postgres broker — cannot be simulated by the ephemeral integration-test DB"
  - test: "Pause across reboot re-applies to Phase-32 re-enqueued jobs"
    expected: "After pausing a stage, reboot the worker, and confirm Phase-32 re-enqueued jobs are re-parked (apply_stage_control hook reads live control state on enqueue)"
    why_human: "Requires a real reboot cycle on homelab and the Phase-32 re-enqueue path — no automated test exercises this sequence"
---

# Phase 37: Per-Stage Pause and Priority Control Plane Verification Report

**Phase Goal:** Add backend controls to pause and reprioritize the three agent pipeline stages — metadata (`extract_file_metadata`), analyze (`process_file`), fingerprint (`fingerprint_file`) — operating on the Postgres-backed `saq_jobs` table via plain UPDATEs.
**Verified:** 2026-06-13
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

All 21 code-level must-haves are VERIFIED. The two human items are deployment-confidence checks (homelab operational behavior), not code-correctness gaps. The phase goal is achieved at the code and integration-test level.

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | pipeline_stage_control table exists with exactly 3 seeded rows (metadata/analyze/fingerprint), paused=false, priority=50 | VERIFIED | `020_add_pipeline_stage_control.py`: `_SEED_STAGES = ("metadata", "analyze", "fingerprint")`, per-stage bound-param INSERT; `test_020.py` asserts count==3, paused==false, priority==50 |
| 2 | the priority column rejects any value outside 0..100 at the database layer (CHECK constraint) | VERIFIED | `PipelineStageControl.__table_args__` = `(CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),)`; migration uses `op.f("ck_pipeline_stage_control_priority_range")`; `test_020.py` asserts `pytest.raises(IntegrityError)` on priority=200 |
| 3 | STAGE_TO_FUNCTION maps the 3 stage labels to their registered SAQ function names and SENTINEL is one shared constant 9999999999 | VERIFIED | `stage_control.py:51-65`: `STAGE_TO_FUNCTION = {"metadata": "extract_file_metadata", "analyze": "process_file", "fingerprint": "fingerprint_file"}`, `SENTINEL: int = 9999999999` |
| 4 | the stage_control constants module imports cleanly with NO sqlalchemy.ext.asyncio / phaze.database import (agent-boundary-safe) | VERIFIED | `stage_control.py` imports only `time`, `structlog`, `TYPE_CHECKING`; `saq.Job` is TYPE_CHECKING-guarded; `test_task_split.py:302` subprocess test `test_stage_control_stays_postgres_free` asserts the forbidden modules absent from sys.modules |
| 5 | apply_stage_control stamps a new stage job with its stage's current priority, and sets scheduled=SENTINEL when the stage is paused | VERIFIED | `stage_control.py:117-141`: `job.priority = priority`; `if paused: job.scheduled = SENTINEL`; `test_stage_control.py` tests `test_stamp_sets_priority_leaves_scheduled` and `test_park_sets_priority_and_sentinel_when_paused` |
| 6 | non-stage jobs are left completely untouched by the hook | VERIFIED | `stage_control.py:131-133`: `stage = _FUNCTION_TO_STAGE.get(job.function); if stage is None: return`; `test_stage_control.py:test_passthrough_non_stage_job_untouched_and_no_read` asserts no read issued |
| 7 | a control-table read failure logs a warning and enqueues at default/unpaused — never raises | VERIFIED | `stage_control.py:134-138`: broad `except Exception` logs warning and returns; `test_stage_control.py:test_best_effort_read_failure_leaves_defaults` confirms no exception propagates |
| 8 | the hook reads control state via job.queue.pool (psycopg3) with a short-TTL in-process cache, never via SQLAlchemy | VERIFIED | `stage_control.py:95-113`: `async with queue.pool.connection() as conn: await conn.execute(...)` with `%(stage)s` psycopg3 paramstyle; `_CACHE_TTL_SECONDS = 5.0`, module-level cache; no SQLAlchemy import in this module |
| 9 | set_stage_priority / pause_stage / resume_stage issue the exact key-prefix-filtered, status-guarded, bound-param saq_jobs UPDATEs | VERIFIED | `services/stage_control.py:46-50`: `_SET_PRIORITY_SQL`, `_PAUSE_SQL`, `_RESUME_SQL` are static `text()` constants with `:p`/`:s`/`:pfx` bound params; `status = 'queued'` guard on all three |
| 10 | apply_stage_control is registered in build_pipeline_queue AFTER apply_deterministic_key, so all 4 construction sites inherit it | VERIFIED | `queue_factory.py:67-74`: `q.register_before_enqueue(apply_project_job_defaults)` then `q.register_before_enqueue(apply_deterministic_key)` then `q.register_before_enqueue(apply_stage_control)`; all 4 queue construction sites (`main.py:104`, `controller.py:158`, `agent_worker.py:204`, `agent_task_router.py:94`) use `build_pipeline_queue` |
| 11 | the new hook module stays out of the agent import boundary (no phaze.database / sqlalchemy.ext.asyncio) | VERIFIED | No `sqlalchemy`/`phaze.database`/`phaze.tasks.session` imports in `stage_control.py`; `test_stage_control_stays_postgres_free` subprocess test at `test_task_split.py:302` |
| 12 | pausing a stage parks its queued backlog (scheduled=SENTINEL) while an active job drains untouched | VERIFIED | `test_stage_pause.py:test_pause_parks_queued_backlog_and_drains_active`: real `pause_stage()` call; asserts queued rows `scheduled == SENTINEL`, active row status/scheduled unchanged |
| 13 | a paused stage's count('queued') drops to 0 while count('incomplete') is unchanged (Pitfall-1 semantic encoded) | VERIFIED | `test_stage_pause.py:86-87`: `assert await queue.count("queued") == 0` and `assert await queue.count("incomplete") == count_incomplete_before` |
| 14 | a lower priority value dequeues before a higher one after set_stage_priority; priority is clamped/bounded to a dequeueable range | VERIFIED | `test_stage_priority.py:test_set_stage_priority_reorders_backlog`: real `queue.dequeue()` confirms analyze job (priority 5) dequeues before comparison job (priority 30); `test_priority_below_zero_is_undequeueable_and_zero_is_the_floor` proves floor |
| 15 | resume un-parks only SENTINEL rows; a retry-backoff job (scheduled=now+delay) is left untouched | VERIFIED | `test_stage_resume.py:test_resume_unparks_sentinel_only_and_preserves_retry_backoff`: SENTINEL-parked rows reset to 0; retry-backoff row `scheduled == retry_backoff` unchanged |
| 16 | a concurrent admin UPDATE vs worker dequeue produces no double-pickup and no deadlock | VERIFIED | `test_stage_concurrency.py:test_concurrent_admin_update_vs_dequeue_no_double_pickup`: `asyncio.gather(admin_reprioritize(), queue.dequeue(...))` completes; asserts `len(active) == expected_active`, `len(active) + len(queued) == len(keys)` |
| 17 | POST /pipeline/stages/{stage}/priority applies a clamped delta and reorders the queued backlog, returning {stage, priority, paused} | VERIFIED | `pipeline_stages.py:83-96`: `new_priority = max(0, min(100, row.priority + body.delta))`; calls `set_stage_priority`; returns `_response(row)`; `test_stage_endpoints.py:test_priority_clamps_high/low/valid_delta` |
| 18 | POST /pipeline/stages/{stage}/pause sets paused=true and parks the queued backlog; returns {stage, priority, paused} | VERIFIED | `pipeline_stages.py:99-110`: `row.paused = True`; calls `pause_stage`; commits; returns `_response(row)`; `test_stage_endpoints.py:test_pause_then_resume_flip_and_persist_paused` |
| 19 | POST /pipeline/stages/{stage}/resume sets paused=false and un-parks only SENTINEL rows; returns {stage, priority, paused} | VERIFIED | `pipeline_stages.py:113-124`: `row.paused = False`; calls `resume_stage`; commits; returns `_response(row)`; `test_stage_endpoints.py:test_pause_then_resume_flip_and_persist_paused` |
| 20 | an unknown stage returns 422 (validated against the metadata/analyze/fingerprint allowlist) | VERIFIED | `pipeline_stages.py:54-57`: `_validate_stage` raises `HTTPException(status_code=422, detail="unknown stage")` when `stage not in STAGE_TO_FUNCTION`; `test_stage_endpoints.py:test_unknown_stage_returns_422` |
| 21 | a priority delta is clamped to [0,100] before the control row and backlog are updated | VERIFIED | `pipeline_stages.py:92`: `new_priority = max(_PRIORITY_MIN, min(_PRIORITY_MAX, row.priority + body.delta))`; `test_stage_endpoints.py:test_priority_clamps_high` (delta 100 → priority 100) and `test_priority_clamps_low` (delta -100 → priority 0) |

**Score:** 21/21 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/models/pipeline_stage_control.py` | PipelineStageControl ORM model (stage PK, paused, priority SmallInteger, CHECK 0-100) | VERIFIED | 36 lines; `stage` PK String(32), `paused` Boolean server_default false, `priority` SmallInteger server_default 50, `CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range")` |
| `alembic/versions/020_add_pipeline_stage_control.py` | Migration 020 creating + seeding the control table, revises 019 | VERIFIED | `revision = "020"`, `down_revision = "019"`; creates table with PK + CHECK; seeds 3 rows via bound-param INSERT; never references `saq_jobs`; `downgrade` = `op.drop_table` |
| `src/phaze/tasks/_shared/stage_control.py` | STAGE_TO_FUNCTION / _FUNCTION_TO_STAGE / SENTINEL constants + apply_stage_control hook + TTL cache | VERIFIED | 144 lines; all constants present; `apply_stage_control` hook; `_read_stage_control` with 5s TTL cache; WR-01 null-row guard at line 101-107 |
| `src/phaze/services/stage_control.py` | set_stage_priority / pause_stage / resume_stage raw saq_jobs UPDATE helpers | VERIFIED | 82 lines; all three helpers as async functions; static `text()` SQL constants; `_key_prefix` allowlist validation; sentinel-guarded `_RESUME_SQL` |
| `src/phaze/routers/pipeline_stages.py` | The 3 control endpoints (priority/pause/resume) wired to service helpers + ORM control row | VERIFIED | 125 lines; 3 POST endpoints; `_validate_stage` (422); `_load_control_row` with `lock=True` for priority (WR-02 fix); single `session.commit()` per endpoint |
| `src/phaze/schemas/pipeline_stages.py` | StagePriorityDelta request body | VERIFIED | 17 lines; `class StagePriorityDelta(BaseModel): delta: int` |
| `tests/test_migrations/test_020.py` | Real-PG upgrade/downgrade + seed + CHECK proof for migration 020 | VERIFIED | 105 lines; `test_revision_identifiers_are_bare_numbers` (no-DB); `test_upgrade_020_creates_seeds_and_check_then_downgrade_drops` (real-PG) |
| `tests/integration/test_stage_pause.py` | REQ-37-1 drain-pause + Pitfall-1 count semantics on real PG | VERIFIED | `pytestmark = pytest.mark.integration`; real `PostgresQueue` via `build_pipeline_queue`; Pitfall-1 count assertion at lines 86-87 |
| `tests/integration/test_stage_priority.py` | REQ-37-2 live reorder + clamp on real PG | VERIFIED | `pytestmark = pytest.mark.integration`; 2 tests; real `queue.dequeue()` for order assertion; priority -1 un-dequeueable proven |
| `tests/integration/test_stage_resume.py` | REQ-37-3 sentinel-guarded resume preserving retry backoff | VERIFIED | `pytestmark = pytest.mark.integration`; real `now_seconds() + 3600` retry-backoff row unchanged after `resume_stage` |
| `tests/integration/test_stage_concurrency.py` | REQ-37-4 no-double-pickup / no-deadlock under concurrent dequeue | VERIFIED | `pytestmark = pytest.mark.integration`; `asyncio.gather(admin_reprioritize(), queue.dequeue(...))` race; row-count conservation + at-most-one-active assertions |
| `tests/test_stage_control.py` | Hook unit tests (fake queue/pool): stamp, park, passthrough, best-effort, TTL-cache | VERIFIED | 5 tests exactly matching the 5 Plan-02 behaviors; fake psycopg3 pool via `SimpleNamespace` |
| `tests/test_routers/test_stage_endpoints.py` | Endpoint validation / clamp / return-shape unit tests (httpx AsyncClient) | VERIFIED | 5 tests: unknown→422, clamp-high→100, clamp-low→0, valid-delta persists, pause+resume flip+persist |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/models/__init__.py` | `phaze.models.pipeline_stage_control.PipelineStageControl` | import + `__all__` | WIRED | Line 11: `from phaze.models.pipeline_stage_control import PipelineStageControl`; line 30: `"PipelineStageControl"` in `__all__` |
| `alembic/versions/020_add_pipeline_stage_control.py` | migration 019 | `down_revision = "019"` | WIRED | Line 36: `down_revision: str | Sequence[str] | None = "019"` |
| `src/phaze/tasks/_shared/queue_factory.py` | `apply_stage_control` | `q.register_before_enqueue(apply_stage_control)` after `apply_deterministic_key` | WIRED | Lines 67-74: three hooks registered in order; `apply_stage_control` is third |
| `src/phaze/tasks/_shared/stage_control.py apply_stage_control` | `pipeline_stage_control` table | `job.queue.pool.connection()` raw SELECT (psycopg3, TTL-cached) | WIRED | Lines 95-100: `async with queue.pool.connection() as conn: await conn.execute("SELECT paused, priority ... WHERE stage = %(stage)s", {"stage": stage})` |
| `src/phaze/services/stage_control.py` | `saq_jobs (key LIKE '<fn>:%')` | `session.execute(text(...))` bound-param UPDATE | WIRED | Lines 46-50: three static SQL constants filtering `key LIKE :pfx` with `status = 'queued'` guard |
| `src/phaze/routers/pipeline_stages.py` | `phaze.services.stage_control` | `await set_stage_priority/pause_stage/resume_stage(session, stage, ...)` then `session.commit()` | WIRED | Lines 39, 94, 108, 122: imported and called in each endpoint |
| `src/phaze/main.py` | `phaze.routers.pipeline_stages.router` | `app.include_router(pipeline_stages.router)` | WIRED | Line 36: import; line 182: `app.include_router(pipeline_stages.router)` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `pipeline_stages.py / set_priority` | `row.priority`, `row.paused` | `session.get(PipelineStageControl, stage, with_for_update=True)` + `set_stage_priority(session, stage, new_priority)` | Yes — ORM read + raw saq_jobs UPDATE | FLOWING |
| `stage_control.py / apply_stage_control` | `paused, priority` | `_read_stage_control(job.queue, stage)` via psycopg3 pool SELECT | Yes — live DB read from `pipeline_stage_control` | FLOWING |
| `services/stage_control.py` helpers | saq_jobs rows affected | `session.execute(_SET_PRIORITY_SQL/_PAUSE_SQL/_RESUME_SQL, {...})` | Yes — raw UPDATE on real saq_jobs table | FLOWING |

### Behavioral Spot-Checks

Step 7b SKIPPED for integration tests (require running Postgres on 5433 — cannot start services). Targeted static checks used instead:

| Behavior | Check | Result | Status |
|----------|-------|--------|--------|
| Routes mounted on app | `grep "include_router.*pipeline_stages" main.py` | Line 182 found | PASS |
| apply_stage_control registered as 3rd hook | `grep -A8 "register_before_enqueue" queue_factory.py` | 3 calls in order; `apply_stage_control` is last | PASS |
| Sentinel-guarded resume SQL | `grep "AND scheduled = :s" services/stage_control.py` | Found in `_RESUME_SQL` at line 50 | PASS |
| No saq_jobs reference in migration 020 | `grep "saq_jobs" 020_add_pipeline_stage_control.py` | Only in comments (docstring), never in DDL/DML | PASS |
| Post-review fixes applied | `grep "with_for_update\|row is None" pipeline_stages.py stage_control.py` | Both WR-01 null-guard and WR-02 FOR UPDATE present | PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` probes found for this phase. VALIDATION.md specifies automated test commands. Phase declares full suite passes at 1739 tests via `just integration-test` (commit `b9180b4` — code review documentation confirms this). Probe execution SKIPPED: probe files not present and re-running the 1739-test suite is explicitly scoped out in the task prompt.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| REQ-37-1 | 37-01, 37-02, 37-03 | Drain-style pause: active jobs finish, queued backlog parks at SENTINEL | SATISFIED | `pause_stage()` with `status='queued'` guard; `apply_stage_control` parks on enqueue; `test_stage_pause.py` drain + Pitfall-1 count assertions |
| REQ-37-2 | 37-01, 37-02, 37-03 | Live backlog reprioritization per agent stage | SATISFIED | `set_stage_priority()` raw UPDATE; `apply_stage_control` stamps new jobs; `test_stage_priority.py` dequeue-order + floor assertions |
| REQ-37-3 | 37-02, 37-03 | Retry backoffs preserved / sentinel-guarded resume | SATISFIED | `_RESUME_SQL` with `AND scheduled = :s` (SENTINEL) guard; `test_stage_resume.py` confirms retry-backoff row untouched |
| REQ-37-4 | 37-03 | No double-pickup under concurrent admin UPDATE vs dequeue | SATISFIED | `status='queued'` guard + Postgres `FOR UPDATE SKIP LOCKED`; `test_stage_concurrency.py` race proves no double-pickup, no deadlock |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | — | — | — | No TBD/FIXME/XXX/TODO/PLACEHOLDER markers found in any phase-37 source files |

No stub patterns (`return {}`, `return []`, `return null`) found in production source. No hardcoded empty props. No console.log equivalents. No orphaned functions. All service helpers take real `AsyncSession` and issue real SQL.

### Code Review Findings (post-phase)

37-REVIEW.md (commit `b9180b4`) found 0 critical / 2 warning / 1 info findings, all resolved in `5da762e`:

- **WR-01 (RESOLVED):** `_read_stage_control` null-check for missing control row (`row is None` guard at `stage_control.py:101-107`) — prevents misleading "read failed" log when migration 020 hasn't run.
- **WR-02 (RESOLVED):** Priority endpoint now fetches control row `FOR UPDATE` (`_load_control_row(..., lock=True)` in `pipeline_stages.py:71`) — prevents lost delta under concurrent requests.
- **IN-01 (RESOLVED):** `_FUNCTION_TO_STAGE` removed from `__all__` — `stage_control.py:144` confirms `__all__ = ["SENTINEL", "STAGE_TO_FUNCTION", "apply_stage_control"]`.

All three fixes are verified in the actual source files.

### Human Verification Required

#### 1. Live backlog reprioritization on homelab

**Test:** Deploy the phase, enqueue a real stage backlog on the homelab Postgres broker, then POST a priority delta (`POST /pipeline/stages/analyze/priority` with `{"delta": -10}`) and observe via `/saq` that lower-priority jobs dequeue sooner.
**Expected:** Jobs whose `saq_jobs.priority` column was lowered appear at the top of the SAQ web UI queue and are picked up first by the agent worker.
**Why human:** Requires a live running backlog on the homelab Postgres broker with real agent workers consuming jobs. The integration tests prove the SQL semantics but not the end-to-end deployed behavior.

#### 2. Pause across reboot re-applies to Phase-32 re-enqueued jobs

**Test:** Pause a stage (e.g., `POST /pipeline/stages/analyze/pause`), reboot the phaze-api and phaze-worker containers, then confirm that Phase-32's reboot re-enqueue path re-parks the jobs (because `apply_stage_control` reads live control state on every enqueue and the `paused=true` row persists in `pipeline_stage_control`).
**Expected:** After reboot, re-enqueued analyze jobs have `scheduled = SENTINEL` in `saq_jobs` and do not dequeue until `POST /pipeline/stages/analyze/resume` is called.
**Why human:** Requires a real reboot cycle on homelab, the Phase-32 re-enqueue path, and homelab Postgres access — not reproducible in a local ephemeral test environment.

### Gaps Summary

No gaps found. All code-level must-haves are fully verified. The two human items are deployment-confidence checks per the VALIDATION.md "Manual-Only Verifications" section; they are not code correctness gaps.

---

_Verified: 2026-06-13_
_Verifier: Claude (gsd-verifier)_
