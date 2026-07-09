---
phase: 81-per-stage-failure-persistence-retry-paths
plan: 01
subsystem: pipeline
tags: [derived-status, eligibility, terminality, sqlalchemy, drift-lock, db-free]

# Dependency graph
requires:
  - phase: 78-derived-status-predicate-layer
    provides: DB-free enums/stage.py resolver + eligible() + ELIGIBILITY_DAG; services/stage_status.py done_clause/failed_clause SQL twins + the DERIV-04 equivalence test
  - phase: 79-shadow-compare-gate
    provides: standing shadow-compare gate that stays green (no derived-status change here)
provides:
  - FAILURE_IS_TERMINAL + ELIGIBLE_AFTER_FAILURE DB-free terminality/eligibility tables (two orthogonal axes)
  - Pure domain_completed(status_map, stage) helper in enums/stage.py
  - domain_completed_clause(stage) SQL twin in services/stage_status.py
  - Semantics-preserving eligible() refactor consuming ELIGIBLE_AFTER_FAILURE
  - Extended equivalence test drift-locking the domain_completed Python<->SQL twins
affects: [80-recovery-reenqueue, 82-counts-pending-cutover]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Two named per-stage tables encode orthogonal axes (recovery terminality vs auto-retry eligibility) so two readers never re-derive the analyze/fingerprint asymmetry independently"
    - "Python domain-predicate + SQL ColumnElement twin shipped in the SAME phase and drift-locked by a parametrized equivalence test (closes the Phase-78 D-04 window)"

key-files:
  created:
    - .planning/phases/81-per-stage-failure-persistence-retry-paths/deferred-items.md
  modified:
    - src/phaze/enums/stage.py
    - src/phaze/services/stage_status.py
    - tests/integration/test_stage_status_equivalence.py

key-decisions:
  - "domain_completed / domain_completed_clause are LEDGER-AGNOSTIC by design (no inflight disjunct); in_flight precedence stays layered at resolve/eligible. Equivalence cells therefore exclude the *_inflight seeds."
  - "FAILURE_IS_TERMINAL kept 3-key (enrich stages only) per D-15; domain_completed_clause is scoped to enrich stages, matching the recovery caller."
  - "eligible()'s three enrich branches collapsed to one ELIGIBLE_AFTER_FAILURE-driven expression rather than adding a fourth table lookup per branch."

patterns-established:
  - "Terminality vs eligibility are two tables, never one: FAILURE_IS_TERMINAL (recovery) + ELIGIBLE_AFTER_FAILURE (retry)"

requirements-completed: [FAIL-01, FAIL-04]

# Metrics
duration: ~35min
completed: 2026-07-09
---

# Phase 81 Plan 01: DB-Free Terminality/Eligibility Tables + domain_completed Twin Summary

**Two orthogonal DB-free tables (FAILURE_IS_TERMINAL for recovery, ELIGIBLE_AFTER_FAILURE for retry) plus a pure `domain_completed` helper and its `domain_completed_clause` SQL twin, drift-locked by the Phase-78 equivalence test, with a semantics-preserving `eligible()` refactor.**

## Performance

- **Duration:** ~35 min (dominated by two ~8-min full `shared`-bucket runs)
- **Started:** 2026-07-09T04:57Z
- **Completed:** 2026-07-09T05:21Z
- **Tasks:** 3
- **Files modified:** 3 (+1 created)

## Accomplishments
- Created `FAILURE_IS_TERMINAL={ANALYZE:True, METADATA:True, FINGERPRINT:False}` and `ELIGIBLE_AFTER_FAILURE={ANALYZE:False, METADATA:True, FINGERPRINT:True}` in the DB-free `enums/stage.py` — `FAILURE_IS_TERMINAL` existed in no `.py` before (D-14); Phase 80 now has a real owner for it.
- Added the pure `domain_completed(status_map, stage)` helper (Python twin, D-17) and its `domain_completed_clause(stage)` SQL twin, built from the LOCKED `done_clause`/`failed_clause` predicates (no new CASE).
- Refactored `eligible()`'s three enrich branches into one `ELIGIBLE_AFTER_FAILURE`-driven expression (D-16) — ELIG-01..04 pass unchanged; FAILED metadata/fingerprint stay eligible, FAILED analyze stays terminal (44.5K over-enqueue guard).
- Extended the equivalence test with 11 `domain_completed` cells so the Python table and SQL twin can never drift; encodes FAIL-01 (analyze terminal) and FAIL-04 (fingerprint auto-retryable).
- Kept `enums/stage.py` stdlib-only / DB-free (0 sqlalchemy/phaze.models imports).

## Task Commits

Each task was committed atomically:

