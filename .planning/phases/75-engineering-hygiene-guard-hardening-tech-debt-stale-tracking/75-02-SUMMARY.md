---
phase: 75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking
plan: 02
subsystem: testing
tags: [pytest, pytest-asyncio, httpx, force-local, duration-router, route-control, regression-test]

# Dependency graph
requires:
  - phase: 71-deployment-config-docs-n-lane-ui
    provides: force-local RouteControl toggle + the three effective_cloud_enabled gate sites (BEUI-02)
  - phase: 49-duration-routing-backfill
    provides: per-file duration router + AWAITING_CLOUD hold + backfill-cloud endpoint
provides:
  - Committed force-local duration-router gate regression region in tests/shared/routers/test_pipeline.py (4 cases)
  - Real-route coverage of the effective_cloud_enabled fold at all 3 gate sites (pipeline.py L396/L718/L793) + a False control
affects: [milestone-close, docs-drift-guard, HYG-04]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Force-local driven by a persisted RouteControl(id='global', force_local=True) row on the shared session (no set_route_control fn, no get_route_control monkeypatch)"
    - "Autouse cloud-ON registry KEPT so the toggle is the only force-local variable; anti-cheat asserts AWAITING_CLOUD row ABSENCE, not an enqueue count"

key-files:
  created: []
  modified:
    - tests/shared/routers/test_pipeline.py

key-decisions:
  - "Seeded the toggle via a direct RouteControl(id='global', force_local=True) row insert + commit (set_route_control is fictional); no monkeypatch of get_route_control"
  - "Backfill no-op case uses _persist_failed_with_duration([_LONG], with_ledger=False) so the 'no SchedulingLedger seeded' signal is a true zero-mutation assertion"
  - "Kept the autouse _cloud_compute_registry (cloud-ON) in all True cases; assert AWAITING_CLOUD absence via a FileRecord state select (anti-cheat, RESEARCH Pitfall 2)"

patterns-established:
  - "Gate-site regression at real-route altitude: persisted control row + client AsyncClient fixture exercises the live effective_cloud_enabled fold, not a unit call"

requirements-completed: [HYG-04]

# Metrics
duration: ~15min
completed: 2026-07-06
---

# Phase 75 Plan 02: Force-Local Duration-Router Gate Regression Test Summary

**Committed force-local regression region (4 cases) covering the effective_cloud_enabled gate at all three live sites (pipeline.py L396/L718/L793) — force-local True routes a _LONG file local with ZERO AWAITING_CLOUD held and makes backfill a zero-mutation no-op, while the False control proves the persisted RouteControl toggle is the only variable.**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-07-06T16:01:00Z
- **Completed:** 2026-07-06T16:06:00Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments
- Gate L396 (POST /api/v1/analyze) + gate L718 (POST /pipeline/analyze): force-local True routes a >=threshold DISCOVERED file LOCAL, zero AWAITING_CLOUD rows held.
- Gate L793 (POST /pipeline/backfill-cloud): force-local True is a zero-mutation no-op — nothing enqueued, candidate stays ANALYSIS_FAILED, no SchedulingLedger row seeded.
- False control (no route_control row) proves the same _LONG file IS held AWAITING_CLOUD when the toggle is off — the toggle is the only variable.
- L396 case cross-references and consolidates the prior partial coverage in test_routing.py::test_route_forced_local_no_hold (D-09).
- Zero `src/` diff — the gate lines are exercised, not modified.

## Task Commits

Each task was committed atomically:

1. **Task 1: Force-local analyze gates (L396 + L718) + False control** - `a01a7bf8` (test)
2. **Task 2: Force-local backfill gate (L793) zero-mutation no-op** - `63589cd5` (test)

_Note: TDD plan tasks — tests authored and confirmed green (TDD_MODE off project-wide; wrote the cases per the plan behavior/action blocks)._

## Files Created/Modified
- `tests/shared/routers/test_pipeline.py` - Added the Phase 75 force-local gate regression region: `test_force_local_analyze_api_routes_local_no_hold`, `test_force_local_analyze_ui_routes_local_no_hold`, `test_force_local_analyze_api_false_control_still_holds`, `test_force_local_backfill_zero_mutation_no_op`; added `from phaze.models.route_control import RouteControl` import.

## Decisions Made
- Seeded force-local via a direct `RouteControl(id="global", force_local=True)` row + `await session.commit()` before the POST — no fictional `set_route_control`, no `get_route_control` monkeypatch.
- Used `with_ledger=False` for the backfill candidate so the "no SchedulingLedger row seeded" signal is a genuine zero-mutation assertion (matching `test_backfill_disabled_when_cloud_local`).
- Kept the autouse cloud-ON `[_COMPUTE_BACKEND]` registry in every True case; asserted AWAITING_CLOUD absence via a `select(FileRecord).where(state == AWAITING_CLOUD)` scalars check (anti-cheat, never a bare enqueue count).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- The suite requires the ephemeral test Postgres/Redis (ports 5433/6380); started via `just test-db` and ran pytest with `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` exported. Not a code issue — standard local test-DB setup.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- HYG-04 is now covered by committed regression tests at all three gate sites. This was the one genuine deliverable of Phase 75; the milestone (2026.7.2) is ready to close after this phase's PR.
- Full test file green (96 passed); all 4 force-local cases pass together; `git diff --stat -- src/` empty.

## Self-Check: PASSED
- FOUND: tests/shared/routers/test_pipeline.py (4 new force_local cases)
- FOUND: commit a01a7bf8 (Task 1)
- FOUND: commit 63589cd5 (Task 2)

---
*Phase: 75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking*
*Completed: 2026-07-06*
