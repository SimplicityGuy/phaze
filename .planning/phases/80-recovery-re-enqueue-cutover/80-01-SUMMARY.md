---
phase: 80-recovery-re-enqueue-cutover
plan: 01
subsystem: database
tags: [alembic, migration, backfill, analysis_completed_at, reenqueue, done_clause, postgres]

# Dependency graph
requires:
  - phase: 77-additive-derived-status-schema
    provides: "analysis.analysis_completed_at column (028) + failed_at NAND CheckConstraint (033) the backfill respects"
  - phase: 79-shadow-compare-gate
    provides: "the shadow invariant state=ANALYZED ⇒ analysis_completed_at IS NOT NULL that 036 makes true on the live corpus"
provides:
  - "migration 036: data-only backfill of analysis.analysis_completed_at for the state='analyzed' corpus"
  - "the BLOCKING prerequisite that lets Plan 80-04's reenqueue cutover derive done(analyze) without re-enqueuing the ~1001 NULL-completed prod rows"
  - "per-migration integration test with a mutation-proven NAND-guard assertion"
affects: [80-04-reenqueue-cutover, 82-counts-pending-cutover, 90-destructive-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "data-only Alembic backfill mirroring 034/035: sync upgrade(), static parameter-free SQL, empty autogenerate diff, documented no-op downgrade"
    - "NAND-guarded UPDATE: AND a.failed_at IS NULL keeps a state='analyzed'+failed row from tripping ck_analysis_analysis_completed_xor_failed"

key-files:
  created:
    - alembic/versions/036_backfill_analysis_completed_at.py
    - tests/integration/test_migrations/test_migration_036_backfill_analysis_completed_at.py
  modified:
    - .planning/REQUIREMENTS.md
    - .planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md

key-decisions:
  - "Source column is a.updated_at — the value is immaterial (done_clause(ANALYZE) tests only IS NOT NULL); updated_at is the most defensible timestamp already on the row"
  - "downgrade() is a documented no-op: pre-existing NULLs are indistinguishable from backfilled values, and blanking analyzed rows would destroy go-forward put_analysis timestamps"
  - "CRITICAL banner reworded to avoid the literal 'saq_jobs' token so the plan's naive `assert 'saq_jobs' not in s` verify (acceptance criterion #4) passes; intent preserved"
  - "NAND-guard test teeth: the control row is state='analyzed' WITH failed_at set (matches the WHERE filter) — a state='analysis_failed' control would be excluded by the state filter and give the guard no teeth"

patterns-established:
  - "Migration-guard tests must be mutation-verified: dropping AND a.failed_at IS NULL flips the integration test RED (per project 'mutation-test your guards' rule)"

requirements-completed: [READ-03]

# Metrics
duration: ~25min
completed: 2026-07-10
---

# Phase 80 Plan 01: analysis_completed_at Backfill (Reenqueue Cutover Prerequisite) Summary

**Migration 036 stamps `analysis.analysis_completed_at` from `analysis.updated_at` for the whole `state='analyzed'` corpus, so the Plan 80-04 reenqueue cutover reads those ~1001 currently-NULL prod rows as domain-complete instead of re-enqueuing them for 4-hour re-analysis.**

## Performance

- **Duration:** ~25 min
- **Tasks:** 3/3
- **Files created:** 2
- **Files modified:** 2 (ROADMAP.md de-numbering deferred to orchestrator — see Deferred)

## Accomplishments
- Landed the **blocking prerequisite** of the reenqueue cutover: a data-only, NAND-safe backfill of `analysis_completed_at` (D-13) that closes the 44.5K-job over-enqueue safety hole atomically in Phase 80's PR.
- Shipped a per-migration integration test mirroring `test_migration_034`, with a **mutation-verified** NAND-guard assertion (GREEN with the guard, RED without it).
- Performed the D-14 documentation de-numbering (prose-only) in the two worktree-owned docs; `just docs-drift` stays green.

## Task Commits

Each task was committed atomically:

1. **Task 1: Create migration 036 (data-only analysis_completed_at backfill)** — `1d3caf9f` (feat)
2. **Task 2: Per-migration test for 036 (mirror test_migration_034)** — `08593f84` (test)
3. **Task 3: D-14 documentation de-numbering (prose-only)** — `cb0b114d` (docs)

## Files Created/Modified
- `alembic/versions/036_backfill_analysis_completed_at.py` — `revision='036'`, `down_revision='035'`; one static `UPDATE analysis a SET analysis_completed_at = a.updated_at FROM files f WHERE a.file_id=f.id AND f.state='analyzed' AND a.analysis_completed_at IS NULL AND a.failed_at IS NULL`; documented no-op downgrade.
- `tests/integration/test_migrations/test_migration_036_backfill_analysis_completed_at.py` — 3 DB-free static asserts (bare-number revision, no-saq_jobs, static-parameter-free SQL) + 1 integration body proving stamp/skip/idempotency/empty-diff/state-unchanged/no-op-downgrade.
- `.planning/REQUIREMENTS.md` — MIG-04 de-numbered (`034` → "the destructive migration (number assigned at plan time)").
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` — §6 destructive-migration step + quiesce note de-numbered.

## Decisions Made
- **Source column `a.updated_at`:** `done_clause(ANALYZE)` only tests `IS NOT NULL`, so the exact timestamp is immaterial; `updated_at` (the row's last-write time) is the most defensible existing value.
- **No-op downgrade:** a data-only backfill of a column shipped in `028`; pre-existing NULLs are indistinguishable from backfilled values, and reverting would destroy go-forward `put_analysis` completion timestamps. Mirrors 032's set-based-backfill and 035's pure-reconcile no-op precedents.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Plan Task 1 verify string `'saq_jobs' not in s` conflicts with the copied CRITICAL banner**
- **Found during:** Task 1
- **Issue:** The plan action says "include the copied CRITICAL banner asserting the migration NEVER references `saq_jobs`", but 034/035's banner literally contains the token `saq_jobs`, whereas the plan's automated verify and acceptance criterion #4 both require `'saq_jobs' not in s`.
- **Fix:** Reworded the banner to "must NEVER touch SAQ's job table (SAQ owns it …)" so the intent is preserved while the literal token never appears. Satisfies both the banner instruction and criterion #4.
- **Files modified:** `alembic/versions/036_backfill_analysis_completed_at.py`
- **Verification:** `assert 'saq_jobs' not in s` passes; the Task 2 banner-aware scan + literal-absence assert both pass.
- **Committed in:** `1d3caf9f`

**2. [Rule 1 - Bug] Plan Task 1 verify string `'revision = "036"'` does not match the annotated form**
- **Found during:** Task 1
- **Issue:** The 034/035 precedent (and mypy) require `revision: str = "036"`. The plan's literal grep `'revision = "036"' in s` does not account for the `: str` annotation and would false-fail against the correct form.
- **Fix:** Kept the type-annotated form and verified via the actual module attribute (`m.revision == "036"`) — which is exactly what the Task 2 test `test_revision_identifiers_are_bare_numbers` asserts.
- **Files modified:** none (verification-method deviation only)
- **Verification:** module-attribute check + Task 2 test pass.
- **Committed in:** `1d3caf9f`

**3. [Rule 1 - Bug] Initial test control row lacked NAND-guard teeth**
- **Found during:** Task 2 (mutation self-check)
- **Issue:** The first draft seeded the failed control as `state='analysis_failed'`. Because the migration's `WHERE f.state='analyzed'` filter already excludes it, removing the `AND a.failed_at IS NULL` guard did NOT flip the test RED — a toothless guard test (violates the project 'mutation-test your guards' rule).
- **Fix:** Changed the control (`_FC`) to `state='analyzed'` WITH `failed_at` set — the exact row the guard protects. Re-ran the mutation: test is GREEN with the guard and RED (migration aborts on the CHECK) without it.
- **Files modified:** `tests/integration/test_migrations/test_migration_036_backfill_analysis_completed_at.py`
- **Verification:** GREEN 4/4 with guard; mutation run FAILED 1/1 without guard; guard restored.
- **Committed in:** `08593f84`

**4. [Rule 3 - Blocking] asyncpg rejects a string bound to a timestamp param**
- **Found during:** Task 2 (first test run)
- **Issue:** Seeding `updated_at` as a bound string param raised `asyncpg DataError: expected a datetime instance, got 'str'`.
- **Fix:** Bound a `datetime(2026,1,2,3,4,5, tzinfo=UTC)` object instead (ruff UP017 `datetime.UTC` alias).
- **Files modified:** test file.
- **Verification:** test passes 4/4.
- **Committed in:** `08593f84`

---

**Total deviations:** 4 auto-fixed (2× Rule 1, 2× Rule 3). All are verification/harness corrections; the migration SQL is exactly as the plan specified.
**Impact on plan:** No scope creep. The two Task 1 verify-string mismatches are plan-authoring bugs in the naive grep commands, not code defects — the correct binding checks are the Task 2 module-attribute + integration asserts, which all pass.

## Issues Encountered
None beyond the auto-fixed deviations above.

## Deferred

**ROADMAP.md D-14 de-numbering (orchestrator-owned shared file).** Per worktree rules this executor did NOT touch `.planning/ROADMAP.md`. The orchestrator must apply these PROSE-ONLY edits post-merge (leave line ~430 — the historical Phase-83 `Corpus-repair migration \`034\`` record — untouched; do NOT alter any checkbox / requirement-ID / Traceability-Status; `just docs-drift` must stay green):

