---
phase: 42
plan: 02
subsystem: pipeline-automation
tags: [recovery, controller, htmx, dag, saq, idempotency]
requires:
  - "tasks/reenqueue.recover_orphaned_work (Wave 1, 42-01)"
  - "app.state.controller_queue + app.state.task_router (main.py lifespan)"
  - "phaze.database.async_session sessionmaker"
provides:
  - "Recovery-only automation gate: zero steady-state auto-enqueues"
  - "Gated controller-startup recovery (no-op on durable Phase-36 restart)"
  - "POST /pipeline/recover manual recovery endpoint (force=True)"
  - "Global DAG 'Recover orphaned work' button"
affects:
  - "src/phaze/tasks/controller.py"
  - "src/phaze/tasks/reenqueue.py"
  - "src/phaze/routers/pipeline.py"
  - "src/phaze/templates/pipeline/partials/dag_canvas.html"
tech-stack:
  added: []
  patterns:
    - "Fire-and-forget background task held in module _background_tasks set"
    - "Identical keyed producer for manual + automatic paths (anti-drift, D-03)"
key-files:
  created:
    - "src/phaze/templates/pipeline/partials/recover_response.html"
  modified:
    - "src/phaze/tasks/controller.py"
    - "src/phaze/tasks/reenqueue.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/templates/pipeline/partials/dag_canvas.html"
    - "tests/test_tasks/test_controller_reenqueue.py"
    - "tests/test_routers/test_pipeline.py"
    - "tests/test_dag_canvas_render.py"
    - "tests/integration/test_pg_dedup.py"
    - "docs/api.md"
    - "docs/architecture.md"
    - "docs/project-structure.md"
    - "README.md"
  deleted:
    - "tests/test_tasks/test_reenqueue.py"
decisions:
  - "D-01: removed the every-5-min reenqueue_discovered auto-advance cron AND the producer itself"
  - "D-02: controller startup now runs the gated recover_orphaned_work(ctx) (force defaults False)"
  - "D-03: manual /pipeline/recover and startup call the identical producer — no drift"
  - "D-05: manual Recover forces past the no-op detect gate (cold-boot safety net); dedup keeps it idempotent"
requirements: [REQ-42-1, REQ-42-4, REQ-42-5]
metrics:
  tasks: 3
  duration_minutes: 25
  completed: 2026-06-14
---

# Phase 42 Plan 02: Controller Wiring + Recovery UI Summary

Wired the Wave-1 recovery engine into the running system: removed the steady-state auto-advance cron, gated the controller-startup recovery on genuine queue-loss, and exposed a global DAG "Recover" button calling the same idempotent `recover_orphaned_work` producer (force=True) — so after this plan the pipeline produces ZERO automatic enqueues except a single gated boot recovery pass.

## What was built

**Task 1 — controller wiring (commit `787de0b`).**
- Removed the `CronJob(reenqueue_discovered, cron="*/5 * * * *")` entry from `settings["cron_jobs"]`; `reap_stalled_scans` (every minute) and `refresh_tracklists` (monthly) remain unchanged.
- Replaced the unconditional boot-time `reenqueue_discovered(ctx)` call with the gated `recover_orphaned_work(ctx)` (force defaults False → a durable Phase-36 restart is a no-op via the `count_inflight_jobs` detector), kept inside the broad `try/except` so a recovery failure never aborts boot.
- Registered `recover_orphaned_work` in `settings["functions"]`; **deleted** the legacy `reenqueue_discovered` function from `reenqueue.py` and every reference (import, functions list, cron). Confirmed via grep that no source/test still imports it.
- Recorded the Phase-36 durability reframe in both `controller.py` and `reenqueue.py` docstrings/comments with an explicit "do not re-add the cron" note.
- Rewrote `test_controller_reenqueue.py` for the new contract; deleted `test_reenqueue.py` (tested the removed producer); updated two stale comment references in `test_pg_dedup.py`.

