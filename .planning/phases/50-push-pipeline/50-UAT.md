---
status: partial
phase: 50-push-pipeline
source: [50-00-SUMMARY.md, 50-01-SUMMARY.md, 50-02-SUMMARY.md, 50-03-SUMMARY.md, 50-04-SUMMARY.md, 50-05-SUMMARY.md, 50-06-SUMMARY.md, 50-07-SUMMARY.md]
started: 2026-06-26T19:24:48Z
updated: 2026-06-26T19:33:24Z
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: App starts from scratch with no errors; push-callback router mounted in main.py; `stage_cloud_window` cron registered on the controller; pipeline dashboard renders.
result: pass
evidence: Driven live by the assistant. Fresh ephemeral Postgres 18 + Redis 7 (docker run), `alembic upgrade head` applied migrations 001→024 clean, `uvicorn phaze.main:app` (PHAZE_ROLE=control) logged "Application startup complete." with no errors. `GET /pipeline/` → HTTP 200. `GET /openapi.json` exposes `/api/internal/agent/push/{file_id}/pushed` and `/api/internal/agent/push/{file_id}/mismatch`. `phaze.tasks.controller.settings` has `stage_cloud_window` in functions and cron_jobs = [refresh_tracklists, reap_stalled_scans, stage_cloud_window] (single staging cron). Approved by assistant-driven run 2026-06-26.

### 2. Cloud-window dashboard count cards
expected: On the pipeline dashboard, two new count cards appear — "Staged (pushing)" (PUSHING count) and "Analyzing (cloud)" (PUSHED count) — styled like the Phase-49 cards. Values live-update via the 5-second HTMX out-of-band poll without a full reload, and degrade to 0 (never a 500) if a count query fails.
result: pass
evidence: Live against the booted app — `GET /pipeline/` initial load renders `id="staged-pushing-card"` ("Staged (pushing)") and `id="analyzing-cloud-card"` ("Analyzing (cloud)") with no OOB; `GET /pipeline/stats` (HTTP 200) re-emits both `<section>`s carrying `hx-swap-oob="true"`. Degrade-safety (`_safe_count` → rollback + 0, poll never 500s) was driven live in 50-HUMAN-UAT (failing sibling count returned 200, cards still rendered) and is unit-covered green.

### 3. Long files held in AWAITING_CLOUD (no direct compute enqueue)
expected: When a long file is discovered (or backfilled), the routing seam always parks it in `AWAITING_CLOUD` rather than enqueuing analysis directly onto a compute agent. Observable as the "Awaiting Cloud" count rising for long files; no analysis job is dispatched outside the bounded window.
result: pass
evidence: `tests/test_routing_seam.py` (4 tests incl. `test_no_direct_to_compute_enqueue_path`) green against a fresh test DB. Code confirms `_route_discovered_by_duration` always sets `AWAITING_CLOUD` with `cloud` hard-wired to 0 (pipeline.py:315); security audit `grep cloud_files src/phaze/routers/pipeline.py` returned 0 — the direct-to-compute enqueue path was fully removed.

### 4. Bounded staging window (≤ cloud_max_in_flight)
expected: The `stage_cloud_window` cron tops up to at most `cloud_max_in_flight` files in flight — COUNT(PUSHING+PUSHED) never exceeds the cap, even across many AWAITING_CLOUD files. Repeated cron ticks / double-clicks do not double-stage (deterministic `push_file:<id>` key).
result: blocked
blocked_by: release-build
reason: "Window-math + deterministic-key logic verified green (tests/test_staging_cron.py, fresh-DB run). Live end-to-end staging requires an online compute + fileserver agent to actually flip rows to PUSHING and enqueue push_file — that cloud infrastructure (OCI A1 + Tailscale) is authored in Phase 51 but not yet deployed, so the live cron behavior can't be user-observed yet."

### 5. End-to-end push → sha256 verify → analyze
expected: A staged long file is rsync-pushed (fileserver → compute over Tailscale/SSH with pinned known_hosts), the compute agent verifies sha256 against the control-pinned hash, analyzes the scratch copy, and reports back; FileState advances PUSHING → PUSHED → ANALYZED and the scratch file is cleaned up.
result: blocked
blocked_by: release-build
reason: "Logic verified green (tests/test_push_pipeline.py + tests/test_process_file_scratch.py, 27 passed) with the rsync/ssh subprocess mocked. The real rsync-over-SSH-over-Tailscale transfer to a live OCI compute box needs the Phase 51 agent image (rsync/openssh-client) + a Tailscale-joined compute agent — not yet deployed. 50-VALIDATION.md already defers this to the Phase 51 deploy runbook."

### 6. Push mismatch / attempt cap
expected: A corrupt transfer (sha256 mismatch) → compute agent reports mismatch, scratch deleted, no analysis, push re-driven; after `push_max_attempts` mismatches the file goes to `ANALYSIS_FAILED` rather than looping.
result: blocked
blocked_by: release-build
reason: "Callback + attempt-cap logic verified green (tests/test_routers/test_agent_push.py, fresh-DB run) — mismatch over cap → ANALYSIS_FAILED + ledger clear; under cap → re-enqueue with incremented push_attempt; no-fileserver → clean 200 hold. Triggering a real mismatch requires a live transfer path (deployment-gated)."

### 7. Recovery re-drive of orphaned pushes
expected: After a worker crash/restart, files left in `PUSHING` are reclassified as orphaned and re-driven onto a fileserver-kind agent (never compute, never raising). Files already PUSHED/ANALYZED/ANALYSIS_FAILED are not re-driven.
result: blocked
blocked_by: release-build
reason: "Recovery classification + fileserver-kind routing verified green (tests/test_reenqueue.py, tests/test_tasks/test_recovery.py, tests/test_tasks/test_controller_reenqueue.py, fresh-DB run). Observing the live re-drive after an actual worker restart requires running agents against deployed cloud infra (deployment-gated)."

## Summary

total: 7
passed: 3
issues: 0
pending: 0
skipped: 0
blocked: 4

## Gaps

[none — no failures. 4 tests blocked on live cloud deployment (Phase 51 / v5.0 redeploy); all four have green automated logic coverage and are recorded as deployment-gated live verifications, consistent with 50-VALIDATION.md Manual-Only deferrals.]
