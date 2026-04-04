---
phase: 20-tag-writing
plan: 01
subsystem: database, services
tags: [mutagen, id3, vorbis, mp4, sqlalchemy, alembic, tag-writing, audio-metadata]

requires:
  - phase: 12-metadata
    provides: "extract_tags service and ExtractedTags dataclass"
  - phase: 15-tracklists
    provides: "Tracklist model with artist/event/date fields"
provides:
  - "TagWriteLog audit model with JSONB before/after snapshots"
  - "TagWriteStatus enum (COMPLETED, FAILED, DISCREPANCY)"
  - "Alembic migration 011 creating tag_write_log table"
  - "Tag proposal service with cascade merge (tracklist > metadata > filename)"
  - "Tag writer service with format-aware writing (ID3/Vorbis/MP4)"
  - "Verify-after-write with NFC Unicode normalization"
  - "EXECUTED state gate on tag writing"
affects: [20-tag-writing-plan-02, ui-layer, tag-write-endpoints]

tech-stack:
  added: []
  patterns: [format-aware-tag-writing, verify-after-write, cascade-merge-priority]

key-files:
  created:
    - src/phaze/models/tag_write_log.py
    - src/phaze/services/tag_proposal.py
    - src/phaze/services/tag_writer.py
    - alembic/versions/011_add_tag_write_log.py
    - tests/test_models/test_tag_write_log.py
    - tests/test_services/test_tag_proposal.py
    - tests/test_services/test_tag_writer.py
  modified:
    - src/phaze/models/__init__.py
    - tests/test_models/test_core_models.py

key-decisions:
  - "Mock-based tests for OGG/M4A formats (no valid test files), real MP3 for end-to-end"
  - "Tracklist date.year is fallback-only for year field (does not override metadata year)"

patterns-established:
  - "Format-aware write maps: reverse of read maps in metadata.py"
  - "Verify-after-write pattern: write then re-read with NFC normalization"
  - "Cascade merge: build from lowest priority up, overwrite with higher"

requirements-completed: [TAGW-01, TAGW-02, TAGW-03]

duration: 15min
completed: 2026-04-03
---

# Phase 20 Plan 01: Tag Writing Data Layer Summary

**TagWriteLog audit model, tag proposal cascade merge (tracklist > metadata > filename), and format-aware tag writer with verify-after-write for MP3/OGG/FLAC/OPUS/M4A via mutagen**

## Performance

- **Duration:** 15 min
- **Started:** 2026-04-03T17:47:56Z
- **Completed:** 2026-04-03T18:03:00Z
- **Tasks:** 2
- **Files modified:** 9

## Accomplishments
- TagWriteLog model with JSONB before/after tag snapshots, status tracking, and file_id/status indexes
- Tag proposal service computing merged tags from tracklist, FileMetadata, and filename with correct per-field priority cascade
- Tag writer service writing format-aware tags (ID3 frames, Vorbis comments, MP4 atoms) with verify-after-write and EXECUTED gate
- 44 tests covering model, proposal cascade, writer formats, verify, and async orchestration

## Task Commits

Each task was committed atomically:

1. **Task 1: TagWriteLog model, migration, and tag proposal service** - `d06c083` (test) + `1dbebfa` (feat)
2. **Task 2: Tag writer service with format-aware writing and verify-after-write** - `e672fab` (test) + `4691956` (feat)

_TDD workflow: each task had RED (failing tests) then GREEN (implementation) commits._

## Files Created/Modified
- `src/phaze/models/tag_write_log.py` - TagWriteLog model and TagWriteStatus enum
- `src/phaze/models/__init__.py` - Registered TagWriteLog and TagWriteStatus
- `alembic/versions/011_add_tag_write_log.py` - Migration 011 creating tag_write_log table
- `src/phaze/services/tag_proposal.py` - parse_filename and compute_proposed_tags cascade merge
- `src/phaze/services/tag_writer.py` - write_tags, verify_write, execute_tag_write
- `tests/test_models/test_tag_write_log.py` - 11 model tests
- `tests/test_services/test_tag_proposal.py` - 15 proposal tests
- `tests/test_services/test_tag_writer.py` - 18 writer tests
- `tests/test_models/test_core_models.py` - Updated table count to 13

## Decisions Made
- Used mock-based tests for Vorbis and MP4 format writing (creating valid OGG/M4A test files from scratch is complex), real MP3 files for end-to-end write/verify tests
- Tracklist date provides year only as fallback -- does not override metadata or filename year (per cascade priority design)
- datetime import uses noqa TC003 comment for SQLAlchemy runtime resolution compatibility (follows tracklist.py pattern)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed datetime import for SQLAlchemy runtime resolution**
- **Found during:** Task 2 (tag writer tests)
- **Issue:** Ruff TC003 moved datetime into TYPE_CHECKING block, but SQLAlchemy needs it at runtime for Mapped[] annotation resolution
- **Fix:** Moved datetime back to module-level with noqa TC003 comment, following tracklist.py pattern
- **Files modified:** src/phaze/models/tag_write_log.py
- **Committed in:** e672fab

**2. [Rule 3 - Blocking] Simplified OGG/M4A test fixtures to mock-based approach**
- **Found during:** Task 2 (writer tests)
- **Issue:** Generating valid OGG Vorbis and M4A files from raw bytes failed -- mutagen requires properly structured multi-page OGG containers and full MP4 atom trees
- **Fix:** Used mock-based tests for Vorbis/MP4 format-specific logic, real MP3 files for end-to-end write+verify tests (as suggested by plan fallback guidance)
- **Files modified:** tests/test_services/test_tag_writer.py
- **Committed in:** 4691956

---

**Total deviations:** 2 auto-fixed (1 bug, 1 blocking)
**Impact on plan:** Both fixes necessary for test execution. No scope creep.

## Issues Encountered
None beyond the deviations documented above.

## User Setup Required
None - no external service configuration required.

## Known Stubs
None - all services are fully implemented with real logic.

## Next Phase Readiness
- Tag proposal and writer services ready for Plan 02 UI layer integration
- execute_tag_write provides the single entry point Plan 02 endpoints will call
- TagWriteLog provides audit trail that Plan 02 UI can display

---
*Phase: 20-tag-writing*
*Completed: 2026-04-03*
