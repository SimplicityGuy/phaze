# Phase 57: Shell & DAG rail - Pattern Map

**Mapped:** 2026-06-29
**Files analyzed:** 19 (9 new, 10 modified)
**Analogs found:** 18 / 19 (1 net-new with no in-repo analog: the dead-template AST guard)

> Read alongside `57-CONTEXT.md` (D-01..D-05), `57-RESEARCH.md` (verified line numbers + the conditional-redirect contract + exact SRI hashes), and `57-UI-SPEC.md` (layout/spacing/type contracts). This map answers ONE question per file: *what existing code does the executor copy from?*

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/routers/shell.py` **(NEW)** | router | request-response | `src/phaze/routers/search.py` (HX-branch) + `pipeline.py` (prefix-less) | role+flow match |
| `src/phaze/templates/shell/shell.html` **(NEW)** | template (full page) | request-response | `src/phaze/templates/base.html` (head/theme/nav/brand) | role match |
| `src/phaze/templates/shell/_stage_fragment.html` **(NEW)** | template (fragment) | request-response | `search/partials/results_content.html` (any HX partial) | role+flow match |
| `src/phaze/templates/shell/partials/rail.html` **(NEW)** | template (partial) | request-response | `base.html:191-240` nav link loop + `stats_bar.html` x-text store bindings | role match |
| `src/phaze/templates/shell/partials/header.html` **(NEW)** | template (partial) | request-response | `base.html:179-268` (brand + theme toggle) | role match |
| `src/phaze/templates/shell/partials/cmdk_modal.html` **(NEW)** | template (partial) | event-driven (Alpine) | `base.html:243-267` (`x-data` toggle pattern) | partial (no modal in repo) |
| `src/phaze/routers/pipeline.py` **(MOD)** | router | request-response | itself — add `/pipeline`→`/` redirect; factor `dashboard()` ctx | self |
| `src/phaze/routers/proposals.py` **(MOD)** | router | request-response | `proposals.py:157` HX-branch (insert redirect above) | self |
| `src/phaze/routers/tracklists.py` **(MOD)** | router | request-response | `tracklists.py:152` HX-branch | self |
| `src/phaze/routers/tags.py` **(MOD)** | router | request-response | `tags.py:201` HX-branch | self |
| `src/phaze/routers/cue.py` **(MOD)** | router | request-response | `cue.py:228` HX-branch | self |
| `src/phaze/routers/duplicates.py` **(MOD)** | router | request-response | `duplicates.py:105` HX-branch | self |
| `src/phaze/routers/search.py` **(MOD)** | router | request-response | `search.py:74` HX-branch | self |
| `src/phaze/routers/preview.py` **(MOD)** | router | request-response | `preview.py:36` (NO HX-branch — see note) | self |
| `src/phaze/main.py` **(MOD)** | config / wiring | — | `main.py:185-227` include block | self |
| `src/phaze/templates/base.html` **(MOD)** | template | — | self (SRI bumps L28/34/40 + skip-link L175) | self |
| `tests/test_shell_routes.py` **(NEW)** | test | request-response | `tests/conftest.py` `client` fixture (L162) | role match |
| `tests/test_redirect_resolution.py` **(NEW)** | test | request-response | `tests/_route_introspection.py` + `client` fixture | role match |
| `tests/test_dead_template_guard.py` **(NEW)** | test | batch (static AST) | **NONE** (jinja2.meta — see "No Analog Found") | none |
| `static/vendor/tailwindcss-browser-4.3.2.min.js` **(NEW asset)** | build artifact | file-I/O | replaces `tailwindcss-browser-4.3.0.min.js` | self |

---

## Pattern Assignments

### `src/phaze/routers/shell.py` (NEW — controller, request-response)

**Primary analog:** `src/phaze/routers/search.py` (the HX-Request fork) + `src/phaze/routers/pipeline.py` (prefix-less router, lives at root paths).

**Router header + prefix-less convention** — copy from `search.py:14-16`, but DROP the `prefix=` (mirror `preview.py:25` / `pipeline.py` which are prefix-less so they can own `/` and `/s/{stage}`):
```python
# search.py:14-16 (has a prefix; shell.py must NOT)
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/search", tags=["search"])   # shell.py → APIRouter(tags=["shell"])
```

**The fork (the central pattern to mirror, NOT invent)** — `search.py:73-77`:
```python
# HTMX requests get partial content only (results swap target)
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(request=request, name="search/partials/results_content.html", context=context)
return templates.TemplateResponse(request=request, name="search/page.html", context=context)
```
Shell variant: HX-Request → `shell/_stage_fragment.html` (bare, NEVER extends base.html); else → `shell/shell.html` (full chrome). One private `_render_stage(request, stage)` helper owns this fork; `GET /` calls it with `"analyze"`, `GET /s/{stage}` validates `stage in STAGE_PARTIALS` (404 otherwise — V5 input-validation, RESEARCH Security Domain) then calls it.

**Analyze-default context** — do NOT duplicate `dashboard()`'s query logic. The `/` Analyze node embeds the existing pipeline-dashboard content (D-01). `pipeline.py:434-546` (`dashboard()`) builds that context; `pipeline.py:131` (`_build_dag_context`) builds the per-node store seeds. RESEARCH Open Question 2 + 57-CONTEXT discretion: **factor the dashboard context builder** so both `/pipeline/` (until removed in P62) and shell `/` can call it. Reference the `oob_counts` gate discipline at `pipeline.py:598-606` (initial render passes `oob_counts` falsy — Pitfall 5).

---

### `src/phaze/templates/shell/shell.html` (NEW — full-page template)

**Analog:** `src/phaze/templates/base.html` (the entire `<head>` + body shell).

**LIFT VERBATIM (SHELL-04 — byte-for-byte, do not rewrite):**
- Vendored Tailwind `<script>` + comment block — `base.html:21-28` (bump 4.3.0→4.3.2 filename, see base.html MOD below).
- htmx / htmx-sse / Alpine `<script>` tags — `base.html:34/37/40` (bump htmx+Alpine SRI, leave sse untouched).
- No-FOUC `_applyTheme` script + `Alpine.store('theme')` — `base.html:54-82`.
- **`Alpine.store('pipeline')` seed block** — `base.html:106-136`. **CONSUME, NEVER REDEFINE** (anti-pattern in RESEARCH). The rail counts + status strip bind to these existing keys (`agentOnline`, `analyzeActive`, `discovered`, `metadataDone`, `fingerprintDone`, `tracklistDone`, `proposalsDone`).
- `@theme` tokens + `@custom-variant dark` — `base.html:140-162`.
- Body color classes + `.font-jura` / `.htmx-indicator` styles — `base.html:164-169`.

**REPLACE:** the `<nav>` tab-bar (`base.html:178-269`) → `{% include "shell/partials/header.html" %}` + `{% include "shell/partials/rail.html" %}` + `<div id="stage-workspace">{% include stage_partial %}</div>` + right pane + `{% include "shell/partials/cmdk_modal.html" %}`.

**CHANGE the skip-link target** — `base.html:174-176`:
```html
<a href="#proposals-table" class="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:bg-blue-600 focus:text-white focus:px-4 focus:py-2 focus:rounded focus:z-50">Skip to proposals table</a>
```
Becomes `href="#stage-workspace"` text "Skip to workspace" (ROADMAP-locked; 57-UI-SPEC Focus/ARIA Baseline).

**Layout shell frame** (57-UI-SPEC Spacing): root `h-screen overflow-hidden flex flex-col`; rail `w-[280px]`, right pane `w-[350px]`, header `h-14`; each column `overflow-y-auto` independently.

**history re-init handler** (57-RESEARCH "htmx history + Alpine re-init", A1 — confirm event name vs htmx 2.0.10):
```javascript
document.body.addEventListener('htmx:historyRestore', () => {
    const ws = document.getElementById('stage-workspace');
    if (ws && window.Alpine) Alpine.initTree(ws);
    syncRailSelection(location.pathname);   // re-apply aria-current / selected class
});
```

---

### `src/phaze/templates/shell/_stage_fragment.html` (NEW — fragment template)

**Analog:** any existing HX partial, e.g. `search/partials/results_content.html` (returned at `search.py:75`), `proposals/partials/proposal_content.html` (`proposals.py:158`).

**Pattern:** a bare content body, NO `{% extends %}`, NO `<html>`/`<head>`. Per RESEARCH this file is just `{% include stage_partial %}` so rail-swap and direct-nav render byte-identical center content. **Anti-pattern (RESEARCH):** a stage fragment that `{% extends "base.html" %}` corrupts the shell — fragment responses are content-only (also a UI-SPEC a11y contract: no duplicate landmarks/skip-links).

---

### `src/phaze/templates/shell/partials/rail.html` (NEW — partial)

**Analog (structure):** the nav link loop in `base.html:191-240`. **Analog (live counts):** the `x-text` / `x-init` store bindings in `stats_bar.html:47-68`.

**Active-link conditional** — `base.html` per-link active styling (`base.html:235-239` shows the `aria-current="page"` form to follow):
```html
<a href="/admin/agents"
   {% if current_page == 'admin_agents' %}aria-current="page"{% endif %}
   class="... {% if current_page == 'admin_agents' %}text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-950{% else %}...{% endif %}">
