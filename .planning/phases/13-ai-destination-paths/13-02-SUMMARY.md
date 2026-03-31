---
phase: 13-ai-destination-paths
plan: 02
subsystem: api, ui
tags: [sqlalchemy, collision-detection, tree-builder, fastapi, jinja2, htmx]

# Dependency graph
requires:
  - phase: 06-ai-proposal-generation
    provides: RenameProposal model with proposed_path column
provides:
  - collision detection service (detect_collisions, get_collision_ids)
  - directory tree builder (build_tree, TreeNode dataclass)
  - GET /preview/ route with collapsible directory tree page
  - collision block template for execution gate
affects: [13-ai-destination-paths]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "SQL GROUP BY collision detection on func.concat(proposed_path, /, proposed_filename)"
    - "Recursive TreeNode dataclass for directory tree building"
    - "Native HTML details/summary for collapsible tree UI"
    - "Jinja2 recursive macro for tree node rendering"

key-files:
  created:
    - src/phaze/services/collision.py
    - src/phaze/routers/preview.py
    - src/phaze/templates/preview/tree.html
    - src/phaze/templates/preview/partials/tree_node.html
    - src/phaze/templates/execution/partials/collision_block.html
    - tests/test_services/test_collision.py
    - tests/test_routers/test_preview.py
  modified:
    - src/phaze/main.py

key-decisions:
  - "Used SQL func.concat for path joining in collision detection rather than application-level grouping"
  - "Used native HTML details/summary elements for tree collapse rather than Alpine.js state management"

patterns-established:
  - "Collision detection via SQL GROUP BY HAVING for O(1) query scaling"
  - "TreeNode dataclass with recursive _count_files for directory tree"

requirements-completed: [PATH-03, PATH-04]

# Metrics
duration: 7min
completed: 2026-03-31
---

# Phase 13 Plan 02: Collision Detection & Tree Preview Summary

**SQL collision detection service, recursive tree builder, and /preview/ route with collapsible directory tree for approved proposals**

## Performance

- **Duration:** 7 min
- **Started:** 2026-03-31T20:27:50Z
- **Completed:** 2026-03-31T20:34:50Z
- **Tasks:** 2
- **Files modified:** 8

## Accomplishments
- Collision detection via SQL GROUP BY finds duplicate destination paths among approved proposals
- get_collision_ids returns affected proposal UUIDs for template collision badges
- build_tree creates nested TreeNode structure from flat proposal paths with recursive file counts
- /preview/ route renders collapsible directory tree with expand/collapse controls and empty state
- Collision block template ready for execution gate integration

## Task Commits

Each task was committed atomically:

1. **Task 1: Create collision detection service and tree builder** - `5970004` (test: RED) -> `a0827df` (feat: GREEN)
2. **Task 2: Create preview route, tree templates, and collision block** - `8019788` (feat)

## Files Created/Modified
- `src/phaze/services/collision.py` - Collision detection SQL queries and tree builder with TreeNode dataclass
- `src/phaze/routers/preview.py` - GET /preview/ route rendering directory tree page
- `src/phaze/templates/preview/tree.html` - Full tree preview page with empty state and expand/collapse
- `src/phaze/templates/preview/partials/tree_node.html` - Recursive Jinja2 macro for tree nodes
- `src/phaze/templates/execution/partials/collision_block.html` - Orange collision warning block
- `src/phaze/main.py` - Added preview router registration
- `tests/test_services/test_collision.py` - 11 tests for collision detection and tree builder
- `tests/test_routers/test_preview.py` - 3 integration tests for preview route

## Decisions Made
- Used SQL func.concat for path joining in collision detection -- database handles 200K rows efficiently with GROUP BY
- Used native HTML details/summary elements for tree collapse -- accessible, no JS dependency, progressively enhanced

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required

None - no external service configuration required.

## Known Stubs

None - all functionality is fully wired.

## Next Phase Readiness
- Collision service ready for execution gate integration (plan 03 will wire detect_collisions into start_execution)
- Tree builder and preview route fully operational
- collision_block.html template ready for HTMX swap in execution flow

## Self-Check: PASSED

- All 7 created files verified on disk
- All 3 task commits verified in git history (5970004, a0827df, 8019788)
- 14 tests passing, mypy clean, ruff clean

---
*Phase: 13-ai-destination-paths*
*Completed: 2026-03-31*
