---
phase: 57-shell-dag-rail
verified: 2026-06-29T00:00:00Z
status: passed
human_verification_resolved: 2026-06-30T02:21:57Z
human_verification_evidence: 57-HUMAN-UAT.md (5/5 passed, Claude-driven via Playwright on live app)
score: 5/5 must-haves verified
overrides_applied: 0
re_verification: null
gaps: []
human_verification:
  - test: "Navigate to / in a browser and confirm the three-column shell renders with a visible DAG rail on the left, a workspace area in the center, and a detail pane on the right"
    expected: "Three-column h-screen layout with the Analyze stage selected (blue tint + inset bar on the Analyze rail node)"
    why_human: "Visual layout and Tailwind utility classes require a real browser to evaluate; CSS aria-[current=page] variant rendering cannot be confirmed by grep"
  - test: "Click a rail stage node (e.g., Discover) and verify only the center #stage-workspace content changes — the header, rail, and right pane stay in place"
    expected: "HTMX innerHTML-swaps only #stage-workspace; the URL updates to /s/discover via hx-push-url; no full-page navigation (no browser flash/reload)"
    why_human: "HTMX swap behavior in a running browser cannot be verified from static code; the wiring attributes are present but actual XHR behavior requires a browser"
  - test: "Press ⌘K (or Ctrl+K on Windows/Linux) and verify the command palette opens; press Escape and verify it closes and focus returns to the ⌘K trigger button"
    expected: "Alpine skeleton modal opens with focus on the search input; ESC closes it; focus returns to id=cmdk-trigger"
    why_human: "Alpine.js x-data open/close state + $nextTick focus and getElementById focus-return require a live browser; cannot verify JS state transitions statically"
  - test: "Click the theme toggle button and cycle through auto / dark / light modes; verify the dark Phaze theme activates without flash on page load"
    expected: "_applyTheme fires before Alpine loads (no FOUC); .dark class toggles on <html>; Tailwind dark: utilities take effect; Jura headings visible in all modes"
    why_human: "CSS custom properties, Tailwind dark: utilities, localStorage persistence, and the no-FOUC timing all require visual browser verification"
  - test: "Visit /proposals/ directly in the browser and confirm it redirects (302) to /s/propose and the shell renders with the propose node selected"
    expected: "Browser lands on the shell at /s/propose with propose's rail node active (aria-current=page); the address bar shows /s/propose"
    why_human: "End-to-end browser navigation including redirect chain + final shell render with correct rail selection cannot be confirmed by static analysis alone"
---

# Phase 57: Shell & DAG Rail Verification Report

**Phase Goal:** Visiting `/` renders the three-column "Hybrid Console" shell with the DAG rail as the navigation spine and Analyze selected by default; clicking a rail stage swaps the center workspace via HTMX with no full-page reload; the legacy tab-bar is gone, brand/theme are preserved, and every old per-tab route resolves into the shell. This locks the cross-cutting contracts (single `#stage-workspace` swap target, fragment-only stage responses, `$store.pipeline` survival, history re-init, focus/ARIA + skip-link, theme, SRI-pinned htmx 2.0.10 / Alpine 3.15.12 / Tailwind 4.3.2) that Phases 58-62 depend on.
**Verified:** 2026-06-29T00:00:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria)

