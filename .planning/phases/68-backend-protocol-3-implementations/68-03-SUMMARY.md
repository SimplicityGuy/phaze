---
phase: 68-backend-protocol-3-implementations
plan: 03
subsystem: infra
tags: [backend-protocol, cloud-dispatch, kueue, compute-agent, cloud_job, typing-protocol, sqlalchemy]

# Dependency graph
requires:
  - phase: 67-backend-registry-config-model
    provides: "backends.toml discriminated-union submodels (LocalBackend/ComputeBackend/KueueBackend) + cloud_enabled gate"
  - phase: 68-01
    provides: "Guarded protocol/invariant test scaffold (tests/analyze/services/test_backends.py, importorskip-gated)"
  - phase: 68-02
    provides: "cloud_job.backend_id nullable column + s3_key nullable (migration 029, D-06/D-08)"
provides:
  - "Backend typing.Protocol (is_available/in_flight_count/dispatch/reconcile) — the seam that removes the if/elif dispatch fork"
  - "LocalBackend / ComputeAgentBackend / KueueBackend implementations re-homing existing dispatch bodies verbatim"
  - "Uniform cloud_job-derived per-backend in_flight_count (D-02/D-10 substrate)"
  - "resolve_backends() boot guard (raise-on->1-non-local, D-07) + resolved_non_local_kind() registry-derived helper"
