---
phase: 68-backend-protocol-3-implementations
plan: 01
subsystem: testing
tags: [pytest, characterization, golden-snapshot, alembic, cloud_job, backends, importorskip]

# Dependency graph
requires:
  - phase: 67-backend-registry-config-model
    provides: "cloud_enabled / active_cloud_kind / active_cap registry-derived settings the drain reads"
provides:
  - "D-01 golden side-effect snapshot of stage_cloud_window over {compute,kueue,local}×{agent up,down} — the BACK-04 acceptance baseline captured on current post-67 code"
  - "Guarded Backend protocol unit + D-02 invariant scaffold (lights up in Wave 2 when services/backends.py lands)"
  - "Guarded migration 029 test scaffold (lights up in Wave 1 when 029_add_cloud_job_backend_id.py lands)"
affects: [68-02, 68-03, 68-04, 68-05]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Golden side-effect characterization snapshot vs current code (asserts observable side effects, not branch structure, so it survives a behavior-preserving refactor)"
    - "Guarded test scaffold: pytest.importorskip (module-level) for a future module; file-exists skipif for a future migration"

key-files:
  created:
    - tests/analyze/core/test_dispatch_snapshot.py
    - tests/analyze/services/test_backends.py
    - tests/integration/test_migrations/test_migration_029_backend_id.py
  modified: []

key-decisions:
  - "D-01: baseline is the current post-67 code (the a1/k8s paths were never deployed live); the snapshot asserts observable side effects so it stays green across the Wave 1-3 refactor"
  - "D-01a: the compute-checks-GATE-1 vs kueue-skips-GATE-1 asymmetry is a first-class explicit assertion via the recorded select_active_agent(kind=...) call log"
  - "Task 1 runs the REAL staging bodies (S3 SDK stubbed, DedupFakeTaskRouter) and observes real cloud_job rows / enqueues — a stronger golden capture than mocking the staging calls"
  - "D-10: the in-flight status set is pinned to {UPLOADING,UPLOADED,SUBMITTED,RUNNING}; terminal = {SUCCEEDED,FAILED}"

patterns-established:
  - "Characterization snapshot: capture on current code, assert unchanged after refactor (BACK-04 proof)"
  - "Forward-compatible mocking: stub the Kueue LocalQueue probe now (uncalled today, called post-refactor) so the snapshot survives Wave 2 without adding a tracked side effect"

requirements-completed: []  # Wave 0 authors the acceptance tests; BACK-01..04 complete once Waves 1-5 land their production targets

# Metrics
duration: 17min
completed: 2026-07-03
---

# Phase 68 Plan 01: Wave 0 Acceptance Tests Summary

**D-01 golden dispatch snapshot captured green on current post-67 code (the BACK-04 gate), plus guarded protocol-unit and migration-029 scaffolds that light up in later waves.**

## Performance

- **Duration:** ~17 min
- **Started:** 2026-07-04T02:31Z
- **Completed:** 2026-07-04T02:49Z
- **Tasks:** 3
- **Files created:** 3

## Accomplishments
- Captured the **D-01 golden side-effect snapshot** for `stage_cloud_window` over the full matrix `{compute, kueue, local} × {agent up, agent down}` (6 cells + 2 explicit D-01a assertions = 8 passing tests) on the UNMODIFIED post-67 code. This is the phase's acceptance baseline (BACK-04): the Wave 1-3 refactor must leave every asserted field byte-identical except the one TODO-marked compute `cloud_job` field.
- Asserted the **D-01a asymmetry** as a first-class observation: the compute cell requests `select_active_agent(kind="compute")` (GATE-1) and holds when it is absent; the kueue cell never requests it and stages regardless.
- Authored the **Layer 3 protocol unit scaffold** (15 cells: 3 impls × {is_available, in_flight_count, dispatch, reconcile}) + the **Layer 2 D-02 equivalence invariant**, guarded by `pytest.importorskip("phaze.services.backends")` so it collects as a clean module-skip now and lights up in Wave 2.
- Authored the **Layer 4 migration-029 scaffold** (static revision-id, saq_jobs grep, full upgrade/downgrade integration proving nullable `backend_id` + nullable `s3_key`), guarded by a file-exists `skipif` so its 3 tests collect and skip now, lighting up in Wave 1.

## Task Commits

Each task was committed atomically:

