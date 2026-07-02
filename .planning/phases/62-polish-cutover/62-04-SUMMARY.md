---
phase: 62-polish-cutover
plan: 04
subsystem: ui
tags: [htmx, jinja2, fastapi, dead-template-guard, cutover, shell]

# Dependency graph
requires:
  - phase: 57-shell-dag-rail
    provides: SHELL-05 legacy-route 302 redirects + the seeded dead-template AST guard
  - phase: 58-enrich-analyze-workspaces
    provides: discover/metadata/fingerprint/analyze shell workspaces that supersede the tab pages
  - phase: 59-identify-workspaces
    provides: trackid/tracklist shell workspaces
  - phase: 60-review-apply
    provides: propose/rename/tagwrite/move/dedupe/cue shell workspaces + the last placeholder supersession
provides:
  - "v7.0 dead-code cutover: 20 legacy tab-era templates deleted, dead-template guard green with an empty allowlist"
  - "/pipeline/ and /preview/ are now pure 302 redirects into the shell"
  - "base.html reduced to the wave-logo home link + theme toggle (legacy tab bar removed)"
affects: [milestone-audit, docs, future-ui-phases]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Dead-code cutover proven by the dead-template AST guard with a drained _ALLOWLIST (closure logic untouched)"
    - "Legacy content-router GETs: keep the live HX fragment branch, delete only the unreachable non-HX list.html tail"

key-files:
  created:
    - .planning/phases/62-polish-cutover/62-04-SUMMARY.md
  modified:
    - src/phaze/routers/proposals.py
    - src/phaze/routers/tracklists.py
    - src/phaze/routers/tags.py
    - src/phaze/routers/cue.py
    - src/phaze/routers/duplicates.py
    - src/phaze/routers/preview.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/base.html
    - tests/test_dead_template_guard.py

key-decisions:
  - "D-03b honored: kept the 5 content routers' live HX pagination/filter/sort fragments; deleted only the unreachable non-HX list.html tail"
  - "pipeline.py + preview.py became pure redirects (their whole non-HX render path was dead, no live shell HX consumer)"
  - "Dashboard-render tests were retargeted to the surviving shell surfaces (/s/discover, /s/analyze, /pipeline/scans/recent, /pipeline/stats) or deleted when they asserted deleted dag_canvas/dashboard-chrome markup"

patterns-established:
  - "Retarget-vs-delete rule for cutover tests: retarget when the content survives at a shell surface with identical partial markup; delete when the markup was dag_canvas/dashboard-chrome-only (covered by workspace tests)"

requirements-completed: [CUT-02]

# Metrics
duration: ~95min
completed: 2026-07-02
---

# Phase 62 Plan 04: Dead-code cutover (CUT-02) Summary

**v7.0 tab-era UI removed: 20 legacy wrapper/partial templates deleted, /pipeline/ + /preview/ made pure redirects, base.html reduced to logo + theme, and the dead-template guard is green with an empty allowlist — while every live shell/record HX fragment was kept.**

## Performance

- **Duration:** ~95 min
- **Completed:** 2026-07-02
- **Tasks:** 3
- **Files modified:** 9 (7 routers + base.html + guard test); 20 templates + 3 test files deleted; 5 test files reconciled

## Accomplishments
- Removed the unreachable non-HX `return ...list.html` tail from the 5 content routers (proposals/tracklists/tags/cue/duplicates); the **live** HX pagination/filter/sort fragment branch is retained (D-03b).
- Made `/pipeline/` and `/preview/` pure 302 redirects — their whole non-HX render path was dead (no live shell HX consumer) — and cleaned the resulting unused imports/helpers.
- Deleted 20 templates: the 8 tab-era wrappers + the 6-partial orphan cascade + the 6 remaining allowlisted `search/*` partials + `tracklists/partials/toast.html`.
- Drained `_ALLOWLIST` to `frozenset()`; the guard closure logic is byte-for-byte unchanged and `test_no_orphan_templates` is green with 0 orphans.
- Stripped the legacy tab-bar nav block from `base.html`, keeping the wave-logo home link + theme toggle (D-04a); retargeted the default skip link from the now-gone `#proposals-table` to `#main-content` (added that id to `<main>`) so the KEPT audit/agents pages are not dead-ends.
- Reconciled the companion test surface with the deletions; full suite **2565 passed**, coverage **96.89%** (gate 85%), ruff + mypy clean.

## Task Commits

1. **Task 1: Remove dead non-HX render tails + strip base.html tab bar** - `a9e6777` (refactor)
2. **Task 2: Delete wrapper + orphaned partial templates, drain the guard allowlist** - `af4f897` (chore)
3. **Task 3: Reconcile companion tests with the cutover** - `6881016` (test)

**Plan metadata:** _(final docs commit)_

## Files Created/Modified
- `src/phaze/routers/{proposals,tracklists,tags,cue,duplicates}.py` - deleted the unreachable `list.html` tail; kept the live HX fragment return.
- `src/phaze/routers/preview.py` - reduced to a pure `/preview/ -> /s/move` 302 redirect (removed the dead tree render + now-unused imports/helper).
- `src/phaze/routers/pipeline.py` - `dashboard()` is now a pure `/pipeline/ -> /` 302 redirect (dropped the dead `dashboard.html` HX branch); `build_dashboard_context` stays (consumed by the shell Analyze render).
- `src/phaze/templates/base.html` - removed the 8-link legacy tab bar; kept logo + theme; skip link -> `#main-content`.
- `tests/test_dead_template_guard.py` - `_ALLOWLIST = frozenset()`; closure untouched.
- **Deleted templates (20):** `proposals/list.html`, `tracklists/list.html`, `tags/list.html`, `cue/list.html`, `duplicates/list.html`, `preview/tree.html`, `pipeline/dashboard.html`, `search/page.html`, `_partials/cross_fs_fingerprint_notice.html`, `pipeline/partials/dag_canvas.html`, `preview/partials/tree_node.html`, `tags/partials/pagination.html`, `tracklists/partials/filter_tabs.html`, `tracklists/partials/stats_header.html`, `search/partials/{results_content,results_row,results_table,search_form,summary_counts}.html`, `tracklists/partials/toast.html`.
- **Deleted tests (3):** `test_dag_canvas_render.py`, `test_template_helpers/test_cross_fs_fingerprint_notice.py`, `test_routers/test_preview.py` (all targeted only deleted templates/pages).
- **Reconciled tests (5):** `test_routers/test_pipeline.py`, `test_pipeline_dag_context.py`, `test_routers/test_pipeline_scans.py`, `test_routers/test_pipeline_inadmissible.py`, `test_routers/test_pipeline_localqueue.py`.

