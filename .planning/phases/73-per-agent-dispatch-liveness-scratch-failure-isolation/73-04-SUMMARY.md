---
phase: 73-per-agent-dispatch-liveness-scratch-failure-isolation
plan: 04
subsystem: cloud-compute-dispatch
tags: [regression-tests, failure-isolation, scratch-retirement, behavior-preservation, D-05, D-08]
requires:
  - ComputeAgentBackend.is_available per-entry agent_ref gating (Phase 72, MCOMP-01)
  - select_backend rank/cap policy (Phase 69, SCHED-01)
  - per-backend snapshot try/except isolation (Phase 70, release_awaiting_cloud.py:151-157)
  - resolve_compute_backend inverse-lookup (Plan 73-01, D-06)
  - payload-driven _build_rsync_argv (Plan 73-02, D-04)
  - backend_id-scoped /pushed scratch resolution (Plan 73-03, D-06)
provides:
  - MCOMP-02 N-compute per-agent liveness regression (only the online bound lane is available)
  - MCOMP-04 N-compute rank/cap load-spread regression (lowest-rank-first then spill)
  - MCOMP-05 one-flaky-compute-lane isolation regression (degrade to 0 slots, tick completes)
  - retired ControlSettings.active_compute_scratch_dir accessor (no runtime reader remains)
  - <=1-compute behavior-preservation golden (rsync remote_dest + /pushed scratch_path byte-identical)
affects:
  - closes the Phase 73 nyquist validation map (every MCOMP-02..06 behavior has an automated regression)
  - completes the transitional-global retirement (scratch resolution fully per-file)
tech-stack:
  added: []
  patterns:
    - regression-only proof of REUSED machinery (D-08: no new scheduler policy, no src behavior change in Tasks 1/3)
    - byte-identical golden characterization on the unchanged one-row-per-file schema (D-05, no migration)
    - transitional-global retirement mirroring the Phase-70 active_kube/active_bucket removals
key-files:
  created: []
  modified:
    - src/phaze/config.py
    - src/phaze/routers/agent_push.py
    - tests/analyze/services/test_backends.py
    - tests/analyze/services/test_backend_selection.py
    - tests/analyze/tasks/test_release_awaiting_cloud.py
    - tests/analyze/services/test_compute_binding_golden.py
    - tests/shared/config/test_bucket_registry.py
decisions:
  - "Migrated (not just docstring-touched) the golden's active_compute_scratch_dir readers in Task 2 so every commit stays green under the accessor deletion; Task 3 then ADDS genuinely new goldens (the rsync remote_dest leg + the recorded-backend_id /pushed path), avoiding a transient red golden between commits."
  - "MCOMP-05 uses the existing generic _StubBackend (a duck-typed non-local cloud backend) labelled as compute lanes -- the snapshot try/except is backend-kind-agnostic, so the compute-flavoured regression exercises the identical isolation surface as the Kueue cell."
  - "Refreshed two stale config.py comments + one agent_push.py comment from 'retained through Phase 70' / 'its deletion is Plan 04' to past tense (accessor deleted) -- directly caused by the Task 2 deletion (Rule 3, doc consistency)."
metrics:
  tasks: 3
  source-files-modified: 2
  test-files-modified: 5
  completed: 2026-07-05
---

# Phase 73 Plan 04: N-Compute Regressions, Scratch-Accessor Retirement + Behavior-Preservation Golden Summary

Closed the Phase 73 nyquist validation map by adding automated regressions for the machinery the phase REUSES (D-08: no new scheduler policy) -- N-compute per-agent liveness (MCOMP-02), rank/cap load-spread (MCOMP-04), and one-flaky-compute-lane failure isolation (MCOMP-05) -- then retired the now-dead `active_compute_scratch_dir` accessor (its last runtime reader was rewired in Plan 03) and locked the <=1-compute push path byte-identical. All regressions and goldens run against the existing one-row-per-file `cloud_job` schema (D-05: no migration, no schema change).

## What Was Built

**Task 1 -- N-compute liveness + rank/cap spread + one-flaky isolation regressions (test-only, D-08).** Added three regressions against UNCHANGED machinery (no `src/` edit):
- **MCOMP-02** (`test_backends.py`): a 2-compute registry where only compute-a's bound agent (`cloud-a`) is online and compute-b's (`cloud-b`) is absent -> `backend_a.is_available` True, `backend_b.is_available` False. Per-entry `agent_ref` gating, not a single-active pick.
- **MCOMP-04** (`test_backend_selection.py`): two compute lanes (free-arm64 rank 10, paid-x86 rank 20) -- while the free lane has a slot it wins (rank-first); once it is at cap (`remaining==0`) the next candidate spills to the paid lane rather than holding.
- **MCOMP-05** (`test_release_awaiting_cloud.py`): `stage_cloud_window` with two compute lanes where lane A's `is_available` RAISES -> A degrades to `available=False / remaining=0` (T-73-11 DoS mitigation via the per-backend snapshot try/except), the tick COMPLETES without raising, and both FIFO candidates route to healthy lane B (`compute-b`), which the flaky lane never being selected (0 dispatch calls).

