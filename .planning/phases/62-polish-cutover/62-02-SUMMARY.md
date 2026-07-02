---
phase: 62-polish-cutover
plan: 02
subsystem: shell-ui
tags: [cut-04, rail, accessibility, tailwind, heroicons, responsive]
requires:
  - "rail.html (v7.0 DAG rail, Phase 57 SHELL-02)"
  - "assets/src/app.css @source template scan + just tailwind build chain"
provides:
  - "Collapsible icon rail: 280px expanded / 64px icon-only strip below 1024px (CUT-04)"
  - "15 per-stage inline-SVG heroicons v2 outline glyphs (D-08)"
  - "tests/test_rail_narrow_width.py — filesystem structural guard for the collapse contract"
affects:
  - "src/phaze/templates/shell/partials/rail.html"
tech-stack:
  added: []
  patterns:
    - "Pure-CSS Tailwind max-lg: (@media not all and (min-width:64rem)) responsive collapse, no JS/no persistence"
    - "Inline-SVG heroicons v2 outline glyphs (no dep, no icon font) — mirrors base.html theme-toggle / header wave-logo idiom"
    - "max-lg:sr-only labels (a11y-tree-preserving) + native title tooltips for the collapsed strip"
key-files:
  created:
    - "tests/test_rail_narrow_width.py"
  modified:
    - "src/phaze/templates/shell/partials/rail.html"
decisions:
  - "D-07: collapse the 280px rail to a 64px (max-lg:w-16) icon strip below 1024px via pure CSS"
  - "D-08: add per-stage glyphs as inline heroicons v2 outline SVG, aria-hidden, label carries the accessible name"
  - "Unrolled the Review & Apply {% for %} loop into 5 explicit buttons so each carries a distinct glyph (a single loop body cannot emit per-item SVGs)"
metrics:
  duration: "~15m"
  completed: "2026-07-01"
  tasks: 2
  files_changed: 2
requirements-completed: [CUT-04]
---

# Phase 62 Plan 02: Narrow-Width Icon Rail (CUT-04) Summary

Collapses the v7.0 three-column shell's 280px DAG rail to a 64px icon-only strip below 1024px via a pure-CSS Tailwind `max-lg:` breakpoint (no JS, no persistence), adds the 15 per-stage inline-SVG heroicons glyphs the icon-only view requires, and locks the collapse contract with a browser-free filesystem structural guard.

## What was built

**Task 1 — rail.html collapse + glyphs (commit `ad2ae59`)**
- `<aside>`: added `max-lg:w-16` alongside the existing `w-[280px]`.
- Added one heroicons v2 **outline** (MIT) glyph per node — 15 total: `+ Scan`=plus, discover=folder-open, metadata=document-text, fingerprint=finger-print, analyze=beaker, trackid=identification, tracklist=list-bullet, propose=sparkles, rename=pencil-square, tagwrite=tag, move=folder-arrow-down, dedupe=document-duplicate, cue=musical-note, audit=clipboard-document-list, agents=cpu-chip. Each uses the verbatim SVG wrapper contract (`viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="w-5 h-5 shrink-0" aria-hidden="true"`), path `d` copied verbatim from the heroicons v2 source.
- Label spans: `max-lg:sr-only` (HARD contract — stay in the a11y tree, NEVER `max-lg:hidden`). Count spans (`x-text`): `max-lg:hidden`. Group eyebrows: `max-lg:hidden`; group boxes: chrome neutralised at narrow (`max-lg:border-transparent max-lg:bg-transparent max-lg:px-0 max-lg:py-0 max-lg:mx-0 max-lg:mt-0`) so buttons stay visible while the box decoration disappears.
- Node inner layout: `max-lg:justify-center max-lg:px-0` (centres the glyph, ~40px hit target per the UI-SPEC spacing exception).
- Native `title="{label}"` on every navigable node (same text as the visible/sr-only label).
- `aria-[current=page]` tint + inset bar and `focus-visible:ring-2 focus-visible:ring-blue-500` preserved verbatim on every node.
- Rebuilt `src/phaze/static/css/app.css` locally with `just tailwind` (v4.3.2 standalone binary) — the new `max-lg:*` utilities compile to `@media not all and (min-width:64rem)` (i.e. < 1024px). The compiled CSS is gitignored and was NOT committed.

