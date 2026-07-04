---
phase: 69-tiered-drain-scheduler
plan: 02
subsystem: scheduler
tags: [tiered-drain, multi-backend, per-backend-cap, advisory-lock, select-backend, D-05]
requires:
  - "src/phaze/services/backend_selection.py select_backend + BackendSlot (Plan 01)"
  - "src/phaze/services/backends.py Backend protocol + in_flight_count substrate (Phase 68)"
  - "src/phaze/config.py cloud_spill_to_local_after_seconds + cloud_submit_max_attempts"
provides:
  - "src/phaze/tasks/release_awaiting_cloud.stage_cloud_window tiered N-backend drain (snapshot + per-candidate select_backend)"
  - "src/phaze/services/backends.resolve_backends without the >1-non-local boot guard (N non-local backends)"
  - "src/phaze/services/backends.KueueBackend.dispatch now stamps cloud_job.backend_id"
affects:
  - "Later 69 waves (reconcile spill-back, recover_orphaned_work single-owner exclusion) build on this per-backend in-flight substrate"
tech-stack:
  added: []
  patterns:
    - "once-per-tick snapshot (M probes) + local remaining[] decrement inside a single advisory-locked txn with one post-loop commit"
    - "per-candidate pure select_backend routing (rank-first + spill) over the snapshot"
    - "per-candidate now-awareness matching for the staleness subtraction without mutating the parked row"
key-files:
  created: []
  modified:
    - "src/phaze/tasks/release_awaiting_cloud.py"
    - "src/phaze/services/backends.py"
    - "src/phaze/services/pipeline.py"
    - "src/phaze/tasks/controller.py"
    - "src/phaze/routers/agent_push.py"
    - "tests/analyze/core/test_staging_cron.py"
    - "tests/analyze/services/test_backends.py"
    - "tests/analyze/core/test_dispatch_snapshot.py"
decisions:
  - "SCHED-01: resolve_backends drops the >1-non-local guard; resolved_non_local_kind keeps its twin guard for the non-drain single-kind callers (WR-01)"
  - "SCHED-02 / D-05: per-backend cap enforced by once-per-tick in_flight_count snapshot + local remaining[] decrement under the single pg_advisory_xact_lock(5_000_504); global get_cloud_window_count retired"
  - "KueueBackend.dispatch stamps cloud_job.backend_id after the shared _stage_file_to_s3 core (which predates the registry) so in_flight_count(kueue) counts its rows (Rule 2 fix)"
  - "Candidate limit = sum(remaining over available slots); a select_backend None is a clean per-candidate hold counted as skipped"
metrics:
  duration: "~1h"
  completed: "2026-07-04"
  tasks: 3
  files_created: 0
  files_modified: 8
---

# Phase 69 Plan 02: Tiered Multi-Backend Drain Summary

The Phase-50 single-backend cloud window becomes the tiered scheduler: `stage_cloud_window` now snapshots every resolved backend's `is_available()` + `remaining = cap - in_flight_count()` ONCE per tick under the single advisory lock, routes each FIFO candidate to the pure `select_backend` policy's rank-first choice, and decrements the local `remaining[]` per claim — so more than one backend runs simultaneously (SCHED-01) and each backend's `cap` is enforced by count-and-claim (SCHED-02). The global `get_cloud_window_count` window is retired in favor of per-backend `in_flight_count` (D-05).

## What Was Built

### Task 1 — Remove the >1-non-local boot guard (SCHED-01) — commit `7ffef6a`
- Deleted the `if len(non_local) > 1: raise ValueError(...)` block from `resolve_backends`; it now resolves a registry with N non-local entries to a full `list[Backend]`. Updated its docstring + the module D-07 bullet.
- Kept `resolved_non_local_kind`'s twin single-kind fail-fast intact — its callers (`routers/pipeline`, `routers/agent_s3`, `controller.startup`) still assume a single non-local kind (WR-01 defense-in-depth). Grep confirmed those are the only remaining callers.
- `controller.startup` dropped its now-redundant `resolve_backends(control_cfg)` boot-guard call (and the import); the single-kind fail-fast the Kueue-probe relies on now comes solely from `resolved_non_local_kind` on the next line — boot behavior on a >1-non-local registry is preserved (still skips the probe, boots regardless).
- Added `test_resolve_backends_returns_all_non_local` (2 compute + 1 local → len 3, no ValueError).

