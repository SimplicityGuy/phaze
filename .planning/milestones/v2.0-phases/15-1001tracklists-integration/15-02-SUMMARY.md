---
phase: 15-1001tracklists-integration
plan: 02
subsystem: tasks, ui
tags: [arq, htmx, jinja2, alpine.js, cron, tracklists, admin-ui]

requires:
  - phase: 15-01
    provides: Tracklist/TracklistVersion/TracklistTrack models, TracklistScraper, TracklistMatcher services
provides:
  - arq task functions for search, scrape, and refresh tracklists
  - Monthly cron job for stale/unresolved tracklist refresh
  - Full HTMX admin UI page at /tracklists/ with card layout, filtering, expand/collapse
  - Navigation link in base template between Duplicates and Audit Log
affects: [17-live-set-matching, tracklist-review]

tech-stack:
  added: []
  patterns: [arq cron job scheduling with monthly cadence, HTMX card expand/collapse with Alpine.js state, OOB stats update on link/unlink actions]

key-files:
  created:
    - src/phaze/tasks/tracklist.py
    - src/phaze/routers/tracklists.py
    - src/phaze/templates/tracklists/list.html
    - src/phaze/templates/tracklists/partials/stats_header.html
    - src/phaze/templates/tracklists/partials/filter_tabs.html
    - src/phaze/templates/tracklists/partials/tracklist_card.html
    - src/phaze/templates/tracklists/partials/tracklist_list.html
    - src/phaze/templates/tracklists/partials/track_detail.html
    - src/phaze/templates/tracklists/partials/search_results.html
    - src/phaze/templates/tracklists/partials/toast.html
    - src/phaze/templates/tracklists/partials/pagination.html
    - tests/test_tasks/test_tracklist.py
    - tests/test_routers/test_tracklists.py
  modified:
    - src/phaze/tasks/worker.py
    - src/phaze/main.py
    - src/phaze/templates/base.html

key-decisions:
  - "arq cron job runs on 1st of every month at 03:00 UTC with run_at_startup=False to avoid refresh storms"
  - "Synchronous-ish search in request handler (2-5s acceptable for manual user action) rather than polling/SSE complexity"
  - "10-second undo toast for auto-linked tracklists matching duplicates UI pattern (D-14, D-23)"

patterns-established:
  - "arq cron_jobs ClassVar pattern on WorkerSettings for scheduled background tasks"
  - "Randomized jitter (60-300s) between batch scrape operations to avoid rate limiting"
  - "Filter tabs with Alpine.js x-data tracking activeTab state, HTMX swapping list content"

requirements-completed: [TL-01, TL-02, TL-03, TL-04]

duration: 60min
completed: 2026-04-01
---

# Phase 15 Plan 02: arq Tasks, Admin UI, and Periodic Refresh Summary

**arq task functions for tracklist search/scrape/refresh with monthly cron job, plus full HTMX admin UI with card layout, filter tabs, expand/collapse tracks, and undo toasts**

## Performance

- **Duration:** ~60 min (across parallel agents and checkpoint)
- **Started:** 2026-04-01T19:30:00Z
- **Completed:** 2026-04-01T20:30:36Z
- **Tasks:** 3 (2 auto + 1 checkpoint)
- **Files modified:** 16

## Accomplishments
- Three arq task functions (search_tracklist, scrape_and_store_tracklist, refresh_tracklists) registered in WorkerSettings with monthly cron job
- Complete HTMX admin UI at /tracklists/ with stats header, filter tabs (All/Matched/Unmatched), card layout with expand/collapse track details
- Four per-card actions: Unlink, Re-scrape, View on 1001tracklists, Find Better Match
- Auto-link undo toast with 10-second timeout matching D-14/D-23 decisions
- Navigation link added between Duplicates and Audit Log per D-19
- 446 tests passing, 93.24% coverage, mypy clean

## Task Commits

Each task was committed atomically:

1. **Task 1: Create arq task functions and register in worker** - `acdd7e8` (feat)
2. **Task 2: Create tracklists router, templates, and navigation link** - `2722001` (feat)
3. **Task 3: Visual verification of Tracklists page** - checkpoint approved, no code commit

## Files Created/Modified
- `src/phaze/tasks/tracklist.py` - arq task functions for search, scrape, and refresh
- `src/phaze/tasks/worker.py` - Updated with new tasks in functions list and cron_jobs
- `src/phaze/routers/tracklists.py` - HTMX endpoints for tracklist management UI
- `src/phaze/main.py` - Router registration for tracklists
- `src/phaze/templates/base.html` - Navigation link added
- `src/phaze/templates/tracklists/list.html` - Main tracklists page
- `src/phaze/templates/tracklists/partials/stats_header.html` - Stats counters
- `src/phaze/templates/tracklists/partials/filter_tabs.html` - All/Matched/Unmatched tabs
- `src/phaze/templates/tracklists/partials/tracklist_card.html` - Card with expand/collapse and actions
- `src/phaze/templates/tracklists/partials/tracklist_list.html` - Card list with empty state
- `src/phaze/templates/tracklists/partials/track_detail.html` - Expanded track listing
- `src/phaze/templates/tracklists/partials/search_results.html` - Search results panel
- `src/phaze/templates/tracklists/partials/toast.html` - Auto-link undo toast (10s)
- `src/phaze/templates/tracklists/partials/pagination.html` - Page navigation
- `tests/test_tasks/test_tracklist.py` - Task function tests
- `tests/test_routers/test_tracklists.py` - Router endpoint tests

## Decisions Made
- arq cron job runs on 1st of every month at 03:00 UTC with run_at_startup=False to avoid refresh storms on deployment
- Search endpoint performs synchronous-ish search in request (2-5s for manual action) rather than adding polling/SSE complexity
- 10-second undo toast for auto-linked tracklists matching the duplicates UI pattern per D-14 and D-23

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Phase 15 complete: all 1001Tracklists integration features shipped (data models, scraper, matcher, tasks, UI, cron refresh)
- Ready for Phase 16 (Fingerprint Service & Batch Ingestion) or Phase 17 (Live Set Matching which depends on both 15 and 16)
- Tracklist models and services available for cross-linking with fingerprint results in Phase 17

---
*Phase: 15-1001tracklists-integration*
*Completed: 2026-04-01*

## Self-Check: PASSED
