---
phase: 54-kube-submit-watch-reconcile-cron
plan: 06
subsystem: infra
tags: [kubernetes, kueue, kr8s, saq-cron, reconcile, cloud-burst, postgres]

# Dependency graph
requires:
  - phase: 54-02
    provides: cloud_job model gains kueue_workload/attempts/inadmissible columns + SUBMITTED/RUNNING/SUCCEEDED status members + cloud_submit_max_attempts config
  - phase: 54-03
    provides: kube_staging seam (get_job / get_workload_for / delete_job) + tests/kube_fakes factories
  - phase: 54-05
    provides: submit_cloud_job fast producer (the re-drive target) + submit_cloud_job_key
provides:
  - "reconcile_cloud_jobs: the */5 in-flight K8s reconcile cron (status->outcome mapping, delete-after-record ordering, S3 cleanup, bounded re-drive, Inadmissible alert)"
  - "the fixed */5 CronJob registration on the controller (control-only, cron-only, not routable)"
  - "the exhaustive fake-kube state-machine test suite (12 transition tests)"
  - "operator docs: the submit -> reconcile Kubernetes-burst lifecycle"
affects: [55-deploy-trigger, 56-live-cluster-verification, pipeline-ui-inadmissible-alert]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Cron-only narrow reconcile loop (mirrors stage_cloud_window CONTROL-ONLY discipline)"
    - "Delete-after-record terminal ordering (record+commit -> S3 delete -> Job delete)"
    - "Confirm-gone re-drive race guard (delete_job -> get_job==None before fresh submit)"
    - "Per-row guard with primitive-id re-fetch (rollback-safe single-session iteration)"

key-files:
  created:
    - src/phaze/tasks/reconcile_cloud_jobs.py
    - tests/test_tasks/test_reconcile_cloud_jobs.py
  modified:
    - src/phaze/tasks/controller.py
    - tests/test_task_split.py
    - docs/cloud-burst.md

key-decisions:
  - "On the under-cap re-drive path the staged S3 object is PRESERVED (the re-submitted Job needs it); S3 is deleted ONLY on the genuinely-terminal at-cap ANALYSIS_FAILED path. The success path makes zero S3 calls (the callback already deleted inline)."
  - "The confirm-gone race guard gates the attempt increment: when the prior Job is still terminating the entire re-drive is deferred with NO state change, so no extra attempt is ever burned."
  - "Single-session per-tick iteration captures primitive cloud_job ids up front and re-fetches each row via async session.get, so a per-row rollback never leaves an expired ORM object that would trigger sync lazy-load (MissingGreenlet) on the next row."

patterns-established:
  - "Status->outcome mapping matches the exact (type,status,reason) Kueue Workload condition tuples; Job succeeded/failed counters are the source of truth and short-circuit before reading the Workload."
  - "reconcile_cloud_jobs registered in BOTH functions and cron_jobs (mirroring reap_stalled_scans) but intentionally absent from enqueue_router.CONTROLLER_TASKS (cron-only)."

requirements-completed: [KSUBMIT-02, KSUBMIT-03, KSUBMIT-04, KSUBMIT-05, KSUBMIT-06]

# Metrics
duration: 35min
completed: 2026-06-28
---

# Phase 54 Plan 06: Reconcile Cron Summary

**The cron-only `*/5` reconcile loop that owns the Kueue Job lifecycle — maps every Job + Workload condition to an outcome, enforces delete-after-record ordering + S3 cleanup, drives the bounded re-drive to ANALYSIS_FAILED, surfaces Inadmissible without consuming the cap, and never writes an analysis result — all proven against the fake-kube substrate with no live cluster.**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-06-28T15:10:00Z
- **Completed:** 2026-06-28T15:45:00Z
- **Tasks:** 2 completed
- **Files modified:** 5 (2 created, 3 modified)

## Accomplishments
- Built `reconcile_cloud_jobs` — the highest-risk Phase 54 deliverable — implementing the full Kueue status→outcome state machine: Succeeded (record + delete), Failed/Evicted (no-callback terminal → bounded re-drive or ANALYSIS_FAILED), Pending (silent), Inadmissible (loud hold, no cap), Admitted/QuotaReserved (SUBMITTED→RUNNING).
- Enforced the load-bearing delete-after-record ordering (D-04): the outcome is committed before `delete_job`, and `delete_staged_object` precedes `delete_job` on the no-callback terminal (D-05); the success path makes zero S3 calls and reconcile never calls `put_analysis`/`report_analysis_failed` (KSUBMIT-03).
- Added the confirm-gone re-drive race guard (D-08): the prior Job is deleted AND confirmed gone via `get_job` before the fresh `submit_cloud_job` is enqueued, so the deterministic-name 409→refresh cannot burn an extra attempt.
- Registered the fixed `*/5` CronJob on the controller (control-only, cron-only, not routable) and documented the submit→reconcile lifecycle in `docs/cloud-burst.md`.

