---
phase: 17-live-set-matching-tracklist-review
plan: 02
subsystem: ui, api
tags: [fastapi, htmx, alpine-js, jinja2, tailwind, arq, tracklist, fingerprint]

requires:
  - phase: 17-live-set-matching-tracklist-review
    provides: Tracklist source/status columns, TracklistTrack confidence, scan_live_set arq task
  - phase: 15-tracklist-integration
    provides: Tracklists page, filter tabs, stats header, tracklist card templates
provides:
  - Scan tab UI for batch file selection and fingerprint scanning
  - Source/status badge partials for visual tracklist differentiation
  - Proposed filter tab and stats column
  - GET/POST /scan and GET /scan/status endpoints on tracklists router
affects: [17-03, tracklist-review-ui, fingerprint-scan-flow]

tech-stack:
  added: []
  patterns: [Alpine.js showScan toggle for non-HTMX tab switching, arq job polling via hx-trigger every 3s, NOT IN subquery for unscanned file filtering]

key-files:
  created:
    - src/phaze/templates/tracklists/partials/scan_tab.html
    - src/phaze/templates/tracklists/partials/scan_progress.html
    - src/phaze/templates/tracklists/partials/source_badge.html
    - src/phaze/templates/tracklists/partials/status_badge.html
  modified:
    - src/phaze/routers/tracklists.py
    - src/phaze/templates/tracklists/list.html
    - src/phaze/templates/tracklists/partials/filter_tabs.html
    - src/phaze/templates/tracklists/partials/stats_header.html
    - src/phaze/templates/tracklists/partials/tracklist_card.html
    - tests/test_routers/test_tracklists.py

key-decisions:
  - "Alpine.js x-data moved to outer container in list.html so filter_tabs and scan-panel share showScan state"
  - "Scan tab uses Alpine.js toggle (not HTMX) per Research Pitfall 6 -- avoids server round-trip for tab switch"
  - "Unscanned file query uses NOT IN subquery on Tracklist.file_id where source='fingerprint'"
  - "Fingerprint-sourced cards hide 1001tracklists-specific actions; approve/reject buttons deferred to Plan 03"

patterns-established:
  - "Source/status badges as includable Jinja2 partials for reuse across card and detail views"
  - "Scan tab lazy-loads via hx-trigger=intersect once when panel becomes visible"

requirements-completed: [FPRINT-03]

duration: 8min
completed: 2026-04-02
---

# Phase 17 Plan 02: Scan Tab UI & Source/Status Badges Summary

**Scan tab with batch file selection, arq-based fingerprint scanning with polling progress, and source/status badge partials on tracklist cards**

## Performance

- **Duration:** 8 min
- **Started:** 2026-04-02T15:31:04Z
- **Completed:** 2026-04-02T15:39:30Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- Scan tab renders batch file selection with checkboxes, select-all, file size/format, and pagination for unscanned audio files
- Source badges (Fingerprint purple, 1001Tracklists blue) and status badges (Proposed yellow, Approved green, Rejected red) on all tracklist cards
- Scan progress polls arq job results every 3s and shows completion with link to Proposed tab
- Proposed filter tab and 4-column stats header with proposed count
- 17 tests passing (6 new + 11 existing)

## Task Commits

Each task was committed atomically:

1. **Task 1: Scan endpoints, proposed filter, and updated stats** - `a05b25c` (feat)
2. **Task 2: Source/status badges, scan tab UI, and updated templates** - `ec34519` (feat)

## Files Created/Modified
- `src/phaze/routers/tracklists.py` - Added scan_tab, trigger_scan, scan_status endpoints; proposed filter; AUDIO_EXTENSIONS constant
- `src/phaze/templates/tracklists/partials/scan_tab.html` - Batch file selection UI with Alpine.js checkboxes and HTMX form
- `src/phaze/templates/tracklists/partials/scan_progress.html` - Polling progress indicator with completion states
- `src/phaze/templates/tracklists/partials/source_badge.html` - Fingerprint/1001Tracklists source badge
- `src/phaze/templates/tracklists/partials/status_badge.html` - Proposed/Approved/Rejected status badge
- `src/phaze/templates/tracklists/list.html` - Alpine.js x-data on outer container, scan-panel with x-show toggle
- `src/phaze/templates/tracklists/partials/filter_tabs.html` - Added Proposed and Scan tabs with showScan toggle
- `src/phaze/templates/tracklists/partials/stats_header.html` - 4-column grid with Proposed count
- `src/phaze/templates/tracklists/partials/tracklist_card.html` - Badge row, conditional action buttons by source
- `tests/test_routers/test_tracklists.py` - 6 new tests for scan tab, trigger, proposed filter, stats

## Decisions Made
- Alpine.js x-data moved to outer container in list.html so filter_tabs and scan-panel share showScan state
- Scan tab uses Alpine.js toggle (not HTMX) per Research Pitfall 6 to avoid server round-trip for tab switch
- Unscanned file query uses NOT IN subquery on Tracklist.file_id where source='fingerprint'
- Fingerprint-sourced cards hide 1001tracklists-specific actions; approve/reject buttons deferred to Plan 03

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Known Stubs
- Fingerprint-sourced tracklist cards show empty action buttons section (approve/reject buttons to be added in Plan 03)

## Next Phase Readiness
- Scan UI complete: users can select files, trigger scans, and see results
- Source/status badges ready for all tracklist views
- Plan 03 can add approve/reject endpoints and fingerprint track detail with inline editing

## Self-Check: PASSED

All files exist. All commits verified.

---
*Phase: 17-live-set-matching-tracklist-review*
*Completed: 2026-04-02*
