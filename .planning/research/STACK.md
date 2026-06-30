# Stack Research

**Domain:** Server-rendered admin console — frontend patterns for a DAG-centric hybrid console (v7.0 UI redesign)
**Researched:** 2026-06-29
**Confidence:** HIGH

> **Scope note.** The backend/runtime stack (FastAPI, Jinja2, SQLAlchemy, SAQ, Postgres, Redis) is **unchanged** and is NOT re-researched here — prior STACK.md content lives in git history. v7.0 is an information-architecture + presentation rewrite over existing routers/services. This file answers one question only: **which patterns, current versions, and minimal CDN libraries best implement the new shell capabilities WITHIN the locked Jinja2 + HTMX + Tailwind + Alpine stack, with no SPA and no build step.**
>
> The locked stack is a confirmed-good fit and is already in production (`src/phaze/templates/base.html`). Every recommendation below either (a) bumps an already-vendored dependency to current, or (b) adds exactly one small official Alpine plugin. Nothing else is added.

---

## Recommended Stack

### Core Technologies (already in the stack — bump to current)

These are the four libraries already loaded in `src/phaze/templates/base.html`. v7.0 keeps all four and bumps each to current stable. All four are delivered the way they are today — three via SRI-pinned CDN `<script>` tags, Tailwind self-vendored in `static/vendor/`.

| Technology | Current ver | In repo now | Purpose in v7.0 | Why / action |
|------------|-------------|-------------|-----------------|--------------|
| **htmx** (`htmx.org`) | **2.0.10** | 2.0.7 | Rail→workspace stage swaps, deep-link history, OOB rail-count/header-strip updates, live polling | Bump 2.0.7→2.0.10 (patch-level; bug fixes only, no API change). **Stay on the 2.0.x line — do NOT adopt the htmx 4.0.0 beta** (see What NOT to Use). Re-pin SRI hash on bump. |
| **htmx SSE ext** (`htmx-ext-sse`) | **2.2.4** | 2.2.4 | Already used ONLY by the bounded execution-progress card | Keep as-is. Already current. Do not expand its use (see Live Updates). |
| **Alpine.js** (`alpinejs`) | **3.15.12** | 3.15.9 | Local UI state: ⌘K palette open/close + filter, theme store, per-row selection, slide-in record panel | Bump 3.15.9→3.15.12 (patch). Re-pin SRI. |
| **Tailwind browser build** (`@tailwindcss/browser`) | **4.3.2** | 4.3.0 | Utility styling, `phaze-bg`/`phaze-panel` tokens, dark variant, Jura/Inter fonts | Bump 4.3.0→4.3.2. **Keep self-vendoring it** in `static/vendor/` (the existing file documents *why* — jsDelivr per-edge minification breaks SRI). Re-download exact file, bump the filename version. |

### Supporting Libraries (the ONE addition)

| Library | Current ver | Purpose | When to use |
|---------|-------------|---------|-------------|
| **@alpinejs/focus** | **3.15.12** | Provides Alpine's `x-trap` directive — focus-trap + focus-restore for the ⌘K command palette and the slide-in full-record panel. Wraps the `focus-trap` library, exposed as an official Alpine plugin. | Add as one SRI-pinned CDN `<script>` in `base.html`, loaded `defer` **before** the Alpine core script (Alpine plugins must register before core init). Version MUST match the Alpine core version (3.15.12). This is the single new dependency v7.0 should add — it is what makes CUT-01's "focus trap on ⌘K / ARIA dialog" correct without hand-rolling focus management in raw JS. |

**Why add a plugin at all, when the project prides itself on zero-build minimalism?** Focus trapping done by hand (capture Tab/Shift-Tab, track first/last focusable, restore focus on close, handle dynamically-added result rows) is the single most commonly-broken accessibility primitive. `x-trap` is ~3KB, official, CDN-delivered, no build step, and turns the entire concern into one attribute: `x-trap.inert.noscroll="open"`. It directly de-risks CUT-01. This is the *only* library worth adding; everything else below is a pattern, not a dependency.

