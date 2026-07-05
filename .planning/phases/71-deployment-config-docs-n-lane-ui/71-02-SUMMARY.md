---
phase: 71-deployment-config-docs-n-lane-ui
plan: 02
subsystem: api
tags: [routing, cloud-burst, control-table, alembic, force-local, sqlalchemy, degrade-safe]

# Dependency graph
requires:
  - phase: 67-multi-cloud-registry
    provides: "cloud_enabled registry gate + all-local (cloud_enabled=False) routing path this override mirrors"
  - phase: 50-cloud-window
    provides: "stage_cloud_window drain + AWAITING_CLOUD hold the force-local gate no-ops"
  - phase: 37-pipeline-stage-control
    provides: "pipeline_stage_control single-purpose control-table + get_stage_controls degrade pattern"
provides:
  - "route_control one-row control table (id PK 'global', force_local bool default false) + migration 031"
  - "RouteControl ORM model registered for Alembic autogenerate + create_all"
  - "get_route_control degrade-safe reader (False on absent row / any DB error, never raises)"
  - "force-local gate wired at the drain + both duration-router callers + the backfill trigger"
affects: [71-04-force-local-endpoint-and-pill, runbook]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Single-row control table ('global' PK) mirroring the per-stage pipeline_stage_control pattern"
    - "Degrade-safe hot-path reader: guarded double-rollback -> safe default, never raises (T-71-03)"
    - "Effective-flag fold at the caller (cloud_enabled AND NOT force_local); select_backend stays pure"

key-files:
  created:
    - src/phaze/models/route_control.py
    - alembic/versions/031_add_route_control.py
    - src/phaze/services/route_control.py
    - tests/integration/test_migrations/test_migration_031_route_control.py
    - tests/shared/routers/test_routing.py
  modified:
    - src/phaze/models/__init__.py
    - src/phaze/tasks/release_awaiting_cloud.py
    - src/phaze/routers/pipeline.py
    - tests/analyze/core/test_staging_cron.py

key-decisions:
  - "Gated the backfill trigger as a THIRD force-local site (fold into its disabled early-return) so forced-local never strands failed long files held-but-never-drained"
  - "Defaulting to False (cloud-enabled) on DB error is the fail-safe: a transient hiccup must not silently flip the whole registry to force-local"

patterns-established:
  - "Force-local override folds into cloud_enabled at every call site that already reads it; the routing policy (select_backend) stays pure and un-gated"

requirements-completed: [BEUI-02]

# Metrics
duration: 20min
completed: 2026-07-05
---

# Phase 71 Plan 02: Force-Local Routing Override Summary

**A persisted one-row `route_control` flag with a degrade-safe reader that, when engaged, makes the drain, both duration-router triggers, and the backfill trigger all behave exactly like an all-local registry — no redeploy.**

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-07-05
- **Tasks:** 3
- **Files modified:** 9 (5 created, 4 modified)

## Accomplishments
- `route_control` control table (migration 031, seeded single `'global'` row, `force_local` default false) + `RouteControl` model registered
- `get_route_control` degrade-safe reader — True iff the `'global'` row is forced; absent row or any DB error → False, never raises (T-71-03)
- Force-local gate wired at every routing site: the `stage_cloud_window` drain (clean `{staged:0,skipped:0}` no-op before the advisory lock), `trigger_analysis` + `trigger_analysis_ui` (effective `cloud_enabled AND NOT force_local`), and the backfill trigger (folded into its disabled early-return)
- `select_backend` left pure/untouched (grep-gated to 0 references)

## Task Commits

1. **Task 1: RouteControl model + migration 031 + register** - `d99f49b` (feat)
2. **Task 2: get_route_control degrade-safe reader** - `eaf72a1` (test, RED) → `c251b69` (feat, GREEN)
3. **Task 3: Wire the two routing gates (+ backfill)** - `25b2fbd` (test, RED) → `7a496bc` (feat, GREEN)

## Files Created/Modified
- `src/phaze/models/route_control.py` - `RouteControl` single-row control model (id PK, force_local bool)
- `alembic/versions/031_add_route_control.py` - create `route_control` + bound-param seed of the `'global'` row; down_revision 030
- `src/phaze/services/route_control.py` - `get_route_control` degrade-safe reader (mirrors `get_stage_controls`)
- `src/phaze/models/__init__.py` - registered `RouteControl` (import + `__all__`)
- `src/phaze/tasks/release_awaiting_cloud.py` - drain no-op gate after session open, before the advisory lock
- `src/phaze/routers/pipeline.py` - effective-flag fold at `trigger_analysis` / `trigger_analysis_ui`; force-local fold into the backfill disabled guard
- `tests/integration/test_migrations/test_migration_031_route_control.py` - upgrade seeds one force_local=false row, downgrade drops the table
- `tests/analyze/core/test_staging_cron.py` - `test_forced_local_drain_noop`
- `tests/shared/routers/test_routing.py` - reader degrade tests + `test_route_forced_local_no_hold`

