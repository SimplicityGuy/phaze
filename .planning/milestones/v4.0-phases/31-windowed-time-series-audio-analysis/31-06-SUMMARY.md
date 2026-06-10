---
phase: 31-windowed-time-series-audio-analysis
plan: 06
subsystem: review-ui
tags: [htmx, jinja2, svg, proposals, analysis-window, xss-hardening]
requires:
  - "AnalysisWindow model (plan 31-02, models/analysis.py)"
  - "proposals review UI router + templates (existing)"
provides:
  - "GET /proposals/{id}/timeline HTMX fragment endpoint (file_id-scoped)"
  - "server-rendered SVG/CSS multi-lane analysis timeline (no charting lib)"
  - "inline BPM sparkline + timeline expand control in each review row"
affects:
  - "src/phaze/routers/proposals.py"
  - "src/phaze/templates/proposals/partials/proposal_row.html"
tech-stack:
  added: []
  patterns:
    - "HTMX lazy-expand into a hidden sibling <tr> (mirrors row_detail)"
    - "server-side SVG geometry with numeric-only coordinate attributes"
    - "Jinja2 autoescaping for all essentia-derived label text (no | safe)"
    - "deterministic integer HSL hue per label for ribbon colours"
key-files:
  created:
    - "src/phaze/templates/proposals/partials/analysis_timeline.html"
  modified:
    - "src/phaze/routers/proposals.py"
    - "src/phaze/templates/proposals/partials/proposal_row.html"
    - "tests/test_routers/test_proposals.py"
decisions:
  - "Embedded sparkline + Timeline control in the existing Actions cell rather than adding a new table column, to avoid touching proposal_table.html header / colspans (kept change inside the plan's file set)."
  - "Batch-loaded fine-window BPMs for the whole page in one query (_build_sparklines) to avoid an N+1 per row."
  - "Computed all SVG geometry (polyline points, ribbon widths, hues) in Python as rounded numeric values so no string ever reaches an SVG/CSS attribute (XSS defence-in-depth on top of autoescaping)."
metrics:
  tasks_completed: 2
  files_created: 1
  files_modified: 3
  tests_added: 7
  completed: 2026-06-10
---

# Phase 31 Plan 06: Windowed Analysis Review-UI Timeline Summary

Server-rendered SVG/CSS analysis timeline for the proposal review UI: a compact inline BPM sparkline + lazy-expand control in each row, and an HTMX-loaded multi-lane fragment (BPM `<polyline>` + width-proportional key/mood/style ribbons) scoped strictly by `file_id`, with all essentia-derived labels HTML-escaped.

## What Was Built

### Task 1 — Timeline fragment endpoint + multi-lane SVG/CSS template (commit `6f132ac`)
- `GET /proposals/{proposal_id}/timeline` (`routers/proposals.py`): resolves the proposal to its `file_id`, queries `select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(tier, window_index)`, and renders the fragment. 404 when the proposal is missing.
- `partials/analysis_timeline.html`: a BPM `<svg>` with a single `<polyline>` (numeric-only `points`), then Key / Mood / Style lanes of flexed colored `<div>` ribbons whose width is proportional to `(end_sec - start_sec)` and whose label text is rendered through normal `{{ }}` autoescaping. Empty-state ("No analysis windows for this file.") when the series is empty; per-lane "No … data." when a lane has no values.
- Helpers in `proposals.py`: `_bpm_polyline_points` (rounded float coords; flat midline when BPMs are equal), `_hue_for` (integer HSL hue from a label), `_ribbons` (width-proportional ribbon descriptors).

### Task 2 — BPM sparkline + HTMX expand control in the review row (commit `5db2495`)
- `proposal_row.html`: a fixed-size inline `<svg>` sparkline (polyline over the file's fine-window BPMs, or a dashed flat baseline when no windows exist) plus a `Timeline` button using `hx-get="/proposals/{{ proposal.id }}/timeline"` → `hx-target="#timeline-{{ proposal.id }}"`, with a hidden sibling `<tr id="timeline-{{ proposal.id }}">` that the fragment un-hides on load.
- `list_proposals` now batch-loads fine-window BPMs for the page via `_build_sparklines` (single `IN (...)` query, keyed by `str(file_id)`), passed into the row context as `sparklines`.

## Tests

7 new tests in `tests/test_routers/test_proposals.py` (30 pass in the file total):
- `test_timeline_with_windows` — 200 + polyline + escaped ribbon labels.
- `test_timeline_empty_state` — 200 + "No analysis windows".
- `test_timeline_not_found` — 404.
- `test_timeline_scoped_by_file_id` — only the proposal's own file's windows render.
- `test_timeline_escapes_label_xss` — a `<script>` mood renders as `&lt;script&gt;…`, never raw.
- `test_row_renders_sparkline_and_timeline_control` — row shows `<svg>`, the `hx-get=…/timeline` control, and the hidden timeline row.
- `test_row_sparkline_without_windows` — sparkline + control still render with no windows.

## Verification

- `uv run pytest tests/test_routers/test_proposals.py -x` — 30 passed.
- `uv run mypy src/phaze/routers/proposals.py` — clean.
- Router coverage 99.17% (single uncovered branch is the no-label `continue` in `_ribbons`); well above the 85% gate.
- All pre-commit hooks (ruff, ruff-format, bandit, mypy) pass on both commits.
- Acceptance greps all match: `AnalysisWindow.file_id == file_id`, `<polyline` in the fragment, `| safe` count = 0, `hx-get="/proposals/.../timeline"` + `id="timeline-"` + `<svg` in the row.

## Threat Model Coverage

- **T-31-06-01 (XSS):** all key/mood/style labels rendered via Jinja2 autoescaping (no `| safe`); SVG geometry / ribbon widths / hues computed as numeric values in Python. Asserted by `test_timeline_escapes_label_xss`.
- **T-31-06-02 (Broken access control):** endpoint scopes strictly by the proposal's `file_id` and lives on the same review-UI router as the rest of the approval workflow (no separate auth surface introduced). Asserted by `test_timeline_scoped_by_file_id`.
- **T-31-06-SC (Supply chain):** zero new packages — SVG/CSS only, no charting library.

## Deviations from Plan

None for Rules 1–4. Two in-scope design choices documented above (sparkline embedded in the Actions cell rather than a new column; Python-side numeric geometry computation) stayed within the plan's declared file set.

## Known Stubs

None. The timeline reads live `analysis_window` rows; sparkline/timeline render real data when windows exist and degrade to explicit empty states otherwise.
