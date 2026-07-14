---
phase: 85-executed-gate-revival
plan: 01
subsystem: api
tags: [sqlalchemy, predicate, proposals, stage_status, jinja2, postgres]

# Dependency graph
requires:
  - phase: 78-single-source-predicates
    provides: dedup_resolved_clause() file-level correlated-exists template + Stage ladder discipline
provides:
  - "applied_clause() ColumnElement predicate: file is applied iff an executed proposal exists"
  - "is_applied(session, file_id) async per-record twin for write guards"
  - "SC#1 shared-bucket unit contract for the predicate pair (incl. file.state-independence case)"
  - "proposal_row.html 'Executed' badge derived from proposal.status (last file.state reader removed)"
affects: [85-02, 85-03, 85-04, 90-drop-files-state]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "applied() predicate pair templated on dedup_resolved_clause(): file-level correlated exists, no Stage arg, kept OUT of the Stage ladders (DERIV-04 untouched)"
    - "Per-record async EXISTS guard (is_applied) is the net-new shape in stage_status.py (module was otherwise all ColumnElement builders)"

key-files:
  created:
    - tests/shared/test_applied_clause.py
  modified:
    - src/phaze/services/stage_status.py
    - src/phaze/templates/proposals/partials/proposal_row.html
    - tests/review/routers/test_proposals.py

key-decisions:
  - "applied() reads proposals.status=='executed' (transactionally coupled to the apply path), never FileState.EXECUTED (no src/ writer produces it) and never execution_log (best-effort audit log, T-85-02)"
  - "exists(status=='executed') is the authoritative multi-proposal test: a file with BOTH a failed and an executed proposal is applied"
  - "import uuid placed under TYPE_CHECKING (not module-level as the plan interface note said) because from __future__ import annotations defers the file_id annotation and ruff's py313 target flags module-level as TC003"

patterns-established:
  - "New file-level predicates mirror dedup_resolved_clause() and stay out of done/failed/stage_status_case ladders"
  - "Load-bearing UI/predicate guard tests are mutation-verified (revert the reader, watch RED, restore)"

requirements-completed: [READ-05]

# Metrics
duration: 35min
completed: 2026-07-10
---

# Phase 85 Plan 01: applied() Predicate Foundation Summary

**Single-source `applied()` predicate pair (`applied_clause()` + `is_applied()`) reading `proposals.status == 'executed'`, a DB-backed SC#1 contract, and the proposal-row 'Executed' badge cut over from the dead `file.state` reader to `proposal.status`.**

## Performance

- **Duration:** 35 min
- **Started:** 2026-07-10T19:53:00Z
- **Completed:** 2026-07-10T20:28:00Z
- **Tasks:** 3
- **Files modified:** 4 (1 created, 3 modified)

## Accomplishments
- `applied_clause()` — correlated `exists(proposals WHERE file_id==FileRecord.id AND status=='executed')`, templated on `dedup_resolved_clause()`, kept out of the Stage dispatch ladders so the DERIV-04 equivalence test is untouched.
- `is_applied(session, file_id)` — async scalar-EXISTS per-record twin for write guards; never lazy-loads `proposal.file` (`lazy="raise"`).
- SC#1 unit contract (`tests/shared/test_applied_clause.py`, 7 cases) exercising both forms, including the load-bearing case: a file with `state='moved'` + an executed proposal is still applied (proves the predicate never reads `files.state`).
- D-04: `proposal_row.html` badge now derives from `proposal.status == "executed"`; the last stray `proposal.file.state` reader is gone (clears a Phase-90 `files.state`-drop trap).

## Task Commits

Each task was committed atomically:

1. **Task 1: Add applied_clause() + is_applied() to services/stage_status.py** - `d0ed761d` (feat)
2. **Task 2: SC#1 unit contract for the predicate pair** - `287a2f5d` (test)
3. **Task 3: Derive the proposal-row badge from proposal.status (D-04)** - `eef1603a` (fix)

_TDD note: Plan structured Task 1 (implementation) and Task 2 (contract test) as separate atomic commits rather than a strict RED→GREEN split within one task._

