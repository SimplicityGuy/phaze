---
phase: 57-shell-dag-rail
reviewed: 2026-06-29T00:00:00Z
depth: standard
files_reviewed: 21
files_reviewed_list:
  - src/phaze/main.py
  - src/phaze/routers/shell.py
  - src/phaze/routers/pipeline.py
  - src/phaze/routers/cue.py
  - src/phaze/routers/duplicates.py
  - src/phaze/routers/preview.py
  - src/phaze/routers/proposals.py
  - src/phaze/routers/search.py
  - src/phaze/routers/tags.py
  - src/phaze/routers/tracklists.py
  - src/phaze/templates/base.html
  - src/phaze/templates/shell/shell.html
  - src/phaze/templates/shell/_stage_fragment.html
  - src/phaze/templates/shell/partials/_stage_placeholder.html
  - src/phaze/templates/shell/partials/cmdk_modal.html
  - src/phaze/templates/shell/partials/header.html
  - src/phaze/templates/shell/partials/rail.html
  - tests/test_redirect_resolution.py
  - tests/test_shell_routes.py
  - tests/test_dead_template_guard.py
findings:
  critical: 0
  warning: 4
  info: 3
  total: 7
status: issues_found
---

# Phase 57: Code Review Report

**Reviewed:** 2026-06-29T00:00:00Z
**Depth:** standard
**Files Reviewed:** 21
**Status:** issues_found

## Summary

Phase 57 is a presentation-only milestone: a three-column app shell, a `GET /` +
`GET /s/{stage}` shell router, an HTMX DAG rail, a ⌘K skeleton modal, and conditional
redirects on legacy GET handlers.

I focused the adversarial pass on the four flagged risk surfaces and clear them all:

- **Template-path injection (T-57-01):** SAFE. `shell.py` matches `stage` against the
  static `STAGE_PARTIALS` whitelist, 404s on a miss (`shell_stage`), and only ever
  interpolates `STAGE_PARTIALS[stage]` (static literals) into `{% include stage_partial %}`.
  The raw `stage` reaches templates only inside autoescaped `data-stage="{{ stage }}"` and
  the placeholder `<h1>{{ stage }}</h1>`. No attacker-controlled path component reaches a
  template loader.
- **HX-conditional redirect:** CORRECT. Every legacy GET handler guards on
  `request.headers.get("HX-Request") != "true"`, so HTMX filter/sort/pagination requests
  fall through to their existing partial branch and are never hijacked (verified by
  `test_hx_filter_not_redirected`).
- **Open-redirect / SSRF:** NONE. Every `RedirectResponse(url=...)` target is a hardcoded
  string literal (`/`, `/?palette=1`, `/s/<stage>`); no request input flows into a redirect
  URL. Every redirect target stage (`propose`, `tracklist`, `tagwrite`, `cue`, `dedupe`,
  `move`) is present in `STAGE_PARTIALS`, so no redirect lands on a 404 and no loop exists.
- **HTMX/Alpine wiring:** Largely correct; the rail nodes, fragment-vs-full fork, and
  ⌘K keybindings (`.cmd.k` / `.ctrl.k`) are sound.

No blockers. The findings below are maintainability/robustness defects introduced or
exposed by the cutover, plus a real a11y-baseline gap on the default node.

## Warnings

### WR-01: Unreachable full-page render return in 5 legacy list handlers

**File:** `src/phaze/routers/cue.py:236`, `src/phaze/routers/duplicates.py:113`, `src/phaze/routers/proposals.py:165`, `src/phaze/routers/tags.py:209`, `src/phaze/routers/tracklists.py:160`
**Issue:** Each list handler now opens with a non-HX redirect guard, then ends with the
original two-way fork:
```python
if request.headers.get("HX-Request") != "true":
    return RedirectResponse(url="/s/...", status_code=302)   # non-HX always returns here
...
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(... partial ...)        # HX always returns here
return templates.TemplateResponse(... list.html ...)          # DEAD: unreachable
```
By the time control reaches the bottom, `HX-Request == "true"` is guaranteed, so the
inner `if` always returns and the final full-page `return` (`cue/list.html`,
`proposals/list.html`, etc.) can never execute. Confirmed: those templates `{% extends "base.html" %}`,
so they are full documents that should never be emitted into an HMTX swap target anyway.
The dead branch is currently load-bearing only as a side effect — its `"...list.html"`
string literal is what keeps the template "reachable" for `test_dead_template_guard.py`.
That couples a dead code path to a passing test: a maintainer who deletes the unreachable
line to satisfy a linter will silently flip the template to an orphan (or vice-versa).
**Fix:** Collapse each handler's tail to a single `return` of the partial and explicitly
allowlist the legacy full-page template in `_ALLOWLIST` (with the CUT-02 justification
already used for `tracklists/partials/toast.html`) until Phase 62 deletes it — so the
"keep the template alive" intent is declared in one place instead of hidden behind
unreachable code. Example for `proposals.py`:
```python
if request.headers.get("HX-Request") != "true":
    return RedirectResponse(url="/s/propose", status_code=302)
...
# HX-only from here (non-HX already redirected): always the content fragment.
return templates.TemplateResponse(request=request, name="proposals/partials/proposal_content.html", context=context)
```

### WR-02: Entire `<head>` (theme script + 30-key `$store.pipeline`) duplicated verbatim between `base.html` and `shell.html`

