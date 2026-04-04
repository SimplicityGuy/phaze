---
phase: 22-tracklist-integration-fixes
plan: 01
subsystem: ui
tags: [htmx, jinja2, sqlalchemy, fastapi, discogs]

requires:
  - phase: 19-discogs-cross-service-linking
    provides: DiscogsLink model, candidate lifecycle, bulk-link endpoint
  - phase: 21-cue-sheet-generation
    provides: _get_cue_version helper, _cue_version dynamic ORM attribute pattern
provides:
  - has_candidates context variable wired in all card-rendering endpoints
  - _has_candidates dynamic ORM attribute on Tracklist in list views
  - _cue_version computation in _render_tracklist_list
  - Bulk-link All button reachable when candidate DiscogsLinks exist
affects: []

tech-stack:
  added: []
  patterns:
    - "Dual-form template guard: top-level context var + ORM dynamic attr for list vs single renders"

key-files:
  created: []
  modified:
    - src/phaze/routers/tracklists.py
    - src/phaze/templates/tracklists/partials/discogs_bulk_link.html
    - tests/test_routers/test_tracklists.py

key-decisions:
  - "Template checks both has_candidates (context var) and tracklist._has_candidates (ORM attr) via Jinja set/if pattern"
  - "_has_candidates added to both list_tracklists and _render_tracklist_list for full coverage"

patterns-established:
  - "Dual template guard pattern: {% set show_x = (x is defined and x) or (obj._x is defined and obj._x) %} for context vars that appear in both single-card and list renders"

requirements-completed: [DISC-04]

duration: 7min
completed: 2026-04-04
---

# Phase 22 Plan 01: Tracklist Integration Fixes Summary

**Wired has_candidates context variable and _cue_version computation to close DISC-04 bulk-link button gap and CUE badge persistence in list views**

## Performance

- **Duration:** 7 min
- **Started:** 2026-04-04T00:42:27Z
- **Completed:** 2026-04-04T00:49:13Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Bulk-link All button now renders when candidate DiscogsLinks exist (approve, reject, match-discogs, list views)
- CUE version badge persists in list view after undo-link operations
- Five new integration tests covering all has_candidates and _cue_version wiring paths
- All 56 tracklist router tests passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Wire has_candidates and _cue_version in tracklist router** - `bd6a5f2` (feat)
2. **Task 1 fix: Add _has_candidates to list_tracklists** - `533cc5b` (fix)
3. **Task 2: Integration tests for has_candidates and _cue_version wiring** - `115363a` (test)

## Files Created/Modified
- `src/phaze/routers/tracklists.py` - Added _has_candidates helper, wired has_candidates in approve/reject/match-discogs endpoints, added _has_candidates and _cue_version computation in _render_tracklist_list and list_tracklists
- `src/phaze/templates/tracklists/partials/discogs_bulk_link.html` - Updated template guard to check both has_candidates context var and tracklist._has_candidates ORM attr
- `tests/test_routers/test_tracklists.py` - Five new integration tests for has_candidates and _cue_version wiring

## Decisions Made
- Template uses dual-form guard pattern ({% set show_bulk = ... %}) to handle both single-card context variable and list-view ORM attribute
- _has_candidates added to both list_tracklists and _render_tracklist_list for complete coverage

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Added _has_candidates to list_tracklists endpoint**
- **Found during:** Task 2 (test_list_tracklists_has_candidates_in_list failed)
- **Issue:** Plan only specified adding _has_candidates to _render_tracklist_list, but the main list_tracklists endpoint also renders tracklist cards and was missing the computation
- **Fix:** Added identical _has_candidates loop to list_tracklists endpoint
- **Files modified:** src/phaze/routers/tracklists.py
- **Verification:** test_list_tracklists_has_candidates_in_list passes
- **Committed in:** 533cc5b

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Essential for correctness -- without this fix, the HTMX list view would never show the Bulk-link All button. No scope creep.

## Issues Encountered
None

## Known Stubs
None -- all data paths are wired to real queries.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- DISC-04 requirement satisfied -- bulk-link button now reachable
- v3.0 audit gap closed
- No blockers

---
*Phase: 22-tracklist-integration-fixes*
*Completed: 2026-04-04*
