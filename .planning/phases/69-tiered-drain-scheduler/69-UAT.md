---
status: complete
phase: 69-tiered-drain-scheduler
source: [69-01-SUMMARY.md, 69-02-SUMMARY.md, 69-03-SUMMARY.md, 69-04-SUMMARY.md, 69-05-SUMMARY.md]
started: 2026-07-04T16:15:51Z
updated: 2026-07-04T16:15:51Z
mode: agent-driven
---

## Current Test

[complete — all automatable behaviors driven; 1 item deployment-gated to Phase 70]

## Tests

Phase 69 is backend-only (no UI). The verifier flagged no `human_verification` items. Rather than
ask the operator to click-test a headless scheduler, the agent **drove the real code**: the pure
`select_backend` policy via a live scenario matrix, and the DB-bound drain/reconcile/recovery
behaviors as live integration checks against the ephemeral `phaze-test-db`/`phaze-test-redis`
containers. Evidence is the actual routing decision / behavior, not the test suite alone.

### 1. Rank-first cheapest dispatch (SCHED-01)
expected: with compute(rank 10) + kueue(rank 50) + local(99) all available with free slots, the file routes to the cheapest available backend.
result: PASS — routed to `compute-a1` (rank 10). [driven: select_backend live]

### 2. Spill when the top rank is full (SCHED-01)
expected: compute(10) online but FULL, kueue(50) free → spill to kueue; local stays gated out (cloud online-but-full, not yet waited).
result: PASS — routed to `kueue-x64` (rank 50). [driven: select_backend live]

### 3. Offline→local immediate vs full→local staleness-gated (D-01/D-03)
expected: (a) all non-local OFFLINE → local eligible immediately; (b) cloud online-but-FULL, waited 60s (<900) → HOLD; (c) same but waited 1200s (>900) → local.
result: PASS — (a) routed to `local` immediately; (b) HOLD (clean no-op, stays AWAITING_CLOUD); (c) routed to `local` after threshold. [driven: select_backend live]

### 4. Attempt-exhaustion forces local, no A↔B thrash (D-04)
expected: cloud_attempts=3 (=cloud_submit_max_attempts) with a free compute slot → forced to local anyway (cloud excluded); local is never excluded (safety net).
result: PASS — routed to `local` despite an open compute slot. [driven: select_backend live]

### 5. Equal-rank tie-break: utilization then stable id (SCHED-04)
expected: two rank-20 backends → lower in_flight/cap utilization wins; on equal utilization, lexicographically-lower id wins.
result: PASS — lower-util `kueue-bravo` won; on equal util `kueue-alpha` won by id. [driven: select_backend live]

### 6. Multi-backend drain dispatches across N backends in one tick (SCHED-01)
expected: one drain tick snapshots every resolved backend and dispatches candidates rank-first across N (>1 non-local) backends — the Phase-68 ">1 non-local" boot guard is gone.
result: PASS — 2 integration cases green. [driven: pytest test_staging_cron.py -k "multi_backend or spill", live DB]

### 7. Per-backend cap never overshoots under overlapping ticks (SCHED-02)
expected: count-and-claim under `pg_advisory_xact_lock(5_000_504)`; two overlapping ticks serialize on the lock → no backend exceeds its cap.
result: PASS — overshoot guard green. [driven: pytest test_staging_cron.py -k overshoot, live DB]

### 8. At-cap failure spills back to AWAITING_CLOUD, not ANALYSIS_FAILED (SCHED-03)
expected: a cloud-failed file at the attempt ceiling returns to AWAITING_CLOUD (attempts maxed so the next tick forces local) — local failure is the only ANALYSIS_FAILED terminal.
result: PASS — spill-back green. [driven: pytest test_reconcile_cloud_jobs.py -k spill_back, live DB]

### 9. Exactly one recovery owner per kind; reconcile is backend_id-scoped (SCHED-05)
expected: a compute file with an in-flight cloud_job is excluded from the recovery-ledger orphan set (single owner, no 44.5k-replay); kueue reconcile never touches compute rows.
result: PASS — 3 recovery + 1 scope case green. [driven: pytest test_recovery.py -k "single_owner or in_flight" + test_backends.py -k reconcile_scope, live DB]

### 10. CR-01 fix: locally-spilled file leaves the candidate set (SCHED-01/03)
expected: `LocalBackend.dispatch` flips the file to `LOCAL_ANALYZING`, excluding it from `get_cloud_staging_candidates`; on the next tick a freed cloud slot does NOT re-dispatch it (no cross-backend double-analysis, no leaked compute slot).
result: PASS — two-tick no-re-dispatch case + LOCAL_ANALYZING flip/exclusion cases green; AWAITING_CLOUD `updated_at` staleness guard green. [driven: pytest test_staging_cron.py -k "local_spill_not_redispatched" + test_backends.py -k local_analyzing + -k awaiting_untouched, live DB]

### 11. Live simultaneous multi-backend E2E (Kueue + compute + local at once)
expected: with a real Kueue cluster + compute agent + fileserver all online, long files drain across all three simultaneously, cheapest-rank-first, under live caps.
result: SKIPPED — deployment-gated. No live Kueue cluster / compute agent in this environment; matches the Phase-68 precedent and the VALIDATION.md manual-only note. Deferred to Phase 70 rollout (which also carries the deferred WR-02/WR-03 registry-validation follow-ups).

## Summary

total: 11
passed: 10
issues: 0
pending: 0
skipped: 1
blocked: 0

All 10 automatable behaviors were driven live against the real code (9/9 select_backend routing
scenarios + 8 DB-bound integration behaviors). The single skipped item is deployment-gated to
Phase 70, not a defect.

## Gaps

None. No UAT issues found. (Non-blocking follow-ups WR-02 and WR-03 are already tracked as
Phase-70 work in 69-VERIFICATION.md `deferred:` and 69-SECURITY.md — not UAT gaps.)