## Task Commits

Each task was committed atomically:

1. **Task 1: reconcile_cloud_jobs state machine (TDD)** - `dae5625` (feat)
2. **Task 2: register the */5 reconcile cron + docs** - `7f71dfb` (feat)

_Task 1 was implemented test-and-code together (the fake-kube seam makes the state machine fully unit-testable); the 12 transition tests are the graded artifact._

## Files Created/Modified
- `src/phaze/tasks/reconcile_cloud_jobs.py` - The `*/5` reconcile cron body: iterates the `cloud_job` sidecar (D-02), maps conditions to outcomes, owns terminal ordering + S3 cleanup, bounded re-drive with race guard, Inadmissible alert. Control-only + FastAPI-free.
- `tests/test_tasks/test_reconcile_cloud_jobs.py` - 12 tests covering the full state machine via the monkeypatched kube seam + `tests/kube_fakes` factories (pending/inadmissible/inadmissible-never-caps/admission→success/eviction-redrive/cap→ANALYSIS_FAILED/ordering/s3-only-on-terminal/never-writes-result/race-guard/per-row-guard + a source-level KSUBMIT-03 grep guard).
- `src/phaze/tasks/controller.py` - Import `reconcile_cloud_jobs`; add to `settings['functions']`; register `CronJob(reconcile_cloud_jobs, '*/5 * * * *')` with the narrow-scope guard comment.
- `tests/test_task_split.py` - Assert reconcile is control-only: absent from the agent worker (subprocess), present as a controller function + exactly one `*/5` CronJob, and absent from `CONTROLLER_TASKS`.
- `docs/cloud-burst.md` - New "Kubernetes burst — the submit → reconcile lifecycle" section (callback-as-sole-result-channel, delete-after-record ordering, bounded re-drive cap, Inadmissible-vs-Pending).

## Requirements Completed
- **KSUBMIT-02** — submit returns fast; reconcile is the separate `*/5` lifecycle owner; the callback writes the result.
- **KSUBMIT-03** — reconcile NEVER writes an analysis result (runtime test + source grep prove zero result-writer calls).
- **KSUBMIT-04** — every Job + Workload condition maps to the correct outcome (exhaustive transition suite).
- **KSUBMIT-05** — bounded re-drive to ANALYSIS_FAILED at the cap, no cross-target fallback; Inadmissible never consumes the cap.
- **KSUBMIT-06** — a finished Job is cleaned up without a TTL-vs-read race (delete-after-record); reconcile reads `cloud_job` and seeds NO `process_file` ledger row (the re-drive enqueues `submit_cloud_job`).

## Deviations from Plan

None of Rules 1–4 were triggered. Two implementation choices made within the plan's intent are documented as key-decisions above:
- **S3 object preserved on the under-cap re-drive.** The plan's general "Failed/Evicted → delete_staged_object" ordering is applied to the genuinely-terminal at-cap path; on the under-cap re-drive the object is kept because the re-submitted Job still needs it. This resolves the latent tension between "delete the staged object" and "re-stage a fresh submit" while honoring D-05 on the terminal path. The ordering test and the s3-only-on-terminal test pin this behavior.
- **Confirm-gone gates the attempt increment.** To satisfy "no extra attempt burned" when the prior Job is still terminating, the under-cap path defers the entire re-drive (no increment, no commit, no enqueue) until `get_job` confirms the Job gone, rather than incrementing first and skipping only the enqueue.

## Threat Surface
No new trust boundaries beyond the plan's `<threat_model>`. The mitigations for T-54-15 (KSUBMIT-03 zero result-writer calls), T-54-16 (D-04 ordering), T-54-17 (D-05 S3 delete), T-54-18 (bounded cap + Inadmissible no-storm), and T-54-19 (no `process_file` ledger seed) are all implemented and test-covered.

## Verification
- `uv run pytest tests/test_tasks/test_reconcile_cloud_jobs.py tests/test_task_split.py -q` → 24 passed.
- Sibling registration regression: `tests/test_staging_cron.py tests/test_tasks/test_submit_cloud_job.py tests/test_services/test_enqueue_router.py` → 36 passed.
- `uv run mypy src/phaze/tasks/reconcile_cloud_jobs.py src/phaze/tasks/controller.py` → clean.
- `uv run ruff check` + `ruff format` → clean (all per-commit pre-commit hooks passed, incl. the local mypy hook + bandit).
