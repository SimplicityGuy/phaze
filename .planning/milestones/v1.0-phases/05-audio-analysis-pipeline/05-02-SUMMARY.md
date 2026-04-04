---
phase: 05-audio-analysis-pipeline
plan: 02
subsystem: audio-analysis
tags: [essentia, tensorflow, bpm, mood, genre, arq, process-pool]

requires:
  - phase: 05-01
    provides: "AnalysisResult model, config.models_path, Docker model baking"
  - phase: 04
    provides: "arq worker infrastructure, process pool, Retry backoff"
provides:
  - "Analysis service with 11 model sets (33 TF models) + discogs-effnet genre model"
  - "analyze_file synchronous function for ProcessPoolExecutor"
  - "Mood derivation from 7 mood model sets"
  - "Style derivation from discogs-effnet genre model"
  - "process_file wired to analysis via run_in_process_pool"
  - "AnalysisResult upsert with bpm, musical_key, mood, style, features JSONB"
  - "FileRecord state transition to ANALYZED"
affects: [06-ai-proposal-generation, 07-web-ui]

tech-stack:
  added: [essentia, numpy]
  patterns: [model-registry-dataclass, module-level-cache, mock-at-boundary-testing]

key-files:
  created:
    - src/phaze/services/analysis.py
    - tests/test_services/test_analysis.py
  modified:
    - src/phaze/tasks/functions.py
    - tests/test_tasks/test_functions.py

key-decisions:
  - "Music file type detection uses extension set (mp3, flac, ogg, etc.) rather than single 'music' string"
  - "Model caches at module level for ProcessPoolExecutor worker reuse"
  - "essentia imported at module top with TF_CPP_MIN_LOG_LEVEL suppression"

patterns-established:
  - "Model registry: frozen dataclasses for immutable model config"
  - "Mock-at-boundary: mock essentia.standard module and _get_labels for analysis tests"
  - "Process pool integration: sync function called via run_in_process_pool in async task"

requirements-completed: [ANL-01, ANL-02]

duration: 11min
completed: 2026-03-28
---

# Phase 5 Plan 2: Analysis Service and Pipeline Wiring Summary

**Essentia-based audio analysis service with 34 model registry (33 characteristic + 1 genre), BPM/key/mood/style detection, wired into arq worker via process pool**

## Performance

- **Duration:** 11 min
- **Started:** 2026-03-28T18:56:50Z
- **Completed:** 2026-03-28T19:07:32Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Analysis service with full model registry: 11 model sets (33 TF models) covering mood, danceability, gender, tonality, voice/instrumental, plus discogs-effnet genre model
- BPM detection via RhythmExtractor2013 at 44.1kHz, key detection via KeyExtractor with EDMA profile
- Mood derivation averaging positive-class confidence across 7 mood model variants, style from top genre prediction
- process_file fully wired: fetches file from DB, skips non-music, runs analysis in process pool, upserts AnalysisResult, transitions state to ANALYZED
- 15 total tests (9 analysis service + 6 task function) all passing with mocked essentia boundary

## Task Commits

Each task was committed atomically:

1. **Task 1: Create analysis service with model registry and analyze_file** - `4d85289` (feat)
2. **Task 2: Wire analyze_file into process_file and update task tests** - `90d4146` (feat)

## Files Created/Modified
- `src/phaze/services/analysis.py` - Analysis service: model registry, essentia wrappers, mood/style derivation, analyze_file
- `tests/test_services/test_analysis.py` - 9 unit tests for analysis service with mocked essentia
- `src/phaze/tasks/functions.py` - process_file wired to analysis via run_in_process_pool with DB upsert
- `tests/test_tasks/test_functions.py` - 6 unit tests for process_file with mocked DB and analysis

## Decisions Made
- Used extension set (mp3, flac, ogg, m4a, wav, aiff, wma, aac, opus) for music file type detection rather than checking for a single "music" string -- the ingestion service stores file extension as file_type
- Module-level classifier and label caches for efficient reuse across files in the same ProcessPoolExecutor worker
- Imported essentia at module top level (after TF logging suppression) rather than lazy import -- workers will always need it

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Music file type check adjusted for actual data**
- **Found during:** Task 2
- **Issue:** Plan specified `file_record.file_type != "music"` but ingestion service stores file extension (e.g., "mp3", "flac") not category name
- **Fix:** Used a frozenset of known music extensions for the skip check
- **Files modified:** src/phaze/tasks/functions.py
- **Verification:** test_process_file_skips_non_music passes with file_type="jpg"
- **Committed in:** 90d4146

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Essential for correctness -- plan's check would have skipped all files since no file has file_type="music".

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Known Stubs
None - all data paths are wired end-to-end from arq job through analysis to DB storage.

## Next Phase Readiness
- Audio analysis pipeline is complete from arq job enqueue through process pool execution to AnalysisResult storage
- Features JSONB contains all 34 model predictions for downstream AI proposal generation
- Ready for Phase 6 (AI Proposal Generation) which will consume AnalysisResult data

---
*Phase: 05-audio-analysis-pipeline*
*Completed: 2026-03-28*
