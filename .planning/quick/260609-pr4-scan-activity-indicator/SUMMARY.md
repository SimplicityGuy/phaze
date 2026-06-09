# PR4: Scan Activity Indicator + Stall Reaper — Summary

Live "is it actually running?" activity indicator for RUNNING scans plus a
control-side cron that auto-fails genuinely-dead scans, all driven by one new
`last_progress_at` heartbeat column.

- **Type:** quick task (PR4 of 5, scan-reliability series)
- **Branch:** `feat/scan-activity-indicator`
- **Worktree:** `/Users/Robert/Code/public/phaze-pr4-activity`
- **Started:** 2026-06-09T16:08:09Z — **Completed:** 2026-06-09T16:35:31Z (~27 min)
- **Tasks:** 6/6 complete, one atomic conventional commit each

## What shipped

1. **Migration 017 + model column** — `scan_batches.last_progress_at`
   (nullable, tz-aware), backfilling existing rows to `updated_at`. Chains
   `down_revision="016"`; single head `017`.
2. **Heartbeat stamping** — `last_progress_at = datetime.now(UTC)` stamped on
   every real agent PATCH (after the same-state no-op early-return, so an
   idempotent PATCH never bumps it), on both scan-create paths
   (`trigger_scan`, `run_scan`), and on `run_scan`'s COMPLETED/FAILED terminal
   updates.
3. **Stall reaper + config** — `reap_stalled_scans(ctx)` control-side SAQ cron
   marks RUNNING batches with no progress for `scan_stall_seconds` (default
   600) as FAILED with a "stalled" `error_message` + frozen `completed_at`,
   logging a structlog WARNING. Registered ONLY in `controller.py` (functions +
   every-minute `CronJob(reap_stalled_scans, cron="* * * * *")`).
   `PHAZE_SCAN_STALL_SECONDS` added on `BaseSettings` (3-name AliasChoices).
4. **UI activity indicator** — `seconds_since_progress` / `is_scan_stalled`
   helpers (UI warns amber at half the reaper threshold). Recent Scans table +
   in-progress poll card show a green pulsing dot + "·Ns ago" while
   progressing, amber "stalled?" once quiet. Terminal branches untouched
   (Pitfall 6 polling-halt preserved).
5. **Tests** — migration round-trip, reaper (stalled/fresh/live/boundary),
   stamping (real PATCH stamps, no-op PATCH does not), UI render + dashboard
   attrs.
6. **Docs** — `.env.example`, `docs/configuration.md`, `README.md`.

## Commits

| # | Hash | Message |
|---|------|---------|
| 1 | `83201a4` | feat(scan): add scan_batches.last_progress_at heartbeat column (migration 017) |
| 2 | `2cab730` | feat(scan): stamp last_progress_at on every scan progress point |
| 3 | `bb79e92` | feat(scan): add stall reaper cron + PHAZE_SCAN_STALL_SECONDS knob |
| 4 | `1b95432` | feat(ui): live activity indicator + stalled-scan affordance |
| 5 | `79b4b7f` | test(scan): cover last_progress_at, stall reaper, and activity indicator |
| 6 | `72ca52a` | docs(scan): document PHAZE_SCAN_STALL_SECONDS + stall reaper / activity indicator |

## Critical-correctness verification (risk block)

1. **Control-vs-agent DB boundary** — reaper registered ONLY in `controller.py`;
   never imported by `agent_worker.py` / `tasks/_shared/*`.
   `tests/test_task_split.py` -> **6 passed** (boundary intact).
2. **LIVE never reaped** — reaper guards on explicit `status == 'running'`.
   `test_live_sentinel_never_reaped` proves an ancient LIVE row is untouched.
3. **Idempotent PATCH heartbeat** — stamp placed AFTER the same-state no-op
   early-return. `test_same_state_no_op_does_not_stamp_last_progress_at` proves
   a same-state PATCH leaves the heartbeat NULL.
4. **CronJob cadence** — `cron="* * * * *"` (5-field every-minute), matching
   `refresh_tracklists`' form.
5. **Pitfall-6 preserved** — only the RUNNING branch of
   `scan_progress_card.html` carries `hx-trigger`/`hx-get`/`hx-swap`; terminal
   branches do not. `test_scan_card_terminal_branches_have_no_polling_trigger`
   asserts it.

## Verification gate (all from the worktree)

- `uv run pytest tests/test_task_split.py` -> **6 passed**.
- Migration 017 / reaper / stamping / UI tests -> **105 passed**.
- `uv run pytest` (full) -> **1435 passed**; **7 failed + 39 errors** confined
  entirely to 4 Redis-dependent files (`test_agent_exec_batches.py`,
  `test_agent_tracklists.py`, `test_execution_dispatch.py`,
  `test_services/test_agent_task_router.py`) — every one a
  `redis ... localhost:6379` ConnectionError. Redis is unavailable in this
  sandbox; these fail identically on `main` and are unrelated to PR4. With those
  4 files excluded: **1428 passed, 0 failed**.
- Reaper unit output: `test_reaps_stalled_running_batch` (stalled->FAILED +
  "stalled" msg + completed_at + WARNING), `test_fresh_running_batch_untouched`
  (fresh->RUNNING), `test_live_sentinel_never_reaped` (LIVE->untouched),
  `test_threshold_boundary` (at-threshold survives, just-past reaped),
  `test_no_running_rows_returns_zero` -> **all PASSED**.
- `uv run ruff check .` -> All checks passed. `uv run ruff format --check .` ->
  272 files already formatted. `uv run mypy .` -> no issues in 140 files.
- `pre-commit run --all-files` -> all hooks pass.
- `alembic heads` -> `017 (head)`; `down_revision = "016"`. `alembic upgrade
  head` against the localhost test DB applied `016 -> 017` cleanly
  (`alembic current` -> `017 (head)`). (Default `postgres` hostname is
  Docker-only, so a bare `upgrade head` only fails on DNS resolution outside
  Docker.)

## Coverage

Overall **95.76%** (gate >=85%). Changed modules: `models/scan_batch.py` 100%,
`routers/agent_scan_batches.py` 100%, `routers/pipeline.py` 100%,
`routers/pipeline_scans.py` 100%, `services/ingestion.py` 100%,
`tasks/controller.py` 100%, `config.py` 97.48%, `tasks/scan_reaper.py` 96.30%.

## Deviations from plan

None functional — plan executed as written.

Minor coverage note (not a deviation): `scan_reaper.py:64` (the defensive
tz-naive -> assume-UTC branch on a reference timestamp) is uncovered because the
test-DB `last_progress_at` column is `TIMESTAMP WITH TIME ZONE`, so asyncpg
always materializes a tz-aware value and the naive branch never fires. Module
coverage (96.30%) and overall (95.76%) remain well above the 85% gate. The
branch mirrors the established `elapsed_seconds` tz-safety pattern and is kept
as a defensive guard.

## Self-Check: PASSED

- `alembic/versions/017_add_scan_batches_last_progress_at.py` — FOUND
- `src/phaze/tasks/scan_reaper.py` — FOUND
- `tests/test_tasks/test_scan_reaper.py` — FOUND
- `tests/test_migrations/test_017_upgrade.py` — FOUND
- Commits `83201a4`, `2cab730`, `bb79e92`, `1b95432`, `79b4b7f`, `72ca52a` — all present in `git log`.

## Not done (out of scope)

- **Delete-scans** is PR5 — intentionally not implemented.
- No push / PR opened — the operator handles that after verification.
