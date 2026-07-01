---
phase: 59-identify-workspaces
plan: 01
subsystem: testing
tags: [pytest, sqlalchemy, fastapi, htmx, fingerprint, tracklist, read-only-assembly]

# Dependency graph
requires:
  - phase: 58-enrich-analyze-workspaces
    provides: "get_analyze_stage_files degrade-safe read-only helper precedent + _workspace_scaffold/_file_table/_lane_card partials + tests/test_enrich_analyze_workspaces.py test model"
provides:
  - "tests/test_identify_workspaces.py — Phase-59 test surface (2 foundation tests filled, 4 IDENT-01/02 xfail behavior stubs, module-level ORM seed helpers)"
  - "get_trackid_stage_files(session) — read-only per-file Track-ID identity-signal rows (audfprint/panako badge words + tracklist match-state/confidence)"
  - "get_tracklist_set_rows(session) — read-only per-set Tracklist rows (N/M track coverage + match state)"
affects: [59-02-trackid-workspace, 59-03-tracklist-workspace]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Degrade-safe read-only assembly helper (SAVEPOINT begin_nested -> [] on any error), mirroring get_analyze_stage_files"
    - "Per-engine LEFT-join via aliased(FingerprintResult) keyed on the persisted lowercase engine vocab"
    - "Wave-0 test scaffold: foundation tests filled + behavior tests as xfail stubs that collect cleanly for later waves"

key-files:
  created:
    - "tests/test_identify_workspaces.py"
  modified:
    - "src/phaze/services/pipeline.py"

key-decisions:
  - "done badge keys on FingerprintResult.status == 'success' (tolerating 'completed'), per Pitfall 1 — never derived from get_stage_progress"
  - "D-04 candidate fallback surfaces the system-wide best unlinked candidate (literal D-04 reading); schema has no per-file candidate link, flagged for Plan 59-02 refinement if UI-SPEC requires per-file candidates"
  - "Service-helper approach (not inline _render_stage assembly) chosen per RESEARCH Open-Q1 resolution — gives unit-testable read paths"

patterns-established:
  - "_trackid_engine_badge(status) -> done/failed/pending mapping centralizes the Pitfall-1 vocabulary"
  - "Per-set track coverage via COUNT(confidence) (non-NULL N) / COUNT(id) (total M) over the tracklist's versioned tracks"

requirements-completed: [IDENT-01, IDENT-02]

# Metrics
duration: ~25min
completed: 2026-06-30
---

# Phase 59 Plan 01: Identify Workspaces Foundation Summary

**Phase-59 test scaffold plus two degrade-safe read-only helpers (get_trackid_stage_files / get_tracklist_set_rows) that assemble the per-file Track-ID identity rows and per-set Tracklist coverage rows Plans 02/03 will render.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-06-30T23:18Z
- **Completed:** 2026-06-30T23:43Z
- **Tasks:** 2
- **Files modified:** 2 (1 created, 1 modified)

## Accomplishments
- `tests/test_identify_workspaces.py` created mirroring the Phase-58 analog: 2 foundation tests FILLED (bare-fragment R-5, single-poll WORK-05/R-2), 4 IDENT-01/02 behavior tests as xfail stubs that collect cleanly, plus 5 module-level ORM seed helpers.
- `get_trackid_stage_files` added: one read-only SELECT over the signal-bearing set (music/video files with ≥1 FingerprintResult OR a linked Tracklist) producing per-engine badge words (Pitfall-1 `success`→`done`) + tracklist match-state/confidence (D-04).
- `get_tracklist_set_rows` added: one read-only SELECT, one row per set, with D-07 N/M track coverage from `TracklistTrack.confidence` + match state (D-08).
- Both helpers are degrade-safe (SAVEPOINT → `[]` on any DB error) and read-only (verified by AST grep: no enqueue/commit/add/flush/DDL).

## Task Commits

Each task was committed atomically:

