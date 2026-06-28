---
phase: 55-routing-state-ledger-integration-the-live-seam
reviewed: 2026-06-28T00:00:00Z
depth: standard
files_reviewed: 14
files_reviewed_list:
  - alembic/versions/027_add_cloud_job_cloud_phase.py
  - src/phaze/config.py
  - src/phaze/models/cloud_job.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/routers/pipeline.py
  - src/phaze/services/cloud_staging.py
  - src/phaze/services/pipeline.py
  - src/phaze/tasks/reconcile_cloud_jobs.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - src/phaze/tasks/submit_cloud_job.py
  - src/phaze/templates/pipeline/dashboard.html
  - src/phaze/templates/pipeline/partials/admission_state_card.html
  - src/phaze/templates/pipeline/partials/backfill_response.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
findings:
  critical: 1
  warning: 2
  info: 0
  total: 3
status: resolved
resolution:
  CR-01: fixed in b169382 (report_upload_failed terminal -> ANALYSIS_FAILED)
  WR-01: fixed in b169382 (clear cloud_phase on both terminal paths)
  WR-02: deferred (S3 multipart orphan; TTL-backstopped, pre-existing robustness tradeoff)
---

# Phase 55: Code Review Report

**Reviewed:** 2026-06-28
**Depth:** standard
**Files Reviewed:** 14
**Status:** resolved (CR-01 + WR-01 fixed in b169382; WR-02 deferred — see resolution)

## Summary

Phase 55 wires `cloud_target` as the single routing selector (replacing the Phase 51 boolean), forks `stage_cloud_window` and `report_uploaded` per target (a1 vs k8s), adds the `cloud_phase` Kueue admission progression column (migration 027), scopes backfill to ledger-tracked work, and extends the dashboard with per-phase admission counts.

The config validators, routing seam, ledger-scoped backfill, advisory-lock window, and migration are all correct. One blocker was found: the S3 upload failure cap path in `report_upload_failed` does not transition the `FileRecord` to `ANALYSIS_FAILED`, leaving files permanently stuck in `PUSHING` and consuming cloud window slots forever. Two warnings: `cloud_phase` is not cleared on any terminal failure path (FAILED rows permanently inflate the admission-state dashboard), and the k8s staging loop has an S3 multipart orphan risk on partial loop failure.

## Critical Issues

### CR-01: `report_upload_failed` at cap leaves `FileRecord` stuck in `PUSHING` forever

**File:** `src/phaze/routers/agent_s3.py:173-189`

**Issue:** When `next_attempt > settings.push_max_attempts` the cap-exceeded branch correctly sets `cloud_job.status = FAILED`, aborts the multipart, deletes the staged object, and clears the ledger. But it never updates `FileRecord.state`. The file remains permanently in `FileState.PUSHING`.

