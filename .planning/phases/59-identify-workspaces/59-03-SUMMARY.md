---
phase: 59-identify-workspaces
plan: 03
subsystem: ui
tags: [fastapi, jinja2, htmx, alpine, tracklist, step-cards, per-set-coverage]

# Dependency graph
requires:
  - phase: 59-identify-workspaces
    plan: 01
    provides: "get_tracklist_set_rows per-set row helper + Phase-59 test scaffold (IDENT-02 xfail stubs)"
  - phase: 59-identify-workspaces
    plan: 02
    provides: "trackid STAGE_PARTIALS static literal + _render_stage supersede-in-place pattern the tracklist branch sits beside"
  - phase: 58-enrich-analyze-workspaces
    provides: "_workspace_scaffold.html + _file_table.html + _lane_card.html visual shape; analyze_workspace cards-on-top + table-below structure"
provides:
  - "tracklist_workspace.html — three Search/Scrape/Match step cards (Pattern B) + per-set N/M coverage table (Pattern C)"
  - "shell.py STAGE_PARTIALS['tracklist'] static literal + _render_stage tracklist branch (live /s/tracklist workspace)"
  - "IDENT-02 behavior tests filled (test_tracklist_step_cards_and_triggers, test_tracklist_per_set_coverage)"
affects: [60-review-apply, 61-record-pane]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Step card = lane-card visual shape (rounded-xl border bg p-4) extended with a per-step R-4-guarded ALL trigger + a local *-trigger-response sink"
    - "Aggregate step cards on top (grid grid-cols-3) + per-set detail table below (border-t section), paralleling Phase-58 Analyze"
    - "Server-rendered step counts from get_stage_progress + pending helpers; live busy pills bind to existing *Busy store keys (no new key, no second poll)"

key-files:
  created:
    - "src/phaze/templates/pipeline/partials/tracklist_workspace.html"
  modified:
    - "src/phaze/routers/shell.py"
    - "tests/test_identify_workspaces.py"

key-decisions:
  - "Per-set row key is set_name (confirmed against get_tracklist_set_rows + its unit tests) — the plan's interface note said 'name' but the helper/tests use 'set_name'; followed the real contract"
  - "tracklist_state color map (matched emerald / candidate amber) authored in-template; the helper only ever emits matched/candidate for a per-set row, fallback gray for safety"
  - "Coverage cell: tracks_total==0 → '—' gray; n==m → emerald (full); else blue (partial) — matches UI-SPEC full/partial/none tiers"

requirements-completed: [IDENT-02]

# Metrics
duration: ~18min
completed: 2026-06-30
---

# Phase 59 Plan 03: Tracklist Workspace Summary

**The Tracklist stage now serves three sequential Search·Scrape·Match step cards — each with its own R-4-guarded ALL trigger wired verbatim to the existing bulk endpoint — over a per-set table showing N/M track coverage, superseding the `tracklist` placeholder with no backend change.**

## Performance
- **Duration:** ~18 min
- **Tasks:** 2
- **Files modified:** 3 (1 created, 2 modified)

## Accomplishments
- `tracklist_workspace.html` created: composes `_workspace_scaffold.html` with the `${tracklistDone} sets matched · 1001Tracklists` sub-count, a `grid grid-cols-3` of three step cards following the `_lane_card.html` visual shape (NOT a breadcrumb stepper, D-05), and a `border-t` per-set table below (D-08). Each card renders a server-rendered count (Search `done / —`, Scrape/Match `done / total`), a `{pending} pending` sub-label, a live `*Busy` busy pill, and a per-step ALL trigger (`SEARCH ALL` → `/pipeline/search-tracklists`, `SCRAPE ALL` → `/pipeline/scrape-tracklists`, `MATCH ALL` → `/pipeline/match-tracklists`) with `hx-confirm` + `:disabled` on its matching `*Busy` key (R-4, D-06). Trigger responses land in per-card `#search/#scrape/#match-trigger-response` sinks (Pitfall 4 asymmetry honored). The per-set table feeds `_file_table.html` with Set · Tracklist · Tracks · Matched-to-file, the Tracks cell carrying the D-07 N/M coverage tiered emerald/blue/gray. No single run-chain button, no new store key, no second poll, inert rows.
- `shell.py` wired: `STAGE_PARTIALS['tracklist']` is now the static `"pipeline/partials/tracklist_workspace.html"` literal (T-57-01); the `services.pipeline` import extended (combine-as-imports, alphabetized) with `get_stage_progress`, `get_tracklist_set_rows`, `get_untracked_files`, `get_scrape_pending_tracklists`, `get_match_pending_tracklists`; and a `_render_stage` `elif stage == "tracklist"` branch injects `tracklist_steps` + the three pending counts + `tracklist_sets` (`oob_counts` stays False, Pitfall 5).
- IDENT-02 behavior tests filled: `test_tracklist_step_cards_and_triggers` (three endpoints, R-4 guard on each, ALL labels, no run-chain) and `test_tracklist_per_set_coverage` (cards-above-table order, `1/2` coverage, matched word, inert rows) — both converted from xfail to real passing assertions.

