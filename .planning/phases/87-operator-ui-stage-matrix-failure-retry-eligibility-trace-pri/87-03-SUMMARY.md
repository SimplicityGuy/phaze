---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 03
subsystem: testing
tags: [stage-skip, deriv-04, sql-python-twin, shadow-compare, recovery, mutation-testing]
requires:
  - phase: 87-02
    provides: "Status.SKIPPED + skipped_clause threaded into stage_status_case / eligible_clause / domain_completed_clause"
  - phase: 87-01
    provides: "stage_skip table + StageSkip ORM model"
provides:
  - "DERIV-04 equivalence harness extended with skipped cells on all 3 axes (CASES / ELIGIBLE_CASES / DOMAIN_COMPLETED_CASES) + load_scalars skipped read"
  - "Per-enrich-stage pending-set-drop guards (metadata/fingerprint/analyze): a skipped file leaves the pending set"
  - "Recovery guards: a force-skipped analyze/metadata file is domain-complete and NOT re-enqueued (auto + manual force paths)"
  - "Shadow-compare-green-post-skip proof (additive-writer property asserted as a hard test)"
  - "Tracked strict-xfail guard for the fingerprint recovery gap (see OPEN ISSUE)"
affects:
  - "Any future plan that touches reenqueue._build_done_sets (the fingerprint skip gap has a strict-xfail tripwire)"
  - "The force-skip writer + UI plans (they inherit a drift-locked skipped derivation)"
tech-stack:
  added: []
  patterns:
    - "extend the DERIV-04 harness in-place (never bypass the drift-lock) -- seed fn + cell rows + load_scalars key, zero test-body edits"
    - "mutation discipline: every behavioral guard broken at the source -> RED -> restored (Phase-84 rule)"
    - "strict-xfail as a tracked regression tripwire for an out-of-ownership source gap"
key-files:
  created:
    - tests/metadata/test_skipped_leaves_pending.py
    - tests/fingerprint/test_skipped_leaves_pending.py
    - tests/analyze/test_skipped_leaves_pending.py
    - tests/integration/test_shadow_compare_skipped.py
  modified:
    - tests/integration/test_stage_status_equivalence.py
    - tests/analyze/tasks/test_recovery.py
key-decisions:
  - "Fingerprint recovery-skip gap is DEFERRED (not fixed inline): reenqueue.py is a source file outside this tests-only parallel plan's ownership; tracked by a strict-xfail guard + deferred-items.md"
  - "metadata/fingerprint skipped CASES cells seed a plain (not-started) skip so the bucket derives 'skipped'; the skipped ≻ failed precedence cell is the analyze one (seed_analysis_skipped_over_failed)"
  - "shadow-compare test asserts failed_clause(ANALYZE) stays True post-skip -- encoding the additive-writer property so a later failed_at-tidying writer trips the gate"
patterns-established:
  - "DERIV-04 extension: add seed_<stage>_skipped, one cell per axis, one load_scalars key -- the 3 parametrized tests cover it"
  - "recovery skip guard: seed a would-be-orphan + stage_skip marker + ledger row, assert reenqueued==0"
requirements-completed: [UI-04]

duration: ~20min
completed: 2026-07-10
---

# Phase 87 Plan 03: Lock the skipped marker against SQL⇔Python drift + prove its load-bearing behaviors Summary

**Extended the DERIV-04 equivalence harness with skipped cells on all three derivation axes and proved skipped's five behaviors (distinct bucket, skipped ≻ failed, leaves all 3 pending sets, recovery-excluded, shadow-green) — every guard mutation-tested; surfaced one real fingerprint-recovery gap tracked by a strict-xfail tripwire.**

> ⚠️ **OPEN ISSUE flagged to the orchestrator (see `deferred-items.md`): recovery re-enqueues a force-SKIPPED fingerprint file.**
> `reenqueue._build_done_sets` derives `fingerprint_done` from `done_clause(FINGERPRINT)` **only** (never `skipped_clause`), so a force-skipped fingerprint with a surviving ledger row IS re-enqueued by `recover_orphaned_work` — violating behavior 5 for the fingerprint stage. analyze/metadata do NOT have this gap (they read `domain_completed_clause`, which Plan 02 gave a `skipped_clause` disjunct). Empirically confirmed. **Not fixed inline** because `src/phaze/tasks/reenqueue.py` is a source file outside this tests-only, parallel-executor plan's file ownership (plan 04 runs concurrently). One-line fix proposed in `deferred-items.md`; tracked by a **strict-xfail** guard (`test_skipped_fingerprint_row_is_excluded_from_recovery`) that flips the suite RED the moment the fix lands.

## Performance

- **Duration:** ~20 min
- **Started:** 2026-07-10T22:19Z (base)
- **Completed:** 2026-07-10T22:39Z
- **Tasks:** 3
- **Files created/modified:** 6 (4 created, 2 modified) + `deferred-items.md`

## Accomplishments
- DERIV-04 harness now exercises `skipped` on CASES (incl. the skipped ≻ failed precedence cell), ELIGIBLE_CASES, and DOMAIN_COMPLETED_CASES; SQL == Python for every new cell (59 passed, +9 cells).
- A skipped file is proven ABSENT from all three enrich pending helpers (`get_metadata_pending_files` / `get_fingerprint_pending_files` / `get_discovered_files_with_duration`) via the zero-edit `~skipped` conjunct.
- Recovery excludes a force-skipped analyze/metadata file on BOTH the automatic and manual (`force=True`) paths.
- The additive skip marker keeps the Phase-79 shadow gate green with NO allowlist growth; `failed_clause(ANALYZE)` stays True post-skip (additive-writer property encoded as a hard test).

## Task Commits

1. **Task 1: Extend the DERIV-04 harness with skipped cells on all 3 axes** — `1fe7f947` (test)
2. **Task 2: Pending-set-drop (per enrich stage) + recovery-not-re-enqueued guards** — `b0898b40` (test)
3. **Task 3: Shadow-compare stays green post-skip (additive-writer proof)** — `336195aa` (test)

## Files Created/Modified
- `tests/integration/test_stage_status_equivalence.py` — +3 seed fns, +9 cells across the 3 axes, `load_scalars` reads a `skipped` bool per enrich stage.
- `tests/metadata/test_skipped_leaves_pending.py` — metadata pending-set-drop (+ positive control).
- `tests/fingerprint/test_skipped_leaves_pending.py` — fingerprint pending-set-drop (+ positive control).
- `tests/analyze/test_skipped_leaves_pending.py` — analyze pending-set-drop (+ positive control).
- `tests/analyze/tasks/test_recovery.py` — `_seed_stage_skip` helper + 3 green recovery guards (analyze auto/manual, metadata) + 1 strict-xfail fingerprint gap tripwire.
- `tests/integration/test_shadow_compare_skipped.py` — additive-writer shadow-green proof (3 tests).
- `.planning/.../deferred-items.md` — the fingerprint recovery gap (Plan 03 section).

## Mutation Observations (RED-on-break, per Phase-84 rule)
- **Task 1:** dropping the `skipped_clause` branch from `stage_status_case` → the 3 skipped `CASES` cells go RED; restored → 59 passed.
- **Task 2 (pending sets):** dropping the `~skipped` conjunct from `eligible_clause` → all 3 pending-set-drop tests RED (positive controls stay green); restored.
- **Task 2 (recovery):** dropping the `skipped_clause` disjunct from `domain_completed_clause` → all 3 analyze/metadata recovery guards RED; restored.
- **Task 3:** simulating a non-additive writer that clears `failed_at` on skip → all 3 shadow tests RED (the `analysis_failed` invariant flags); restored.
- All source mutations were applied to the working tree and restored via `git checkout --` (the twins are committed at base); no source change is committed by this plan.

## Decisions Made
- **Fingerprint recovery-skip gap deferred, not fixed** — out of a tests-only parallel plan's file ownership; tracked by a strict-xfail guard + `deferred-items.md` (see OPEN ISSUE).
- **Precedence cell placement** — the single load-bearing skipped ≻ failed cell is the analyze `seed_analysis_skipped_over_failed`; metadata/fingerprint skipped cells seed a plain not-started skip (sufficient to prove the distinct bucket on those axes).
- **Shadow test encodes the antecedent** — asserting `failed_clause(ANALYZE)` stays True (not just `hard_fail_total == 0`) is what gives the additive-writer guard teeth against a future `failed_at`-tidying writer.

## Deviations from Plan

Plan executed as written (tests-only). One in-scope finding surfaced and was handled per the issue-handling rule (documented + tracked, not silently skipped):

**1. [Finding — deferred, out of ownership] Recovery re-enqueues a force-skipped fingerprint file**
- **Found during:** Task 2 (recovery guard authoring).
- **Issue:** `reenqueue._build_done_sets.fingerprint_done` = `done_clause(FINGERPRINT)` only; `skipped_clause` never consulted, so behavior 5 fails for the fingerprint stage.
- **Handling:** NOT fixed inline — `src/phaze/tasks/reenqueue.py` is outside plan 03's declared `files_modified` and plan 04 runs concurrently. Logged in `deferred-items.md` (with the 1-line fix), flagged at the TOP of this summary, and guarded by a strict-xfail test that turns RED when the fix lands.
- **Verification:** empirically confirmed via a probe (`is_domain_completed` False for skipped fingerprint, True for skipped analyze/metadata); the xfail currently xfails as expected.

---

**Total deviations:** 0 code auto-fixes (tests-only plan). **1 tracked out-of-scope finding.**
**Impact on plan:** All planned guards delivered and mutation-verified. The fingerprint gap is a pre-existing Plan-02 threading omission surfaced by this plan's coverage, correctly deferred and tracked.

## Issues Encountered
- **Self-inflicted DB contamination:** an ad-hoc probe script (written to verify the fingerprint gap) created tables + a `legacy-application-server` agent in `phaze_test` and never dropped them, breaking the shared `async_engine` fixture (`duplicate key ... pk_agents`). Resolved by `DROP SCHEMA public CASCADE; CREATE SCHEMA public` on `phaze_test`; all buckets green afterward. (Lesson: probe against a throwaway DB or always drop_all.)

## Verification
- `tests/integration/test_stage_status_equivalence.py` → 59 passed.
- `tests/metadata|fingerprint|analyze/test_skipped_leaves_pending.py` → 6 passed.
- `tests/analyze/tasks/test_recovery.py` → 53 passed, 1 xfterm (the fingerprint gap tripwire).
- `tests/integration/test_shadow_compare_skipped.py` → 3 passed.
- Combined coexistence run (all plan-03 files + sibling `test_shadow_compare.py`) → 157 passed, 1 xfailed.
- `uv run ruff check` on all new/modified files → clean; pre-commit (incl. mypy) passed on every commit.

## Next Phase Readiness
- The `skipped` marker is drift-locked on all three derivation axes; downstream force-skip writer + UI plans inherit a proven derivation.
- **Blocker for a source-owning plan:** close the fingerprint recovery gap (`deferred-items.md`) and remove the strict-xfail marker.

## Self-Check: PASSED

All 7 created/modified files present on disk; all 4 commits (1fe7f947, b0898b40, 336195aa, ac8d84ba)
found in git history. Worktree clean.

---
*Phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri*
*Completed: 2026-07-10*
