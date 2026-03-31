---
phase: 13-ai-destination-paths
plan: 03
subsystem: ui
tags: [htmx, jinja2, collision-detection, proposal-table, execution-gate]

# Dependency graph
requires:
  - phase: 13-ai-destination-paths
    plan: 01
    provides: "proposed_path field on RenameProposal, path normalization in store_proposals"
  - phase: 13-ai-destination-paths
    plan: 02
    provides: "collision service (detect_collisions, get_collision_ids), tree builder, preview router and templates"
provides:
  - "Destination column in proposal approval table (path, No path badge, Collision badge)"
  - "collision_ids computed per page load and passed to proposal row template"
  - "Execution gate blocking batch start when collisions exist"
  - "Preview nav link in base template navigation"
affects: [ui-proposals, ui-execution]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-request collision_ids set injected into Jinja2 template context for row-level collision badges"
    - "Execution gate pattern: detect_collisions before enqueue, return collision_block.html partial if non-empty"

key-files:
  created: []
  modified:
    - src/phaze/templates/proposals/partials/proposal_table.html
    - src/phaze/templates/proposals/partials/proposal_row.html
    - src/phaze/templates/base.html
    - src/phaze/routers/proposals.py
    - src/phaze/routers/execution.py
    - tests/test_routers/test_proposals.py
    - tests/test_routers/test_execution.py

key-decisions:
  - "collision_ids passed as a set of string UUIDs in template context rather than embedding collision logic in templates"
  - "Execution gate returns HTMX partial (collision_block.html) rather than an HTTP error code, preserving inline feedback UX"

patterns-established:
  - "Execution gate pattern: collision check before job enqueue, return HTML partial on block"

requirements-completed: [PATH-02, PATH-03]

# Metrics
duration: 18min
completed: 2026-03-31
---

# Phase 13 Plan 03: Destination Column and Collision Gate UI Summary

**Wired collision detection and proposed_path display into the approval table and execution router, adding a Destination column with three visual states and an execution gate that blocks batch start when duplicate destination paths exist**

## Performance

- **Duration:** 18 min
- **Started:** 2026-03-31T20:58:00Z
- **Completed:** 2026-03-31T21:16:00Z
- **Tasks:** 3 (2 auto + 1 human-verify checkpoint)
- **Files modified:** 7

## Accomplishments

- Added Destination column (7th column total) to the proposal approval table with three display states: path text (truncated to 40 chars with full-path tooltip), gray "No path" badge, and orange "Collision" badge
- Updated list_proposals router to compute collision_ids per page load via get_collision_ids() and inject into Jinja2 template context
- Added Preview nav link to base.html navigation bar between Proposals and Audit Log, using current_page active-state logic
- Added collision gate to start_execution router: calls detect_collisions() before enqueuing arq job; returns collision_block.html HTMX partial if collisions exist
- Added tests covering destination column rendering (path present, null path, Destination header text) in test_proposals.py
- Added tests covering collision gate behavior (blocked when collisions, proceeds when none) in test_execution.py
- Human verified all UI elements at checkpoint: Destination column, No path badge, Preview link, /preview/ tree page, collision block on execution

## Task Commits

Each task was committed atomically:

1. **Task 1: Add Destination column to proposal table and wire collision data** - `6551a74` (feat)
2. **Task 2: Add collision gate to execution start handler** - `f97c1a6` (feat)
3. **Task 3: Human-verify checkpoint** - approved, no code commit

## Files Created/Modified

- `src/phaze/templates/proposals/partials/proposal_table.html` - Added Destination `<th>` after Proposed Filename column header
- `src/phaze/templates/proposals/partials/proposal_row.html` - Added Destination `<td>` with path display, No path badge, and Collision badge states; collision_ids default guard at top
- `src/phaze/templates/base.html` - Added Preview nav link between Proposals and Audit Log
- `src/phaze/routers/proposals.py` - Imported get_collision_ids, computed collision_ids in list_proposals, added to template context
- `src/phaze/routers/execution.py` - Imported detect_collisions and get_session, added session parameter to start_execution, added collision gate before enqueue
- `tests/test_routers/test_proposals.py` - Added tests for destination column header, path rendering, No path badge
- `tests/test_routers/test_execution.py` - Added tests for collision gate block and pass-through behavior

## Decisions Made

- collision_ids passed as a set of string UUIDs into template context (not embedded logic) to keep templates simple and testable
- Execution gate returns an HTMX HTML partial (collision_block.html) rather than an HTTP 409 error, preserving the inline UI feedback pattern used throughout the app

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- PATH-02 (destination column in approval table) and PATH-03 (collision warnings + execution gate) are complete
- Phase 13 is fully complete: all 3 plans executed, all 4 PATH requirements satisfied
- 345 tests passing, 95.84% coverage

## Self-Check: PASSED

- All 7 modified files exist on disk
- Commit 6551a74 (Task 1) verified in git log
- Commit f97c1a6 (Task 2) verified in git log
- 345 tests passing, 0 failures
- 95.84% coverage (exceeds 85% minimum)

---
*Phase: 13-ai-destination-paths*
*Completed: 2026-03-31*
