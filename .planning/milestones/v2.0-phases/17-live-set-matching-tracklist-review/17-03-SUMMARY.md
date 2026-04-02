---
phase: 17-live-set-matching-tracklist-review
plan: 03
subsystem: ui
tags: [htmx, jinja2, fastapi, inline-edit, tracklist-review, fingerprint]

requires:
  - phase: 17-02
    provides: Tracklist card with source/status badges, scan tab, filter tabs
  - phase: 17-01
    provides: Tracklist/TracklistVersion/TracklistTrack models with confidence field, fingerprint source

provides:
  - Inline edit GET/PUT endpoints for track artist, title, timestamp fields
  - Delete track endpoint
  - Approve/reject tracklist status transition endpoints
  - Bulk reject low-confidence tracks endpoint
  - Fingerprint track detail template with confidence badges and inline editing
  - Template routing by tracklist source (fingerprint vs 1001tracklists)

affects: []

tech-stack:
  added: []
  patterns:
    - "HTMX click-to-edit pattern: display cell has hx-get to swap in input, input has hx-put on blur/enter to save and swap back to display"
    - "Template routing by model attribute: get_tracks selects template based on tracklist.source"
    - "Jinja2 template for inline HTML responses to avoid XSS taint warnings from semgrep"

key-files:
  created:
    - src/phaze/templates/tracklists/partials/fingerprint_track_detail.html
    - src/phaze/templates/tracklists/partials/inline_edit_field.html
    - src/phaze/templates/tracklists/partials/inline_display_field.html
    - src/phaze/templates/tracklists/partials/confidence_badge.html
    - src/phaze/templates/tracklists/partials/bulk_actions.html
  modified:
    - src/phaze/routers/tracklists.py
    - src/phaze/templates/tracklists/partials/tracklist_card.html
    - tests/test_routers/test_tracklists.py

key-decisions:
  - "Used Jinja2 template (inline_display_field.html) for save endpoint response instead of inline HTML construction to satisfy semgrep XSS taint analysis"
  - "Approve button hidden for already-approved tracklists; all action buttons hidden for rejected tracklists"
  - "Bulk reject uses hx-vals with JS expression to dynamically read threshold input value"

patterns-established:
  - "Click-to-edit HTMX pattern: GET swaps in input, PUT saves on blur/enter and returns display partial"
  - "Field allowlist validation: EDITABLE_FIELDS set checked before any setattr call"

requirements-completed: [FPRINT-04]

duration: 9min
completed: 2026-04-02
---

# Phase 17 Plan 03: Review Flow UI Summary

**HTMX inline editing, approve/reject status transitions, bulk reject low-confidence tracks, and fingerprint track detail with color-coded confidence badges**

## Performance

- **Duration:** 9 min
- **Started:** 2026-04-02T15:42:18Z
- **Completed:** 2026-04-02T15:51:35Z
- **Tasks:** 2
- **Files modified:** 8

## Accomplishments
- Inline editing for artist, title, and timestamp fields via HTMX click-to-edit with field allowlist validation
- Approve/reject tracklist status transitions with conditional button rendering based on current status
- Bulk reject removes all tracks below a user-specified confidence threshold
- Fingerprint track detail template with color-coded confidence badges (green >=90%, yellow 70-89%, red <70%)
- Template routing: fingerprint-sourced tracklists get fingerprint_track_detail.html, scraped tracklists keep track_detail.html
- 8 new tests, 25 total tracklist router tests, 522 tests passing across full suite (92.35% coverage)

## Task Commits

Each task was committed atomically:

1. **Task 1: Review endpoints** - `3d0366c` (feat)
2. **Task 2: Review flow templates** - `a1aa5e9` (feat)

## Files Created/Modified
- `src/phaze/routers/tracklists.py` - Added 7 new endpoints: inline edit GET/PUT, delete track, approve, reject, bulk reject-low; updated get_tracks for template routing
- `src/phaze/templates/tracklists/partials/fingerprint_track_detail.html` - Track table with confidence badges, inline edit, delete buttons
- `src/phaze/templates/tracklists/partials/inline_edit_field.html` - HTMX input field for click-to-edit
- `src/phaze/templates/tracklists/partials/inline_display_field.html` - Display-mode partial returned after save
- `src/phaze/templates/tracklists/partials/confidence_badge.html` - Color-coded confidence badge (3 tiers)
- `src/phaze/templates/tracklists/partials/bulk_actions.html` - Threshold input and reject button
- `src/phaze/templates/tracklists/partials/tracklist_card.html` - Added approve/reject buttons for fingerprint-sourced cards
- `tests/test_routers/test_tracklists.py` - 8 new tests for all review endpoints

## Decisions Made
- Used Jinja2 template (inline_display_field.html) for save endpoint response instead of inline HTML construction to satisfy semgrep XSS taint analysis
- Approve button hidden for already-approved tracklists; all action buttons hidden for rejected tracklists
- Bulk reject uses hx-vals with JS expression to dynamically read threshold input value

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed XSS taint in save_track_field endpoint**
- **Found during:** Task 1
- **Issue:** Semgrep flagged inline HTML construction with user input as XSS risk
- **Fix:** Created inline_display_field.html Jinja2 template (auto-escapes) and used TemplateResponse instead of raw HTMLResponse
- **Files modified:** src/phaze/routers/tracklists.py, src/phaze/templates/tracklists/partials/inline_display_field.html
- **Verification:** Semgrep reports 0 findings
- **Committed in:** 3d0366c

**2. [Rule 1 - Bug] Fixed unused request parameter lint error**
- **Found during:** Task 1
- **Issue:** Ruff ARG001 flagged unused `request` parameter in delete_track endpoint
- **Fix:** Removed unused parameter
- **Files modified:** src/phaze/routers/tracklists.py
- **Committed in:** 3d0366c

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes necessary for code quality. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Known Stubs
None - all endpoints are fully wired to database operations with real templates.

## Next Phase Readiness
- Phase 17 is now complete (all 3 plans executed)
- Full tracklist review workflow operational: scan, view, edit, approve/reject
- FPRINT-04 requirement satisfied

---
*Phase: 17-live-set-matching-tracklist-review*
*Completed: 2026-04-02*

## Self-Check: PASSED
