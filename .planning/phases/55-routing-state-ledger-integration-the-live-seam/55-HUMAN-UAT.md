---
status: partial
phase: 55-routing-state-ledger-integration-the-live-seam
source: [55-VERIFICATION.md]
started: 2026-06-28T00:00:00Z
updated: 2026-06-28T16:30:00Z
---

## Current Test

[testing complete — 2 passed via in-app harness, 1 blocked on live cluster]

## Tests

### 1. Admission-state card visual appearance
expected: On the pipeline dashboard with a k8s deploy that has live cloud_job rows, the admission-state card renders per-phase tiles (queued_behind_quota / admitted / running / finished) with the 55-UI-SPEC hues (gray / blue / violet / green-for-finished), no role="alert", no amber. The card stays quiet (empty carrier, no heading/grid) when all counts are 0 — e.g. on a1/local deploys where cloud_phase is NULL. Tiles update live on the 5s OOB poll.
result: pass
verified_by: in-app harness against real Postgres (2026-06-28). (a) Standalone render of admission_state_card.html through the app's Jinja2Templates env — 14/14 assertions: heading+4 tiles with correct hues (gray/blue/violet/green-for-finished), hx-swap-oob attr on poll, stable #admission-state-card id, NO role="alert", NO amber; quiet empty carrier when all-zero (a1/local); per-tile gating (only running tile when only running>0). (b) Full GET /pipeline dashboard with seeded cloud_job rows (queued=1/admitted=1/running=2/finished=1) — card present, heading shown, all 4 tiles rendered. Live 5s OOB refresh is structurally verified (oob fragment re-push in stats_bar.html + test_pipeline_counts.py) but the animated browser update is the one sub-aspect still pending a live deploy.

### 2. End-to-end K8s routing (live cluster + real S3)
expected: With cloud_target="k8s", a ≥threshold long file flows AWAITING_CLOUD → PUSHING (staged to S3, within the cloud_max_in_flight window) → PUSHED → submit_cloud_job → a suspended Kueue Job admitted by quota → analysis result POSTed back → file completes. The a1 rsync path is unchanged when cloud_target="a1"; cloud is fully off when cloud_target="local". (Unit tests use respx/moto/fake-kube; this confirms the real seam.)
result: blocked
blocked_by: prior-phase
reason: "Requires a live Kueue cluster + real S3 bucket, which do not exist in this sandbox. This is the Phase 56 (deployment) live-validation item. All in-phase logic is verified against the fake kube API (kube_respx) + moto S3 + ephemeral Postgres (full suite: 2474 passed), so only the real-cluster confirmation remains."

### 3. Ledger-scoped backfill operator flow
expected: In the running app, the "Backfill to K8s" operator action backfills only previously-scheduled, timed-out long files (analysis_failed, duration ≥ threshold, with a process_file scheduling-ledger row) — never a whole-backlog sweep. The k8s branch resets candidates without seeding a process_file ledger row (so recover_orphaned_work won't replay them onto a local queue). The HTMX backfill response renders correctly.
result: pass
verified_by: in-app harness — POST /pipeline/backfill-cloud against the real app + Postgres with cloud_target="k8s" (2026-06-28). Scenario: 2 eligible (ANALYSIS_FAILED, long, ledgered) + 1 un-ledgered long + 1 short ledgered. HTMX response rendered "Backfilled 2 long files: 0 cloud, 2 awaiting cloud." Post-conditions: the 2 eligible reset to AWAITING_CLOUD; the un-ledgered long file stayed ANALYSIS_FAILED (L4 EXISTS-ledger scoping excludes never-scheduled work); the short file stayed ANALYSIS_FAILED (duration filter); NO new process_file ledger row seeded for the held files (L3 — k8s skips the seed so recover_orphaned_work can't replay onto a local queue). ALL PASS.

## Summary

total: 3
passed: 2
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

[none — no code issues found. The one blocked item (#2) is a live-cluster prerequisite, deferred to the Phase 56 deployment, not a defect.]
