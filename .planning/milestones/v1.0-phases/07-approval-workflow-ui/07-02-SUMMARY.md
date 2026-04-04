---
phase: 07-approval-workflow-ui
plan: 02
subsystem: ui
tags: [fastapi, htmx, alpine, jinja2, tailwind, oob-swap, keyboard-nav]

# Dependency graph
requires:
  - phase: 07-approval-workflow-ui
    provides: Proposal list page with pagination, filtering, stats bar, and template infrastructure
provides:
  - Approve/reject/undo endpoints returning HTMX-compatible template responses
  - OOB stats bar updates on every action
  - Toast notifications with 5-second auto-dismiss and undo
  - Expandable row detail panel with AI reasoning, metadata, original path
  - Bulk approve/reject via checkbox selection
  - Keyboard navigation (arrows, a, r, e) via Alpine.js
affects: [07-03-plan, approval-workflow]

# Tech tracking
tech-stack:
  added: []
  patterns: [htmx-oob-swap, alpine-keyboard-nav, toast-auto-dismiss, bulk-form-actions]

key-files:
  created:
    - src/phaze/templates/proposals/partials/row_detail.html
    - src/phaze/templates/proposals/partials/toast.html
    - src/phaze/templates/proposals/partials/approve_response.html
    - src/phaze/templates/proposals/partials/undo_response.html
    - src/phaze/templates/proposals/partials/bulk_actions.html
  modified:
    - src/phaze/routers/proposals.py
    - src/phaze/services/proposal_queries.py
    - src/phaze/templates/proposals/list.html
    - src/phaze/templates/proposals/partials/proposal_table.html
    - src/phaze/templates/proposals/partials/proposal_row.html

key-decisions:
  - "Used Any type annotation for bulk_update cursor result to work around SQLAlchemy async Result type missing rowcount attribute"
  - "Placed Alpine x-data on proposal-list-container (not inside table) to survive HTMX swaps per RESEARCH Pitfall 4"
  - "Detail rows use hidden class toggled by inline script on load rather than Alpine to work with HTMX innerHTML swap"

patterns-established:
  - "OOB swap pattern: primary response includes row update + OOB divs for stats bar and toast container"
  - "Alpine.js proposalTable component with keyboard shortcuts and Set-based row selection"
  - "Toast notification with x-init setTimeout auto-dismiss and inline undo button"

requirements-completed: [APR-02]

# Metrics
duration: 9min
completed: 2026-03-29
---

# Phase 7 Plan 2: Interactive Approval Workflow Summary

**HTMX approve/reject/undo with OOB stats updates, expandable row details, bulk actions, keyboard navigation, and toast notifications**

## Performance

- **Duration:** 9 min
- **Started:** 2026-03-29T03:56:56Z
- **Completed:** 2026-03-29T04:05:34Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- Added five new router endpoints (approve, reject, undo, detail, bulk) with proper HTTP methods and error handling
- Created five new template partials for row details, toast, approve/undo responses, and bulk action bar
- Implemented full keyboard navigation with Alpine.js (arrows, a/r/e shortcuts) and checkbox-based row selection
- Wired OOB swap pattern for live stats bar updates after every approve/reject/undo action

## Task Commits

Each task was committed atomically:

1. **Task 1: Add approve/reject/undo/detail/bulk endpoints to proposal router** - `d2af7c5` (feat)
2. **Task 2: Create interactive template partials** - `fb4a3b1` (feat)

## Files Created/Modified
- `src/phaze/routers/proposals.py` - Added approve, reject, undo, detail, and bulk endpoints
- `src/phaze/services/proposal_queries.py` - Added update_proposal_status, bulk_update_status, get_proposal_with_file
- `src/phaze/templates/proposals/list.html` - Added Alpine.js proposalTable component with keyboard nav
- `src/phaze/templates/proposals/partials/proposal_table.html` - Added select-all checkbox with Alpine binding
- `src/phaze/templates/proposals/partials/proposal_row.html` - Added approve/reject/detail buttons, Alpine checkbox, focus styling
- `src/phaze/templates/proposals/partials/row_detail.html` - Expandable detail panel with AI reasoning, metadata, path
- `src/phaze/templates/proposals/partials/toast.html` - Auto-dismiss toast with undo button
- `src/phaze/templates/proposals/partials/approve_response.html` - Row update + OOB stats + toast injection
- `src/phaze/templates/proposals/partials/undo_response.html` - Row revert + OOB stats update
- `src/phaze/templates/proposals/partials/bulk_actions.html` - Fixed bottom bar with approve/reject selected

## Decisions Made
- Used `Any` type annotation for `cursor_result` in bulk_update_status to work around SQLAlchemy async Result type lacking rowcount attribute
- Placed Alpine `x-data` on the `#proposal-list-container` div (not inside the table) so keyboard nav and selection state survive HTMX table swaps
- Used inline script in row_detail.html to reveal hidden detail rows after HTMX swap, since Alpine scope lives on parent container

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Ruff TC003 rule required moving `uuid` import into TYPE_CHECKING block in proposal_queries.py due to `from __future__ import annotations` -- resolved by placing import under `if TYPE_CHECKING`
- SQLAlchemy async `Result` type doesn't expose `rowcount` attribute to mypy -- resolved with `Any` annotation on the cursor result

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Interactive approval workflow is complete, ready for Plan 03 (tests and CI integration)
- All HTMX partials support fragment-level testing via direct endpoint calls
- Keyboard navigation and bulk actions ready for user testing

---
*Phase: 07-approval-workflow-ui*
*Completed: 2026-03-29*
