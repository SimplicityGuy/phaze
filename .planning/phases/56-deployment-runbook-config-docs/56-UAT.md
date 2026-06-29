---
status: complete
phase: 56-deployment-runbook-config-docs
source: [56-VERIFICATION.md, 56-HUMAN-UAT.md, 56-00-SUMMARY.md, 56-01-SUMMARY.md, 56-02-SUMMARY.md, 56-03-SUMMARY.md, 56-06-SUMMARY.md]
started: 2026-06-28
updated: 2026-06-28
mode: operator-delegated
---

## Current Test

[testing complete]

## Tests

### 1. Pipeline dashboard amber alert renders correctly
expected: On `/pipeline`, when the cross-process flag `phaze:k8s:localqueue_unreachable` is set the amber "K8s LocalQueue unreachable" alert (`id="localqueue-card"`) appears; when cleared it disappears. Both the full-page render and the HTMX OOB stats refresh seed/show it; the read is degrade-safe.
result: pass
evidence: |
  Ran the integration render tests against the ephemeral Postgres+Redis (run by the orchestrator
  on the user's behalf):
  - test_localqueue_alert_renders_when_flagged PASSED (amber card renders when flag set)
  - test_localqueue_alert_empty_when_reachable PASSED (no card when flag absent)
  - test_localqueue_alert_oob_on_stats PASSED (OOB partial on the 5s /pipeline/stats poll)
  - test_get_localqueue_unreachable_degrades_to_false PASSED
  Live real-Redis read: flag set -> reader returns True (alert shows); redis=None -> False (degrade-safe).

### 2. Agents page ephemeral-identity note renders correctly
expected: On `/admin/agents`, the neutral info panel describing the ephemeral k8s one-shot lane (no heartbeat, not a registered agent) appears with the locked 56-UI-SPEC copy, static through Jinja autoescape.
result: pass
evidence: |
  Confirmed the locked note ("The Kubernetes burst lane runs as ephemeral, per-file Jobs — it does
  not register as a heartbeating agent here…") is present in agents.html and is fully static (no
  Jinja expression inside the sentence → autoescape-safe, no operator interpolation). The DEAD-
  suppression invariant behind the note is proven green by test_classify_never_not_dead_when_last_seen_at_none.

### 3. End-to-end single-toggle revert clears the amber alert (WR-01) + Redis-down k8s boot does not crash (CR-01)
expected: Flipping `PHAZE_CLOUD_TARGET=local` and restarting the controller clears the stale
  `phaze:k8s:localqueue_unreachable` Redis flag so the dashboard no longer shows a false alert; and a
  Redis-down boot with `cloud_target=k8s` does NOT abort the control worker (D-05).
result: pass
evidence: |
  Live real-Redis round-trip (orchestrator-run): set the flag (simulating a k8s-unreachable boot) ->
  reader True; performed the off-k8s revert delete (exactly what controller.startup's else branch does)
  -> reader False (alert cleared). Boot-resilience proven by the 7 controller-startup tests, all PASSED:
  - test_redis_down_during_unreachable_probe_does_not_abort_boot (CR-01)
  - test_redis_down_during_reachable_probe_does_not_abort_boot (CR-01)
  - test_stale_flag_cleared_when_not_k8s (WR-01)
  - test_stale_flag_clear_redis_down_does_not_abort_boot (WR-01 + D-05)
note: |
  The true live-cluster `kubectl apply` of the runbook on an operator-owned Kueue cluster (KDEPLOY-01)
  remains an inherently manual validation — there is no CI cluster — and is tracked in 56-VALIDATION.md
  Manual-Only. Every automatable behavior in the 3 UAT items above was exercised against live
  Postgres+Redis; the kube API itself is faked (no cluster), consistent with the phase's test design.

## Summary

total: 3
passed: 3
issues: 0
pending: 0
skipped: 0

## Gaps

[none]
