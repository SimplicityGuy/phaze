---
phase: 74-docs-runbook-n-lane-compute-ui-verification
plan: 03
subsystem: backend-lane-snapshot (BEUI verification)
tags: [testing, mcomp-07, n-lane-ui, compute-parity, regression]
requires:
  - "get_backend_lane_snapshot + ComputeAgentBackend/LocalBackend (Phase 71 / Phase 72)"
  - "seed_active_agent(kind='compute') fixture (tests/_queue_fakes.py)"
  - "ComputeBackend config entry (phaze.config_backends)"
provides:
  - "Compute-parity lane regression tests (Variant A deterministic + Variant B real fan-out)"
  - "Arbiter verdict for Plan 04: the N>=2-compute shared-session probe race does NOT manifest"
affects:
  - "Plan 74-04 scope: verification/docstring-only (no _probe_availability serialization needed)"
tech-stack:
  added: []
  patterns:
    - "Verification test over existing production code (deterministic monkeypatched probe)"
    - "Real-fan-out arbiter test: two ONLINE compute agents seeded, no probe monkeypatch"
key-files:
  created: []
  modified:
    - "tests/shared/services/test_lane_snapshot.py"
decisions:
  - "Variant B (real fan-out, two online compute agents) is deterministically GREEN across 5 runs -> the R-2/Pitfall-1 shared-session race does NOT manifest; Plan 04 is verification/docstring-only."
metrics:
  duration: "~20 min"
  completed: "2026-07-06"
  tasks: 2
  files: 1
---

# Phase 74 Plan 03: N-Lane Compute Parity Regression Tests Summary

Two regression tests added to `tests/shared/services/test_lane_snapshot.py` that lock the Phase-71 BEUI
N-lane compute rendering (MCOMP-07, D-04) and settle the R-2/Pitfall-1 shared-session probe-race question:
a deterministic Variant A proving each of N≥2 compute backends renders its own lane (no `kind` dedup), and
a real-`_probe_availability`-fan-out Variant B — the arbiter — proving both online compute lanes come back
`available=True`.

## Variant B Result (ARBITER for Plan 04)

**Variant B PASSED — verification-only; Plan 04 does docstring-only (no probe serialization required).**

The real `asyncio.gather` fan-out over the single shared `AsyncSession`, with two ONLINE `kind="compute"`
agents whose ids match the two bound backends' `agent_ref`, returned `available == True` for BOTH compute
lanes. Run 5 additional times back-to-back — deterministically green every time. The N≥2-compute
shared-session concurrency race described in 74-RESEARCH Pitfall 1 (MEDIUM confidence, "the real-session
test is the arbiter") does **not** manifest in practice. Per the plan's gating rule, this means Plan 04 is
scoped to verification / docstring cleanup only (update the stale `_probe_availability` docstring that still
claims the retired "≤1 compute" invariant); it does NOT need to serialize the compute probes or give each its
own session.

## What Was Built

### Task 1 — Variant A: deterministic "one lane per compute backend" (commit `2b1fc277`)
- `test_snapshot_renders_one_lane_per_compute_backend`: two `ComputeAgentBackend` (`a1-arm64` rank 10 cap 2,
  `x86-spill` rank 20 cap 1) + one `LocalBackend` (rank 99), `resolve_backends` monkeypatched, and
  `_probe_availability` monkeypatched to an all-available shim.
- Asserts exactly 2 `kind=="compute"` lanes with ids `["a1-arm64","x86-spill"]` in rank order (no kind
  collapse), the local lane still present, and full order `["a1-arm64","x86-spill","local"]` sorted (rank, id).
- Green immediately — a verification test over existing correct behavior.

### Task 2 — Variant B: real probe fan-out arbiter (commit `d5d676fb`)
- `test_compute_probe_real_fanout_keeps_both_lanes_online` + `_bound_compute` helper (mirrors the
  `tests/analyze/services/test_backends.py::_compute` factory) building `ComputeAgentBackend` with a REAL
  bound `ComputeBackend` config (`agent_ref == id`, `scratch_dir`, `push_host`).
- Seeds TWO online compute agents via `seed_active_agent(session, id, kind="compute")` whose ids equal the
  backends' `agent_ref`; does NOT monkeypatch `_probe_availability`, so `ComputeAgentBackend.is_available` →
  `select_agent_by_id` → `session.execute` runs concurrently over the one shared session.
- Asserts both compute lanes `available == True`.

## Deviations from Plan

None - plan executed exactly as written. Both variants landed in `tests/shared/services/test_lane_snapshot.py`
(the plan-confirmed real home for `get_backend_lane_snapshot` coverage). Added imports:
`ComputeBackend as ComputeEntry` (phaze.config_backends) and `seed_active_agent` (tests._queue_fakes).

## Verification

- `uv run pytest tests/shared/services/test_lane_snapshot.py -k one_lane_per_compute -x` → 1 passed.
- `uv run pytest tests/shared/services/test_lane_snapshot.py -k compute_probe_real -x` → 1 passed (×6, deterministic).
- `uv run pytest tests/shared/services/test_lane_snapshot.py` → 17 passed.
- `uv run pytest tests/shared/services/` → 122 passed (no regressions).
- `uv run ruff check tests/shared/services/test_lane_snapshot.py` → clean.
- mypy: the enforced project hook excludes `tests/`; the 2 pre-existing protocol-stub `arg-type` notes on the
  older sibling tests are unchanged, and no error is introduced in the added region.

## Known Stubs

None. Both tests exercise real production code paths (Variant B hits the live probe fan-out end to end).

## Self-Check: PASSED

- `tests/shared/services/test_lane_snapshot.py` — FOUND (contains both `one_lane_per_compute` and `compute_probe_real`).
- Commit `2b1fc277` (Variant A) — FOUND.
- Commit `d5d676fb` (Variant B) — FOUND.
