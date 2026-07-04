---
phase: 68-backend-protocol-3-implementations
plan: 04
subsystem: infra
tags: [backend-protocol, cloud-dispatch, drain, cloud_job, terminalization, reader-rewire, characterization-snapshot]

# Dependency graph
requires:
  - phase: 68-03
    provides: "Backend protocol + LocalBackend/ComputeAgentBackend/KueueBackend + resolve_backends() boot guard + resolved_non_local_kind() helper + per-backend in_flight_count (D-02 substrate)"
  - phase: 68-02
    provides: "cloud_job.backend_id nullable + s3_key nullable (migration 029, D-06/D-08)"
provides:
  - "Live drain (stage_cloud_window) dispatching through backend.dispatch()/is_available()/cap -- the if/elif cloud-kind fork is gone (BACK-01)"
  - "Compute cloud_job terminalization in report_pushed (D-08) so the D-02 equivalence invariant holds LIVE (BACK-03)"
  - "All active_cloud_kind readers resolve through resolved_non_local_kind() (D-09) -- the config accessor is now unreferenced (deletion in 68-05)"
  - "D-01 golden snapshot proven unchanged modulo the single deliberate compute cloud_job artifact (BACK-04 acceptance gate)"
affects: [68-05, 69-scheduler, 70-mkue, phase-71-beui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "typing.Protocol dispatch seam replacing an if/elif fork -- the drain owns the single post-loop commit; dispatch never commits (Landmine L1)"
    - "dispatch returns staged-vs-skipped bool so the drain preserves the Phase-50 deterministic-key dedup tally without re-introducing the fork"
    - "In-txn terminal cloud_job write gated behind the existing WR-02 rowcount!=0 idempotency guard (T-68-08)"

key-files:
  created: []
  modified:
    - "src/phaze/tasks/release_awaiting_cloud.py"
    - "src/phaze/services/backends.py"
    - "src/phaze/routers/agent_push.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/routers/agent_s3.py"
    - "src/phaze/tasks/controller.py"
    - "tests/analyze/core/test_dispatch_snapshot.py"
    - "tests/analyze/core/test_staging_cron.py"
    - "tests/shared/tasks/test_controller_startup_localqueue.py"

key-decisions:
  - "D-01/BACK-04: only the compute-cell cloud_job_count snapshot field changed (0 -> 2); every other asserted field byte-identical -- the behavior-preserving proof"
  - "D-08: report_pushed terminalizes the compute cloud_job (-> SUCCEEDED) in the same txn as PUSHING->PUSHED, WR-02-guarded (idempotent no-op writes nothing)"
  - "D-09: readers rewired to resolved_non_local_kind(); the config accessor stays until 68-05 (readers change before the accessor disappears -- no mid-wave import break)"
  - "D-02a: the drain KEEPS get_cloud_window_count for slot math; nothing consults per-backend in_flight_count for cap yet (that flip is Phase 69 / SCHED-02)"
  - "Backend.dispatch now returns bool (staged vs deduped-skip) so the drain keeps the Phase-50 tally without the fork -- a necessary Wave-2-protocol tweak within this refactor"
  - "Controller probe keeps its raise-on->1-non-local behavior via resolve_backends() inside the existing try/except (resolved_non_local_kind alone does not raise)"

requirements-completed: [BACK-01, BACK-03, BACK-04]

# Metrics
duration: ~75min
completed: 2026-07-03
---

# Phase 68 Plan 04: Rewire the Live Seams onto the Backend Protocol Summary

**The live cloud-window drain now dispatches through `backend.dispatch()`/`is_available()`/`cap` (the `if/elif cloud_target` fork is gone), `report_pushed` terminalizes compute's new `cloud_job` row so the D-02 invariant holds live, and every `active_cloud_kind` reader resolves through the registry-derived helper — proven behavior-preserving by the D-01 golden snapshot, which changed exactly one field (the deliberate compute `cloud_job` artifact).**

## Performance

- **Duration:** ~75 min
- **Completed:** 2026-07-03
- **Tasks:** 3 (Tasks 1 & 2 TDD-tagged; verified against the Wave-0 characterization gate)
- **Files modified:** 6 source + 3 test harnesses

## Accomplishments

- **Task 1 — drain rewire (BACK-01):** `stage_cloud_window` resolves its single non-local backend via `resolve_backends(cfg)` and dispatches through the protocol. GATE-1 became `backend.is_available()` (the D-01a asymmetry now lives inside the impls — compute gates on a live agent, kueue probes the cluster with no compute dependency); `active_cap` became `backend.cap`; the per-file `if active_cloud_kind == "kueue" … else …` fork became `await backend.dispatch(file, session, task_router)`. The skeleton is verbatim: advisory lock, `cloud_enabled` gate, `get_cloud_window_count` slot math (D-02a), GATE-2 fileserver, single post-loop commit.
- **Task 2 — compute terminalization (BACK-03 / D-08):** after the WR-02-guarded `PUSHING→PUSHED` UPDATE (rowcount != 0), `report_pushed` writes the file's `cloud_job` to `SUCCEEDED` in the same committed txn, so `in_flight_count(compute)` drains and `sum(in_flight_count) == get_cloud_window_count` holds live. The idempotent-no-op path (rowcount == 0) writes nothing (T-68-08).
- **Task 3 — reader rewire (D-09 / Layer 5):** `pipeline.py` dashboard `cloud_lane_kind` (:575) + ledger-seed fork (:810), `agent_s3.py` defensive guard (:113), and `controller.py` LocalQueue-probe gate (:179) all resolve through `resolved_non_local_kind()`. All three readers are grep-clean of `active_cloud_kind`; the config accessor is now unreferenced (68-05 deletes it).
- **BACK-04 acceptance gate:** the D-01 golden snapshot is green with exactly one changed field — the compute cell's `cloud_job_count` (0 → 2, the deliberate D-03/D-08 artifact). Every other asserted field across all 6 cells stayed byte-identical.

## Task Commits

1. **Task 1: Rewire stage_cloud_window onto backend.dispatch()/is_available()/cap** — `5ac51f7` (refactor)
2. **Task 2: Terminalize compute cloud_job in report_pushed (D-08)** — `a76c00c` (feat)
3. **Task 3: Rewire active_cloud_kind readers to resolved_non_local_kind() (D-09)** — `c4b75b7` (refactor)

## Verification

- `uv run pytest tests/analyze/core/test_dispatch_snapshot.py tests/analyze/core/test_staging_cron.py tests/analyze/services/test_backends.py` → **40 passed** (D-01 snapshot + Phase-50 staging regressions + 15-cell protocol suite + D-02 invariant).
- `uv run pytest tests/analyze/ tests/shared/ tests/agents/` → **1584 passed** (no regression across the rewired readers; 45 warnings are pre-existing AsyncMock coroutine warnings in unrelated `reenqueue`/`pipeline` test mocks).
- `uv run ruff check` + `uv run mypy` → clean on all 6 modified source files.
- Grep: no `active_cloud_kind` / `active_cap` reference remains in the drain or the 3 rewired readers; `_enqueue_push_file` no longer defined in `release_awaiting_cloud.py`.

## Deviations from Plan

The plan's `files_modified` listed 6 files, but the behavior-preserving refactor mechanically required three additional harness/relocation edits. All are Rule 1/3 fixes (relocation + test harness adaptation to the moved seam); none change any behavioral assertion. Each is documented below.

### Auto-fixed / relocation

**1. [Rule 3 — relocate] Moved `_enqueue_push_file` into `backends.py` + `dispatch` now returns `bool`**
- **Found during:** Task 1
- **Issue:** Wave 2 (68-03) re-homed `_enqueue_push_file` **by import** (its SUMMARY explicitly deferred the physical relocation to this wave). The Task-1 acceptance criterion "`_enqueue_push_file` no longer defined in `release_awaiting_cloud.py`" therefore requires **moving** the definition into `backends.py` and dropping `backends.py`'s import of it — which touches `backends.py` (not in `files_modified`). Additionally, `Backend.dispatch` was `-> None`, which cannot signal the Phase-50 deterministic-key dedup, so `test_staging_cron::test_double_tick_dedups_via_deterministic_key` (`{"staged":0,"skipped":1}`) would break.
- **Fix:** Relocated `_enqueue_push_file` into `backends.py` (keeping `push_file_job_key` in the drain — `test_staging_cron` imports it); the drain now imports `resolve_backends`/`LocalBackend` at call time (`# noqa: PLC0415`) to keep the `backends ↔ drain` import graph acyclic. Changed `Backend.dispatch` (+ all 3 impls) to return `bool` (True = genuine stage, False = deduped/held), so the drain preserves the staged/skipped tally without re-introducing the fork.
- **Files modified:** `src/phaze/services/backends.py`
- **Commit:** `5ac51f7`

**2. [Rule 3 — test harness] `test_staging_cron.py` `_StubCfg` gains `.backends`; kueue cells stub `get_local_queue`**
- **Found during:** Task 1
- **Issue:** The refactored drain calls `resolve_backends(cfg)` (needs `cfg.backends`) and clears GATE-1 through `KueueBackend.is_available` (probes `kube_staging.get_local_queue`). `test_staging_cron`'s `_StubCfg` predates the seam move — it only exposed `active_cap`/`cloud_enabled`/`active_cloud_kind`, and its kueue tests never stubbed the cluster probe.
- **Fix:** `_StubCfg` now also builds a registry-shaped `.backends` list (one entry duck-typing the Phase-67 submodel's `kind`/`id`/`rank`/`cap`); `_patch_s3` also stubs `get_local_queue` "reachable" so the kueue cells proceed exactly as before. **No behavioral assertion changed** — all staged/skipped counts and state assertions are byte-identical.
- **Files modified:** `tests/analyze/core/test_staging_cron.py`
- **Commit:** `5ac51f7`

**3. [Rule 3 — test harness] `test_controller_startup_localqueue.py` `_stub_controller` seeds the registry shape**
- **Found during:** Task 3
- **Issue:** The rewired controller probe reads the registry (`resolve_backends` + `resolved_non_local_kind`), not `cfg.active_cloud_kind`. `_stub_controller` set only `.active_cloud_kind` on a `MagicMock`, so the rewired reader would read a `MagicMock` `.backends`/`.cloud_enabled`.
- **Fix:** `_stub_controller` now seeds `.cloud_enabled` + a `.backends` list (single entry of the requested kind, `local` when disabled) — the exact registry shape the probe reads. The `>1-non-local → skip-probe` case (`test_multi_backend_registry_does_not_abort_boot`, real `ControlSettings`) is preserved because `resolve_backends()` carries the same raise the retired accessor did. **All probe-called/flag assertions unchanged.**
- **Files modified:** `tests/shared/tasks/test_controller_startup_localqueue.py`
- **Commit:** `c4b75b7`

### Snapshot test edit (sanctioned by the plan)

**4. [BACK-04 gate] Flipped the one compute cloud_job field + adapted the D-01a spy to the moved GATE-1**
- The compute-cell `cloud_job_count` expected value flipped `0 → 2` (the deliberate D-03/D-08 in-txn compute `cloud_job` write) and its `TODO(68-04)` comment was removed — the single sanctioned change.
- The `select_active_agent` spy now also patches the `backends` module reference (recording ONLY the `kind=="compute"` GATE-1 probe), because the compute gate moved from the drain into `ComputeAgentBackend.is_available`. `ComputeAgentBackend.dispatch`'s internal fileserver lookups stay un-tracked, exactly as `cloud_staging`'s already were — so `gate_kinds` stays byte-identical. This is a harness adaptation to the moved seam, not an assertion change.
- **Files modified:** `tests/analyze/core/test_dispatch_snapshot.py`
- **Commit:** `5ac51f7`

## Known Stubs

None. No placeholder/empty-value stubs introduced. `LocalBackend.dispatch` returning `True`/`False` and `KueueBackend.dispatch` returning `True` are genuine tally signals, not stubs.

## Threat Flags

None. No new network endpoint, auth path, or trust-boundary surface. The compute `cloud_job` terminal write is behind the existing WR-02 idempotency guard (T-68-08 mitigated); `dispatch` never commits mid-loop (T-68-09 mitigated); `is_available`/`dispatch` failures degrade to holds (T-68-10 mitigated); `resolved_non_local_kind` returns only a kind string, no creds (T-68-11 accept).

## Self-Check: PASSED
