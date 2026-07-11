---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 02
subsystem: derivation
tags: [stage-skip, derive-dont-store, single-source, sql-python-twin, deriv-04]
requires:
  - stage_skip table + StageSkip ORM model (Plan 01)
provides:
  - "Status.SKIPPED (5th member) + precedence in_flight ≻ done ≻ skipped ≻ failed ≻ not_started"
  - "resolve_status threads a `skipped` scalar into the three enrich branches only"
  - "eligible / domain_completed handle SKIPPED (leaves pending set / is domain-complete)"
  - "skipped_clause(stage) SQL builder (enrich-only ValueError guard)"
  - "skipped_clause threaded into stage_status_case (5-way enrich) / eligible_clause / domain_completed_clause"
affects:
  - "every enrich pending set + both recovery paths + the pill matrix derive skipped from ONE place (zero per-caller edits)"
  - "Plan 03 extends the DERIV-04 equivalence harness to lock the new skipped cells"
tech-stack:
  added: []
  patterns:
    - "single-source derivation: thread one clause into the shared composers, never per-caller filters"
    - "SQL⇔Python twin edited in lockstep; enrich-only ValueError guard mirrored across both twins"
    - "correlated exists(... == FileRecord.id) marker probe (mirrors done_clause / dedup_resolved_clause)"
key-files:
  created: []
  modified:
    - src/phaze/enums/stage.py
    - src/phaze/services/stage_status.py
    - tests/shared/test_stage_resolver.py
decisions:
  - "SKIPPED ordered done ≻ skipped ≻ failed: the writer is additive (never clears failed_at), so CASE / branch order — not the writer — decides"
  - "skipped scalar passed to enrich branches ONLY; downstream stages ignore it (resolve_status) and skipped_clause raises on them (D-10)"
  - "domain_completed_clause: skipped is an UNCONDITIONAL disjunct (alongside done); the terminal-failure disjunct stays gated on FAILURE_IS_TERMINAL"
  - "SQL-twin guard test uses a function-local import to preserve test_stage_resolver.py's DB-free module-level contract (the subprocess boundary guard)"
metrics:
  duration: ~40m
  completed: 2026-07-11
  tasks: 2
  files: 3
---

# Phase 87 Plan 02: Thread the skipped marker into the single-source derivation contract Summary

Threaded the D-08 force-skip marker into the single-source per-stage derivation layer via ONE new
`skipped_clause` builder plus a 5th `Status.SKIPPED` member — so a force-skipped enrich file (a) reads as
a distinct `skipped` bucket, (b) leaves all three enrich pending sets, and (c) is treated as
domain-complete by recovery, ALL through the existing composers with zero per-caller edits. Pure
composition change; no reader/writer cut over here. The SQL twin (`stage_status.py`) and the Python twin
(`enums/stage.py`) were edited in lockstep.

## What Was Built

- **Python twin (`src/phaze/enums/stage.py`)**: added `SKIPPED = "skipped"` to `Status` between `DONE` and
  `FAILED` (the enum is now 5-way, precedence `in_flight ≻ done ≻ skipped ≻ failed ≻ not_started`). Threaded
  a `skipped: bool = False` parameter into `_analyze_status` / `_metadata_status` / `_fingerprint_status`,
  placing `if skipped: return Status.SKIPPED` AFTER the done check and BEFORE the failed check (the
  load-bearing precedence, Pitfall 2). `resolve_status` reads `skipped = bool(scalars.get("skipped", False))`
  and passes it to the three enrich branches ONLY (downstream signatures unchanged). `eligible`'s enrich
  branch excludes SKIPPED (`status not in (DONE, IN_FLIGHT, SKIPPED)`); `domain_completed` treats SKIPPED as
  complete (`st in (DONE, SKIPPED) or (FAILED and FAILURE_IS_TERMINAL[stage])`). The stdlib-only import
  boundary (T-78-01) is preserved — no model import added.
- **SQL twin (`src/phaze/services/stage_status.py`)**: imported `StageSkip`; added
  `skipped_clause(stage) -> ColumnElement[bool]` mirroring `done_clause`'s correlated-`exists` shape with the
  enrich-only `ValueError` guard (same shape as `eligible_clause`). Threaded it into THREE composers:
  (a) `stage_status_case` builds a 5-way CASE for enrich stages inserting `(skipped_clause, SKIPPED)` after
  the done branch and before the failed branch, while downstream stages keep the 4-way ladder (guarded on
  `stage in ELIGIBLE_AFTER_FAILURE`); (b) `eligible_clause` appends `not_(skipped_clause(stage))` to the
  enrich conjuncts; (c) `domain_completed_clause` adds `skipped_clause(stage)` as an unconditional disjunct
  alongside `done_clause`.