### Development Tools

No new dev tooling. No Node, no npm, no PostCSS, no Tailwind CLI, no bundler. The browser Tailwind build compiles utilities at runtime in the page; CDN scripts are loaded directly. This is a deliberate, already-shipping constraint — preserve it.

| Tool | Purpose | Notes |
|------|---------|-------|
| `curl ... \| openssl dgst -sha384 -binary \| openssl base64 -A` | Compute SRI `integrity` hash for each bumped/new CDN script | Required on every version bump — `base.html` pins `integrity=` + `crossorigin="anonymous"` on htmx, sse, and Alpine tags. The new `@alpinejs/focus` tag needs its own hash. |

## Installation

There is no package manager step for the frontend — these are CDN tags + one vendored file. Concrete changes to `src/phaze/templates/base.html`:

```html
<!-- Tailwind: re-vendor 4.3.0 -> 4.3.2 into static/vendor/, bump filename -->
<script src="/static/vendor/tailwindcss-browser-4.3.2.min.js"></script>

<!-- HTMX core: 2.0.7 -> 2.0.10 (re-pin integrity) -->
<script src="https://unpkg.com/htmx.org@2.0.10/dist/htmx.min.js"
        integrity="sha384-<recompute>" crossorigin="anonymous"></script>

<!-- HTMX SSE ext: unchanged (2.2.4) -->
<script src="https://cdn.jsdelivr.net/npm/htmx-ext-sse@2.2.4/sse.js"
        integrity="sha384-QA9wXqexhwzXTuTvuF5QP82pddm3R2hy81UzXi7ioNTqNF2b75hlkkSGjafohhL3"
        crossorigin="anonymous"></script>

<!-- NEW: Alpine focus plugin — MUST come before Alpine core, same version -->
<script defer src="https://cdn.jsdelivr.net/npm/@alpinejs/focus@3.15.12/dist/cdn.min.js"
        integrity="sha384-<recompute>" crossorigin="anonymous"></script>

<!-- Alpine core: 3.15.9 -> 3.15.12 (re-pin integrity) -->
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js"
        integrity="sha384-<recompute>" crossorigin="anonymous"></script>
```

---

## The five capability questions — patterns + recommendations

### 1. Rail → workspace stage swap (HTMX): hx-target / hx-swap / hx-push-url / history / OOB

