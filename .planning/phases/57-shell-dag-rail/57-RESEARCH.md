# Phase 57: Shell & DAG rail - Research

**Researched:** 2026-06-29
**Domain:** Server-rendered FastAPI + Jinja2 + HTMX 2.x + Alpine 3.x UI shell (IA/template rewrite, no backend behavior change)
**Confidence:** HIGH (in-repo facts verified by reading source; stack versions + SRI hashes verified against live CDN)

> **Research mode:** implementation-grounding, NOT exploratory. Per CONTEXT, all patterns are in-repo. This document (1) verifies/corrects the asserted in-repo facts, (2) resolves the central fragment-vs-shell rendering question against the *existing* codebase pattern, (3) supplies the exact bumped SRI hashes, and (4) specifies the two NEW test artifacts. The canonical visual/behavioral target is `prototype.html`, not the web.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01: Embed existing content as fragments.** Each rail node renders the matching legacy template's content as a fragment inside `#stage-workspace`. App stays fully usable through cutover; Phases 58–61 swap each node's fragment one at a time. Each bridged legacy route must return **just its content block** as a fragment on HTMX request, and the full shell on direct/bookmark navigation. The default **Analyze** node embeds the existing pipeline-dashboard content (redesigned lane cards are Phase 58).
- **D-02: Path segment `/s/<stage>`.** `/` = shell with Analyze (bare root, no stage suffix). Other stages: `/s/discover`, `/s/proposals`, `/s/metadata`, etc. Rail clicks `hx-get` the stage fragment and `hx-push-url` the `/s/<stage>` path. One handler owns stage resolution + per-stage validation. This is the redirect target for D-03.
- **D-03: Redirect to the canonical shell URL.** The 6 render-in-shell legacy routes (`/proposals`, `/tracklists`, `/tags`, `/cue`, `/duplicates`, `/preview`) **302-redirect** to their canonical `/s/<stage>`. Two true renames stay: `/pipeline` → `/` and `/search` → ⌘K via `RedirectResponse` on the trailing-slash canonical form (`redirect_slashes=True`). Redirect-loop test asserts every one of the 8 legacy routes lands on a 200 with the matching rail node pre-selected.
- **D-04: ⌘K skeleton modal.** Header shows ⌘K button + keybinding opens an empty/placeholder palette (Alpine-driven). NO search wiring in Phase 57. `/search` → ⌘K rename still applies but lands in the skeleton.
- **D-05: Minimal header status strip — agent status dots + Agents link.** Fed by the single existing `/pipeline/stats` 5s poll fanned out via `hx-swap-oob` behind the `oob_counts` gate (no new poll loop). Lane-capacity detail deferred to Phase 58.

### Claude's Discretion
- Exact rail-node→count mapping; which DAG nodes show a live count vs. static label (drive from existing `/pipeline/stats` payload + existing `$store.pipeline` keys; do not redefine the store).
- Precise fragment-extraction mechanism for bridged legacy routes (`HX-Request` header branch vs. shared `_shell_or_fragment` helper) — pick the lowest-touch option.
- Skeleton-modal visual treatment (must read as C3 / Jura-blue, contents placeholder).

### Deferred Ideas (OUT OF SCOPE)
- Redesigned rich workspaces (Analyze lane cards, Discover/Metadata/Fingerprint views) — **Phase 58**.
- Identify workspaces (Track-ID, Tracklist 3-step) — **Phase 59**.
- Unified Review & Apply diff/approve gate (Dedupe, Cue) — **Phase 60**.
- Functional ⌘K palette, per-file full-record slide-in, Agents page rebuild, empty/first-run — **Phase 61**.
- a11y depth, responsive rail-collapse, density pass, **dead-template/route removal (CUT-02)**, docs/README IA rewrite — **Phase 62**.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SHELL-01 | `/` renders the three-column shell with Analyze selected by default — no `/pipeline` redirect, no secondary-tab landing. | No `/` route exists today (verified) — Phase 57 ADDS it. Analyze = bare root per D-02. Shell embeds the existing pipeline-dashboard content (D-01). See §"New `/` + `/s/<stage>` handlers". |
| SHELL-02 | Persistent left DAG rail lists every stage with live counts; clicking a stage swaps the center workspace via HTMX without full-page nav. | Rail order/IDs verified from `prototype.html` RAIL config (§"Rail model"). Counts ride the existing `$store.pipeline` keys (base.html:106) + `/pipeline/stats` poll — do NOT redefine the store. Swap target `#stage-workspace`. |
| SHELL-03 | Legacy top tab-bar removed; global search → ⌘K header command bar; compute/agent status → header status strip. | base.html nav row (lines 178–269) is the tab-bar to remove. ⌘K skeleton (D-04). Status strip = agent dots + Agents link fed by `/pipeline/stats` OOB (D-05). |
| SHELL-04 | Existing auto/dark/light theme toggle + Jura/blue/wave brand preserved. | Theme machinery (base.html:54–138) + C3 tokens (`@theme`, lines 143–162) + Jura/wave logo lifted verbatim. SHELL-04 = these survive unchanged. |
| SHELL-05 | Old per-tab routes (`/pipeline`, `/proposals`, `/tracklists`, `/tags`, `/cue`, `/duplicates`, `/search`, `/preview`) redirect into the corresponding shell stage. | Hybrid routing per D-02/D-03. **Conditional redirect** recommended (see §"Legacy route resolution") to avoid breaking the existing in-page HTMX filter on the same path. |
</phase_requirements>

## Summary