| #  | Truth | Status | Evidence |
|----|-------|--------|----------|
| SC-1 | `/` renders the three-column shell with Analyze rail node `aria-current="page"` — no redirect to `/pipeline` | ✓ VERIFIED | `shell_home` calls `_render_stage(request, "analyze", session)` (shell.py:103); rail.html sets `aria-current="page"` via `{% if stage == 'analyze' %}` (rail.html:78); `test_root_renders_shell_analyze_default` asserts 200, `id="stage-workspace"`, `data-stage="analyze"`, `id="pipeline-dag"` |
| SC-2 | Clicking a rail stage swaps only `#stage-workspace` (fragment, never extends base.html) with `hx-push-url`; `$store.pipeline` persists | ✓ VERIFIED | All 12 rail nodes carry `hx-get="/s/<id>"` `hx-target="#stage-workspace"` `hx-swap="innerHTML"` `hx-push-url="true"` (rail.html); `_stage_fragment.html` contains only `{% include stage_partial %}` — no `{% extends %}`, no `<html>`/`<head>` (confirmed grep); `$store.pipeline` defined exactly once in shell.html head |
| SC-3 | Legacy top tab-bar removed; ⌘K header affordance + status strip fed by single `/pipeline/stats` poll | ✓ VERIFIED | `aria-label="Main navigation"` absent from shell; `id="cmdk-trigger"` + ⌘K chip present in header.html; status strip binds `$store.pipeline.agentOnline` (header.html:44-48); no `setInterval` in header.html (grep confirms 0 hits); `test_tabbar_removed_header_present` passes |
| SC-4 | Auto/dark/light theme toggle and Jura/blue/wave-logo brand preserved verbatim; vendored Tailwind 4.3.2 with recomputed SRI | ✓ VERIFIED | SRI hashes identical in shell.html and base.html (htmx sha384-H5Srcfyg…, Alpine sha384-pb6hrQvo…, SSE sha384-QA9wXq…); `tailwindcss-browser-4.3.2.min.js` exists (276 272 bytes); 4.3.0 file deleted; `_applyTheme` and `Alpine.store('theme'` present in shell.html head; `Alpine.store('pipeline'` count = 1 |
| SC-5 | All 8 legacy routes resolve ≤1 hop to a 200; dead-template AST guard green | ✓ VERIFIED | All 7 routers carry conditional `HX-Request != "true"` redirect guard (proposals→/s/propose, tracklists→/s/tracklist, tags→/s/tagwrite, cue→/s/cue, duplicates→/s/dedupe, preview→/s/move, search→/?palette=1) plus pipeline.py→/; `test_redirect_resolution.py` proves ≤1-hop + HX-not-redirected; `uv run pytest tests/test_dead_template_guard.py` → 1 passed (confirmed in sandbox) |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/routers/shell.py` | GET / + GET /s/{stage}; STAGE_PARTIALS whitelist; HX fork | ✓ VERIFIED | 116 lines; `STAGE_PARTIALS` with 12 ids; `_render_stage` HX fork; 404 on unknown stage; `from .pipeline import build_dashboard_context` |
| `src/phaze/templates/shell/shell.html` | Three-column shell with lifted head, #stage-workspace, skip-link, history/focus handlers | ✓ VERIFIED | Full standalone HTML; `id="stage-workspace"` present; skip-link `href="#stage-workspace"`; `htmx:historyRestore` + `htmx:afterSwap` handlers; includes header/rail/cmdk |
| `src/phaze/templates/shell/_stage_fragment.html` | Bare `{% include stage_partial %}` — no extends | ✓ VERIFIED | 9 lines; `{% include stage_partial %}` only; no `{% extends %}`, no `<html`/`<head` (grep: 0 matches) |
| `src/phaze/templates/shell/partials/rail.html` | DAG rail: 12 nodes in prototype order, HTMX wiring, aria-current, x-text live counts | ✓ VERIFIED | 154 lines; 10 hx-target="#stage-workspace" in source (expands to 14 at render due to {% for %} loop); aria-current via Jinja condition; live counts on 6 nodes, no count on trackid/5 amber nodes |
| `src/phaze/templates/shell/partials/header.html` | Wave logo + ⌘K button + D-05 status strip + theme toggle | ✓ VERIFIED | 79 lines; `id="cmdk-trigger"`; `$dispatch('cmdk:open')`; `$store.pipeline.agentOnline` bound; no setInterval |
| `src/phaze/templates/shell/partials/cmdk_modal.html` | Alpine skeleton modal: role=dialog, ⌘K/Ctrl+K keybind, ESC close, focus contract, ?palette=1 auto-open | ✓ VERIFIED | 69 lines; `role="dialog"` + `aria-modal="true"`; `@keydown.window.cmd.k.prevent` + `.ctrl.k`; `@keydown.escape.window`; `$nextTick` input focus; `getElementById('cmdk-trigger').focus()` on close; `x-init` reads `palette` query param; no `@alpinejs/focus` |
| `src/phaze/static/vendor/tailwindcss-browser-4.3.2.min.js` | Vendored Tailwind 4.3.2 | ✓ VERIFIED | 276 272 bytes; 4.3.0 file deleted |
| `tests/test_shell_routes.py` | 6 behavioral tests for SHELL-01..04 | ✓ VERIFIED | 6 tests filled; `test_root_renders_shell_analyze_default`, `test_stage_fragment_is_bare`, `test_unknown_stage_404`, `test_rail_nodes_wired`, `test_tabbar_removed_header_present`, `test_theme_and_store_preserved` |
| `tests/test_redirect_resolution.py` | 8-route ≤1-hop + HX-not-redirected test | ✓ VERIFIED | `CANONICAL` map with 8 entries; `follow_redirects=False` hop count; `test_hx_filter_not_redirected`; uses `effective_route_paths` (never `app.routes`) |
| `tests/test_dead_template_guard.py` | jinja2.meta orphan-template AST guard, green | ✓ VERIFIED | `find_referenced_templates` closure; entry set = all `.html` literals in routers/*.py; `_ALLOWLIST` with 1 justified entry (tracklists/partials/toast.html for CUT-02); 1 passed in sandbox |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/phaze/main.py` | `src/phaze/routers/shell.py` | `app.include_router(shell.router)` (prefix-less) | ✓ WIRED | main.py:199; `from phaze.routers import (… shell …)` |
| `src/phaze/routers/shell.py` | `src/phaze/routers/pipeline.py` | `from .pipeline import build_dashboard_context` | ✓ WIRED | shell.py:31; `build_dashboard_context` called at shell.py:90 |
| `src/phaze/templates/shell/shell.html` | `#stage-workspace` | `{% include stage_partial %}` inside the swap target div | ✓ WIRED | shell.html:186; `<div id="stage-workspace" data-stage="{{ stage }}" …>{% include stage_partial %}</div>` |
| `src/phaze/templates/shell/partials/rail.html` | `#stage-workspace` | `hx-get="/s/<stage>" hx-target="#stage-workspace" hx-swap="innerHTML" hx-push-url="true"` on every navigable node | ✓ WIRED | rail.html: every navigable button carries all 4 HTMX attrs |
| `src/phaze/templates/shell/partials/rail.html` | `$store.pipeline` | `x-text="$store.pipeline.<key>"` on 6 live-count nodes (discovered/metadataDone/fingerprintDone/analyzeActive/tracklistDone/proposalsDone) | ✓ WIRED | All 6 x-text bindings reference EXISTING keys from base.html:106-136; trackid + 5 amber items have no count binding |
| `src/phaze/templates/shell/partials/header.html` | `$store.pipeline.agentOnline` | `:class` dot binding + `x-text` count | ✓ WIRED | header.html:44-48; no new store keys, no setInterval |
| Legacy routers (7) | `/s/<stage>` or `/?palette=1` | `if HX-Request != "true": return RedirectResponse(…, 302)` as first handler statement | ✓ WIRED | Confirmed in proposals.py, tracklists.py, tags.py, cue.py, duplicates.py, preview.py, search.py + pipeline.py→/ |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `shell/shell.html` → analyze workspace | Dashboard context (dag context, counts) | `build_dashboard_context(request.app.state, session)` in shell.py:90 | DB-backed (PostgreSQL, not runnable in sandbox) | ✓ FLOWING — shared builder factored from `dashboard()` per plan; same DB queries, no duplication |
| `rail.html` live counts | `$store.pipeline.*Done/Active` | Seeded to 0 in shell.html head; updated by existing `/pipeline/stats` 5s OOB poll | Real DB values via poll | ✓ FLOWING — EXISTING poll mechanism; Phase 57 makes no changes to the poll path |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| `test_dead_template_guard.py` | `uv run pytest tests/test_dead_template_guard.py -q` | 1 passed in 0.49s | ✓ PASS |
| `test_base_html_sri.py` | `uv run pytest tests/test_base_html_sri.py -q` | 3 passed in 0.49s (static pin check) | ✓ PASS |
| Shell router route registration | `grep -n "include_router(shell" src/phaze/main.py` | Line 199 confirmed | ✓ PASS |
| STAGE_PARTIALS contains 12 ids | `grep -c '".*":' src/phaze/routers/shell.py` | 12 entries confirmed | ✓ PASS |
| `_stage_fragment.html` has no extends/html/head | `grep -c "extends\|<html\|<head" …/_stage_fragment.html` | 0 | ✓ PASS |
| All 7 legacy routers have redirect guard | `grep -n "HX-Request.*!=.*true"` on all 7 | All confirmed | ✓ PASS |
| Tailwind 4.3.0 deleted | `ls …/tailwindcss-browser-4.3.0.min.js` | No such file | ✓ PASS |
| Tailwind 4.3.2 non-empty | `wc -c …/tailwindcss-browser-4.3.2.min.js` | 276272 bytes | ✓ PASS |
| shell.html `Alpine.store('pipeline'` count = 1 | `grep -c` | 1 | ✓ PASS |
| DB-backed tests (`test_shell_routes.py`, `test_redirect_resolution.py`) | `uv run pytest` full suite | Not runnable (no DB in sandbox); executor verified 2517 passed, 0 failed, 97.24% coverage against ephemeral DB | ? SKIP (environmental) |

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
|-------------|-------------|-------------|--------|---------|
| SHELL-01 | Plans 01, 02 | `GET /` renders the shell with Analyze selected by default | ✓ SATISFIED | shell.py `shell_home` → `_render_stage(…, "analyze")`; `test_root_renders_shell_analyze_default` |
| SHELL-02 | Plans 02, 03 | Persistent DAG rail with HTMX stage swaps (no full-page nav) | ✓ SATISFIED | All 12 rail nodes wired; fragment response is bare; `test_stage_fragment_is_bare` + `test_rail_nodes_wired` |
| SHELL-03 | Plan 03 | Legacy tab-bar removed; ⌘K header + status strip | ✓ SATISFIED | header.html carries ⌘K + D-05 strip; tab-bar absent from shell; `test_tabbar_removed_header_present` |
| SHELL-04 | Plans 01, 02 | Theme/brand preserved; SRI-pinned libs at locked versions | ✓ SATISFIED | SRI hashes identical in shell.html and base.html; Tailwind 4.3.2 vendored; `test_theme_and_store_preserved`; `test_base_html_sri.py` |
| SHELL-05 | Plans 01, 04 | Old routes redirect into the shell; dead-template guard green | ✓ SATISFIED | 7 + 1 conditional redirects wired; `test_redirect_resolution.py`; `test_dead_template_guard.py` |

