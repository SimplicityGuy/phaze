---
phase: 49-duration-routing-backfill
plan: 04
subsystem: tasks
tags: [saq, cron, routing, postgres, recovery]

# Dependency graph
requires:
  - phase: 49-duration-routing-backfill
    plan: 01
    provides: "FileState.AWAITING_CLOUD, select_active_agent(kind='compute'), get_files_by_state, seed_active_agent(kind=...)"
  - phase: 48-compute-agent-type
    provides: "Agent.kind column (fileserver/compute)"
provides:
  - "release_awaiting_cloud(ctx) state-driven held-file release producer (AWAITING_CLOUD -> compute queue + reset to DISCOVERED)"
  - "controller CronJob(release_awaiting_cloud, '*/5 * * * *') — a NARROW recovery-only drain (NOT a general auto-advance)"
  - "D-04 regression guard: AWAITING_CLOUD is not analyze-done and not domain-completed for process_file"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "State-driven (not ledger-driven) recovery cron for the held set — held files have no scheduling-ledger row, so recover_orphaned_work cannot see them"
    - "Reuse the shared enqueue_process_file producer so the release path emits the IDENTICAL process_file:<id> deterministic key (cross-path dedup, no drift)"
    - "Reset to DISCOVERED on EVERY outcome (even a dedup no-op) so the released file leaves the scanned set and the dashboard held-count stays honest (D-03a)"

key-files:
  created:
    - src/phaze/tasks/release_awaiting_cloud.py
    - tests/test_tasks/test_release_awaiting_cloud.py
  modified:
    - src/phaze/tasks/controller.py
    - tests/test_tasks/test_recovery.py
    - tests/test_tasks/test_controller_reenqueue.py

key-decisions:
  - "Release is a SEPARATE narrow state-driven cron, NOT a recover_orphaned_work extension — held files have no ledger row so ledger replay structurally cannot release them (critical_reconciliation)"
  - "models_path comes from get_settings().models_path directly (it lives on BaseSettings, so no ControlSettings cast is needed)"
  - "D-04 is satisfied BY OMISSION (no change to _DOMAIN_COMPLETED_STAGES or the analyze done-set); a regression test guards the omission"

patterns-established:
  - "AWAITING_CLOUD->compute is the single transition the new cron advances; it never touches any other stage, preserving the Phase-42 automation-only-in-recovery principle"

requirements-completed: [CLOUDROUTE-02]

# Metrics
duration: 20min
completed: 2026-06-25
---

# Phase 49 Plan 04: Held-File Release Cron Summary

**A new narrow `CronJob(release_awaiting_cloud, "*/5 * * * *")` on the controller that state-scans `AWAITING_CLOUD` and, once a compute agent is online, enqueues each held file to the compute queue via the shared `enqueue_process_file` producer AND resets it to `DISCOVERED` — plus a D-04 regression guard proving a held file stays pending in recovery.**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-06-25
- **Tasks:** 2 (Task 1 TDD)
- **Files:** 5 (2 created, 3 modified)

## Accomplishments

- `src/phaze/tasks/release_awaiting_cloud.py` — `release_awaiting_cloud(ctx)` producer:
  - SCAN: `get_files_by_state(session, FileState.AWAITING_CLOUD)`; empty -> `{"released": 0, "skipped": 0}` no-op.
  - GATE: `select_active_agent(session, kind="compute")` inside `try/except NoActiveAgentError` -> on miss a structured no-op (D-02): nothing enqueued, nothing raised, no state change; held files are never routed locally.
  - RELEASE: per held file, `enqueue_process_file(compute_queue, file, agent.id, models_path)` (deterministic key `process_file:<id>`; `None` return = dedup -> skipped) AND `file.state = FileState.DISCOVERED` on every outcome (D-03a); single `commit()`. Returns `{"released": N, "skipped": M}`.
  - Control-only and FastAPI-free (imports mirror `recover_orphaned_work`).
