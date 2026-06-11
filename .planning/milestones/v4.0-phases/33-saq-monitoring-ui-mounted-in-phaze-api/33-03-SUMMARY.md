---
phase: 33-saq-monitoring-ui-mounted-in-phaze-api
plan: 03
subsystem: web-ui
tags: [pipeline-dashboard, saq, navigation, htmx]
requires:
  - "33-02: SAQ dashboard mounted at /saq"
provides:
  - "Visible 'Queue Monitor' link from the pipeline dashboard to the mounted SAQ UI at /saq"
affects:
  - src/phaze/templates/pipeline/dashboard.html
tech-stack:
  added: []
  patterns:
    - "Plain navigation anchor (target=_blank rel=noopener) to a separately-mounted full-page app, not an HTMX partial"
    - "Reuse base.html nav-link Tailwind convention for in-page action links"
key-files:
  created: []
  modified:
    - src/phaze/templates/pipeline/dashboard.html
    - tests/test_routers/test_pipeline.py
decisions:
  - "Link opens in a new tab (target=_blank rel=noopener) because /saq is SAQ's own full-page Starlette app, so the operator keeps the pipeline page"
  - "Matched the established base.html nav-link Tailwind classes for visual consistency"
metrics:
  duration: ~5 min
  completed: 2026-06-11
  tasks: 2
  files: 2
requirements: [SAQUI-06]
---

# Phase 33 Plan 03: SAQ-UI Pipeline-Page Link Summary

Added a visible "Queue Monitor ↗" link on the pipeline dashboard (`GET /pipeline/`) that
opens the SAQ queue-monitor UI mounted at `/saq` (delivered in plan 33-02) in a new tab.

## What Was Built

- **RED test** (`tests/test_routers/test_pipeline.py::test_dashboard_links_to_saq_ui`):
  asserts `GET /pipeline/` returns 200 and that `href="/saq"` appears in the rendered HTML.
- **GREEN template change** (`src/phaze/templates/pipeline/dashboard.html`): wrapped the
  existing `<h1>Pipeline Dashboard</h1>` in a `flex items-center justify-between gap-4` row
  and added a trailing anchor:
  `<a href="/saq" target="_blank" rel="noopener" ...>Queue Monitor ↗</a>` using the
  established `base.html` nav-link Tailwind classes (muted gray + hover treatment).

The heading text and the rest of the dashboard body (trigger-scan card, recent-scans table,
processing card, stats bar, stage cards) are unchanged — no regression to `test_dashboard_page`.

## Tasks Completed

| Task     | Name                                                | Commit    | Files                                  |
| -------- | --------------------------------------------------- | --------- | -------------------------------------- |
| 33-03-01 | RED — pin SAQ-UI link on the pipeline dashboard     | `fdc136d` | tests/test_routers/test_pipeline.py    |
| 33-03-02 | GREEN — link the pipeline dashboard to /saq         | `e8ae425` | src/phaze/templates/pipeline/dashboard.html |

## Verification

- `uv run pytest tests/test_routers/test_pipeline.py -q` → 36 passed (new
  `test_dashboard_links_to_saq_ui` plus the unchanged `test_dashboard_page`).
- `uv run ruff check .` → All checks passed.
- The link is `href="/saq"`, opens in a new tab with `rel="noopener"`, and the rendered
  pipeline page still shows the "Pipeline Dashboard" heading.

## Deviations from Plan

None - plan executed exactly as written.

## TDD Gate Compliance

RED (`test(33-03): ... (RED)`, `fdc136d`) precedes GREEN (`feat(33-03): ...`, `e8ae425`).
The RED test failed on the `href="/saq"` assertion before the template change and passes
after it; the page returned 200 in both states (the queue-activity degrade warnings in the
RED run are the pre-existing missing-queue degrade path, unrelated to this link).

## Self-Check: PASSED

- FOUND: src/phaze/templates/pipeline/dashboard.html (contains `href="/saq"`)
- FOUND: tests/test_routers/test_pipeline.py (contains `test_dashboard_links_to_saq_ui`)
- FOUND commit: fdc136d
- FOUND commit: e8ae425