**File:** `src/phaze/templates/shell/shell.html:42-169` (vs `src/phaze/templates/base.html:42-169`)
**Issue:** `shell.html` deliberately does not extend `base.html`, so the no-FOUC theme
script, the `Alpine.store('theme', …)` block, the `Alpine.store('pipeline', …)` seed (30+
keys: `discovered`, `metadataDone`, `analyzeActive`, `scrapeBusy`, `metadataPaused`, …),
the Tailwind `@theme` palette, and the font/vendor `<script>`/`<link>` tags are copy-pasted
byte-for-byte into the shell. The rail (`x-text="$store.pipeline.tracklistDone"`,
`analyzeActive`, etc.) and header (`$store.pipeline.agentOnline`) bind directly to these
keys. When a later phase adds a store key to `base.html` (the file has grown one phase at a
time per its own comments), `shell.html` will silently drift and any shell binding to the
new key will read `undefined`. This is a maintainability defect with a latent runtime
failure mode, not a style nit.
**Fix:** Extract the shared `<head>` machinery (theme script + `$store.pipeline` seed +
Tailwind/`@theme` + font/vendor tags) into a single included partial (e.g.
`partials/_head_core.html`) that both `base.html` and `shell.html` `{% include %}`, so the
store schema has one source of truth.

### WR-03: Focus-to-heading a11y baseline is a no-op on the Analyze (default) node

**File:** `src/phaze/templates/shell/shell.html:228-236`
**Issue:** `_focusStageHeading()` does `ws.querySelector('h1')` and focuses it after every
rail swap / history restore — the stated Phase 57 "Focus/ARIA baseline." The
`_stage_placeholder.html` provides `<h1 tabindex="-1">`, so the 11 placeholder stages work.
But the Analyze node — the `/` default and the single most-used workspace — bridges
`pipeline/partials/dag_canvas.html`, which contains **no `<h1>`** (verified). So on a swap
to `/s/analyze` and on the initial `/` load path that exercises this handler, focus is
never moved; keyboard/screen-reader users are left where they were. The baseline the phase
claims to deliver is silently absent on its primary node.
**Fix:** Either add a visually-hidden focusable `<h1 tabindex="-1">Analyze</h1>` to the
Analyze workspace, or broaden the selector to the first heading and fall back to the
workspace container:
```js
const heading = ws.querySelector('h1, h2, [role="heading"]') || ws;
if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
heading.focus();
```

### WR-04: `/preview/` HX branch returns a full `base.html` document, violating the fragment contract

**File:** `src/phaze/routers/preview.py:45-65`
**Issue:** `tree_preview` redirects non-HX requests to `/s/move`, but its only remaining
reachable branch (HX-Request: true) returns `preview/tree.html`, which `{% extends "base.html" %}`
— i.e. a complete `<!DOCTYPE html>…</html>` document. Every other shell-resolving route
either redirects or returns a content-only partial; the shell's `#stage-workspace` swap
contract (and `_stage_fragment.html`'s explicit "NEVER `{% extends %}`" rule) requires
fragments. This is harmless *today* only because nothing HTMX-requests `/preview/` (grep
confirms zero `hx-get="/preview…"` callers), but it is a primed trap: when the Move
workspace bridges `/preview/` content in Phase 58-61, an `hx-get="/preview/"` will inject a
nested full document (duplicate `<html>`/`<head>`/landmarks/skip-links) into the shell —
exactly the ROADMAP-locked anti-pattern `_stage_fragment.html` warns against.
**Fix:** When `/preview/` is bridged, give it a content-only partial (mirroring
`proposals/partials/proposal_content.html`) and return that from the HX branch; until then,
add a comment that the HX branch is intentionally inert and unreachable, or drop the HX
branch entirely and let the route be redirect-only in Phase 57.

## Info

### IN-01: `/search/` redirect silently discards existing query params

**File:** `src/phaze/routers/search.py:39-40`
**Issue:** A bookmarked `/search/?q=coachella&artist=foo&bpm_min=120` redirects to
`/?palette=1`, dropping `q` and all filters. No functional loss in Phase 57 (the ⌘K palette
is a skeleton with no search wiring), so this is informational — but when the palette gains
search in Phase 61, this redirect should forward the query (e.g. `/?palette=1&q=...`) or the
bookmark-resolution promise (SHELL-05) is only half-kept.
**Fix:** Carry the original query string into the redirect target when palette search lands.

### IN-02: Dead-template guard treats any `.html` string literal (incl. docstrings/comments) as a render entry

**File:** `tests/test_dead_template_guard.py:50,68-73`
**Issue:** `_HTML_LITERAL = re.compile(r'["\']([^"\']+\.html)["\']')` scans whole router
source, so a `.html` name mentioned only in a docstring or comment is counted as a reachable
entry template. This biases the guard toward false-negatives: a genuinely orphaned template
can be masked by an incidental mention. Acceptable as the documented permissive tradeoff,
but worth noting that the guard cannot prove WR-01's full-page templates are *rendered* —
only that their names appear in source.
**Fix:** None required for Phase 57; if tightened later, restrict extraction to
`TemplateResponse(...)`/`_render_partial(...)` call arguments rather than free-text literals.

### IN-03: Shell opens a DB session for every `/s/{stage}` even when unused

**File:** `src/phaze/routers/shell.py:101,107` (`Depends(get_session)`)
**Issue:** `shell_home` and `shell_stage` always acquire a session via `get_session`, but
`_render_stage` only touches the DB for `stage == "analyze"`; the 11 placeholder stages open
and discard a connection per navigation. Not a correctness bug (sessions are cleanly
released) and out of v1 perf scope, but it is needless coupling that will matter once
non-analyze stages have their own (possibly different) data needs.
**Fix:** Acceptable as-is for the placeholder phase; revisit per-stage when the real
workspaces land so each stage declares only the dependencies it uses.

---

_Reviewed: 2026-06-29T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
