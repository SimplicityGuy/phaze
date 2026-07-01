---
phase: 59-identify-workspaces
plan: 02
subsystem: ui
tags: [fastapi, jinja2, htmx, fingerprint, tracklist, read-only-workspace, trackid]

# Dependency graph
requires:
  - phase: 59-identify-workspaces
    plan: 01
    provides: "get_trackid_stage_files read-only row helper + Phase-59 test scaffold (IDENT-01 xfail stubs)"
  - phase: 58-enrich-analyze-workspaces
    provides: "_workspace_scaffold.html + _file_table.html reusable workspace partials; shell.py STAGE_PARTIALS/_render_stage supersede-in-place pattern"
provides:
  - "trackid_workspace.html — the Track-ID combined per-file identity table (Pattern A)"
  - "shell.py STAGE_PARTIALS['trackid'] + _render_stage trackid branch (live /s/trackid workspace)"
  - "IDENT-01 behavior tests filled (test_trackid_table_signals, test_trackid_success_renders_done)"
affects: [59-03-tracklist-workspace, 60-review-apply, 61-record-pane]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Read-only consolidated workspace: scaffold with EMPTY actions slot (no trigger button) — signals produced upstream, surfaced here"
    - "Status-word + color cell via _file_table.html cell-dict (never hue-only, WCAG 1.4.1); color maps live in-template as Jinja dicts"
    - "Supersede-in-place STAGE_PARTIALS literal + degrade-safe helper in _render_stage (no router try/except, oob_counts False)"

key-files:
  created:
    - "src/phaze/templates/pipeline/partials/trackid_workspace.html"
  modified:
    - "src/phaze/routers/shell.py"
    - "tests/test_identify_workspaces.py"

key-decisions:
  - "test_trackid_success_renders_done seeds BOTH engines success so the Pitfall-1 guard 'pending not in tbl' is exact (panako-absence would otherwise legitimately render pending in the combined table)"
  - "Confidence percent uses '%d%%'|format (int) per D-02 — the tracklist match_confidence, never a fabricated fingerprint score"
  - "All four status colors (emerald-600/300, rose-600/400, amber-600/300, gray-500/400) already exist in compiled app.css — no new Tailwind utility introduced, so no just-tailwind rebuild needed"

requirements-completed: [IDENT-01]

# Metrics
duration: ~12min
completed: 2026-06-30
---

# Phase 59 Plan 02: Track-ID Workspace Summary

**The Track-ID stage now serves one combined, read-only per-file identity table (File · audfprint · Panako · Tracklist · Confidence) that surfaces the existing audfprint/Panako `FingerprintResult` state and the linked/best-candidate tracklist match + confidence, superseding the `trackid` placeholder.**

## Performance
- **Duration:** ~12 min
- **Tasks:** 2
- **Files modified:** 3 (1 created, 2 modified)