**Task 2 -- deleted the `active_compute_scratch_dir` accessor + migrated its unit tests.** Removed the transitional `ControlSettings.active_compute_scratch_dir` `@property` (its last runtime reader, `/pushed`, was rewired to per-file `resolve_compute_backend` in Plan 03) and replaced it with a retirement note mirroring the adjacent Phase-70 `active_kube` / `active_bucket` retirements. Re-confirmed no runtime reader remained (`grep` showed only comments). Migrated all readers to the equivalent per-file `resolve_compute_backend(cfg, backend_id).scratch_dir`, keeping the resolved strings byte-identical: `test_bucket_registry.py` (implicit-local None, single-compute value, the >1-compute reduction test rewritten to assert each entry resolves its OWN scratch_dir), `test_backends.py` (the local+2Kueue+1compute cell), and `test_compute_binding_golden.py` (the pure-resolution + zero-compute cells). `AgentSettings.cloud_scratch_dir` (the compute janitor field, Landmine 2) left intact.

**Task 3 -- <=1-compute behavior-preservation golden (D-05) + reenqueue known-limitation note.** Extended the compute-binding golden with two byte-identical characterizations for a single-compute registry on the unchanged schema: (a) `_build_rsync_argv` remote_dest equals `bursty@oci-a1.push.example:/srv/scratch/<file_id>.mp3` with `dest_ssh_user=None` falling back to `cfg.push_ssh_user` (proves A3 -- the fallback preserves the user); (b) the `/pushed` scratch_path resolved from the RECORDED `cloud_job.backend_id` via `resolve_compute_backend` equals `/srv/scratch/<file_id>.mp3` (the same string the retired accessor produced). Documented `reenqueue.py:374 recover_orphaned_work`'s remaining `select_active_agent(kind="compute")` single-active reader as a PROV-01 backlog known limitation in the module docstring -- OUT OF SCOPE, NOT widened (44.5k over-enqueue incident risk). `reenqueue.py` received no change.

## Deviations from Plan

### Auto-fixed Issues (Rule 3 -- blocking, directly caused by the accessor deletion)

**1. [Rule 3 - Blocking] Migrated the golden's accessor readers in Task 2 (not just docstrings)**
- **Found during:** Task 2
- **Issue:** Deleting the `active_compute_scratch_dir` property breaks `test_compute_binding_golden.py`'s L80/L85/L150 runtime reads (`AttributeError`). The plan scoped the golden re-anchoring to Task 3, whose verify command excludes the golden -- so a Task-2-only docstring edit would leave a red golden between the Task 2 and Task 3 commits.
- **Fix:** Migrated the golden's pure-resolution + zero-compute cells to `resolve_compute_backend(...).scratch_dir` (byte-identical strings) in Task 2, keeping every commit green. Task 3 then ADDS the genuinely new goldens (the rsync remote_dest leg + the recorded-backend_id `/pushed` path), so there is no overlap.
- **Files modified:** `tests/analyze/services/test_compute_binding_golden.py`
- **Commit:** 942da5c3

**2. [Rule 3 - Blocking] Refreshed stale accessor comments to past tense**
- **Found during:** Task 2
- **Issue:** Two `config.py` comments described the accessor as "retained through Phase 70 / MKUE-01" and `agent_push.py:149` said the accessor "is now UNUSED here (its deletion is Plan 04)" -- all stale once the property is deleted.
- **Fix:** Updated all three to past tense (accessor deleted in Phase 73 / MCOMP-03; scratch resolved per file via `resolve_compute_backend`).
- **Files modified:** `src/phaze/config.py`, `src/phaze/routers/agent_push.py`
- **Commit:** 942da5c3

## Threat Model Coverage

| Threat ID | Disposition | Realized |
|-----------|-------------|----------|
| T-73-11 (a flaky compute backend's probe failure aborting the whole drain tick -> DoS on healthy lanes) | mitigate | The MCOMP-05 regression PROVES the per-backend snapshot try/except (release_awaiting_cloud.py:151-157): a raising `is_available` degrades that lane to 0 slots, the tick completes, and a healthy sibling still dispatches. |
| T-73-12 (recover_orphaned_work single-active re-drive mis-routing on N-compute deploys) | accept | Documented as a PROV-01 known limitation in the golden module docstring; reenqueue.py NOT widened (44.5k over-enqueue incident risk). |
| T-73-SC (dependency installs) | accept | Zero new dependencies; pyproject untouched. |

No new security surface. No threat flags.

## Verification

- Task verifies (serial, per test-env note): Task 1 `-k "compute or rank or spread or flaky or isolat or available or mcomp"` -> 26 passed; Task 2 (`test_bucket_registry.py` + `test_backends.py`) -> 57 passed; Task 3 (`test_compute_binding_golden.py`) -> 6 passed.
- Full targeted suite (all five files) -> **83 passed**.
- Phase gate: `uv run ruff check .` + `uv run ruff format --check .` -> clean (483 files); `uv run mypy .` -> **Success: no issues found in 196 source files**.
- Acceptance greps: `def active_compute_scratch_dir` absent from `src/`; no `settings.active_compute_scratch_dir` / `self.active_compute_scratch_dir` attribute-access expression in `src/` (only comments); `cloud_scratch_dir` field intact in `config.py` (Landmine 2); `reenqueue` note present in the golden (3 mentions); `git diff --name-only src/phaze/tasks/reenqueue.py` empty; Task 1 introduced NO `src/` change.

## Known Stubs

None -- every regression asserts against real, wired machinery on the existing schema; no placeholder data or unwired components.

## Self-Check: PASSED
