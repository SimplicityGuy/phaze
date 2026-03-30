---
phase: 11-polish-cleanup
plan: 01
subsystem: api
tags: [sqlalchemy, fastapi, state-machine, file-discovery, execution]

# Dependency graph
requires:
  - phase: 07-approval-ui
    provides: proposal approval/rejection workflow
  - phase: 08-execution
    provides: copy-verify-delete execution service
  - phase: 09-pipeline
    provides: pipeline dashboard and orchestration
provides:
  - FileRecord.state transitions on proposal approval/rejection
  - .opus file discovery via EXTENSION_MAP
  - proposed_path-based destination routing in execution
  - settings_batch_size in pipeline dashboard context
affects: [execution, pipeline, file-discovery]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "State machine transitions coupled to proposal status changes"
    - "Conditional destination routing based on proposed_path presence"

key-files:
  created: []
  modified:
    - src/phaze/services/proposal_queries.py
    - src/phaze/constants.py
    - src/phaze/services/execution.py
    - src/phaze/routers/pipeline.py
    - tests/test_services/test_proposal_queries.py
    - tests/test_constants.py
    - tests/test_services/test_execution.py
    - tests/test_routers/test_pipeline.py

key-decisions:
  - "Import settings at module level in execution.py rather than lazy import inside if-block"
  - "Default proposed_path=None in test helper to avoid MagicMock truthiness breaking existing tests"

patterns-established:
  - "FileRecord state transitions must accompany proposal status changes (both single and bulk)"
  - "Execution destination computed conditionally: proposed_path uses output_path base, None uses source.parent"

requirements-completed: [APR-02, ING-05, EXE-01]

# Metrics
duration: 6min
completed: 2026-03-30
---

# Phase 11 Plan 01: Gap Closure - Code Fixes Summary

**Fixed four v1.0 audit gaps: APPROVED state transition, .opus extension, proposed_path execution routing, and settings_batch_size dashboard injection**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-30T20:16:48Z
- **Completed:** 2026-03-30T20:23:08Z
- **Tasks:** 2
- **Files modified:** 8

## Accomplishments
- FileRecord.state now transitions to APPROVED/REJECTED when proposals are approved/rejected (single and bulk)
- .opus audio format added to EXTENSION_MAP for file discovery
- Execution service uses proposed_path with settings.output_path as base directory when set
- Pipeline dashboard and stats partial expose settings_batch_size in template context

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix APPROVED state transition and .opus extension** - `0e06a2d` (feat)
   - TDD RED: tests included in same commit (changes were pre-staged)
2. **Task 2: Wire proposed_path in execution and inject settings_batch_size**
   - TDD RED: `0e70681` (test) - failing tests for proposed_path and settings_batch_size
   - TDD GREEN: `ce79af2` (feat) - implementation passing all tests

## Files Created/Modified
- `src/phaze/services/proposal_queries.py` - Added FileState import and state transitions in update_proposal_status and bulk_update_status
- `src/phaze/constants.py` - Added .opus to EXTENSION_MAP as FileCategory.MUSIC
- `src/phaze/services/execution.py` - Conditional destination routing using proposed_path with settings.output_path base
- `src/phaze/routers/pipeline.py` - Injected settings_batch_size into dashboard and stats partial contexts
- `tests/test_services/test_proposal_queries.py` - Tests for APPROVED/REJECTED state transitions (single and bulk)
- `tests/test_constants.py` - Test for .opus extension classification, updated extension count
- `tests/test_services/test_execution.py` - Tests for proposed_path destination and None fallback
- `tests/test_routers/test_pipeline.py` - Test for settings_batch_size in dashboard response

## Decisions Made
- Imported settings at module level in execution.py for consistency with other service files
- Set proposed_path=None as default in test helper _make_proposal to prevent MagicMock truthiness from breaking existing tests

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed _make_proposal test helper default for proposed_path**
- **Found during:** Task 2 (proposed_path wiring)
- **Issue:** MagicMock's default attribute returns a truthy MagicMock, causing existing tests to hit the new proposed_path branch and try to create /data/output directories
- **Fix:** Added explicit proposed_path=None parameter to _make_proposal helper
- **Files modified:** tests/test_services/test_execution.py
- **Verification:** All 282 tests pass
- **Committed in:** ce79af2 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Essential fix for test compatibility. No scope creep.

## Issues Encountered
None

## Known Stubs
None - all changes are fully wired with no placeholder data.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All four audit gaps closed
- Full test suite (282 tests) passing
- Ready for remaining Phase 11 plans (11-02, 11-03)

## Self-Check: PASSED

- All 8 modified files exist on disk
- Commits 0e06a2d, 0e70681, ce79af2 all found in git log
- Full test suite (282 tests) passing

---
*Phase: 11-polish-cleanup*
*Completed: 2026-03-30*