**Task 2 — structural guard (commit `5e9bfe5`)**
- `tests/test_rail_narrow_width.py`: pure-filesystem guard (no DB/client/browser/CSS build), mirroring `test_dead_template_guard.py` / `test_base_html_sri.py`. 7 tests: rail exists, collapse width, labels sr-only (and explicitly NOT `max-lg:hidden`), counts hidden, ≥15 aria-hidden glyphs (wrapper-contract-checked), per-node titles, focus + aria-current preserved.

## Verification

- `uv run pytest tests/test_rail_narrow_width.py -q` → 7 passed.
- `uv run ruff check .` → clean; `uv run mypy .` → clean (186 files).
- `just tailwind` → regenerates `src/phaze/static/css/app.css`; `git check-ignore` confirms it is ignored (not committed).
- Dead-template guard still green (unrolling the loop did not orphan anything).
- Manual viewport-resize + overlay-usability check is deferred to UAT per 62-VALIDATION.md.

## Deviations from Plan

### Rule-driven / discretion decisions (no user permission required)

**1. [Design — necessary] Unrolled the Review & Apply `{% for %}` loop into 5 explicit buttons.**
- **Found during:** Task 1.
- **Why:** D-08 requires a *distinct* glyph per node (rename=pencil-square, tagwrite=tag, move=folder-arrow-down, dedupe=document-duplicate, cue=musical-note). A single loop body cannot emit five different inline SVGs, and the plan's Task-1 verify counts literal source occurrences (`aria-hidden="true" >= 15`, `max-lg:sr-only >= 14`) which a loop (1 literal occurrence) cannot satisfy. Unrolling keeps every item's hx-get / aria-current / focus-visible wiring byte-identical to the former loop output — rendered markup is unchanged; only the source is expanded.
- **Files:** `src/phaze/templates/shell/partials/rail.html`.

**2. [Design — discretion] Glyphs render neutral `currentColor` (status dots dropped).**
- Per the UI-SPEC Color section ("glyphs render currentColor at the node's normal text color; accent must NOT bleed onto the collapsed icon glyphs"), the former per-status colored dots (emerald done / blue-pulse analyze / gray idle) are replaced by the neutral glyph. Status is still conveyed by the numeric counts in the expanded view; the active node keeps its blue tint + inset bar. This is the locked D-08 design, not a regression.

**3. [Copy — minor] `+ Scan` CTA visible label is now "Scan" (plus carried by the glyph).**
- The plus glyph now provides the "+", so the textual label is "Scan" (title="Scan") to avoid a doubled "＋ + Scan" in the expanded view. Single-source-of-truth (title == visible label) preserved.

### Auto-fixed issues
None beyond the two lint fixes made while authoring the test (RUF001/003 ambiguous `×` → `x`; `<aside>` regex tightened to require `class=` so it skips the literal `<aside>` in the header comment).

## Known Stubs
None. No placeholder data, no unwired data source — the change is purely presentational (CSS classes + decorative inline SVG + static tooltips).

## Threat Flags
None. Per the plan threat model: the only net-new markup is static, decorative (`aria-hidden`) inline-SVG geometry copied verbatim from a known MIT source, Tailwind class strings, and static `title` tooltips. No new endpoint, input, auth path, data flow, or `<script>`/`integrity` surface introduced.

## TDD note
Task 2 is an impl-first characterization guard (the plan orders Task 1 implementation before the Task-2 guard), so there is no separate RED commit — the guard was authored green against the Task-1 rail.html, which is the intended structure. It fails loudly on any future regression (e.g. a label switched to `max-lg:hidden`).

## Self-Check: PASSED
- Files present: rail.html, tests/test_rail_narrow_width.py, 62-02-SUMMARY.md.
- Commits present: `ad2ae59` (Task 1), `5e9bfe5` (Task 2).