Phase 57 is an **IA/template rewrite over existing routers** — zero backend behavior change. Three columns: header (wave logo · ⌘K · agent dots · Agents) · left DAG rail (nav spine) · center `#stage-workspace` · right per-file pane. `/` renders the shell with Analyze selected; rail clicks `hx-get /s/<stage>` and swap only `#stage-workspace`; the 8 legacy routes resolve into the shell in ≤1 hop.

The single most important correction to the CONTEXT: **the "fragment-vs-full-page rendering pattern" already exists in the codebase.** Seven routers (`search`, `proposals`, `tracklists`, `tags`, `cue`, `duplicates`, `execution`) already branch on `request.headers.get("HX-Request") == "true"` and return a partial. **But** that branch returns an *in-page filter* partial (e.g. `proposals/partials/proposal_content.html` — the filtered list), not "the whole stage content block bare." Phase 57 introduces a *variant*: render the SAME stage content either bare (rail swap, HTMX) or shell-wrapped (direct/bookmark). This collides with the existing same-path filter unless the redirect is made conditional on the HTMX header. That collision and its resolution are the core architectural finding (§"Legacy route resolution").

Two NEW test artifacts: a **redirect-loop / ≤1-hop test** (must use the existing `_route_introspection.iter_effective_routes` helper because FastAPI 0.138 lazy-includes routers) and the **dead-template AST guard** (`jinja2.meta.find_referenced_templates` over all templates, entry set = literal `name=` strings in routers). An SRI guard test **already exists** (`tests/test_base_html_sri.py`) — bumping htmx/Alpine means updating the inline hashes; the static test enforces full-semver pins and the integration test validates against the live CDN.

**Primary recommendation:** Add a dedicated shell router (`src/phaze/routers/shell.py`) owning `GET /` + `GET /s/{stage}`, rendering a new `shell.html` whose `#stage-workspace` `{% include %}`s the bridged stage's existing content partial. Bridge each legacy GET with a **conditional redirect at the top of the handler**: `if request.headers.get("HX-Request") != "true": return RedirectResponse(canonical_url, 302)` — preserves the in-page filter, satisfies SHELL-05 bookmarks. Bump htmx→2.0.10 / Alpine→3.15.12 / Tailwind→4.3.2 using the exact SRI hashes in §"Stack version bumps".

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `/` shell render + stage resolution | Frontend Server (FastAPI route) | — | New shell router; one handler owns `/s/{stage}` resolution + per-stage validation (D-02). |
| Rail nav swap (`#stage-workspace`) | Browser (HTMX) | Frontend Server (fragment endpoint) | HTMX `hx-get`/`hx-target`/`hx-push-url`; server returns the bare content fragment. |
| Live counts / status dots | Frontend Server (`/pipeline/stats` poll) | Browser (Alpine `$store.pipeline`) | Existing 5s poll + OOB fanout; Alpine store is the single client source of truth. Do NOT add a parallel poll or store. |
| Theme (auto/dark/light) + no-FOUC | Browser (Alpine store + pre-Alpine `<head>` script + localStorage) | — | Lifted verbatim from base.html (SHELL-04). |
| ⌘K skeleton modal open/close + keybind | Browser (Alpine) | — | Client-only affordance; no server search in Phase 57 (D-04). |
| Legacy bookmark resolution | Frontend Server (RedirectResponse) | — | Conditional 302 to canonical `/s/<stage>` / `/` (D-03). |

## Standard Stack

This phase adds **no new Python packages**. It bumps three browser-delivered JS/CSS libs and reuses the entire existing server stack. Verified versions from `uv.lock`:

### Core (existing, reused)
| Library | Installed Version | Purpose | Notes |
|---------|-------------------|---------|-------|
| FastAPI | 0.138.0 | Web framework / routing | `[VERIFIED: uv.lock]`. **0.138 lazy-includes routers** → tests must use `_route_introspection.iter_effective_routes`. |
| Starlette | 1.3.1 | ASGI / `RedirectResponse` / `redirect_slashes` | `[VERIFIED: uv.lock]`. `redirect_slashes` defaults True; `create_app()` does NOT override it (so it is already on). |
| Jinja2 | 3.1.6 | Server-side templating | `[VERIFIED: uv.lock]`. `jinja2.meta.find_referenced_templates` available for the dead-template guard. |
| httpx | 0.28.1 | Test client (`AsyncClient`) | `[VERIFIED: uv.lock]`. |
| pytest / pytest-asyncio | 9.1.1 / 1.4.0 | Test framework (`asyncio_mode = "auto"`) | `[VERIFIED: uv.lock + pyproject.toml]`. |

### Browser libs (bumped this phase)
| Library | Current → Target | Delivery | SRI required? |
|---------|------------------|----------|---------------|
| htmx | 2.0.7 → **2.0.10** | CDN (`unpkg`/`jsdelivr`) | YES — `integrity=` in base.html:34 |
| Alpine.js | 3.15.9 → **3.15.12** | CDN (`jsdelivr`) | YES — `integrity=` in base.html:40 |
| @tailwindcss/browser | 4.3.0 → **4.3.2** | **VENDORED** at `/static/vendor/tailwindcss-browser-4.3.0.min.js` | NO — local file, no `integrity=` attr (base.html:28) |

> `[VERIFIED: live CDN, 2026-06-29]` All three target versions exist and were fetched successfully. Stay on htmx 2.0.x (4.0 is beta — ROADMAP-locked).

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Conditional `HX-Request` redirect in each legacy handler | `jinja2-fragments` library (block-level rendering) | jinja2-fragments would let `/s/<stage>` render just a `{% block %}` of the legacy `list.html` without a separate partial. But it is a NEW dependency, and the repo already has separate content partials per page. **Lowest-touch = reuse existing partials via `{% include %}`; no new dep.** |
| Dedicated `shell.py` router | Adding `/` + `/s/{stage}` to prefix-less `pipeline.py` | Both work (pipeline.py is prefix-less). A dedicated `shell.py` keeps the load-bearing cross-cutting contract code isolated and easy for Phases 58–62 to find. **Recommend dedicated router.** |

