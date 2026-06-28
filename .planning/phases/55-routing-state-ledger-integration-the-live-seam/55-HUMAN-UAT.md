---
status: partial
phase: 55-routing-state-ledger-integration-the-live-seam
source: [55-VERIFICATION.md]
started: 2026-06-28T00:00:00Z
updated: 2026-06-28T00:00:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. Admission-state card visual appearance
expected: On the pipeline dashboard with a k8s deploy that has live cloud_job rows, the admission-state card renders per-phase tiles (queued_behind_quota / admitted / running / finished) with the 55-UI-SPEC hues (gray / blue / violet / green-for-finished), no role="alert", no amber. The card stays quiet (empty carrier, no heading/grid) when all counts are 0 — e.g. on a1/local deploys where cloud_phase is NULL. Tiles update live on the 5s OOB poll.
result: [pending]

### 2. End-to-end K8s routing (live cluster + real S3)
expected: With cloud_target="k8s", a ≥threshold long file flows AWAITING_CLOUD → PUSHING (staged to S3, within the cloud_max_in_flight window) → PUSHED → submit_cloud_job → a suspended Kueue Job admitted by quota → analysis result POSTed back → file completes. The a1 rsync path is unchanged when cloud_target="a1"; cloud is fully off when cloud_target="local". (Unit tests use respx/moto/fake-kube; this confirms the real seam.)
result: [pending]

### 3. Ledger-scoped backfill operator flow
expected: In the running app, the "Backfill to K8s" operator action backfills only previously-scheduled, timed-out long files (analysis_failed, duration ≥ threshold, with a process_file scheduling-ledger row) — never a whole-backlog sweep. The k8s branch resets candidates without seeding a process_file ledger row (so recover_orphaned_work won't replay them onto a local queue). The HTMX backfill response renders correctly.
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps
