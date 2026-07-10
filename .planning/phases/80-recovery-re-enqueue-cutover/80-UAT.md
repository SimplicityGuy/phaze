---
status: passed
phase: 80-recovery-re-enqueue-cutover
source:
  - 80-01-SUMMARY.md
  - 80-02-SUMMARY.md
  - 80-03-SUMMARY.md
  - 80-04-SUMMARY.md
  - 80-05-SUMMARY.md
started: 2026-07-10T19:00:00Z
updated: 2026-07-10T19:00:00Z
---

## Current Test

number: 7
name: Zero FileRecord.state drift guard
expected: |
  The CI source guard fails if any FileRecord.state read creeps back into recovery/reconcile.
awaiting: none — all automated checks complete

> **Note on method:** Phase 80 is control-plane logic (recovery producer, reconcile cron,
> Alembic migration, source guards) with **no UI or interactive surface**. Per the user's
> request to run the UAT autonomously, each operator-observable outcome below was exercised
> by running the behavior's regression against a fresh ephemeral Postgres (5433) rather than
> by manual click-through. Results are the actual pytest outcomes.

## Tests

### 1. Recovery run completes without crashing
expected: A controller restart or a manual "Recover" (force=True) over a corpus that includes an orphaned metadata-FAILED file completes and re-drives correctly — no `TypeError` from the naive/aware datetime comparison that previously aborted the whole run (CR-02).
result: pass — 3 passed (`test_d10_gate_does_not_crash_on_db_read_ledger_row` DB-round-trip + D-10 cells A/B)

### 2. Never-scheduled files are left alone
expected: Clicking "Recover" on a corpus that has never-scheduled `discovered` files (no ledger row) does NOT re-enqueue them — the 2026-06-18 ~44.5K over-enqueue class cannot recur (SC-2).
result: pass — 1 passed (`test_sc2_never_scheduled_discovered_file_with_no_ledger_row_is_not_recovered`)

### 3. A terminally-failed analyze is never auto-re-driven
expected: A file whose analyze terminally failed (with a surviving ledger row) is treated domain-complete and never auto-re-enqueued by recovery — `FAILURE_IS_TERMINAL[analyze]` holds at the recovery layer (SC-3).
result: pass — 1 passed (`test_sc3_failed_analyze_with_surviving_ledger_row_is_terminal_never_reenqueued`)

### 4. Held long files go to compute, never analyzed locally
expected: An AWAITING_CLOUD long file recovered while only a fileserver is online is SKIPPED (left for the release cron), never analyzed locally on the fileserver; with a compute agent online it routes to compute (CLOUDROUTE-02 / CR-01).
result: pass — 3 passed (`test_held_process_file_orphan_is_not_analyzed_locally_on_a_fileserver`, `…_routes_to_a_compute_agent`, `test_held_file_with_process_file_seed_is_in_the_held_set`)

### 5. Migration 036 backfills analyzed files
expected: After `036`, every `state='analyzed'` file with NULL `analysis_completed_at` is backfilled (so it is not re-enqueued for 4h re-analysis after the cutover); `analysis_failed` rows are left untouched (NAND guard); the backfill is idempotent with a no-op downgrade (SC-4).
result: pass — 4 passed (`test_migration_036_backfill_analysis_completed_at.py`, incl. idempotent upgrade + downgrade + NAND-guard mutation)

### 6. Reconcile at-cap spill stays re-drivable
expected: When a kueue reconcile spills at cap, the file's `cloud_job` is re-stamped `status='awaiting'` (re-drivable), NOT `FAILED` with a `state=AWAITING_CLOUD` shadow-gate violation; MKUE-04 clean-before-flip ordering is preserved (attempts not incremented; staged object deleted under the lock before commit) (T-80-07/08/09).
result: pass — 30 passed (`test_reconcile_cloud_jobs.py`)

### 7. Zero FileRecord.state drift guard
expected: The CI AST guard fails if any `FileRecord.state` read (any of forms #1–#6) creeps back into `reenqueue.py` or `reconcile_cloud_jobs.py`; benign reads (`cloud_job.status`, `FileRecord.id`) do not trip it (SC-1).
result: pass — 13 passed (`test_reenqueue_reconcile_source_scan.py`, mutation-proven RED + GREEN false-positives)

## Summary

total: 7
passed: 7
issues: 0
pending: 0
skipped: 0
blocked: 0

Automated pytest total across the 7 groups: **55 tests passed, 0 failed** (ephemeral Postgres 5433 / Redis 6380).

## Gaps

None from the automated UAT.

**One operator-side verification remains (documented, not a CI gap):** the live-corpus
shadow-compare. It requires the production corpus (~1001 `analyzed` files with
`analysis_completed_at` NULL) that no CI fixture reproduces. Operator step, at deploy time:

1. Deploy migration `036` to prod.
2. Run `just shadow-compare` against a read-only prod replica.
3. It must exit 0 (the `analyzed` HARD invariant is now satisfied because `036` backfilled the
   NULL `analysis_completed_at` rows).

This is the same Manual-Only item recorded in `80-VALIDATION.md`; it is expected to be RED
*before* `036` is deployed (see the standing note that the `analyzed` invariant is RED on prod
until this migration lands) and must be confirmed green *after* deploy.