**Installation:** No `uv add`. The one new dep for the whole milestone (`@alpinejs/focus@3.15.12`) lands in **Phase 61**, not 57.

## Package Legitimacy Audit

**N/A — this phase installs no external packages.** The three bumped libraries are browser-delivered (htmx/Alpine via CDN with SRI; Tailwind vendored as a static file). No `uv add`, no PyPI/npm install. SRI hash verification (below) is the integrity gate that substitutes for a registry slopcheck here. The one new milestone dependency (`@alpinejs/focus`) is deferred to Phase 61.

## Stack Version Bumps — exact SRI hashes (the silent-failure landmine)

**Why this matters:** When a `<script src>` carries `integrity=`, the browser refuses to execute the file if the SHA-384 of the served bytes does not match. A stale hash after a version bump silently blocks htmx (page non-interactive) or Alpine (theme/store dead) with **no console error that points at SRI** unless you read carefully. `tests/test_base_html_sri.py` already guards this in two ways (static full-semver-pin check + a network check that recomputes SHA-384 against the live CDN).

**Computed live on 2026-06-29** via `curl -fsSL -H "Accept-Encoding: identity" <url> | openssl dgst -sha384 -binary | openssl base64 -A`. The mechanism was proven correct: recomputing the *current* htmx 2.0.7 produced `sha384-ZBXiYtYQ6hJ2Y0ZNoYuI+Nq5MqWBr+chMrS/RkXpNzQCApHEhOt2aY8EJgqwHLkJ`, which exactly matches the inline hash in base.html:34.

| Script | base.html line | New `src` | New `integrity` |
|--------|---------------|-----------|-----------------|
| htmx | 34 | `https://unpkg.com/htmx.org@2.0.10/dist/htmx.min.js` | `sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V` `[VERIFIED: live CDN]` |
| Alpine.js | 40 | `https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js` | `sha384-LUONAH/vnlbGK96OtMBbN0l0Fcsr7dW3BK7NOImE4oHZAZ/IwIEvvpxyajWxvpaD` `[VERIFIED: live CDN]` |
| Tailwind (vendored) | 28 | re-download `@tailwindcss/browser@4.3.2/dist/index.global.min.js` → `static/vendor/tailwindcss-browser-4.3.2.min.js`, bump the filename in the `<script src>` | **No `integrity=` attr** (local file). For reference only, the 4.3.2 SHA-384 is `sha384-shaHAtPgz0ulP7R/YmFe0nZtC8FxdhJPi73vwJQcADVttxvHLJBJt/pjzkLSbIvL`. |

**Critical scoping notes (verified by reading base.html):**
1. **Tailwind is vendored, not CDN.** Line 28 has NO `integrity=` attribute. Bumping it = download the new file into `static/vendor/` and update the filename in the `<script src>`. Delete the stale `tailwindcss-browser-4.3.0.min.js`. Update the in-file comment that references `4.3.0` + `sha384-d5Pc0U2…`.
2. **The htmx-ext-sse script (base.html:37, `htmx-ext-sse@2.2.4`) is NOT in the bump list.** Leave its `src` and `integrity` untouched. The SRI test will still validate it — do not break it.
3. After editing base.html, run `uv run pytest tests/test_base_html_sri.py` (offline: static pin test only; with network: full hash match).

## Architecture Patterns

### System Architecture Diagram

```
Browser address bar / bookmark            HTMX rail click (hx-get + hx-push-url)
        │ (plain GET, no HX header)               │ (HX-Request: true, hx-target=#stage-workspace)
        ▼                                          ▼
  GET / or /s/{stage}  ──────────────► shell router (shell.py)
        │                                  │  resolve stage → validate → build context
        │                                  │  (reuses pipeline.py data builders for Analyze)
        ▼                                  ▼
  full shell.html                    bare content fragment
  (header + rail + #stage-workspace   (the stage's existing content partial,
   {% include stage partial %}        NO extends base.html)
   + theme <head> machinery)                 │
        │                                     │ swapped into #stage-workspace
        ▼                                     ▼
  Browser renders shell ───────────► Alpine.initTree on swapped subtree
        ▲                                     │
        │ back/forward                        │ rail "selected" class re-synced
        │                            htmx:historyRestore handler
        │                                     │
  GET /proposals/ (bookmark, no HX) ─► legacy router: conditional 302 → /s/proposals (≤1 hop)
  GET /proposals/?q=… (HX filter) ───► legacy router: existing HX-Request branch → filter partial (unchanged)

  Parallel, independent:  /pipeline/stats  ──5s poll──► OOB hx-swap-oob fanout
                          (oob_counts gate)             → $store.pipeline (rail counts + status dots)
```

### New `/` + `/s/{stage}` shell handlers (recommended shape)