All 5 Phase 57 requirements are satisfied. No orphaned requirements.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `cue.py`, `duplicates.py`, `proposals.py`, `tags.py`, `tracklists.py` | Various | Unreachable full-page `return templates.TemplateResponse(…list.html…)` after the non-HX redirect guard and HX partial return (WR-01) | ⚠️ Warning | Dead code; the `*.html` string literal is what keeps the legacy template "reachable" for the dead-template guard; a future developer removing the dead branch would silently orphan the template. Intentionally left for CUT-02 (Phase 62), consistent with the plan's scope boundary. |
| `src/phaze/templates/shell/shell.html` lines 42-169 vs `src/phaze/templates/base.html` lines 42-169 | — | `<head>` machinery duplicated verbatim (WR-02): 30-key `$store.pipeline` seed + theme script + `@theme` tokens | ⚠️ Warning | Maintainability: if a later phase adds a store key to `base.html`, `shell.html` drifts silently and any shell binding to the new key reads `undefined`. Fix in a later phase (extract to `_head_core.html` partial). Not blocking Phase 57 goal. |
| `src/phaze/templates/shell/shell.html` | 232 | `_focusStageHeading()` queries `ws.querySelector('h1')` — silently no-ops on Analyze because `dag_canvas.html` has no `h1` (only an `h2` at line 259) (WR-03) | ⚠️ Warning | Focus-to-heading is absent for the default workspace (`/`). The 11 placeholder stages work via `_stage_placeholder.html`'s `<h1 tabindex="-1">`. Skip-link and `aria-current` are present; full ARIA/focus-trap baseline is Phase 61/62. |
| `src/phaze/routers/preview.py` | ~50 | HX branch (`HX-Request: true`) returns `preview/tree.html` which `{% extends "base.html" %}` — a full document injected into `#stage-workspace` (WR-04) | ⚠️ Warning | Harmless today (confirmed by grep: zero `hx-get="/preview…"` callers). Becomes a live trap when the Move workspace bridges `/preview/` in Phases 58-61. No blocker for Phase 57. |