affects: [68-04, 69-scheduler, 70-mkue, phase-71-beui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "typing.Protocol structural dispatch seam over already-isolated async bodies (thin adapter, re-home not rewrite)"
    - "Per-backend in-flight accounting via COUNT(cloud_job WHERE backend_id AND status IN in-flight)"
    - "Fail-fast-at-boot guard relocated from a config accessor into resolve_backends()"

key-files:
  created:
    - "src/phaze/services/backends.py"
  modified: []

key-decisions:
  - "D-01a: GATE-1 asymmetry lives in per-kind is_available (compute requires a live agent; kueue probes the cluster with NO compute dependency)"
  - "D-02/D-10: in_flight_count counts {UPLOADING,UPLOADED,SUBMITTED,RUNNING}; equivalence invariant sum(in_flight_count)==get_cloud_window_count proven"
  - "D-03: dispatch owns the FileState->PUSHING flip AND the cloud_job upsert in one caller-passed session, before the enqueue, never a separate commit"
  - "D-05: KueueBackend calls single-cluster _stage_file_to_s3 / kube_staging verbatim; no per-cluster parameterization (Phase 70)"
  - "D-07: raise-on->1-non-local guard relocated into resolve_backends(); cloud_enabled stays in config"
  - "Re-home by IMPORT (not move): backends.py imports _enqueue_push_file / _stage_file_to_s3 / _reconcile_one / get_local_queue so the live drain stays untouched (pure additive); Wave 3 (68-04) relocates + rewires"

patterns-established:
  - "Backend protocol: structural typing.Protocol + _BaseBackend carrying id/rank/cap + shared in_flight_count; concrete impls override is_available/dispatch/reconcile"
  - "Cron no-op discipline preserved: is_available/dispatch/reconcile degrade to a hold (return False / clean no-op), never raise out to a cron"

requirements-completed: [BACK-01, BACK-03]

# Metrics
duration: 35min
completed: 2026-07-03
---

# Phase 68 Plan 03: Backend Protocol + 3 Implementations Summary

**A single internal `Backend` protocol with Local/ComputeAgent/Kueue implementations re-homing the existing dispatch/staging/submit/reconcile bodies verbatim, plus a uniform per-backend `in_flight_count` substrate whose D-02 equivalence invariant is proven — purely additive, the live drain is untouched.**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-07-03
- **Completed:** 2026-07-03
- **Tasks:** 2 (both TDD, GREEN against the Wave-0 guarded scaffold)
- **Files modified:** 1 created (`src/phaze/services/backends.py`)

## Accomplishments
- Created `src/phaze/services/backends.py`: the `Backend` `typing.Protocol` (design §4.2) + `_BaseBackend` + the three concrete implementations, all re-homing existing bodies (thin adapter, not a rewrite).
- Uniform `in_flight_count` (D-02/D-10 substrate) filtered by `backend_id` + the `{UPLOADING,UPLOADED,SUBMITTED,RUNNING}` in-flight set; the D-02 equivalence invariant (`sum(in_flight_count(b)) == get_cloud_window_count()`) passes green.
- GATE-1 asymmetry proven (D-01a): `ComputeAgentBackend.is_available` gates on a live compute agent; `KueueBackend.is_available` probes the LocalQueue with **no** compute-agent dependency; both return `bool` and never raise.
- D-03 atomicity proven: `ComputeAgentBackend.dispatch` flips `PUSHING` **and** writes its `cloud_job` row (`backend_id` set, `s3_key` NULL, `SUBMITTED`) in the same uncommitted session; no commit inside dispatch.
- `resolve_backends()` boot guard (D-07, raise-on->1-non-local naming offending ids) + `resolved_non_local_kind()` registry-derived helper for the Wave-3 reader rewires.
- The guarded protocol suite (15 cells + D-02 invariant) lit up and passes; the D-01 golden dispatch snapshot stayed byte-identical (8 cells); `tests/analyze/` + `tests/shared/` = 1190 passed.

## Task Commits

Each task was committed atomically:

1. **Task 1: Protocol + shared in_flight_count + LocalBackend + resolve_backends + kind helper** - `29a62c8` (feat)
2. **Task 2: ComputeAgentBackend + KueueBackend (re-home dispatch/is_available/reconcile)** - `23592e8` (feat)

_TDD note: the RED contract is the Wave-0 `pytest.importorskip`-gated scaffold (`tests/analyze/services/test_backends.py`), which was skipped until `backends.py` appeared and turned green on creation. Task 1 was verified against the local-cells + D-02-invariant subset; Task 2 turned the full 15-cell suite green._

## Files Created/Modified
- `src/phaze/services/backends.py` - The `Backend` protocol + `_BaseBackend` (id/rank/cap + shared `in_flight_count`) + `LocalBackend` / `ComputeAgentBackend` / `KueueBackend` + `resolve_backends()` boot guard + `resolved_non_local_kind()` helper.

## Verification
- `uv run pytest tests/analyze/services/test_backends.py` → **15 passed** (all protocol cells + D-02 invariant).
- `uv run pytest tests/analyze/core/test_dispatch_snapshot.py` → **8 passed** (D-01 golden snapshot byte-identical — the live drain was not touched).
- `uv run pytest tests/analyze/ tests/shared/` → **1190 passed** (no regression).
- `uv run ruff check src/phaze/services/backends.py` → clean; `uv run ruff format --check` → formatted.
- `uv run mypy src/phaze/services/backends.py` → clean.

## Deviations from Plan

**None affecting behavior — one deliberate re-home mechanism choice within the plan's `files_modified` boundary:**

- **[Re-home by import, not move]** The plan's Task 2 action text suggested *moving* `_enqueue_push_file` from `release_awaiting_cloud.py` into `backends.py` ("Wave 3 deletes the now-dead original"). Because this plan's `files_modified` is scoped to `backends.py` + its test and the plan is explicitly **pure-additive (do NOT rewire any live seam this phase)**, the re-home is implemented by **importing** `_enqueue_push_file` / `_stage_file_to_s3` / `_reconcile_one` / `kube_staging.get_local_queue` from their existing homes rather than duplicating or moving them. This keeps the live drain byte-identical (D-01 snapshot green), avoids intra-phase drift from a copy, and leaves the actual relocation/rewire to Wave 3 (plan 68-04). Net behavior is identical to the plan's intent.

## Known Stubs
None. `ComputeAgentBackend` / `KueueBackend` were committed as `raise NotImplementedError` stubs at the end of Task 1 (to keep that commit's imports lint-clean and the module importable for the Task-1 subset) and were fully implemented in Task 2's commit — no stub remains in the final state.

## Threat Flags
None. No new network endpoint, auth path, or trust-boundary surface was introduced — every body is a verbatim re-home of an already-existing dispatch/staging/submit/reconcile path. Secret hygiene (T-68-04) is preserved: `backends.py` logs only `{id, kind, rank, cap}`/`backend_id`-level fields; the kube SA-token hack is read via the re-homed body but never logged.
