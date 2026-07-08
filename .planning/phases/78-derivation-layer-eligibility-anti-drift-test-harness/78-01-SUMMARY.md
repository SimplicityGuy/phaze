---
phase: 78-derivation-layer-eligibility-anti-drift-test-harness
plan: 01
subsystem: derivation
tags: [enums, strenum, stage-status, eligibility-dag, db-free, agent-boundary, deriv, elig]

# Dependency graph
requires:
  - phase: 77-stage-failure-markers-partial-indexes
    provides: "nullable failed_at markers on analysis/metadata + partial indexes the resolver semantics mirror"
  - phase: 26-agent-import-boundary
    provides: "the DB-free StrEnum + agent import-boundary pattern (enums/execution.py) copied here"
provides:
  - "src/phaze/enums/stage.py — DB-free Stage/Status StrEnums, ELIGIBILITY_DAG topology, resolve_status() precedence ladder, eligible() pure predicate"
  - "The CONTRACT the Wave-2 SQL twin (services/stage_status.py, plan 78-02) is locked against by the DERIV-04 equivalence test"
  - "tests/shared/test_stage_resolver.py + tests/shared/test_stage_eligibility_dag.py — DB-free unit proofs of DERIV-02/03/05, D-03, ELIG-01/02/03/04"
affects: [78-02-stage-status-sql-twin, stage-status-equivalence-test, phase-79-linear-filestate-cutover]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "DB-free StrEnum + agent import boundary (T-78-01): stdlib-only module imported inside the Postgres-free agent worker; subprocess banned-import guard"
    - "Pure per-row status resolver over plain scalars — no DB round-trip (D-04 boundary)"
    - "Precedence ladder in_flight > done > failed > not_started encoded per-stage twin"

key-files:
  created:
    - "src/phaze/enums/stage.py"
    - "tests/shared/test_stage_resolver.py"
    - "tests/shared/test_stage_eligibility_dag.py"
  modified: []

key-decisions:
  - "D-04: shipped the DB-free half only (enums + DAG + Python resolver over scalars); the SQLAlchemy twin is plan 78-02"
  - "eligible() dispatches per-stage — enrich stages do NOT share one uniform rule: metadata/fingerprint eligible when NOT_STARTED|FAILED, analyze eligible only when NOT_STARTED (ELIG-03 terminal)"
  - "apply gated on has_approved_proposal flag supplied by caller, NOT on bare done(review) (ELIG-02)"
  - "type-only Mapping placed under TYPE_CHECKING to satisfy ruff TC003 — safe here because the module has no runtime annotation resolution (no Pydantic/SQLAlchemy)"

patterns-established:
  - "Downstream presence twins funnel through a shared _presence_status ladder so every stage can express all 4 statuses uniformly"
  - "Non-vacuous ELIG-04 proof: FAILED status is first DERIVED via resolve_status(engine_statuses=['failed']) before asserting eligibility, not hand-stubbed"

requirements-completed: [DERIV-01, DERIV-02, DERIV-03, DERIV-05, ELIG-01, ELIG-02, ELIG-03, ELIG-04]

# Metrics
duration: ~20min
completed: 2026-07-08
---

# Phase 78 Plan 01: Derivation Layer (DB-free half) Summary

**DB-free `enums/stage.py` shipping Stage/Status StrEnums, the ELIGIBILITY_DAG topology, a pure per-row `resolve_status()` precedence ladder (in_flight > done > failed > not_started), and a pure `eligible()` predicate — the agent-safe contract the Wave-2 SQL twin is locked against.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-08T09:50:05-07:00 (first task commit)
- **Completed:** 2026-07-08T09:54:36-07:00 (last task commit)
- **Tasks:** 2 (both TDD: RED → GREEN)
- **Files modified:** 3 created

## Accomplishments
- `src/phaze/enums/stage.py` (193 lines): stdlib-only module — `Stage` (7 members) / `Status` (4 members) StrEnums, `ELIGIBILITY_DAG`, `resolve_status()`, `eligible()`. Zero `phaze.models`/`phaze.database`/`sqlalchemy` in its import graph (T-78-01 agent boundary), enforced by a subprocess banned-import test.
- `resolve_status()` encodes the DERIV-02 precedence ladder per-stage: analyze done gated on `completed_at IS NOT NULL` (DERIV-03, a partial NULL row is NOT done); metadata done requires row present AND `failed_at IS NULL` (D-03, failure-only row → FAILED); fingerprint 1:N aggregation where one `success`/`completed` engine beats a `failed` sibling (DERIV-05).
- `eligible()` pure predicate: enrich stages have no upstream (a discovered file is simultaneously eligible for metadata/fingerprint/analyze); metadata/fingerprint stay eligible while FAILED (ELIG-04 auto-retry); analyze FAILED is terminal (ELIG-03, the 44.5K over-enqueue guard); downstream stages gate on DAG upstream conjuncts; apply gates on an APPROVED proposal, not bare done(review) (ELIG-02).
- 44 DB-free unit tests, 100% coverage on `enums/stage.py`.

