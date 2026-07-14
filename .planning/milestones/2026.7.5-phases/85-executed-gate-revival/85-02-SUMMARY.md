---
phase: 85-executed-gate-revival
plan: 02
subsystem: api
tags: [sqlalchemy, applied-predicate, tag-writer, mutagen, htmx, review, mutation-test]

# Dependency graph
requires:
  - phase: 85-executed-gate-revival
    plan: 01
    provides: applied_clause() + is_applied() predicate pair reading proposals.status=='executed'
provides:
  - "tag-write path (execute_tag_write guard + 5 tags.py gate/count/list sites) revived onto applied()"
  - "bounded no-discrepancy bulk builder (_MAX_BULK_TAG_WRITE=2000, D-03)"
  - "SC#2 mutation-checked behavior test: an actually-applied file (state!='executed' + executed proposal) now passes a previously-always-failing guard"
affects: [85-03, 85-04, 90-drop-files-state]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Write guards consume await is_applied(session, file_id); WHERE/COUNT readers consume applied_clause() (single-source, never a hand-rolled subquery)"
    - "Operator-triggered one-shot bulk loops bounded by a module .limit(N) cap so a first-time-visible applied backlog cannot blow up at 200K scale (D-03)"
    - "Load-bearing guard tests seed REAL rows and are mutation-verified (revert the predicate, watch RED, restore GREEN)"

key-files:
  created: []
  modified:
    - src/phaze/services/tag_writer.py
    - src/phaze/routers/tags.py
    - tests/review/services/test_tag_writer.py
    - tests/review/routers/test_tags.py

key-decisions:
  - "Bulk cap _MAX_BULK_TAG_WRITE=2000 (low-thousands, consistent with in-tree page bounds) bounds the operator-triggered no-discrepancy loop (D-03/T-85-04)"
  - "completed_subq (TagWriteLog.status==COMPLETED anti-join) preserved verbatim at the bulk builder — idempotency intact, no state-based de-dupe reintroduced (D-02)"
  - "Write-mechanics unit tests patch is_applied to admit explicitly (decoupled from the guard) so they exercise the mutagen path in isolation; only the two SC#2 guard cases seed real DB rows"

requirements-completed: [READ-05]

# Metrics
duration: 40min
completed: 2026-07-10
---

# Phase 85 Plan 02: Tag-Write Path applied() Cutover Summary

**The tag-write write guard (`tag_writer.py:185`) and all five `tags.py` gate/count/list sites cut over from the permanently-dead `FileState.EXECUTED` reader to the D-01 `applied()` predicate, the unbounded no-discrepancy bulk builder bounded by a `.limit(2000)` cap, and a mutation-checked SC#2 behavior test proving an actually-applied file (`state='moved'` + executed proposal) now passes a guard that previously ALWAYS failed.**

## Performance

- **Duration:** ~40 min
- **Tasks:** 2
- **Files modified:** 4 (0 created, 4 modified)

## Accomplishments
- **`execute_tag_write` guard revived (`tag_writer.py:185`):** `if file_record.state != FileState.EXECUTED` → `if not await is_applied(session, file_record.id)`. The `raise ValueError("Only executed files can have tags written")` message (matched by callers/tests) is unchanged. Dead `FileState` import removed (ruff F401).
- **Five `tags.py` sites swapped:** stat-card count (`_get_tag_stats`, :44), `list_tags` WHERE + COUNT (:174/:179), per-record write guard (`write_file_tags`, :336), and the bulk-builder WHERE (`bulk_write_no_discrepancies`, :422) all now read `applied_clause()` / `await is_applied(session, file_id)`. The previously-empty operator Tag list now populates for actually-applied files. Dead `FileState` import removed.
- **D-03 bound:** `bulk_write_no_discrepancies` gains `.limit(_MAX_BULK_TAG_WRITE)` (constant `= 2000`) so the operator-triggered one-shot loop cannot blow up on a large first-time-visible applied backlog at 200K scale (T-85-04 mitigation).
- **D-02 preserved:** `completed_subq = select(TagWriteLog.file_id).where(status == COMPLETED)` anti-join stays verbatim at the bulk builder — idempotency intact, no state-based de-dupe reintroduced.
- **Commit discipline unchanged:** the three `await session.commit()` calls (`tags.py:369/438/475` pre-edit → :375/447/484 post-format) are untouched; the swaps are read-only at the guards. No new commit added.
- **SC#2 behavior test (mutation-checked):** the applied fixture is a real `FileRecord(state='moved')` (deliberately NOT `'executed'`) + a `RenameProposal(status='executed')`; `execute_tag_write` PROCEEDS to a `COMPLETED` write. The non-applied fixture (a `failed` proposal, no executed) RAISES `ValueError(match="executed")`.

## Task Commits

1. **Task 1: gate tag-write path on applied() + bound bulk builder** — `8a9f5f43` (feat)
2. **Task 2: SC#2 mutation-checked applied() guard + migrate EXECUTED fixtures** — `03432cb8` (test)

