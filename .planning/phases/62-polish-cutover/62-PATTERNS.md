# Phase 62: Polish & cutover - Pattern Map

**Mapped:** 2026-07-01
**Files analyzed:** 25 (new: 1 test · modified: ~15 templates/routers/tests · deleted: 14 templates · docs: 3-4)
**Analogs found:** 24 / 25 (in-repo; only D-08 icon glyph choice has no in-repo analog — inline-SVG *idiom* does)

> **Stack:** Python 3.14 / uv / FastAPI + Jinja2 + HTMX + Alpine + Tailwind v4.3.2 (self-hosted standalone binary), server-rendered, no SPA build, **no new runtime/dev dep**. Presentation-only phase — no backend/logic change (REQUIREMENTS line 82 binds).
>
> This is an **in-repo cutover** phase: almost every "new file" is a *modification* or *deletion* of an existing file, so the closest analog is frequently the file itself in its current form. Excerpts below are the literal target shapes the executor copies/edits.

---

## File Classification

### CUT-01 — Accessibility (audit + close-gaps + guard)

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `tests/test_a11y_guards.py` (NEW) | test (filesystem structural guard) | transform (read template text → regex/parse assert) | `tests/test_dead_template_guard.py` + `tests/test_base_html_sri.py` | exact (idiom) |
| `src/phaze/templates/shell/partials/cmdk_modal.html` (MOD) | component (Jinja partial) | request-response | itself (add 1 `aria-label`) | exact |
| `src/phaze/templates/shell/partials/rail.html` (MOD) | component | request-response | itself | exact |
| `src/phaze/templates/shell/partials/record_host.html` (MOD/verify) | component | event-driven (slide-in) | itself | exact |
| `src/phaze/templates/shell/shell.html` (verify only) | component (layout) | request-response | itself | exact |

### CUT-02 — Dead-code cutover (pure deletion; guard is the arbiter)

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `tests/test_dead_template_guard.py` (MOD — drain `_ALLOWLIST`) | test (AST guard) | transform | itself | exact |
| `src/phaze/routers/proposals.py` (MOD — delete dead tail) | route | request-response | `src/phaze/routers/search.py` (target shape) | exact |
| `src/phaze/routers/tracklists.py`,`tags.py`,`cue.py`,`duplicates.py`,`preview.py` (MOD) | route | request-response | `search.py` + `proposals.py` current form | exact |
| `src/phaze/routers/pipeline.py` (MOD — make `/pipeline/` pure redirect) | route | request-response | `search.py` | exact |
| `src/phaze/templates/base.html` (MOD — strip tab-bar nav block :161-251) | component (layout) | request-response | itself + `shell/partials/header.html` (reduced-header target) | role-match |
| 8 wrapper templates (DELETE) | component | — | — (deletion) | n/a |
| 6-partial cascade (DELETE) | component | — | — (deletion) | n/a |
| 7 `_ALLOWLIST` files (DELETE) | component | — | — (deletion) | n/a |
| `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` (MOD/DELETE) | test | — | itself | exact |
| `tests/test_dag_canvas_render.py` (MOD/DELETE) | test | — | itself | exact |
| `tests/test_routers/test_{proposals,tags,cue,tracklists,duplicates,preview}.py`, `test_pipeline.py` (AUDIT) | test | request-response | itself | exact |

### CUT-03 — Docs

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `README.md` (MOD) | config/docs | — | itself (§ Architecture Overview :37, § Key Features :166) | exact |
| `docs/architecture.md` (MOD) | docs | — | itself | partial (no UI section yet — ADD one) |
| `docs/project-structure.md` (MOD) | docs | — | itself | partial (no UI/template section yet — ADD one) |
| `docs/quick-start.md` (MOD — inline nav fix, D-06 discretion) | docs | — | itself (:136, :162-164 legacy `/pipeline/`,`/proposals/` steps) | exact |

