---
status: partial
phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker
source: [37-VERIFICATION.md]
started: 2026-06-13
updated: 2026-06-13
---

## Current Test

[blocked — Phase 37 not yet deployed to homelab; re-run /gsd:verify-work 37 after deploy]

## Tests

### 1. Live backlog reprioritization observed end-to-end on homelab
expected: After deploying the phase and enqueueing a real stage backlog on the homelab Postgres broker, `POST /pipeline/stages/{stage}/priority` with a delta (e.g. `{"delta": -10}` on `analyze`) reorders the queued backlog so lower-priority jobs dequeue sooner, observable via `/saq`.
why_human: Requires a live running backlog on the homelab Postgres broker with real agent workers consuming jobs — the ephemeral integration-test DB proves the SQL semantics but not end-to-end deployed behavior.
result: blocked
blocked_by: not-deployed
reason: "User confirmed Phase 37 is not deployed — still on the unmerged local gsd/phase-37-… branch. Requires homelab deploy (PR → merge → release → redeploy via datum@nox / datum@lux) before this can be exercised."

### 2. Pause across reboot re-applies to Phase-32 re-enqueued jobs
expected: After `POST /pipeline/stages/analyze/pause`, rebooting the phaze-api + phaze-worker containers, the Phase-32 reboot re-enqueue path re-parks jobs — re-enqueued `analyze` jobs have `scheduled = SENTINEL` in `saq_jobs` and do not dequeue until `POST /pipeline/stages/analyze/resume`.
why_human: Requires a real reboot cycle on homelab, the Phase-32 re-enqueue path, and homelab Postgres access — not reproducible locally.
result: blocked
blocked_by: not-deployed
reason: "Same prerequisite as Test 1 — Phase 37 not deployed to homelab. Reboot-cycle verification cannot run locally."

## Summary

total: 2
passed: 0
issues: 0
pending: 0
skipped: 0
blocked: 2

## Gaps

[none — both open items are deployment-prerequisite gates, not code issues]