```
Rail nodes apply `aria-current="page"` + `bg-blue-500/10` + inset bar to the selected stage (57-UI-SPEC DAG Rail states).

**HTMX wiring (every navigable node)** — NEW but trivial: `hx-get="/s/<stage>"` `hx-target="#stage-workspace"` `hx-swap="innerHTML"` `hx-push-url="true"`. `audit`/`agents` are below-the-line plain links to `/audit/` and `/admin/agents` (NOT `/s/` stages — RESEARCH "Rail model").

**Live-count binding** — bind `x-text` to the EXISTING `$store.pipeline` keys (`base.html:106-136`). Only render a count for nodes whose key exists (57-UI-SPEC live-vs-static table); do NOT add store keys. Order is VERBATIM from `prototype.html` RAIL config (57-UI-SPEC DAG Rail table, lines 189-208).

---

### `src/phaze/templates/shell/partials/header.html` (NEW — partial)

**Analog:** `base.html:179-268`.

**LIFT VERBATIM:** wave logo SVG (`base.html:183-188`, links to `/`) and the theme-toggle button (`base.html:243-267`, the auto/moon/sun `x-show` SVGs + the 3-way `@click` cycle). Add: the ⌘K affordance button (opens the modal), the agent status dots + "Agents" link (`AGENTS · {n}`) bound to `$store.pipeline.agentOnline`, refreshed by the existing `/pipeline/stats` OOB poll — NO new poll loop (D-05 anti-pattern).

---

### `src/phaze/templates/shell/partials/cmdk_modal.html` (NEW — partial, Alpine event-driven)

**Analog (weak — no modal exists in repo):** the inline `x-data` + `@click` + `x-show` toggle idiom at `base.html:243-267`.

**Pattern:** `x-data="{ open:false }"`, `@keydown.window.cmd.k.prevent` / `.ctrl.k`, `@keydown.escape`, backdrop `@click`, `role="dialog" aria-modal="true"`, focus input on open via `$nextTick`, return focus to ⌘K button on close. Skeleton body only — no search wiring (D-04). Visual contract in 57-UI-SPEC "⌘K Command Palette" (`w-[640px]`, `phaze-panel`, copy strings). `@alpinejs/focus` is a Phase 61 dep — use core `Alpine` only.

---

### Legacy router conditional-redirect (MOD — the 6 render-in-shell routes + 2 renames)

**The pattern (D-03 / SHELL-05 / RESEARCH "Legacy route resolution"):** insert ONE conditional redirect as the FIRST statement of each GET handler, ABOVE the existing body. The existing `HX-Request` filter branch is left UNTOUCHED so the in-page filter still works (Pitfall 2).

```python
# Insert at top of handler, before any query work:
if request.headers.get("HX-Request") != "true":
    return RedirectResponse(url="/s/proposals", status_code=302)