## Files Modified
- `src/phaze/services/tag_writer.py` — write guard reads `await is_applied(session, file_record.id)`; imports `is_applied` from `phaze.services.stage_status`; dead `FileState` import dropped; docstring reworded to the applied() invariant.
- `src/phaze/routers/tags.py` — 4 `applied_clause()` readers + 1 `is_applied` per-record guard; `_MAX_BULK_TAG_WRITE = 2000` module constant + `.limit(...)` on the bulk builder; `completed_subq` preserved; `FileState` import dropped; bulk docstring updated for the applied()/cap semantics.
- `tests/review/services/test_tag_writer.py` — `TestExecuteTagWrite` reshaped off the `MagicMock().state` guard fixtures: two DB-backed SC#2 guard cases (`test_applied_file_passes_guard`, `test_non_applied_file_raises`) via the `session`/`make_file` conftest fixtures; the four write-mechanics cases now patch `phaze.services.tag_writer.is_applied` to admit explicitly; added `_add_proposal` helper + `ProposalStatus`/`RenameProposal`/`uuid` imports.
- `tests/review/routers/test_tags.py` — `_create_executed_file` migrated to seed a `RenameProposal(status='executed')` (default `state=FileState.MOVED`) with an `applied: bool = True` switch; `test_write_tags_non_executed_rejected` now calls `applied=False` (no executed proposal → 400). Added `ProposalStatus`/`RenameProposal` imports.

## Decisions Made
- **`_MAX_BULK_TAG_WRITE = 2000`** — planner gave discretion on the value; chose a low-thousands batch cap consistent with the in-tree page bounds (`page_size <= 100`, list/link limits) so one operator submit is bounded without truncating realistic backlogs mid-batch. Non-qualifying/uncapped files remain reachable on the next submit (the query re-runs and excludes COMPLETED via `completed_subq`).
- **Write-mechanics tests patch `is_applied` rather than seeding rows** — those cases test the mutagen write/verify/log path, not the guard; patching keeps them fast, DB-free, and self-documenting (the guard admission is explicit, not an incidental AsyncMock-truthiness artifact).

## TDD Gate Compliance
Task 2 is `tdd="true"`. Per the phase's structure (and the plan-01 precedent), the implementation landed in Task 1 (`feat`, `8a9f5f43`) and the load-bearing behavior test in Task 2 (`test`, `03432cb8`) — so there is no strict test-first `test(...)`→`feat(...)` RED gate commit for this seam. The RED→GREEN discipline is instead satisfied by the **mutation check** (memory `feedback_mutation_test_guard_tests`: a green guard proves nothing until you watch it go red), recorded below.

## Mutation Check (SC#2 — RED → GREEN, VERIFIED)
- **RED:** temporarily reverted `tag_writer.py:185` to `if file_record.state != FileState.EXECUTED:` (re-adding the `FileState` import). `test_applied_file_passes_guard` — whose file fixture is `state='moved'` — then RAISED `ValueError: Only executed files can have tags written` and the test **FAILED** (`1 failed`), proving the test actually exercises the guard.
- **GREEN:** restored `if not await is_applied(session, file_record.id):`. The test **PASSED** (observed clean on 2 fresh-DB runs). `git diff --stat` on `tag_writer.py` confirmed the restore is byte-identical to the committed Task 1 source (mutation fully reverted).

## Verification
- `uv run ruff check` + `uv run mypy` clean on both touched source files (no dead `FileState` import; F401-free).
- `grep -c "FileState.EXECUTED"` == 0 across `tag_writer.py` + `tags.py`.
- `grep -c "state=\"executed\"|state='executed'"` == 0 in `test_tags.py`.
- `list_tags` retains its `offset(offset).limit(page_size)` + `Pagination(...)` (not re-paginated); bulk builder retains `TagWriteStatus.COMPLETED` (completed_subq) AND `.limit(`.
- `just test-bucket review` — **426 passed, exit 0** on a clean fresh ephemeral DB.

## Deviations from Plan
None — plan executed as written. (The bulk `.where(...)` and `list_tags` `stmt` were auto-collapsed onto single lines by the `ruff-format` pre-commit hook once the shorter `applied_clause()` calls fit the 150-char limit; cosmetic, no behavior change.)

## Issues Encountered
- **Local colima test-DB flake (environment, NOT code):** the function-scoped `async_engine` conftest fixture recreates the whole schema (`create_all`/`drop_all`) per test; under colima VM pressure — and after any interrupted run leaves the schema dirty — single-test and repeated invocations sporadically error with `CREATE TABLE agents ... type "agents" already exists` / `ConnectionError` / `OSError` at setup (matches memory `reference_local_fullsuite_colima_flake`). Confirmed environment-only: the **known-good** plan-01 `tests/shared/test_applied_clause.py` errors identically under the same conditions. The authoritative signal is the clean single-shot bucket run (**426 passed, exit 0**); the mutation RED→GREEN was captured on fresh-DB single-test runs.

## Threat Flags
None new. T-85-02 (stale/deleted-path tampering) is mitigated exactly as planned — the guard reads `proposals.status=='executed'` (transactionally coupled to the apply path), never `execution_log`. T-85-03 (mutagen path-traversal) is untouched/accepted — no new write surface; the writer was already wired, only the dead predicate changed. T-85-04 (bulk DoS) mitigated by the `.limit(_MAX_BULK_TAG_WRITE)` cap. Zero new packages (T-85-SC).

## User Setup Required
None.

## Next Phase Readiness
- Plans 03/04 consume the same `applied_clause()`/`is_applied()` interface to revive the remaining dead `state == EXECUTED` gates (`services/review.py`, `routers/cue.py`, `routers/tracklists.py`).
- The tag-write path is the milestone's one behavior-reviving, filesystem-mutating seam and is now live + bounded + mutation-verified.

## Self-Check: PASSED

All 4 touched files exist on disk; both task commits (`8a9f5f43`, `03432cb8`) are present in the log.

---
*Phase: 85-executed-gate-revival*
*Completed: 2026-07-10*
