---
phase: 68-backend-protocol-3-implementations
reviewed: 2026-07-04T04:24:27Z
depth: standard
files_reviewed: 12
files_reviewed_list:
  - src/phaze/services/backends.py
  - src/phaze/config.py
  - src/phaze/models/cloud_job.py
  - src/phaze/routers/agent_push.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/routers/pipeline.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - src/phaze/tasks/reconcile_cloud_jobs.py
  - src/phaze/config_backends.py
  - alembic/versions/029_add_cloud_job_backend_id.py
  - tests/integration/test_migrations/test_migration_029_backend_id.py
findings:
  critical: 1
  warning: 2
  info: 3
  total: 6
status: resolved
resolution:
  fixed_at: 2026-07-04
  fixed: [CR-01, WR-01, WR-02]
  deferred: [IN-01, IN-02, IN-03]  # info-only, intentionally out of phase scope
  commits:
    CR-01: af7756e
    WR-02: 0ccf6a3
    WR-01: 1671732
---

# Phase 68: Code Review Report

**Reviewed:** 2026-07-04T04:24:27Z
**Depth:** standard
**Files Reviewed:** 12 (+ cross-referenced test files: `tests/analyze/core/test_dispatch_snapshot.py`,
`tests/analyze/services/test_backends.py`, `tests/shared/config/test_bucket_registry.py`,
`tests/analyze/core/test_push_pipeline.py`)
**Status:** issues_found

## Summary

Phase 68's re-home is largely faithful to the stated behavior-preserving intent: the protocol shape,
the D-01a GATE-1 asymmetry, the D-03 in-txn write ordering for the compute `cloud_job` row, and the
migration 029 nullability changes all check out against the pre-refactor code (`git show a818d70` vs
`HEAD`) and their own golden-snapshot test. However, the D-08 "compute terminalization keeps the D-02
equivalence invariant true LIVE" claim is **not actually true** for the push-mismatch permanent-failure
path — a genuine, confirmed correctness gap (see CR-01). Two further issues degrade the phase's own
stated invariants: a silent multi-backend misconfiguration in three rewired dashboard/callback readers
(WR-01), and an unguarded per-file agent re-lookup inside `ComputeAgentBackend.dispatch` that can raise
out of the "never raises" cron (WR-02). None of the findings are covered by the existing test suite —
each was verified by reading the call graph and the pre-refactor git history, not by a failing test.

## Critical Issues

### CR-01: Compute `cloud_job` row is never terminalized on the push-mismatch permanent-failure path — breaks the D-02 invariant live

**RESOLVED (af7756e):** `report_push_mismatch`'s cap-reached branch now terminalizes the compute
`cloud_job` row to `FAILED` in the same transaction as the `ANALYSIS_FAILED` flip (mirroring
`report_pushed`'s SUCCEEDED write). No-op for non-compute files. Regression tests added for both the
compute (SUBMITTED → FAILED) and non-compute (no row) cases in `test_agent_push.py`.


**File:** `src/phaze/routers/agent_push.py:151-197` (`report_push_mismatch`, cap-reached branch)
**Issue:**

`ComputeAgentBackend.dispatch` (`src/phaze/services/backends.py:233-269`) writes a `cloud_job` row with
`status=SUBMITTED` for every compute-staged file (D-03/D-08). The **only** place that terminalizes this
row is `report_pushed`'s success path (`agent_push.py:121-127`, added by 68-04):

```python
await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUCCEEDED.value))
```

`report_push_mismatch`'s permanent-failure branch (cap reached, `agent_push.py:184-197`) flips the
`FileRecord` to `ANALYSIS_FAILED` and clears the scheduling ledger, but **never touches `cloud_job` at
all** — confirmed by `grep -rln "CloudJobStatus.FAILED" src/phaze/` (agent_push.py does not appear in
that list; only `reconcile_cloud_jobs.py`, `agent_s3.py`, and `submit_cloud_job.py` terminalize their
respective FAILED paths).

Consequence: once a compute push permanently fails (sha256 mismatch cap reached), the `FileRecord` exits
the `{PUSHING, PUSHED}` window that `get_cloud_window_count()` reads, but its `cloud_job` row is
permanently stranded at `SUBMITTED` — exactly the "dispatch-partial limbo" the phase's own D-03/Pitfall-4
write-ordering rule was designed to prevent, except on the *termination* side rather than the *dispatch*
side. `in_flight_count(compute)` over-counts relative to `get_cloud_window_count()` from that point on,
for as long as the process runs. The 68-CONTEXT/68-04-SUMMARY claim that "the D-02 equivalence invariant
holds live" is therefore false for this path. This is currently harmless for Phase 68 (D-02a: nothing
consults `in_flight_count` for cap yet), but it directly undermines the exact substrate Phase 69
(SCHED-02) is supposed to flip the drain onto, and it will silently shrink effective compute capacity
once that flip lands.