### CUT-04 — Narrow-width rail

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `src/phaze/templates/shell/partials/rail.html` (MOD — `max-lg:` + 15 inline-SVG glyphs) | component | request-response | itself + `base.html:237-247` theme-icon inline SVG + `header.html:17-21` wave logo | exact (idiom) |
| `assets/src/app.css` (rebuild via `just tailwind`; do NOT commit `src/phaze/static/css/app.css`) | config (build) | batch | itself (`@source` :15) | exact |

---

## Pattern Assignments

### `tests/test_a11y_guards.py` (NEW — test, filesystem structural guard)

**Analog:** `tests/test_dead_template_guard.py` (path constants + filesystem-only idiom) and `tests/test_base_html_sri.py` (regex-over-template-text assertion style). Both run WITHOUT a DB — they touch no `client`/session fixture, so `conftest.py:134` will NOT auto-mark them `integration`. This is the fast verification lane for CUT-01.

**Repo-root + templates-dir constant pattern** (copy from `test_dead_template_guard.py:43-45`):
```python
from __future__ import annotations
from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _REPO_ROOT / "src" / "phaze" / "templates"
```
(`test_base_html_sri.py:44-50` is the alternate single-file form: `_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "phaze" / "templates"`, then name specific files like `_SHELL_HTML = _TEMPLATES_DIR / "shell" / "shell.html"`.)

**Regex-over-text assertion idiom** (copy from `test_base_html_sri.py:51-54, 63-67`):
```python
_SCRIPT_TAG = re.compile(
    r"<script\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*?\bintegrity=[\"']([^\"']+)[\"']",
    re.IGNORECASE | re.DOTALL,
)

def _extract_cdn_scripts(template: Path = _BASE_HTML) -> list[tuple[str, str]]:
    html = template.read_text()
    return _SCRIPT_TAG.findall(html)
```
Mirror this: `html = (_TEMPLATES / "shell" / "partials" / "cmdk_modal.html").read_text()` then `assert 'role="combobox"' in html`, `assert 'aria-label=' in <input block>`, etc.

**Assertion targets (from UI-SPEC "Interaction & Accessibility Contract" + RESEARCH §CUT-01):** each is a plain substring/regex check against the template source below. The exact ARIA strings to assert-present already live in the files (see the four excerpts under CUT-01 below). The only NET-NEW attribute to assert (and add) is the combobox `aria-label`.

**Docstring style:** both analogs open with a multi-paragraph docstring explaining *why* the guard exists (`test_dead_template_guard.py:1-33`, `test_base_html_sri.py:1-30`). Match it — explain that CUT-01 proves WCAG-2.1-AA-relevant ARIA is present without a browser dep (D-01).

---

### `src/phaze/templates/shell/partials/cmdk_modal.html` (MOD — combobox accessible-name gap)

**Analog:** itself. The ONLY real CUT-01 code fix (D-01a). Current input block (`cmdk_modal.html:50-66`) already carries `role="combobox" aria-expanded="true" aria-controls="cmdk-results" :aria-activedescendant="activeId"` and the dialog wrapper (`:33-35`) has `role="dialog" aria-modal="true" aria-label="Command palette"`, results container (`:72-76`) `role="listbox" aria-label="Search and command results"`. **Gap:** the `<input x-ref="input">` has no accessible name (placeholder ≠ name).

**Fix — add one attribute** to the input at `cmdk_modal.html:50`:
```html
<input x-ref="input"
       name="q"
       type="text"
       autocomplete="off"
       aria-label="Search files and commands"   {# NET-NEW — the one CUT-01 fix #}
       placeholder="Search files, artists, tracklists — or type a command…"
       role="combobox"
       aria-expanded="true"
       aria-controls="cmdk-results"
       :aria-activedescendant="activeId"
       ...>
```
Do NOT touch the `hx-get="/search/"` / focus-trap wiring. (`x-trap` lives on the panel `:42`.)

---

### `src/phaze/templates/shell/partials/record_host.html` (verify — dialog complete)

