---
phase: 69-tiered-drain-scheduler
reviewed: 2026-07-04T14:43:23Z
depth: standard
files_reviewed: 20
files_reviewed_list:
  - src/phaze/config.py
  - src/phaze/routers/agent_push.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/services/backend_selection.py
  - src/phaze/services/backends.py
  - src/phaze/services/pipeline.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/reconcile_cloud_jobs.py
  - src/phaze/tasks/reenqueue.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - tests/agents/routers/test_agent_push.py
  - tests/agents/routers/test_agent_s3.py
  - tests/analyze/core/test_dispatch_snapshot.py
  - tests/analyze/core/test_staging_cron.py
  - tests/analyze/services/test_backend_selection.py
  - tests/analyze/services/test_backends.py
  - tests/analyze/tasks/test_reconcile_cloud_jobs.py
  - tests/analyze/tasks/test_recovery.py
  - tests/shared/config/test_cloud_spill_to_local.py
findings:
  critical: 1
  warning: 3
  info: 1
  total: 5
status: issues_found
---

# Phase 69: Code Review Report

**Reviewed:** 2026-07-04T14:43:23Z
**Depth:** standard
**Files Reviewed:** 20
**Status:** issues_found

## Summary

Phase 69 turns the single-backend cloud drain into a tiered multi-backend scheduler:
a pure `select_backend` policy, a once-per-tick per-backend snapshot in
`stage_cloud_window`, per-backend `in_flight_count` caps, per-row advisory locking in
`KueueBackend.reconcile`, and at-cap spill-back to `AWAITING_CLOUD`.

The cloud-dispatch paths (compute/kueue) are careful and well-tested: the advisory-lock
serialization is cap-safe, the snapshot/decrement loop spills rank-first correctly, the
tz-awareness matching for the staleness subtraction is handled, `_utilization` guards
`cap == 0`, and the config knob is bounded. The reconcile/callback single-owner split for
compute vs kueue is deliberate.

The problem is the **local** dispatch path — the one genuinely-new spill target this phase
adds. Unlike the compute/kueue backends, `LocalBackend.dispatch` never transitions the file
out of `AWAITING_CLOUD`, so a locally-spilled file remains an eligible drain candidate on
every subsequent tick. That single omission produces a cross-backend double-dispatch
(BLOCKER, CR-01) and a mis-reported staged/skipped tally (WR-01). Two robustness gaps round
out the findings: no invariant guarantees a local safety-net backend exists (WR-02), and the
new recovery exclusion assumes a reconcile owner that compute does not have (WR-03).

## Critical Issues

### CR-01: A locally-spilled file stays AWAITING_CLOUD and can be re-dispatched to a cloud backend mid-analysis (double-dispatch)

**File:** `src/phaze/services/backends.py:193-208` (`LocalBackend.dispatch`); interacts with `src/phaze/services/pipeline.py:1244-1260` (`get_cloud_staging_candidates`) and `src/phaze/services/backend_selection.py:80-119`

**Issue:**
`ComputeAgentBackend.dispatch` and `KueueBackend.dispatch` both flip `file.state =
FileState.PUSHING` (backends.py:252, :316), which removes the file from the
`AWAITING_CLOUD` candidate set (`get_cloud_staging_candidates` selects only
`state == FileState.AWAITING_CLOUD`). `LocalBackend.dispatch` does **not** flip state — it
only enqueues `process_file` and returns. `enqueue_process_file` does not touch state
either. So a file spilled to local stays `AWAITING_CLOUD` until its `process_file`
eventually completes (the `put_analysis` callback flips it to `ANALYZED`,
agent_analysis.py:222).

Because `select_backend` is intentionally stateless (D-06) and the file remains a
candidate, the drain re-evaluates it on every `*/5` tick. A file spills to local only when
cloud is offline (D-03) or online-but-full-past-threshold (D-01). In **both** those cases,
if a cloud slot becomes available before the (long-running, which is why it was
cloud-routed) local `process_file` finishes, the next tick's `select_backend` routes the
still-`AWAITING_CLOUD` file to the lower-rank cloud backend:

1. Tick N: cloud offline → `LocalBackend.dispatch` enqueues `process_file:<id>` on the
   fileserver queue; file stays `AWAITING_CLOUD`; local analysis begins (long file).
2. Tick N+1: a compute agent comes online with a free slot → `select_backend` picks compute
   → `ComputeAgentBackend.dispatch` flips the file to `PUSHING`, writes a `SUBMITTED`
   `cloud_job`, and enqueues `push_file:<id>`.

The file is now analyzed **twice**: the local `process_file` is still in flight, and the
file is pushed to cloud scratch and analyzed again there. SAQ dedup does not save this —
`process_file:<id>` (fileserver queue) and `push_file:<id>` / compute-queue
`process_file:<id>` are different keys/queues, so nothing collapses them.

Downstream, this also re-opens the exact leaked-slot class Phase 68's CR-01 fixed: when the
local `process_file` finishes first and flips the file to `ANALYZED`, the later `/pushed`
callback is rowcount-guarded on `state == PUSHING` (agent_push.py:109) → it no-ops and never
terminalizes the compute `cloud_job`, which stays `SUBMITTED` (in-flight) forever because
`ComputeAgentBackend.reconcile` is a no-op (backends.py:275-277). That permanently consumes
a compute cap slot.

Note this path has no direct test — `test_staging_cron.py` exercises the multi-backend
tick with compute-only registries; local dispatch through the drain is untested.

**Fix:** `LocalBackend.dispatch` must remove the file from the candidate set atomically in
the caller's session, mirroring the cloud backends. Since local writes no `cloud_job`, use a
distinct terminal-for-the-drain FileState (e.g. flip out of `AWAITING_CLOUD` into the
in-analysis lane) so the file is no longer a `get_cloud_staging_candidates` match while its
local `process_file` is in flight:

```python
async def dispatch(self, file, session, task_router):
    cfg = cast("ControlSettings", get_settings())
    try:
        agent = await select_active_agent(session, kind="fileserver")
    except NoActiveAgentError:
        logger.info("LocalBackend.dispatch hold: no fileserver agent online", file_id=str(file.id))
        return False
    # Leave the AWAITING_CLOUD candidate set in the SAME session so a later tick can never
    # re-route this file to a cloud backend while its local process_file is in flight.
    file.state = FileState.DISCOVERED  # or a dedicated in-analysis state the drain excludes
    queue = task_router.queue_for(agent.id)
    job = await enqueue_process_file(queue, file, agent.id, cfg.models_path)
    return job is not None
```

(Confirm the chosen state is one `put_analysis`'s unconditional `-> ANALYZED` flip and the
recovery/domain-completed predicates handle; add a drain test that spills to local, brings
cloud online next tick, and asserts the file is NOT pushed.)

## Warnings

### WR-01: LocalBackend.dispatch returns True on a dedup no-op, breaking the staged/skipped tally contract

**File:** `src/phaze/services/backends.py:193-208`

**Issue:** The `Backend.dispatch` protocol contract (backends.py:129-135) states the method
"Returns `True` when new dispatch work was actually enqueued ... and `False` when the
enqueue was a deterministic-key dedup no-op." `ComputeAgentBackend.dispatch` honors it
(`return job is not None`, backends.py:270-273). `LocalBackend.dispatch` ignores the return
value of `enqueue_process_file` (which is `None` on a `process_file:<id>` dedup,
analysis_enqueue.py:66-67, :93) and unconditionally `return True`. Combined with CR-01's
repeated re-selection, every tick counts an already-in-flight local file as a fresh
`staged`, inflating the tally and masking the churn. Even after CR-01 is fixed, the contract
violation stands.

**Fix:** Capture and inspect the return value:
```python
job = await enqueue_process_file(queue, file, agent.id, cfg.models_path)
return job is not None
```

### WR-02: No invariant guarantees a local safety-net backend exists — cloud-exhausted files can strand permanently

**File:** `src/phaze/config.py:417-451` (`_validate_registry`); `src/phaze/services/backend_selection.py:97-115`