Verified untested: `test_push_pipeline.py` and `test_agent_s3.py`/agent_push tests never reference
`cloud_job`/`CloudJob` around the mismatch-cap path, and the D-01 golden snapshot
(`test_dispatch_snapshot.py`) only exercises `stage_cloud_window`, never the `/pushed` or `/mismatch`
callbacks.

**Fix:** Terminalize the compute `cloud_job` row to `FAILED` in the same transaction as the
`ANALYSIS_FAILED` flip in `report_push_mismatch`'s cap-reached branch:

```python
if next_attempt > settings.push_max_attempts:
    await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYSIS_FAILED))
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.FAILED.value))
    await clear_ledger_entry(session, ledger_key)
    await session.commit()
```
(Import `CloudJob`/`CloudJobStatus`, already imported for the `report_pushed` handler in the same
module.) Add a regression test asserting `in_flight_count(compute) == 0` after a mismatch-cap failure.

## Warnings

### WR-01: `resolved_non_local_kind()` silently picks a backend instead of raising on a >1-non-local registry, unlike the accessor it replaced

**RESOLVED (1671732):** `resolved_non_local_kind()` now raises `ValueError` naming the offending backend
ids when `len(non_local) > 1`, mirroring `resolve_backends()`'s boot guard. All-local and
single-non-local paths remain byte-identical. Regression test added in `test_backends.py`.


**File:** `src/phaze/services/backends.py:387-397`
**Issue:**

The retired `active_cloud_kind` property called `_single_non_local()` internally, which **raises**
`ValueError` when the registry resolves to more than one non-local backend (`config.py:463-476`,
explicitly documented and still covered by
`tests/shared/config/test_bucket_registry.py::test_multiple_non_local_backends_accessor_raises`). Its
Phase-68 replacement does not preserve that guard:

```python
def resolved_non_local_kind(settings: ControlSettings) -> str:
    if not settings.cloud_enabled:
        return "local"
    non_local = [backend for backend in settings.backends if backend.kind != "local"]
    return non_local[0].kind
```

This silently returns the *first* non-local backend's kind rather than raising when `len(non_local) > 1`
— exactly the "silently pick one" failure mode `resolve_backends()`'s own docstring says must never
happen ("fail fast here naming the offending ids rather than silently picking one"). `resolve_backends()`
itself still raises, but it is invoked only at controller boot (`tasks/controller.py:179`, wrapped in a
`try/except` that swallows the raise and just skips the Kueue probe) and inside the drain
(`release_awaiting_cloud.py:104`, uncaught — but this mirrors pre-refactor behavior, not a new
regression). `resolved_non_local_kind()` is used **without** any `resolve_backends()` guard at three
production call sites that were explicitly named in the CONTEXT/RESEARCH as "must be rewired" (Q1/D-09):
- `routers/pipeline.py:576` (`cloud_lane_kind`, dashboard display)
- `routers/pipeline.py:810` (`trigger_backfill_cloud` ledger-seed fork)
- `routers/agent_s3.py:114` (`report_uploaded`'s kueue-vs-compute guard)

None of these three call sites, nor `resolved_non_local_kind()` itself, has any test coverage of the
`>1`-non-local case (confirmed via `grep -rn resolved_non_local_kind tests/` — the only hit is a
docstring reference in `test_controller_startup_localqueue.py`, which exercises the controller path, not
these three).

**Fix:** Either (a) have `resolved_non_local_kind()` raise the same `ValueError` on `len(non_local) > 1`
(mirroring `_single_non_local()`), or (b) route it through `resolve_backends()` and derive the kind from
the resolved list so the same fail-fast guard applies everywhere the retired accessor used to apply.

### WR-02: `ComputeAgentBackend.dispatch` re-resolves the fileserver agent per file with no exception guard, risking an uncaught raise out of the "never raises" cron

**RESOLVED (0ccf6a3):** the drain's per-file `backend.dispatch(...)` call in `stage_cloud_window` is now
wrapped so a mid-tick `NoActiveAgentError` degrades to a clean hold of the remaining candidates (counted
skipped, left `AWAITING_CLOUD`), preserving the cron no-op discipline. `dispatch` gates the fileserver
before any mutation, so the raising file is untouched. Regression test added in `test_staging_cron.py`.


**File:** `src/phaze/services/backends.py:233-245`
**Issue:**

Pre-refactor, `stage_cloud_window` resolved the fileserver agent **once** at GATE 2
(`release_awaiting_cloud.py:140-144`, in a `try/except NoActiveAgentError`) and reused that single
`fileserver_agent` object for every candidate in the per-file loop. Phase 68's `ComputeAgentBackend.
dispatch` instead re-issues `select_active_agent(session, kind="fileserver")` **per file**, with no
`try/except` around it:

```python
async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: AgentTaskRouter) -> bool:
    # Gate on the fileserver agent (the push initiator) BEFORE mutating: absent -> clean hold, nothing written.
    fileserver_agent = await select_active_agent(session, kind="fileserver")
    ...