- `controller.py` — registered `release_awaiting_cloud` in `settings["functions"]` AND as `CronJob(release_awaiting_cloud, cron="*/5 * * * *")`, with a comment stating it is a NARROW recovery-only cron scoped ONLY to `AWAITING_CLOUD -> compute` (distinct from the deleted reenqueue cron; respects the Phase-42 principle).
- `test_recovery.py` — added the D-04 regression test asserting an `AWAITING_CLOUD` file is absent from the analyze done-set `{ANALYZED, ANALYSIS_FAILED}` and `is_domain_completed` returns `False` for its `process_file` row (D-04 by omission).

## Task Commits

1. **Task 1: release producer + controller cron (D-03/D-03a)** — `28b4b1e` (test RED), `70a0b3d` (feat GREEN)
2. **Task 2: D-04 regression test** — `5b079e1` (test)

_No REFACTOR commit needed — the GREEN implementation was already minimal/clean._

## Decisions Made

Followed the plan's `<interfaces>` and `<critical_reconciliation>` exactly. One simplification: `models_path` is read via `get_settings().models_path` without a `ControlSettings` cast, because `models_path` is defined on `BaseSettings` (so it is type-visible to mypy without narrowing) — verified clean by `mypy src/phaze`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Refined `test_no_auto_advance_cron` to permit the narrow release cron**
- **Found during:** Task 1 (controller registration)
- **Issue:** The plan mandates `CronJob(release_awaiting_cloud, "*/5 * * * *")`, but the existing Phase-42 regression `tests/test_tasks/test_controller_reenqueue.py::test_no_auto_advance_cron` asserted `all(cj.cron != "*/5 * * * *")` — a schedule-string ban that would fail on the new (legitimate, narrow) cron.
- **Fix:** Refined the assertion to ban any `*/5` cron whose function is NOT `release_awaiting_cloud` (preserving the test's true intent: no GENERAL pipeline auto-advance, and `recover_orphaned_work` is never a cron). The deleted-reenqueue and never-a-cron-recovery guarantees are unchanged.
- **Files modified:** `tests/test_tasks/test_controller_reenqueue.py`
- **Commit:** `70a0b3d`

## Issues Encountered

- A first draft of the `test_release_module_is_fastapi_free` guard did a raw substring scan of the module source and false-positived on the docstring prose ("nor `phaze.routers`"). Rewrote it to parse actual `import`/`from` statements via `ast` — the correct way to assert an import boundary. Caught and fixed within Task 1 before the GREEN commit.
- DB-backed tests require the ephemeral test stack (Postgres :5433, Redis :6380) with the matching `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` env vars. Standard local-test setup, not a code change.

## Verification

- `uv run pytest tests/test_tasks/test_release_awaiting_cloud.py tests/test_tasks/test_recovery.py` — passes (release: 6, recovery: 33).
- `uv run pytest tests/test_tasks/test_recovery.py -k "awaiting or pending or domain"` — 5 passed (D-04 selected).
- `uv run pytest tests/test_task_split.py` — 7 passed (controller import boundary intact; agent worker never imports the control-only release module).
- `uv run pytest tests/test_tasks/test_controller_reenqueue.py` — passes (refined no-auto-advance guard + existing crons preserved).
- `uv run ruff check src/phaze tests` — All checks passed.
- `uv run mypy src/phaze` — Success: no issues found in 137 source files.
- `alembic/versions/` unchanged at 24 files — AWAITING_CLOUD remains code-only (no migration).

## Next Phase Readiness

- CLOUDROUTE-02's full lifecycle is now closed: Plan 02 HOLDS long files in `AWAITING_CLOUD` when no compute agent is online; this plan DRAINS them to the compute queue within ~5 min once one comes online. No blockers for Plan 49-50/51 (rsync push pipeline / deploy).

## Self-Check: PASSED

- Created files exist: `src/phaze/tasks/release_awaiting_cloud.py`, `tests/test_tasks/test_release_awaiting_cloud.py`.
- Commits present in git history: `28b4b1e`, `70a0b3d`, `5b079e1`.

---
*Phase: 49-duration-routing-backfill*
*Completed: 2026-06-25*