**Analog:** itself (`:60-65`). Already correct; guard asserts, no edit expected:
```html
<div x-ref="panel"
     role="dialog"
     aria-modal="true"
     aria-label="File record"
     x-trap.inert.noscroll.noreturn="open"
     class="absolute inset-y-4 right-4 w-[760px] max-w-[94vw] ...">
```
**XSS note (SECURITY):** the aria-label refinement at `:71-74` uses `textContent` (XSS-safe). Do NOT convert to `|e` in a JS context (MEMORY: HIGH XSS fixed Phase 60 on apostrophe filenames). Overlay is viewport-anchored (`fixed inset-y-4 right-4`) → unaffected by CUT-04 rail collapse.

---

### `src/phaze/templates/shell/shell.html` (verify — skip link baseline)

**Analog:** itself. Skip link (`:153`) is first focusable in `<body>` and targets `#stage-workspace` (`:165`). Guard asserts existence + first-focusable + target-id-present:
```html
<a href="#stage-workspace" class="sr-only focus:not-sr-only focus:absolute ...">Skip to workspace</a>
...
<div id="stage-workspace" data-stage="{{ stage }}" class="h-full overflow-y-auto">{% include stage_partial %}</div>
```
**Deferred-from-61 cleanup (CONTEXT):** delete the dead empty right `<aside aria-label="Detail pane">` at `shell.html:169` (superseded by Phase 61 record slide-in). **SRI pitfall:** `test_base_html_sri.py` guards `shell.html` scripts too — do not drift `<script integrity=…>` lines when editing near the head.

---

### `src/phaze/routers/{proposals,tracklists,tags,cue,duplicates,preview}.py` (MOD — delete dead render tail)

**Reference target shape (D-03):** `src/phaze/routers/search.py:34-77` — a legacy GET that already 302-redirects non-HX and returns ONLY the HX partial (no dead wrapper tail):
```python
async def search_page(request: Request, ...) -> Response:
    """Render the search page, or an HTMX results fragment."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/?palette=1", status_code=302)
    # ... HX-only branch ...
    return templates.TemplateResponse(request=request, name="search/partials/palette_results.html", context=context)
```

**Current dead-tail shape to remove** — `proposals.py:128-167` (representative; the other 5 are structurally identical, redirect line + dead wrapper line pairs in RESEARCH §CUT-02 table):
```python
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/s/propose", status_code=302)   # ← redirect ALREADY exists (Phase 57)
    ...
    if request.headers.get("HX-Request") == "true":                  # ← LIVE branch — serves the shell's pagination/filter/sort fragment (KEEP)
        return templates.TemplateResponse(request=request, name="proposals/partials/proposal_content.html", context=context)
    return templates.TemplateResponse(request=request, name="proposals/list.html", context=context)  # ← UNREACHABLE dead code — DELETE this line + the wrapper file
```
**D-03b (CORRECTED — the cut for the 5 content routers):** delete ONLY the final `...list.html` return line (+ the wrapper file). **KEEP the `if HX-Request == "true": return ..._content.html` branch fully intact** — it is LIVE (the shell workspaces hx-get these routes for pagination/filter/sort; verified `proposals.py:164-165` + RESEARCH "Partials that STAY"). Deleting the `"proposals/list.html"` string literal is what lets the guard drop the wrapper from its entry set. Removing the live HX branch would break shell functionality (REQUIREMENTS.md line 82) — that earlier "aggressive cut" framing was a mis-scope and is REJECTED.

**Reach recompute (guard, expects zero extra orphans):** after the deletions, re-run the reach computation (`scratchpad/reach.py` pattern, RESEARCH Pitfall 3) as a SAFETY check — for these 5 routers the private partials stay live, so the expected orphan set is exactly the D-03a 6-partial cascade (+ the 6 remaining allowlisted `search/*` partials), no more.

