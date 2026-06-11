---
phase: 34-pipeline-queue-depth-status-double-enqueue-guard
plan: "03"
subsystem: pipeline-ui
tags: [htmx-oob, processing-card, queue-depth, progress-bar, partial-render-tests]
requires:
  - "queue_progress_percent + the six queue-activity counts in both pipeline contexts (Plan 02)"
  - "stats_bar.html oob_counts gate + OOB-swap pattern (Plan 02/existing)"
  - "Jinja2Templates + _fake_request partial-render harness (test_progress_partial.py)"
provides:
  - "processing_card.html — persistent Processing card: progress bar at queue_progress_percent + '{queued} queued · {active} active' + controller/proposals line, OOB-gated"
  - "dashboard.html initial (non-OOB) include of the card above #pipeline-stats"
  - "stats_bar.html OOB include of the card inside the oob_counts gate (5s-poll swap)"
affects:
  - "Operator UX: an enqueued run now survives a page refresh (the headline phase must-have)"
  - "Plan 04 (button :disabled gating) is independent; the card is display-only and never writes $store.pipeline"
tech-stack:
  added: []
  patterns:
    - "Stable outer id with hx-swap-oob conditional on oob_counts: rendered once on initial load (no swap attr), OOB-swapped in place on every poll — no duplicate-id DOM"
    - "Display-only partial: reads server context (percent + counts) but never writes the Alpine store; store writes stay in stats_bar OOB seeds + stage_cards anchors"
    - "Inline width style driven by a server-computed integer percent; visual block wrapped in an {% if busy %} guard so idle renders an empty shell"
key-files:
  created:
    - src/phaze/templates/pipeline/partials/processing_card.html
    - tests/test_template_helpers/test_processing_card_partial.py
  modified:
    - src/phaze/templates/pipeline/dashboard.html
    - src/phaze/templates/pipeline/partials/stats_bar.html
decisions:
  - "Card outer #processing-card element renders ALWAYS (even idle) so the OOB swap target id exists in the DOM at all times; only the inner visual block is {% if agent_busy>0 or controller_busy>0 %}-gated — an empty-body swap correctly clears a finished run"
  - "hx-swap-oob is emitted only when oob_counts is truthy (poll responses), mirroring stats_bar's files-ready/busy-seed nodes; the initial dashboard include omits oob_counts so the card renders once without a swap attr (no duplicate-id collision)"
  - "Card is display-only — no x-init $store.pipeline writes (those live in stats_bar OOB seeds from Plan 02); keeps the card decoupled from Plan 04 button gating"
metrics:
  duration: "~8 min"
  completed: "2026-06-11"
  tasks: 2
  files: 4
---

# Phase 34 Plan 03: Persistent Processing Card Summary

Built the persistent "Processing" card that fixes the headline UX bug: after clicking Run Analysis and refreshing, the in-flight run now stays visible. `processing_card.html` renders from server context on the initial `/pipeline/` full-page load (above `#pipeline-stats`, no OOB attribute) AND is OOB-swapped on every 5s `/pipeline/stats` tick via an include inside `stats_bar.html`'s existing `oob_counts` gate. When `agent_busy > 0` it shows a DB-derived progress bar (`queue_progress_percent` from Plan 02) plus `"{agent_queued} queued · {agent_active} active"`; a second compact "Proposals:" line appears when `controller_busy > 0`; the body is empty when both are idle (the divide-by-zero already guarded upstream to percent 0). Six direct partial-render tests prove the busy/idle/controller/percent/zero-denominator/OOB-gating states.

## What Was Built

- **`processing_card.html`** — Stable outer `<div id="processing-card">` (always rendered, so the OOB swap target exists at all times). The `hx-swap-oob="true"` attribute is conditional on `{% if oob_counts %}`, so it is emitted only on the poll response and omitted on the initial dashboard include — avoiding duplicate-id DOM, mirroring the `stats_bar.html` files-ready/busy-seed OOB pattern. The inner visual block is wrapped in `{% if agent_busy > 0 or controller_busy > 0 %}`: a `rounded-full` Tailwind progress track with a filled bar at `style="width: {{ queue_progress_percent }}%"` (dark-mode-aware, `role="progressbar"` + `aria-valuenow`), the `{{ agent_queued }} queued &middot; {{ agent_active }} active` text line (gated on `agent_busy > 0`), and a second `Proposals: {{ controller_queued }} queued &middot; {{ controller_active }} active` line (gated on `controller_busy > 0`). Display-only — no `$store.pipeline` writes.
- **`dashboard.html`** — Added `{% include "pipeline/partials/processing_card.html" %}` immediately above the `#pipeline-stats` polling div (initial render, no oob flag).
- **`stats_bar.html`** — Added the same include inside the existing `{% if oob_counts %}` gate (after the busy-seed nodes) so the 5s poll response carries the OOB card swap.
- **`test_processing_card_partial.py`** — Six tests mirroring `test_progress_partial.py` (`Jinja2Templates` + `_fake_request` + a `_render(**context)` helper): `test_card_busy_shows_bar_and_counts` (asserts `7 queued`, `3 active`, middle-dot, `width: 75%`), `test_card_idle_renders_empty` (no bar/text, stable shell present), `test_card_controller_line`, `test_card_percent_math_seventyfive` (`width: 75%` + `aria-valuenow="75"`), `test_card_zero_denominator_guard` (empty, no exception), and `test_card_oob_attribute_only_on_poll` (oob gating).

## Deviations from Plan

None - plan executed exactly as written. (One extra test beyond the five specified — `test_card_oob_attribute_only_on_poll` — was added to lock the OOB gating contract; covered by Rule 2, correctness of the duplicate-id-avoidance behavior.)

## Verification

- `uv run pytest tests/test_template_helpers/test_processing_card_partial.py -q` → 6 passed.
- All three templates parse via Jinja (`Jinja2Templates(...).env.get_template(...)` exits 0).
- `grep -c processing_card.html` → dashboard.html 1, stats_bar.html 2 (include + doc-comment mention).
- `uv run pytest tests/test_routers/test_pipeline.py -q` → 35 passed (no dashboard/stats endpoint regression with the new include).
- `uv run ruff check tests/test_template_helpers/test_processing_card_partial.py` → All checks passed.
- Pre-commit hooks (ruff, ruff-format, bandit, mypy `uv run mypy .`) passed on both code commits.

## Commits

- `cc074ae` feat(34-03): persistent Processing card, OOB-swapped on the 5s poll
- `8bd46f1` test(34-03): direct partial-render tests for the Processing card states

## Self-Check: PASSED

- FOUND: src/phaze/templates/pipeline/partials/processing_card.html
- FOUND: src/phaze/templates/pipeline/dashboard.html (include above #pipeline-stats)
- FOUND: src/phaze/templates/pipeline/partials/stats_bar.html (include inside oob_counts gate)
- FOUND: tests/test_template_helpers/test_processing_card_partial.py
- FOUND: commit cc074ae
- FOUND: commit 8bd46f1