## Decisions Made
- **Backfill as a third gate site (extends plan's two named callers).** The plan named `trigger_analysis` + `trigger_analysis_ui` explicitly and said to route the effective flag "through it consistently". The backfill trigger (`/pipeline/backfill-cloud`) is the third `_route_discovered_by_duration` caller and independently dispatches to cloud. Under force-local, leaving it un-gated would reset failed long files to DISCOVERED and HOLD them in AWAITING_CLOUD while the (forced) drain no-ops — stranding them. Folding `force_local` into its existing `not settings.cloud_enabled` disabled early-return keeps it a clean zero-mutation no-op, byte-identical to the all-local path (D-08, T-71-08). Documented here as a Rule 2 correctness addition.
- **Fail-safe default False on DB error** — a transient hiccup must never silently flip the whole registry to force-local; the override is an explicit operator action, so its unreadability degrades to normal cloud-enabled behavior.

## Runbook Note (A4)
Already-held `AWAITING_CLOUD` files STAY held while force-local is engaged: the drain no-ops, so nothing dispatches them, but nothing releases them either. Engaging force-local stops NEW cloud routing; it does not retroactively drain or spill the existing held backlog. This is the documented A4 behavior for the Plan 05 runbook.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Gated the backfill trigger as the third force-local site**
- **Found during:** Task 3 (wiring the routing gates)
- **Issue:** Gating only the drain + the two analysis triggers leaves `/pipeline/backfill-cloud` able to reset ANALYSIS_FAILED long files to DISCOVERED and hold them in AWAITING_CLOUD under force-local, where the no-op'd drain would never drain them (T-71-08 unintended-dispatch / stranding).
- **Fix:** Folded `await get_route_control(session)` into backfill's existing `not settings.cloud_enabled` disabled early-return so forced-local is a clean zero-mutation no-op.
- **Files modified:** src/phaze/routers/pipeline.py
- **Verification:** ruff + mypy clean; existing `test_backfill_disabled_when_cloud_local` and all backfill tests still green in isolation.
- **Committed in:** 7a496bc (Task 3 GREEN)

---

**Total deviations:** 1 auto-fixed (1 missing critical, Rule 2)
**Impact on plan:** Necessary for the force-local override to actually behave like the all-local path across every dispatch site. No scope creep — same pattern, one additional caller the plan's "route consistently" note anticipated.

## Issues Encountered
- **Cross-file DB-isolation flake (not a regression):** running `test_staging_cron.py` + `test_routing.py` (or the full `test_pipeline.py`) back-to-back against the shared `phaze_test` DB intermittently errors with an `IntegrityError` on `pk_agents` (the documented colima `drop_all`/`create_all` seed race — see MEMORY "Local full-suite colima flake"). Every suite passes cleanly in isolation: migration 3/3, `test_routing.py` 5/5, `test_staging_cron.py` 23/23, `test_pipeline.py` 92/92. Validation followed the plan's "run new tests in isolation (Pitfall 5)" guidance.

## Test / Verification Results
- `test_migration_031_route_control.py`: 3 passed (against ephemeral Postgres on :5433)
- `tests/shared/routers/test_routing.py`: 5 passed (isolation)
- `tests/analyze/core/test_staging_cron.py`: 23 passed (isolation)
- `tests/shared/routers/test_pipeline.py`: 92 passed (isolation) — no regressions from the router changes
- `uv run ruff check .`: All checks passed
- `uv run mypy .`: Success, no issues in 195 source files
- Grep gates: `get_route_control` present in `release_awaiting_cloud.py` (3) + `pipeline.py` (4); absent in `backend_selection.py` (0 — select_backend stays pure)

## Next Phase Readiness
- Ready for Plan 04: the write endpoint + header pill can flip `route_control.force_local` on the seeded `'global'` row; all read/gate sites already honor it.
- No external service configuration required.

## Self-Check: PASSED

---
*Phase: 71-deployment-config-docs-n-lane-ui*
*Completed: 2026-07-05*