# ...existing handler body unchanged (incl. its own `if HX-Request == "true": return <filter partial>`)...
```

**Per-file insertion point + canonical target** (verified line numbers — the HX-branch each sits ABOVE):

| File | GET handler | Existing HX-branch line | Redirect target | Notes |
|------|-------------|------------------------|-----------------|-------|
| `proposals.py` | `list_proposals` (`:114`) | `:157` | `/s/proposals` | prefix `/proposals` |
| `tracklists.py` | `list` (`:78`) | `:152` | `/s/tracklist` | prefix `/tracklists` |
| `tags.py` | GET `/` (`:140`) | `:201` | `/s/tagwrite` | prefix `/tags` |
| `cue.py` | `list_cue` (`:176`) | `:228` | `/s/cue` | prefix `/cue` |
| `duplicates.py` | GET `/` (`:79`) | `:105` | `/s/dedupe` | prefix `/duplicates` |
| `preview.py` | `tree_preview` (`:36`) | **NONE** | `/s/move` | **No HX-branch exists** — handler is full-page only (no in-page filter). An unconditional redirect would be safe, but use the SAME conditional form for consistency (`HX-Request != "true"`). prefix-less, path `/preview/`. |
| `search.py` | `search_page` (`:19`) | `:74` | `/?palette=1` | rename → ⌘K (D-04). A3: shell Alpine reads `palette` query param to auto-open skeleton. |
| `pipeline.py` | `dashboard` (`:434`) | none | `/` | rename → shell root. |

**Import to add** where missing: `from fastapi.responses import RedirectResponse` (RESEARCH "Don't Hand-Roll": use `starlette.responses.RedirectResponse`, not a hand-rolled `Response`).

**Route→rail-node mapping is a planning decision** (RESEARCH A2 / Open Question 1) — confirm against design §6/§7; the table above is the recommended direct mapping (each legacy route → the rail node embedding its existing content).

**≤1-hop caveat (Pitfall 4):** `redirect_slashes=True` (Starlette default, not overridden in `create_app()`). Test the canonical trailing-slash forms (`/proposals/`) — they resolve in 1 hop. The no-slash form (`/proposals`) is a framework-level 2-hop that still terminates.

---

### `src/phaze/main.py` (MOD — wiring)

**Analog:** the include block `main.py:185-227`.
```python
app.include_router(pipeline.router)   # existing, :193
# ADD: shell router owning GET / + GET /s/{stage}
app.include_router(shell.router)
```
Each router declares its own prefix and is included WITHOUT an extra `prefix=` (required by `_route_introspection.iter_effective_routes`). No `redirect_slashes` change needed — it is already on by default.

---

### `src/phaze/templates/base.html` (MOD — SRI bumps + Tailwind swap)

**Self-analog.** Three edits (exact hashes in 57-RESEARCH "Stack version bumps"):
- **L34 htmx** → `https://unpkg.com/htmx.org@2.0.10/dist/htmx.min.js` + `integrity="sha384-H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"`.
- **L40 Alpine** → `https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js` + `integrity="sha384-LUONAH/vnlbGK96OtMBbN0l0Fcsr7dW3BK7NOImE4oHZAZ/IwIEvvpxyajWxvpaD"`.
- **L28 Tailwind** → swap filename to `tailwindcss-browser-4.3.2.min.js`; update the L21-28 comment (4.3.0 → 4.3.2). NO `integrity=` attr (vendored local file).
- **DO NOT touch** L37 `htmx-ext-sse@2.2.4` (not in bump list; the SRI test still validates it).