```python
# Source: pattern derived from existing search.py:74 / proposals.py:157 HX-Request branch
# (verified in repo). New file: src/phaze/routers/shell.py

router = APIRouter(tags=["shell"])  # prefix-less, like pipeline.py

# Single source of stage → (content partial, rail-node id) mapping.
# Drive rail order/ids from prototype.html RAIL config (see §"Rail model").
STAGE_PARTIALS: dict[str, str] = {
    "analyze": "pipeline/partials/dag_canvas.html",   # default; Phase 58 replaces with lane cards
    "discover": ...,  # planner maps each to its existing content partial (D-01 bridge)
    # metadata, fingerprint, trackid, tracklist, proposals, rename, tagwrite, move, dedupe, cue
}

@router.get("/", response_class=HTMLResponse)
async def shell_home(request: Request) -> HTMLResponse:
    return await _render_stage(request, "analyze")   # Analyze default, bare root (SHELL-01)

@router.get("/s/{stage}", response_class=HTMLResponse)
async def shell_stage(request: Request, stage: str) -> HTMLResponse:
    if stage not in STAGE_PARTIALS:        # per-stage validation owned here (D-02)
        raise HTTPException(status_code=404)
    return await _render_stage(request, stage)

async def _render_stage(request: Request, stage: str) -> HTMLResponse:
    context = {"request": request, "stage": stage, "stage_partial": STAGE_PARTIALS[stage], ...}
    if request.headers.get("HX-Request") == "true":
        # bare fragment for the rail swap — NEVER extends base.html (ROADMAP-locked)
        return templates.TemplateResponse(request=request, name="shell/_stage_fragment.html", context=context)
    return templates.TemplateResponse(request=request, name="shell/shell.html", context=context)
```

`shell.html` carries the `<head>` theme machinery + header + rail + `<div id="stage-workspace">{% include stage_partial %}</div>`. `_stage_fragment.html` is just `{% include stage_partial %}` (the same content, no chrome) so the rail swap and direct nav render byte-identical center content.

### Pattern: Legacy route resolution (the core finding — SHELL-05 / D-03)

**The collision:** Every legacy GET (`/proposals/`, `/tags/`, etc.) already has an `if HX-Request == "true": return <filter partial>` branch (verified: search.py:74, proposals.py:157, tracklists.py:152, tags.py:201, cue.py:228, duplicates.py:105). That branch serves the in-page search/filter box, which `hx-get`s the **same path** with query params. An *unconditional* 302 would make a filter keystroke redirect to `/s/<stage>` and swap the whole shell into the inner filter div.

**Recommended resolution — conditional redirect at the top of the handler:**
```python
# Add as the FIRST statement of each of the 6 render-in-shell legacy GET handlers:
if request.headers.get("HX-Request") != "true":
    return RedirectResponse(url="/s/proposals", status_code=302)   # canonical shell URL
# ...existing handler body unchanged (incl. the existing HX-Request filter branch) ...
```
- A browser address-bar navigation / bookmark sends a plain GET with **no `HX-Request` header** → redirects into the shell (SHELL-05 satisfied).
- The in-page filter sends `HX-Request: true` → falls through to the existing filter partial (unchanged, app stays fully usable per D-01).
- The rail itself `hx-get`s `/s/<stage>` directly, never the legacy path — so the legacy route only ever sees (a) bookmark→redirect or (b) HX filter→partial.

**True renames (D-03):** `/pipeline/` → `/` and `/search/` → ⌘K, same conditional pattern. `/search/` cannot "open a modal" by redirect alone (⌘K is client-side); recommend `RedirectResponse("/?palette=1")` and have the shell's Alpine read the `palette` query param on load to open the skeleton modal (D-04). Flag for planner.

**≤1-hop guarantee + `redirect_slashes` interaction:** `redirect_slashes=True` (Starlette default, not overridden in `create_app()`) means `/proposals` (no slash) → 307 → `/proposals/` → (our 302) → `/s/proposals` = **2 hops**. The canonical bookmark form `/proposals/` (trailing slash, the form base.html links use) → 302 → `/s/proposals` = **1 hop**. The redirect-loop test should assert the **canonical trailing-slash forms resolve in ≤1 hop to a 200** with the matching rail node pre-selected, and that no form loops. Note the no-slash 2-hop case is framework-level and still terminates correctly.

### Pattern: htmx history + Alpine re-init (ROADMAP-locked)

```javascript
// Source: htmx hx-push-url docs (Context7 /bigskysoftware/htmx) — htmx snapshots DOM
// into its history cache (sessionStorage 'htmx-history-cache') and restores innerHTML on
// back/forward. Alpine does NOT auto-initialize restored DOM, so re-init the swapped subtree.
document.body.addEventListener('htmx:historyRestore', () => {
    const ws = document.getElementById('stage-workspace');
    if (ws && window.Alpine) Alpine.initTree(ws);     // re-bind Alpine in restored center
    syncRailSelection(location.pathname);             // re-mark the active rail node
});
// Optional hardening: clean Alpine-added attrs before the snapshot so the cache is clean.
document.body.addEventListener('htmx:beforeHistorySave', () => { /* teardown if needed */ });
```
- Rail nodes use `hx-get="/s/<stage>"` `hx-target="#stage-workspace"` `hx-swap="innerHTML"` `hx-push-url="true"`.
- `hx-push-url="true"` pushes the fetched `/s/<stage>` URL; back/forward restores the cached center HTML and fires `htmx:historyRestore`. `[CITED: htmx hx-push-url docs]`
- `[ASSUMED]` the precise event name `htmx:historyRestore` (ROADMAP-locked; Context7 confirmed the history-cache mechanism + `htmx:beforeHistorySave` but did not return the restore event by name in this session). The planner/executor must confirm against htmx 2.0.10 source — if the event differs, the re-init handler must bind the correct one. This is the one event-name claim to verify in code.

### Pattern: status strip + rail counts via the existing OOB poll (D-05)

Reuse the `/pipeline/stats` 5s poll. The status strip (agent dots) and rail counts are seeded from `$store.pipeline` keys (base.html:106) and refreshed by the OOB `hx-swap-oob` paragraphs in `stats_bar.html` behind the `oob_counts` gate (verified: stats_bar.html emits `agent-busy-seed`, `dag-seed-<key>` etc. only when `oob_counts` truthy). **Do not add a second poll loop or a parallel store** — bind the new strip/rail markup to the existing keys (`agentOnline`, `analyzeActive`, `discovered`, etc.; the full key list is in base.html:107–135).

