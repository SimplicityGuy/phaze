---
phase: 81-per-stage-failure-persistence-retry-paths
plan: 02
subsystem: database
tags: [alembic, postgres, sqlalchemy, check-constraint, migration, failure-markers]

# Dependency graph
requires:
  - phase: 77-additive-schema-rescan-wipe-fix-migration-032
    provides: "migration 032 -- the additive analysis.failed_at / error_message columns and the unguarded ON CONFLICT DO UPDATE backfill whose mixed rows this migration cleans"
  - phase: 78-derivation-layer-eligibility-anti-drift
    provides: "_analyze_status done-over-failed precedence, which the D-09 cleanup's done-wins choice matches"
  - phase: 79-shadow-compare-gate-live-corpus
    provides: "the standing state<->derived implication gate that the cleanup must not perturb"
provides:
  - "alembic migration 033: mixed-row cleanup UPDATE followed by the analysis_completed_at XOR failed_at CHECK"
  - "ck_analysis_analysis_completed_xor_failed enforced at the DB, mirrored into AnalysisResult.__table_args__"
  - "tests/integration/test_migrations/test_migration_033_additive_check.py -- proves the D-09 ordering against 032's real backfill"
  - "planning docs renumbered: the destructive Phase-90 migration is now 034"
affects: [81-03, 81-04, 81-05, 90-destructive-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "data-cleanup-before-constraint: a validating DDL constraint is preceded, in the same upgrade(), by the set-based UPDATE that makes the existing corpus satisfy it"
    - "bare-constraint-name: pass the unprefixed name to create_check_constraint/drop_constraint and let the ck_%(table_name)s_%(constraint_name)s convention render it"
    - "source-order test gate: assert the op.* call sites' relative position in upgrade(), because an empty test DB would round-trip either ordering"

key-files:
  created:
    - alembic/versions/033_add_analysis_completed_xor_failed.py
    - tests/integration/test_migrations/test_migration_033_additive_check.py
  modified:
    - src/phaze/models/analysis.py
    - .planning/ROADMAP.md
    - .planning/REQUIREMENTS.md
    - .planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md
    - .planning/PROJECT.md
    - .planning/STATE.md

key-decisions:
  - "D-06: the analysis completed-XOR-failed CHECK is added at the DB and mirrored into the ORM __table_args__ so alembic autogenerate stays empty"
  - "D-09: the mixed-row cleanup UPDATE runs before create_check_constraint, since 032's unguarded backfill already produced rows the CHECK would reject"
  - "D-04: the cleanup clears failed_at and retains analysis_completed_at -- done wins -- so no file's derived analyze status changes and the Phase 79 shadow gate stays green"
  - "D-08: Phase 81 claims alembic revision 033; the destructive Phase-90 migration is renumbered 034 in every forward-looking planning doc, while dated historical records keep 033"
  - "STATE.md line 67 carried two destructive-migration references, not the one the plan enumerated; both were renumbered"

patterns-established:
  - "Cleanup-then-constrain: any validating constraint added over a live corpus must be preceded by the statement that makes the corpus satisfy it, in the same upgrade()"
  - "Migration tests reproduce the hazard through the real prior migration rather than hand-seeding its output, so the premise is proven rather than assumed"

requirements-completed: [FAIL-01]

# Metrics
duration: 22min
completed: 2026-07-08
---

# Phase 81 Plan 02: Migration 033 (Analysis Completed XOR Failed) Summary

**Migration `033` cleans the mixed `analysis` rows that `032`'s unguarded backfill produced, then adds the `analysis_completed_at` XOR `failed_at` CHECK — the DB-level mutual exclusion FAIL-01's analyze failure marker depends on — with the destructive Phase-90 migration renumbered `033 → 034` across the forward-looking planning docs.**

## Performance

- **Duration:** ~22 min
- **Started:** 2026-07-08T22:12:00-07:00 (approx.)
- **Completed:** 2026-07-08T22:33:00-07:00
- **Tasks:** 3/3
- **Files modified:** 8 (2 created, 6 modified)

## Accomplishments

### Task 1 — Migration 033 + ORM mirror (`00c616c7`)

`alembic/versions/033_add_analysis_completed_xor_failed.py` (`revision = "033"`, `down_revision = "032"`).

`upgrade()` executes, in this mandatory order (D-09):

1. `UPDATE analysis SET failed_at = NULL WHERE analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL` — the mixed-row cleanup. It clears `failed_at` and retains `analysis_completed_at`, so **done wins** (D-04), matching `_analyze_status`'s done-over-failed precedence. No file's derived analyze status changes, so the Phase 79 shadow gate stays green.
2. `op.create_check_constraint("analysis_completed_xor_failed", "analysis", "NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)")` — the bare name; the `ck_%(table_name)s_%(constraint_name)s` convention renders `ck_analysis_analysis_completed_xor_failed` (confirmed against `pg_constraint`).

`downgrade()` drops the CHECK only. The D-09 cleanup is deliberately **not** reversed (016/032 best-effort-DDL precedent) — the migration cannot know which rows were mixed before it ran.

`src/phaze/models/analysis.py` gains the CHECK as a third `__table_args__` element with the same bare name and identical predicate text, keeping `alembic revision --autogenerate` empty (D-06).

### Task 2 — Migration test (`8f7b464d`)

`tests/integration/test_migrations/test_migration_033_additive_check.py`, modelled on the `032` test.

The integration body does **not** hand-seed a mixed row. It seeds three analyze-stage shapes at revision `031`, upgrades `031 → 032` so `032`'s **real** `ON CONFLICT (file_id) DO UPDATE` backfill manufactures the mixed row, asserts the row is genuinely mixed at `032`, then upgrades to `033`. This proves the D-09 premise empirically rather than assuming it. The test then asserts:

- the upgrade **succeeds at all** despite the pre-existing mixed row (the cleanup precedes the CHECK);
- done wins: the mixed row ends with `failed_at IS NULL` and `analysis_completed_at` retained;
- the failed-only row keeps its marker; the done-only row is untouched;
- the rendered constraint name is `ck_analysis_analysis_completed_xor_failed` in `pg_constraint`;
- the ORM `__table_args__` CHECK renders the identical name over the identical predicate;
- the autogenerate diff is empty for the `033` objects;
- the CHECK actually rejects a newly-mixed row (`IntegrityError`);
- down/up round-trips.

Four DB-free cells run without Postgres: bare-number revision ids, the `saq_jobs` banner guard, the source-order gate, and the ORM-mirror parity check.

### Task 3 — Destructive-migration doc renumber (`07eccebe`)

Discovery first (`rg -n '\b033\b' .planning/`), then classify-and-edit. Renumbered `033 → 034` in the forward-looking statements only:

| File | What changed |
|------|--------------|
| `.planning/ROADMAP.md` | milestone-shape line; Phase 79 "before 034" ordering; Phase 90 title; Phase 90 summary-table row; Phase 90 detail block's one-transaction line **and** its `034.downgrade()` line |
| `.planning/REQUIREMENTS.md` | MIG-04 only (MIG-02 carries no literal `033`) |
| `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` | the "`034` — destructive" step; the drain-the-cloud-push-lanes-before line |
| `.planning/PROJECT.md` | line 22's current two-step design line |
| `.planning/STATE.md` | both destructive-migration references on the roadmap-created entry |

The two ROADMAP lines `81-RESEARCH.md` flagged as **missing from D-08's enumeration** (the one-transaction line and the `.downgrade()` line, at 504/506 after drift) were both renumbered.

`.planning/PROJECT.md:423` is the resolved judgment call: it is an explicitly dated entry, so its `033` stays, and it now carries the appended parenthetical `(Phase 81 later claimed 033; the destructive migration is 034)`.

**Deliberately left unchanged** (dated historical records, named here so the omission is auditable rather than an oversight): `.planning/phases/77-*/**`, `.planning/phases/79-*/**`, `.planning/research/**`, and `.planning/PROJECT.md` lines 113/115. `.planning/ROADMAP.md:378` names Phase 81's **own** additive `033` and is correct as written. This phase's own `81-*` docs likewise keep their `033` references.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Source-order test gate matched a prose comment, not the call site**

- **Found during:** Task 2
- **Issue:** The first draft of `test_cleanup_update_precedes_create_check_constraint` compared `upgrade_body.index("_CLEANUP_MIXED_ROWS")` against `upgrade_body.index("create_check_constraint")`. `upgrade()`'s explanatory comment names `create_check_constraint` *above* the cleanup call, so the bare-identifier search matched the comment and the test failed (281 < 141) against correct source.
- **Fix:** Match the actual call sites — `op.execute(sa.text(_CLEANUP_MIXED_ROWS))` and `op.create_check_constraint(` — and document why in the test docstring. Also added an assertion that the cleanup SQL contains `SET failed_at = NULL` and never a `SET analysis_completed_at` clause (D-04).
- **Files modified:** `tests/integration/test_migrations/test_migration_033_additive_check.py`
- **Commit:** `8f7b464d`

**2. [Rule 3 - Blocking] `_CLEANUP_MIXED_ROWS` collapsed to a single-line SQL literal**

- **Found during:** Task 2
- **Issue:** The plan's acceptance criterion requires the literal string `UPDATE analysis SET failed_at = NULL` to appear at a **line number less than** the `create_check_constraint(` line. The first draft followed `032`'s multi-line triple-quoted constant style, so that exact string never appeared on any single line and the criterion was unverifiable by `rg`.
- **Fix:** Collapsed the constant to one line (127 chars, within the 150-char limit), preserving `032`'s module-constant convention while satisfying the literal criterion.
- **Files modified:** `alembic/versions/033_add_analysis_completed_xor_failed.py`
- **Commit:** `8f7b464d`

**3. [Rule 2 - Missing critical] STATE.md carried a second destructive-migration reference the plan did not enumerate**

- **Found during:** Task 3
- **Issue:** The plan's Step 2 named `.planning/STATE.md:67` for the phrase "the gated, LAST destructive `033`". The same line also reads "must pass before any reader cutover AND before `033`" — the same destructive migration, in the same forward-looking class the plan explicitly renumbers at `ROADMAP.md:25`. Leaving it would have violated the plan's own acceptance criterion (no surviving forward-looking reference to the destructive migration as `033`).
- **Fix:** Renumbered both occurrences on that line.
- **Files modified:** `.planning/STATE.md`
- **Commit:** `07eccebe`

**4. [Rule 1 - Bug] Pre-commit `ruff-format` rewrote the test file mid-commit**

- **Found during:** Task 2
- **Issue:** The first `git commit` for Task 2 aborted because `ruff format` joined three wrapped `await conn.execute(...)` expressions. Cosmetic only.
- **Fix:** Re-staged the formatted file and re-committed. Re-ran the test afterwards (5 passed) to confirm the reformat was behaviour-neutral.
- **Files modified:** `tests/integration/test_migrations/test_migration_033_additive_check.py`
- **Commit:** `8f7b464d`

**5. [Rule 3 - Blocking] `requirements.mark-complete FAIL-01` broke the docs-drift guard**

- **Found during:** post-plan state updates
- **Issue:** The executor's standard state-update step runs `gsd-sdk query requirements.mark-complete` for the plan's `requirements` frontmatter. Doing so flipped FAIL-01's checkbox **and** its traceability-table row to Complete, which `tests/shared/core/test_requirements_traceability.py` rejects: `FAIL-01 marked Complete but Phase 81 not passed`. Requirement completion is a **phase**-level event (gated on the ROADMAP phase checkbox / a `81-VERIFICATION.md`), not a plan-level one — and Phase 81 still has wave 2 (plans 05-06) outstanding. `just docs-drift` went from 10 passed to 3 failed.
- **Fix:** Reverted both FAIL-01 edits, restoring `.planning/REQUIREMENTS.md` byte-for-byte to its Task-3-committed state. `just docs-drift` is green again (10 passed). Per the project's CLAUDE.md-enforcement rule, the repo's own guard outranks the executor's default step. FAIL-01 stays `Pending` until Phase 81 as a whole passes verification; this SUMMARY's `requirements-completed: [FAIL-01]` records what the **plan** delivered, which is the input the phase-level gate will later consume.
- **Files modified:** `.planning/REQUIREMENTS.md` (net zero — reverted)
- **Commit:** n/a (no net change committed)

## Verification

All four of the plan's verification commands were run against the ephemeral test Postgres (`phaze-test-db`, `localhost:5433`).

| Command | Result |
|---------|--------|
| `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` | exit 0 — `032 -> 033`, `033 -> 032`, `032 -> 033` |
| `uv run just test-bucket integration` (isolation) | **146 passed** in 59.29s |
| `uv run just docs-drift` | **10 passed** |
| `uv run mypy src/phaze/models/analysis.py` | Success: no issues found |

Direct probe: `SELECT conname FROM pg_constraint WHERE contype='c' AND conrelid='analysis'::regclass` returns `ck_analysis_analysis_completed_xor_failed`.

Renumber gate (the real one — `docs-drift` does **not** assert on migration-number prose, per 81-RESEARCH A2). After the edit, `rg -n '\b033\b'` over the five target docs returns exactly four lines, each consciously classified:

- `.planning/PROJECT.md:113` — dated Phase-79 completion narrative (historical)
- `.planning/PROJECT.md:115` — dated Phase-78 completion narrative (historical)
- `.planning/PROJECT.md:423` — dated milestone-start entry, now carrying the clarifying parenthetical
- `.planning/ROADMAP.md:378` — Phase 81's **own** additive migration `033`

No surviving forward-looking reference to the destructive migration as `033`.

## Success Criteria

- [x] Migration `033` exists; the cleanup precedes the CHECK; round-trips down/up.
- [x] CHECK mirrored into the ORM; autogenerate diff empty for the `033` objects.
- [x] Destructive-migration references renumbered `033 -> 034`; `docs-drift` green.

## Known Stubs

None. No placeholder values, no `TODO`/`FIXME`, no unwired data sources introduced by this plan.

## TDD Gate Compliance

Task 2 carried `tdd="true"`, but the plan sequences Task 1 (implementation) **before** Task 2 (test) by design — a migration test cannot meaningfully run RED against a migration file that does not exist, since the test loads the module by path and asserts its revision identifiers. The gate commits therefore appear as `feat(...)` then `test(...)` rather than `test(...)` then `feat(...)`.

A genuine RED did occur within Task 2: `test_cleanup_update_precedes_create_check_constraint` failed on first run (deviation 1 above), and the failure was resolved before the task committed. No test was written to pass vacuously.

## Threat Flags

None. The plan's `<threat_model>` mitigations are all present in the implementation:

- **T-81-02-01** (CHECK aborts on a pre-existing mixed row) — mitigated by the D-09 cleanup ordering; the migration test proves the upgrade succeeds against a mixed row manufactured by `032`'s real backfill.
- **T-81-02-02** (data loss on the cleanup UPDATE) — the cleanup only clears `failed_at` on rows whose `analysis_completed_at` is already set; a source-level assertion forbids a `SET analysis_completed_at` clause. No done marker can be lost.
- **T-81-02-03** (ORM/migration schema drift) — the ORM mirror is asserted to render the same name and predicate as the migration, plus the empty-autogenerate-diff check.
- **T-81-02-SC** (package installs) — accepted; zero new dependencies, no installs performed.

No new network endpoints, auth paths, file-access patterns, or trust-boundary schema surface were introduced.

## Commits

| Task | Commit | Message |
|------|--------|---------|
| 1 | `00c616c7` | `feat(81-02): add migration 033 analysis completed XOR failed CHECK` |
| 2 | `8f7b464d` | `test(81-02): prove migration 033 cleans mixed rows before the XOR CHECK` |
| 3 | `07eccebe` | `docs(81-02): renumber the destructive Phase-90 migration 033 -> 034` |

All commits landed on `SimplicityGuy/phase-81`. None on `main`.

## Self-Check: PASSED

All created files exist on disk; all three task commits resolve in `git log`.
