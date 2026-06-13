---
phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
plan: 03
subsystem: tests
tags: [saq, postgres, integration-test, priority, dedup, import-boundary, saq-web]

# Dependency graph
requires:
  - phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
    plan: 02
    provides: All queue-construction sites build a PostgresQueue via build_pipeline_queue; cache decoupled to cache_redis/ctx['redis']
provides:
  - "Real-PG integration proof: lower-priority int dequeues first + future-scheduled jobs park (REQ-36-2)"
  - "Real-PG integration proof: in-flight deterministic key re-enqueue returns None (ON CONFLICT no-op), re-enqueues after completion (REQ-36-3)"
  - "tests/integration/ package auto-marked 'integration' so 'pytest -m not integration' stays green offline"
  - "/saq monitor renders over a real PostgresQueue.info() (REQ-36-4 / T-36-07 regression guard)"
  - "agent_worker import boundary: forbids sqlalchemy.ext.asyncio AND positively requires saq.queue.postgres under PHAZE_QUEUE_URL (T-36-08 / Phase-26 D-25)"
affects: [36-04, ci-integration-suite]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Real-PostgresQueue integration test harness: PostgresQueue.from_url(libpq DSN) + connect() (init_db creates saq_jobs) + per-test-unique queue name + best-effort row cleanup + connectivity-probe skip"
    - "Backend-agnostic /saq render proof: construct PostgresQueue open=False, patch .info() to the canonical QueueInfo mapping, drive saq_web /api/queues — no live DB needed"
    - "Import-boundary test now asserts BOTH directions: ORM async engine absent AND psycopg3 broker (saq.queue.postgres) present"

key-files:
  created:
    - tests/integration/__init__.py
    - tests/integration/test_pg_queue_priority.py
    - tests/integration/test_pg_dedup.py
  modified:
    - tests/conftest.py
    - tests/test_web/test_saq_mount.py
    - tests/test_task_split.py

key-decisions:
  - "Built real PostgresQueue.from_url(libpq DSN) directly (NOT via build_pipeline_queue) in the priority/dedup tests so the dequeue/ON-CONFLICT behaviors are observed with no before_enqueue hook interference — the acceptance criterion is 'a real PostgresQueue (not a fake)', which from_url satisfies; the factory's hooks are covered by test_queue_factory + the live router tests"
  - "Proved scheduled-park deterministically WITHOUT a real-time wait: a ready (scheduled<=now) higher-priority sibling dequeues ahead of a future-scheduled lower-priority job, and a second dequeue against the still-parked job returns None — avoids a flaky sleep-until-scheduled"
  - "Re-enqueue-after-completion uses finish(Status.COMPLETE) (default ttl 600 keeps the row 'complete') + an explicitly larger scheduled, so the test exercises the ON CONFLICT terminal-status UPDATE path rather than a fresh INSERT into a vacated key"
  - "saq_mount render test patches PostgresQueue.info to the QueueInfo mapping (open=False, no connection) AND separately asserts the genuine PostgresQueue.info is an async coroutine function — proving mount wiring + backend-agnostic shape with zero live Postgres"

requirements-completed: [REQ-36-2, REQ-36-4]

# Metrics
duration: 35min
completed: 2026-06-12
---

# Phase 36 Plan 03: Real-Postgres Behavior + Regression-Surface Tests Summary

**Proved the migration-target behaviors that are only observable against a live Postgres broker — native `ORDER BY priority, scheduled` dequeue ordering with the `now >= scheduled` park gate (REQ-36-2) and the `ON CONFLICT (key)` in-flight dedup no-op (REQ-36-3) — plus the two regression surfaces this migration most threatens: the `/saq` monitor rendering over `PostgresQueue.info()` (REQ-36-4) and the agent import boundary staying ORM-engine-free under the psycopg3 broker (Phase-26 D-25).**

## Performance

- **Duration:** ~35 min
- **Completed:** 2026-06-12
- **Tasks:** 2
- **Files:** 6 (3 new integration tests + 3 modified)

## Accomplishments

