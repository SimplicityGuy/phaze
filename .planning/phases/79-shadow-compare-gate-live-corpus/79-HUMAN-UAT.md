---
status: partial
phase: 79-shadow-compare-gate-live-corpus
source: [79-VERIFICATION.md]
started: 2026-07-08T00:00:00Z
updated: 2026-07-08T00:00:00Z
---

## Current Test

[awaiting human testing — deferred to homelab rollout per D-02]

## Tests

### 1. Gate passes on a restore of the live ~200K-file corpus after the `032` backfill
expected: `just shadow-compare --database-url <restore-dsn>` (or `python -m phaze.cli.shadow_compare --database-url <restore-dsn>`) exits 0, or exits 1 only on FINGERPRINTED/LOCAL_ANALYZING (soft) divergence with all HARD invariants at zero. Record the run's output in 79-VERIFICATION.md.
result: [pending]
why_human: No live corpus dump is available to this worktree/verifier. Per 79-CONTEXT.md decision D-02 this run is explicitly deferred to the next homelab rollout and tracked as the sole Manual-Only verification in 79-VALIDATION.md. ROADMAP Success Criterion 3 requires this run's output recorded in VERIFICATION before Phase 90's destructive `033` migration proceeds.

## Summary

total: 1
passed: 0
issues: 0
pending: 1
skipped: 0
blocked: 0

## Gaps
