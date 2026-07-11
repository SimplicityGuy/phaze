---
phase: 87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
plan: 09
subsystem: operator-ui
tags: [stage-matrix, files-page, rail-nav, reachability, ui-01, ui-02, shell-fork, a11y]
requires:
  - "GET /pipeline/files + files_table_view.html + get_files_page (Plan 04)"
  - "v7.0 shell fork: _render_stage + STAGE_PARTIALS + _stage_fragment.html/shell.html (Phase 57)"
  - "CUT-04 rail collapse contract: max-lg:sr-only labels + aria-hidden glyphs (Phase 62)"
provides:
  - "STAGE_PARTIALS['files'] -> the static files_table_view.html literal (a reachable stage, dead-template entry root #2)"
  - "_render_stage 'files' branch: builds the pipeline_files context (get_files_page page=1/size=25/no filters) with the fork inherited for free"
  - "A keyboard-accessible 'Files' rail node (hx-get=/s/files) placed right after Summary"
affects:
  - "The files matrix is now a first-class reachable workspace: direct nav -> full shell chrome; HX rail swap -> bare fragment"
tech-stack:
  added: []
  patterns:
    - "surface an already-built partial as a rail stage: add a STATIC STAGE_PARTIALS literal + a context branch mirroring the standalone route + a sibling rail nav node — the fragment/full fork is inherited from _render_stage, never bespoke"
key-files:
  created:
    - .planning/phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-09-SUMMARY.md
  modified:
    - src/phaze/routers/shell.py
    - src/phaze/templates/shell/partials/rail.html
    - tests/shared/core/test_shell_routes.py
decisions:
  - "Placed Files right after Summary (rail + STAGE_PARTIALS order): the file-level 'where's this file at?' overview sibling of the stage-level Summary landing."
  - "Files is a PLAIN nav node (no x-text count, no priority stepper/pause sub-row): it has no backing $store.pipeline key and is not one of the three enrich agent nodes."
  - "The 'files' branch re-asserts stage/stage_partial/oob_counts AFTER building context (mirrors the analyze branch) — defensive against context shadowing even though get_files_page only adds files_page."
metrics:
  duration: ~40m
  completed: 2026-07-11
  tasks: 3
  files: 3
---

# Phase 87 Plan 09: Reachable derived files stage-matrix workspace Summary

Closed the VERIFIED phase-87 gap: the fully-built, fully-tested derived per-file stage-matrix files
page (`GET /pipeline/files` → `files_table_view.html`, Plan 04) was UNREACHABLE — no nav entry pointed
at it and a direct hit returned a chrome-less fragment. Surfaced it as a first-class rail workspace at
`/s/files` following the shell's established per-stage pattern exactly, so UI-01 (the derived matrix
replaces the retired raw-enum State column as a reachable surface) and UI-02 (its metadata + per-file
retry affordances) are now actually reachable.

## Top-of-summary flags (issues hit)

- **[Footgun hit + recovered] blanket `git checkout -- rail.html` during the mutation-check wiped my
  UNCOMMITTED Files rail node.** After proving the a11y guard has teeth (both guards went RED under the
  mutation), I restored the mutated file with `git checkout -- src/phaze/.../rail.html`. Because nothing
  was committed yet, that reverted rail.html all the way to the base commit, silently discarding the
  Files node edit. Caught immediately on the next full run (2 reachability tests went RED), re-applied
  the edit, re-ran → 67 passed. No lasting damage; the mutation evidence (guards RED) is still valid.
  This is the destructive-git footgun the executor rules warn about — future mutation-checks on
  uncommitted work should restore via an Edit-back, not `git checkout`.

## What Was Built

- **`src/phaze/routers/shell.py`**:
  - Imported `get_files_page` into the existing `phaze.services.pipeline` import block.
  - Added `"files": "pipeline/partials/files_table_view.html"` to `STAGE_PARTIALS` right after
    `"summary"` — a STATIC string literal (T-57-01: `stage` is never spliced into a template path) that
    also doubles as a second dead-template-guard entry root for that partial.
  - Added an `elif stage == "files":` branch in `_render_stage` that builds the SAME context the
    standalone `pipeline_files()` route does — `get_files_page(session, page=1, page_size=25,
    stage=None, bucket=None)` into `files_page`, with `active_stage=None` / `active_bucket=None` — then
    re-asserts `stage`/`stage_partial`/`oob_counts=False` after (the analyze-branch idiom). The
    fragment-vs-full-page fork is inherited from `_render_stage` for free (no bespoke fork added to
    `pipeline_files()`).
