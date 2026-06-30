---
phase: 58-enrich-analyze-workspaces
plan: 01
subsystem: ui
tags: [htmx, alpine, jinja2, polling, visibilitychange, pytest, shell]

# Dependency graph
requires:
  - phase: 57-shell-dag-rail
    provides: "shell.html chrome, #stage-workspace swap target, $store.pipeline seed, STAGE_PARTIALS whitelist, fragment-only /s/<stage> routes"
  - phase: 57.1-incremental-window-persistence-live-analyze-progress-signal
    provides: "mid-flight analysis.fine_windows_analyzed/total signal (read by Plan 58-04's WORK-04 bar)"
provides:
  - "Single persistent /pipeline/stats poll wired into v7.0 shell chrome (#pipeline-stats, outside #stage-workspace) — the shell now live-refreshes for the first time"
  - "visibilitychange shed: htmx trigger filter [document.visibilityState === 'visible'] + foreground-resume listener (R-3)"
  - "tests/test_enrich_analyze_workspaces.py — the single Phase-58 test file (2 filled foundation tests + 4 xfail workspace stubs + _seed_file/_seed_analysis/_seed_cloud_job helpers)"
  - "D-02 reconciliation note in 58-UI-SPEC.md (EXTRACT SELECTED + row-checkboxes deferred)"
affects: [58-02, 58-03, 58-04, 59, 60, 61, 62]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Single-poll discipline: exactly one /pipeline/stats poll in persistent chrome; workspace fragments carry NO hx-trigger=every / setInterval and ride the OOB fanout"
    - "htmx-native polling shed via the every-Ns [filter] trigger-filter syntax + a visibilitychange foreground-resume listener (no htmx.process double-timer)"
    - "Phase test scaffold: foundation tests filled in Wave 0, workspace tests land as strict=False xfail stubs converted by later plans"

key-files:
  created:
    - "tests/test_enrich_analyze_workspaces.py"
  modified:
    - "src/phaze/templates/shell/shell.html"
    - ".planning/phases/58-enrich-analyze-workspaces/58-UI-SPEC.md"

key-decisions:
  - "Used the htmx-native every-5s [document.visibilityState === 'visible'] trigger filter for the R-3 shed instead of toggling hx-trigger + htmx.process, avoiding htmx's reprocess double-timer footgun while keeping a single poll element"
  - "#pipeline-stats element is class=hidden: its job is the OOB $store.pipeline / cloud-card fanout, not a visible counts grid (the v7.0 workspaces render their own content)"
  - "visibilitychange listener fires ONE immediate htmx.ajax refresh on foreground so live values catch up without waiting for the next 5s tick"

patterns-established:
  - "Single-poll discipline (WORK-05 / R-2): one chrome poll, zero second loops; structural test guards it"
  - "Phase test scaffold seeded in Wave 0 with xfail workspace stubs"

requirements-completed: [WORK-05]

# Metrics
duration: ~11min
completed: 2026-06-30
---

# Phase 58 Plan 01: Single live-refresh poll + Phase-58 test scaffold Summary

**The v7.0 shell now live-refreshes through exactly one persistent `/pipeline/stats` poll wired into chrome (with a `visibilitychange` shed), and the single Phase-58 test file is seeded with filled foundation tests + xfail workspace stubs.**

## Performance

- **Duration:** ~11 min
- **Started:** 2026-06-30T18:41Z (approx, execution start)
- **Completed:** 2026-06-30T18:49Z
- **Tasks:** 2
- **Files modified:** 3 (1 created, 2 modified)

## Accomplishments
- Closed the load-bearing gap: the Phase-57 shell had NO live poll element (the only poll lived in the never-rendered legacy `pipeline/dashboard.html`). The shell now fires exactly one `/pipeline/stats` request per 5s from persistent chrome (WORK-05 / R-2).
- Added the `visibilitychange` shed (R-3) — the one new client behavior Phase 58 adds to the Phase-57 poll — via htmx's `every 5s [document.visibilityState === 'visible']` trigger filter plus a foreground-resume listener.
- Created the single Phase-58 test file with two FILLED foundation tests (`test_stage_fragment_is_bare`, `test_single_poll_discipline`), four xfail workspace stubs (WORK-01..04), and the `_seed_file` / `_seed_analysis` / `_seed_cloud_job` ORM helpers later plans build on.
- Recorded the D-02 deferral (EXTRACT SELECTED + row-checkboxes) in 58-UI-SPEC.md.

## Task Commits

Each task was committed atomically:

1. **Task 0: Phase-58 test scaffold + D-02 note** - `34d2dfb` (test)
2. **Task 1: wire single persistent poll + visibilitychange shed** - `876de4c` (feat)

**Plan metadata:** (this commit) (docs: complete plan)

## Files Created/Modified
- `tests/test_enrich_analyze_workspaces.py` - Single Phase-58 test file: foundation tests (R-5 fragment bareness + WORK-05 single-poll discipline) filled; WORK-01..04 as strict=False xfail stubs; module-level async seed helpers.
- `src/phaze/templates/shell/shell.html` - Added the hidden `#pipeline-stats` chrome poll (outside `#stage-workspace`) reusing the existing `GET /pipeline/stats` endpoint, plus the `visibilitychange` foreground-resume listener.
- `.planning/phases/58-enrich-analyze-workspaces/58-UI-SPEC.md` - D-02 reconciliation note near the Copywriting "Primary actions" table.

## Decisions Made
- Chose the htmx-native trigger-filter shed (`every 5s [document.visibilityState === 'visible']`) over the plan's example `hx-trigger=none` + `htmx.process` toggle. htmx docs (verified via Context7) confirm the `every <timing> [filter]` polling-filter syntax; this keeps a single poll element and avoids htmx's reprocess double-timer footgun while still satisfying R-3. The visibilitychange listener provides the prompt foreground resume (and the literal `visibilitychange` the WORK-05 test asserts).
- `#pipeline-stats` is `class="hidden"` — its role is the OOB store/cloud-card fanout, not a visible stats grid (legacy `dashboard.html` showed the grid; the v7.0 workspaces do not).

## Deviations from Plan

None - plan executed exactly as written. (The trigger-filter shed is the htmx-native realization of the plan's explicitly-"e.g." example; single-poll discipline, single element, and the visibilitychange behavior are all preserved as specified.)

## Issues Encountered
- The local test database was not running; started the ephemeral Postgres/Redis via `just test-db` (ports 5433/6380) and exported `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` accordingly. Test-environment setup only — no code impact.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The single chrome poll + OOB fanout is in place; Plans 58-02..04 can mount their workspace fragments and OOB targets onto the one poll with no second loop.
- The four xfail workspace tests (`test_discover_workspace`, `test_metadata_trigger_all_wired`, `test_lane_cards_states`, `test_analyze_file_table_lane_and_windows`) and the seed helpers are ready for conversion by 58-02-03 / 58-03-02 / 58-04-02 / 58-04-03.
- Full suite green: 2519 passed, 4 xfailed, 97.24% coverage; ruff + mypy clean.

## Self-Check: PASSED

- `tests/test_enrich_analyze_workspaces.py` exists; commits `34d2dfb` + `876de4c` present.
- `shell.html` carries exactly one `hx-get="/pipeline/stats"` + `visibilitychange` (3 occurrences across element + listener comment).

---
*Phase: 58-enrich-analyze-workspaces*
*Completed: 2026-06-30*