**Issue:** `select_backend` treats local as "the guaranteed safety net" (D-04): when a
file's `cloud_attempts >= cloud_submit_max_attempts` it filters `eligible` down to
`LocalBackend` instances only (backend_selection.py:98-99). If the operator's
`backends.toml` declares only cloud backends (no `kind = "local"` entry), that filter yields
an empty set → `select_backend` returns `None` forever → the file is held in
`AWAITING_CLOUD` permanently. `_validate_registry` enforces bucket/scope invariants but does
**not** require at least one local backend. `test_staging_cron.py::test_held_awaiting_untouched_keeps_updated_at`
even encodes this permanent hold as expected behavior (a cloud-only registry, attempts==max,
`{"staged": 0, "skipped": 1}` every tick). The implicit-local `default_factory` only fires
when the `backends` key is entirely absent, so a present cloud-only registry has no local
fallback.

**Fix:** Add a whole-registry invariant to `_validate_registry` requiring at least one
`kind == "local"` backend whenever `cloud_enabled` is true (fail fast at boot), or have the
drain surface a loud operator alert when it holds a cloud-exhausted file with no local
target. Failing fast at construction is preferable — the current behavior silently wedges
long files.

### WR-03: Recovery now excludes every in-flight-cloud_job file, but compute has no reconcile owner — a stuck compute PUSHING file can strand

**File:** `src/phaze/tasks/reenqueue.py:204-219` (`_in_flight_cloud_job_ids`), `:343`; `src/phaze/services/backends.py:275-277` (`ComputeAgentBackend.reconcile`)

**Issue:** Phase 69 (SCHED-05) adds `_in_flight_cloud_job_ids` and subtracts it from the
ledger orphan set (`_natural_id(r) not in in_flight`, reenqueue.py:343) so "the backend
reconcile/`/pushed` callback is the SINGLE owner" of cloud-backed files. That assumption
holds for kueue (`KueueBackend.reconcile` is a real cron read) but is **false for compute**:
`ComputeAgentBackend.reconcile` is a no-op — compute's only terminalizer is the `/pushed` /
`/mismatch` callback, which fires only if the `push_file` job actually runs and reports back.

On a genuine queue-loss (saq_jobs truncate / restore-from-backup / fresh migration), or when
an operator clicks "Recover", a compute file stuck in `PUSHING` with a `SUBMITTED` `cloud_job`
whose `push_file` job was lost (callback never fired) is now excluded from the ledger orphan
set (its `cloud_job` is in-flight) AND has no reconcile owner. Pre-Phase-69, recovery would
have replayed its `push_file` ledger row. It is now stranded in `PUSHING` indefinitely
(the staging cron only touches `AWAITING_CLOUD`). This is the mirror of CR-01's leaked-slot:
a single-owner exclusion that yields a **no-owner** state for compute.

**Fix:** Scope the exclusion to backends that actually reconcile their in-flight rows, or
give `ComputeAgentBackend.reconcile` a real body that re-drives / spills a stuck `SUBMITTED`
compute `cloud_job` (mirroring the kueue re-drive), so every in-flight cloud file has exactly
one owner rather than zero. At minimum, do not exclude compute-`backend_id` in-flight rows
from ledger recovery while compute reconcile remains a no-op.

## Info

### IN-01: Dead transitional accessors and a self-referential default branch add reader friction

**File:** `src/phaze/config.py:981-996` (`_build_default_settings`), `:463-518` (transitional accessors)

**Issue:** `_build_default_settings` has an `if role == Role.AGENT.value: return ControlSettings()`
branch whose body is identical to the fallthrough `return ControlSettings()` — the branch is
a documented no-op kept for narrative. Several `_single_non_local` / `active_*` accessors are
marked "TRANSITIONAL — retained through Phase 70" and raise on `>1` non-local even though
Phase 69's `resolve_backends` now supports N backends; they are a latent trap for a future
caller. These are not defects (correct today), but they raise the cognitive cost of reasoning
about the ≤1-non-local vs N-non-local split. Consider collapsing the dead branch and adding a
`# noqa`-style pointer that these accessors are Phase-70-scheduled for removal.

**Fix:** Collapse the identical `_build_default_settings` branch; leave a single TODO(Phase 70)
tracking accessor removal.

---

_Reviewed: 2026-07-04T14:43:23Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