Consequences:
- `get_cloud_window_count` (which counts `state IN {PUSHING, PUSHED}`) permanently includes the stuck file. With `cloud_max_in_flight = 2` (default), one stuck file cuts effective capacity to 1; two stuck files freeze the cloud pipeline entirely.
- The operator sees a persistent non-zero "Staged (pushing)" count card with no explanation and no path to remediation.
- The stuck file cannot be backfilled (it is not `ANALYSIS_FAILED`, so `_backfill_candidates_stmt`'s filter excludes it).

Both analogous terminal-failure callers set `ANALYSIS_FAILED`:
- `reconcile_cloud_jobs._handle_no_callback_terminal` (cap path): `await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYSIS_FAILED))`
- `agent_push.report_push_mismatch` (cap path): same pattern

The omission is specific to the S3 upload failure path added in Phase 53/55.

**Fix:**
```python
# After: await session.execute(update(CloudJob)...values(status=CloudJobStatus.FAILED.value))
# Add before session.commit():
await session.execute(
    update(FileRecord)
    .where(FileRecord.id == file_id)
    .values(state=FileState.ANALYSIS_FAILED)
)
```

The `FileRecord` import and `update` are already present in the module. Add the import of `FileState` if not present (it is already imported at line 40).

## Warnings

### WR-01: `cloud_phase` not cleared on terminal FAILED `cloud_job` — FAILED rows permanently inflate admission-state counts

**File:** `src/phaze/tasks/reconcile_cloud_jobs.py:164-173` and `src/phaze/routers/agent_s3.py:173-189`

**Issue:** Both terminal-failure cap paths set `cloud_job.status = FAILED` (and `inadmissible = False` per CR-01 / the existing guard), but neither sets `cloud_phase = None`. A `cloud_job` row that was in phase `RUNNING` when it failed retains `cloud_phase = "running"` indefinitely.

`get_cloud_phase_counts` in `services/pipeline.py` (lines 857-878) has no `status` filter — it counts ALL `cloud_job` rows by `cloud_phase` value. So every terminal FAILED row is permanently counted in whichever admission-phase bucket it happened to be in at failure time (e.g., "Running" count shows a file that actually failed). The counts never return to zero as long as FAILED rows exist, confusing any operator using the admission-state card to monitor active work.

The success path (`_record_success`) correctly sets `cloud_phase = CloudPhase.FINISHED.value`, which is semantically distinct from active phases. The failure path has no equivalent cleanup.

**Fix — `reconcile_cloud_jobs._handle_no_callback_terminal`, cap branch (~line 166):**
```python
if next_attempt > cap:
    cloud_job.status = CloudJobStatus.FAILED.value
    cloud_job.cloud_phase = None          # <-- add: clear admission-state on terminal failure
    cloud_job.inadmissible = False
    await session.execute(update(FileRecord)...)
    ...
```

**Fix — `agent_s3.report_upload_failed`, cap branch (~line 175):**
```python
await session.execute(
    update(CloudJob)
    .where(CloudJob.file_id == file_id)
    .values(status=CloudJobStatus.FAILED.value, cloud_phase=None)  # <-- add cloud_phase=None
)
```

Alternatively, scope `get_cloud_phase_counts` to in-flight rows only:
```python
select(func.count(CloudJob.id)).where(
    CloudJob.cloud_phase == CloudPhase.RUNNING.value,
    CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
)
```
Either fix is correct; clearing at write time is preferable (the column stays self-consistent).

### WR-02: k8s staging loop in `stage_cloud_window` orphans S3 multiparts on mid-loop failure

**File:** `src/phaze/tasks/release_awaiting_cloud.py:173-189` (the k8s candidate loop)

**Issue:** The k8s branch of `stage_cloud_window` calls `_stage_file_to_s3(session, file, task_router)` for each candidate in a shared transaction that commits ONCE after the loop (the advisory-lock L1 invariant). `_stage_file_to_s3` calls `s3_staging.create_multipart_upload` (an external S3 operation) before any DB writes for that candidate.

If the N-th candidate's call to `_stage_file_to_s3` raises (S3 error, DB error, or `NoActiveAgentError` from the redundant inner `select_active_agent` call) after the first N-1 candidates have already created their S3 multiparts, the exception propagates out of the loop. The advisory-lock context manager rolls back the entire DB transaction (clean), but the S3 multipart uploads created for candidates 1 through N-1 are **not cleaned up** — S3 is non-transactional. Those multiparts remain as orphaned in-flight objects until the S3 lifecycle TTL (default `s3_lifecycle_ttl_days = 2`) removes them.

This is distinct from the single-file `stage_file_to_s3` path (called by `redrive_upload`), which has a documented best-effort abort in `redrive_upload`'s `contextlib.suppress(Exception)` wrapper. No equivalent abort exists for the mid-loop failure case.

Additionally, the inner `select_active_agent(session, kind="fileserver")` call inside `_stage_file_to_s3` is redundant: GATE 2 in `stage_cloud_window` already resolved the fileserver agent (line 163) in the same session and transaction. This N+1 DB query pattern is harmless in practice (the agent won't change within the transaction window), but it conflates the GATE 2 resolution with the per-call resolution.

**Fix (safest):** Add a `try/except` around `_stage_file_to_s3` inside the loop that aborts the just-initiated multipart on failure before re-raising, preventing orphans even on partial loop failure:

```python
for file in candidates:
    file.state = FileState.PUSHING
    if cfg.cloud_target == "k8s":
        try:
            await _stage_file_to_s3(session, file, task_router)
        except Exception:
            # Best-effort abort to avoid orphaned multipart; re-raise so the
            # advisory-lock context manager rolls back the DB transaction.
            with contextlib.suppress(Exception):
                await s3_staging.abort_multipart_upload(file.id, ...)
            raise
        tally["staged"] += 1
```

A cleaner fix is to expose a public no-commit `stage_file_to_s3_nocmt` API from `cloud_staging` and accept `agent` as a parameter (pre-resolved by GATE 2), eliminating the redundant `select_active_agent` and giving the loop full control over cleanup.

---

_Reviewed: 2026-06-28_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
