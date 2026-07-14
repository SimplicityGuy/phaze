---
phase: 80-recovery-re-enqueue-cutover
reviewed: 2026-07-10T17:56:04Z
depth: standard
files_reviewed: 5
files_reviewed_list:
  - alembic/versions/036_backfill_analysis_completed_at.py
  - src/phaze/services/pipeline.py
  - src/phaze/services/stage_status.py
  - src/phaze/tasks/reconcile_cloud_jobs.py
  - src/phaze/tasks/reenqueue.py
findings:
  critical: 2
  warning: 1
  info: 0
  total: 3
status: resolved
resolution:
  resolved_at: 2026-07-10
  resolved_in: 5da4036e
  cr_01: "FIXED — _get_awaiting_cloud_ids no longer conjoins ~inflight_clause; held files reach held_agent_rows; mutation-proven regression + compute-routing companion"
  cr_02: "FIXED — D-10 gate coerces naive ledger enqueued_at to UTC-aware; DB-round-trip regression via get_ledger_rows (mutation-proven RED)"
  wr_01: "ACKNOWLEDGED — benign under the advisory lock; left as-is (minor)"
---

# Phase 80: Code Review Report

**Reviewed:** 2026-07-10T17:56:04Z
**Depth:** standard
**Files Reviewed:** 5
**Status:** issues_found

## Summary

Phase 80 cuts recovery/reconcile over from `FileRecord.state` reads to the Phase-78/81 stage_status
predicate layer + the `cloud_job` sidecar, adds `awaiting_candidate_clause()` as the single-source
awaiting-cloud predicate, ledger-scopes every done-set query with a `= ANY(:ids)` array bind, and
backfills `analysis.analysis_completed_at` via migration 036.

The migration is clean (static, parameter-free SQL; NAND-guarded; idempotent; no-op downgrade — no
injection surface, matches its stated contract), and the reconcile at-cap CAS ordering
(clean-before-flip; no pre-mutation of `cloud_job.status` before `hold_awaiting_cloud`'s CAS) is
correct — an autoflush cannot make the CAS miss its own row because the object is not dirtied until
after the CAS runs.

However, two BLOCKER-level correctness regressions slipped through, both in `reenqueue.py`, and both
masked by tests that exercise in-memory objects instead of round-tripping through the database. One
re-introduces the Phase-49 CLOUDROUTE-02 bug for compute-backend deployments; the other crashes the
entire recovery run with a `TypeError` whenever the D-10 metadata-failed cell is reached in
production.

## Critical Issues

### CR-01: Held AWAITING_CLOUD long files route kind-agnostically on recovery — CLOUDROUTE-02 regression

**File:** `src/phaze/tasks/reenqueue.py:300-316` (`_get_awaiting_cloud_ids`), consumed at `reenqueue.py:492-497`
**Issue:**
`_get_awaiting_cloud_ids` was cut over from `FileRecord.state == AWAITING_CLOUD` to
`awaiting_candidate_clause()`, which conjoins `~inflight_clause(Stage.ANALYZE)`. `inflight_clause(ANALYZE)`
is True iff a `process_file:<file_id>` scheduling-ledger row exists (`stage_status.py:190-193`, ledger
is the sole source — `saq_jobs` never flips it). Therefore `held_ids` can NEVER contain a file that
has a `process_file` ledger row.

But a compute-path held long file DOES carry a `process_file` ledger row: the D-09 held-file seed at
`routers/pipeline.py:865-874` inserts `insert_ledger_if_absent(key=process_file:<id>, function="process_file", ...)`
for every file the duration router parks in `AWAITING_CLOUD` when the resolved backend is not kueue
(`routers/pipeline.py:849` forks — only the kueue branch skips the seed; the compute branch seeds).
This seed is unchanged by Phase 80 and is live for every compute deployment (the phaze homelab runs a
compute agent).

Consequences in `recover_orphaned_work`:
- The held file has `cloud_job.status='awaiting'`, which is NOT in `IN_FLIGHT`, so
  `_in_flight_cloud_job_ids` does not exclude it (`reenqueue.py:334`); it is not analyze-done, so it is
  not domain-completed — it reaches `orphaned`.
- Its file has a `process_file` ledger row ⇒ `~inflight_clause(ANALYZE)` is False ⇒ it is excluded from
  `held_ids` ⇒ `held_agent_rows` (`reenqueue.py:493`) is empty.
- It therefore falls into `other_agent_rows` (`reenqueue.py:497`) and is routed via the kind-agnostic
  `select_active_agent(session)` (`reenqueue.py:538`) — typically a fileserver, because "no compute
  agent online" is the exact condition that held the file (as the code's own comment at
  `reenqueue.py:489-491` states). The long file is then analyzed LOCALLY on the fileserver.

This is precisely the CLOUDROUTE-02 violation the Phase-49 CR-01 held-routing was written to prevent,
re-introduced by the cutover. The 80-04 SUMMARY's safety claim ("genuinely-parked long files carry NO
process_file ledger row — the hold path parks without enqueuing") is contradicted by the live compute
D-09 seed; the reasoning conflated the kueue path (which skips the seed) with the compute path (which
seeds). The `held_agent_rows` compute-only branch (`reenqueue.py:504-516`) is now unreachable dead
code that was silently supposed to fire.

**Fix:**
The recovery held-routing must NOT use the `~inflight_clause` conjunct, because during recovery every
row being recovered is a ledger row by construction, so `inflight_clause` is always True for exactly
those files. Give recovery its own predicate that identifies a held long file by its sidecar alone,
without the drain's in-flight exclusion:

