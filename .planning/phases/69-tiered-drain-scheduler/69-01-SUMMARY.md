---
phase: 69-tiered-drain-scheduler
plan: 01
subsystem: scheduler
tags: [backend-selection, routing-policy, config, tdd, pure-function]
requires:
  - "src/phaze/services/backends.py Backend protocol + LocalBackend (Phase 68)"
  - "src/phaze/config.py ControlSettings.cloud_submit_max_attempts (Phase 54)"
provides:
  - "src/phaze/services/backend_selection.py select_backend pure policy"
  - "src/phaze/services/backend_selection.py BackendSlot TypedDict (Plan 02 imports it)"
  - "src/phaze/config.py cloud_spill_to_local_after_seconds knob (D-02)"
affects:
  - "Plan 02 drain (release_awaiting_cloud.stage_cloud_window) will consume select_backend + BackendSlot"
tech-stack:
  added: []
  patterns:
    - "pure synchronous fully-typed decision function (reenqueue.py is_domain_completed idiom)"
    - "bounded pydantic int Field mirroring cloud_route_threshold_sec"
    - "TDD RED->GREEN (no refactor needed)"
key-files:
  created:
    - "src/phaze/services/backend_selection.py"
    - "tests/analyze/services/test_backend_selection.py"
    - "tests/shared/config/test_cloud_spill_to_local.py"
  modified:
    - "src/phaze/config.py"
decisions:
  - "D-02: cloud_spill_to_local_after_seconds default 900 (15 min), bounded gt=0/lt=86400"
  - "Signature deviates from RESEARCH pseudocode: cloud_attempts passed explicitly (lives on cloud_job, not FileRecord)"
  - "Local detection via isinstance(LocalBackend), never a rank-99 literal (RESEARCH Open Q4)"
  - "Reading 1 (total-cloud attempt budget) — cloud_attempts is the single per-file cloud_job counter"
metrics:
  duration: "~15 min"
  completed: "2026-07-04"
  tasks: 2
  files_created: 3
  files_modified: 1
---

# Phase 69 Plan 01: Backend Selection Policy Summary

Pure, mypy-strict `select_backend` decision function encoding rank-first eligible dispatch (SCHED-01), the D-01/D-03 staleness gate on local spill, D-04 attempt-exclusion, D-06 stateless re-rank, and the SCHED-04 utilization+id tie-break — plus the one new bounded `cloud_spill_to_local_after_seconds` config knob it consumes. No I/O; the Plan-02 drain supplies a once-per-tick snapshot and each candidate's cloud attempt count.

## What Was Built

### Task 1 — `cloud_spill_to_local_after_seconds` config knob (D-02)
- Added a bounded int `Field` to `ControlSettings` in `src/phaze/config.py`, placed next to `cloud_submit_max_attempts`, mirroring `cloud_route_threshold_sec` verbatim: `default=900`, `gt=0`, `lt=86400`, `validation_alias=AliasChoices("PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS", "cloud_spill_to_local_after_seconds")`.
- New pure config test `tests/shared/config/test_cloud_spill_to_local.py`: default is 900, env alias overrides, values 0 and 86400 both raise `ValidationError` (fail-fast bounds — T-69-01-01 mitigation).
- Confirmed `cloud_max_in_flight` does not reappear (D-05 retirement intact).
- Commit `3ed961f`.

### Task 2 — pure `select_backend` policy (SCHED-01/03/04), TDD
- **RED** (`f4eb02b`): 13-case unit suite in `tests/analyze/services/test_backend_selection.py` covering rank-first, spill-when-full, D-03 offline→local-immediate, D-01 full→local staleness gate (before/after threshold), D-04 attempt-exclusion, D-06 stateless re-rank (+ signature-has-no-history assertion), SCHED-04 tie-break by utilization then stable id, and two hold=None cases. Failed on `ModuleNotFoundError` (module absent) — clean RED.
- **GREEN** (`3fc8e63`): `src/phaze/services/backend_selection.py` — `BackendSlot` TypedDict (exported for Plan 02) + the pure `select_backend(file, cloud_attempts, snapshot, now, cfg) -> Backend | None`. Steps: (1) eligible = available AND remaining>0; (2) D-04 — if `cloud_attempts >= cfg.cloud_submit_max_attempts`, restrict to local-only; (3) D-01/D-03 staleness gate keyed off `any_non_local_online` (online-vs-full distinction) and `now - file.updated_at`; (4) hold→None; (5) sort by `(rank, utilization, id)`. Local detected via `isinstance(..., LocalBackend)` at a runtime import (no cycle — `backends.py` does not import this module). Never raises.
- All 13 tests pass; `-k stale`, `-k attempt`, `-k tiebreak` each bind ≥1 test.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 — Missing critical functionality] cap≤0 division guard in tie-break**
- **Found during:** Task 2 GREEN.
- **Issue:** The RESEARCH pseudocode tie-break computes `(cap - remaining) / cap` directly; a `LocalBackend` constructed with `cap=0` (the `_local()` test-helper default) would raise `ZeroDivisionError` mid-sort — a latent crash the drain could hit.
- **Fix:** Extracted `_utilization(slot)` returning `0.0` when `cap <= 0`, otherwise `(cap - remaining) / cap`. Keeps the pure function's never-raise contract (a hold, never a crash).
- **Files modified:** `src/phaze/services/backend_selection.py`.
- **Commit:** `3fc8e63`.

Signature deviation (`cloud_attempts` passed explicitly rather than `file.cloud_attempts`) was pre-specified in the plan, not a deviation. No architectural changes; no auth gates.

## Verification

- `uv run pytest tests/analyze/services/test_backend_selection.py tests/shared/config/test_cloud_spill_to_local.py` — 17 passed.
- `uv run mypy src/phaze/config.py src/phaze/services/backend_selection.py` — clean.
- `pre-commit run --files <4 plan files>` — all hooks pass (ruff, ruff-format, bandit, mypy).
- Acceptance guards: `grep "== 99"` → none; no `await`/`async` in the module (pure sync); `-k` filters bind.

## TDD Gate Compliance

RED (`test(69-01)` `f4eb02b`) precedes GREEN (`feat(69-01)` `3fc8e63`); RED failed for the right reason (module absent, not an assertion). No REFACTOR commit — the GREEN implementation needed no cleanup.

## Known Stubs

None. `select_backend` is fully implemented and exhaustively unit-tested; the config knob is live and bounded. Plan 02 wires `select_backend` + `BackendSlot` into the drain tick.

## For Plan 02

- Import `from phaze.services.backend_selection import BackendSlot, select_backend`.
- Build `snapshot: dict[str, BackendSlot]` once per tick (probe `is_available()` / `in_flight_count()` once; `remaining = cap - in_flight`). For the local slot, supply a positive `remaining` (local headroom) — the policy treats local under the same `remaining > 0` filter as every other backend.
- Pass each candidate's `cloud_attempts` (from its `cloud_job` row) and `saq_now()`; a `None` return is the clean-hold path (file stays `AWAITING_CLOUD`).

## Self-Check: PASSED
