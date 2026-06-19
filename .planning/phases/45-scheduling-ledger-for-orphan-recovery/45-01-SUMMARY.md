---
phase: 45-scheduling-ledger-for-orphan-recovery
plan: 01
subsystem: database
tags: [saq, postgres, sqlalchemy, alembic, before_enqueue, after_process, recovery, ledger]

# Dependency graph
requires:
  - phase: 35-deterministic-keys
    provides: "apply_deterministic_key before_enqueue chokepoint + increment_completed after_process hook"
  - phase: 36-postgres-broker
    provides: "PostgresQueue + build_pipeline_queue seam + cache_redis dynamic-attr idiom"
  - phase: 37-stage-control
    provides: "standalone-app-table template (pipeline_stage_control) + Postgres-free hook discipline"
provides:
  - "SchedulingLedger ORM model + reversible Alembic migration 022 (scheduling_ledger table)"
  - "control-only ledger service: upsert / insert-if-absent / clear / read + routing classifier"
  - "get_live_job_keys(session) -> degrade-safe set of queued/active saq_jobs keys"
  - "wired ledger WRITE hook (before_enqueue) + controller-stage CLEAR hook (after_process)"
  - "ledger_sessionmaker plumbed through build_pipeline_queue + AgentTaskRouter + controller + API lifespan"
affects: [45-02-agent-stage-clear, 45-03-recovery-rewrite, 45-04-startup-backfill]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ledger_sessionmaker dynamic queue attr (symmetric with cache_redis) read via getattr in the _shared hooks"
    - "function-local lazy import of the control-only ledger service so the agent _shared graph stays Postgres-free"
    - "upsert + insert-if-absent pair owned by Plan 01 so later plans add no new contract"

key-files:
  created:
    - src/phaze/models/scheduling_ledger.py
    - alembic/versions/022_add_scheduling_ledger.py
    - src/phaze/services/scheduling_ledger.py
    - tests/test_models/test_scheduling_ledger.py
    - tests/test_migrations/test_022.py
    - tests/test_services/test_scheduling_ledger.py
  modified:
    - src/phaze/models/__init__.py
    - src/phaze/services/pipeline.py
    - src/phaze/tasks/_shared/deterministic_key.py
    - src/phaze/tasks/_shared/queue_factory.py
    - src/phaze/tasks/controller.py
    - src/phaze/services/agent_task_router.py
    - src/phaze/main.py
    - tests/test_deterministic_key.py

key-decisions:
  - "Store routing ('agent'|'controller') explicitly on the ledger row, derived from enqueue_router.AGENT_TASKS/CONTROLLER_TASKS, for an explicit testable replay (no re-importing the routing sets into the recovery loop)."
  - "The WRITE/CLEAR hooks own their own short-lived session and commit; the service helpers never commit, so the Plan-04 backfill controls its own transaction boundary."
  - "Attach ledger_sessionmaker in the API lifespan (main.py) too, not just controller.py: manual DAG-trigger enqueues go through the API process's AgentTaskRouter, so without it the manual path would never write the ledger (Rule 2)."

patterns-established:
  - "Control-only service module boundary documented in the module docstring + enforced by the function-local lazy import at the only two call sites in _shared."
  - "Per-stage clear is ONE after_process hook gated on job.status in TERMINAL_STATUSES (clear-on-success == clear-on-terminal-failure), never on Status.QUEUED (retry)."

requirements-completed: [L-01, L-02, L-05, L-06]

# Metrics
duration: ~45min
completed: 2026-06-19
---

# Phase 45 Plan 01: Scheduling Ledger Foundation Summary

**Durable scheduling_ledger table + control-only service + the wired before_enqueue WRITE / controller after_process CLEAR hooks that record "a stage was scheduled for an item" at the single SAQ chokepoint while keeping the agent worker Postgres-free.**

## Performance

