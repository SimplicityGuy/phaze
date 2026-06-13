---
phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
plan: 02
subsystem: tasks
tags: [saq, postgres, psycopg3, redis, queue, pool, cache-decoupling]

# Dependency graph
requires:
  - phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
    plan: 01
    provides: build_pipeline_queue factory (PostgresQueue + both hooks + cache_redis) and PHAZE_QUEUE_URL config
provides:
  - "All four queue-construction sites build a PostgresQueue via build_pipeline_queue (no Queue.from_url(redis_url) remains)"
  - "Cache plane fully decoupled: generate_proposals rate-limits on ctx['redis']; counter hooks read getattr(job.queue, 'cache_redis', None); no queue.redis read anywhere"
  - "AgentTaskRouter(queue_url, cache_redis_url) constructor (Postgres broker + Redis cache)"
  - "Producer-side Postgres pools opened (idempotent connect()) at the enqueue chokepoints + API lifespan"
affects: [36-03, pipeline-queue-construction-sites, worker-startup, api-enqueue-path]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Single construction seam adopted at every site: build_pipeline_queue owns backend + pool sizing + hook chain"
    - "Cache plane decoupled from the now-Postgres broker: ctx['redis'] for proposals, queue.cache_redis for counters"
    - "Producer-side PostgresQueue pools opened via idempotent connect() at enqueue chokepoints (the open=False pool does not auto-connect like the old Redis client did)"

key-files:
  created: []
  modified:
    - src/phaze/tasks/controller.py
    - src/phaze/tasks/agent_worker.py
    - src/phaze/main.py
    - src/phaze/services/agent_task_router.py
    - src/phaze/services/enqueue_router.py
    - src/phaze/services/analysis_enqueue.py
    - src/phaze/tasks/proposal.py
    - src/phaze/tasks/_shared/deterministic_key.py
    - src/phaze/routers/pipeline.py
    - tests/_queue_fakes.py
    - tests/test_deterministic_key.py
    - tests/test_tasks/test_proposal.py
    - tests/test_services/test_agent_task_router.py
    - tests/test_tasks/test_reenqueue.py
    - tests/test_main_lifespan.py
    - tests/test_phase04_gaps.py

key-decisions:
  - "Opened producer-side Postgres pools with idempotent connect() at the enqueue chokepoints (enqueue_router, enqueue_process_file, enqueue_for_agent) + API lifespan — the open=False pool does NOT auto-connect like the old redis-backed Queue, so the first API enqueue would otherwise raise PoolClosed (Rule 2 deviation; pool-open was scoped to this plan per 36-01 summary but under-specified in the task text)"
  - "Moved saq.Queue into a TYPE_CHECKING block in agent_task_router.py (now annotation-only after the factory swap) — safe under `from __future__ import annotations`"
  - "Updated two integration tests (test_agent_task_router, test_reenqueue) and four lifespan/router tests (test_main_lifespan, test_phase04_gaps) broken by the constructor signature + factory swap (Rule 3)"

patterns-established:
  - "Producer enqueue paths call `await queue.connect()` (idempotent) before enqueueing a PostgresQueue"
  - "Integration tests derive the libpq broker DSN from TEST_DATABASE_URL by stripping the +asyncpg dialect suffix"

requirements-completed: [REQ-36-1, REQ-36-3, REQ-36-5]

# Metrics
duration: 40min
completed: 2026-06-12
---

# Phase 36 Plan 02: Atomic Queue-Construction Swap + Cache Decoupling Summary

**Routed all four queue-construction sites through `build_pipeline_queue` (Postgres backend) and, in the same change, repointed every `queue.redis` cache reader at the decoupled `cache_redis` / `ctx["redis"]` handle — then opened the producer-side psycopg pools so API enqueues do not hit a `PoolClosed` on the `open=False` pool.**

## Performance

- **Duration:** ~40 min
- **Completed:** 2026-06-12
- **Tasks:** 2 (plus one required cross-cutting pool-open deviation)
- **Files modified:** 16 (6 source non-test in scope + 2 deviation-source + 8 tests)

## Accomplishments

- **Construction swap (Task 1):** `controller.py`, `agent_worker.py`, `main.py`, and `agent_task_router.py` build a `PostgresQueue` via the Plan-01 factory (`build_pipeline_queue(name, queue_url, cache_redis_url=redis_url, ...)`) with conservative per-queue pool sizing (control 2/8, agent + per-agent 1/4). No `Queue.from_url(redis_url)` remains anywhere; `grep -rn "Queue.from_url" src/phaze` shows only the factory's `PostgresQueue.from_url`.
- `AgentTaskRouter.__init__` now takes `(queue_url, cache_redis_url)`; both callers (`controller.py`, `main.py`) updated. The duplicated `before_enqueue` hook registration was dropped (the factory owns the chain), and the unused `from saq import Queue` imports removed.
- A dedicated cache-Redis client is wired into the control + agent worker contexts as `ctx["redis"]` and closed in shutdown (mirrors the discogs_client lifecycle).
- **Cache decoupling (Task 2):** `generate_proposals` rate-limits on `ctx["redis"]`; both counter hooks in `deterministic_key.py` read `getattr(job.queue, "cache_redis", None)`; the dead `controller_queue.redis` counter fallback in `pipeline.py` is gone. No code reads `queue.redis` (remaining matches are docstrings/comments).
- The `tests/test_no_default_queue_producers.py` static + runtime guard stays green; the factory's named `PostgresQueue.from_url(..., name=...)` is not a default-queue producer.