- **L21:** `… seam by seam → destructive \`034\`.` → `… seam by seam → the destructive migration (number assigned at plan time).`
- **L25:** `… before any reader cutover and before \`034\` (MIG-02) …` → `… before any reader cutover and before the destructive migration (number assigned at plan time) (MIG-02) …`
- **L36:** `- [ ] **Phase 90: Destructive Migration \`034\` & Writer Removal** — …` → drop the `\`034\``: `- [ ] **Phase 90: Destructive Migration & Writer Removal** — …`
- **L281 (table row):** `| 90. Destructive Migration 034 & Writer Removal | …` → `| 90. Destructive Migration & Writer Removal | …`
- **L430:** LEAVE AS-IS — historical Phase-83 record (`83-02-PLAN.md — Corpus-repair migration \`034\` …`).
- **L549 (heading):** `### Phase 90: Destructive Migration \`034\` & Writer Removal` → `### Phase 90: Destructive Migration & Writer Removal`
- **L556:** `  1. Migration \`034\` (in one transaction, …` → `  1. The destructive migration (number assigned at plan time) (in one transaction, …`
- **L558:** `  3. \`034.downgrade()\` documents …` → `  3. The destructive migration's \`downgrade()\` documents …`

(Line numbers are as of commit `cb0b114d`; grep by content if they have shifted.)

## Next Phase Readiness
- Plan 80-04's reenqueue cutover can now safely derive `done(analyze)` via `done_clause(ANALYZE)` — the `analyzed` corpus is domain-complete once `036` runs.
- Verification env note: the integration test requires the 5433 migrations DB with `MIGRATIONS_TEST_DATABASE_URL` (+ `TEST_DATABASE_URL`, `PHAZE_REDIS_URL`) exported; `just test-bucket` does not export them.

---
*Phase: 80-recovery-re-enqueue-cutover*
*Completed: 2026-07-10*