No `TBD`, `FIXME`, or `XXX` markers found in Phase 57 new files.

### Human Verification Required

#### 1. Three-Column Shell Visual Layout

**Test:** Navigate to `/` in a real browser (after `docker-compose up` or `just dev`)
**Expected:** Three-column h-screen layout — `w-[280px]` DAG rail on the left, scrollable center workspace, `w-[350px]` right pane — with the Analyze rail node showing a blue tint and inset-left bar (`bg-blue-500/10` + `shadow-[inset_3px_0_0_...]`)
**Why human:** Visual layout, Tailwind `aria-[current=page]` variant CSS rendering, and column proportions require a real browser

#### 2. HTMX Rail Navigation (No Full-Page Reload)

**Test:** Click the "Discover" rail node from the Analyze workspace
**Expected:** Only `#stage-workspace` content changes (placeholder panel); the header, rail, and right pane remain; the URL updates to `/s/discover` via `hx-push-url`; no browser flash (no full-page navigation); back button returns to Analyze
**Why human:** HTMX XHR + innerHTML swap + `hx-push-url` history behavior requires a running browser; static wiring confirmed but interaction cannot be verified programmatically

#### 3. ⌘K Skeleton Modal Open/Close/Focus

**Test:** Press ⌘K (macOS) or Ctrl+K (Windows/Linux); then press Escape
**Expected:** Command palette panel opens centered at top (`left-1/2 top-24`); focus moves to the search input; pressing Escape closes it and focus returns to the ⌘K trigger button (`id="cmdk-trigger"`); clicking the backdrop also closes it
**Why human:** Alpine.js `x-data` state, `$nextTick` focus, `getElementById.focus()` on close, and CSS transition all require a live browser; modal is skeleton-only (D-04) so content accuracy is not the check — only the open/close/focus contract