**pipeline.py special case:** make `/pipeline/` a PURE redirect — drop the HX branch too (nothing in the shell hx-gets `/pipeline/`; shell uses `/s/analyze` + `/pipeline/stats`). Delete `pipeline.py:601` (`dashboard.html`) + the HX branch. Exact redirect lines per router: `proposals.py:130`, `tracklists.py:89`, `tags.py:167`, `cue.py:186`, `duplicates.py:89`, `preview.py:45`, `pipeline.py:598` (all already present). Dead-tail literals to delete: `proposals.py:167`, `tracklists.py:160`, `tags.py:226`, `cue.py:236`, `duplicates.py:113`, `preview.py:55-64`, `pipeline.py:601`.

---

### `tests/test_dead_template_guard.py` (MOD — drain `_ALLOWLIST` only)

**Analog:** itself. The CUT-02 arbiter. Edit ONLY `_ALLOWLIST` (`:56-77`) → `frozenset()`. **Do NOT relax the closure logic** (`:80-111`) — CONTEXT explicitly forbids it. Definition of done = allowlist empty + `test_no_orphan_templates` green + closure untouched.
```python
_ALLOWLIST: frozenset[str] = frozenset()  # CUT-02 (Phase 62): drained — all legacy wrappers deleted.
```
Guard mechanics recap (for the executor): entry set = every `"…html"` literal in `routers/*.py` (`_HTML_LITERAL` regex `:50`); reachable = transitive `jinja2.meta.find_referenced_templates` closure (`:99-108`); orphan = `all_templates - reachable - _ALLOWLIST` (`:110`). A template stays "reachable" while its string literal survives in ANY router branch — so you MUST delete both the literal AND the file.

**Files to DELETE (RESEARCH §"Exact deletion worklist", simulated GREEN):**
- 8 wrappers: `proposals/list.html`, `tracklists/list.html`, `tags/list.html`, `cue/list.html`, `duplicates/list.html`, `preview/tree.html`, `pipeline/dashboard.html`, `search/page.html`
- 6 cascade partials: `_partials/cross_fs_fingerprint_notice.html`, `pipeline/partials/dag_canvas.html`, `preview/partials/tree_node.html`, `tags/partials/pagination.html`, `tracklists/partials/filter_tabs.html`, `tracklists/partials/stats_header.html`
- 6 remaining allowlisted search partials: `search/partials/{results_content,results_row,results_table,search_form,summary_counts}.html`, `tracklists/partials/toast.html`
- **KEEP** all other `partials/` (live shell/record fragments — RESEARCH §"Partials that STAY").

**Companion test cleanup:** `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` + `tests/test_dag_canvas_render.py` render cascade-deleted partials — update/delete in the same commit (verify what each asserts first). Audit `tests/test_routers/test_{proposals,tags,cue,tracklists,duplicates,preview}.py` + `test_pipeline.py` for any assertion on non-HX 200-with-`list.html`-body → change to expect the 302.

---

### `src/phaze/templates/base.html` (MOD — strip tab-bar nav, keep logo + theme)

**Analog:** itself (current header `:155-251`) + `shell/partials/header.html` (the reduced-header target: logo home link + affordance + theme toggle only). `base.html` SURVIVES — `admin/agents.html` + `execution/audit_log.html` still `{% extends base.html %}` (D-04 KEPT pages).

**KEEP** the wave-logo home link (`base.html:164-171`, `<a href="/" aria-label="Phaze home">`) and the theme toggle (`:224-249`, `aria-label="Toggle theme"`). **DELETE** the 8 legacy tab `<a>` links (`:173-222`, the `<div class="flex gap-1 flex-wrap">` block with Search/Pipeline/Proposals/Preview/Duplicates/Tracklists/Tags/CUE + Audit/Agents). Do not strip the whole `<nav>` chrome — audit/agents pages would become dead-ends (their only "back to shell" is the logo link).

**Skip-link follow-through (UI-SPEC :195):** `base.html:157` skip link targets `#proposals-table` (legacy). If the KEPT pages no longer render that id, correct the target to a valid id (or the page `<main>`). Supersession-hygiene, not a rebuild.

---

### `src/phaze/templates/shell/partials/rail.html` (MOD — CUT-04 collapse + CUT-01 DAG ARIA)