- **Tests (`tests/shared/test_stage_resolver.py`)**: six Python-twin behaviors (skipped ≻ failed,
  done ≻ skipped, in_flight ≻ skipped, per-enrich-stage skipped bucket, skipped-not-eligible,
  skipped-is-domain-complete, downstream-ignores-skipped) plus two SQL-twin guard tests
  (`skipped_clause` builds for each enrich stage; raises `ValueError` on each downstream stage). The
  SQL-twin guard tests use a function-local import so the module-level DB-free contract (the subprocess
  banned-import guard) stays intact.

## How to Verify

- `uv run mypy src/phaze/enums/stage.py` → clean; `uv run ruff check .` → clean.
- `uv run pytest tests/shared/test_stage_resolver.py -q` → 43 passed.
- With test DB up (`just test-db`, port 5433, DB `phaze_test`):
  `TEST_DATABASE_URL=postgresql://phaze:phaze@localhost:5433/phaze_test PHAZE_REDIS_URL=redis://localhost:6380/0 uv run pytest tests/integration/test_stage_status_equivalence.py -q`
  → 50 passed (the existing DERIV-04 equivalence suite still green — no regression; Plan 03 adds the skipped cells).
- Clause consumers (`test_domain_completed_contract`, `test_awaiting_candidate_clause`,
  `test_pending_set_source_scan`, `test_pending_set_divergence`) → all pass.

## Deviations from Plan

None — plan executed as written. (Two environment/tooling notes below, neither a plan deviation.)

## Notes

- **Mutation-tested guard teeth (project memory rule):**
  - Python twin (Task 1): removed the `if skipped:` branch from `_analyze_status` → the skipped ≻ failed
    case (`test_skipped_wins_over_failed`) went RED with `AttributeError`/wrong bucket; restored → green.
  - SQL twin (Task 2): removed the enrich-only guard from `skipped_clause` → the three downstream
    `test_skipped_clause_raises_on_downstream_stage[propose|review|apply]` cases went RED (no ValueError);
    restored from a saved copy → green. Note: the restore was done via a saved file copy (not
    `git checkout --`) because uncommitted twin edits were live in the working tree.
- **Test-DB port/name footguns (project memory):** the integration suite defaults to
  `postgresql://phaze:phaze@localhost:5432/phaze`, but `just test-db` provisions port 5433 and the DB is
  named `phaze_test` (not `phaze`). Both must be set (`TEST_DATABASE_URL=...localhost:5433/phaze_test`)
  or the whole suite silently `pytest.skip`s "Postgres broker unavailable".
- **Out-of-scope (logged to `deferred-items.md`):** `tests/integration/test_drain_double_dispatch.py`
  errors in fixture setup with `ModuleNotFoundError: No module named 'psycopg2'` (the SAQ Postgres-broker
  `scoped_runner` fixture resolves a sync `postgresql://` DSN). This fires before any test body, was last
  touched in Phase 83, and is unrelated to this plan's asyncpg-only ORM changes.

## Threat Register Coverage

- **T-87-05** (SQL injection via stage param): mitigated — `skipped_clause` is pure ORM with a bound
  `StageSkip.stage == stage.value` operand + the enrich-only `ValueError` guard; no f-string SQL.
- **T-87-06** (skip applied to a downstream stage): mitigated — the enrich-only guard on `skipped_clause`
  mirrors `eligible_clause`; the downstream `stage_status_case` stays 4-way (skipped branch is guarded on
  `stage in ELIGIBLE_AFTER_FAILURE`). Asserted by `test_skipped_clause_raises_on_downstream_stage`.
- **T-87-07** (SQL⇔Python twin drift): mitigated — both twins edited in lockstep here; the existing
  DERIV-04 equivalence suite stays green, and Plan 03 extends it to lock the new skipped cells.

No new threat surface introduced beyond the plan's register.

## Self-Check: PASSED

All 3 modified files present on disk; both task commits (74f662d6, eb39247b) + the deferred-items chore
commit (b9bfa01b) found in git history.