### Task 2 — Rewire stage_cloud_window; retire get_cloud_window_count (SCHED-01/02, D-05) — commit `94788e6`
- `stage_cloud_window` rewritten in place preserving every load-bearing invariant: the `cloud_enabled` no-op gate, `pg_advisory_xact_lock(5_000_504)` acquired once at the top of the one transaction, the single post-loop `commit()`, the GATE-2 fileserver clean-hold, and the per-file `except NoActiveAgentError` hold.
  - **Snapshot** (Pitfall 1): each backend's `is_available()` + `in_flight_count()` are probed exactly M times, never inside the candidate loop; `remaining = max(0, cap - in_flight)`.
  - **Candidate limit** = `sum(remaining over available slots)` (non-local free capacity + local headroom); `<= 0` → clean no-op.
  - **Per-candidate routing** (SCHED-01): read the file's `cloud_job.attempts` (0 when no row) via `_cloud_attempts_for`, call `select_backend(file, cloud_attempts, snapshot, now, cfg)`; `None` → clean hold (skipped, no state change); else `dispatch()` then decrement `snapshot[id]["remaining"]` (a claimed slot on both a genuine stage and a dedup no-op — this drives the within-tick spill).
- Retired `pipeline.get_cloud_window_count` (D-05): deleted the function and purged EVERY textual reference in `src/phaze` (grep-clean) — the drain, the dashboard-counter docstrings (`get_pushing_count`/`get_pushed_count`), the `agent_push` terminalization comments, and the `backends.py` module/`_BaseBackend` docstrings. `get_cloud_staging_candidates` kept unchanged (only its `limit` argument changed).
- Updated the existing staging-cron tests to model in-flight via `cloud_job` rows (the D-05 count is `cloud_job`-derived, not FileState) via a new `_seed_in_flight` helper; extended `_StubCfg` with the two knobs `select_backend` reads; rewrote the D-02 equivalence test to an inline FileState count after the helper retirement.

### Task 3 — Extend the drain suite (SCHED-01/02, A3) — commit `9a344d2`
- `test_multi_backend_tick_dispatches_rank_first_and_spills`: 2 compute backends (rank10 cap1 + rank20 cap2); one tick sends the oldest to the top rank then spills the next two to the next rank once the top is full (asserted via per-file `cloud_job.backend_id`).
- `test_overlapping_ticks_never_overshoot_per_backend_cap`: two concurrent ticks serialize on the advisory lock so `compute-1`'s in-flight `cloud_job` count stays ≤ cap.
- `test_held_awaiting_untouched_keeps_updated_at`: an attempt-exhausted file with no local backend is held (`select_backend` → None); its `updated_at` and `cloud_job` stay byte-untouched — the staleness clock the spill gate reads is protected (RESEARCH A3).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 — Missing critical functionality] KueueBackend.dispatch now stamps cloud_job.backend_id**
- **Found during:** Task 2 (per-backend counting flip).
- **Issue:** The shared `_stage_file_to_s3` core that `KueueBackend.dispatch` calls predates the registry and upserts the `cloud_job` row WITHOUT `backend_id`. Under the new per-backend `in_flight_count` (COUNT WHERE `backend_id == self.id`), kueue rows never matched → kueue in-flight always read 0 → the kueue cap would be overshot (the existing `test_k8s_overlapping_ticks_never_exceed_window` would fail).
- **Fix:** After `_stage_file_to_s3`, `KueueBackend.dispatch` issues an `UPDATE cloud_job SET backend_id=self.id WHERE file_id=...` in the same uncommitted session. Compute already stamped `backend_id` at dispatch; this brings kueue to parity.
- **Files modified:** `src/phaze/services/backends.py`.
- **Commit:** `94788e6`.