**Task 2 — manual recovery surface (commit `622ec08`).**
- Added `POST /pipeline/recover` mirroring the Phase-39–41 bulk-trigger shape: builds a worker-shaped ctx (`async_session` + `app.state.controller_queue` + `app.state.task_router`) and schedules `recover_orphaned_work(ctx, force=True)` as a fire-and-forget background task held in `_background_tasks`; returns the `recover_response.html` fragment immediately (never 500s).
- Created `recover_response.html` with the LOCKED idempotency reassurance copy ("Already-running work is deduplicated — nothing will double-enqueue").
- Added a GLOBAL slate-outline "Recover orphaned work" button to the DAG header (`flex items-center justify-between` row), visually distinct from the per-stage blue/rose triggers, with a `Recovering…` `hx-indicator` and an Alpine error flag. `NODE_LAYOUT`/`EDGES` unchanged (still 10 nodes / 10 edges).

**Task 3 — docs + full gate (commit `9165801`).**
- `docs/api.md`: added the `/pipeline/recover` table row + a "Recovery-only automation model" prose paragraph.
- `docs/architecture.md` + `docs/project-structure.md`: replaced stale `reenqueue_discovered` references with `recover_orphaned_work`.
- `README.md`: added a concise Phase-42 recovery-only automation note (badges untouched, GSD marker preserved).
- Updated the `test_dag_canvas_render.py` hx-post guard to pin the new global `/pipeline/recover` action separately from the per-stage enqueue surface.

## Verification

- `uv run pytest --cov` → **1804 passed**, total coverage **97.58%** (≥85%); after the render-guard fix the full affected set (`test_dag_canvas_render` + `test_pipeline` + `test_controller_reenqueue` + `test_recovery`) is green (104 passed).
- `uv run ruff check .` → All checks passed. `uv run ruff format --check .` → 336 files already formatted. `uv run mypy .` → no issues in 155 source files.
- `pre-commit run --all-files` → all hooks pass (never `--no-verify`).
- Cron gate one-liner confirms the `*/5` auto-advance cron is gone; grep confirms `reenqueue_discovered` is fully removed from source.

Tests run against the ephemeral integration Postgres/Redis (host ports 5433/6380) via the `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` env (the `integration-test` recipe's wiring); DB-backed tests are auto-marked `integration` by conftest.

## Deviations from Plan

**1. [Rule 3 — Blocking] Updated the `test_dag_canvas_render` hx-post guard.**
- **Found during:** Task 3 full-suite gate.
- **Issue:** `test_gating_triggers_post_only_to_existing_endpoints` pins the canvas's exact hx-post surface; the new global `/pipeline/recover` button (added in Task 2, intentional REQ-42-5 surface) failed the exhaustive count assertion.
- **Fix:** Pinned `/pipeline/recover` with its own assertion as a pipeline-LEVEL action (distinct from the 8 per-stage enqueue triggers + 12 stage controls), and added it to the total-count check. This preserves the T-35-10 "no net-new per-stage trigger" guard while admitting the deliberate global recovery action.
- **Files modified:** `tests/test_dag_canvas_render.py` — **Commit:** `9165801`.

**2. [Rule 2 — Doc consistency] Updated stale `reenqueue_discovered` references beyond the planned doc set.**
- Removing the producer left dangling references in `docs/architecture.md`, `docs/project-structure.md`, and two `tests/integration/test_pg_dedup.py` comments. Updated all to `recover_orphaned_work` so no doc/comment points at a deleted symbol (project "docs up to date" convention).
- **Commits:** `787de0b` (test comment), `9165801` (architecture/project-structure docs).

No authentication gates were encountered. No packages were installed (pure wiring/UI/docs — T-42-SC accepted).

## Known Stubs

None.

## Self-Check: PASSED
- Commits `787de0b`, `622ec08`, `9165801` exist in `git log`.
- Created file `src/phaze/templates/pipeline/partials/recover_response.html` exists.
- `/pipeline/recover` present in router, DAG canvas, and `docs/api.md`.
- `reenqueue_discovered` fully removed from source (0 definitions).
