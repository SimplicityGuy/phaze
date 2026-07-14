---
phase: 83-cloud-routing-sidecar-cutover
reviewed: 2026-07-09T00:00:00Z
depth: standard
files_reviewed: 22
files_reviewed_list:
  - alembic/versions/034_backfill_cloud_awaiting.py
  - src/phaze/routers/agent_analysis.py
  - src/phaze/routers/agent_push.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/routers/pipeline.py
  - src/phaze/services/backend_selection.py
  - src/phaze/services/backends.py
  - src/phaze/services/pipeline.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - tests/agents/routers/test_agent_analysis.py
  - tests/agents/routers/test_agent_push.py
  - tests/agents/routers/test_agent_s3.py
  - tests/analyze/core/test_dispatch_snapshot.py
  - tests/analyze/core/test_staging_cron.py
  - tests/analyze/services/test_backend_selection.py
  - tests/analyze/services/test_backends.py
  - tests/analyze/tasks/test_release_awaiting_cloud.py
  - tests/integration/test_drain_double_dispatch.py
  - tests/integration/test_migrations/test_migration_034_backfill_cloud_awaiting.py
  - tests/integration/test_shadow_compare.py
  - tests/shared/routers/test_pipeline.py
  - tests/shared/services/test_pipeline.py
findings:
  critical: 0
  warning: 4
  info: 2
  total: 6
status: issues_found
---

# Phase 83: Code Review Report

**Reviewed:** 2026-07-09
**Depth:** standard
**Files Reviewed:** 22
**Status:** issues_found

## Summary

Phase 83 moves cloud routing off `FileRecord.state` and onto the `cloud_job` sidecar. I traced
the drain path (`stage_cloud_window` → `get_cloud_staging_candidates` → `select_backend` →
`Backend.dispatch`), the CAS-guarded callback routers (`agent_push`, `agent_s3`,
`agent_analysis`), the shared awaiting writer (`hold_awaiting_cloud`), and the repair migration
(`034`).

The **load-bearing invariants hold and are well-tested**:

- **SC#3 double-dispatch gate** (`test_drain_double_dispatch.py`) genuinely exercises all three
  exclusion classes (committed-ledger, rolled-back-tick-with-committed-ledger, terminally-failed),
  including the case row-deletion would have broken. The `~inflight_clause`/`~domain_completed_clause`
  conjuncts (D-05), the FOR-UPDATE-SKIP-LOCKED-of-CloudJob candidacy lock (D-06), and the
  single-post-loop-commit-under-advisory-lock (SCHED-02) are correct and mutually consistent.
- **Cap safety**: cloud dispatch re-stamps the awaiting row to `submitted` (dropping it from the
  `status='awaiting'` candidate filter), and `in_flight_count` only counts non-terminal rows —
  concurrent spills/reconciles only ever *decrement* in-flight, which is cap-safe.
- **Timezone reconciliation** (`stage_cloud_window` L213-215) correctly matches `now`'s awareness
  to `lane_entered_at`'s so `select_backend`'s staleness subtraction never raises.
- **Migration 034** is static, parameter-free, idempotent (`ON CONFLICT (file_id) DO NOTHING`),
  READ-ONLY on `files.state`, and its test corpus proves the DO-NOTHING no-clobber case.

No BLOCKER-class defects (no stranding, no double-dispatch, no data-loss path) were found. The
findings below are latent-drift, an authz-asymmetry that is not reachable in the default config,
and a display-lane mislabel introduced as a side effect of the new awaiting-row writer.

## Warnings

### WR-01: New awaiting-row writer breaks `get_analyze_stage_files` lane derivation

**File:** `src/phaze/services/pipeline.py:787-791, 828-834`
**Issue:** Before Phase 83, an `AWAITING_CLOUD` / locally-spilled file carried **no** `cloud_job`
row, so the lane derivation (`cloud_job_id IS NULL → "local"`, `cloud_phase IS NULL → "a1"`) was
safe. Phase 83's `hold_awaiting_cloud` now writes a `cloud_job(status='awaiting', cloud_phase=NULL)`
row for **every** held long file, and `LocalBackend.dispatch` keeps that row after a local spill
(D-13 keeps it; D-14 reaps it only at the analyze-terminal seam). As a result:
- Every `AWAITING_CLOUD` held file is now labeled lane `"a1"` (compute) instead of `"local"`.
- A locally-spilled file that has acquired a partial (57.1) analysis row appears at
  `state=LOCAL_ANALYZING` with a lingering `cloud_phase=NULL` awaiting row → mislabeled `"a1"`.

The function docstring explicitly asserts the now-false invariant: *"A local-routed file never
enters stage_file_to_s3, so it never carries a cloud_job row and cannot be mislabeled."* This is a
dashboard display regression, not a routing defect.
**Fix:** Derive `"local"` when the row's `status='awaiting'` (or `backend_id IS NULL`), reserving
`"a1"`/`"k8s"` for genuinely-dispatched rows, e.g. add `CloudJob.backend_id` / `CloudJob.status`
to the select and branch:
```python
if cloud_job_id is None or cloud_status == CloudJobStatus.AWAITING.value:
    lane = "local"  # held / spilled-local: not yet on any cloud backend
elif cloud_phase is None:
    lane = "a1"
else:
    lane = "k8s"
```
and update the stale docstring invariant.

### WR-02: `report_push_mismatch` skips the D-07 gate and leaves the over-cap CAS backend-kind-unguarded when `backend is None`