**Pattern.** Each DAG-rail node is a link/button with:
```html
<a hx-get="/shell/analyze" hx-target="#workspace" hx-swap="innerHTML"
   hx-push-url="/analyze" role="link" aria-current="page" ...>Analyze</a>
```
- **`hx-target="#workspace"`** — a single stable center content region (the prototype names it `#center`; pick one id and keep it). The rail and right pane live *outside* this region so they are not clobbered by the swap.
- **`hx-swap="innerHTML"`** — replace the workspace body. (`innerHTML` over `outerHTML` so the `#workspace` container + its id survive the swap.) This mirrors the existing `#pipeline-stats` div which already does `hx-swap="innerHTML"` (`dashboard.html:50`).
- **`hx-push-url`** — deep-links the stage so reload / bookmark / share lands on that stage (satisfies SHELL-05's "old routes redirect into the corresponding shell stage state"). Recommend a clean per-stage URL (`/analyze`, `/review/rename`, …) and redirect the legacy routes (`/proposals` etc.) to them.

**The double-render rule (the one correctness pitfall to design for up front).** With `hx-push-url`, three things can request a stage URL: (a) the HTMX swap, (b) browser **back/forward** (htmx history restore), (c) a **cold load / bookmark** of the URL. (a) wants a *fragment* (just the workspace inner HTML); (b) and (c) want the *full shell* with that stage already selected. The router must branch on the **`HX-Request` header**: present → return the workspace partial; absent → return `base.html` shell with the workspace pre-rendered and the rail node pre-selected. **The codebase already does exactly this `HX-Request`-vs-full-page split elsewhere** (partials vs. full templates throughout `routers/`), so this is an established local idiom, not a new technique. Note htmx caches the swapped target's innerHTML in history and restores *only `#workspace`* on back — so the rail's selected highlight, the breadcrumb, and the right pane must be **derived from the URL**, not from the swap, or they will desync on back-navigation (see below).

**Rail "selected" state + header breadcrumb (avoid the back-button desync).** Make the active-stage highlight a **function of the current URL**, recomputed on `htmx:afterSettle`, `htmx:historyRestore`, and `popstate` — single source of truth = the URL. Concretely: a small Alpine component on the rail reads `location.pathname` and toggles `aria-current="page"` + the active classes. This survives back/forward correctly because htmx history restore only re-injects `#workspace`; everything chrome-side reads the restored URL. (Alternative — re-render the rail OOB on every swap — is *wrong* here because history restore does not replay OOB swaps, so back-navigation would leave the rail highlighting the wrong node.)

**`<title>` on stage change.** No extension needed — htmx core extracts a `<title>` element found in the swap response and updates `document.title` automatically. Put `<title>` in the partial. (Do **not** add `htmx-ext-head-support`; it manages `<head>` CSS/JS de-duplication across boosted navigations, which this design does not need.)

**OOB for the header status strip + rail live counts.** Use **out-of-band swaps** exactly as the dashboard already does. The existing `#pipeline-stats` poll response (`stats_bar.html`) carries a stack of `hx-swap-oob="true"` fragments that update cards and Alpine-store seeds living *outside* the polled target — this is the canonical, in-repo, battle-tested template for "one response updates many regions." For v7.0: the live poll response (see Q3) carries `hx-swap-oob` fragments for `#rail-counts` (per-stage badges) and `#header-status` (agent/compute dots). **Do not make each stage swap re-render the rail** — keep rail counts owned by the single poller, so a stage navigation and a count refresh never race. (The existing `stats_bar.html` header comment already articulates this exact "don't clobber the interactive subtree" discipline.)

**Extensions needed for this:** none beyond what's loaded. Core htmx 2.0.10 covers `hx-get`/`hx-target`/`hx-swap`/`hx-push-url`/`hx-swap-oob`/title-extraction.

### 2. ⌘K command palette: Alpine alone vs. a palette library

**Recommendation: Alpine alone + `@alpinejs/focus` for the focus trap. Do NOT add a command-palette library.**

There is no good CDN-friendly, no-build palette library: `cmdk` and `kbar` are React-only; `ninja-keys` is a Web Component but pulls in lit, fights the Tailwind/Jura/`phaze-bg` brand styling, and is overkill for "filter a list + hit a search endpoint." The prototype already proves the interaction in ~5 lines of vanilla JS (`prototype.html:387` — a global `keydown` listener for `(metaKey||ctrlKey)+k` and `Escape`). Productionize that as Alpine:

- **Open/close + filter:** Alpine `x-data` modal; global `@keydown.window` for ⌘K (reuse the existing keyboard idiom — `proposals/list.html` already does `@keydown.window="handleKeydown($event)"` with an Alpine component, so this is an established pattern).
- **Search results:** the input does `hx-get="/command/search" hx-trigger="keyup changed delay:200ms" hx-target="#cmd-results"` — server renders matching files/tracklists/artists as a partial. Reuse the existing search router/service; this is server-rendered, debounced, no client search index.
- **Quick commands** (scan, jump-to-stage, open Agents): static `<li>` entries the same Alpine filter narrows client-side.
- **Focus trap + restore:** wrap the dialog in `x-trap.inert.noscroll="open"` from `@alpinejs/focus`. This handles Tab cycling, returns focus to the trigger on close, and `inert`s the rest of the shell.

**Accessibility (ARIA combobox pattern, satisfies CUT-01):**
- Container: `role="dialog" aria-modal="true" aria-label="Command palette"`.
- Input: `role="combobox" aria-expanded="true" aria-controls="cmd-results" aria-activedescendant="<focused option id>"`, `autocomplete="off"`.
- Results: `role="listbox" id="cmd-results"`; each result `role="option" id=... aria-selected`.
- Arrow-up/down moves `aria-activedescendant`; Enter activates; Escape closes (already in the prototype).

### 3. Live updates for stage workspaces + rail counts: keep HTMX polling, do NOT move to SSE

**Recommendation: KEEP HTMX polling (`hx-trigger="every Ns"`). Reserve SSE for the one bounded job it already serves.**

**The existing stats-poll pattern to reuse (named + located):**
- **Trigger / container:** `src/phaze/templates/pipeline/dashboard.html:50` — `<div id="pipeline-stats" hx-get="/pipeline/stats" hx-trigger="every 5s" hx-swap="innerHTML">`.
- **Polled partial (the OOB carrier):** `src/phaze/templates/pipeline/partials/stats_bar.html` — renders the visible stat grid AND, guarded by `if oob_counts`, a stack of `hx-swap-oob="true"` fragments that (i) re-seed the `$store.pipeline` Alpine store via `x-init` and (ii) re-render ~8 cards that live outside `#pipeline-stats` (straggler, awaiting-cloud, inadmissible, localqueue, admission-state, …). The `dag.items()` loop emits one `#dag-seed-<key>` OOB paragraph per store key.
- **Server endpoint:** `src/phaze/routers/pipeline.py:549` — `@router.get("/pipeline/stats")` → `pipeline_stats_partial(...)`, which sets `oob_counts=True` so OOB fragments fire only on the poll (never on the initial full-page include — emitting OOB at page load would create duplicate-id DOM).
- **Other live pollers in repo:** `recent_scans_table.html:18` (every 5s), `admin/partials/agents_table.html:16` (every 5s), `tracklists/.../scan_progress.html:29` (every 3s), `pipeline/partials/scan_progress_card.html:13` (every 2s, halts on terminal state).

**Why polling, not SSE, for a single-user tool:** polling is stateless, degrade-safe (a dropped tick self-heals on the next), needs no long-lived connection bookkeeping behind uvicorn/TLS, and is already the project's proven idiom across five surfaces. SSE (`htmx-ext-sse@2.2.4`) **is** loaded — but it is used in exactly one place (`execution/partials/progress.html:27`, `hx-ext="sse"` / `sse-connect`) for a **bounded, terminal** execution-progress stream that closes on `complete`/`complete_with_errors`. That is the correct use of SSE: a short-lived burst of high-frequency updates with a definite end. Continuous workspace/rail "is anything happening" status is exactly what 5s polling is for. **Do not migrate the rail counts or stage workspaces to SSE** — it would add connection-lifecycle complexity for zero benefit at one concurrent user. WORK-05 ("refresh live via the existing stats-poll pattern") is literally a directive to reuse `#pipeline-stats` + `stats_bar.html` OOB seeds for the new rail/workspace counts.

### 4. DAG rail rendering: pure HTML/CSS/Tailwind list with state chips — NO graph library

**Recommendation: render the rail as a semantic, styled HTML list. Add no graph/diagram library.**

The rail is a **navigation spine that happens to show status**, not a free-form node-edge canvas — it has a fixed, linear, shallow hierarchy (Discover → Enrich{Metadata·Fingerprint·Analyze} → Identify{Track-ID·Tracklist} → Propose → Review&Apply{…}). That is a `<nav><ul>` with indented sub-items, count badges, and status dots — pure Tailwind flex/space utilities. The project **already renders a richer node-edge pipeline graph this way**: `src/phaze/templates/pipeline/partials/dag_canvas.html` is a styled-div DAG driven entirely by `$store.pipeline` counts and Tailwind — no SVG/canvas/graph lib involved. The rail is strictly simpler than that existing canvas.

- **Live counts:** each stage's badge binds to `$store.pipeline.<key>` (e.g. `x-text="$store.pipeline.discovered"`), refreshed by the same OOB poll seeds from Q3. Zero new wiring — extend the existing store keys.
- **Clickable stages:** each item is the `hx-get`/`hx-push-url` link from Q1.
- **Visible sub-chains (Tracklist: Search → Scrape → Match):** three inline pills with chevron separators (`Search ✓ → Scrape ✓ → Match ⏳`), each pill's state bound to existing store keys (`scrapeBusy`, `matchBusy`, etc. already exist in the store — see `base.html:106-136`). Pure CSS arrows, no diagramming engine.
- **State chips/dots:** Tailwind background tokens already in the palette (`blue-*`, `phaze-panel`, status colors used across the partials).

A graph library (mermaid, cytoscape, vis-network, d3, reactflow) would add a heavy JS dependency, a build/CDN burden, and an imperative render loop that fights the server-rendered + Alpine-store model — to draw a list that CSS already draws. Reject all of them.

### 5. Accessibility primitives within this stack — native semantics + one Alpine plugin, no framework

All achievable with native HTML semantics, Tailwind variants, and `@alpinejs/focus`. No a11y framework.

| Primitive | How (in this stack) | In-repo anchor |
|-----------|---------------------|----------------|
| **Skip link** | Already implemented: `base.html` `{% block skip_link %}` renders an `sr-only focus:not-sr-only` anchor. Re-point it from `#proposals-table` to the new `#workspace` region. Pattern is correct as-is. | `base.html:173-176`, overridden in `pipeline/dashboard.html:3` |
| **Visible focus states** | Tailwind `focus-visible:` variant + a ring utility (`focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:outline-none`). Apply to rail links, ⌘K input, all interactive controls. `:focus-visible` (not `:focus`) so mouse clicks don't show rings but keyboard nav does. | new — Tailwind 4.3.2 supports the variant natively |
| **ARIA on the DAG rail** | `<nav aria-label="Pipeline stages">` › `<ul>` › items as links with `aria-current="page"` on the active stage. The `aria-current="page"` idiom is **already used** for the Agents nav link. Sub-chains: `<ol aria-label="Tracklist sub-steps">` with `aria-current="step"` on the in-flight step. | `base.html:235-239` (existing `aria-current` usage) |
| **⌘K focus trap + dialog semantics** | `@alpinejs/focus` `x-trap.inert.noscroll="open"` + `role="dialog" aria-modal="true"` + the combobox/listbox ARIA from Q2. | new (the plugin add) |
| **Keyboard nav (rail + ⌘K)** | Alpine `@keydown.window` global listener (⌘K, Escape) — same idiom as `proposals/list.html:19`. Rail is native links (Tab/Enter work for free). | `proposals/list.html:19,56-89` |
| **Live-region announcements** | Existing `aria-live="polite"` on the toast container and SSE progress card; add `aria-live="polite"` to count regions that change under poll if they need announcing. | `base.html:276`, `execution/.../progress.html:29` |

---

## Alternatives Considered

| Recommended | Alternative | When the alternative would win |
|-------------|-------------|--------------------------------|
| HTMX polling (`every 5s`) for rail/workspace status | SSE everywhere (`htmx-ext-sse`, already loaded) | If this became multi-user with sub-second update needs and many concurrent watchers. At one user, polling is simpler and self-healing. SSE stays scoped to the bounded execution-progress stream. |
| ⌘K hand-built in Alpine + `@alpinejs/focus` | `ninja-keys` web component | If you wanted a turnkey palette and were willing to accept a lit dependency + restyle it to the brand. Not worth it for "filter list + hit search endpoint." |
| ⌘K hand-built focus trap via `x-trap` | Roll your own Tab-cycle handler in raw JS | Never — focus trapping is the most-commonly-broken a11y primitive; the official plugin is 3KB and correct. |
| Pure HTML/CSS rail | A lightweight inline-SVG status strip | If the rail ever needed true non-linear edges/branch visualization. The v7.0 pipeline is linear+shallow; CSS list suffices (and `dag_canvas.html` proves even the richer graph renders fine as styled divs). |
| `hx-get` on each rail node | `hx-boost` on the whole rail | `hx-boost` is terser but gives less control over target/swap/OOB coordination. Explicit `hx-get`/`hx-target` is clearer for the rail↔workspace↔OOB-rail-count dance. |
| Bump htmx to 2.0.10 | Adopt htmx **4.0.0** | Only once htmx 4.x is GA, vetted, and the migration is scoped as its own task. v7.0 must not ride a beta. |

## What NOT to Use

| Avoid | Why | Use instead |
|-------|-----|-------------|
| **htmx 4.0.0-beta** (beta2–beta5 are on npm) | Pre-release; API still moving. v7.0 is a large UI rewrite — do not also absorb a major framework migration mid-flight. | htmx **2.0.10** (current stable 2.x). |
| **React / Vue / Svelte / any SPA** | Violates the locked no-SPA / no-build constraint; reintroduces a Node pipeline the project deliberately excludes. | Jinja2 partials + HTMX swaps + Alpine local state. |
| **cmdk, kbar** (command-palette libs) | React-only; require a bundler. | Alpine `x-data` + `@alpinejs/focus` + an HTMX search endpoint (Q2). |
| **ninja-keys** (web-component palette) | Pulls in lit, fights the Tailwind/Jura/`phaze-bg` brand, overkill. | Same Alpine + HTMX palette. |
| **mermaid.js / cytoscape.js / vis-network / d3 / reactflow** (graph libs) | Heavy JS + imperative render loop to draw a shallow linear list that CSS already draws; fights the server-rendered + store-driven model. | Tailwind `<nav><ul>` rail with store-bound count badges (Q4); `dag_canvas.html` is the in-repo precedent. |
| **Tailwind via bare CDN/jsDelivr URL** | jsDelivr per-edge Terser minification yields nondeterministic bytes → SRI mismatch → blocked stylesheet on divergent edges (the bug the repo already hit). Also wrong for an isolated/private LAN. | Keep self-vendoring `@tailwindcss/browser` in `static/vendor/` (re-download 4.3.2, bump filename). |
| **Node / npm / PostCSS / Tailwind CLI / any bundler** | The whole point of the browser Tailwind build + CDN scripts is zero build step. | Runtime browser build (already in place). |
| **`htmx-ext-head-support`** | Manages `<head>` CSS/JS dedup across boosted navigations — not a need here; htmx core already updates `document.title` from a `<title>` in the swap. | htmx core title extraction (Q1). |
| **WebSockets / `htmx-ext-ws`** | Bidirectional persistent socket for a single-user, mostly-read status console is unjustified connection complexity. | 5s HTMX polling (Q3). |
| **Extra Alpine plugins** (`persist`, `collapse`, `intersect`, `mask`) | Not needed — theme persistence already uses raw `localStorage` (`base.html:54-82`); collapse/slide can be CSS + `x-show`/`x-transition`. | Only `@alpinejs/focus`. Keep the plugin surface at one. |
| **A second JS framework for the slide-in record panel** | Alpine `x-show` + `x-transition` + `x-trap` covers a slide-in panel completely. | Alpine + `@alpinejs/focus` (shared with ⌘K). |

## Stack Patterns by Variant

**For a navigation swap that must deep-link + survive back/forward (rail → stage):**
- `hx-get` + `hx-target="#workspace"` + `hx-swap="innerHTML"` + `hx-push-url`.
- Router branches on `HX-Request`: fragment when present, full `base.html` shell (stage pre-selected) when absent.
- Rail-active highlight + breadcrumb derived from the URL (recomputed on `htmx:afterSettle` / `htmx:historyRestore` / `popstate`), NOT from the swap (history restore only re-injects `#workspace`).

**For "one response updates several regions" (poll → counts + cards + store):**
- One polled `hx-get` container with `hx-swap="innerHTML"`; the partial appends `hx-swap-oob="true"` fragments for every out-of-region target, guarded by an `oob_counts`-style flag so OOB fires only on the poll, never the initial include. This is the existing `stats_bar.html` contract — copy it for the rail counts + header status strip.

**For a modal/palette/slide-in that needs accessible focus management:**
- Alpine `x-data` open flag + `@keydown.window` for the hotkey + `x-trap.inert.noscroll="open"` + `role="dialog" aria-modal="true"`. Server-rendered results via a debounced `hx-get`.

## Version Compatibility

| Package | Compatible with | Notes |
|---------|-----------------|-------|
| `htmx.org@2.0.10` | `htmx-ext-sse@2.2.4` | Both on the htmx 2.x line. SSE ext 2.2.x targets htmx 2.x. No change to SSE needed. |
| `@alpinejs/focus@3.15.12` | `alpinejs@3.15.12` | **Plugin version must equal Alpine core version.** Load the plugin `<script defer>` **before** the Alpine core `<script defer>` so it registers prior to `Alpine.start()`. Mismatched majors silently break `x-trap`. |
| `@tailwindcss/browser@4.3.2` | The existing `@theme`/`@custom-variant dark` block in `base.html:140-162` | Tailwind 4.x in-browser; the project's `@custom-variant dark (&:where(.dark, .dark *))` and `@theme` tokens are 4.x syntax already in use. Patch bump 4.3.0→4.3.2 is non-breaking. Re-vendor the exact file (don't hot-link). |
| All CDN `<script>` tags | `integrity` + `crossorigin="anonymous"` | Every version bump invalidates the SRI hash. Recompute with `curl <url> \| openssl dgst -sha384 -binary \| openssl base64 -A` and re-pin. A stale hash blocks the script silently. |