- **Task 1 — real-PG priority/dedup (REQ-36-2/3):**
  - `tests/integration/test_pg_queue_priority.py`: enqueues priorities {50, 10, 90} against a real `PostgresQueue` and asserts the dequeue order is 10 → 50 → 90 (lower int first); a second test enqueues a future-`scheduled` (now+3600) low-priority job alongside a ready high-priority job and asserts the ready one dequeues first while the parked job stays parked (a follow-up dequeue returns `None`).
  - `tests/integration/test_pg_dedup.py`: enqueues a `process_file:<id>` key twice and asserts the second returns `None` (in-flight `ON CONFLICT` no-op); a second test `finish()`es the job to `COMPLETE` then re-enqueues with a strictly-greater `scheduled` and asserts it lands again — the exact `reenqueue_discovered` skipped-then-re-enqueue contract.
  - `tests/integration/__init__.py` documents the package; `tests/conftest.py`'s auto-marker path rule now matches `tests/integration/` so `pytest -m 'not integration'` excludes them offline (verified: 4 deselected).
- **Task 2 — regression surfaces (REQ-36-4 + import boundary):**
  - `tests/test_web/test_saq_mount.py`: new `test_mount_renders_over_postgres_queue_info` constructs a real `PostgresQueue` (`open=False`, no connection), patches `.info()` to the canonical `QueueInfo` mapping, mounts `build_saq_app`, and asserts `/saq/api/queues` renders the queue (name + counts) via the PASSED PostgresQueue instance's `.info()` — plus a parity assertion that the genuine `PostgresQueue.info` is an async coroutine function.
  - `tests/test_task_split.py`: the `agent_worker` import-boundary subprocess now sets `PHAZE_QUEUE_URL`, keeps the `sqlalchemy.ext.asyncio` forbidden assertion, and adds a positive assertion that `saq.queue.postgres` IS in the import graph — proving the psycopg3 broker is wired without smuggling the ORM async engine into the agent role.

## Task Commits

1. **Task 1: real-PG priority/scheduled-park + dedup integration tests** — `73d8c26` (test)
2. **Task 2: /saq mount + agent import boundary under Postgres broker** — `c3d978d` (test)

## Deviations from Plan

None — both tasks executed as written. (No production code changed: SAQ's `PostgresQueue` and Waves 1+2 already implement the behaviors; these tests pin them.)

## Authentication Gates

None.

## Threat Surface

- **T-36-07 (Tampering / `/saq` monitor regression):** mitigated — `test_saq_mount.py` now asserts the dashboard renders over a real `PostgresQueue.info()`, catching a backend-specific monitor break before deploy.
- **T-36-08 (Elevation / boundary erosion):** mitigated — `test_task_split.py` keeps `sqlalchemy.ext.asyncio` forbidden and positively requires the psycopg3 broker, so the Postgres backend cannot smuggle the ORM engine into the agent role.
- **T-36-09 (DoS / offline test break):** accepted as designed — the auto-marker path rule excludes `tests/integration/` from `pytest -m 'not integration'`, keeping CI offline runs green while real-PG runs stay gated to `just integration-test`.

No new security surface beyond the plan's threat register.

## Known Stubs

None.

## Verification

- `just test-db` (ephemeral PG 5433 + Redis 6380): new integration tests **4 passed**; combined with the extended unit tests and the adjacent live-broker tests (`test_reenqueue`, `test_agent_task_router`) **29 passed**.
- `tests/test_web/test_saq_mount.py` + `tests/test_task_split.py`: **11 passed** (DB-free + subprocess boundary).
- `uv run pytest -m 'not integration' tests/integration` → **4 deselected** (marker excludes them offline).
- `uv run ruff check` + `uv run ruff format --check` on all touched files: clean. mypy (excludes `tests/`): pre-commit `mypy` hook **Passed** on both commits.

## Environment Note

The host disk hit 100% mid-run, which surfaced as a spurious mypy `INTERNAL ERROR` (`sqlite3.OperationalError: database or disk is full` writing `.mypy_cache`). Reclaimed ~1.4 GiB safely via `uv cache clean` (re-downloadable; does not touch the materialized `.venv`); the pre-commit `mypy` hook then passed normally. No code or config change was needed.

## Next Phase Readiness

- REQ-36-2/3 are now proven against a real Postgres broker and correctly gated behind the `integration` marker; REQ-36-4 and the agent import boundary are green under the Postgres broker. The migration's behavioral and regression contracts are locked.
- Plan 36-04 (docs / homelab change prompt) runs in parallel; this plan touches no shared orchestrator artifacts (STATE.md / ROADMAP.md left to the orchestrator).
- Blockers: none.

## Self-Check: PASSED

- All created/modified files exist on disk (verified below).
- Both task commits present in git log (`73d8c26`, `c3d978d`).

---
*Phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq*
*Completed: 2026-06-12*
