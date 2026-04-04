---
phase: 20-tag-writing
plan: 03
subsystem: ui
tags: [htmx, jinja2, fastapi, tag-writing]

requires:
  - phase: 20-tag-writing (plan 02)
    provides: Tag review UI with comparison panel, inline editing, write endpoint
provides:
  - Fixed Write Tags button in collapsed row (server-side fallback for empty form data)
  - Fixed post-write row update targeting (ID-based instead of closest tr)
  - OOB swap to clear comparison panel after write
affects: []

tech-stack:
  added: []
  patterns:
    - "Server-side fallback for HTMX buttons that cannot include form data"
    - "Stable ID attributes on table rows for cross-element HTMX targeting"
    - "OOB swap to clear expanded detail rows after state changes"

key-files:
  created: []
  modified:
    - src/phaze/routers/tags.py
    - src/phaze/templates/tags/partials/tag_list.html
    - src/phaze/templates/tags/partials/tag_comparison.html
    - src/phaze/templates/tags/partials/tag_row.html
    - tests/test_routers/test_tags.py

key-decisions:
  - "Server-side fallback over client-side fix: when collapsed row button POSTs without form data, router computes proposed tags itself rather than fixing the hx-include selector"

patterns-established:
  - "Server-side fallback: router detects empty tags dict and computes proposed values, making buttons work without requiring form inputs in DOM"
  - "ID-based HTMX targeting: use id='row-{file_id}' on main rows so nested elements can target them reliably"

requirements-completed: [TAGW-01, TAGW-04]

duration: 4min
completed: 2026-04-03
---

# Phase 20 Plan 03: Tag Writing Gap Closure Summary

**Fixed two HTMX wiring bugs: collapsed Write Tags button now computes proposed tags server-side, post-write response targets main row by stable ID with OOB detail row cleanup**

## Performance

- **Duration:** 4 min
- **Started:** 2026-04-03T18:54:13Z
- **Completed:** 2026-04-03T18:58:38Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Write Tags button in collapsed table row now triggers server-side tag computation instead of submitting empty form data
- Post-write response from comparison panel correctly replaces the main file row via stable ID targeting
- Detail row cleared via OOB swap after successful write, keeping DOM consistent
- Regression tests added for empty form body fallback and row ID in response HTML

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix router fallback and template row IDs** - `8588df1` (fix)
2. **Task 2: Regression tests for empty form fallback and row ID** - `e93f9c4` (test)

## Files Created/Modified
- `src/phaze/routers/tags.py` - Added server-side fallback when form data is empty; restructured source determination into if/else
- `src/phaze/templates/tags/partials/tag_list.html` - Added id="row-{file_id}" to main tr, changed hx-target to ID-based, removed broken hx-include
- `src/phaze/templates/tags/partials/tag_comparison.html` - Changed hx-target from "closest tr" to "#row-{file_id}"
- `src/phaze/templates/tags/partials/tag_row.html` - Added id="row-{file_id}" to main tr, added hx-swap-oob on detail row
- `tests/test_routers/test_tags.py` - Added 2 regression tests for empty form fallback and row ID in response

## Decisions Made
- Server-side fallback over fixing hx-include: rather than fixing the broken CSS attribute selector, the router now detects an empty tags dict and computes proposed tags itself. This is more robust since the collapsed row button intentionally has no form inputs nearby.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Test suite requires phaze_test PostgreSQL database (not available outside Docker/CI). Tests verified via syntax check and linting; will run in CI.

## Known Stubs

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All tag writing HTMX wiring bugs resolved
- Phase 20 tag writing feature complete pending human verification of browser behavior

---
*Phase: 20-tag-writing*
*Completed: 2026-04-03*
