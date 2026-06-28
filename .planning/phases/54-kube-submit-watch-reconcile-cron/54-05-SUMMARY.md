---
phase: 54-kube-submit-watch-reconcile-cron
plan: 05
subsystem: cloud-burst-kube-submit
tags: [kube, kueue, saq, controller, idempotency, enqueue-routing]
requires:
  - "54-02: cloud_job D-09 columns (kueue_workload/attempts/inadmissible) + SUBMITTED status member"
  - "54-03: kube_staging.submit_job seam (one POST, 409->refresh idempotency)"
  - "53-04: cloud_staging.stage_file_to_s3 upsert-then-act producer precedent"
provides:
  - "submit_cloud_job: the fast controller-queue Kube-submit producer (upsert cloud_job, no ledger seed)"
  - "submit_cloud_job_key: deterministic submit_cloud_job:<file_id> enqueue key for Phase 55 callers"
  - "submit_cloud_job routability: CONTROLLER_TASKS + controller.settings['functions'] registration"
affects:
  - "Phase 55: wires submit_cloud_job into the live stage_cloud_window routing seam (trigger owner)"
tech-stack:
  added: []
  patterns:
    - "pg_insert(...).on_conflict_do_update(index_elements=['file_id']) upsert with PK + s3_key OUT of set_"
    - "thin controller task: external POST in the seam, ORM upsert + commit in the task"
key-files:
  created:
    - "src/phaze/tasks/submit_cloud_job.py"
    - "tests/test_tasks/test_submit_cloud_job.py"
  modified:
    - "src/phaze/services/enqueue_router.py"
    - "src/phaze/tasks/controller.py"
    - "tests/test_services/test_enqueue_router.py"
    - "tests/test_task_split.py"
decisions:
  - "Submit path writes ONLY the cloud_job row -- NO scheduling-ledger seed (KSUBMIT-06); enforced by a source grep test (SchedulingLedger count == 0)."
  - "INSERT supplies s3_key via s3_staging.staged_object_key(file_id) (NOT NULL column); on conflict only status + kueue_workload refresh (PK + s3_key are immutable identity)."
  - "submit_cloud_job is a controller function (operator/Phase-55-enqueueable), NOT a CronJob -- Phase 55 owns the live trigger."
  - "Sync invariant relaxed to a subset check: CONTROLLER_TASKS must be a subset of (functions UNION cron functions), since refresh_tracklists is cron-only yet routable."
metrics:
  duration_min: 11
  completed: 2026-06-28
  tasks: 2
  files_changed: 6
---

# Phase 54 Plan 05: submit_cloud_job Fast Kube-Submit Producer Summary

A fast, idempotent `submit_cloud_job` controller task that does ONE kube POST (via the
`kube_staging.submit_job` seam) and upserts the `cloud_job` row to `SUBMITTED` with the Kueue Job
name in `kueue_workload` — writing NO scheduling-ledger row and no analysis result — registered as a
routable controller task with its live trigger deferred to Phase 55.

## What Was Built

### Task 1 — `submit_cloud_job` fast producer (TDD)
`src/phaze/tasks/submit_cloud_job.py` mirrors the `cloud_staging.stage_file_to_s3` producer
discipline. `submit_cloud_job(ctx, file_id)`:
- coerces `file_id` (str over SAQ / UUID from a direct caller) to `uuid.UUID`,
- calls `kube_staging.submit_job(fid)` exactly once (the 409→refresh idempotency lives in the seam),
- upserts the `cloud_job` row `ON CONFLICT (file_id)` setting `status=SUBMITTED` and
  `kueue_workload=<job-name>` — the PK (`id=uuid.uuid4()`) and `s3_key` are stamped on INSERT but
  kept OUT of `set_` (immutable identity, the CR-01 precedent), commits, and returns
  `{"file_id", "kueue_workload"}`.

It writes ONLY the `cloud_job` row: **no** `SchedulingLedger` `process_file:<id>` seed (KSUBMIT-06,
the CLOUDROUTE-02 hazard — a ledger row would let `recover_orphaned_work` replay a K8s file onto a
local agent queue) and **no** analysis result (KSUBMIT-02, fast return). `submit_cloud_job_key`
exposes the deterministic `submit_cloud_job:<file_id>` enqueue key for Phase 55.

Tests (`tests/test_tasks/test_submit_cloud_job.py`, 6) drive the task with a monkeypatched
`kube_staging.submit_job` spy and the controller-shaped `ctx` (`async_session` only): one SUBMITTED
row with `kueue_workload`; a re-submit upserts to a single row and re-hits the seam (idempotent);
zero scheduling-ledger rows; zero analysis-result rows; a source-grep guard (no `SchedulingLedger` /
`AnalysisResult` / result-writer / FastAPI tokens); and the deterministic key helper.

### Task 2 — routable controller-task registration
- `enqueue_router.CONTROLLER_TASKS` gains `"submit_cloud_job"` (control-plane work — kube creds live
  there); it is absent from `AGENT_TASKS`.
- `controller.py` imports `submit_cloud_job` and adds it to `settings["functions"]`. **No CronJob**
  (Phase 55 owns the `stage_cloud_window` trigger).
- `tests/test_services/test_enqueue_router.py`: `submit_cloud_job` resolves to the controller queue
  (agent_id `None`); `CONTROLLER_TASKS` is a subset of the registered controller functions
  (functions ∪ cron functions); `submit_cloud_job` is a function and not a cron.
- `tests/test_task_split.py`: a subprocess assertion that `submit_cloud_job` is NOT registered on the
  agent worker, plus an in-process control-side assertion that it IS a controller function with no cron.

## Verification

- `uv run pytest tests/test_tasks/test_submit_cloud_job.py tests/test_services/test_enqueue_router.py tests/test_task_split.py` — 35 passed.
- `uv run ruff check .` — all checks passed.
- `uv run mypy .` — success, no issues in 180 source files (`submit_cloud_job.py` clean).
- Controller-dependent regressions green (`test_staging_cron` registration test, `test_no_default_queue_producers`).
- Acceptance greps: `async def submit_cloud_job` == 1; `SchedulingLedger` == 0; `submit_cloud_job:` helper present; `CronJob(submit_cloud_job` == 0.

## Deviations from Plan

None — both tasks executed as written. One in-test refinement: the "CONTROLLER_TASKS in sync with
controller functions" invariant is a subset check against `functions ∪ cron_functions` (not
`functions` alone), because `refresh_tracklists` is a cron-only-yet-routable controller task. This
matches the existing `CONTROLLER_TASKS` docstring ("+ the refresh_tracklists cron").

## Notes for Downstream (Phase 55)

`submit_cloud_job` is built, unit-tested, and routable but intentionally **not** wired into any live
trigger. Phase 55 enqueues it (using `submit_cloud_job_key(file_id)`) from the cloud-window routing
seam. The reconcile cron (`reconcile_cloud_jobs`, a sibling plan) iterates the `cloud_job` sidecar by
`kueue_workload` to drive SUBMITTED → RUNNING → SUCCEEDED/FAILED.

## Self-Check: PASSED

- `src/phaze/tasks/submit_cloud_job.py` — FOUND
- `tests/test_tasks/test_submit_cloud_job.py` — FOUND
- Commit b72d3e8 (test RED) — FOUND
- Commit e9c41c2 (feat producer) — FOUND
- Commit 3e0d4ee (feat registration) — FOUND