```

`select_active_agent` raises `NoActiveAgentError` (uncaught here) when no fileserver agent satisfies the
filter (`services/enqueue_router.py:96-128` — no time-windowed heartbeat check, just "most recent,
non-revoked"). Under Postgres's default READ COMMITTED isolation, a fileserver agent revocation
committed by a concurrent session between the drain's GATE-2 check and a later iteration of the
candidate loop (a real possibility when `slots` spans many candidates) would raise `NoActiveAgentError`
straight out of `backend.dispatch()`, which is called with no `try/except` in `stage_cloud_window`'s loop
(`release_awaiting_cloud.py:154-158`). This violates the cron's own documented invariant ("TWO gates,
both a clean no-op ... NOT a raise", restated as the T-50-cron-raise / T-68-05 discipline throughout the
module and phase docstrings) and is not exercised by the D-01 golden snapshot (which never simulates a
mid-tick agent revocation). Practically this degrades to a failed cron tick (the whole transaction rolls
back on session close, so no partial state is committed) rather than data loss, but it is a real,
previously-impossible failure mode introduced by this refactor.

**Fix:** Thread the already-resolved `fileserver_agent` (or its id) from the drain into
`Backend.dispatch(...)` as a parameter instead of re-querying it inside `ComputeAgentBackend.dispatch`,
or at minimum wrap the internal lookup in `try/except NoActiveAgentError: return False` to preserve the
no-raise contract.

## Info

### IN-01: `ComputeAgentBackend.dispatch`'s `cloud_job` upsert `set_` clause never clears `s3_key`/kueue-only fields on conflict

**File:** `src/phaze/services/backends.py:249-262`
**Issue:** The `on_conflict_do_update` only updates `backend_id` and `status`:
```python
set_={"backend_id": stmt.excluded.backend_id, "status": stmt.excluded.status},
```
If a file's existing `cloud_job` row was previously written by a *different* backend kind (e.g. a stale
kueue row with a real `s3_key`/`kueue_workload` from an earlier attempt, later re-dispatched via
compute after a config change), the conflict path leaves those kueue-only fields stale rather than
resetting them. Low risk under Phase 68's single-non-local invariant (a file rarely re-dispatches across
kind changes within one run), but worth tracking before Phase 69 introduces real multi-backend
spillover, where this becomes more plausible.
**Fix:** Include `s3_key`, `kueue_workload`, `attempts`, `inadmissible`, `cloud_phase` (reset to `None`/
defaults) in the `set_` clause, or note explicitly why they are intentionally left untouched.

### IN-02: `resolve_backends()` silently drops any registry entry whose `kind` isn't one of the three known literals

**File:** `src/phaze/services/backends.py:369-377`
**Issue:** The `if/elif` chain over `entry.kind` has no `else` branch:
```python
for entry in settings.backends:
    if entry.kind == "local": ...
    elif entry.kind == "compute": ...
    elif entry.kind == "kueue": ...
```
Currently unreachable in practice (the `BackendConfig` discriminated union in `config_backends.py`
only accepts `Literal["local"|"compute"|"kueue"]`, so pydantic would already reject an unknown kind at
config-load time), so this is not exploitable today. But it is inconsistent with this module's own
fail-fast philosophy (`resolve_backends` explicitly raises rather than silently dropping the >1-non-local
case a few lines below) and would silently vanish an entry if a future `kind` variant is added to
`config_backends.py` without a matching branch here.
**Fix:** Add an `else: raise ValueError(f"unrecognized backend kind {entry.kind!r} for entry {entry.id!r}")`.

### IN-03: `KueueBackend.reconcile` is fully implemented but unreachable in production this phase

**File:** `src/phaze/services/backends.py:316-358`, `src/phaze/tasks/controller.py:288,319`
**Issue:** The live `*/5` reconcile cron registered on the controller is still the original
module-level `reconcile_cloud_jobs` function (`tasks/reconcile_cloud_jobs.py`), which iterates **all**
`SUBMITTED`/`RUNNING` rows regardless of `backend_id` — not `KueueBackend.reconcile`, which is scoped by
`backend_id == self.id`. This matches the phase's explicit D-02a/D-07 scope ("lay and prove, don't
flip" — the backend_id-scoped reconcile is proven by unit test only), so it is not a defect, but it is
genuinely dead code from a production-reachability standpoint and should be tracked so it isn't
mistaken for the live reconcile path when Phase 69 wires it in.
**Fix:** None required this phase; flag for Phase 69 (SCHED) to confirm `controller.py`'s cron
registration is switched to the backend-scoped `reconcile` when the cap flip lands.

---

_Reviewed: 2026-07-04T04:24:27Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