```python
async def _get_awaiting_cloud_ids(session: AsyncSession) -> set[str]:
    # Recovery-specific: a held long file is one with an 'awaiting' cloud_job sidecar that has NOT
    # domain-completed its analyze. Do NOT conjoin ~inflight_clause(ANALYZE): the D-09 held-file
    # seed gives every held compute file a process_file ledger row, which makes inflight_clause
    # True by construction and would wrongly drop it from the compute-only routing set (CLOUDROUTE-02).
    stmt = (
        select(FileRecord.id)
        .select_from(FileRecord)
        .join(CloudJob, CloudJob.file_id == FileRecord.id)
        .where(
            CloudJob.status == CloudJobStatus.AWAITING.value,
            ~domain_completed_clause(Stage.ANALYZE),
        )
    )
    return {str(fid) for fid in (await session.scalars(stmt)).all()}
```

Add a routing test that round-trips through `get_ledger_rows` (a committed `process_file` ledger row
for an `awaiting`/`AWAITING_CLOUD` file must land in `held_agent_rows`, never `other_agent_rows`).

### CR-02: D-10 metadata gate raises TypeError (naive vs aware datetime) — crashes the whole recovery run

**File:** `src/phaze/tasks/reenqueue.py:390` (`is_domain_completed`)
**Issue:**
The D-10 gate compares two timestamp columns of DIFFERENT awareness:

```python
return row.enqueued_at <= failed_at
```

- `row.enqueued_at` comes from `SchedulingLedger.enqueued_at`, declared `sa.DateTime()` (no timezone)
  in `models/scheduling_ledger.py:63` and created as `sa.Column("enqueued_at", sa.DateTime(), ...)` in
  `alembic/versions/022_add_scheduling_ledger.py:57` — a `TIMESTAMP WITHOUT TIME ZONE` column, which
  asyncpg returns as a **naive** `datetime`.
- `failed_at` comes from `FileMetadata.failed_at`, declared `DateTime(timezone=True)` in
  `models/metadata.py:33` — a `TIMESTAMP WITH TIME ZONE` column, which asyncpg returns as an
  **aware** `datetime`.

In production, `recover_orphaned_work` reads the ledger fresh via `rows = await get_ledger_rows(session)`
(`reenqueue.py:464`), so `row.enqueued_at` is naive. `naive <= aware` raises
`TypeError: can't compare offset-naive and offset-aware datetimes`. There is no `try/except` around the
`orphaned = [r for r in rows if ... not is_domain_completed(r, done_sets) ...]` comprehension
(`reenqueue.py:475`), so the exception propagates out of `recover_orphaned_work` and aborts the entire
recovery run — both the manual "Recover" button and the controller startup hook.

This fires whenever an orphaned `extract_file_metadata` ledger row exists for a file whose metadata is
FAILED (`failed_at` set) — a realistic path, since `retry_metadata_failed` leaves `failed_at` set
(81 D-11) and a lost failure-callback leaves the ledger row behind (exactly the D-10 cell this code was
written for).

The D-10 Cell A/B tests (`tests/analyze/tasks/test_recovery.py:1295-1338`) pass only because
`_metadata_done_sets_for` (`test_recovery.py:1286-1292`) constructs the `SchedulingLedger` row IN MEMORY
with `enqueued_at=datetime.now(UTC) ± timedelta` (tz-aware) and passes that object straight to
`is_domain_completed` — it never round-trips through `get_ledger_rows`, so both sides are aware in the
test and the mismatch is invisible.

**Fix:**
Normalize both operands to the same awareness before comparison. Simplest is to coerce the naive
ledger timestamp to UTC-aware at the comparison site:

```python
from datetime import UTC
...
enqueued_at = row.enqueued_at if row.enqueued_at.tzinfo is not None else row.enqueued_at.replace(tzinfo=UTC)
return enqueued_at <= failed_at
```

Better long-term, make `SchedulingLedger.enqueued_at` a `DateTime(timezone=True)` column (aligning it
with `TimestampMixin`'s columns and `metadata.failed_at`) so all timestamp comparisons are apples-to-
apples. Then change the D-10 tests to seed the ledger row and re-read it via `get_ledger_rows` so a
naive/aware mismatch would turn them RED.

## Warnings

### WR-01: Reconcile at-cap spill ignores `hold_awaiting_cloud`'s CAS return value

**File:** `src/phaze/tasks/reconcile_cloud_jobs.py:231-243`
**Issue:**
`hold_awaiting_cloud`'s spill mode returns `res.rowcount > 0`, and its documented contract
(`backends.py:111-115`) is that a `False` return means a late/duplicate write matched an already-
advanced row and "the CALLER keeps its FULL no-op (no FileRecord write, no cleanup, no ledger clear)".
The three sibling callers honor this. The reconcile at-cap path does not check the return: regardless of
whether the CAS hit, it proceeds to set `cloud_job.inadmissible = False`, `cloud_job.staging_bucket = None`,
`session.commit()`, `delete_job(...)`, and `tally["failed"] += 1`.

In practice this path holds the per-row advisory lock and only processes `SUBMITTED`/`RUNNING` rows, and
`expect_status=(SUBMITTED, RUNNING)` matches, so the CAS normally hits and the deviation is benign. But
if the row were concurrently advanced by a non-locked writer, reconcile would still delete the Job and
count it `failed` for a row it did not actually spill — a metric/state inconsistency the contract was
designed to prevent. Lower severity than the two blockers (unlikely under the lock), but it is an
undocumented divergence from the spill-writer contract.

**Fix:** Capture and branch on the return, mirroring the sibling callers:

```python
spilled = await hold_awaiting_cloud(session, file, attempts=cap, expect_status=(...), clear_cloud_phase=True)
if not spilled:
    # late/duplicate: keep the full no-op, do not re-stamp/commit/delete as a spill
    await session.commit()  # release the lock only
    return
```

---

_Reviewed: 2026-07-10T17:56:04Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
