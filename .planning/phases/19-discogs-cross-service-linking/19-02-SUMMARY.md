---
phase: 19-discogs-cross-service-linking
plan: 02
subsystem: api, ui
tags: [fastapi, htmx, jinja2, discogs, sqlalchemy, saq]

# Dependency graph
requires:
  - phase: 19-discogs-cross-service-linking
    provides: DiscogsLink model, match_tracklist_to_discogs SAQ task, DiscogsographyClient
provides:
  - Five HTMX endpoints for Discogs match, candidates, accept, dismiss, bulk-link
  - Three new template partials (candidates, match button, bulk-link)
  - Track detail expand toggle for per-track Discogs candidate viewing
  - Tracklist card action bar with Match to Discogs and Bulk-link All buttons
affects: [19-03, discogs-search-integration]

# Tech tracking
tech-stack:
  added: []
  patterns: [htmx-candidate-lifecycle, auto-dismiss-siblings, bulk-accept-top-candidate]

key-files:
  created:
    - src/phaze/templates/tracklists/partials/discogs_candidates.html
    - src/phaze/templates/tracklists/partials/discogs_match_button.html
    - src/phaze/templates/tracklists/partials/discogs_bulk_link.html
  modified:
    - src/phaze/routers/tracklists.py
    - src/phaze/templates/tracklists/partials/track_detail.html
    - src/phaze/templates/tracklists/partials/tracklist_card.html
    - tests/test_routers/test_tracklists.py

key-decisions:
  - "tracklist_id passed as ARG001-suppressed path param for URL namespace consistency"
  - "Bulk-link groups candidates by track_id using defaultdict, accepts highest confidence"
  - "Auto-dismiss siblings on accept via in-memory loop rather than bulk UPDATE for auditability"

patterns-established:
  - "HTMX candidate lifecycle: accept/dismiss with innerHTML swap back to candidates partial"
  - "Conditional template includes: discogs_bulk_link only renders when has_candidates is truthy"

requirements-completed: [DISC-01, DISC-02, DISC-04]

# Metrics
duration: 7min
completed: 2026-04-03
---

# Phase 19 Plan 02: Discogs Matching UI Summary

**Five HTMX endpoints and three template partials for Discogs match triggering, inline candidate review with accept/dismiss, and bulk-link functionality**

## Performance

- **Duration:** 7 min
- **Started:** 2026-04-03T03:52:48Z
- **Completed:** 2026-04-03T04:00:36Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments
- Five new router endpoints: match-discogs, get candidates, accept link, dismiss link, bulk-link
- Accept auto-dismisses all sibling candidates for the same track (D-07)
- Bulk-link accepts highest-confidence candidate per track across all tracks (D-11)
- Three template partials with confidence badges, HTMX interactions, and UI-SPEC styling
- Track detail rows gain per-track purple Discogs expand toggle
- Tracklist card action bar includes Match to Discogs CTA and conditional Bulk-link All

## Task Commits

Each task was committed atomically:

1. **Task 1: Router endpoints for match, candidates, accept, dismiss, and bulk-link** - `5191757` (feat)
2. **Task 2: Template partials for candidates, match button, bulk-link, and track row updates** - `5cfd871` (feat)

## Files Created/Modified
- `src/phaze/routers/tracklists.py` - Five new Discogs endpoints with DiscogsLink import
- `src/phaze/templates/tracklists/partials/discogs_candidates.html` - Inline candidate rows with accept/dismiss buttons
- `src/phaze/templates/tracklists/partials/discogs_match_button.html` - Match to Discogs CTA with queued state
- `src/phaze/templates/tracklists/partials/discogs_bulk_link.html` - Conditional bulk-link button with confirmation
- `src/phaze/templates/tracklists/partials/track_detail.html` - Added Discogs expand toggle and candidate container per track
- `src/phaze/templates/tracklists/partials/tracklist_card.html` - Added match and bulk-link button includes to action bar
- `tests/test_routers/test_tracklists.py` - 12 new Discogs test functions

## Decisions Made
- tracklist_id kept as path param in get_discogs_candidates for URL namespace consistency, suppressed ARG001
- Bulk-link uses defaultdict grouping + in-memory sort rather than SQL subquery for clarity
- Auto-dismiss siblings done via individual status assignment loop for explicitness
- Template partials use Jinja2 conditional rendering (is defined and truthy) for optional context vars

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added tracklist_id to track_detail.html context**
- **Found during:** Task 2 (template updates)
- **Issue:** track_detail.html needs tracklist_id for Discogs expand button HTMX URLs, but get_tracks and bulk_link endpoints didn't pass it
- **Fix:** Added tracklist_id to context dict in both get_tracks and bulk_link_discogs endpoints
- **Files modified:** src/phaze/routers/tracklists.py
- **Committed in:** 5cfd871 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Essential for template rendering. No scope creep.

## Issues Encountered
- Test database (phaze_test) not available locally -- verified code correctness via syntax checks, ruff, and mypy instead

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All Discogs UI endpoints and templates ready for Plan 03 (search integration, end-to-end verification)
- Endpoints follow existing HTMX patterns and return HTML partials consistently
- 12 new test functions ready to run when test database is available

## Self-Check: PASSED

- All 7 key files verified present on disk
- Both task commits (5191757, 5cfd871) verified in git log

---
*Phase: 19-discogs-cross-service-linking*
*Completed: 2026-04-03*
