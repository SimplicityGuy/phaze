---
phase: 86-proposals-cutover
plan: 03
subsystem: testing
tags: [ast, anti-drift, guard, proposals, filestate, sidecar-03, mutation-testing]

# Dependency graph
requires:
  - phase: 86-proposals-cutover (Plan 01)
    provides: "proposal.py + proposal_queries.py service-layer cutover — clean absence of FileState/.state (merged in Wave 1)"
  - phase: 86-proposals-cutover (Plan 02)
    provides: "agent_proposals.py router cutover — apply-PATCH file_record.state write removed (merged in Wave 1)"
provides:
  - "tests/shared/test_proposals_cutover_source_scan.py — mutation-verified AST guard proving zero FileRecord.state read/write and zero FileState.<member> across all three cutover source files (D-01 anti-drift)"
  - "existence-asserted repo root (parents[2]) that fails loud on a wrong root — closes the toothless-guard risk T-86-06"
  - "_state_writes Store-context scan added over the template's read-only scans, so a re-added apply-PATCH file.state mirror WRITE is caught, not merely its FileState RHS"
affects: [87-operator-ui, 90-destructive-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "AST attribute scan (ast.walk over Call.args AND Call.keywords) keyed on the EXACT attribute name — never a line grep (Pitfall 1; feedback_mutation_test_guard_tests)"
    - "Existence-asserted resolved paths so a mis-resolved root fails loudly instead of scanning nothing and going uselessly GREEN"
    - "Mutation directions encoded permanently as test_guard_flags_* (RED) + test_guard_ignores_* (GREEN) over crafted source STRINGS — DB-free, leaves no source dirty"

key-files:
  created:
    - tests/shared/test_proposals_cutover_source_scan.py
  modified: []

key-decisions:
  - "Corrected repo root to parents[2] (PATTERNS.md/phase-guidance parents[1] was an ERROR — this file sits at tests/shared/, the SAME depth as the template test_reenqueue_reconcile_source_scan.py which uses parents[2]); guarded with module-level assert _PROPOSAL.exists() so any wrong root fails loud"
  - "Added a _state_writes (Store-context) scan beyond the template's read-only helpers because Plan 02's removed site was a WRITE (file_record.state = FileState.MOVED); the Store side is now flagged directly, not only via its FileState RHS"
  - "No allow-list variant (unlike the Phase-84 dedup scanner): RESEARCH + Wave-1 summaries confirm clean absence across all three files, so the invariant is the strictly-stronger 'zero occurrences'"

patterns-established:
  - "Wave-2 absence guard: an AST source-scan asserting == [] across every file a multi-plan cutover touched, mutation-verified RED→restore against the real sources before shipping"

requirements-completed: [SIDECAR-03]

# Metrics
duration: ~15min
completed: 2026-07-11
---

# Phase 86 Plan 03: Proposals Cutover Source-Scan Guard Summary

**A mutation-verified AST source-scan guard (`tests/shared/test_proposals_cutover_source_scan.py`) proves the proposal→`FileRecord.state` cascade cannot silently regress: zero `FileRecord.state` read/write and zero `FileState.<member>` occurrences across `services/proposal.py`, `services/proposal_queries.py`, and `routers/agent_proposals.py` — standing anti-drift insurance behind SIDECAR-03 (D-01).**

## Performance

- **Duration:** ~15 min
- **Tasks:** 1
- **Files created:** 1

## Accomplishments
- Created `tests/shared/test_proposals_cutover_source_scan.py` by copying the template `tests/shared/test_reenqueue_reconcile_source_scan.py` verbatim for its helpers (`_filerecord_bound_names`, `_state_reads`, `_filestate_occurrences`, `_getattr_state_calls`, `_where_family_arg_violations`, `_violations`, `_lines`), its `_WHERE_FUNCS` frozenset, and both negative suites, then retargeting the three module constants to `proposal.py`, `proposal_queries.py`, and `agent_proposals.py`.
- Resolved `_SRC_ROOT` empirically at `parents[2]` (the correct depth for a `tests/shared/` file) and guarded it with three module-level `assert <target>.exists()` checks that fail loudly on a wrong repo root — closing the toothless-guard risk (T-86-06).
- Added a `_state_writes` (Store-context `.state`) scan to the aggregate `_violations` union, because Plan 02's removed site was a WRITE (`file_record.state = FileState.MOVED`); the template only scanned reads. A re-added mirror write is now flagged on the Store side directly, not merely via its `FileState` RHS.
- Three real-source guards (`test_proposal_py_has_zero_state_writes`, `test_proposal_queries_has_zero_state_writes`, `test_agent_proposals_has_zero_state_writes`) each assert `_violations(...) == []` and pass against the post-Wave-1 files. The two `FileRecord.state` mentions in `agent_proposals.py` are docstring prose — invisible to the AST attribute scan (proven by `test_guard_ignores_state_mention_in_docstring`).
- Mutation suite: 7 `test_guard_flags_*` RED cases (attribute-in-call read, compare read + FileState, `.values(state=...)` write, instance `.state` WRITE, instance `.state` read, `getattr(_,"state")`, positional `.where` read) plus 2 explicit `.where`-family walker tests. False-positive suite: 4 `test_guard_ignores_*` GREEN cases (`.status` read, `body.file_state` echo, `FileRecord.id` read, docstring mention).

## Task Commits

1. **Task 1: Create the AST source-scan guard over the three cutover files** — `a00d1c1d` (test)

## Files Created/Modified
- `tests/shared/test_proposals_cutover_source_scan.py` — 16-test AST anti-drift guard: 3 real-source `== []` guards, 7 mutation `test_guard_flags_*` RED cases, 2 `_where_family_arg_violations` walker tests, 4 `test_guard_ignores_*` GREEN false-positive cases. Walks `ast.walk` over `Call.args` AND `Call.keywords` (`grep -c '\.keywords'` = 6); existence-asserted `parents[2]` root.

## Decisions Made
- **Corrected root to `parents[2]`, not `parents[1]`.** PATTERNS.md and the phase guidance said `parents[1]`; that is an ERROR — it assumed this file sat one directory deeper. The new file is at `tests/shared/`, the identical depth as the template, which uses `parents[2]`. Verified empirically: `Path("tests/shared/test_x.py").resolve().parents[2]` is the repo root and `(root/"src"/"phaze"/"services"/"proposal.py").exists()` is True. The three `assert <target>.exists()` guards make a wrong root fail loud with the resolved path instead of silently scanning nothing.
- **Added `_state_writes` Store-context scan.** The template guarded read-only files (`reenqueue.py`, `reconcile_cloud_jobs.py`), but Plan 02's retired site is a WRITE. Rather than rely solely on the `FileState.MOVED` RHS occurrence to catch a re-added `file_record.state = ...`, the Store side is scanned directly — so a re-added write with the RHS obscured (e.g. a variable) is still caught.
- **No allow-list.** Unlike the Phase-84 dedup scanner (which permits one surviving dual-writer), all three files hold clean absence per RESEARCH and the Wave-1 summaries, so the invariant is the strictly-stronger "zero occurrences."

## Deviations from Plan

None — plan executed exactly as written. (The `_state_writes` helper and the `test_guard_flags_instance_state_write` case are additive strengthening within the plan's explicit intent to catch the removed WRITE — the plan's own MUTATION-VERIFY step (a) injects a `file_record.state = FileState.MOVED` write, which the template's read-only scans would have caught only via the FileState RHS; the added Store scan makes the write-detection direct and independently tested. Not a functional deviation from the plan's guard contract.)

## AST-Guard Mutation-Verify Observation (RED→restore, recorded)

The load-bearing proof the guard has teeth — performed against the REAL source files, then restored (git tree left clean; only the new test file is added):

- **(a) apply-PATCH write.** Injected `file_record.state = FileState.MOVED` after the `file_record.current_path = body.current_path` write in `routers/agent_proposals.py` → `test_agent_proposals_has_zero_state_writes` went **RED** (`AssertionError`) → `git checkout -- src/phaze/routers/agent_proposals.py` → **GREEN**.
- **(b) `.values(state=...)` write.** Injected `update(FileRecord).where(FileRecord.id == 0).values(state=FileState.MOVED)` before the `return int(...)` in `services/proposal_queries.py` → `test_proposal_queries_has_zero_state_writes` went **RED** → `git checkout -- src/phaze/services/proposal_queries.py` → **GREEN**.
- **False-positive confirmation.** A legitimate `.status` read, a `body.file_state` echo, a `FileRecord.id` read, and a prose docstring mention are all NOT flagged (`test_guard_ignores_*` GREEN). After both restores the full file is 16/16 GREEN and `git status --short` shows only the untracked test file — no source dirty.

## Verification
- `uv run pytest tests/shared/test_proposals_cutover_source_scan.py -x` — 16 passed.
- `uv run ruff check` + `uv run ruff format --check` clean; `uv run mypy` clean on the new file; all pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on the task commit.
- Acceptance greps: `.keywords`=6 (≥1, and it is `ast.walk`, not a source line-grep), `def test_guard_flags_`=7 (≥4), `def test_guard_ignores_`=4 (≥2), 3 real-source `_has_zero_state_writes` guards, 3 `.exists(),` module-level asserts.
- Clean-absence pre-check on all three targets: `proposal.py` FileState=0 `.state`(AST)=0; `proposal_queries.py` FileState=0 `.state`(AST)=0; `agent_proposals.py` FileState=0, the only two `.state` substrings are module-docstring prose (AST-invisible).

## Known Stubs
None — this is a complete anti-drift guard, no placeholder/mock data.

## Next Phase Readiness
- SIDECAR-03 anti-drift insurance complete: the proposal→`FileRecord.state` cascade (deleted in Wave 1) is now protected by a mutation-verified AST guard across all three source files. A re-added `file.state` read/write, `.values(state=...)` splat, or multi-line SQLAlchemy comparator at ANY site (not just the sites the behavioral tests exercise) will fail this guard.
- Full `just test-bucket shared` should be confirmed green post-Wave-2-merge by the orchestrator; note the documented `test_pipeline.py` enum-type-creation race is an unrelated pre-existing bucket-isolation flake (`reference_ci_bucket_isolation`), not introduced here — this guard is DB-free and hermetic.

## Self-Check: PASSED

- `tests/shared/test_proposals_cutover_source_scan.py` present on disk.
- `86-03-SUMMARY.md` present.
- Task commit `a00d1c1d` in git history.

---
*Phase: 86-proposals-cutover*
*Completed: 2026-07-11*
