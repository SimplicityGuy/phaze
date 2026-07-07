---
quick_id: 260707-c3a
status: complete
date: 2026-07-07
commit: d8a6d769
subsystem: shell-ui
tags: [ui, dag-rail, cleanup, shell]
files_created: []
files_modified:
  - src/phaze/templates/shell/partials/rail.html
  - tests/shared/core/test_rail_narrow_width.py
  - tests/shared/core/test_shell_routes.py
requirements_completed: [QUICK-260707-c3a]
---

# Quick 260707-c3a: Remove Redundant Pipeline-Header "+ Scan" Button Summary

Removed the duplicate "+ Scan" CTA from the DAG rail header — it only issued
`hx-get="/s/discover"`, byte-identical to the Discover rail node directly below it, and was
never a real scan trigger (the actual Scan control lives on the Discover screen via
`trigger_scan_card.html` → `POST /pipeline/scans`). The "Pipeline" eyebrow is kept; the two
structural test guards drop from a 15-node to a 14-node count in lockstep.

## What Changed

- **`src/phaze/templates/shell/partials/rail.html`**: Deleted the entire header
  `<button type="button" ... title="Scan" ...>` element (its `hx-get="/s/discover"` opening
  tag, the inline plus-glyph `<svg>`, and the `<span class="max-lg:sr-only">Scan</span>`).
  Kept the `<span ...>Pipeline</span>` eyebrow verbatim. Simplified the wrapper `<div>` flex
  classes (dropped `justify-between` — now a single child left-aligns naturally); padding and
  `max-lg:` collapse behavior unchanged. Updated the Jinja comment from
  `{# Eyebrow + primary "+ Scan" CTA ... #}` to `{# Pipeline eyebrow (section label for the DAG rail). #}`.
  The `<nav>` and all rail nodes (including the surviving `data-rail-stage="discover"` Discover
  node) were left untouched.
- **`tests/shared/core/test_rail_narrow_width.py`**: `test_glyphs_present` and
  `test_titles_present` guards lowered `>= 15` → `>= 14` (the removed CTA carried one inline-SVG
  glyph and was one navigable node). Updated the module docstring, `_navigable_node_tags`, and
  `_label_span_attrs` docstrings to drop the "+ Scan" CTA and reflect 14 navigable nodes
  (12 stage buttons + 2 below-line links). `test_labels_sr_only_not_hidden` (`>= 14`) and
  `test_focus_and_current_preserved` (`>= 12` stage buttons) left AS-IS — both still hold.
- **`tests/shared/core/test_shell_routes.py`**: Refreshed the stale `-- the +Scan CTA adds one
  more.` comment at line 112; the `>= len(_RAIL_STAGES)` assertion is unchanged and still passes.

## Verification

- `rg 'title="Scan"|>Scan<' rail.html` → no matches (button gone).
- `rg '>Pipeline<' rail.html` → matches the eyebrow (kept).
- `rg 'data-rail-stage="discover"' rail.html` → still matches (canonical `/s/discover` entry intact).
- Tests (against ephemeral Postgres/Redis on 5433/6380 via `just test-db`):
  `uv run pytest tests/shared/core/test_rail_narrow_width.py tests/shared/core/test_shell_routes.py tests/shared/core/test_a11y_guards.py -q` → **22 passed**.
- `pre-commit run --files <3 changed files>` → all hooks Passed (ruff, ruff-format, bandit,
  mypy, whitespace/EOF, etc.), no `--no-verify`.

## Deviations from Plan

None — plan executed exactly as written. (The DB-backed `test_shell_routes.py` cases required
the ephemeral test Postgres, started via `just test-db` per the project's local-test convention;
this is standard, not a deviation.)

## Known Stubs

None.

## Self-Check: PASSED

- rail.html, test_rail_narrow_width.py, test_shell_routes.py all present and modified.
- Commit `d8a6d769` exists in git log.