#### 4. Theme Toggle (No FOUC + Dark Mode Visual)

**Test:** Load the page in dark OS mode; cycle through auto/dark/light with the toggle button
**Expected:** No flash on load (the `_applyTheme` IIFE applies `.dark` before Alpine); the Jura font renders for headings; blue accent color appears on active nodes; dark/light/auto cycling works with LocalStorage persistence across reload
**Why human:** Flash-before-Alpine timing, Tailwind CSS custom property resolution, and localStorage persistence require a running browser

#### 5. Legacy Bookmark → Shell Resolution (End-to-End Browser Check)

**Test:** Type `/proposals/` directly in the browser address bar
**Expected:** Browser redirects (302) to `/s/propose`; the shell renders with the "Propose" rail node active; the placeholder workspace content shows; address bar shows `/s/propose`
**Why human:** The test suite confirms the 302 response; this human check confirms the final rendered shell state with the correct rail node highlighted

### Gaps Summary

No gaps blocking the Phase 57 goal. All 5 ROADMAP success criteria are verified in the codebase.

The code review (57-REVIEW.md) identified 4 warnings (WR-01 through WR-04) and 3 info items. None are blockers: WR-01/04 are intentionally deferred to Phase 62 CUT-02; WR-02 is a maintainability concern for later phases; WR-03 is a partial a11y gap (focus-to-heading silently fails on Analyze, but skip-link and aria-current are in place).

The 5 human verification items above are standard browser-behavior checks for a UI phase — the server-side wiring, HTMX attributes, Alpine data contracts, and structural HTML are all verified programmatically and by the automated test suite (2517 passed, 97.24% coverage per executor; 4 non-DB static tests confirmed green in this sandbox).

---

_Verified: 2026-06-29T00:00:00Z_
_Verifier: Claude (gsd-verifier)_