- **Duration:** ~45 min
- **Completed:** 2026-06-19
- **Tasks:** 3/3
- **Files modified:** 14 (6 created, 8 modified)

## Accomplishments

### Task 1 — SchedulingLedger model + reversible migration 022 (commit 4e261a2)
- `SchedulingLedger(TimestampMixin, Base)`: PK=`key` (String 255), `function`, `routing`, `payload` (JSONB), `enqueued_at` (server default), plus TimestampMixin. Plain index `ix_scheduling_ledger_function`. NO foreign keys (the row must survive a mid-flight target; the natural id lives inside `payload`). Registered in `models/__init__.py`.
- Migration `022` (`down_revision="021"`): `op.create_table(scheduling_ledger)` + function index; `downgrade()` drops both. Purely additive, no data step, and references no `saq_jobs` DDL (020 CRITICAL banner carried forward).
- Tests: model schema/PK/no-FK/index/round-trip; static revision-id asserts; a no-`saq_jobs` grep assertion; an integration upgrade-022 → assert table → INSERT round-trip → downgrade-drops sequence.

### Task 2 — Ledger service + get_live_job_keys (commit f144833)
- `src/phaze/services/scheduling_ledger.py` (control-only, documented boundary): `upsert_ledger_entry` (ON CONFLICT DO UPDATE, the WRITE-hook primitive), `insert_ledger_if_absent` (ON CONFLICT DO NOTHING, the Plan-04 backfill primitive — owned here so Plan 04 edits no Plan-01 test), `clear_ledger_entry` (DELETE, no-op if absent), `get_ledger_rows`, and `routing_for_function` (agent/controller classifier sourced from `enqueue_router` frozensets; raises `ValueError` on an unknown function).
- `get_live_job_keys(session)` added to `services/pipeline.py`: a SAVEPOINT-isolated (`begin_nested`) read of `saq_jobs` keys with status in (queued, active), degrading to an empty set on any DB error (clones the `get_stage_busy_counts` discipline verbatim).
- Tests: upsert idempotency, insert-if-absent non-overwrite + insert-when-missing, clear + clear-when-absent no-op, read-all, and the routing classifier (agent/controller/unknown).