## Files Created/Modified
- `src/phaze/services/stage_status.py` - Added `applied_clause()` + `is_applied()` beneath `dedup_resolved_clause()`; both express the predicate purely over `proposals`, never `FileRecord.state`/`execution_log`, and stay out of the Stage ladders.
- `tests/shared/test_applied_clause.py` - New DB-backed SC#1 contract (executed→True; failed/approved/pending/none→False; multi-proposal→True; `state='moved'`+executed→True).
- `src/phaze/templates/proposals/partials/proposal_row.html` - Badge branch `:46` now reads `proposal.status == "executed"`.
- `tests/review/routers/test_proposals.py` - Added a render test (Executed badge from status) and a source-scan test (no `proposal.file.state` survives); both mutation-verified.

## Decisions Made
- **Predicate source = `proposals.status`** (not `FileState.EXECUTED`, not `execution_log`): the executed status is transactionally coupled to the agent's copy→verify→delete apply path; an IO failure forces `status='failed'` (T-85-01/T-85-02). This is the whole reason READ-05's `state == EXECUTED` gates were dead.
- **Kept `applied_clause()` out of the Stage ladders** — mirrors `dedup_resolved_clause()`; a file-level fact, not a pipeline stage, so it must not perturb `tests/integration/test_stage_status_equivalence.py`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `import uuid` moved into the TYPE_CHECKING block**
- **Found during:** Task 1 (add predicate pair)
- **Issue:** The plan's interface note said add module-level `import uuid`. With `from __future__ import annotations` active, `uuid.UUID` in the `is_applied` annotation is deferred/string-only, so ruff (py313 target) flagged the module-level import `TC003 Move standard library import 'uuid' into a type-checking block`, failing the ruff gate.
- **Fix:** Placed `import uuid` under the existing `if TYPE_CHECKING:` block. CLAUDE.md's ruff gate takes precedence over the plan's placement note.
- **Files modified:** src/phaze/services/stage_status.py
- **Verification:** `uv run ruff check` + `uv run mypy` on the file both exit 0.
- **Committed in:** d0ed761d (Task 1 commit)

**2. [Rule 3 - Blocking] Docstrings reworded to avoid the literal `FileRecord.state` token**
- **Found during:** Task 1 (add predicate pair)
- **Issue:** The two new functions' docstrings originally documented the invariant with `:attr:\`FileRecord.state\``, which the acceptance grep counts as `FileRecord.state` occurrences and could trip a line-grep source-scan drift guard.
- **Fix:** Reworded to "the file's `state` column"; the two functions now contain zero `FileRecord.state` text while keeping the invariant documented.
- **Files modified:** src/phaze/services/stage_status.py
- **Verification:** `awk '/^def applied_clause/,/^def done_clause/' ... | grep -c FileRecord.state` == 0.
- **Committed in:** d0ed761d (Task 1 commit)

---

**Total deviations:** 2 auto-fixed (both Rule 3 - blocking/lint-gate). **Impact on plan:** Both are lint-gate compliance under CLAUDE.md's ruff config; no behavior change, no scope creep. The predicate semantics are exactly as specified.

## Issues Encountered
- Task 3 render test initially errored (`InvalidRequestError` from SQLAlchemy) because the premise assertion accessed `proposal.file.state` on a `lazy="raise"` relationship not eager-loaded by `create_test_proposal`. Fixed by reading the file state via an independent `select(FileRecord.state)` scalar query. Mutation-verified afterward: reverting the template to `proposal.file.state` flips both badge tests RED; restoring returns GREEN.

## Threat Flags
None — no new security surface. The predicate only reads the existing `proposals.status` column (the PATCH writer stays `Depends(get_authenticated_agent)`, unchanged); zero new packages (T-85-SC).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- `applied_clause()` / `is_applied()` are the interface Plans 02–04 consume to revive the remaining dead `state == EXECUTED` gates (services/review.py, routers/tags.py, routers/cue.py, routers/tracklists.py, services/tag_writer.py).
- The last `proposal.file.state` reader in `src/phaze/templates/` is gone (`grep -rn "proposal.file.state" src/phaze/templates/` is clean), removing a Phase-90 `files.state`-drop blocker.

## Self-Check: PASSED

All 4 touched files exist on disk; all task commits (`d0ed761d`, `287a2f5d`, `eef1603a`) and the metadata commit (`315547ee`) are present in the log.

---
*Phase: 85-executed-gate-revival*
*Completed: 2026-07-10*