### Recommended new files
```
src/phaze/routers/shell.py                         # GET / + GET /s/{stage}; stage→partial map
src/phaze/templates/shell/shell.html               # full three-column shell (head theme machinery + header + rail + #stage-workspace)
src/phaze/templates/shell/_stage_fragment.html     # {% include stage_partial %} — bare, no base.html
src/phaze/templates/shell/partials/rail.html        # DAG rail nav (prototype RAIL order)
src/phaze/templates/shell/partials/header.html      # logo · ⌘K button · status dots · Agents link
src/phaze/templates/shell/partials/cmdk_modal.html  # D-04 skeleton palette (Alpine)
tests/test_shell_routes.py                          # SHELL-01..05 behavioral (NEW)
tests/test_redirect_resolution.py                   # 8-route ≤1-hop (NEW; uses _route_introspection)
tests/test_dead_template_guard.py                   # AST orphan guard (NEW)
```

### Anti-Patterns to Avoid
- **A stage fragment that `{% extends "base.html" %}`** — ROADMAP-locked: stage responses are fragment-only. A swapped fragment containing `<html>`/`<head>` corrupts the shell.
- **Unconditional 302 on a legacy route** — breaks the in-page HTMX filter (see collision above).
- **A second `setInterval`/poll for the status strip or rail counts** — violates D-05; ride the existing `/pipeline/stats` OOB fanout.
- **Redefining `Alpine.store('pipeline')`** — consume the existing keys; a redefinition resets seeded counts and breaks the button gating other phases depend on.
- **Introspecting `app.routes` directly in tests** — FastAPI 0.138 lazy-includes routers; use `_route_introspection.iter_effective_routes`.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Effective route enumeration in tests | A manual `app.routes` walk | `tests/_route_introspection.iter_effective_routes` / `effective_route_paths` | FastAPI 0.138 inserts lazy `_IncludedRouter` placeholders with no `.path`; the helper already handles recursion. `[VERIFIED: tests/_route_introspection.py]` |
| SRI hash validation | A new ad-hoc script | `tests/test_base_html_sri.py` (exists) | Already does static pin + live-CDN SHA-384 recompute with retry. Just update the inline hashes; the test confirms them. `[VERIFIED: file read]` |
| Template reference graph | Regex over `{% extends %}` | `jinja2.meta.find_referenced_templates(env.parse(src))` | Handles extends/include/import uniformly; jinja2 3.1.6 ships it. `[CITED: jinja2 docs]` |
| Theme / no-FOUC | A new dark-mode toggle | Lift base.html:54–138 verbatim | SHELL-04 requires byte-for-byte preservation; the `_applyTheme` pre-Alpine script is the anti-FOUC mechanism. |
| Redirects | `Response(headers={"Location": ...})` | `starlette.responses.RedirectResponse(url, status_code=302)` | Standard, correct status handling. |

**Key insight:** Almost everything this phase needs already exists in-repo (HX-Request branch, OOB poll, theme store, SRI guard, route introspection). The genuinely new code is the shell router + templates and two test files.

## Common Pitfalls

### Pitfall 1: Stale SRI hash silently disables htmx/Alpine
**What goes wrong:** Bump the version in `src` but forget (or mistype) the `integrity=` → browser blocks the script with only a quiet console warning; the rail stops swapping or the theme dies.
**How to avoid:** Use the exact hashes in §"Stack version bumps". Run `uv run pytest tests/test_base_html_sri.py` (the integration test recomputes against the live CDN).
**Warning signs:** Rail clicks do nothing; theme toggle inert; `Failed to find a valid digest` in console.

### Pitfall 2: Unconditional legacy redirect breaks the in-page filter
**What goes wrong:** 302 the whole legacy GET → search/filter keystrokes (same-path `hx-get`) redirect and swap the shell into the inner div.
**How to avoid:** Conditional redirect — only when `HX-Request` is absent (§"Legacy route resolution").
**Warning signs:** Typing in a legacy page's search box blows away the page / nests a shell.

### Pitfall 3: Alpine not re-initialized after history back/forward
**What goes wrong:** htmx restores cached center HTML on back-nav; Alpine `x-data`/`x-show` in the restored fragment are dead (no `:disabled`, no toggles).
**How to avoid:** `Alpine.initTree(#stage-workspace)` in the `htmx:historyRestore` handler + re-sync rail selection.
**Warning signs:** After pressing Back, buttons/badges in the workspace are frozen.

### Pitfall 4: `redirect_slashes` adds a hop
**What goes wrong:** `/proposals` (no slash) → 307 → `/proposals/` → 302 → `/s/proposals` (2 hops) surprises a "≤1 hop" assertion.
**How to avoid:** Test the canonical trailing-slash forms; document the no-slash 2-hop as framework-level + terminating.

### Pitfall 5: Duplicate DOM ids when OOB seeds render at page load
**What goes wrong:** `hx-swap-oob` paragraphs rendered on the initial full-page include collide with the in-place store anchors (same id). stats_bar.html already gates these behind `oob_counts` (emit only on the poll response) — the new shell's initial render must follow the same gate when it includes any OOB-seeded partial.
**How to avoid:** Initial shell render passes `oob_counts` falsy; only `/pipeline/stats` sets it True. `[VERIFIED: stats_bar.html comment + pipeline.py:606]`

## Runtime State Inventory

