---
status: partial
phase: 43-analyze-throughput-fix-bound-per-file-analysis-cost-kill-on
source: [43-VERIFICATION.md]
started: 2026-06-17T00:00:00Z
updated: 2026-06-17T00:00:00Z
---

## Current Test

[awaiting human testing — requires homelab redeploy]

## Tests

### 1. Bounded cost + deterministic kill on a real long set
expected: Deploy to the homelab and trigger a file known to previously exceed the 4h timeout (a 3h+ DJ set). Monitor logs and the SAQ UI. The job completes in minutes (not hours); if the file is still too long the pebble inner timeout fires at 6600s and the slot is reclaimed; `files.state` advances to `analyzed` (or `analysis_failed` on terminal); CPU/slot usage drops after completion; no blind retry for timeout/crash outcomes.
result: [pending]

### 2. State advances out of `discovered` on the live archive
expected: After redeploy, check the pipeline dashboard for `analyzed` vs `discovered` state counts. Processed files show `analyzed` or `analysis_failed` in `files.state` (not stuck `discovered`). The latent re-enqueue-all bug (all 11,428 stuck at `discovered`) no longer reproduces on a fresh trigger.
result: [pending]

## Summary

total: 2
passed: 0
issues: 0
pending: 2
skipped: 0
blocked: 0

## Gaps