- **`src/phaze/templates/shell/partials/rail.html`**: added ONE navigable "Files" rail node right after
  Summary — a native `<button type="button">` with `data-rail-stage="files"`, wired
  `hx-get="/s/files" hx-target="#stage-workspace" hx-swap="innerHTML" hx-push-url="true"`, an inline
  `<svg aria-hidden="true">` table-cells glyph (heroicons v2 outline, 24×24/1.5-stroke/currentColor/w-5
  h-5 wrapper), a `max-lg:sr-only` label span (NEVER `max-lg:hidden` — CUT-04 ↔ CUT-01), a native
  `title="Files"` tooltip, and the `aria-current="page"` binding. A PLAIN nav node: no `x-text` count
  (no backing store key), no priority stepper / pause sub-row (those belong only to the three enrich
  agent nodes).
- **`tests/shared/core/test_shell_routes.py`**:
  - Added `"files"` to `_RAIL_STAGES` (now 14 nodes), so `test_rail_nodes_wired` asserts `/s/files`
    wiring alongside the other 13.
  - `test_files_stage_route_and_fragment` (new, mirrors `test_summary_stage_route_and_fragment`):
    `/s/files` direct nav → 200 with full shell chrome (`id="stage-workspace"` + `data-stage="files"`)
    and the distinctive derived-matrix markup (`id="files-table-view"` + a rendered `_stage_pill`
    token); the HX-Request fragment carries `id="files-table-view"` but NO `<html>`/`<head>`.
  - `test_files_rail_node_is_reachable_and_accessible` (new): the rail exposes a Files
    `<button data-rail-stage="files">` wired to `/s/files`, keyboard-operable (`focus-visible:`),
    carrying its `title`, a `max-lg:sr-only` (never `max-lg:hidden`) label, and an aria-hidden glyph.

## How to Verify

With the test DB up (port 5433):

```
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"
export PHAZE_QUEUE_URL="postgresql://phaze:phaze@localhost:5433/phaze_test"
uv run pytest tests/shared/core/test_shell_routes.py tests/shared/core/test_rail_narrow_width.py \
  tests/shared/core/test_a11y_guards.py tests/shared/core/test_dead_template_guard.py \
  tests/shared/test_rail_priority_controls.py tests/integration/test_files_page.py \
  tests/integration/test_files_filter.py tests/shared/routers/test_routing.py -q
```

→ **67 passed**. `uv run ruff check` + `uv run ruff format --check` clean on the touched files;
`uv run mypy src/phaze/routers/shell.py` → Success.

### Mutation evidence (guard has teeth)

Mutated the Files node label `max-lg:sr-only` → `max-lg:hidden` in rail.html:
`test_files_rail_node_is_reachable_and_accessible` AND `test_rail_narrow_width::test_labels_sr_only_not_hidden`
both went RED (2 failed); restored → green. Proves the new a11y guard fails on a real regression, not
vacuously.

## Deviations from Plan

None functional — the fix followed the shell's established workspace pattern exactly (STAGE_PARTIALS
static literal + `_render_stage` context branch mirroring the standalone route + a sibling rail nav
node; fork inherited). The only incident was the destructive-`git checkout` mutation-restore footgun
documented at the top (hit and recovered within the same session; no committed state affected).

## Threat Register Coverage

No new threat surface. The `files` stage renders through the SAME degrade-safe, bounded, no-COUNT
`get_files_page` read the standalone route already used (T-87-11/T-87-12/T-87-13/T-87-14 all mitigated
in Plan 04); `stage` is matched against the `STAGE_PARTIALS` whitelist and never spliced into a template
path (T-57-01). The endpoint is read-only — no new writes, auth paths, or schema.

## Self-Check: PASSED

All three modified files carry the intended edits (verified via grep: 1× `data-rail-stage="files"` node,
1× `"files"` STAGE_PARTIALS literal, 1× `elif stage == "files":` branch); the SUMMARY exists on disk.
Commit hash recorded below at write time.
