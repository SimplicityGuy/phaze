---
phase: 21-cue-sheet-generation
plan: 02
subsystem: ui
tags: [cue-sheet, htmx, fastapi, jinja2, tailwind, management-page]

requires:
  - phase: 21-cue-sheet-generation/01
    provides: CUE generator service (generate_cue_content, write_cue_file, parse_timestamp_string, CueTrackData)
  - phase: 20-tag-writing
    provides: Tag router pattern with stats header, HTMX partials, OOB toast pattern
provides:
  - CUE management page at /cue/ with stats and eligible tracklist list
  - Single CUE generation endpoint POST /cue/{id}/generate
  - Batch CUE generation endpoint POST /cue/generate-batch
  - CUE Sheets nav tab between Tags and Audit Log
  - Generate CUE inline button on approved tracklist cards
  - CUE status badges (No CUE, CUE vN, Not Eligible)
affects: []

tech-stack:
  added: []
  patterns: [cue-management-page, cue-status-badge, inline-generate-button]

key-files:
  created:
    - src/phaze/routers/cue.py
    - src/phaze/templates/cue/list.html
    - src/phaze/templates/cue/partials/cue_list.html
    - src/phaze/templates/cue/partials/cue_row.html
    - src/phaze/templates/cue/partials/cue_status.html
    - src/phaze/templates/cue/partials/toast.html
    - tests/test_routers/test_cue.py
  modified:
    - src/phaze/main.py
    - src/phaze/templates/base.html
    - src/phaze/templates/tracklists/partials/tracklist_card.html

key-decisions:
  - "Dropped from __future__ annotations in router to avoid FastAPI uuid runtime resolution issues"
  - "Eligible tracklist query uses subquery for has-timestamp check rather than post-filtering"
  - "CUE version detection via filesystem scan matches Plan 01 next_cue_path pattern"

patterns-established:
  - "CUE management page: stats header + eligible tracklist list with inline actions"
  - "CUE status badge: emerald for generated, gray for no CUE, yellow for not eligible"

requirements-completed: [CUE-01, CUE-02, CUE-03]

duration: 12min
completed: 2026-04-03
---

# Phase 21 Plan 02: CUE UI Router and Management Page Summary

**CUE management page with stats, batch generation, inline tracklist card buttons, and nav tab integration**

## Performance

- **Duration:** 12 min
- **Started:** 2026-04-03T22:00:29Z
- **Completed:** 2026-04-03T22:12:59Z
- **Tasks:** 2 (Task 1 TDD: RED + GREEN, Task 2 auto)
- **Files modified:** 10

## Accomplishments

- CUE management page at /cue/ with 3-column stats header (eligible, generated, missing timestamps)
- Single and batch CUE generation endpoints with Discogs metadata enrichment
- CUE Sheets nav tab positioned between Tags and Audit Log
- Generate CUE inline button on approved tracklist cards
- 10 integration tests covering all endpoints and error cases
- 728 tests passing total, 95.65% coverage

## Task Commits

Each task was committed atomically:

1. **RED: Failing tests for CUE router** - `d2abead` (test)
2. **GREEN: CUE router + templates** - `98f8d16` (feat)
3. **Task 2: Nav tab + tracklist card button** - `e9a7def` (feat)

_TDD Task 1: RED (failing tests) then GREEN (implementation + templates passing all tests)_

## Files Created/Modified

- `src/phaze/routers/cue.py` - CUE management router with list, generate, batch endpoints
- `src/phaze/templates/cue/list.html` - Full CUE management page with stats header
- `src/phaze/templates/cue/partials/cue_list.html` - Tracklist list partial for HTMX swap
- `src/phaze/templates/cue/partials/cue_row.html` - Individual tracklist row with action buttons
- `src/phaze/templates/cue/partials/cue_status.html` - CUE version status badge
- `src/phaze/templates/cue/partials/toast.html` - OOB toast notification partial
- `src/phaze/templates/base.html` - Added CUE Sheets nav tab
- `src/phaze/templates/tracklists/partials/tracklist_card.html` - Added Generate CUE button
- `src/phaze/main.py` - Registered cue router
- `tests/test_routers/test_cue.py` - 10 integration tests

## Decisions Made

- Dropped `from __future__ import annotations` in cue.py to avoid FastAPI runtime uuid resolution issues (tags.py pattern)
- Eligible tracklist query uses SQLAlchemy subquery for timestamp existence check rather than Python post-filtering
- CUE version detection reuses same regex pattern as Plan 01's next_cue_path for consistency

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed mypy no-redef error for `link` variable**
- **Found during:** Task 1 GREEN phase
- **Issue:** `link` variable reused in nested loop, mypy flagged `no-redef`
- **Fix:** Renamed inner variable to `discogs_link`
- **Files modified:** src/phaze/routers/cue.py
- **Verification:** `uv run mypy src/phaze/routers/cue.py` clean
- **Committed in:** 98f8d16

**2. [Rule 3 - Blocking] Removed `from __future__ import annotations` to fix FastAPI uuid resolution**
- **Found during:** Task 1 GREEN phase
- **Issue:** `from __future__ import annotations` caused Pydantic TypeAdapter error on uuid.UUID path params at runtime
- **Fix:** Removed future annotations import, moved uuid out of TYPE_CHECKING (matching tags.py pattern)
- **Files modified:** src/phaze/routers/cue.py
- **Verification:** All 10 tests passing
- **Committed in:** 98f8d16

---

**Total deviations:** 2 auto-fixed (2 blocking -- type errors)
**Impact on plan:** Standard type system fixes, no scope change.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Known Stubs

None -- all endpoints are fully implemented with real database queries and filesystem operations.

## Next Phase Readiness

- CUE generation feature complete: service (Plan 01) + UI (Plan 02)
- All CUE-01, CUE-02, CUE-03 requirements satisfied
- Ready for Phase 21 completion

## Self-Check: PASSED

All 7 created files verified on disk. All 3 commits verified in git log.

---
*Phase: 21-cue-sheet-generation*
*Completed: 2026-04-03*
