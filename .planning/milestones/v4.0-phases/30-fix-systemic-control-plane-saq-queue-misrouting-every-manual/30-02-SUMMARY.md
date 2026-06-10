---
phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual
plan: 02
subsystem: control-plane-routing
tags: [saq, redis, fastapi, htmx, pipeline, queue-routing]

# Dependency graph
requires:
  - phase: 30-01 (enqueue-routing foundation)
    provides: "resolve_queue_for_task, RoutedQueue, NoActiveAgentError, controller_queue, AgentTaskRouter.queue_for"
provides:
  - "src/phaze/routers/pipeline.py: all 8 trigger handlers route through resolve_queue_for_task; zero app.state.queue references"
  - "Visible no-active-agent empty-state on the 6 per-agent pipeline triggers (JSON enqueued=0+reason / HTMX amber fragment)"
affects: [pipeline.py, trigger_response.html, "Run analysis button"]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Every operator enqueue resolves its destination via resolve_queue_for_task — no handler grabs app.state.queue"
    - "Per-agent triggers catch NoActiveAgentError and render a visible empty-state instead of a silent success"

key-files:
  created: []
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/trigger_response.html
    - tests/test_routers/test_pipeline.py
    - tests/test_routers/test_pipeline_fingerprint.py

key-decisions:
  - "no_active_agent passed explicitly to all HTMX trigger contexts (incl. controller proposals_ui=False) rather than relying on Jinja undefined-falsy"
  - "Single _NO_ACTIVE_AGENT_MESSAGE constant shared by the 3 per-agent JSON handlers for a consistent operator message"
  - "Imported the module (from phaze.services import enqueue_router) so grep gate counts exactly 8 resolve_queue_for_task call-sites, not a 9th import line"

requirements-completed: [QR-01, QR-02]

# Metrics
duration: ~15min
completed: 2026-06-09
---

# Phase 30 Plan 02: Pipeline trigger queue-routing fix Summary

**All eight pipeline trigger handlers (4 JSON `/api/v1/*` + 4 HTMX `/pipeline/*`) now route through `resolve_queue_for_task`, so `process_file`/`extract_file_metadata`/`fingerprint_file` land on the active agent's `phaze-agent-<id>` queue and `generate_proposals` lands on `controller` — eliminating the dead default-queue producer that stranded 11,428 jobs.**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-06-09
- **Completed:** 2026-06-09
- **Tasks:** 2
- **Files modified:** 4 (0 created, 4 modified)

## Accomplishments
- Replaced every `request.app.state.queue` reference in `pipeline.py` (the exact misrouting path of the v4.0.6 incident) with a `resolve_queue_for_task` call — `grep -c "app.state.queue"` now returns 0.
- Controller handlers (`trigger_proposals`, `trigger_proposals_ui`) resolve `generate_proposals` to the always-present `controller` queue (no NoActiveAgentError handling needed).
- Per-agent handlers (analyze, extract-metadata, fingerprint — JSON + HTMX, 6 total) resolve their task to the active agent's queue and catch `NoActiveAgentError`, surfacing a **visible** empty-state: JSON returns `{"enqueued": 0, "message": "No active agent available …"}`; HTMX renders an amber fragment with the same copy.
- Extended `trigger_response.html` with a `no_active_agent` branch (amber) ahead of the existing count/no-files branches.
- Migrated both pipeline test suites off the removed `app.state.queue` attribute onto a fake `controller_queue` + `task_router` capture harness, and added named-queue + 0-agent assertions proving destination targeting and the visible empty-state.

## Task Commits

1. **Task 1: route all 8 pipeline triggers through resolve_queue_for_task** — `d5d78a0` (fix)
2. **Task 2: assert per-endpoint queue targeting + 0-agent empty-state** — `1375d6f` (test)

## Files Created/Modified
- `src/phaze/routers/pipeline.py` (modified) — 8 handlers now resolve via `enqueue_router.resolve_queue_for_task`; 6 per-agent handlers catch `NoActiveAgentError`; added `_NO_ACTIVE_AGENT_MESSAGE`; `pipeline_stats_partial` left untouched.
- `src/phaze/templates/pipeline/partials/trigger_response.html` (modified) — new `{% if no_active_agent %}` amber branch.
- `tests/test_routers/test_pipeline.py` (modified) — fake-queue capture harness + active-agent seeding + drain; per-endpoint queue-name assertions; new 0-agent empty-state tests for analyze/extract/proposals.
- `tests/test_routers/test_pipeline_fingerprint.py` (modified) — same harness; fingerprint queue-name assertion + 0-agent empty-state test.

## Decisions Made
- **Module-import style** (`from phaze.services import enqueue_router`) so the `grep -c "resolve_queue_for_task"` gate counts exactly the 8 call-sites and not a 9th `from … import` line; same for `NoActiveAgentError` (6 `except` lines, no import-line hit).
- **`no_active_agent` passed explicitly** to all HTMX trigger contexts (including controller `proposals_ui=False`) rather than relying on Jinja's undefined-as-falsy, for clarity and robustness.
- **Shared `_NO_ACTIVE_AGENT_MESSAGE` constant** for the three per-agent JSON handlers so the operator message stays consistent.

## Deviations from Plan
None — plan executed exactly as written. The 4 listed files were the only files modified; the helper signatures from Plan 01 were used unchanged.

## Threat Register Outcomes
- **T-30-01 (DoS / data-integrity, misrouted enqueues):** mitigated — 0 `app.state.queue` references remain; tests assert named-queue targeting (`phaze-agent-nox` / `controller`, never `default`).
- **T-30-04 (DoS, silent 0-agent success):** mitigated — per-agent handlers return a visible empty-state and capture zero enqueues; covered by 5 dedicated 0-agent tests.
- **T-30-SC (package installs):** accept — no new packages introduced.

## Issues Encountered
None. Local Postgres was available; all 32 tests in the two suites pass. (Unlike Plan 01's `test_agent_task_router.py`, these suites do not require a live Redis — the fake queues replace SAQ entirely.)

## Verification
- `uv run pytest tests/test_routers/test_pipeline.py tests/test_routers/test_pipeline_fingerprint.py -q` → **32 passed**.
- `grep -c "app.state.queue" src/phaze/routers/pipeline.py` → **0**.
- `grep -c "resolve_queue_for_task" src/phaze/routers/pipeline.py` → **8**.
- `grep -n "NoActiveAgentError" src/phaze/routers/pipeline.py` → 6 per-agent `except` lines.
- `grep -c "app.state.queue ="` across both test files → **0** (migrated).
- `uv run mypy src/phaze/routers/pipeline.py` → Success; repo-wide pre-commit `mypy .` hook passed on both commits.
- `uv run ruff check` (pipeline.py + both test files) → all checks passed.

## Next Phase Readiness
- Pipeline (Plan 02) is done. Plans 03–04 still need the same treatment for the remaining misrouted sites (`tracklists.py`, `scan.py`/`ingestion.py`), which continue to reference the now-removed `app.state.queue` until they land.

## Self-Check: PASSED

All modified files exist on disk; both task commits (`d5d78a0`, `1375d6f`) are present in git history.

---
*Phase: 30-fix-systemic-control-plane-saq-queue-misrouting-every-manual*
*Completed: 2026-06-09*
