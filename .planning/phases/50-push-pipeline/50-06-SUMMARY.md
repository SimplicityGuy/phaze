---
phase: 50-push-pipeline
plan: 06
subsystem: cloud-pipeline
tags: [routing, cron, bounded-window, saq, backpressure]
requires:
  - "50-01: cloud_max_in_flight (ControlSettings), FileState.PUSHING/PUSHED"
  - "50-02: push_file task (AGENT_TASKS) + PushFilePayload + push_file:<id> deterministic key"
provides:
  - "single-entry AWAITING_CLOUD hold (router long-file branch, no direct-to-compute path)"
  - "stage_cloud_window bounded top-up cron (the single 'stay one ahead' driver)"
  - "get_cloud_window_count + get_cloud_staging_candidates service helpers"
affects:
  - "controller cron registration (replaced release_awaiting_cloud drain)"
  - "POST /api/v1/analyze + /pipeline/analyze + /pipeline/backfill-cloud (long files now always held)"
tech-stack:
  added: []
  patterns:
    - "window counted from committed FileState IN {PUSHING, PUSHED}, not the SAQ ledger (D-08)"
    - "COUNT + SELECT FOR UPDATE SKIP LOCKED + state=PUSHING in one transaction (TOCTOU-safe)"
    - "deterministic push_file:<id> key collapses double-tick to a skipped no-op"
key-files:
  created: []
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/tasks/release_awaiting_cloud.py
    - src/phaze/services/pipeline.py
    - src/phaze/tasks/controller.py
    - tests/test_routing_seam.py
    - tests/test_staging_cron.py
    - tests/test_routers/test_pipeline.py
    - tests/test_tasks/test_controller_reenqueue.py
  deleted:
    - tests/test_tasks/test_release_awaiting_cloud.py
decisions:
  - "no_active_agent keys off the fileserver agent only (compute is reached solely via the cron)"
  - "flip state to PUSHING even on a dedup no-op so the next tick's window count stays honest"
  - "Task 2 + Task 3 committed together: the function rename breaks controller import, so a green/importable tree needs both"
metrics:
  tasks: 3
  commits: 4
  files_changed: 9
  completed: 2026-06-26
---

# Phase 50 Plan 06: Bounded Cloud-Window Single-Entry Pipeline Summary

Reshaped the Phase-49 routing seam so every cloud-routed long file holds in `AWAITING_CLOUD` (no direct-to-compute enqueue remains), and replaced the `release_awaiting_cloud` drain with a `stage_cloud_window` cron that tops the in-flight window up to `cloud_max_in_flight` by staging `push_file` for the oldest held files — making the ≤N window the single, unbypassable entry to the compute pipeline.

## What Was Built

- **Single-entry hold (Task 1):** `_route_discovered_by_duration` now ALWAYS sets a long file to `FileState.AWAITING_CLOUD`. The `compute_agent`/`compute_q`/`cloud_files` direct-enqueue locals were removed; `cloud` is always 0 in the return dict; `no_active_agent` keys off the fileserver agent. Because the seam is shared, the `/pipeline/backfill-cloud` path now also funnels every long backfill candidate into the bounded window (held + explicit ledger row).
- **Bounded top-up cron (Task 2):** `stage_cloud_window(ctx)` (in `release_awaiting_cloud.py`) computes `window = COUNT(state IN {PUSHING, PUSHED})` from committed FileState truth, `slots = cloud_max_in_flight - window`, then selects up to `slots` oldest `AWAITING_CLOUD` rows `ORDER BY created_at ASC ... FOR UPDATE SKIP LOCKED`, flips each to `PUSHING`, and enqueues `push_file` on the fileserver queue — COUNT + SELECT + state change in ONE transaction. Two `NoActiveAgentError` gates (compute consumer, fileserver push-initiator) each return a clean no-op rather than raising. New service helpers `get_cloud_window_count` and `get_cloud_staging_candidates`; a local `push_file_job_key` + `_enqueue_push_file` producer carrying the deterministic `push_file:<id>` key.
- **Controller registration (Task 3):** `stage_cloud_window` added to `settings["functions"]` and registered as the single `CronJob(stage_cloud_window, "*/5 * * * *")`, replacing the deprecated drain cron. The Phase-42 no-general-auto-advance guard comment is retained and updated.

