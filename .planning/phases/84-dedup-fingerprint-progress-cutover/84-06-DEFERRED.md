---
plan: 84-06
status: deferred
blocking: true
gate: pre-merge
deferred_on: 2026-07-10
requirement: SIDECAR-02
decision: D-16.2
---

# 84-06 — Live-Corpus Shadow-Compare: DEFERRED (blocking pre-merge gate)

Plan 84-06 was **not executed**. It is `autonomous: false` and requires a restore of the
live corpus plus an operator-supplied DSN, neither of which the executor has access to.

**This phase is NOT complete.** Plans 84-01 … 84-05 are done and verified; 84-06 remains open.

## Why this cannot be skipped

D-16 proves SC#3 two ways:

1. **D-16.1 — committed CI test.** Shipped in 84-03
   (`tests/integration/test_dedup_resolve_undo_shadow.py`): asserts
   `run_shadow_compare(session).hard_fail_total == 0` across `resolve → undo → re-resolve`
   on a *synthetic* corpus. This gates every future PR.

2. **D-16.2 — live-corpus run.** ← **THIS, still open.**

The CI test **provably cannot** cover D-16.2. Its corpus is constructed by the test itself,
so it can never contain the real post-`032` `state='duplicate_resolved'`-without-marker rows
that D-01 discovered. Only a run against a production restore proves migration `035` actually
repaired them.

Phase 79 built this gate to be re-runnable and then **deferred the live run** (79 D-02).
That deferral is precisely why D-01 — a `dedup_resolution` table with no go-forward writer
since migration `032` — went unnoticed across two phases. Deferring it a second time without
closing it before merge would repeat the exact failure.

## Runbook

```bash
# 1. Restore a recent live snapshot into a DB whose name ends in _test
#    (shadow_compare's destructive-write guard refuses any other name; the run is read-only)

# 2. Apply 035
uv run alembic upgrade head

# 3. Capture the BEFORE reading (for the SUMMARY)
curl -s localhost:8000/api/v1/fingerprint/progress   # or the justfile:500 recipe

# 4. The gate — exits 1 iff any HARD invariant diverged
just shadow-compare --database-url <restore-dsn>

# 5. Capture the AFTER reading
```

## Pass condition

- Exit code `0`
- `TOTALS: hard_fail_total=0`
- The `duplicate_resolved` invariant line reads `0 divergent`

## Fail condition

`hard_fail_total > 0` means files that `035` did not repair. **Do not merge.** Return to
84-01 and widen the reconcile.

## Recording rules

- Record only the invariant TOTALS and the before/after counts. **Never paste the `--database-url`
  DSN** (it carries a password) into the SUMMARY, the PR body, or any committed log. `make_url`
  keeps the DSN password-safe in the tool's own output — do not defeat that by echoing it.
- The `completed` jump and `failed` drop in `get_fingerprint_progress` are **the fix, not a
  regression** (D-11). `completed` previously read `state == FINGERPRINTED`, whose sole writer is
  `retry_analysis_failed`, so it counted almost nothing; `failed` was a per-engine **row** count
  that double-counted files and misclassified one-success/one-failure files. Frame both deltas as
  intended in the SUMMARY so they are not read as breakage.

## To close

Run the runbook, then write `84-06-SUMMARY.md` with the recorded TOTALS and count deltas,
and mark the phase complete via `/gsd:execute-phase 84` (it will resume at the only
incomplete plan).