## Sources

- `/bigskysoftware/htmx` (Context7) — confirmed library identity for hx-target/hx-swap/hx-push-url/OOB/history patterns; HIGH.
- npm registry `registry.npmjs.org` (live, 2026-06-29) — current versions: htmx.org **2.0.10** (4.0.0 in beta only), htmx-ext-sse **2.2.4**, htmx-ext-head-support **2.0.5**, alpinejs **3.15.12**, @alpinejs/focus **3.15.12**, @tailwindcss/browser **4.3.2**; HIGH.
- `src/phaze/templates/base.html` — current loaded versions (htmx 2.0.7, sse 2.2.4, Alpine 3.15.9, Tailwind 4.3.0), theme store, SRI/vendoring rationale, brand tokens; HIGH (read directly).
- `src/phaze/templates/pipeline/dashboard.html:50` + `partials/stats_bar.html` + `routers/pipeline.py:549` — the stats-poll + OOB-seed pattern to reuse for rail counts / header strip; HIGH (read directly).
- `src/phaze/templates/pipeline/partials/dag_canvas.html` — in-repo precedent for a styled-div DAG (no graph lib); HIGH.
- `src/phaze/templates/proposals/list.html:19,46-108` — existing Alpine `@keydown.window` keyboard-handler idiom to reuse for ⌘K; HIGH.
- `src/phaze/templates/execution/partials/progress.html:27` — the single existing SSE use (bounded execution progress), establishing SSE's correct narrow scope; HIGH.
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html:387` — prototype's ⌘K keydown + Escape interaction the production palette formalizes; HIGH.

---
*Stack research for: v7.0 DAG-centric hybrid console — frontend patterns within the locked Jinja2 + HTMX + Tailwind + Alpine stack*
*Researched: 2026-06-29*
