---
phase: 50-push-pipeline
plan: 07
subsystem: ui
tags: [htmx, jinja2, sqlalchemy, dashboard, observability, cloud-window]

# Dependency graph
requires:
  - phase: 50-01
    provides: FileState.PUSHING / FileState.PUSHED enum members
  - phase: 50-06
    provides: services/pipeline.py bounded cloud-window helper (get_cloud_window_count)
provides:
  - get_pushing_count + get_pushed_count degrade-safe per-card counts
  - "Staged (pushing)" + "Analyzing (cloud)" dashboard count cards (D-09)
  - dashboard + 5s stats-poll context wiring for both window counts
affects: [push-pipeline deploy, future D-09 click-through per-file lists]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-card degrade-safe COUNT via _safe_count (rolls back, returns 0 — never 500s the poll)"
    - "OOB count-card contract: identical section id on initial render + hx-swap-oob re-push"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/staged_pushing_card.html
    - src/phaze/templates/pipeline/partials/analyzing_cloud_card.html
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/dashboard.html
    - src/phaze/templates/pipeline/partials/stats_bar.html
    - tests/test_pipeline_dag_context.py
    - tests/test_services/test_pipeline.py

key-decisions:
  - "Per-card counts are observational (degrade-safe via _safe_count); the load-bearing ≤N window cap remains get_cloud_window_count (intentionally NOT degrade-safe so the cron never over-stages)."
  - "Cloned the Phase-49 awaiting_cloud_card pattern verbatim (same markup classes + OOB id contract) rather than introducing new styling."
  - "Task-1 context wiring proven via a TemplateResponse context-capture spy, decoupling the context-key assertion from Task-2 template rendering."

patterns-established:
  - "Window count-card: degrade-safe service count → router surfaces in BOTH dashboard + stats-poll context → partial with {% if oob %}hx-swap-oob{% endif %} included inline (dashboard) and OOB (stats_bar)."

requirements-completed: [CLOUDPIPE-01]

# Metrics
duration: 18min
completed: 2026-06-26
---

# Phase 50 Plan 07: Cloud-Window Count Cards Summary

**The operator can now see the bounded "one ahead" cloud window at a glance — a "Staged (pushing)" (PUSHING) card and an "Analyzing (cloud)" (PUSHED) card — beside the existing "Awaiting cloud" backlog card, refreshed degrade-safely on the same 5s poll.**

## Performance

- **Duration:** ~18 min
- **Tasks:** 2 completed
- **Files modified:** 6 (2 created, 4 modified) + 2 test files

## Accomplishments

### Task 1 — PUSHING/PUSHED count helpers + router surfacing (TDD)
- Added `get_pushing_count` (`state == FileState.PUSHING`, `node="pushing"`) and `get_pushed_count` (`state == FileState.PUSHED`, `node="analyzing_cloud"`) to `services/pipeline.py`, both routed through the existing `_safe_count` (rolls back the aborted transaction and returns 0 on any DB error — the hot 5s poll never 500s).
- Surfaced `pushing_count` + `analyzing_cloud_count` in BOTH the dashboard initial-load context dict and the `/pipeline/stats` poll context dict, mirroring the `awaiting_cloud_count` wiring (service-owns-degrade, no router try/except).
- RED commit (`2a33b21`): service happy-path + degrade-to-0 tests, plus context-capture tests proving both keys ride both contexts. GREEN commit (`689d140`): implementation.

### Task 2 — Count-card partials + dashboard/stats-bar wiring
- Created `staged_pushing_card.html` (`#staged-pushing-card`, label "Staged (pushing)", renders `{{ pushing_count }}`) and `analyzing_cloud_card.html` (`#analyzing-cloud-card`, label "Analyzing (cloud)", renders `{{ analyzing_cloud_count }}`), each cloning `awaiting_cloud_card.html` verbatim including the `{% if oob %}hx-swap-oob="true"{% endif %}` structure.
- Included both inline in `dashboard.html` (outside `#pipeline-stats`) and added both OOB re-push includes in `stats_bar.html` (`{% with oob = True %}…{% endwith %}`). Identical id on initial render + OOB swap is the OOB contract.

## Verification

- `uv run pytest tests/test_pipeline_dag_context.py tests/test_dag_canvas_render.py tests/test_services/test_pipeline.py` — green (new: 5 service tests + 3 context tests).
- `uv run ruff check .` — All checks passed.
- `uv run mypy .` — Success, no issues in 168 source files.
- Visual confirmation of the two cards is the Phase-49-style Manual-Only check in 50-VALIDATION.md.

## Deviations from Plan

**1. [Rule 3 - Blocking] Task-1 tests placed across two files instead of only test_pipeline_dag_context.py**
- **Found during:** Task 1 (TDD RED).
- **Issue:** Asserting the router context keys in isolation at Task 1 (before Task 2 wires the partials) cannot rely on rendered HTML.
- **Fix:** Added a `TemplateResponse` context-capture spy in `tests/test_pipeline_dag_context.py` to assert both keys appear in the dashboard + stats-poll contexts with correct seeded values, and added the degrade-to-0 + happy-path unit tests for the two helpers in `tests/test_services/test_pipeline.py` (their natural home, beside the existing `get_awaiting_cloud_count` tests). All acceptance criteria are met across the two files.
- **Commit:** `2a33b21`

## Known Stubs

None — both counts are wired to live `FileRecord.state` COUNT queries and rendered through context on initial load and the 5s OOB poll.

## Threat Flags

None — no new network/auth/file surface. The two count queries run through `_safe_count` (T-50-poll-500 mitigation: the poll never 500s); the window cap itself stays enforced by the cron from committed FileState, not by these observational cards (T-50-stale-count accepted). No new packages (T-50-SC N/A).

## Self-Check: PASSED

- Created files verified present: staged_pushing_card.html, analyzing_cloud_card.html, 50-07-SUMMARY.md
- Commits verified in git log: 2a33b21 (test RED), 689d140 (feat GREEN), 173828d (feat Task 2)