## Task Commits

1. **Task 1: Route construction sites through factory + open producer pools** — `5a7cbc3` (feat)
2. **Task 2: Repoint cache readers off queue.redis to cache_redis** — `887fc1e` (fix)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical functionality] Open producer-side Postgres pools**
- **Found during:** Task 1 verification (integration enqueue tests raised `psycopg_pool.PoolClosed: the pool is not open yet`).
- **Issue:** The factory builds the `PostgresQueue` pool with `open=False` and, unlike the old redis-backed `Queue` (which auto-connects on first command), it does NOT auto-connect on enqueue. Every producer-side enqueue (API `controller_queue`, per-agent router queues) would raise `PoolClosed` in production. 36-01's summary scoped pool open/close wiring to this plan, but the Task-1 action text only spelled out the cache client + `disconnect()` half.
- **Fix:** Added idempotent `await queue.connect()` (guarded by SAQ's `self._connected`) at the enqueue chokepoints — `services/enqueue_router.resolve_queue_for_task`, `services/analysis_enqueue.enqueue_process_file`, `AgentTaskRouter.enqueue_for_agent` — and in the API lifespan for `controller_queue` and each `/saq`-mount per-agent queue.
- **Files modified:** `main.py`, `services/agent_task_router.py`, `services/enqueue_router.py`, `services/analysis_enqueue.py`
- **Commit:** `5a7cbc3`

**2. [Rule 3 - Blocking] Update tests broken by the constructor signature + factory swap**
- **Found during:** Full-suite verification.
- **Issue:** Tests patched the now-removed `phaze.main.Queue` / asserted `Queue.from_url` call shape, constructed `AgentTaskRouter(redis_url=...)`, and asserted hook registration that the lifespan no longer performs.
- **Fix:** `test_main_lifespan.py` + `test_phase04_gaps.py` patch `build_pipeline_queue`, assert the queue name as the first positional arg, add `connect()` doubles, and drop the hook-registration asserts (now owned by `test_queue_factory.py`). `test_services/test_agent_task_router.py` + `test_tasks/test_reenqueue.py` use the two-arg constructor with a libpq broker DSN derived from `TEST_DATABASE_URL` and probe the Postgres broker. `tests/_queue_fakes.py` gained a `cache_redis` FakeRedis double + a no-op async `connect()`; `stub_app_state` sentinels gained an async `connect`.
- **Files modified:** `tests/test_main_lifespan.py`, `tests/test_phase04_gaps.py`, `tests/test_services/test_agent_task_router.py`, `tests/test_tasks/test_reenqueue.py`, `tests/_queue_fakes.py`
- **Commit:** `5a7cbc3` (router/lifespan tests) + `887fc1e` (`_queue_fakes` cache_redis half)

**3. [Rule 3 - Blocking] Move `saq.Queue` into TYPE_CHECKING (agent_task_router.py)**
- **Issue:** After the factory swap `saq.Queue` is annotation-only; ruff `TC002` flagged it.
- **Fix:** Moved the import into the existing `TYPE_CHECKING` block (safe — module uses `from __future__ import annotations`).
- **Commit:** `5a7cbc3`

## Authentication Gates

None.

## Threat Surface

- **T-36-01 (DoS / silent counter failure):** mitigated — cache readers repointed to `ctx["redis"]` / `job.queue.cache_redis`; no reader borrows the now-Postgres broker's (nonexistent) `.redis`.
- **T-36-04 (DoS / connection exhaustion):** mitigated — conservative per-queue pool sizing (control 2/8, agent + per-agent 1/4) preserved at every construction site. NOTE: the pool-open deviation now opens the controller pool + one pool per non-revoked agent at API startup (for the `/saq` mount), and per-agent pools on first enqueue — the connection budget vs Postgres `max_connections` is the homelab concern documented in Plan 04's change prompt.
- **T-36-06 (Tampering / regression):** mitigated — `tests/test_no_default_queue_producers.py` stays green; the factory uses a named `PostgresQueue.from_url(..., name=...)`.

No new security surface beyond the plan's threat register.

## Known Stubs

None.

## Verification

- `uv run ruff check src/phaze tests` → clean; `uv run mypy src/phaze` → no issues (130 files)
- `grep -rn "Queue.from_url" src/phaze` → only the factory's `PostgresQueue.from_url`
- `grep` for queue-object `.redis` reads → only docstrings/comments remain (no code reads)
- Full suite against the ephemeral Postgres+Redis harness (`just test-db`): **1716 passed, 0 failed** (the plan's verification set — `test_deterministic_key`, `test_tasks/test_proposal`, `test_no_default_queue_producers`, `test_task_split`, `test_routers` — all green; the two live-broker integration files + the six lifespan tests updated for the swap also pass).

## Next Phase Readiness

- All producer + worker queues are Postgres-backed via the single factory, cache fully decoupled, and producer pools open correctly. Plan 03 covers the `/saq` mount + import-boundary test adjustments; Plan 04 is the homelab change prompt (PHAZE_QUEUE_URL, connection budget, cutover).
- Blockers: none.

## Self-Check: PASSED

- All modified files exist on disk (verified below).
- Both task commits present in git log (`5a7cbc3`, `887fc1e`).

---
*Phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq*
*Completed: 2026-06-12*