> Rename-adjacent (SHELL-05 route renames + base.html nav removal), so this is included.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — no DB rows, collection names, or keys reference UI routes/tab names. Routes are code-only. | None — verified: SHELL-05 changes are FastAPI routes + templates, no persisted route strings. |
| Live service config | None — the UI is served by `phaze-api`; no external service stores these URLs. The reverse proxy fronts the whole app at `/`; sub-paths are not individually proxy-configured. | None — verified: no per-route proxy entries (single app behind one internal-realm proxy). |
| OS-registered state | None — no OS task/cron references UI routes. | None. |
| Secrets/env vars | None — no env var names the renamed routes. | None. |
| Build artifacts | **Vendored Tailwind file** `static/vendor/tailwindcss-browser-4.3.0.min.js` becomes stale after the 4.3.2 bump. The `<script src>` filename + the in-file comment (base.html:21–28) reference `4.3.0`. | Download `4.3.2` build → `static/vendor/tailwindcss-browser-4.3.2.min.js`; update `src` + comment; delete the `4.3.0` file. |

**Bookmark/cache consideration:** External bookmarks to the 8 legacy URLs are exactly what SHELL-05 + D-03 redirects handle — no migration needed beyond the redirects. README/docs that link old routes are a **Phase 62** concern (deferred).

## Code Examples

### Verified existing HX-Request branch (the pattern to mirror, NOT a new invention)
```python
# Source: src/phaze/routers/search.py:74-77 (VERIFIED in repo)
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(request=request, name="search/partials/results_content.html", context=context)
return templates.TemplateResponse(request=request, name="search/page.html", context=context)
```

### Dead-template AST guard (NEW — seed green this phase)
```python
# Source: jinja2.meta.find_referenced_templates (jinja2 3.1.6). Entry set = literal name= strings
# in routers (TemplateResponse(..., name="x.html")); orphan = a .html reachable from no entry.
import ast, re
from pathlib import Path
from jinja2 import Environment

TEMPLATES = Path("src/phaze/templates")
ROUTERS = Path("src/phaze/routers")

def referenced_from(env, tpl_path):                      # extends/include/import targets
    src = tpl_path.read_text()
    return {t for t in __import__("jinja2").meta.find_referenced_templates(env.parse(src)) if t}

def entry_templates():                                   # literal name="...html" in router source
    names = set()
    for py in ROUTERS.glob("*.py"):
        names |= set(re.findall(r'name=["\']([^"\']+\.html)["\']', py.read_text()))
    return names

def test_no_orphan_templates():
    env = Environment()
    all_tpls = {p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html")}
    reachable, frontier = set(), set(entry_templates())
    while frontier:
        cur = frontier.pop(); reachable.add(cur)
        frontier |= (referenced_from(env, TEMPLATES / cur) - reachable)
    orphans = all_tpls - reachable
    assert not orphans, f"Orphaned templates (referenced by nobody): {sorted(orphans)}"
```
**Caveat:** Only static literal template names resolve. phaze's `TemplateResponse`/`{% include %}` calls use string literals (verified across routers and stats_bar.html), so static extraction is sound. If a future dynamic name appears, add it to an explicit allowlist. **This phase must seed the test GREEN** (no orphans yet) and keep it green through cutover; Phase 62 (CUT-02) removes dead templates and the guard proves nothing was left dangling.

### Redirect-loop / ≤1-hop test (NEW)
```python
# Source: httpx AsyncClient + tests/_route_introspection (VERIFIED helper). follow_redirects=False
# to count hops; assert canonical trailing-slash forms reach a 200 in ≤1 hop with the right rail node.
import pytest
from httpx import ASGITransport, AsyncClient

CANONICAL = {  # legacy → expected shell URL (D-03)
    "/proposals/": "/s/proposals", "/tracklists/": "/s/tracklist", "/tags/": "/s/tagwrite",
    "/cue/": "/s/cue", "/duplicates/": "/s/dedupe", "/preview/": "/s/move",
    "/pipeline/": "/", "/search/": "/",   # true renames; /search → /?palette=1 if used
}

@pytest.mark.parametrize("legacy,target", CANONICAL.items())
async def test_legacy_route_redirects_one_hop(app, legacy, target):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(legacy, follow_redirects=False)        # plain GET, no HX header
        assert r.status_code in (302, 307)
        assert r.headers["location"].split("?")[0] == target   # ≤1 hop to canonical
        final = await c.get(legacy, follow_redirects=True)
        assert final.status_code == 200                        # lands on a live shell
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `paths = [r.path for r in app.routes]` | `iter_effective_routes(app)` | FastAPI 0.137 lazy router include | Route-introspecting tests must use the helper (already in repo). |
| Multi-tab top nav (`base.html` nav row) | Three-column shell, DAG rail = nav | Phase 57 | The nav row (base.html:178–269) is removed; `current_page`-based tab highlighting retired. |
| htmx 2.0.7 / Alpine 3.15.9 / Tailwind 4.3.0 | htmx 2.0.10 / Alpine 3.15.12 / Tailwind 4.3.2 | Phase 57 | SRI hashes recomputed (above); stay on htmx 2.0.x. |

**Deprecated/outdated:** The `current_page` context var + per-link active-tab styling in base.html become dead once the tab-bar is removed (their final removal is Phase 62 CUT). The skip-link target `#proposals-table` (base.html:175) must change to `#stage-workspace` (ROADMAP-locked baseline).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The exact htmx event name is `htmx:historyRestore` | Patterns: history + Alpine | If the event differs in 2.0.10, back/forward leaves Alpine un-bound in the restored workspace. ROADMAP-locked the name; confirm against htmx 2.0.10 source/changelog when wiring. |
| A2 | Legacy route→stage mapping: `/preview`→`move`, `/proposals`→`proposals`(Propose), `/tags`→`tagwrite`, `/duplicates`→`dedupe`, `/cue`→`cue`, `/tracklists`→`tracklist` | Legacy route resolution | A mismatched rail node pre-selected on redirect. The rail node ids are verified from prototype; the *route→node* assignment is a planning decision — confirm against design §6/§7. `/proposals` could map to `rename` rather than the `propose` node (Propose vs Review&Apply overlap). |
| A3 | `/search` → ⌘K is implemented as `RedirectResponse("/?palette=1")` + Alpine reading the query param | Legacy route resolution | If a different signal is chosen, the skeleton modal won't auto-open from a `/search` bookmark. D-04 only requires the skeleton; the open-on-redirect mechanism is unspecified. |
| A4 | All `TemplateResponse(name=...)` / `{% include %}` use string literals (dead-template guard can resolve statically) | Code examples | A dynamic name would make the guard under-count reachable templates and false-positive an orphan. Verified across current routers; add an allowlist if a dynamic name is introduced. |

