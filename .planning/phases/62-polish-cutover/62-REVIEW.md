---
phase: 62-polish-cutover
reviewed: 2026-07-02T06:30:00Z
depth: standard
files_reviewed: 15
files_reviewed_list:
  - src/phaze/routers/cue.py
  - src/phaze/routers/duplicates.py
  - src/phaze/routers/pipeline.py
  - src/phaze/routers/preview.py
  - src/phaze/routers/proposals.py
  - src/phaze/routers/tags.py
  - src/phaze/routers/tracklists.py
  - src/phaze/templates/base.html
  - src/phaze/templates/shell/partials/cmdk_modal.html
  - src/phaze/templates/shell/partials/rail.html
  - src/phaze/templates/shell/shell.html
  - tests/test_a11y_guards.py
  - tests/test_dead_template_guard.py
  - tests/test_docs_ia_current.py
  - tests/test_rail_narrow_width.py
findings:
  critical: 0
  warning: 1
  info: 3
  total: 4
status: issues_found
---

# Phase 62: Code Review Report

**Reviewed:** 2026-07-02T06:30:00Z
**Depth:** standard
**Files Reviewed:** 15
**Status:** issues_found

## Summary

Phase 62 is a presentation-only CUT-02 cutover (dead-template removal) plus a11y and
narrow-width-rail polish. I focused the review on the single most important correctness
property: that the cutover deleted ONLY the dead non-HX `return ...list.html` render tails
while KEEPING each router's live HX fragment branch, and that pipeline.py / preview.py are
correct pure redirects.

**Core correctness property: VERIFIED PASS.** All five list routers (cue, duplicates,
proposals, tags, tracklists) retain their `if HX-Request != "true": return RedirectResponse(...)`
guard ABOVE the render tail, and the render tail now unconditionally returns the LIVE shell
`partials/..._content.html`/`..._list.html` fragment — exactly what the shell's HX
pagination/filter/sort rail-swaps consume. No live HX branch was removed. pipeline.py's
`dashboard()` and preview.py's `tree_preview()` collapse to pure `302` redirects (`/pipeline/`→`/`,
`/preview/`→`/s/move`) with the routes still registered so old bookmarks resolve; their whole
legacy render path was genuinely dead. `build_dashboard_context` (kept in pipeline.py) is still
consumed by shell.py, so it is not dead. No broken routes, no dangling template references were
introduced (the deleted `search/*` legacy partials are not referenced by the still-live
`search.py`, which renders the surviving `palette_results.html`).

Template edits are sound: base.html strips the tab-bar nav to logo + theme toggle and retargets
the skip link to the live `#main-content` anchor (which exists); shell.html removes the dead
empty detail-pane `<aside>`; cmdk_modal.html adds the missing combobox `aria-label`; rail.html
adds the pure-CSS `max-lg:` collapse with per-node glyphs and unrolls the Review & Apply loop
into five explicit buttons whose `data-rail-stage`/`hx-get` targets match the former loop
verbatim.

Verification: the 4 new/edited guard test files pass (21/21); `ruff check` is clean on all 7
routers; `test_redirect_resolution.py` + `test_shell_routes.py` (the non-integration slices)
pass. The ~247 errors seen in a broader router-test run are Postgres connection failures
(no local DB running) — the known infra situation, not code defects.

Findings below are all low-severity quality/documentation drift; none block ship.

## Warnings

### WR-01: Stale STAGE_PARTIALS comment claims deleted `dag_canvas.html` "stays reachable until CUT-02"

**File:** `src/phaze/routers/shell.py:82-84` (not in the reviewed file set, but a direct fallout of this phase's deletions)
**Issue:** The `STAGE_PARTIALS` comment still reads: *"dag_canvas.html stays reachable via the
legacy dashboard.html until CUT-02 (Phase 62), so the dead-template guard stays green
(supersede-in-place)."* This phase IS CUT-02, and both `pipeline/partials/dag_canvas.html` and
`pipeline/dashboard.html` were deleted from disk in it. The comment now makes a false claim about
the current state and will mislead a maintainer reading the analyze-stage mapping. A parallel
stale reference exists in `src/phaze/templates/pipeline/partials/stats_bar.html:28`
(`#pipeline-stages holds the dag_canvas`). These are outside the reviewed file list but are the
documentation half of the reviewed deletions.
**Fix:** Update the comment to state the analyze workspace now fully replaces the retired
`dag_canvas.html` (deleted in Phase 62 / CUT-02), dropping the "stays reachable until CUT-02"
clause. Trim the `stats_bar.html` reference likewise.

## Info

### IN-01: Redirect-only routes still declare `response_class=HTMLResponse`

**File:** `src/phaze/routers/pipeline.py:579`, `src/phaze/routers/preview.py:12`
**Issue:** `dashboard()` and `tree_preview()` now only ever return `RedirectResponse`, but their
decorators still carry `response_class=HTMLResponse`. Behavior is correct (FastAPI honors the
returned `Response` object, so the 302 fires), but the declared `response_class` and the
generated OpenAPI response schema are now inaccurate for these routes.
**Fix:** Drop `response_class=HTMLResponse` from both decorators (a plain redirect route needs no
`response_class`), or set it to reflect a redirect.

### IN-02: base.html `<nav aria-label="Main navigation">` no longer contains navigation items

**File:** `src/phaze/templates/base.html:161`
**Issue:** After the tab-bar removal, the `<nav aria-label="Main navigation">` landmark contains
only the Phaze home logo link and the theme toggle — no actual navigation list. A `nav` landmark
labelled "Main navigation" that holds no nav items is mildly misleading to assistive-tech users
enumerating landmarks. base.html now backs only the two kept standalone pages (audit_log,
agents), so the impact is small.
**Fix:** Optional — either relabel the landmark (e.g. `aria-label="Site header"`) or demote the
wrapper from `<nav>` to a plain `<div>`/`<header>`, since its sole remaining nav affordance is the
logo home link.

### IN-03: Routers still populate `current_page` context now unused by base.html

**File:** `src/phaze/routers/cue.py:227`, `duplicates.py:107`, `proposals.py:160`, `tags.py:217`, `tracklists.py:153`
**Issue:** Each list router still sets `"current_page": "..."` in its context. base.html's tab nav
(the only consumer that keyed off `current_page` for active-tab highlighting) was removed this
phase. The key may still be read by the surviving `*_content.html`/`*_list.html` partials, so this
is not necessarily dead — but if it is not, these are now-inert context keys. Harmless either way.
**Fix:** Confirm whether the shell partials still read `current_page`; if not, drop the key from
these contexts to avoid implying a nav-highlight behavior that no longer exists.

---

_Reviewed: 2026-07-02T06:30:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
