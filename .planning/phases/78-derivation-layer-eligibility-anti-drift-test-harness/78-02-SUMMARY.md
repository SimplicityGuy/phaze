---
phase: 78-derivation-layer-eligibility-anti-drift-test-harness
plan: 02
subsystem: derivation-layer
tags: [sql-derivation, anti-drift, in-flight, scheduling-ledger, savepoint, additive]
requires:
  - "phaze.enums.stage.resolve_status (Wave-1 / plan 78-01) — the Python twin locked against"
  - "phaze.tasks._shared.stage_control.STAGE_TO_FUNCTION — the ledger-key function names"
  - "phaze.models.scheduling_ledger.SchedulingLedger — the durable in_flight source (D-01)"
provides:
  - "phaze.services.stage_status.done_clause/failed_clause/inflight_clause — per-stage ColumnElement[bool] builders"
  - "phaze.services.stage_status.stage_status_case — the 4-way status CASE ladder (SQL twin)"
  - "phaze.services.stage_status.saq_detail — SAVEPOINT-isolated corroborating saq_jobs read"
  - "DERIV-04 SQL⇔Python equivalence matrix (the standing anti-drift lock)"
affects:
  - "No existing reader/writer wired this phase (PURELY ADDITIVE); cutover is Phase 82+"
tech-stack:
  added: []
  patterns:
    - "Correlated ~exists(...) anti-joins (never LEFT-JOIN-null / NOT IN)"
    - "begin_nested() SAVEPOINT degrade idiom copied from pipeline.py:488-499"
    - "case((inflight, ...), (done, ...), (failed, ...), else_=not_started) precedence ladder"
key-files:
  created:
    - src/phaze/services/stage_status.py
    - tests/integration/test_stage_status_equivalence.py
  modified: []
decisions:
  - "D-01 written record persisted in stage_status.py module docstring: scheduling_ledger is authoritative for in_flight; saq_jobs corroborating-only; union and ledger-alone alternatives rejected"
  - "Presence semantics for propose/review (done = a proposal exists) mirror the Wave-1 Python twin to satisfy DERIV-04 equivalence — a failed-only proposal still derives done"
  - "in_flight scoped to the 3 file-keyed enrich stages; downstream + batch-keyed propose return false() (Pitfall 5 / OQ1)"
metrics:
  tasks: 2
  files_created: 2
  files_modified: 0
  tests_added: 25
  module_coverage: "100%"
  completed: 2026-07-08
---

# Phase 78 Plan 02: SQL Derivation Layer + Anti-Drift Equivalence Lock Summary

Shipped `services/stage_status.py` — the SQLAlchemy `ColumnElement` half of the single-source
per-stage predicate layer — and locked it against the Wave-1 DB-free Python resolver with a
25-cell DERIV-04 SQL⇔Python equivalence matrix (the anti-drift guarantee), deriving `in_flight`
authoritatively from the durable `scheduling_ledger` and reading `saq_jobs` only as a
SAVEPOINT-isolated corroborating detail.

## What Was Built

- **`src/phaze/services/stage_status.py`** — `done_clause(stage)` / `failed_clause(stage)` /
  `inflight_clause(stage)` returning `ColumnElement[bool]` via correlated `exists(...)`/`~exists(...)`,
  composed by `stage_status_case(stage)` into the precedence ladder `in_flight ≻ done ≻ failed ≻
  not_started`. Per-stage predicates: analyze done = `analysis_completed_at IS NOT NULL` (DERIV-03);
  metadata done = row present AND `failed_at IS NULL` (D-03); fingerprint done =
  `status.in_(('success','completed'))` any engine, renders `= ANY (ARRAY[...])` (DERIV-05); apply
  done = `execution_log` completed joined through `proposals` (execution_log has no `file_id`).
  `inflight_clause` matches a `scheduling_ledger` row on the deterministic
  `func.concat('<function>:', cast(FileRecord.id, String))` key (STAGE_TO_FUNCTION imported, never
  re-spelled). `saq_detail(session)` runs static `text()` SQL inside `session.begin_nested()` and
  degrades to `{queued:0, active:0}` on ANY error. The module docstring is the written D-01 decision
  record (INFLIGHT-03 / SC#5).
- **`tests/integration/test_stage_status_equivalence.py`** — real-PG parametrized matrix
  (`sql_status == py_status == expected` for every stage × status + edge cells), a dedicated
  ELIG-04 "failed fingerprint stays eligible" test, and the INFLIGHT-02 `savepoint_degrade` test
  (drop `saq_jobs` mid-test → `saq_detail` safe default, no raise, `in_flight` still True from the
  ledger). `stage_status` import is deferred into runtime helpers so `pytest --co` collects the
  matrix in the TDD RED state.

## TDD Gate Compliance

- RED gate: `test(78-02): ...` commit `6b49d354` — matrix authored, collects (25 tests), RED at
  runtime (deferred import fails) while PG connected (not skipped).
- GREEN gate: `feat(78-02): ...` commit `7af6aa9b` — builders implemented, all 25 tests pass.
- No REFACTOR commit needed (module clean on first GREEN).

## Verification

- `uv run pytest tests/integration/test_stage_status_equivalence.py -x` → 25 passed (real PG :5433).
- `just test-bucket integration` in isolation → 96 passed (no cross-test regression).
- `tests/shared/test_partition_guard.py` → 3 passed (new test correctly in the `integration` bucket).
- `uv run mypy src/phaze/services/stage_status.py` clean; `uv run ruff check` + `ruff format` clean.
- Module coverage: **100%** (62/62 lines) — exceeds the per-module 90 floor.
- `grep -nE "LEFT JOIN|not_in\(" src/phaze/services/stage_status.py` → empty (anti-joins are `~exists` only).
- Alembic unchanged w.r.t. `saq_jobs` (this plan adds no migration).
- D-01 decision record present in the module docstring (`authoritative.*ledger` grep matches).

## Deviations from Plan

None — plan executed exactly as written. The two docstring rewordings (avoiding the literal
`LEFT JOIN` string so the anti-join acceptance grep stays empty, and replacing the `∪` glyph flagged
by ruff RUF002) are lint/acceptance hygiene, not behavioral deviations.

## Known Stubs

None. Every builder is fully wired to real ORM columns and proven against real Postgres. The module
is intentionally not consumed by any existing reader/writer this phase (PURELY ADDITIVE — cutover is
Phase 82+ behind the shadow-compare gate), which is the documented plan scope, not a stub.

## Self-Check: PASSED

- FOUND: `src/phaze/services/stage_status.py`
- FOUND: `tests/integration/test_stage_status_equivalence.py`
- FOUND commit: `6b49d354` (test / RED)
- FOUND commit: `7af6aa9b` (feat / GREEN)