## Open Questions

1. **Route→rail-node mapping for the 6 render-in-shell legacy routes**
   - What we know: rail node ids (prototype): `proposals`(=Propose), `rename/tagwrite/move/dedupe/cue` (Review & Apply), `tracklist`, `discover`, `metadata`, `fingerprint`, `trackid`, `analyze`.
   - What's unclear: design §7 collapses Proposals/Preview/Tags/Cue/Duplicates into one "Review & Apply" gate, but Phase 57 bridges to *existing* per-page content. So which rail node does each legacy route pre-select? (`/proposals`→Propose node or Rename node? `/preview`→Move node?)
   - Recommendation: For Phase 57's bridge, map each legacy route to the rail node whose embedded content is that route's existing page (most direct, keeps the app usable). Lock the mapping in the plan as a single `STAGE_PARTIALS` / route table. Defer the Review&Apply *consolidation* to Phase 60.

2. **Does `/` build the Analyze workspace by calling pipeline.py's existing context builders, or by `{% include %}` of `dag_canvas.html` with a shared context?**
   - What we know: the Analyze default embeds the existing pipeline-dashboard content (D-01); `_build_dag_context` (pipeline.py:131) + the dashboard context (pipeline.py:435) build it today.
   - Recommendation: factor the dashboard's context builder so both `/pipeline/` (until removed) and `/` (shell) can call it; avoid duplicating the query logic. Planner decides extraction granularity.

3. **`@alpinejs/focus` and `Alpine.initTree` availability** — focus is a Phase 61 dep, but `Alpine.initTree` (core API) is needed in Phase 57's history handler. Confirm `Alpine.initTree` exists in 3.15.12 (it is core, present since Alpine 3.x). `[ASSUMED — core API, low risk]`

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `openssl` (sha384 + base64) | Computing/verifying SRI hashes | ✓ | system | The SRI test also recomputes via Python `hashlib` — no openssl needed for the test. |
| `curl` | Downloading the bumped CDN files / vendored Tailwind | ✓ | system | `httpx` (already a dep) can fetch in a script. |
| jsdelivr / unpkg CDN | htmx/Alpine SRI integration test + Tailwind download | ✓ | live | The SRI integration test `skipif(not _has_internet())` — degrades gracefully offline (static pin test still runs). |

**No blocking missing dependencies.** This phase is code/template/static-asset only.

## Validation Architecture

> nyquist_validation = true (config.json) — section included.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths = ["tests"]`, `integration` marker) |
| Quick run command | `uv run pytest tests/test_shell_routes.py tests/test_redirect_resolution.py tests/test_dead_template_guard.py tests/test_base_html_sri.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SHELL-01 | `GET /` → 200, renders shell, Analyze rail node pre-selected, no redirect | integration (ASGI client) | `uv run pytest tests/test_shell_routes.py::test_root_renders_shell_analyze_default` | ❌ Wave 0 |
| SHELL-02 | `GET /s/<stage>` with `HX-Request: true` → bare fragment (no `<html>`); rail lists all stages; counts bound to `$store.pipeline` | integration + render assert | `uv run pytest tests/test_shell_routes.py::test_stage_fragment_is_bare` | ❌ Wave 0 |
| SHELL-02 | Rail markup carries `hx-get=/s/<stage>` `hx-target=#stage-workspace` `hx-push-url=true` for every node | render assert | `uv run pytest tests/test_shell_routes.py::test_rail_nodes_wired` | ❌ Wave 0 |
| SHELL-03 | Legacy `<nav>` tab-bar absent from shell; header has ⌘K button + Agents link + status dots | render assert | `uv run pytest tests/test_shell_routes.py::test_tabbar_removed_header_present` | ❌ Wave 0 |
| SHELL-04 | Theme `<head>` script + `Alpine.store('theme')` + Jura/wave brand present in shell; `$store.pipeline` NOT redefined | render assert | `uv run pytest tests/test_shell_routes.py::test_theme_and_store_preserved` | ❌ Wave 0 |
| SHELL-05 | All 8 legacy canonical (trailing-slash) routes → ≤1-hop redirect → 200 with matching rail node | integration parametrized | `uv run pytest tests/test_redirect_resolution.py` | ❌ Wave 0 |
| SHELL-05 | In-page filter on a legacy route (`HX-Request: true`) still returns its filter partial (NOT a redirect) | integration | `uv run pytest tests/test_redirect_resolution.py::test_hx_filter_not_redirected` | ❌ Wave 0 |
| (cross-cut) | SRI hashes match served CDN bytes for bumped htmx/Alpine; full-semver pins | static + integration | `uv run pytest tests/test_base_html_sri.py` | ✅ exists (update hashes) |
| (cross-cut) | No orphaned Jinja2 templates | static AST | `uv run pytest tests/test_dead_template_guard.py` | ❌ Wave 0 (seed green) |