**2. [Rule 3 — Blocking issue] controller.py + agent_push.py touched for the grep-clean + guard removal**
- **Found during:** Tasks 1 & 2.
- **Issue:** `controller.startup` called `resolve_backends()` purely as a boot guard (redundant once the guard moved); and the plan's own verification requires `grep get_cloud_window_count src/phaze` to return nothing, but the symbol was named in comments in `routers/agent_push.py` and docstrings in `pipeline.py`/`backends.py`.
- **Fix:** Removed the redundant `resolve_backends` call + import from `controller.py` (guard preserved via `resolved_non_local_kind`); reworded all comment/docstring references so the grep is clean. Neither file is in the plan's `files_modified`, but both are directly implicated by the plan's Task actions and verification.
- **Files modified:** `src/phaze/tasks/controller.py`, `src/phaze/routers/agent_push.py`.
- **Commit:** `7ffef6a` (controller), `94788e6` (agent_push).

**3. [Rule 3 — Blocking issue] test_dispatch_snapshot.py golden stub updated**
- **Found during:** overall `just test-bucket analyze` verification.
- **Issue:** The Phase-68 BACK-04 golden's `_StubCfg` predates the two knobs `select_backend` reads; the cells that reach `select_backend` raised `AttributeError`.
- **Fix:** Added `cloud_submit_max_attempts=3` + `cloud_spill_to_local_after_seconds=900` to the stub. All 6 golden cells then pass with **byte-identical** expected dicts — confirming the multi-backend drain preserved single-backend observable behavior (the BACK-04 / D-01 golden proof holds).
- **Files modified:** `tests/analyze/core/test_dispatch_snapshot.py`.
- **Commit:** `ab6ea35`.

No architectural changes; no authentication gates. No new dependencies; no migrations.

## Threat Model Compliance

- **T-69-02-01 (over-dispatch DoS):** per-backend cap enforced by the once-per-tick `in_flight_count` snapshot + local `remaining[]` decrement inside the single `pg_advisory_xact_lock(5_000_504)` txn with one commit; verified by `test_overlapping_ticks_never_overshoot_per_backend_cap`.
- **T-69-02-02 (probe storm under lock):** `is_available()`/`in_flight_count()` probed exactly M times in the snapshot, never inside the candidate loop.
- **T-69-02-03 (SQLi):** advisory lock uses a bound literal; ORM + bound params throughout (no f-string SQL).
- **T-69-02-04 (info disclosure):** drain logs only backend ids + tally counts.

## Verification

- `uv run pytest tests/analyze/core/test_staging_cron.py tests/analyze/services/test_backends.py -x` — 38 passed.
- `just test-bucket analyze` — 409 passed (incl. the 8-cell BACK-04 golden byte-identical).
- Affected shared tests (`test_controller_startup_localqueue`, `test_bucket_registry`) — 23 passed.
- `-k overshoot`, `-k awaiting_untouched`, `-k multi_backend` each bind ≥1 test.
- `grep -rn "get_cloud_window_count" src/phaze` — nothing (retired).
- `uv run mypy src/phaze` — clean (157 files). `pre-commit run --files <changed>` — all hooks pass (never `--no-verify`).

## Known Stubs

None. The tiered drain is fully wired: it dispatches across N backends via `select_backend`, enforces per-backend `cap` by count-and-claim under the single advisory lock, and the global window count is gone. `select_backend`'s local-spill and attempt-exclusion branches are config-driven and exercised by Task 3's `awaiting_untouched` test and Plan 01's unit suite.

## Self-Check: PASSED

- SUMMARY.md present at `.planning/phases/69-tiered-drain-scheduler/69-02-SUMMARY.md`.
- All modified source files present on disk.
- All four commits present: `7ffef6a`, `94788e6`, `9a344d2`, `ab6ea35`.