**File:** `src/phaze/routers/agent_push.py:225, 257-280`
**Issue:** The D-07 reporter-authorization gate is `if backend is not None and agent.id != backend.agent_ref`
— it is **skipped entirely** when `backend is None`. Unlike `report_pushed`, which returns a clean
200 hold *before* its CAS when `backend is None` (L107-109), `report_push_mismatch` falls through to
the over-cap spill CAS `UPDATE cloud_job WHERE status='submitted' → 'awaiting'` with no
backend-kind guard. Because kueue also transits `submitted` (per the comment at
`agent_push.py:123`), a `/mismatch` for a kueue file's id could clobber a live kueue `submitted`
row to `awaiting`, spilling it to local. This is **not reachable in the default config** (a kueue
file accrues its attempts on the `s3_upload:<id>` ledger, not `push_file:<id>`, so `current_attempt`
stays 0 and the over-cap branch is never entered unless `push_max_attempts < 1`), but it is a latent
asymmetry with `report_pushed`'s guarding and relies on config invariants rather than code.
**Fix:** Mirror `report_pushed`: return the clean hold before the over-cap CAS when `backend is None`,
so an unattributed/non-compute file is never spilled by a `/mismatch`:
```python
if backend is None:
    logger.warning("report_push_mismatch held: no attributed compute backend", file_id=str(file_id))
    await session.commit()
    return PushMismatchResponse(file_id=file_id, cleared=False)
# ... then read ledger / compute next_attempt / over-cap CAS
```

### WR-03: The D-01/D-02 "single shared go-forward writer" invariant is not realized in code

**File:** `src/phaze/services/backends.py:84-118` (docstring) vs `src/phaze/routers/agent_s3.py:206-215`,
`src/phaze/routers/agent_push.py:273-280`
**Issue:** `hold_awaiting_cloud`'s docstring states it is *"the single go-forward writer of
`cloud_job.status='awaiting'` … shared by the hold path (`trigger_analysis`) and both over-cap spill
paths (`report_upload_failed` / `report_push_mismatch`) … instead of three hand-copied writers."*
In reality `hold_awaiting_cloud` is called from exactly one site (`pipeline.py:351`); both spill
paths re-implement the awaiting re-stamp inline as direct CAS `UPDATE`s. So the "one writer"
guarantee the docstring leans on for the hard shadow invariant is actually **three independent
writers**. All three currently produce `status='awaiting'` correctly, so this is not a live bug —
but the invariant that is supposed to prevent drift does not exist, and a future edit to one
writer (e.g. also stamping `cloud_phase=NULL` or clearing `backend_id`) will silently diverge.
**Fix:** Either (a) route the spill CAS through `hold_awaiting_cloud` (it accepts `attempts=`, which
is exactly what the spills pass), or (b) correct the docstring to state that the spill paths carry
their own CAS-guarded re-stamp and enumerate the shared fields that must stay in lockstep.

### WR-04: `stage_cloud_window` candidate-fetch and GATE-2 sit outside the CR-02 safety net

**File:** `src/phaze/tasks/release_awaiting_cloud.py:173, 181, 203`
**Issue:** The module docstring and the CR-02 comment (L194-202) promise the cron *"NEVER raises
(T-50-cron-raise discipline)."* The outer safety-net `try` begins at L203, but
`get_cloud_staging_candidates(session, limit)` (L173) — the newly-rewritten Phase-83 join + FOR
UPDATE query — and the GATE-2 `select_active_agent` (L181) run **before** it. A DB error surfaced
by the candidate SELECT (or any non-`NoActiveAgentError` from GATE-2) propagates straight out of
`stage_cloud_window`, so the "never raises" contract is not fully upheld for the pre-loop DB work.
Impact is low (SAQ logs the failed tick, files stay `AWAITING_CLOUD`, next `*/5` tick retries — no
stranding), but it contradicts the stated discipline and the query is exactly the code this phase
changed.
**Fix:** Widen the guarded region so the candidate SELECT is covered, e.g. move the `try:` up to
enclose `get_cloud_staging_candidates` (and GATE-2), degrading to
`{"staged": 0, "skipped": <len or 0>}` on an unexpected raise, keeping the single rollback semantics.

## Info

### IN-01: Duplicated comment block in `KueueBackend.reconcile`

**File:** `src/phaze/services/backends.py:530-533`
**Issue:** The two-line comment *"MKUE-01/D-04: thread THIS backend's KubeConfig so every
get_job/get_workload_for/ delete_job inside reconcile targets the file's own cluster."* is pasted
twice back-to-back.
**Fix:** Delete the duplicate copy (L532-533).

### IN-02: Stale docstring in `LocalBackend.dispatch` contradicts actual wiring

**File:** `src/phaze/services/backends.py:230-236`
**Issue:** The class docstring says `dispatch` is *"unit-tested here, NOT wired into the single-path
drain"*, but Phase 69's `select_backend` can return a `LocalBackend` and `stage_cloud_window` calls
`target.dispatch()` on it (the local-spill path, exercised by
`test_drain_double_dispatch.py` case (a)/(b)). The comment predates the wiring and is now
misleading.
**Fix:** Update the docstring to reflect that `LocalBackend.dispatch` is the drain's local-spill
target (Phase 69 SCHED-01/D-03), consistent with `ComputeAgentBackend`/`KueueBackend`.

---

_Reviewed: 2026-07-09_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
