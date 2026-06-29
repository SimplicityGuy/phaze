---
status: complete
phase: 56-deployment-runbook-config-docs
source: [56-VERIFICATION.md]
started: 2026-06-28
updated: 2026-06-28
---

## Current Test

[awaiting human testing]

## Tests

### 1. Pipeline dashboard amber alert renders correctly
expected: On `/pipeline`, when the cross-process flag `phaze:k8s:localqueue_unreachable` is set, the amber "K8s LocalQueue unreachable" alert (`id="localqueue-card"`) appears; when the flag is cleared it disappears. Both the full-page render and the HTMX OOB stats refresh paths must seed/show it. (Code wiring verified; the 3 DB-dependent render tests need a live Postgres+Redis — they pass in CI / `just integration-test`.)
result: pass (verified by orchestrator against live Postgres+Redis — see 56-UAT.md)

### 2. Agents page ephemeral-identity note renders correctly
expected: On `/admin/agents`, the neutral gray informational panel describing the ephemeral k8s one-shot lane (no heartbeat → classified "never", never "dead") appears in the correct position.
result: pass (verified by orchestrator against live Postgres+Redis — see 56-UAT.md)

### 3. End-to-end single-toggle revert clears the amber alert (WR-01)
expected: With a live deployment that had `PHAZE_CLOUD_TARGET=k8s` and a set unreachable flag, flipping `PHAZE_CLOUD_TARGET=local` and restarting the controller clears the stale `phaze:k8s:localqueue_unreachable` Redis flag (the new off-k8s else branch), so the dashboard no longer shows a perpetual false alert. Also confirm a Redis-down boot with `cloud_target=k8s` does NOT crash the controller (CR-01 / D-05).
result: pass (verified by orchestrator against live Postgres+Redis — see 56-UAT.md)

## Summary

total: 3
passed: 3
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps
