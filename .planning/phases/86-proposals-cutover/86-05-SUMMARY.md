---
phase: 86-proposals-cutover
plan: 05
subsystem: testing
tags: [ast, anti-drift-guard, mutation-testing, sqlalchemy, sidecar-03]

# Dependency graph
requires:
  - phase: 86-proposals-cutover (plans 01/02/03)
    provides: "The proposal->FileRecord.state cutover + the AST source-scan guard this plan hardens"
provides:
  - "Base-kind-agnostic `.state` read/write scanners (flag chained-attribute bases like `proposal.file.state`)"
  - "`_orm_row_bound_names` helper binding locals fetched via `.scalar_one_or_none()`-family idioms"
  - "Two permanently-encoded, mutation-verified RED cases for the exact deleted-cascade shapes"
affects: [proposals-cutover, anti-drift-guards, future-sidecar-work]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Base-kind-agnostic AST attribute scanning: match `.attr == 'state'` off ANY ast.Attribute chain OR a FileRecord/ORM-row-bound bare Name"
    - "ORM-row-idiom binding: a local assigned from `.scalar_one_or_none()`/`.scalar_one()`/`.scalar()`/`.first()`/`.one_or_none()`/`.one()` is treated as a FileRecord row"

key-files:
  created: []
  modified:
    - "tests/shared/test_proposals_cutover_source_scan.py"

key-decisions:
  - "Added a dedicated `_orm_row_bound_names(tree)` helper (the plan's preferred alternative) rather than overloading `_filerecord_bound_names`, keeping the FileRecord-textual-RHS binding and the ORM-row-fetch binding as two clearly-named, independently-testable concerns."
  - "Kept `.attr == 'state'` as the sole key (never broadened to substring/`.status`/`.file_state`/`.id`), so the three clean source files — which have zero `.state` attribute nodes — stay false-positive-free."
  - "Mutation-verified via a scratch copy inside tests/shared/ (NOT `git stash`, which is prohibited in worktrees and leaks across sibling worktrees), so `parents[2]` resolves and the module-level existence asserts run authentically."

patterns-established:
  - "Pattern: broaden an AST guard's base predicate to `isinstance(base, ast.Attribute) or (isinstance(base, ast.Name) and base.id in bound)` to catch chained-attribute evasions without weakening the attribute-name key."
  - "Pattern: every new guard scanner form is mutation-verified RED->restore->GREEN and the evidence recorded, per feedback_mutation_test_guard_tests."

requirements-completed: [SIDECAR-03]

# Metrics
duration: ~35min
completed: 2026-07-10
---

# Phase 86 Plan 05: Proposals-Cutover Guard Gap Closure Summary

**Broadened the SIDECAR-03 AST anti-drift guard to catch `.state` writes off any attribute chain (`proposal.file.state`) and off two-step ORM-row locals (`file_record = result.scalar_one_or_none()`), closing VERIFICATION Gap 2 / code-review WR-01 with two mutation-verified RED cases.**

## Performance

- **Duration:** ~35 min
- **Completed:** 2026-07-10
- **Tasks:** 2
- **Files modified:** 1 (tests-only)

## Accomplishments
- Closed the WR-01 blind spot: `_state_reads`/`_state_writes` now flag a `.state` attribute regardless of base kind — a bare `ast.Name` bound to a FileRecord (textual-RHS OR ORM-row idiom) OR ANY `ast.Attribute` chain (`proposal.file.state`).
- Added `_orm_row_bound_names(tree)`: a local assigned from a `.scalar_one_or_none()`/`.scalar_one()`/`.scalar()`/`.first()`/`.one_or_none()`/`.one()` call is now treated as a FileRecord row, so the exact two-step idiom the deleted `store_proposals` used is no longer invisible.
- Encoded two permanently-RED mutation cases (`test_guard_flags_chained_attr_string_write`, `test_guard_flags_two_step_orm_idiom_write`) and mutation-verified each RED->restore->GREEN.
- All three real-source guards, all four false-positive GREEN checks, and all seven prior mutation cases stay GREEN; full `tests/shared` bucket green (1059 passed); `src/` left byte-clean.

## Task Commits

Each task was committed atomically:

1. **Task 1: Broaden the attribute scanners to base-kind-agnostic `.state` matching** - `088ad44a` (test)
2. **Task 2: Add and mutation-verify the two RED cases for the missed shapes** - `bfac2b89` (test)

## Files Created/Modified
- `tests/shared/test_proposals_cutover_source_scan.py` - Added `_ROW_FETCH_METHODS` + `_orm_row_bound_names`; broadened `_state_reads`/`_state_writes` to base-kind-agnostic matching with the ORM-row-idiom union; updated the module "form #4" description and both scanner docstrings; added the two new RED mutation cases.

