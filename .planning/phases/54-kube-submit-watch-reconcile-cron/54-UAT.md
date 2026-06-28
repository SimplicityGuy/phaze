---
status: partial
phase: 54-kube-submit-watch-reconcile-cron
source: [54-01-SUMMARY.md, 54-02-SUMMARY.md, 54-03-SUMMARY.md, 54-04-SUMMARY.md, 54-05-SUMMARY.md, 54-06-SUMMARY.md]
started: 2026-06-28
updated: 2026-06-28
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start: fresh migrations + app/controller boot
expected: |
  On a clean database, `alembic upgrade head` runs through migration 026 without
  error; `cloud_job` gains the `kueue_workload` / `attempts` / `inadmissible`
  columns and the 6-member status CHECK. The FastAPI app imports, the controller
  registers `submit_cloud_job` as a routable task, and `reconcile_cloud_jobs` as a
  fixed `*/5` cron (cron-only, not routable).
result: pass
evidence: |
  Fresh `alembic upgrade head` ran 020→026 cleanly; `alembic current` = 026 (head).
  `\d cloud_job` shows kueue_workload(varchar 255, null), attempts(int not null
  default 0), inadmissible(bool not null default false), and
  ck_cloud_job_status_enum with the 6 members (uploading/uploaded/submitted/running/
  succeeded/failed). `from phaze.main import app` → "Phaze"; controller cron_jobs
  includes ('reconcile_cloud_jobs','*/5 * * * *'); 'submit_cloud_job' in
  CONTROLLER_TASKS and controller functions; 'reconcile_cloud_jobs' NOT in
  CONTROLLER_TASKS (cron-only). Verified by orchestrator 2026-06-28.

### 2. Inadmissible operator alert on the pipeline dashboard
expected: |
  The pipeline dashboard shows an amber "⚠ K8s Jobs not admitting — check LocalQueue
  config" alert banner ONLY when one or more Kueue Workloads are Inadmissible
  (inadmissible_count > 0). In a healthy state (count = 0, including normal Pending
  quota waits) the card is invisible — no banner.
result: pass
evidence: |
  Rendering `pipeline/partials/inadmissible_card.html`: count=0 → just an empty
  carrier `<section id="inadmissible-card"></section>` (no banner); count=2 → the
  amber `role="alert"` banner with the "⚠ K8s Jobs not admitting" heading. End-to-end
  router suite green (4/4): test_dashboard_renders_inadmissible_alert,
  test_dashboard_hides_inadmissible_alert_when_none,
  test_stats_poll_repushes_inadmissible_card_oob, test_dashboard_renders_on_all_zero_path.
  Verified by orchestrator 2026-06-28.

### 3. Live kube submit → reconcile lifecycle (end-to-end)
expected: |
  Enqueuing `submit_cloud_job` for a file POSTs a suspended Kueue Job
  (`phaze-analyze-<file_id>`) and writes a SUBMITTED cloud_job row; the `*/5`
  reconcile cron then reads the Job/Workload, advances SUBMITTED→RUNNING→SUCCEEDED
  (or bounded re-drive → ANALYSIS_FAILED), deletes the Job after recording, and
  cleans up the staged S3 object on no-callback terminals — all driven by the
  out-of-band analysis callback as the sole result writer.
result: blocked
blocked_by: prior-phase
reason: |
  No live exercise path exists yet. Two prerequisites are out of Phase 54 scope:
  (a) a reachable Kubernetes cluster running Kueue with a configured LocalQueue
  (homelab provisioning / deploy = Phase 56), and (b) the live routing wiring that
  actually triggers `submit_cloud_job` for a cloud-routed file (Phase 55 — the
  producer is built but not yet wired into the live seam). Against the fake-kube
  seam the full state machine is exhaustively covered (test_reconcile_cloud_jobs:
  24 tests incl. delete-after-record ordering, bounded re-drive + race guard,
  Inadmissible no-cap, vanished-Job terminal routing). This row is the documented
  manual-only item from 54-VALIDATION.md, deferred to Phase 56 deploy.

## Summary

total: 3
passed: 2
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

[none — 2 passed, 1 blocked on Phase 55/56 prerequisites; no code defects found]