**Analog:** itself. Landmarks already present (`:25` `<aside aria-label="Pipeline navigation">`, `:35` `<nav aria-label="Pipeline stages">`), `aria-current="page"` idiom on every node (`:41` etc.), `focus-visible:ring-2 focus-visible:ring-blue-500` throughout. CUT-01 guard asserts these; no ARIA rebuild.

**Current node shape** (`:38-46`, top-level Discover — the pattern all 12 navigable nodes + 2 below-line links follow):
```html
<button type="button"
        data-rail-stage="discover"
        hx-get="/s/discover" hx-target="#stage-workspace" hx-swap="innerHTML" hx-push-url="true"
        {% if stage == 'discover' %}aria-current="page"{% endif %}
        class="rail-node w-[calc(100%-1rem)] mx-2 rounded-lg px-3 py-2.5 flex items-center gap-3 text-left transition-colors hover:bg-gray-100 dark:hover:bg-white/5 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 aria-[current=page]:bg-blue-500/10 aria-[current=page]:shadow-[inset_3px_0_0_var(--color-blue-500)]">
    <span class="w-5 h-5 rounded-full bg-emerald-500/15 text-emerald-400 grid place-items-center text-[10px] shrink-0">✓</span>
    <span class="flex-1 text-sm text-gray-700 dark:text-gray-200">Discover</span>
    <span class="font-mono text-xs text-gray-500" x-text="$store.pipeline.discovered">0</span>
</button>
```

**CUT-04 transform (class additions only, UI-SPEC §"Collapsed transform rules"):**
- Aside `:25`: `w-[280px]` → add `max-lg:w-16`.
- Label span: add `max-lg:sr-only` (HARD contract — NOT `max-lg:hidden`; keeps it in a11y tree, enforced by CUT-01 guard).
- Count span (`x-text`): add `max-lg:hidden` (visual-only data).
- Group boxes / eyebrows (`:49-50`, `:87-88`, `:124-125`): add `max-lg:hidden` or restyle to thin divider.
- Node inner layout: add `max-lg:justify-center max-lg:px-0` (~40px hit target; keep `py-2.5`).
- Add native `title="{label}"` per node (tooltip; identical text to the label var — single source of truth).

**D-08 inline-SVG icon idiom — copy the shape from `base.html:237-247` (theme icons) / `header.html:17-21` (wave logo):**
```html
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
     stroke-linecap="round" stroke-linejoin="round"
     class="w-5 h-5 shrink-0" aria-hidden="true"> <path d="…"/> </svg>
```
`aria-hidden="true"` on every glyph (the `sr-only` label is the accessible name). One glyph per node, heroicons v2 outline paths inlined verbatim (MIT, no dep). Glyph→node map in UI-SPEC §"Glyph assignment" table (15 rows: 12 stages + Scan CTA + audit + agents). Glyph replaces/sits where the status dot is; must be present in BOTH expanded and collapsed states (it is the only visual when collapsed).

**Build:** `max-lg:*` + new SVG classes are picked up by `@source "../../src/phaze/templates"` (`assets/src/app.css:15`). Rebuild locally with `just tailwind` (`justfile:63-73`, pinned v4.3.2 standalone binary, no Node). **Do NOT commit** `src/phaze/static/css/app.css` (gitignored; Docker rebuilds on image build). A11y guard asserts CLASS STRINGS in HTML source, not compiled CSS → tests pass without a build.

---

### Docs (CUT-03)

**`README.md`** — refresh § "Architecture Overview" (`:37`) + § "Key Features" (`:166`) to describe the DAG-centric shell (three-column layout, rail-as-nav, `/s/<stage>` HTMX stages, ⌘K palette, header status strip, per-file record slide-in, Agents page). No screenshots (D-06). Keep badges one-line (MEMORY: README badge style).

**`docs/architecture.md`** — has no dedicated UI/frontend section today (only "Per-agent task routing" :274). ADD a UI/IA section describing the shell; the analog for section style is the existing pipeline/queue sections in the same file.

