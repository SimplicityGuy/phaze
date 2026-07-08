---
phase: 76-compute-push-hardening
plan: 01
subsystem: infra
tags: [backends, asyncio, sqlalchemy, async-session, compute-agent, probe, concurrency]

# Dependency graph
requires:
  - phase: 72-per-entry-compute-binding
    provides: N>=2 compute backends are legal (MCOMP-01 retired single-active-compute)
  - phase: 74-docs-runbook-n-lane-ui
    provides: Plan 74-03 Variant B arbiter test (empirical race probe) that this plan supersedes
provides:
  - Serialized services/backends._probe_availability (sequential await loop, no asyncio.gather)
  - Structural (not empirical) session-safety guarantee for the N-compute liveness probe
  - Deterministic N>=2-compute probe regression test replacing the Variant B arbiter form
affects: [complete-milestone, 2026.7.2, compute-push-hardening]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Serialize shared-AsyncSession fan-out (sequential await loop) instead of asyncio.gather to make concurrent-use safety a structural guarantee"

key-files:
  created: []
  modified:
    - src/phaze/services/backends.py
    - tests/shared/services/test_lane_snapshot.py

key-decisions:
  - "Serialized _probe_availability with a plain for-loop over sequential await _probe_one (D-01), chosen over per-probe sessionmaker sessions because N is tiny and it keeps the single request-scoped session"
  - "Kept _PROBE_TIMEOUT_SEC wait_for in _probe_one and the post-fan-out session.rollback in get_backend_lane_snapshot byte-unchanged (D-02)"
  - "Reworded _probe_availability docstring from empirical (Plan-74-03 arbiter) to a by-construction structural guarantee (D-03); dropped 'arbiter'/'in practice'/'empirical' phrasing"
  - "Retargeted the Variant B arbiter test to a single deterministic _probe_availability assertion over two seeded online compute agents (D-04)"

patterns-established:
  - "Race-hardening refactor: convert an empirically-observed concurrency claim into a structural guarantee by serializing the shared resource access, and retarget the arbiter test to a deterministic single assertion"

requirements-completed: [HARD-01]

# Metrics
duration: ~12min
completed: 2026-07-06
---

# Phase 76 Plan 01: N-compute liveness probe session-safety Summary

**Serialized `services/backends._probe_availability` (sequential `await _probe_one`, no `asyncio.gather`) so N>=2 compute probes never use the shared AsyncSession concurrently — closing WR-01 as a structural guarantee.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-07-06
- **Completed:** 2026-07-06
- **Tasks:** 2 (Task 1 code+test; Task 2 verification-only gate)
- **Files modified:** 2

## Accomplishments
- Replaced the `asyncio.gather` fan-out in `_probe_availability` with a sequential `for backend in backends` loop that awaits each `_probe_one` one at a time, so concurrent `session.execute` on the shared request-scoped `AsyncSession` is impossible by construction (the SQLAlchemy concurrent-use hazard the WR-01 review flagged).
- Reworded the `_probe_availability` docstring from the empirical Plan-74-03 arbiter framing ("proven race-free in practice") to a structural guarantee: probes run sequentially on the one session, so there is never concurrent use (Pitfall 1 resolved by construction).
- Superseded the Plan 74-03 Variant B arbiter test with a deterministic single-assertion regression test that calls the real `_probe_availability` over two seeded online compute agents and asserts `{"a1-arm64": True, "x86-spill": True}` once.
- Preserved the bounded per-probe `asyncio.wait_for(..., _PROBE_TIMEOUT_SEC)` in `_probe_one` and the post-fan-out `await session.rollback()` in `get_backend_lane_snapshot` byte-unchanged; no new dependencies (`pyproject.toml`/`uv.lock` untouched).

## Task Commits

Each task was committed atomically:

1. **Task 1: Serialize _probe_availability + reword docstring + deterministic N>=2 regression test** - `38e68d8f` (fix)
2. **Task 2: Quality gate (ruff, mypy, docs-drift, targeted suite)** - no commit (verification-only; the gate surfaced no findings, working tree clean)

## Files Created/Modified
- `src/phaze/services/backends.py` - `_probe_availability` now serial (no `asyncio.gather`); docstring rewritten as a structural session-safety guarantee.
- `tests/shared/services/test_lane_snapshot.py` - `test_compute_probe_real_fanout_keeps_both_lanes_online` retargeted to a deterministic single `_probe_availability` assertion over two online compute agents (no `get_backend_lane_snapshot`/`resolve_backends` monkeypatch needed).

## Decisions Made
- None beyond the plan's D-01..D-04, which were followed as specified. TDD note: this is a race-hardening structural refactor, so a true RED phase is not possible (the concurrency hazard did not manifest empirically per Plan 74-03); Task 1 landed as one atomic `fix` commit (the plan frontmatter is `type: execute`, not `type: tdd`, so no plan-level RED/GREEN gate applies).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None. All quality gates passed on the first run: `ruff check` and `ruff format --check` on both files (clean), `mypy src/phaze/services/backends.py` (Success), `just docs-drift` (10 passed, green), and `pytest tests/shared/services/test_lane_snapshot.py` (17 passed). `git diff` confirms `pyproject.toml`/`uv.lock` unchanged.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- HARD-01 complete; the N-compute probe fan-out is structurally session-safe. Ready for HARD-02 (ledger RMW atomicity) and HARD-03 (agent_id boundary validation) as independent-wave siblings.
- No blockers. `just docs-drift` stays green; coverage gate unaffected (only a probe serialization + test retarget).

## Self-Check: PASSED
- `src/phaze/services/backends.py` — FOUND (modified, committed in 38e68d8f)
- `tests/shared/services/test_lane_snapshot.py` — FOUND (modified, committed in 38e68d8f)
- Commit `38e68d8f` — FOUND in git log
- `asyncio.gather` absent from `backends.py` — CONFIRMED
- Targeted suite `tests/shared/services/test_lane_snapshot.py` — 17 passed

---
*Phase: 76-compute-push-hardening*
*Completed: 2026-07-06*
