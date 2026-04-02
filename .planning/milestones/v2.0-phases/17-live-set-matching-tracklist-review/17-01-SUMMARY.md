---
phase: 17-live-set-matching-tracklist-review
plan: 01
subsystem: database, tasks
tags: [sqlalchemy, alembic, arq, fingerprint, tracklist, dataclass]

requires:
  - phase: 16-audio-fingerprinting
    provides: FingerprintOrchestrator, CombinedMatch, QueryMatch, FingerprintEngine Protocol
  - phase: 15-tracklist-integration
    provides: Tracklist, TracklistVersion, TracklistTrack models
provides:
  - Tracklist source/status columns for distinguishing fingerprint vs 1001tracklists data
  - TracklistTrack confidence column for fingerprint match scores
  - QueryMatch/CombinedMatch timestamp and resolved metadata fields
  - scan_live_set arq task for fingerprint-to-tracklist pipeline
affects: [17-02, 17-03, live-set-review-ui]

tech-stack:
  added: []
  patterns: [fingerprint-to-tracklist pipeline, re-scan versioning, metadata resolution via FileMetadata join]

key-files:
  created:
    - alembic/versions/008_add_tracklist_source_status_confidence.py
    - src/phaze/tasks/scan.py
    - tests/test_tasks/test_scan.py
  modified:
    - src/phaze/models/tracklist.py
    - src/phaze/services/fingerprint.py
    - src/phaze/tasks/worker.py
    - tests/test_services/test_fingerprint.py

key-decisions:
  - "source_url set to empty string for fingerprint-sourced tracklists (no external URL)"
  - "Re-scan creates new TracklistVersion with incremented version_number via MAX query"
  - "Fixed pre-existing datetime import bug in tracklist model (TYPE_CHECKING block incompatible with SQLAlchemy annotation resolution)"

patterns-established:
  - "Fingerprint-to-tracklist: scan_live_set queries combined_query, resolves metadata, persists Tracklist+Version+Tracks"
  - "Re-scan versioning: check existing external_id, increment version_number, update latest_version_id"

requirements-completed: [FPRINT-03]

duration: 8min
completed: 2026-04-02
---

# Phase 17 Plan 01: Backend Data Layer & Scan Task Summary

**Tracklist source/status columns, track confidence, fingerprint dataclass extensions, and scan_live_set arq task for fingerprint-to-tracklist pipeline**

## Performance

- **Duration:** 8 min
- **Started:** 2026-04-02T15:19:57Z
- **Completed:** 2026-04-02T15:28:00Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments
- Migration 008 adds source, status to tracklists and confidence to tracklist_tracks with backfill defaults
- scan_live_set arq task queries fingerprint DB and creates proposed tracklists with resolved metadata
- Re-scanning creates new version (not duplicate tracklist), retries with exponential backoff
- 45 tests passing across fingerprint and scan test suites

## Task Commits

Each task was committed atomically:

1. **Task 1: Alembic migration + model extensions** - `eb22433` (feat)
2. **Task 2: scan_live_set arq task + worker registration** - `2eb4e3a` (feat)

## Files Created/Modified
- `alembic/versions/008_add_tracklist_source_status_confidence.py` - Migration adding source, status, confidence columns
- `src/phaze/models/tracklist.py` - Extended Tracklist (source, status) and TracklistTrack (confidence)
- `src/phaze/services/fingerprint.py` - Extended QueryMatch/CombinedMatch with timestamp, resolved_artist, resolved_title
- `src/phaze/tasks/scan.py` - scan_live_set arq task: fingerprint query -> tracklist creation
- `src/phaze/tasks/worker.py` - Registered scan_live_set in WorkerSettings.functions
- `tests/test_services/test_fingerprint.py` - 7 new tests for dataclass extensions
- `tests/test_tasks/test_scan.py` - 9 tests for scan_live_set task

## Decisions Made
- source_url set to empty string for fingerprint-sourced tracklists (no external URL to reference)
- Re-scan creates new TracklistVersion with incremented version_number via MAX query on existing versions
- Fixed pre-existing datetime import bug: moved datetime/date from TYPE_CHECKING to module-level with noqa: TC003 since SQLAlchemy resolves Mapped[] annotations at runtime

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed datetime import for SQLAlchemy annotation resolution**
- **Found during:** Task 1 (model extensions)
- **Issue:** Pre-existing bug: `datetime` and `date` imports under `TYPE_CHECKING` block, but SQLAlchemy needs them at runtime for `Mapped[datetime]` and `Mapped[date]` annotation resolution. All tests were failing with `MappedAnnotationError`.
- **Fix:** Moved imports to module level with `# noqa: TC003` comment explaining the SQLAlchemy runtime requirement.
- **Files modified:** src/phaze/models/tracklist.py
- **Verification:** `uv run python -c "from phaze.models.tracklist import Tracklist"` succeeds, ruff passes
- **Committed in:** eb22433 (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Essential fix for any test to run. Pre-existing issue, not caused by plan changes.

## Issues Encountered
None beyond the datetime import fix documented above.

## User Setup Required
None - no external service configuration required.

## Known Stubs
None - all data flows are wired end-to-end within the scan task.

## Next Phase Readiness
- Backend data layer complete for plans 02 (scan API endpoint + results UI) and 03 (tracklist review/approval UI)
- scan_live_set task registered and testable via arq
- Tracklist model supports both source types (1001tracklists, fingerprint) with appropriate status defaults

---
*Phase: 17-live-set-matching-tracklist-review*
*Completed: 2026-04-02*