## Accomplishments
- `trackid_workspace.html` created: composes `_workspace_scaffold.html` with an EMPTY actions slot (read-only — the prototype's AcoustID IDENTIFY flow is the dropped IDENT-03) and feeds ONE combined table (D-03) through `_file_table.html`. Engine status words coloured done/failed/pending (D-01); tracklist match-state matched/candidate/no-match (D-04); Confidence as `{n}%` tiered emerald/amber/gray or "—" (D-02 — the tracklist `match_confidence` int, never a fabricated score). Rows inert (no `hx-get`, R-1); no second poll loop (R-2).
- `shell.py` wired: `STAGE_PARTIALS['trackid']` is now the static `"pipeline/partials/trackid_workspace.html"` literal (T-57-01), the import line extended with `get_trackid_stage_files`, and a `_render_stage` `elif stage == "trackid"` branch injects `trackid_files` (degrade-safe helper → no router try/except; `oob_counts` stays False).
- IDENT-01 behavior tests filled: `test_trackid_table_signals` (one combined table; done/failed/pending engine words; matched/candidate states; 90% confidence; inert rows) and `test_trackid_success_renders_done` (Pitfall-1 success→done guard) — both converted from xfail to real passing assertions.

## Task Commits
1. **Task 1: Create trackid_workspace.html (Pattern A combined table)** — `3b1202c` (feat)
2. **Task 2: Wire shell.py trackid stage + convert IDENT-01 tests** — `a50284b` (feat)

## Files Created/Modified
- `src/phaze/templates/pipeline/partials/trackid_workspace.html` — the Track-ID combined identity table fragment (scaffold + single `_file_table` feed, empty actions, in-template color maps).
- `src/phaze/routers/shell.py` — `trackid` STAGE_PARTIALS static literal + `get_trackid_stage_files` import + `_render_stage` trackid branch.
- `tests/test_identify_workspaces.py` — two IDENT-01 xfail stubs converted to real assertions.

## Decisions Made
- **Pitfall-1 test shape:** `test_trackid_success_renders_done` seeds both audfprint AND panako `success`. In the combined table a single-engine seed would leave panako legitimately "pending", which would break the `"pending" not in tbl` guard; seeding both keeps the success→done assertion exact while still catching a success→pending regression.
- **No new Tailwind utility:** all four required status color classes already appear in the compiled `app.css` (verified by grep across existing partials), so no `just tailwind` rebuild was required (and `app.css` is a gitignored build artifact regardless).

## Deviations from Plan
None — plan executed exactly as written. The template, the shell wiring, and the two converted tests all match the task specs and acceptance criteria. (Implementation note within plan scope: the Pitfall-1 test seeds both engines to keep the global `pending` assertion valid for the combined table — consistent with the plan's "renders 'done' NOT 'pending'" intent.)

## Threat Surface
No new surface beyond the plan's `<threat_model>`. T-57-01 (static literal), T-59-XSS (all cell `text` via `_file_table.html` autoescape, no `| safe`), and T-59-DOS (degrade-safe helper, no router try/except, `oob_counts` False) are all honored.

## Verification
- `uv run pytest tests/test_identify_workspaces.py tests/test_dead_template_guard.py tests/test_shell_routes.py -x` → 18 passed, 2 xfailed (the Plan 59-03 tracklist stubs)
- `uv run pytest tests/test_identify_workspaces.py::test_identify_fragments_are_bare tests/test_identify_workspaces.py::test_identify_single_poll_discipline` → 2 passed
- `uv run mypy src/phaze/routers/shell.py` → clean
- `uv run ruff check src/phaze/routers/shell.py tests/test_identify_workspaces.py` → clean
- pre-commit hooks (ruff, ruff-format, bandit, mypy) → passed on both task commits (never --no-verify)
- Task-1 grep gates: single tabindex (scaffold h1 only, via macro); no `<html`/`<head`/`<header`/`{% extends`/`hx-trigger="every`/`setInterval`/`| safe`/`confidence_badge`/`status_badge`/trigger `<button`; columns exactly File · audfprint · Panako · Tracklist · Confidence; 0 `hx-get`

## Next Phase Readiness
- Plan 59-03 (Tracklist workspace) can wire `get_tracklist_set_rows` + the three step cards and convert the remaining two xfail stubs (`test_tracklist_step_cards_and_triggers`, `test_tracklist_per_set_coverage`).
- D-04 candidate-fallback open point from 59-01 confirmed against UI-SPEC: the system-wide best-candidate reading is the locked literal D-04 behavior; UI-SPEC Pattern A treats "candidate" as the no-link-but-candidate-exists state, matching the helper — no per-file candidate refinement needed this phase.

## Self-Check: PASSED
- FOUND: src/phaze/templates/pipeline/partials/trackid_workspace.html
- FOUND: src/phaze/routers/shell.py
- FOUND: tests/test_identify_workspaces.py
- FOUND commit: 3b1202c (Task 1)
- FOUND commit: a50284b (Task 2)

---
*Phase: 59-identify-workspaces*
*Completed: 2026-06-30*