**`docs/project-structure.md`** — no template/router section today. ADD one mapping `templates/shell/` (shell.html, rail, cmdk_modal, record_host) + `/s/<stage>` router → workspace-partial relationship.

**`docs/quick-start.md`** (D-06 discretion) — has now-wrong legacy nav steps: `:136` "Visit …/pipeline/", `:162-164` "Review proposals … Visit …/proposals/". Correct inline to the shell (`/` → rail navigation), but no full walkthrough rewrite.

---

## Shared Patterns

### Filesystem-only pytest guard (no DB, fast lane)
**Source:** `tests/test_dead_template_guard.py:43-45` (path consts), `tests/test_base_html_sri.py:44-54,63-67` (regex-over-text).
**Apply to:** the new `tests/test_a11y_guards.py`. These touch no `client`/session fixture, so `conftest.py:134` won't auto-mark them `integration` — they run in the fast lane (`uv run pytest -m "not integration"`). Coverage: deleting dead branches raises coverage; new guard adds covered assertions (net positive vs 85% gate).

### Legacy-GET redirect-then-fragment shape
**Source:** `src/phaze/routers/search.py:39-40` (the canonical `if HX-Request != "true": return RedirectResponse(..., 302)`), already replicated in all 6 target routers.
**Apply to:** every CUT-02 router edit — the redirect STAYS, only the dead wrapper tail is deleted.

### Inline-SVG icon idiom (no dep)
**Source:** `base.html:237-247` (theme icons), `header.html:17-21` (wave logo).
**Apply to:** all 15 rail glyphs (`24×24 viewBox`, `stroke="currentColor"`, `class="w-5 h-5 shrink-0"`, `aria-hidden="true"`).

### Focus-visible ring standard
**Source:** `rail.html` throughout (`focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500`), offset form on `+ Scan` (`rail.html:32`, `focus-visible:ring-offset-1 dark:focus-visible:ring-offset-phaze-panel`).
**Apply to:** every interactive control incl. new collapsed icon buttons; guard asserts a `focus-visible:` class is present.

### Tailwind `@source` auto-scan + gitignored compiled CSS
**Source:** `assets/src/app.css:15` (`@source`), `justfile:63-73` (`just tailwind`), `.gitignore` (`src/phaze/static/css/app.css`).
**Apply to:** CUT-04 — never commit compiled CSS; rebuild locally, Docker rebuilds on image build.

### Alpine JS-context escaping (XSS)
**Source:** `record_host.html:71-74` (`textContent`, XSS-safe).
**Apply to:** any a11y/record edit in a JS-attribute context — use `|tojson`, NEVER `|e` (MEMORY: HIGH XSS Phase 60).

### SRI-hash non-drift
**Source:** `tests/test_base_html_sri.py:50` (`_ALL_TEMPLATES` guards BOTH `base.html` + `shell.html`).
**Apply to:** all edits near `<script integrity=…>` in base.html/shell.html — leave pinned integrity lines untouched.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| D-08 per-stage glyph *choices* | component (inline SVG) | — | Prototype uses status dots (`✓`/`∿`/colored dots), NOT per-stage glyphs (RESEARCH A3 VERIFIED). No in-repo icon set to match → copy heroicons v2 outline paths verbatim. The inline-SVG *idiom* has an analog (`base.html`/`header.html`); the glyph *selection* does not. |

(Docs `architecture.md`/`project-structure.md` UI sections are "partial" not "no-analog" — the file exists and its section-writing style is the analog; the specific UI section is net-new content.)

---

## Metadata

**Analog search scope:** `tests/`, `src/phaze/routers/`, `src/phaze/templates/{shell,base.html}`, `assets/src/app.css`, `justfile`, `docs/`, `README.md`.
**Files scanned:** ~14 read in full/targeted + grep across routers/templates/docs.
**Pattern extraction date:** 2026-07-01
**Note:** router line numbers verified against RESEARCH §CUT-02 (valid until 2026-07-31; re-verify if intervening commits land).