## Decisions Made
- Kept the live per-router HX branches (D-03b corrected): stripping them would have broken shell pagination/filter/sort (REQUIREMENTS.md line 82).
- Dashboard-render tests were classified by content: retarget when the element survives at a shell surface with the same partial (trigger scan card -> `/s/discover`; recent-scans/status-pills/delete -> `/pipeline/scans/recent`; cloud/admission/inadmissible cards -> `/s/analyze`; straggler/localqueue cards -> `/pipeline/stats` OOB poll; window-count context capture -> `/s/analyze`), and delete when the assertion targeted deleted `dag_canvas`/dashboard-chrome markup (the `nodes.*.blocked` getter, stage-card ids, one-button-per-action, store x-init seeds, the "Pipeline Dashboard" heading, the `/saq` link, the batch-size display, and the "Needs metadata/agent/tracklist" DAG gate copy) — that behavior is covered by the Phase 58/59/60 workspace tests + the surviving POST-endpoint tests.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Pipeline dashboard test blast radius far exceeded the plan's Task 3 estimate**
- **Found during:** Task 3 (companion-test reconciliation)
- **Issue:** The plan's Task 3 anticipated updating only `test_dag_canvas_render.py` + `test_cross_fs_fingerprint_notice.py` and "a few" router-test 302 changes. In reality, making `/pipeline/` a pure redirect (the plan-mandated D-03b cut) broke ~45 tests that `GET /pipeline/` with an `HX-Request` header expecting the now-deleted `dashboard.html`. These span `test_pipeline.py`, `test_pipeline_dag_context.py`, `test_pipeline_scans.py`, `test_pipeline_inadmissible.py`, `test_pipeline_localqueue.py`.
- **Fix:** Retargeted each test to the surviving shell surface that now renders the same partial (see Decisions), and deleted the tests whose assertions were `dag_canvas`/dashboard-chrome-only (superseded by existing workspace tests). Added the shell router to the pipeline-scans `smoke` app and updated the `_capture_context` helper to also patch the shell router's `templates` (window counts now render via `/s/analyze`).
- **Files modified:** the 5 reconciled test files listed above.
- **Verification:** `uv run pytest` -> 2565 passed; coverage 96.89%; ruff + mypy clean.
- **Committed in:** `6881016` (Task 3 commit)

**2. [Rule 3 - Blocking] Unused imports/helpers after the pure-redirect conversions**
- **Found during:** Task 1
- **Issue:** Reducing `preview.py`/`pipeline.dashboard()` to redirects orphaned imports (`Response` in pipeline.py; the query stack + `_count_dirs` + templating in preview.py).
- **Fix:** Removed the dead imports/helper (ruff `--fix` caught the `Response` import; preview.py was rewritten to the minimal redirect form).
- **Verification:** ruff + mypy clean.
- **Committed in:** `a9e6777` (Task 1 commit)

---

**Total deviations:** 2 auto-fixed (both blocking-issue handling within Task 3's "audit router tests... change to expect the 302 or remove" mandate). **Impact:** no scope creep — no backend behavior changed; the reconciliation kept coverage at 96.89%.

## Issues Encountered
- **Supersession gap surfaced (D-05, NOT fixed):** the `/saq` SAQ-monitor link that lived in `dashboard.html` has no counterpart anywhere in the v7.0 shell templates. The `/saq` app is still mounted (`main.py`) and reachable by direct URL, but there is no longer an in-UI link to it. Per D-05 this is surfaced, not closed (adding a shell link would be new capability / scope creep for a later phase).
- **Stale comments (left as-is, harmless):** `src/phaze/routers/shell.py` and `pipeline/partials/stats_bar.html` still contain prose comments referencing `dag_canvas.html`/`dashboard.html` as reachability anchors "until CUT-02". These are comments only (no code/guard impact) and editing shell.py was out of this plan's file scope; noted for a future doc pass.

## User Setup Required
None - presentation-only cutover, no external service configuration.

## Next Phase Readiness
- CUT-02 complete: no orphaned old-UI code; dead-template guard green with an empty allowlist; redirects + live HX branches intact; base.html reduced.
- This was the dependency-strict LAST work item of v7.0. Remaining v7.0 close-out items (milestone audit) can proceed. Flag the `/saq` in-UI-link gap for consideration during audit.

## Self-Check: PASSED

- SUMMARY.md exists on disk.
- Task commits `a9e6777`, `af4f897`, `6881016` all present in git history.
- Deleted templates (`pipeline/dashboard.html`, `pipeline/partials/dag_canvas.html`, `search/page.html`, +17 others) confirmed gone from disk.
- `tests/test_dead_template_guard.py` green with an empty allowlist; full suite 2565 passed; coverage 96.89%.

---
*Phase: 62-polish-cutover*
*Completed: 2026-07-02*