1. **Task 1: tables + domain_completed() + eligible() refactor (enums/stage.py)** - `f7373b15` (feat)
2. **Task 2: domain_completed_clause() SQL twin (services/stage_status.py)** - `da970ba1` (feat)
3. **Task 3: domain_completed equivalence cells (test) + deferred-items log** - `dbaf8bcc` (test)

## Files Created/Modified
- `src/phaze/enums/stage.py` - Two terminality/eligibility tables, pure `domain_completed()`, collapsed enrich-stage `eligible()` branch.
- `src/phaze/services/stage_status.py` - `domain_completed_clause()` SQL twin (`or_(done_clause, failed_clause)` terminal / `done_clause` fingerprint); imports `FAILURE_IS_TERMINAL`, adds `or_`.
- `tests/integration/test_stage_status_equivalence.py` - `DOMAIN_COMPLETED_CASES` + `test_domain_completed_sql_equals_python` (Python<->SQL drift-lock).
- `.planning/phases/81-.../deferred-items.md` - Logged the unrelated migration-019 full-suite isolation flake.

## Decisions Made
- **Ledger-agnostic twins:** `domain_completed`/`domain_completed_clause` intentionally do NOT model in_flight (that precedence lives at the resolve/eligible layer). The equivalence cells therefore cover only non-in-flight rows; verified Python==SQL for every done/failed/not_started enrich cell. Documented inline.
- **3-key terminality table:** `FAILURE_IS_TERMINAL` stays scoped to the three enrich stages (D-15); `domain_completed_clause` is likewise enrich-scoped, matching the Phase-80 recovery caller. No over-generalization to downstream stages.
- **Collapse over four-table lookups:** the enrich `eligible()` branch reads one `ELIGIBLE_AFTER_FAILURE[stage]`, preserving exact prior truth (ANALYZE eligible iff NOT_STARTED; METADATA/FINGERPRINT iff NOT_STARTED or FAILED).

## Deviations from Plan

None - plan executed exactly as written. (The `domain_completed` cell set was scoped to non-in-flight enrich rows to keep the Python<->SQL equivalence sound — this is the semantically-correct reading of the plan's "reuse the same seed fns", not a scope change; the four load-bearing cells the plan names are all included.)

## Issues Encountered
- **Dev Postgres (5432) was down;** the `shared` bucket's `tests/shared/services/*` DB tests errored (324) until the test DB (5433) was wired via `TEST_DATABASE_URL`. With the test DB wired, the bucket is `938 passed, 1 failed`.
- **The one failure — `test_migration_019_dedupe.py::test_upgrade_019_dedupes_...` — is unrelated to this plan** (migration 019, not `enums/stage.py`) and **passes in isolation** (`1 passed` standalone). It is the known local full-suite / bucket-isolation flake. Out of scope (SCOPE BOUNDARY); logged to `deferred-items.md`, not fixed.

## Verification
- `tests/shared/test_stage_eligibility_dag.py` + `tests/shared/test_stage_resolver.py`: **44 passed** (ELIG-01..04 unchanged; DB-free import guard green).
- `rg -c 'import sqlalchemy|from sqlalchemy|from phaze.models' src/phaze/enums/stage.py` → **0** (DB-free invariant, T-81-01-01).
- `tests/integration/test_stage_status_equivalence.py`: **36 passed** (23 existing CASES unchanged + 11 new `domain_completed` cells + 2 others), against the 5433 test DB.
- `uv run mypy src/phaze/enums/stage.py src/phaze/services/stage_status.py`: **clean**.
- All three commits passed the full pre-commit hook chain (ruff, ruff-format, bandit, mypy) — no `--no-verify`.
- Behavior spot-checks: `eligible(METADATA, {METADATA:FAILED})`=True; `eligible(ANALYZE, {ANALYZE:FAILED})`=False; `eligible(FINGERPRINT, {FINGERPRINT:FAILED})`=True.

## Threat Flags

None - this plan is pure derivation logic (DB-free tables + SQL predicate); no new trust boundary or untrusted input.

## Next Phase Readiness
- `FAILURE_IS_TERMINAL` + `domain_completed`/`domain_completed_clause` are ready for Phase 80 recovery/reenqueue to consume (the previously-unowned dependency now exists and is drift-locked).
- Phase 79 shadow gate expected green (no derived-status change; `eligible()` refactor is semantics-preserving) — to be confirmed at wave merge.
- Note for reviewers: the two `domain_completed` twins are ledger-agnostic; recovery callers must apply in_flight/ledger checks separately (as `stage_status_case` already does at the status layer).

---
*Phase: 81-per-stage-failure-persistence-retry-paths*
*Completed: 2026-07-09*