Validated by the EXISTING `tests/test_base_html_sri.py` (static pin + live-CDN recompute). The skip-link L175 change is covered under shell.html above (base.html's own skip-link is inside the `{% block skip_link %}` that the shell overrides; if base.html keeps being rendered by un-migrated pages, leave its block default until P62 CUT).

---

### `tests/test_shell_routes.py` / `tests/test_redirect_resolution.py` (NEW — tests)

**Fixture analog:** `tests/conftest.py:161-167` — the `client` fixture (already builds the app via `create_app()`, overrides `get_session`, yields an `httpx.AsyncClient` over `ASGITransport`). Reuse it; do NOT spin up a parallel app.
```python
@pytest_asyncio.fixture
async def client(session) -> AsyncGenerator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
```

**Route-introspection analog (redirect test):** `tests/_route_introspection.py` — use `effective_route_paths(app)` / `iter_effective_routes(app)`, NEVER `app.routes` directly (FastAPI 0.138 lazy-includes routers — the helper recurses through `_IncludedRouter` placeholders). Verified helper at `tests/_route_introspection.py:37-56`.

**Test shapes** are spelled out in 57-RESEARCH "Code Examples" (redirect-loop parametrize over the 8 canonical routes; `follow_redirects=False` to count hops, assert `location.split("?")[0] == target`; then `follow_redirects=True` asserts 200) and the Phase Requirements → Test Map (SHELL-01..05 per-test). For the HX-filter-not-redirected case, send `headers={"HX-Request": "true"}` and assert the filter partial returns (NOT a 302).

---

## Shared Patterns

### HX-Request fork (fragment-vs-full)
**Source:** `src/phaze/routers/search.py:73-77` (also `proposals.py:157`, `tracklists.py:152`, `tags.py:201`, `cue.py:228`, `duplicates.py:105`).
**Apply to:** shell.py (`_render_stage`), and the 8 legacy conditional redirects.
```python
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(request=request, name="<partial>", context=context)
return templates.TemplateResponse(request=request, name="<full page>", context=context)
```

### OOB stats fanout (`oob_counts` gate)
**Source:** `pipeline.py:549-619` (`pipeline_stats_partial`, `oob_counts=True` at `:606`) + `pipeline/partials/stats_bar.html:46-104` (the gated `hx-swap-oob` paragraphs).
**Apply to:** rail counts + header status strip — they RIDE this one 5s poll. Initial shell render passes `oob_counts` falsy (Pitfall 5: avoid duplicate-id collision). NO second `setInterval`/poll.
```html
{% if oob_counts %}
<p id="agent-busy-seed" hx-swap-oob="true" x-init="$store.pipeline.agentBusy = {{ agent_busy }}" class="hidden"></p>
{% for key, value in dag.items() %}
<p id="dag-seed-{{ key }}" hx-swap-oob="true" x-init="$store.pipeline.{{ key }} = {{ value }}" class="hidden"></p>
{% endfor %}
{% endif %}
```

### Theme store + no-FOUC + `$store.pipeline`
**Source:** `base.html:54-82` (theme) + `base.html:106-136` (pipeline store).
**Apply to:** shell.html `<head>` — lift VERBATIM (SHELL-04). `$store.pipeline` is CONSUMED, never redefined.

### RedirectResponse
**Source:** standard `starlette.responses.RedirectResponse(url, status_code=302)`.
**Apply to:** all 8 legacy route handlers. Static internal targets only (no open-redirect surface — RESEARCH Security Domain).

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `tests/test_dead_template_guard.py` | test | batch (static AST) | No in-repo use of `jinja2.meta.find_referenced_templates`. RESEARCH "Code Examples" supplies the full implementation: entry set = literal `name="...html"` strings grepped from `src/phaze/routers/*.py`; reachable set = transitive `extends`/`include`/`import` closure; orphan = any `templates/**/*.html` reachable from nobody. **Seed GREEN this phase** (no orphans yet) and keep green through cutover; P62 (CUT-02) removes dead templates and the guard proves nothing dangles. Caveat (A4): only static literal template names resolve — verified true across current routers; add an allowlist if a dynamic `name=` is ever introduced. |
| `cmdk_modal.html` (partial match only) | template/Alpine | event-driven | No focus-trapping modal exists in the codebase. Nearest idiom is the inline `x-data`/`@click`/`x-show` toggle at `base.html:243-267`; the open/close/ESC/focus-return contract is net-new (spec in 57-UI-SPEC). |

---

## Metadata

**Analog search scope:** `src/phaze/routers/` (shell, search, pipeline, proposals, tracklists, tags, cue, duplicates, preview, execution, admin_agents, main), `src/phaze/templates/base.html`, `src/phaze/templates/pipeline/partials/stats_bar.html`, `tests/conftest.py`, `tests/_route_introspection.py`, `tests/test_base_html_sri.py`.
**Files scanned:** ~16 source/test files read directly; line numbers verified against live code 2026-06-29 (matched RESEARCH).
**Pattern extraction date:** 2026-06-29