**Manual-only (UAT, document, do not automate):** no-FOUC on hard reload across dark/light; visual C3 "evolution not reskin" fidelity vs `aesthetic-C3-evolved.html`; back/forward re-binds Alpine in the restored workspace (browser-level); ⌘K keybinding opens the skeleton modal.

### Sampling Rate
- **Per task commit:** the quick run command above (the 4 phase-critical test files).
- **Per wave merge:** `uv run pytest` (full suite — must stay green; ~1750+ tests today).
- **Phase gate:** full suite green + `uv run ruff check . && uv run mypy .` before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_shell_routes.py` — SHELL-01..04 (root render, bare fragment, rail wiring, tab-bar removal, theme/store preserved)
- [ ] `tests/test_redirect_resolution.py` — SHELL-05 8-route ≤1-hop + HX-filter-not-redirected (uses `_route_introspection`)
- [ ] `tests/test_dead_template_guard.py` — orphan-template AST guard (seed green)
- [ ] Update inline SRI hashes in `base.html` (existing `tests/test_base_html_sri.py` then validates)
- [ ] Shared ASGI-app fixture for the new route tests (check `tests/conftest.py` for an existing `app`/`client` fixture to reuse)

## Security Domain

> `security_enforcement` is not set in config.json (treated as enabled). This is a presentation-only IA/template phase with **no auth, session, crypto, or data-handling change** — most ASVS categories are unchanged from prior phases.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | Unchanged — admin UI sits behind the reverse proxy's internal-realm auth (existing). |
| V3 Session Management | no | Unchanged. |
| V4 Access Control | no | Unchanged — private LAN, single-user. |
| V5 Input Validation | **yes** | The new `/s/{stage}` path param must be validated against the known stage set (404 on unknown) — prevents arbitrary template-name reflection. The shell handler owns this (D-02). |
| V6 Cryptography | no (but adjacent) | **SRI is an integrity control, not crypto** — the bumped htmx/Alpine SHA-384 hashes must match (Pitfall 1). The SRI test already enforces https-only URLs (CWE-939). |

### Known Threat Patterns for FastAPI + Jinja2 + HTMX shell
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Unvalidated `{stage}` → template path injection / reflection | Tampering | Whitelist stages in the handler (`stage not in STAGE_PARTIALS → 404`); never interpolate `stage` directly into a template path. |
| Stale/forged SRI hash → blocked or swapped script | Tampering | Exact recomputed hashes (above) + `test_base_html_sri.py` (https-only, live recompute). |
| OOB `hx-swap-oob` count seeds interpolated into `x-init` | Injection (XSS) | Every dag value is a server-computed `int` (verified: pipeline.py coerces to `int`) — safe to interpolate. Keep the new strip/rail counts numeric. |
| Open redirect via legacy-route `Location` | Tampering | Redirect targets are static internal constants (`/s/<stage>`, `/`), never user input — no open-redirect surface. |

## Sources

### Primary (HIGH confidence)
- In-repo source (read directly, 2026-06-29): `src/phaze/main.py`, `src/phaze/templates/base.html`, `src/phaze/routers/pipeline.py` (lines 120–240), `src/phaze/routers/{search,proposals,tracklists,tags,cue,duplicates,preview,execution}.py`, `src/phaze/templates/pipeline/partials/stats_bar.html`, `tests/test_base_html_sri.py`, `tests/_route_introspection.py`, `uv.lock`, `pyproject.toml`, `.planning/config.json`.
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` §5/6/7/11 — IA, rail order, technical approach.
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` — RAIL config (lines 96–145), stage ids, header structure.
- Live CDN fetch + SHA-384 recompute (2026-06-29) — htmx 2.0.10 / Alpine 3.15.12 / Tailwind 4.3.2 hashes; mechanism proven against the existing htmx 2.0.7 inline hash.
- Context7 `/bigskysoftware/htmx` — `hx-push-url`, history cache mechanism, `htmx:beforeHistorySave`.

### Secondary (MEDIUM confidence)
- jinja2 3.1.6 `jinja2.meta.find_referenced_templates` (training + standard API) — for the dead-template guard.

### Tertiary (LOW confidence)
- `htmx:historyRestore` exact event name — ROADMAP-locked, not re-confirmed verbatim from htmx 2.0.10 docs this session (A1). Verify in code.

## Rail model (verified from prototype.html RAIL config, lines 97–116)

Order and ids the rail must render (drives SHELL-02 + the `/s/<stage>` keys):
```
discover                                  (node, count)
Enrich · parallel:  metadata · fingerprint · analyze[lanes: local/A1/k8s]   (analyze = / default)
Identify:           trackid · tracklist
propose                                   (node)
Review & Apply:     rename · tagwrite · move · dedupe · cue   (amber group)
below the line:     audit log · agents    (NOT in the 8-redirect set; rail links direct)
```
`audit` (`/audit/`, execution.py:350) and `agents` (`/admin/agents`, admin_agents.py) keep their own routes in Phase 57 — they are below-the-line rail links, not part of SHELL-05's 8 redirected routes.

## Metadata

**Confidence breakdown:**
- In-repo facts (routes, line numbers, store keys, existing HX-Request pattern, SRI test, route-introspection helper): HIGH — read directly.
- Stack versions + SRI hashes: HIGH — fetched live, mechanism proven.
- Fragment-vs-shell + conditional-redirect recommendation: HIGH — derived from verified existing pattern + the verified filter collision.
- htmx `historyRestore` event name: MEDIUM — ROADMAP-locked, Context7 confirmed mechanism not the exact event token.
- Route→rail-node mapping: MEDIUM — rail ids verified; route assignment is a planning decision (Open Question 1).

**Research date:** 2026-06-29
**Valid until:** 2026-07-29 (stack pins are explicit; in-repo facts valid until the files change).
```
