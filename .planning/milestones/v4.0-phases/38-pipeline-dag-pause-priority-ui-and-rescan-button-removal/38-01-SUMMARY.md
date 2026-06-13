---
phase: 38-pipeline-dag-pause-priority-ui-and-rescan-button-removal
plan: 01
subsystem: ui
tags: [htmx, jinja2, dag-canvas, pipeline, dead-code-removal]

# Dependency graph
requires:
  - phase: 35-pipeline-determinism-idempotency
    provides: the store-driven DAG canvas (dag_canvas.html) with the Discovery node + Trigger Scan card split
provides:
  - Discovery node is display-only (header + count + bar); no duplicate scan affordance
  - Negative render guard asserting the Rescan anchor is gone (prevents reintroduction)
affects: [38-02, 38-03, dag-canvas, pipeline-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Negative render assertion (string-absence) as a deletion guard in the pure-Jinja render test layer"

key-files:
  created: []
  modified:
    - src/phaze/templates/pipeline/partials/dag_canvas.html
    - tests/test_dag_canvas_render.py

key-decisions:
  - "Removed the unused cta: 'Rescan Files' from the discovery mk() getter (not just the anchor) so no data implies a Discovery-node button still exists; mk's o={} default makes the cta omission safe (label resolves to '')."

patterns-established:
  - "Pattern: guard a UI deletion with a string-absence render assertion (\"X\" not in html) so a future edit cannot silently re-add the affordance"

requirements-completed: [REQ-38-3]

# Metrics
duration: 3min
completed: 2026-06-13
---

# Phase 38 Plan 01: Remove Dead Rescan Anchor Summary

**The duplicate, non-interactive "Rescan Files" scroll anchor is gone from the DAG Discovery node — scanning is now driven solely by the Trigger Scan card, guarded by a negative render assertion.**

## Performance

- **Duration:** ~3 min
- **Started:** 2026-06-13T20:37:07Z
- **Completed:** 2026-06-13T20:39:32Z
- **Tasks:** 2 completed
- **Files modified:** 2

## Accomplishments
- Deleted the dead `<a href="#trigger-scan-heading">Rescan Files</a>` anchor from `node-discovery`; the chip now ends at its `node_bar('discovery', ...)`.
- Cleaned the now-unused `cta: 'Rescan Files'` from the discovery `mk()` getter and corrected the two stale "Rescan" comments (empty-state hint, Discovery section header).
- Added `test_discovery_node_has_no_rescan_anchor` — a negative render guard asserting both `"Rescan Files"` and `href="#trigger-scan-heading"` are absent from the canvas.

## Task Commits

Each task was committed atomically (TDD: RED → GREEN):

1. **Task 1: Add failing negative render assertion** - `73f0f88` (test)
2. **Task 2: Delete Rescan anchor + correct stale comments + clean cta** - `e4d8888` (feat)

**Plan metadata:** (final docs commit)

## Files Created/Modified
- `tests/test_dag_canvas_render.py` - Added `test_discovery_node_has_no_rescan_anchor` negative guard (12 lines).
- `src/phaze/templates/pipeline/partials/dag_canvas.html` - Removed the 2-line Rescan anchor, dropped the discovery `cta`, corrected the `:152` and `:191` comments.

## Decisions Made
- Removed the discovery `cta: 'Rescan Files'` entirely rather than blanking it. The RED test surfaced that the `cta` literal — not just the anchor — contained the "Rescan Files" string, and the plan (Task 2) explicitly called for cleaning it so no data implies a Discovery-node button. `mk(done, total, o = {})` defaults the options object, so `mk(s.discovered, s.discovered)` is safe and `label` resolves to `''` (the discovery node renders no label-bearing element anyway).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None. The 4 DB-backed integration tests in `test_dag_canvas_render.py` error at fixture setup with `Connect call failed ('127.0.0.1', 5432)` because no local Postgres is running in this execution environment — a pre-existing environment dependency, not a regression. All 20 pure-Jinja render/topology/gating tests pass, including the new negative guard and the unchanged `test_gating_triggers_post_only_to_existing_endpoints` (the removed anchor was an `<a href>`, never an `hx-post`, so the exactly-4-hx-post assertion is unaffected).

## Verification
- `uv run pytest tests/test_dag_canvas_render.py` → 20 passed, 4 errors (all 4 = DB-connection-only integration tests, environment-gated).
- `grep -c 'trigger-scan-heading' src/phaze/templates/pipeline/partials/dag_canvas.html` → 0
- `grep -c 'Rescan Files' src/phaze/templates/pipeline/partials/dag_canvas.html` → 0
- Pre-commit (ruff + ruff-format + mypy + bandit) passed on both commits.

## Known Stubs
None. This plan is a pure deletion of a dead affordance.

## TDD Gate Compliance
RED gate (`test` commit `73f0f88`) precedes GREEN gate (`feat` commit `e4d8888`); RED verified failing before implementation, GREEN verified passing after. No REFACTOR commit needed.

## Self-Check: PASSED
- `38-01-SUMMARY.md` exists
- `tests/test_dag_canvas_render.py` exists
- Commit `73f0f88` (RED) present
- Commit `e4d8888` (GREEN) present
