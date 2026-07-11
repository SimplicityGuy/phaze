---
phase: 82-counts-pending-set-cutover
plan: 01
subsystem: stage-status-predicates
tags: [sql-twin, eligibility, drift-lock, enrich-stages, tdd]
requires:
  - "phaze.enums.stage.eligible / ELIGIBLE_AFTER_FAILURE (Python truth, Phase 78)"
  - "phaze.services.stage_status.{inflight_clause,done_clause,failed_clause} (LOCKED builders, Phase 78)"
  - "tests/integration/test_stage_status_equivalence.py DERIV-04 harness (Phase 78)"
provides:
  - "eligible_clause(stage) — SQL twin of eligible() for the three enrich stages (READ-01 foundation)"
  - "ELIGIBLE_CASES matrix + test_eligible_sql_equals_python (SQL==Python==expected drift-lock)"
affects:
  - "Plan 82-02 pending-set cutovers (get_metadata_pending_files / get_fingerprint_pending_files / get_discovered_files_with_duration compose eligible_clause)"
tech-stack:
  added: []
  patterns:
    - "Table-driven per-stage carve-out off ELIGIBLE_AFTER_FAILURE (never inline stage identity)"
    - "Enrich-only ValueError guard mirroring domain_completed_clause"
    - "Correlated ~exists composition of LOCKED sibling clauses (DERIV-04)"
key-files:
  created: []
  modified:
    - "src/phaze/services/stage_status.py"
    - "tests/integration/test_stage_status_equivalence.py"
decisions:
  - "eligible_clause signature stays a single `stage` param — has_approved_proposal is APPLY-only, irrelevant to the enrich-only builder"
  - "Added `not_` to the sqlalchemy import (the plan's interfaces block claimed it was already present; it was not) — Rule 3 blocking-issue fix"
metrics:
  tasks_completed: 2
  files_modified: 2
  completed: 2026-07-10
requirements: [READ-01]
---

# Phase 82 Plan 01: eligible_clause SQL Twin + Drift-Lock Summary

Added `eligible_clause(stage)` to `services/stage_status.py` — the SQL twin of the pure-Python
`enums.stage.eligible()`, defined only for the three enrich stages and driven table-first off
`ELIGIBLE_AFTER_FAILURE` — and drift-locked it against the Python truth by extending Phase-78's
DERIV-04 equivalence harness with an additive 14-cell `ELIGIBLE_CASES` matrix asserting
`sql_eligible == py_eligible == expected` for every enrich fixture.

## What Was Built

- **Task 1 (RED, `test`):** Extended `tests/integration/test_stage_status_equivalence.py` with a
  third parametrized matrix `ELIGIBLE_CASES` (14 enrich cells), `eval_sql_eligible` (lazy-imports
  `eligible_clause` so `pytest --co` stays green in the RED state), and
  `test_eligible_sql_equals_python`. Mirrors the existing `DOMAIN_COMPLETED_CASES` structure
  verbatim; reuses every existing `seed_*` fixture — no new fixtures. Confirmed RED at execution
  (ImportError on `eligible_clause`) while collection stayed green.
- **Task 2 (GREEN, `feat`):** Implemented `eligible_clause(stage) -> ColumnElement[bool]` next to
  `domain_completed_clause`. Base conjunction `~inflight_clause(stage) ∧ ~done_clause(stage)`
  (the SQL image of `status not in (DONE, IN_FLIGHT)` under the CASE precedence
  `in_flight ≻ done ≻ failed ≻ not_started`); appends `~failed_clause(stage)` **only** when
  `not ELIGIBLE_AFTER_FAILURE[stage]` (analyze — the ELIG-03 terminal-failed carve-out). Enrich-only
  `ValueError` guard mirrors `domain_completed_clause`'s message shape and raw-`str` handling.

## Verification

- `uv run pytest tests/integration/test_stage_status_equivalence.py -q` → **50 passed** (all three
  matrices: `CASES`, `DOMAIN_COMPLETED_CASES`, `ELIGIBLE_CASES`), run against an isolated ephemeral
  Postgres 18 (`localhost:5455`).
- `eligible_clause(Stage.PROPOSE/REVIEW/APPLY/TRACKLIST)` each raise `ValueError` (enrich-only guard).
- `uv run ruff check` + `uv run mypy` on `src/phaze/services/stage_status.py` → clean.
- Pre-commit hooks (incl. mypy) passed on both commits — no `--no-verify`.

## ELIG-03 Mutation Check (recorded per plan success criteria)

Temporarily forced the analyze failure conjunct never to append (`if False:` in place of
`if not ELIGIBLE_AFTER_FAILURE[stage]:`) and re-ran the load-bearing cell:

- **Mutated:** `test_eligible_sql_equals_python[analyze-seed_analysis_failed-False]` → **RED (1 failed)**
  — a FAILED analyze becomes eligible, the exact 44.5K over-enqueue class the guard exists to prevent.
- **Restored:** same cell → **GREEN (1 passed)**.

The guard has teeth: dropping the analyze `~failed_clause` conjunct is caught by the
`(Stage.ANALYZE, seed_analysis_failed, False)` cell.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added `not_` to the sqlalchemy import**
- **Found during:** Task 2
- **Issue:** The plan's `<interfaces>` block stated the module "already imports: and_, not_, exists,
  false". In fact `not_` was absent from the `from sqlalchemy import ...` line, so the proposed
  `not_(inflight_clause(stage))` body would not import.
- **Fix:** Added `not_` to the existing sqlalchemy import line (kept alphabetical position).
- **Files modified:** src/phaze/services/stage_status.py
- **Commit:** 30c508c2

## Notes

- **Shadow-compare gate:** unaffected by construction — this plan is purely additive and wires no
  reader (`eligible_clause` is not yet consumed by any query). Not executed here because the standing
  gate probes the live production DB (read-only) which is off-limits from the executor environment;
  it is definitionally green since no reader changed. Plan 82-02 wires the first consumers.
- **Test-isolation note (environment, not a code issue):** the shared `just test-db` container
  (port 5433) was concurrently in use by sibling wave agents and carried a persisted
  `agents.legacy-application-server` row, producing `UniqueViolationError` at fixture setup for some
  cells. Verified instead against a private ephemeral Postgres on port 5455 (removed after), which
  gave a clean 50-passed run. No product code is implicated — the collisions are the harness's
  committed FK-agent seed racing a polluted shared DB.

## Self-Check: PASSED

- FOUND: src/phaze/services/stage_status.py (`def eligible_clause`)
- FOUND: tests/integration/test_stage_status_equivalence.py (`ELIGIBLE_CASES`)
- FOUND commit 5dcf28ca (test: RED ELIGIBLE_CASES)
- FOUND commit 30c508c2 (feat: eligible_clause implementation)
