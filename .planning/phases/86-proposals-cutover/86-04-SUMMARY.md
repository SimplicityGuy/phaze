---
phase: 86-proposals-cutover
plan: 04
subsystem: testing
tags: [pytest, proposals, sidecar-03, gap-closure, filerecord-state]

# Dependency graph
requires:
  - phase: 86-proposals-cutover
    provides: "Plan 01 deleted the FileRecord.state cascade from store_proposals"
provides:
  - "Green full tests/review bucket (428 passed) with no test asserting the removed FileRecord.state cascade"
  - "SIDECAR-03 re-verification unblocked (VERIFICATION Gap 1 / truth 9 closed)"
affects: [86-verification, sidecar-03]

# Tech tracking
tech-stack:
  added: []
  patterns: ["Gap-closure plan: fix stale test expectations of deleted behavior, run the FULL bucket as evidence"]

key-files:
  created:
    - .planning/phases/86-proposals-cutover/86-04-SUMMARY.md
  modified:
    - tests/review/services/test_proposal.py

key-decisions:
  - "Removed only the deleted-cascade assertion; kept both positive assertions (pg_insert called, session.execute awaited) so store_proposals' real behavior stays covered"
  - "Kept the harmless file_record MagicMock scaffolding — trimming it is out of scope"

patterns-established:
  - "Gap-closure verification: acceptance requires the FULL `uv run pytest tests/review -q` exit 0, not a single-file run"

requirements-completed: [SIDECAR-03]

# Metrics
duration: 6min
completed: 2026-07-10
---

# Phase 86 Plan 04: Proposals Cutover Gap Closure Summary

**Removed the stale `file_record.state == "proposal_generated"` assertion (and its misleading comment) from `test_creates_rename_proposal_records`, restoring a green full `tests/review` bucket (428 passed) after the SIDECAR-03 cascade deletion.**

## Performance

- **Duration:** ~6 min
- **Tasks:** 2
- **Files modified:** 1 (tests only)

## Accomplishments
- Deleted the `assert file_record.state == "proposal_generated"` assertion that tested the exact `FileRecord.state` cascade Plan 01 removed from `store_proposals`.
- Reworded the inline comment (was: "the file state advances") to state that `store_proposals` issues the upsert only and does NOT touch `FileRecord.state` (SIDECAR-03 cutover).
- Preserved both surviving positive assertions (`mock_pg_insert.assert_called_once()`, `session.execute.assert_awaited()`) so the upsert-writer behavior stays covered (mitigates T-86-04-01).
- Proved the FULL `tests/review` bucket is green via an actual full-bucket run, not a single-file run (mitigates T-86-04-02 / closes Gap 1's false-claim).

## Task Commits

1. **Task 1: Remove the stale file.state cascade assertion and its misleading comment** — `8389e03f` (test)
2. **Task 2: Prove the full tests/review bucket is green** — verification-only, no additional source/test changes required (nothing else in the bucket asserted the deleted cascade).

## Evidence

- **Task 1 (touched file):** `uv run pytest tests/review/services/test_proposal.py -q` → `42 passed, 1 warning in 3.37s` (exit 0).
- **Task 2 (full bucket):** `uv run pytest tests/review -q` → **`428 passed, 5 warnings in 79.79s`**, re-confirmed `REVIEW_BUCKET_EXIT=0`. This is the previously-failing test now passing alongside the 427 that already passed (was `1 failed, 427 passed` at verification time).
- **Acceptance greps:** `grep -n 'file_record.state == "proposal_generated"'` → nothing; `grep -niE "file[_ ]state advances"` → nothing.
- **Tests-only invariant:** `git diff --name-only -- src/` is empty.

## Files Created/Modified
- `tests/review/services/test_proposal.py` — removed stale cascade assertion + reworded inline comment in `TestStoreProposals::test_creates_rename_proposal_records`.

## Decisions Made
- Only the assertion of the DELETED cascade was removed; the two positive assertions were preserved to keep coverage of the real `store_proposals` behavior.
- No production source touched — only a stale test expectation of deleted behavior was in scope.

## Deviations from Plan
None - plan executed exactly as written.

## Issues Encountered
None. No colima full-suite flake observed — the full `tests/review` bucket ran clean (428 passed, 0 failed, 0 setup errors) in a single pass, so no isolation re-run was needed.

## Threat Flags
None — change is test-only, removes an assertion of deleted behavior; introduces no new security surface.

## Next Phase Readiness
- VERIFICATION Gap 1 (truth 9) closed; SIDECAR-03 review-bucket regression resolved.
- Note: VERIFICATION Gap 2 (truth 8 — AST source-scan guard mutation coverage in `tests/shared/test_proposals_cutover_source_scan.py`) is a SEPARATE gap and explicitly NOT in this plan's scope; it remains open for a follow-up plan.

## Self-Check: PASSED
- FOUND: `.planning/phases/86-proposals-cutover/86-04-SUMMARY.md`
- FOUND: commit `8389e03f` (Task 1)

---
*Phase: 86-proposals-cutover*
*Completed: 2026-07-10*