## Task Commits
1. **Task 1: Create tracklist_workspace.html (Pattern B step cards + Pattern C table)** — `97d5b4b` (feat)
2. **Task 2: Wire shell.py tracklist stage + convert IDENT-02 tests** — `f076270` (feat)

## Files Created/Modified
- `src/phaze/templates/pipeline/partials/tracklist_workspace.html` — the Tracklist 3-step-card + per-set-coverage fragment (scaffold + in-template color/coverage maps + `_file_table` feed).
- `src/phaze/routers/shell.py` — `tracklist` STAGE_PARTIALS static literal + 5 extended imports + `_render_stage` tracklist branch.
- `tests/test_identify_workspaces.py` — two IDENT-02 xfail stubs converted to real assertions.

## Decisions Made
- **`set_name` not `name`:** the plan's `<interfaces>` note listed the per-row key as `name`, but the live `get_tracklist_set_rows` helper (and its Plan-01 unit tests) emit `set_name`. Followed the real contract — the template reads `s.set_name`.
- **In-template tracklist_state color map:** `matched`→emerald, `candidate`→amber, gray fallback. A per-set row from the helper is always a real `Tracklist` so it is only ever `matched`/`candidate`; the fallback is defensive.
- **Coverage tiering:** `tracks_total == 0` → `—` gray (covers the unlinked-candidate `0/0` case); `confident == total` → emerald (full); otherwise blue (partial), matching the UI-SPEC full/partial/none tiers and the two-weight contract.

## Deviations from Plan
None beyond the documented `set_name` key correction (the plan body itself, in Task-1 `<action>`, already specified `s.name` — corrected to the real `s.set_name` to match the shipped helper + tests). The template, shell wiring, and both converted tests match the task specs and acceptance criteria.

## Threat Surface
No new surface beyond the plan's `<threat_model>`. T-57-01 (static literal; `stage` matched against dict keys, never spliced into a path), T-59-XSS (all DB-sourced cell `text` via `_file_table.html` autoescape, no `| safe`), T-59-OVERENQ (R-4 `hx-confirm` + `:disabled` busy-gate on each ALL trigger; endpoints unchanged/idempotent), and T-59-DOS (degrade-safe helpers, no router try/except, `oob_counts` False) are all honored.

## Verification
- `uv run pytest tests/test_identify_workspaces.py tests/test_dead_template_guard.py tests/test_shell_routes.py -x` → 20 passed (the 2 IDENT-02 xfails now real-pass)
- `uv run pytest tests/test_identify_workspaces.py::test_identify_single_poll_discipline -x` → 1 passed (Task-1 gate)
- `uv run pytest --cov --cov-report=term-missing` → 2575 passed, **TOTAL 97.18%** (≥85% gate met)
- `uv run mypy src/phaze/routers/shell.py` → clean
- `uv run ruff check src/phaze/routers/shell.py tests/test_identify_workspaces.py` → clean
- pre-commit hooks (ruff, ruff-format, bandit, mypy, file hygiene) → passed on both task commits (never --no-verify)
- Template grep gates: `grid grid-cols-3` present; `border-t ... p-6` table section present; 1 hx-post each to search/scrape/match; 3 `:disabled` busy-gates; 0 occurrences of `<html`/`<head`/`<header`/`{% extends`/`hx-trigger="every`/`setInterval`/`| safe`/`confidence_badge`/`status_badge`; 0 non-comment `hx-get`

## Next Phase Readiness
- Both Identify workspaces (Track-ID + Tracklist) now serve real content; the four IDENT-01/02 behavior tests are live. Phase 60 (Review & Apply) and Phase 61 (record pane / row→detail wiring, R-1) can build on the inert rows + stable `row_id_prefix` ids both tables carry.

## Self-Check: PASSED
- FOUND: src/phaze/templates/pipeline/partials/tracklist_workspace.html
- FOUND: src/phaze/routers/shell.py
- FOUND: tests/test_identify_workspaces.py
- FOUND commit: 97d5b4b (Task 1)
- FOUND commit: f076270 (Task 2)

---
*Phase: 59-identify-workspaces*
*Completed: 2026-06-30*
