---
phase: 21-cue-sheet-generation
plan: 01
subsystem: services
tags: [cue-sheet, timestamp, audio, file-generation]

requires:
  - phase: 19-discogs-cross-service-linking
    provides: DiscogsLink model with accepted status for REM metadata
  - phase: 17-live-set-scanning
    provides: TracklistTrack model with timestamp and position fields
provides:
  - CUE content generation from tracklist data
  - 75fps timestamp conversion (seconds to MM:SS:FF)
  - Timestamp string parsing (HH:MM:SS, MM:SS, raw seconds)
  - Version suffix naming for re-generation
  - UTF-8 BOM file writing
  - CueTrackData dataclass for track input
affects: [21-02-cue-ui]

tech-stack:
  added: []
  patterns: [pure-function-service, dataclass-input, utf8-bom-writing, version-suffix-naming]

key-files:
  created:
    - src/phaze/services/cue_generator.py
    - tests/test_services/test_cue_generator.py
  modified: []

key-decisions:
  - "Path import moved to TYPE_CHECKING block per ruff TCH003 (safe with __future__ annotations)"
  - "CueTrackData uses dataclass not Pydantic -- pure service with no validation overhead"
  - "Track numbering is sequential after filtering (not position-based) per CUE spec"

patterns-established:
  - "Pure function service: generate_cue_content takes structured data, returns string"
  - "Filesystem version scanning: glob for .vN.cue files to determine next version"

requirements-completed: [CUE-01, CUE-02, CUE-03]

duration: 4min
completed: 2026-04-03
---

# Phase 21 Plan 01: CUE Sheet Generator Service Summary

**Pure-Python CUE sheet generator with 75fps timestamp conversion, Discogs REM enrichment, version suffix naming, and UTF-8 BOM file writing**

## Performance

- **Duration:** 4 min
- **Started:** 2026-04-03T21:53:25Z
- **Completed:** 2026-04-03T21:57:44Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 2

## Accomplishments

- CUE content generation with FILE/TRACK/INDEX/REM/TITLE/PERFORMER commands
- 75fps frame conversion (seconds_to_cue_timestamp) with correct 00-74 frame range
- Timestamp string parsing supporting HH:MM:SS, MM:SS, and raw seconds
- Discogs REM comments (GENRE, LABEL, YEAR) per-track from accepted DiscogsLinks
- Version suffix naming (audio.cue, audio.v2.cue, audio.v3.cue)
- UTF-8 BOM encoding via Python's utf-8-sig codec
- 44 unit tests covering all behaviors

## Task Commits

Each task was committed atomically:

1. **RED: Failing tests for CUE generator** - `4dd6a21` (test)
2. **GREEN: Implement CUE generator service** - `da49f60` (feat)

_TDD plan: RED (failing tests) then GREEN (implementation passing all tests)_

## Files Created/Modified

- `src/phaze/services/cue_generator.py` - CUE content generation, timestamp conversion, file writing, version naming
- `tests/test_services/test_cue_generator.py` - 44 unit tests across 6 test classes

## Decisions Made

- Used dataclass (not Pydantic) for CueTrackData -- pure service with no validation overhead needed
- Track numbering is sequential after filtering out timestamp-less tracks, not based on original position
- Path import in TYPE_CHECKING block -- safe with `from __future__ import annotations` since Path is never constructed directly

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Removed unused `field` import and fixed ruff lint errors**
- **Found during:** GREEN phase verification
- **Issue:** Unused `field` import from dataclasses, import sorting (I001), TCH003 for Path, PTH123 for open()
- **Fix:** Removed unused import, moved Path to TYPE_CHECKING, replaced open() with Path.open(), ran ruff --fix for isort
- **Files modified:** src/phaze/services/cue_generator.py
- **Verification:** `uv run ruff check` and `uv run mypy` both clean
- **Committed in:** da49f60

---

**Total deviations:** 1 auto-fixed (1 blocking -- lint errors)
**Impact on plan:** Standard lint cleanup, no scope change.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Known Stubs

None -- all functions are fully implemented with no placeholder logic.

## Next Phase Readiness

- CUE generator service ready for Plan 02 (UI integration)
- All exports available: generate_cue_content, write_cue_file, seconds_to_cue_timestamp, parse_timestamp_string, next_cue_path, CueTrackData
- Router endpoint will call generate_cue_content + write_cue_file for inline CUE generation

---
*Phase: 21-cue-sheet-generation*
*Completed: 2026-04-03*