1. **Task 1: Phase-59 test scaffold** - `a04c635` (test)
2. **Task 2: Read-only row-assembly helpers + unit tests** - `02c3661` (feat)

_Note: foundation tests passed against the existing `_STAGE_PLACEHOLDER` fragments today, so Task 1 was a GREEN scaffold-creation step rather than RED/GREEN; Task 2's helper unit tests are RED-then-GREEN within the single commit._

## Files Created/Modified
- `tests/test_identify_workspaces.py` - Phase-59 test surface: foundation tests, IDENT xfail stubs, ORM seed helpers, and 7 helper unit tests (shape, Pitfall-1 success→done, candidate/no-match, degrade-safety for both helpers).
- `src/phaze/services/pipeline.py` - Added `_TRACKID_ENGINE_AUDFPRINT`/`_TRACKID_ENGINE_PANAKO` constants, `_trackid_engine_badge` helper, and the two `async def get_trackid_stage_files` / `get_tracklist_set_rows` read-only assembly helpers; added `and_` + `aliased` imports.

## Decisions Made
- **Pitfall-1 vocabulary:** done ⟺ `status == "success"` (tolerate `"completed"`), failed ⟺ `"failed"`, pending ⟺ no row. Centralized in `_trackid_engine_badge`. Engine join keys are the lowercase persisted `"audfprint"`/`"panako"`.
- **D-04 candidate fallback semantics:** the schema ties a tracklist to a file only via `Tracklist.file_id`; an unlinked candidate (`file_id IS NULL`) is not bound to a specific file. The literal D-04 three-branch rule is implemented as matched (linked) → candidate (system-wide best unlinked) → no match. Documented in the helper docstring as a point Plan 59-02 may refine against UI-SPEC.
- **Per-set track counts** summed across the tracklist's versions (a freshly scraped tracklist has a single version) since seeds/real data may not set `latest_version_id`.

## Deviations from Plan

None - plan executed exactly as written. The two helpers, their dict shapes, the degrade-safe wrapper, the Pitfall-1 guard, and the test scaffold all match the plan's task specs and acceptance criteria.

## Issues Encountered
- The test harness requires Postgres + Redis; the dev DB on :5432 was not running. Started the project's ephemeral containers via `just test-db` (Postgres :5433, Redis :6380) and exported `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` for the test runs (the documented integration-test recipe). No code impact.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Plan 59-02 (Track-ID workspace) can wire `get_trackid_stage_files` into a `_render_stage` `trackid` branch + `_file_table.html`, and convert `test_trackid_table_signals` / `test_trackid_success_renders_done` from xfail to real assertions.
- Plan 59-03 (Tracklist workspace) can wire `get_tracklist_set_rows` + the three step cards, and convert `test_tracklist_step_cards_and_triggers` / `test_tracklist_per_set_coverage`.
- Open point for Plan 59-02: confirm the D-04 candidate-fallback rendering against UI-SPEC empty-state intent (system-wide vs per-file candidate).

## Verification
- `uv run pytest tests/test_identify_workspaces.py -x` → 9 passed, 4 xfailed
- `uv run pytest tests/test_identify_workspaces.py tests/test_shell_routes.py tests/test_dead_template_guard.py` → 16 passed, 4 xfailed
- `uv run mypy src/phaze/services/pipeline.py` → clean
- `uv run ruff check` + `ruff format --check` on both files → clean
- AST read-only grep on both helper bodies → no enqueue/commit/add/flush/DDL

## Self-Check: PASSED

- FOUND: tests/test_identify_workspaces.py
- FOUND: src/phaze/services/pipeline.py
- FOUND: .planning/phases/59-identify-workspaces/59-01-SUMMARY.md
- FOUND commit: a04c635 (Task 1)
- FOUND commit: 02c3661 (Task 2)

---
*Phase: 59-identify-workspaces*
*Completed: 2026-06-30*
