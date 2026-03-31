---
phase: 13-ai-destination-paths
plan: 01
subsystem: ai
tags: [litellm, pydantic, llm-prompt, path-generation, proposal]

# Dependency graph
requires:
  - phase: 06-ai-proposals
    provides: "FileProposalResponse model, store_proposals function, naming.md prompt template"
provides:
  - "proposed_path field on FileProposalResponse Pydantic model"
  - "Path normalization in store_proposals (strip slashes, collapse doubles)"
  - "Directory Path Rules section in naming.md prompt with 3-step decision tree"
affects: [13-02, 13-03, ui-proposals]

# Tech tracking
tech-stack:
  added: []
  patterns: ["3-step LLM decision tree for path categorization (category -> subcategory -> year handling)"]

key-files:
  created: []
  modified:
    - src/phaze/services/proposal.py
    - src/phaze/prompts/naming.md
    - tests/test_services/test_proposal.py

key-decisions:
  - "proposed_path field placed after proposed_filename and before confidence in Pydantic model for logical grouping"
  - "Path normalization (strip slashes, collapse doubles) applied in store_proposals rather than in Pydantic validator to keep model simple for LLM structured output"

patterns-established:
  - "Path normalization pattern: strip leading/trailing slashes + collapse double slashes before DB persistence"

requirements-completed: [PATH-01]

# Metrics
duration: 5min
completed: 2026-03-31
---

# Phase 13 Plan 01: Prompt and Model Extension for Destination Paths Summary

**Extended LLM prompt with 3-step directory path decision tree and added proposed_path field to FileProposalResponse with slash normalization in store_proposals**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-31T20:27:42Z
- **Completed:** 2026-03-31T20:33:04Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Added proposed_path: str | None = None to FileProposalResponse Pydantic model for LLM structured output
- Updated store_proposals to normalize paths (strip leading/trailing slashes, collapse double slashes) before DB persistence
- Extended naming.md prompt template with Directory Path Rules section containing performances/ and music/ category trees
- Added 6 new tests covering path field acceptance, default None, normalization, and persistence

## Task Commits

Each task was committed atomically:

1. **Task 1: Extend FileProposalResponse and store_proposals with proposed_path** - `473b451` (feat) - TDD: RED -> GREEN
2. **Task 2: Extend naming.md prompt with directory path rules** - `a6e1ab8` (feat)

## Files Created/Modified
- `src/phaze/services/proposal.py` - Added proposed_path field to FileProposalResponse, path normalization and persistence in store_proposals
- `src/phaze/prompts/naming.md` - Added Directory Path Rules section with 3-step decision tree and proposed_path in Output Instructions
- `tests/test_services/test_proposal.py` - Added 6 tests for proposed_path (2 model tests + 4 store_proposals tests)

## Decisions Made
- Path normalization applied in store_proposals (not Pydantic validator) to avoid complicating LLM structured output parsing
- proposed_path placed between proposed_filename and confidence in model field order for logical grouping

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- FileProposalResponse now carries proposed_path, ready for Plan 02 (UI display of path proposals)
- naming.md prompt will generate proposed_path values on next LLM batch call
- store_proposals persists normalized paths to RenameProposal.proposed_path column

## Self-Check: PASSED

- All 3 modified files exist on disk
- Commit 473b451 (Task 1) verified in git log
- Commit a6e1ab8 (Task 2) verified in git log
- 42 tests passing, 0 failures
- ruff check + mypy clean

---
*Phase: 13-ai-destination-paths*
*Completed: 2026-03-31*
