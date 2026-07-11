---
phase: 86-proposals-cutover
plan: 01
subsystem: database
tags: [proposals, filestate, sqlalchemy, postgres, review-workflow]

# Dependency graph
requires:
  - phase: 85-executed-gate-revival
    provides: "is_applied(session, file_id) — the stable apply-outcome predicate over proposals.status == 'executed', reused here instead of any file.state read"
  - phase: 78-derivation-layer
    provides: "stage_status.py single-source predicate module hosting is_applied()"
provides:
  - "proposal.py store_proposals with the _TERMINAL_FILE_STATES frozenset + file-load-and-guard block deleted (D-01 site 1); proposals.status is sole authority"
  - "proposal_queries.py update_proposal_status + bulk_update_status writing only proposals.status (D-01 sites 2, 3); FileState import dropped from both modules"
  - "D-03 regression test proving a stale store_proposals batch over an executed file leaves the executed row untouched and is_applied() stays True"
affects: [86-03-source-scan-guard, 87-operator-ui, 90-destructive-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Independent-session assertion after await session.commit() (conftest override reads uncommitted rows — project_get_session_never_commits)"
    - "is_applied(session, file_id) reused as the apply-outcome predicate in place of any file.state read"

key-files:
  created: []
  modified:
    - src/phaze/services/proposal.py
    - src/phaze/services/proposal_queries.py
    - tests/shared/core/test_proposals_upsert.py
    - tests/review/services/test_proposal_queries.py

key-decisions:
  - "Removed the dead select(FileRecord) load in store_proposals entirely (it existed only to write file.state); the pg_insert on_conflict_do_update upsert stays intact as the sole protection via uq_proposals_file_id_pending"
  - "Narrowed (not deleted) the two review *_sets_file_state tests to proposals.status coverage so REJECTED-single and APPROVED-bulk status paths keep a test"
  - "Reworded (not just deleted) three stale docstrings that referenced the removed file.state cascade so grep -c FileState returns 0 and the Plan-03 AST guard sees clean source"

patterns-established:
  - "Writer-deletion cutover: delete the mirror-write, keep the single-authority write, prove the removed regression via an is_applied() independent read"

requirements-completed: [SIDECAR-03]

# Metrics
duration: ~35min
completed: 2026-07-11
---

# Phase 86 Plan 01: Proposals Cutover (Service Layer) Summary

**Deleted the service-layer half of the proposal → FileRecord.state cascade (D-01 sites 1, 2, 3) so proposals.status is the sole review-decision authority; the drift-prone file.state mirror whose _TERMINAL_FILE_STATES frozenset omitted MOVED/UNCHANGED (the MOVED-regression bug) is gone, proven dissolved by a D-03 regression test.**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-07-11T00:50Z
- **Completed:** 2026-07-11T01:30Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- `store_proposals` (proposal.py) no longer loads a FileRecord or writes `file.state`; the `_TERMINAL_FILE_STATES` frozenset and its file-load-and-guard block are deleted and the `FileState` import is dropped. The `pg_insert(...).on_conflict_do_update(index_where=status=='pending')` upsert is intact — the `uq_proposals_file_id_pending` partial index is now the sole protection.
- `update_proposal_status` + `bulk_update_status` (proposal_queries.py) write only `proposals.status`; the APPROVED/REJECTED `file.state` limbs, the `file_id` subquery, and the `update(FileRecord).values(state=...)` are gone. `FileState` import dropped.
- New D-03 test `test_stale_batch_does_not_disturb_executed_file`: seeds an EXECUTED RenameProposal, runs a stale `store_proposals` batch, and asserts from an independent read after commit that the executed row is untouched and `await is_applied(session, file_id) is True` — authority moved to proposals, MOVED-regression dissolved.

## Task Commits

1. **Task 1: Delete proposal.py state cascade + add the D-03 regression test** — `216e14f9` (feat)
2. **Task 2: Delete proposal_queries.py APPROVED/REJECTED file.state limbs + adapt its tests** — `b9a91ee6` (feat)

## Files Created/Modified
- `src/phaze/services/proposal.py` — removed `_TERMINAL_FILE_STATES` frozenset + the file-load-and-guard block in `store_proposals`; dropped the `FileState` import; trimmed the stale "Also transitions each file's state" docstring line. `FileRecord`/`select` kept (still used by `build_file_context`/`load_companion_contents`).
- `src/phaze/services/proposal_queries.py` — removed the `.file.state` cascade in `update_proposal_status` and the file_state derivation + subquery + `update(FileRecord)` in `bulk_update_status`; dropped `FileState`; reworded two stale docstrings.
- `tests/shared/core/test_proposals_upsert.py` — added `test_stale_batch_does_not_disturb_executed_file` (imports `is_applied`), replacing `test_rerun_does_not_regress_terminal_file_state` (premise deleted); dropped the `PROPOSAL_GENERATED` assertion from `test_fresh_insert_stamps_pk`. `FileState` import kept (used by `_seed_file`).
- `tests/review/services/test_proposal_queries.py` — narrowed `test_update_proposal_status_reject` and `test_bulk_update_status_approve` to `proposals.status` assertions; deleted the duplicate `*_approve_sets_file_state` test (covered by `test_update_proposal_status_approve`). `FileState`/`FileRecord` imports kept (used by `_create_proposal`).

## Decisions Made
- Removed the dead `select(FileRecord)` load in `store_proposals` entirely rather than leaving an unused fetch — it existed only to write state (Claude's-discretion resolved to remove per D-04).
- Narrowed the two review `*_sets_file_state` tests instead of outright deleting so REJECTED-single-update and APPROVED-bulk-update `proposals.status` paths keep explicit coverage.

## Deviations from Plan

None — plan executed exactly as written. (Three stale docstrings that referenced the removed cascade were reworded as part of the same deletions — ordinary hygiene within scope, not a functional deviation. The `NO FileState transition` wording in `update_proposal_fields` was changed to `no status change` so the Task 2 `grep -c "FileState" == 0` acceptance holds.)

## D-03 Mutation-Verify Observation (honest record)

The plan is explicit that Wave 1 has **no automated drift detector** — the load-bearing RED→restore AST source-scan guard lands in Plan 03 (86-03). What was verified here is a **lint/type sanity check only**, and it behaved exactly as the plan predicted:

- Re-adding a bare `file_record.state = FileState.PROPOSAL_GENERATED` write in `store_proposals` **without** restoring the import fails `uv run ruff check` with two `F821 Undefined name` errors (`file_record` and `FileState`) — observed and recorded, then reverted.
- Re-adding the same write **with** the `FileState` import restored would lint clean — no Wave-1 detector would catch it. The D-03 test itself proves `is_applied()` is unaffected either way, documenting that authority has moved to `proposals.status`; it does not, and is not intended to, catch a re-added state mirror.

The rigorous, mutation-verified drift guard (flagging a re-added `file.state` write across all three files, including `.values(state=...)` splats and multi-line SQLAlchemy) is Plan 03's AST guard.

## Issues Encountered
- **Full `tests/shared` bucket run surfaced 1 failure + 7 errors, all in `test_pipeline.py` / `test_pipeline_scans.py`** — unrelated to proposals. Root cause traced to `asyncpg UniqueViolationError: duplicate key ... "pg_type_typname_nsp_index"` — a concurrent enum-type creation race in the shared-session schema setup, reproducible in isolation without any proposal code and matching the known local/CI-bucket isolation flake (`reference_local_fullsuite_colima_flake`, `reference_ci_bucket_isolation`). The proposal-scoped test files are green: `test_proposals_upsert.py` 5/5, `test_proposal_queries.py` 29/29.
- **Just recipes `test-file`/`test-bucket` do not export `TEST_DATABASE_URL`** (only `integration-test` does), so tests defaulted to port 5432 and failed to connect. Ran pytest with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL` exported against the `just test-db` ephemeral Postgres on 5433 (MIGRATIONS_TEST_DATABASE_URL port footgun noted in memory).

## Verification
- `uv run ruff check` clean on all four touched files (no F401 on trimmed `FileState` imports); `uv run mypy .` clean via the pre-commit hook on both task commits.
- Greps: `proposal.py` — `_TERMINAL_FILE_STATES`=0, `FileState`=0, `FileRecord`=4, `select(FileRecord)` in `store_proposals`=0 (the one remaining is in `load_companion_contents`). `proposal_queries.py` — `FileState`=0, `FileRecord`=3, `.file.state`=0, `update(FileRecord)...values(state=`=0, `return int(cursor_result.rowcount)`=1.
- `test_proposals_upsert.py::test_stale_batch_does_not_disturb_executed_file` passes and calls `is_applied(session, file_id)`.

## Next Phase Readiness
- SIDECAR-03 service-layer half complete. `agent_proposals.py` router cutover (D-01 site "5"/"4") and the Plan-03 AST source-scan guard remain for the rest of Phase 86.
- Plan 03's AST guard should assert clean absence (`== []`) across `proposal.py`, `proposal_queries.py`, and `agent_proposals.py` — the first two are confirmed clean of any `FileState.<member>` occurrence or `.state` Store/Load node after this plan.

## Self-Check: PASSED

All 4 modified files present on disk; all 3 commits (`216e14f9`, `b9a91ee6`, `f9342e60`) exist in git history.

---
*Phase: 86-proposals-cutover*
*Completed: 2026-07-11*