### Task 3 — WRITE hook + controller CLEAR hook + queue ledger_sessionmaker (commit 82d1b1c)
- `apply_deterministic_key`: after `job.key` is finalized, a best-effort ledger upsert fires ONLY when `getattr(job.queue, "ledger_sessionmaker", None)` is present (control-side); the lazy function-local import keeps `phaze.services.scheduling_ledger` (and thus `phaze.models` / `sqlalchemy.ext.asyncio`) out of the agent `_shared` import graph. A ledger hiccup is logged, never raised.
- `increment_completed`: now clears the ledger on `job.status in TERMINAL_STATUSES` (COMPLETE/FAILED/ABORTED) and NOT on a `Status.QUEUED` retry; the existing COMPLETE completed-counter INCR is preserved. Same getattr + lazy-import + try/except no-op discipline; on the agent worker (no handle) the clear is a logged no-op (agent-stage clears are Plan 02's job).
- `build_pipeline_queue` gained an optional `ledger_sessionmaker` kwarg (TYPE_CHECKING-only `async_sessionmaker` import to stay agent-import-safe) that attaches `q.ledger_sessionmaker` when provided. `AgentTaskRouter` accepts + forwards it to every per-agent queue. `controller.startup` attaches it to the module-level controller queue (built before the engine exists) and passes it to the router; the API lifespan (`main.py`) does the same with `phaze.database.async_session`.
- Tests added to `tests/test_deterministic_key.py`: WRITE fires-with-handle / no-op-without / skip-non-keyed / failure-swallowed; CLEAR on COMPLETE/FAILED/ABORTED, no-clear on QUEUED, counter-still-fires on COMPLETE, failure-swallowed. The agent import-boundary test (`test_task_split.py`) stays green.

## Deviations from Plan

### Auto-added Critical Functionality

**1. [Rule 2 - Missing critical functionality] Wired ledger_sessionmaker into the API lifespan (main.py)**
- **Found during:** Task 3
- **Issue:** The plan's `files_modified` lists `controller.py` but not `main.py`. RESEARCH §1 establishes that manual DAG-trigger enqueues originate in the API process via its `AgentTaskRouter` + controller queue. Without attaching the ledger sessionmaker there, the manual trigger path would never write the ledger, so recovery could not distinguish "manually scheduled then lost" from "never scheduled" — defeating the phase goal for the manual path.
- **Fix:** Attached `phaze.database.async_session` to the API's controller queue (`build_pipeline_queue(..., ledger_sessionmaker=async_session)`) and to its `AgentTaskRouter(..., ledger_sessionmaker=async_session)`.
- **Files modified:** src/phaze/main.py
- **Commit:** 82d1b1c

### Tooling adjustments

**2. [Rule 3 - Blocking issue] `# noqa: PLC0415` on the two function-local ledger imports**
- The lazy function-local imports are mandatory (they keep the agent `_shared` graph Postgres-free, enforced by `test_task_split.py`), but ruff's `PLC0415` ("import should be at top-level") flags them. Added an inline `# noqa: PLC0415` with an explanatory comment on each import line. This is the load-bearing exception that makes the control/agent boundary work.

## Threat Mitigations Applied

- **T-45-02 (boundary):** ledger access only via `getattr(job.queue, "ledger_sessionmaker", None)` + a function-LOCAL lazy import that runs only control-side. `test_task_split.py` stays green (verified).
- **T-45-03 (DoS):** both hook sites wrap the ledger op in try/except that logs and never raises — a ledger hiccup degrades to "row not written/cleared", recovered by Plan 04 / next recovery.
- **T-45-04 (tampering):** migration 022 is purely additive DDL for `scheduling_ledger`; a test grep asserts the body contains no `saq_jobs` reference.

## Verification

- `uv run pytest tests/test_models/test_scheduling_ledger.py tests/test_services/test_scheduling_ledger.py tests/test_deterministic_key.py tests/test_task_split.py tests/test_migrations/test_022.py -q` → 54 passed (against the ephemeral test DB on :5433).
- Migration up/down round-trip verified on the ephemeral migrations DB (full create → INSERT → downgrade-drops).
- `uv run mypy .` → clean (159 files). `uv run ruff check .` → clean.
- Regression: `tests/test_services/test_agent_task_router.py`, `test_enqueue_router.py`, `tests/test_tasks/`, `tests/test_main_lifespan.py` all pass (187 tests) after the signature changes.
- Coverage on the new/modified core modules: scheduling_ledger model 100%, scheduling_ledger service 100%, deterministic_key 100%.

## Known Stubs

None — every helper and hook is fully wired. Agent-stage CLEAR (the four agent stages' terminal clears via the control-side callback handlers) is intentionally deferred to Plan 02 per the locked design; on the agent worker the after_process clear is a documented logged no-op, not a stub that breaks this plan's goal.

## Notes for Downstream Plans

- **Plan 02 (agent-stage clear):** add `clear_ledger_entry(session, "<function>:<file_id>")` in the existing control-side agent callback handlers (`agent_analysis.py` PUT + `/failed`, and the metadata/fingerprint/scan siblings) — the only path by which an agent terminal outcome becomes control-visible.
- **Plan 03 (recovery rewrite):** drive `recover_orphaned_work` off `get_ledger_rows(session)` minus `get_live_job_keys(session)`, replaying each row's stored `function` + `payload` through the existing keyed producers.
- **Plan 04 (startup backfill):** use `insert_ledger_if_absent` (already shipped here) to seed the ledger from live `saq_jobs` rows idempotently at boot.

## Self-Check: PASSED

All 7 created/key files exist on disk; all 3 task commits (4e261a2, f144833, 82d1b1c) are present in the worktree branch history.