## Tasks Completed

| Task | Name | Commits |
| ---- | ---- | ------- |
| 1 | Reshape routing seam to single-entry AWAITING_CLOUD hold | 6a45ac5 (RED), ba0b35e (GREEN) |
| 2 | stage_cloud_window bounded top-up cron + window-count helper | 610f066 (RED), 4031c6d (GREEN) |
| 3 | Register stage_cloud_window on the controller (replace drain cron) | 4031c6d |

## Verification

- `tests/test_routing_seam.py` (2), `tests/test_staging_cron.py` (9) — all pass, including the 144-file-backlog → ≤2 assertion, FIFO ordering, no-compute no-op, no-fileserver no-op (held), and the double-tick dedup.
- `tests/test_routers/test_pipeline.py` (updated) + `tests/test_tasks` — pass except two DB-environment-limited tests (`test_ledger_backfill::...no_overwrite`, `test_recovery::test_count_inflight_jobs_reads_real_saq_jobs`) that require a fully migrated SAQ schema on a bare ephemeral DB — pre-existing, explicitly called out in the plan's `<note_on_test_db>`, not a regression.
- `uv run ruff check .` — All checks passed. `uv run mypy .` — Success (167 source files).
- Acceptance greps: `grep -c cloud_files routers/pipeline.py` == 0; `in_([FileState.PUSHING` present in services/pipeline.py; `cloud_max_in_flight` present and `NoActiveAgentError` appears 3× in release_awaiting_cloud.py; `python -c "import phaze.tasks.controller"` imports cleanly.

## Deviations from Plan

### Auto-fixed / blocking issues (Rules 1 & 3)

**1. [Rule 3 - Blocking] Updated existing analyze/backfill router tests to the new held contract**
- **Found during:** Task 1
- **Issue:** The Phase-49 `test_routers/test_pipeline.py` encoded the now-removed direct-to-compute behavior (`data["cloud"] == 1`, enqueue onto `phaze-agent-cloud`, backfill routes to compute). The shared-seam reshape necessarily breaks these.
- **Fix:** Rewrote 6 tests (2 analyze JSON, 2 analyze UI, 4 backfill) to assert the held-in-AWAITING_CLOUD / no-direct-enqueue contract and the `0 cloud, N awaiting cloud` split; the backfill held-branch now writes exactly one ledger row.
- **Files:** tests/test_routers/test_pipeline.py
- **Commit:** ba0b35e

**2. [Rule 3 - Blocking] Removed the obsolete release_awaiting_cloud test + retargeted the cron guard**
- **Found during:** Task 2/3
- **Issue:** Renaming the cron function to `stage_cloud_window` left `tests/test_tasks/test_release_awaiting_cloud.py` importing a removed symbol, and `test_controller_reenqueue.py::test_no_auto_advance_cron` asserted the old `release_awaiting_cloud` cron was the only `*/5`.
- **Fix:** Deleted the obsolete test file (its coverage is fully reproduced in `test_staging_cron.py`, including the controller-registration + fastapi-free import-boundary tests) and pointed the `*/5` guard at `stage_cloud_window`.
- **Files:** tests/test_tasks/test_release_awaiting_cloud.py (deleted), tests/test_tasks/test_controller_reenqueue.py
- **Commit:** 4031c6d

### Process note

Tasks 2 and 3 landed in one commit (4031c6d) rather than two: the function rename breaks the `controller.py` import, and the pre-commit `mypy .` hook runs project-wide, so a green/importable tree requires the cron implementation and its controller registration together.

## Threat Surface

No new external surface. All mitigations in the plan's threat register are realized in code: single-entry hold (T-50-bypass), window from committed FileState + one-transaction COUNT/SELECT/PUSHING (T-50-scratch-dos), `push_file:<id>` dedup (T-50-double-enqueue), and both agent gates as clean no-ops (T-50-cron-raise). No package installs (T-50-SC N/A).

## Known Stubs

None — the held→stage→push path is fully wired. The downstream `report_pushed`/`process_file` against the scratch copy and sha256 verify land in later Phase-50 plans (50-04), as designed.