## Task Commits

Each task was executed TDD (RED test → GREEN implementation):

1. **Task 1 RED: failing resolver tests** - `db933a26` (test)
2. **Task 1 GREEN: Stage/Status enums + resolve_status** - `f128f914` (feat)
3. **Task 2 RED: failing ELIGIBILITY_DAG + eligible() tests** - `74d3ed2d` (test)
4. **Task 2 GREEN: eligible() predicate** - `93880f3d` (feat)

_Note: `ELIGIBILITY_DAG` was authored in the Task-1 GREEN commit (it is a shared module constant `eligible()` consumes); `eligible()` itself landed in Task-2 GREEN._

## Files Created/Modified
- `src/phaze/enums/stage.py` - DB-free Stage/Status enums, ELIGIBILITY_DAG, resolve_status() precedence ladder, eligible() pure predicate
- `tests/shared/test_stage_resolver.py` - DB-free resolver + precedence + DERIV-05 + D-03 unit cells + subprocess DB-free import guard
- `tests/shared/test_stage_eligibility_dag.py` - DAG topology + eligible() conjuncts + ELIG-03 terminal-failed-analyze regression + non-vacuous ELIG-04 failed-fingerprint + ELIG-02 approved-vs-pending apply cells

## Decisions Made
- **eligible() is dispatched per-stage, not a uniform rule.** The three enrich stages diverge: metadata/fingerprint are eligible while NOT_STARTED or FAILED (retryable), analyze is eligible only while NOT_STARTED (FAILED analyze is terminal — mirrors `reenqueue._select_done_analyze_ids`). Documented inline with a citation (not an import — kept DB-free).
- **Downstream presence twins share `_presence_status`.** Keeps the ladder DRY while still exposing named `_tracklist_status`/`_propose_status`/`_review_status`/`_apply_status` twins and letting every stage express all 4 statuses uniformly.
- **`Mapping` under `TYPE_CHECKING`.** ruff TC003 flagged the type-only stdlib import; moving it into a `TYPE_CHECKING` block is safe here (pure module, no runtime `get_type_hints`), unlike the Pydantic/SQLAlchemy modules the project keeps on `py313` for.

## Deviations from Plan

None - plan executed exactly as written. (Two minor ruff-driven adjustments were made inside the tasks, not scope deviations: replaced an ambiguous `×` glyph in a test comment with `x` per RUF003, and placed the type-only `Mapping` import under `TYPE_CHECKING` per TC003.)

## Issues Encountered
- **`just test-bucket shared` is not green in this worktree — but for an unrelated, pre-existing reason.** The run reported `606 passed, 1 failed, 324 errors`. Every error/failure is a DB-connection failure at fixture setup in `tests/shared/services/test_pipeline.py` and `tests/shared/services/test_pipeline_counts.py` (confirmed: `sqlalchemy ... pool.connect()` raising because no live Postgres is reachable in this worktree). This is the documented "Local full-suite colima flake" — those service tests require a live Postgres normally supplied in CI. It is NOT caused by this plan's purely-additive, DB-free changes. Per the scope boundary rule these unrelated failures were left untouched. This plan's own two test files (44 tests) pass in isolation with 100% module coverage, and `uv run ruff check .` / `uv run mypy src/phaze/enums/stage.py` are clean.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The DB-free contract (`Stage`, `Status`, `ELIGIBILITY_DAG`, `resolve_status`, `eligible`) is ready for plan 78-02 to build the SQLAlchemy twin `services/stage_status.py` against, and for the DERIV-04 SQL⇔Python equivalence test to lock the two halves together.
- No cutover happened (purely additive alongside the existing linear `FileState`); the reader/writer cutover is Phase 79+.

---
*Phase: 78-derivation-layer-eligibility-anti-drift-test-harness*
*Completed: 2026-07-08*