1. **Task 1: D-01 golden snapshot matrix (BACK-04 baseline)** - `563707b` (test)
2. **Task 2: Backend protocol unit + D-02 invariant scaffold (guarded)** - `0103404` (test)
3. **Task 3: Migration 029 test scaffold (guarded)** - `cc67b62` (test)

## Files Created/Modified
- `tests/analyze/core/test_dispatch_snapshot.py` - D-01 golden characterization matrix; drives the unmodified `stage_cloud_window`, records the ordered side-effect log per cell (gate kinds, staging task, FileState transition, `cloud_job` count, tally), compares to inline expected-dicts. 8 tests PASS on current code.
- `tests/analyze/services/test_backends.py` - Layer 3 protocol unit cells (≥12: 3 impls × 4 methods) + `test_in_flight_equivalence` (D-02). Module-skipped via `importorskip` until `phaze.services.backends` lands in Wave 2.
- `tests/integration/test_migrations/test_migration_029_backend_id.py` - Layer 4 migration test mirroring the 026 analog; `backend_id` nullable + no-backfill (D-06) and `s3_key` becomes nullable (D-08). 3 tests skip via file-exists guard until migration 029 lands in Wave 1.

## Decisions Made
- **Ran the real staging bodies in Task 1** (S3 SDK stubbed via `s3_staging.create_multipart_upload`/`presign_upload_parts`, `DedupFakeTaskRouter` for queues) rather than `AsyncMock`-ing `_stage_file_to_s3`/`_enqueue_push_file`. This produces a stronger golden capture — real `cloud_job` rows and real enqueue task names are observed — and reuses the exact harness of the already-passing `test_staging_cron.py`, giving high confidence the baseline is faithful. `select_active_agent` is spied (wraps the real selector) so the D-01a GATE-1 call log is recorded without changing behavior.
- **Kueue `up` vs `down` cells are byte-identical** (the compute agent is irrelevant to kueue) — that identity IS the D-01a proof, so `_KUEUE_DOWN_EXPECTED = dict(_KUEUE_UP_EXPECTED)`.
- **Forward-compatible `get_local_queue` stub** applied in every cell: uncalled on current code (harmless), probed by the post-refactor `KueueBackend.is_available`; stubbing now keeps the snapshot green across Wave 2 without adding a tracked side effect (the tracked gate observation remains the `select_active_agent` call log only).

## Deviations from Plan

None - plan executed exactly as written. All three verify commands pass; the combined phase `<verification>` collect and run both exit 0.

## Issues Encountered
- The ephemeral test DB (`phaze-test-db` on port 5433) was not running at start; the DB-backed snapshot fixtures errored. Resolved by `just test-db` (starts Postgres 5433 + Redis 6380) and exporting `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` for the pytest runs. This is standing local-UAT setup, not a code issue.

## Test / Verification Notes
- `tests/analyze/core/test_dispatch_snapshot.py` — **8 passed** on current post-67 code.
- `tests/analyze/services/test_backends.py` — **1 skipped** (module-level `importorskip`; `backends.py` absent). Running this file alone returns pytest exit 5 (no items collected) by design of module-level `importorskip`; the phase-level combined collect/run (all three files) exits 0.
- `tests/integration/test_migrations/test_migration_029_backend_id.py` — **3 collected, 3 skipped** (file-exists guard; migration 029 absent), exit 0.
- Combined `--collect-only -q` and full run of all three files: **exit 0** (8 passed, 4 skipped).

## Next Phase Readiness
- BACK-04 acceptance gate is **armed**: `test_dispatch_snapshot.py` is green on today's code. Wave 1-3 refactors must keep it byte-identical except the single `TODO(68-04)` compute `cloud_job` field.
- Wave 1 (68-02): when `alembic/versions/029_add_cloud_job_backend_id.py` lands, the migration scaffold auto-activates — it expects `revision == "029"`, `down_revision == "028"`, nullable `backend_id` (no backfill), and nullable `s3_key`.
- Wave 2: when `src/phaze/services/backends.py` lands with `LocalBackend`/`ComputeAgentBackend`/`KueueBackend` + `resolve_backends`, the protocol scaffold auto-activates. Constructor signatures were factored into `_local`/`_compute`/`_kueue` helpers; if the finalized signatures differ, adjust only those three factories.

---
*Phase: 68-backend-protocol-3-implementations*
*Completed: 2026-07-03*