## Mutation RED->restore->GREEN Evidence

Per `feedback_mutation_test_guard_tests` (a GREEN guard test proves nothing), each new scanner form was mutation-verified. Method: the committed (broadened) test file was copied to `tests/shared/_scratch_mutation_verify_8605.py` (same depth, so `parents[2]` resolves and the module-level `assert <target>.exists()` guards run), then the ENTIRE Task 1 broadening was reverted in the scratch copy via two exact substitutions:
- `_filerecord_bound_names(tree) | _orm_row_bound_names(tree)` -> `_filerecord_bound_names(tree)` (drops the ORM-row binding)
- `if isinstance(base, ast.Attribute) or (isinstance(base, ast.Name) and base.id in bound):` -> `if isinstance(base, ast.Name) and base.id in bound:` (drops the chained-attribute match)

**RED run — new cases against the REVERTED (old bare-Name-only) scanners:**
```
$ uv run pytest tests/shared/_scratch_mutation_verify_8605.py::test_guard_flags_chained_attr_string_write \
                tests/shared/_scratch_mutation_verify_8605.py::test_guard_flags_two_step_orm_idiom_write -q
        violations = _violations(source)
>       assert violations != []
E       assert [] != []
        violations = _violations(source)
>       assert violations != []
E       assert [] != []
2 failed, 1 warning in 0.09s
```
Both cases FAIL — `_violations` returns `[]` for `proposal.file.state = "approved"` (chained `ast.Attribute` base, no `FileState` node) and for `file_record = result.scalar_one_or_none(); file_record.state = "moved"` (RHS lacks the `FileRecord` textual token). This reproduces the exact WR-01 blind spot the reverted scanner cannot see.

**GREEN run — same two cases against the RESTORED (broadened, committed) scanners:**
```
$ uv run pytest tests/shared/test_proposals_cutover_source_scan.py::test_guard_flags_chained_attr_string_write \
                tests/shared/test_proposals_cutover_source_scan.py::test_guard_flags_two_step_orm_idiom_write -q
2 passed, 1 warning in 0.01s
```
Both PASS under the broadening. The scratch file was deleted after verification (`git status --short` clean; only the committed test file changed).

**False-positive GREEN preserved (broadening did not over-fire):** the full guard file is 18/18 GREEN — the three real-source guards (`test_proposal_py_has_zero_state_writes`, `test_proposal_queries_has_zero_state_writes`, `test_agent_proposals_has_zero_state_writes`) and the four false-positive checks (`.status` read, `body.file_state` echo, `FileRecord.id` read, docstring prose) all stay clean. Independently confirmed the three source files carry zero real `.state` attribute nodes (the only matches are docstring prose in `agent_proposals.py:8-9`, invisible to an AST attribute scan).

## Verification Results
- `uv run pytest tests/shared/test_proposals_cutover_source_scan.py -q` -> **18 passed** (16 prior + 2 new).
- `grep -c "def test_guard_flags_chained_attr_string_write\|def test_guard_flags_two_step_orm_idiom_write"` -> **2**.
- `uv run pytest tests/shared -q` (against ephemeral Postgres 5433 / Redis 6380) -> **1059 passed**, exit 0.
- `git diff --name-only -- src/` -> empty (tests-only; source byte-clean).

## Decisions Made
- Chose a parallel `_orm_row_bound_names` helper over broadening `_filerecord_bound_names` — cleaner separation and directly testable.
- Used a scratch-copy reversion for mutation verification instead of `git stash` (prohibited in worktrees: the stash ref is shared across worktrees and would risk cross-contamination).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- `uv run pytest tests/shared -q` initially reported 331 errors + 1 failure — all `Connect call failed ('127.0.0.1', 5432)`. Root cause: the ephemeral test Postgres runs on host port **5433** (the known `MIGRATIONS_TEST_DATABASE_URL` port footgun), while the default DB URL targets 5432. Re-running with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` pointed at 5433/6380 (mirroring the `integration-test` recipe) yielded 1059 passed, exit 0. Not related to this plan's change; the guard test itself is DB-free and hermetic.

## Next Phase Readiness
- VERIFICATION Gap 2 / WR-01 is closed: the anti-drift guard now has real teeth against the actual deleted-cascade shapes. Gap 1 (the stale `tests/review/services/test_proposal.py` assertion) is a separate wave/plan and is out of scope here.

## Self-Check: PASSED

- FOUND: `tests/shared/test_proposals_cutover_source_scan.py`
- FOUND: `.planning/phases/86-proposals-cutover/86-05-SUMMARY.md`
- FOUND commit `088ad44a` (Task 1), FOUND commit `bfac2b89` (Task 2)
- `git diff --name-only -- src/` empty (source byte-clean)

---
*Phase: 86-proposals-cutover*
*Completed: 2026-07-10*
