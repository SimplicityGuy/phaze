# Phase 62: Polish & cutover - Research

**Researched:** 2026-07-01
**Domain:** Server-rendered UI cutover (FastAPI + Jinja2 + HTMX + Tailwind v4 + Alpine) — a11y hardening, dead-template removal, docs, narrow-width CSS
**Confidence:** HIGH (this is an in-repo inventory phase; nearly every finding is grounded in file:line evidence verified this session)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Target WCAG 2.1 AA; enforce with **pytest structural guards** (rendered-HTML assertions in the style of `test_dead_template_guard.py` / `test_base_html_sri.py`), **no new runtime dep, no browser audit**. Assertions cover at minimum: skip link present + first focusable; DAG rail landmarks + `aria-current`; ⌘K palette combobox/listbox semantics (`role="combobox"`/`"listbox"`/`"option"`, `aria-activedescendant`, `aria-expanded`); record slide-in `role="dialog" aria-modal="true"` + accessible name + `x-trap`; visible focus states. **Rejected:** axe-core/pa11y (CI dep + flake); manual-only.
- **D-02:** "Parity with or better than today" is the floor, WCAG 2.1 AA the ceiling. The four named surfaces (rail keyboard nav, ⌘K, focus states, DAG ARIA + skip link) are non-negotiable.
- **D-03:** Full purge — supersede legacy full-page routes then delete. Convert each non-HX top-level GET to a shell redirect (the `/search` pattern), then delete orphaned page wrappers + partials/JS they alone referenced. Empty `_ALLOWLIST`; guard green. **Rejected:** minimal (delete only the 7 allowlisted).
- **D-04:** `/audit/` and `/admin/agents` are **KEPT**, not superseded (rail plain links; Agents = Phase 61 RECORD-03). `execution/audit_log.html` + `admin/*` templates and everything they transitively reference stay reachable.
- **D-05:** Supersession must be verified before deletion, per legacy page. Expected mapping: `/proposals`→`/s/propose`, `/tracklists`→`/s/tracklist`, `/tags`→`/s/tagwrite`, `/cue`→`/s/cue`, `/duplicates`→`/s/dedupe`, `/preview`→`/s/move`. Surface any supersession gap; do NOT silently drop a capability and do NOT add new capability to close it.
- **D-06:** Refresh `README.md` + `docs/architecture.md` + `docs/project-structure.md` for the DAG-centric IA; **no screenshots**. Correct now-wrong nav steps in `docs/quick-start.md` inline if present, but no full walkthrough rewrite. **Rejected:** README-only; full sweep + screenshots.
- **D-07:** Auto-collapse the 280px rail to an icon-only strip via a **CSS breakpoint — pure CSS, no persistence, no JS toggle**. Below ~`lg` (~1024px). Labels/counts hide; icons + `aria-label`/tooltip remain. Overlays (record slide-in, ⌘K) verified usable at narrow width. **Rejected:** manual toggle + persisted state; off-canvas hamburger.
- **D-08:** Add real per-stage icons (**inline SVG, no new dep**), one glyph per rail node, exposed with `aria-hidden` + the node's existing accessible label. No icon font, no icon library.

### Claude's Discretion
- Exact CSS breakpoint value and collapsed-rail width (D-07).
- The specific inline-SVG glyph per stage (D-08) — match the prototype/design language where one exists.
- Precise redirect status codes + whether legacy HX branches are also removed or left as thin redirects (D-03) — pick the cleanest cut that keeps the guard green and bookmarks working (SHELL-05).
- Which exact pytest assertions/roles constitute the CUT-01 guard set (D-01), so long as the four named surfaces are covered and WCAG 2.1 AA is the target.
- Whether `docs/quick-start.md` needs inline nav corrections (D-06).

### Deferred Ideas (OUT OF SCOPE)
- Touch-input / tablet support (SHELL-06) — CUT-04 is desktop narrow-width only; no phone UI ever.
- Full first-class C3 light theme (RECORD-05) — dark stays primary for v7.0.
- Per-stage configurable confidence thresholds + override UI (REVIEW-06).
- axe-core/pa11y browser a11y audit (rejected for CUT-01).
- Screenshots/GIFs in docs (rejected for CUT-03).
- Manual rail-collapse toggle with persisted state (rejected for CUT-04).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CUT-01 | Baseline WCAG 2.1 AA — keyboard rail + ⌘K, visible focus, skip link, DAG ARIA, at parity-or-better | Palette/record/rail already carry most ARIA (Phase 61 built combobox/listbox/dialog + x-trap). CUT-01 is largely AUDIT + writing pure-filesystem pytest structural guards. Gaps enumerated in "CUT-01 Accessibility" below. |
| CUT-02 | Remove dead old-UI templates/routers/partials; dead-template guard green | **All 8 legacy routes ALREADY redirect (Phase 57 SHELL-05).** The full-page render branches are pure dead code. CUT-02 = delete dead branches + wrapper templates + a small 6-partial cascade + drain `_ALLOWLIST`. Simulated green — see "CUT-02 Cutover Inventory". |
| CUT-03 | Docs/README describe new IA | `README.md`, `docs/architecture.md`, `docs/project-structure.md`. No screenshots (D-06). |
| CUT-04 | Rail collapses to icons at narrow widths (desktop tool) | Tailwind v4.3.2 `max-lg:` variant (verified via Context7). Requires adding inline-SVG per-stage icons (D-08) — no prototype icon set exists (prototype uses status dots). |
</phase_requirements>

## Summary

Phase 62 is the final v7.0 phase and is **presentation-only** — no backend/routing/logic change (REQUIREMENTS.md line 82 still binds). Four independent workstreams: CUT-01 (a11y), CUT-02 (dead-code cutover), CUT-03 (docs), CUT-04 (narrow-width rail). This is an **in-repo inventory phase**: almost nothing needs web research; the value is precise file:line mapping of what to delete/harden.

The single most important finding overturns a pessimistic reading of the CONTEXT: **all eight legacy top-level GET routes already 302-redirect non-HX requests into the shell** — the Phase 57 SHELL-05 work already did the "supersede" half of D-03. `proposals.py:130`, `tracklists.py:89`, `tags.py:167`, `cue.py:186`, `duplicates.py:89`, `preview.py:45`, `pipeline.py:598`, `search.py:39` all redirect. The `return templates.TemplateResponse(... list.html)` line at the *end* of each handler (`proposals.py:167`, `tags.py:226`, etc.) is **unreachable dead code** — non-HX already redirected, HX already returned the filter partial. So CUT-02 is **dead-code deletion, not a routing change**: remove the dead branch + its `.html` string literal, delete the wrapper template, delete the small cascade of partials that only the wrapper referenced, then empty `_ALLOWLIST`.

The dead-template guard is the objective CUT-02 arbiter and its entry set is **any quoted `"…html"` literal in `routers/*.py`** — so a template stays "reachable" as long as its string literal survives in router source, even inside a dead branch. This means CUT-02 must delete *both* the code literal *and* the file. I simulated the full deletion against the guard's exact closure logic: after removing the 8 wrapper literals + deleting the wrappers + 7 allowlisted files + a **6-partial cascade**, the guard is fully green (0 orphans). CUT-01 is mostly done already — Phase 61 gave the ⌘K palette `role="combobox"`/`role="listbox"`/`aria-activedescendant`/`aria-expanded` and the record slide-in `role="dialog" aria-modal` + `x-trap`. CUT-01's real work is (a) writing pure-filesystem structural guards and (b) closing a handful of specific gaps (combobox input has no accessible name; skip-link "first focusable" not asserted; collapsed-rail labels must stay screen-reader-visible).

**Primary recommendation:** Structure the phase as four small, mostly-independent plans (a11y guards, dead-code cutover, docs, narrow-width rail). Do CUT-02 **last within the phase** (dependency-strict per ROADMAP). For CUT-02, delete dead branches + literals + wrapper files + the exact 6-partial cascade below, empty `_ALLOWLIST`, and re-run the two filesystem-only tests (`test_dead_template_guard.py`, `test_base_html_sri.py`) which run without a DB. Keep the legacy HX filter branches as thin dead paths OR remove them too — but if removed, re-run the cascade simulation (removing an HX literal re-orphans its partials).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Accessibility semantics (ARIA/roles) | Frontend templates (Jinja partials) | — | Rendered as static HTML attributes; no backend involvement. |
| A11y enforcement | Test tier (pytest, filesystem) | — | D-01: rendered-HTML structural assertions, no browser/CI dep. |
| Legacy-route supersession | API/router tier (already done) | — | Redirects live in `routers/*.py`; Phase 57 already wired them. |
| Dead-template detection | Test tier (AST guard) | — | `test_dead_template_guard.py` is the objective arbiter. |
| Narrow-width collapse | CSS/build tier (Tailwind `@source` scan) | Frontend template (rail.html classes) | Pure CSS breakpoint; no JS. |
| Per-stage icons | Frontend template (inline SVG) | — | No-dep inline SVG, consistent with wave logo idiom. |
| Docs | Docs tier (markdown) | — | Pure content; no code. |

## Standard Stack

No new packages. This phase adds **zero** runtime and zero dev dependencies. Everything uses the existing stack.

| Tool | Version | Role in this phase | Source |
|------|---------|--------------------|--------|
| jinja2 | 3.1.6 | `meta.find_referenced_templates` powers the dead-template guard; already used | [VERIFIED: tests/test_dead_template_guard.py:40] |
| pytest / pytest-asyncio | existing | CUT-01 structural guards + CUT-02 regression | [VERIFIED: pyproject.toml markers] |
| Tailwind CSS standalone binary | v4.3.2 (NO Node) | CUT-04 `max-lg:` responsive collapse; compiles `assets/src/app.css` → `src/phaze/static/css/app.css` | [VERIFIED: justfile:14, assets/src/app.css:9] |
| httpx AsyncClient | existing | redirect/route tests (require DB fixture) | [VERIFIED: tests/conftest.py:169] |

## Package Legitimacy Audit

**N/A — this phase installs no external packages.** It is a presentation-only cutover: a11y attributes, template deletions, docs, and CSS breakpoint classes. No `uv add`, no npm, no new binary. (The Tailwind standalone binary at v4.3.2 already exists in the repo build chain — not introduced here.)

## CUT-02 Cutover Inventory (highest-value)

### Per-page supersession table (D-03 / D-05 verified)

Every legacy top-level GET **already redirects non-HX to the shell** (Phase 57 SHELL-05). The wrapper template render is dead code after the redirect + HX-partial branches.

| Legacy template | Router:line (dead render) | Redirect already at | Redirect target | Superseding workspace | Supersession verified? |
|-----------------|---------------------------|---------------------|-----------------|----------------------|------------------------|
| `proposals/list.html` | `proposals.py:167` | `proposals.py:130` | `/s/propose` (302) | `pipeline/partials/propose_workspace.html` (`shell.py:99`) | ✅ redirect live + workspace exists |
| `tracklists/list.html` | `tracklists.py:160` | `tracklists.py:89` | `/s/tracklist` (302) | `tracklist_workspace.html` (`shell.py:95`) | ✅ |
| `tags/list.html` | `tags.py:226` | `tags.py:167` | `/s/tagwrite` (302) | `tagwrite_workspace.html` (`shell.py:108`) | ✅ |
| `cue/list.html` | `cue.py:236` | `cue.py:186` | `/s/cue` (302) | `cue_workspace.html` (`shell.py:119`) | ✅ |
| `duplicates/list.html` | `duplicates.py:113` | `duplicates.py:89` | `/s/dedupe` (302) | `dedupe_workspace.html` (`shell.py:114`) | ✅ |
| `preview/tree.html` | `preview.py:57` | `preview.py:45` | `/s/move` (302) | `move_workspace.html` (`shell.py:109`) | ✅ (D-05: `/preview`→Move workspace — confirmed) |
| `pipeline/dashboard.html` | `pipeline.py:601` | `pipeline.py:598` | `/` (302) | shell root (Analyze default) via `analyze_workspace.html` | ✅ (`shell.py:82-85` documents dag_canvas supersede-in-place until CUT-02) |
| `search/page.html` (+5 partials) | *no router literal* (already removed) | `search.py:39` | `/?palette=1` (302) | ⌘K palette (`cmdk_modal.html` + `palette_results.html`) | ✅ — already in `_ALLOWLIST` |

**Reference redirect pattern to replicate (confirmed):** `search.py:39-40`:
```python
if request.headers.get("HX-Request") != "true":
    return RedirectResponse(url="/?palette=1", status_code=302)
```
Every one of the 6 render-in-shell routers uses this exact conditional form already. **No new redirects need to be written** — the discretion in D-03 ("convert each non-HX GET to a shell redirect") is already satisfied; the remaining work is deleting the now-dead render tail.

### Exact deletion worklist (simulated GREEN against the guard)

I ran the guard's exact closure logic (jinja2 `meta.find_referenced_templates`, same entry-set regex) simulating the deletions. Result: **0 orphans** when all of the following are removed together.

**1. Wrapper templates to delete (8):**
- `proposals/list.html`, `tracklists/list.html`, `tags/list.html`, `cue/list.html`, `duplicates/list.html`, `preview/tree.html`, `pipeline/dashboard.html`, `search/page.html`

**2. Router `.html` literals to remove (with their dead branch):**
- `proposals.py:167`, `tracklists.py:160`, `tags.py:226`, `cue.py:236`, `duplicates.py:113`, `preview.py:55-64` (the whole dead render), `pipeline.py:601`.
- For pipeline: make `/pipeline/` a **pure redirect** — the HX branch renders `dashboard.html` and nothing in the shell hx-gets `/pipeline/` (the shell uses `/s/analyze` + `/pipeline/stats`), so drop the HX branch too.
- `search/page.html` has **no remaining literal** in `search.py` (Phase 61 already removed it) — it's file-only, hence its allowlist entry.

**3. Cascade partials that orphan once the wrappers are gone — must ALSO be deleted (6):**
- `_partials/cross_fs_fingerprint_notice.html` (only included by `duplicates/list.html`)
- `pipeline/partials/dag_canvas.html` (only included by `pipeline/dashboard.html`; `shell.py:82-84` explicitly flags it for CUT-02)
- `preview/partials/tree_node.html` (only imported by `preview/tree.html`)
- `tags/partials/pagination.html` (only included by `tags/list.html`)
- `tracklists/partials/filter_tabs.html` (only included by `tracklists/list.html`)
- `tracklists/partials/stats_header.html` (only included by `tracklists/list.html`)

**4. Allowlisted files to delete (the current 7 `_ALLOWLIST` entries):**
- `search/page.html` (also in #1), `search/partials/results_content.html`, `search/partials/results_row.html`, `search/partials/results_table.html`, `search/partials/search_form.html`, `search/partials/summary_counts.html`, `tracklists/partials/toast.html`

**5. Drain `_ALLOWLIST` to `frozenset()`** in `tests/test_dead_template_guard.py:56-77` (keep the closure logic untouched — CONTEXT explicitly forbids relaxing it).

**6. Remove the legacy top tab-bar nav block** in `base.html:161-251` (the `<nav aria-label="Main navigation">` with 8 legacy tab links + Audit/Agents). See "base.html reconciliation" below.

**Partials that STAY (do NOT delete) — reused by shell/record, verified reachable:**
- All HX-branch partials of the kept routers stay reachable via their surviving HX literals: e.g. `proposals/partials/proposal_content.html`, `tags/partials/tag_list.html`, `cue/partials/cue_list.html`, `tracklists/partials/tracklist_list.html`, `duplicates/partials/group_list.html`.
- `proposals/partials/row_detail.html` + `analysis_timeline.html` are reused by the **record view** (`record.py:89` / `record_body.html`). KEEP.
- `pipeline/partials/_diff_row.html`, `stats_bar.html`, all `*_workspace.html`, `_lane_card.html`, cloud cards — the live shell. KEEP.

### base.html reconciliation (D-04 vs ROADMAP "keep all partials/")

`base.html` **survives** — it is still extended by `admin/agents.html` and `execution/audit_log.html` (the two D-04 KEPT pages) [VERIFIED: reachability script]. The ROADMAP note "delete … the base.html nav block" refers to the legacy top tab-bar (`base.html:161-251`), NOT the whole file. **Reconciliation of "keep all partials/":** it means keep partials still referenced by the surviving HX branches / shell / record — NOT literally every file (6 partials genuinely orphan and must go per D-03 "partials they alone referenced"). The ROADMAP list and D-03 agree once you read "keep all partials/" as "keep the still-referenced partials."

**Supersession-gap watch (D-05):** when the tab-bar nav is removed, `/audit/` and `/admin/agents` (accessed via rail plain links `rail.html:147-150`) render through `base.html` and will lose their only in-page navigation back to the shell. The wave-logo home link (`base.html:164`, `href="/"`) survives and covers "return to shell." Recommend the planner keep the logo link + theme toggle and delete only the 8 tab `<a>` links — do not strip the whole `<nav>` chrome or those two pages become dead-ends. This is a presentation nicety, not a capability loss (no scope creep).

### Dead-template guard mechanics (confirmed by reading the test in full)

- **Entry set** (`_entry_templates`, line 80): every quoted `"…html"` literal across `routers/*.py` via regex `["']([^"']+\.html)["']` (line 50). Captures `name=`, positional `_render_partial(...)`, and ternary-assigned template vars. A literal with no on-disk file is harmless (line 106-108 only follows refs for files that exist).
- **Reachable set** (line 99-108): transitive closure of `jinja2.meta.find_referenced_templates` over `extends`/`include`/`import`, dropping dynamic `None` targets.
- **Orphan** = `all_templates - reachable - _ALLOWLIST` (line 110).
- **Definition of done for CUT-02:** `_ALLOWLIST` empty + `test_no_orphan_templates` passes + closure logic unchanged. **Verified:** baseline `test_dead_template_guard.py` is GREEN today (2 passed, filesystem-only, no DB needed); my simulated post-deletion state is also GREEN.

## CUT-01 Accessibility (WCAG 2.1 AA via pytest structural guards)

### Current ARIA state (audited this session)

| Surface | File | Current state | Gap for CUT-01 |
|---------|------|---------------|----------------|
| Skip link | `shell.html:153` | `<a href="#stage-workspace">` first in `<body>`, `sr-only focus:not-sr-only` | Present. Guard should assert it exists + is the first focusable element + targets an existing id. |
| DAG rail | `rail.html:25,35` | `<aside aria-label="Pipeline navigation">` + `<nav aria-label="Pipeline stages">`; every node `aria-current="page"` when active; `focus-visible:ring-2` | Landmarks + aria-current present. When collapsed (CUT-04) labels must stay screen-reader-readable (see D-08 note). |
| ⌘K palette | `cmdk_modal.html:33-77` | `role="dialog" aria-modal="true" aria-label="Command palette"`; input `role="combobox" aria-expanded="true" aria-controls="cmdk-results" :aria-activedescendant`; results `role="listbox"`; rows `role="option"` (in `palette_results.html`); `x-trap.inert.noscroll` | **Gap:** combobox input has NO accessible name (placeholder ≠ name). Add `aria-label` (or `title`). `aria-expanded="true"` is hardcoded — acceptable for an always-open palette but note it. |
| Record slide-in | `record_host.html:60-65` + `record_body.html:34` | `role="dialog" aria-modal="true" aria-label="File record"` (refined to filename via `textContent`); `x-trap.inert.noscroll`; focus→`<h2 tabindex=-1>` | Complete. Guard should assert role+aria-modal+aria-label + presence of `x-trap`. |
| Focus states | rail/header/palette/record | `focus-visible:ring-2 focus-visible:ring-blue-500` throughout | Present. Guard can assert interactive controls carry a `focus`/`focus-visible` class. |
| Theme toggle | `header.html` / `base.html:226` | `aria-label="Toggle theme"` | Present. |

**Key insight:** Phase 61 already built the hard ARIA (combobox/listbox/dialog/x-trap). CUT-01 is ~80% audit + guard-writing, ~20% small fixes (combobox accessible name; verify collapsed-rail labels; assert skip-link-first).

### WAI-ARIA APG conformance notes (verified)

Per the W3C APG Combobox pattern [CITED: w3.org/WAI/ARIA/apg/patterns/combobox]:
- `role="combobox"` element has default `aria-expanded=false`; must reflect popup visibility. The palette hardcodes `"true"` — fine for an always-visible listbox while open.
- When a listbox popup descendant is "focused," DOM focus stays on the combobox and `aria-activedescendant` points at the active option. ✅ matches `cmdk_modal.html` (roving index via `activeId`).
- A listbox popup does **not** require `aria-haspopup` (only non-listbox popups do). ✅ current markup omits it correctly.
- Accessible name: an `<input>` combobox should be labeled (label element or `aria-label`). ⚠️ **the one real gap** — add `aria-label` to the `x-ref="input"`.
- Dialog (`role="dialog" aria-modal="true"`) needs an accessible name [CITED: APG dialog pattern] — both dialogs have `aria-label`. ✅

### Recommended pytest structural guards (D-01) — pure filesystem, mirror `test_dead_template_guard.py`

Write a new `tests/test_a11y_guards.py` that reads template source (no DB, no client). Suggested assertions:
1. `shell.html` contains a skip link whose `href="#stage-workspace"` and appears before any other focusable element in `<body>`; the target id exists in the shell include graph.
2. `rail.html` has `<nav>` + `<aside>` landmarks with non-empty `aria-label`; every rail `<button>` carries the `{% if stage == ... %}aria-current="page"{% endif %}` idiom; every navigable node has a `focus-visible:` class.
3. `cmdk_modal.html`: input has `role="combobox"`, `aria-controls`, `aria-expanded`, `:aria-activedescendant`, **and an accessible name** (`aria-label` or `title`); results container `role="listbox"` with `aria-label`; dialog wrapper `role="dialog" aria-modal="true"` + `aria-label`.
4. `record_host.html`: panel `role="dialog" aria-modal="true"` + `aria-label` + `x-trap`.
5. After CUT-04: rail node labels remain in the DOM (as `sr-only`, not `display:none`) when the icon-only collapse is active — assert the label spans are not removed, only visually hidden.

**Precedent for the style:** `test_base_html_sri.py` reads template text + regex-asserts (`_SCRIPT_TAG`); `test_dead_template_guard.py` parses with jinja2. Follow either idiom. These run in the fast lane (no `integration` marker — they touch no DB fixture, so `conftest.py:134` won't auto-mark them).

## CUT-04 Narrow-width rail collapse

### Tailwind v4 breakpoint approach (verified via Context7)

Tailwind v4.3.2 provides `max-*` variants out of the box [VERIFIED: Context7 /tailwindlabs/tailwindcss.com]:

| Variant | Media query |
|---------|-------------|
| `max-lg` | `@media (width < 64rem)` (< 1024px) |
| `lg` | `@media (width >= 64rem)` |

D-07 says "below ~lg (~1024px)". Use the **`max-lg:` variant** to apply the collapsed styles only below 1024px. Example approach for `rail.html:25`:
- Aside width: `w-[280px] max-lg:w-16` (icon strip; exact collapsed width is Claude's discretion).
- Labels + counts: add `max-lg:sr-only` (NOT `max-lg:hidden` — keep screen-reader access per D-08) or `max-lg:hidden` on visual-only count spans.
- Icons: always visible; add per-stage inline SVG (D-08).
- Group boxes / eyebrows: `max-lg:hidden` or restyle to icon-only.

### Build mechanics (critical)

- `assets/src/app.css` has `@source "../../src/phaze/templates"` (line 15) — Tailwind scans all templates for utility class strings [VERIFIED]. New `max-lg:*` classes in `rail.html` are picked up automatically on rebuild.
- **The compiled CSS is gitignored** (`.gitignore:246 src/phaze/static/css/app.css`) — built at Docker image-build time (Dockerfile css stage) and locally via `just tailwind` (justfile:63). **Do not commit `app.css`.**
- For local verification the planner must run `just tailwind` (downloads the pinned v4.3.2 standalone binary, no Node) to regenerate `app.css`; CI/Docker regenerates on build.
- Structural pytest guards assert the **class strings are present in the HTML**, not the compiled CSS — so tests pass without a build step.

### D-08 per-stage icons — inline SVG idiom (no dep)

**No prototype icon set exists to match.** The prototype rail (`prototype.html:97-135`) uses status *dots* (`✓`, `∿`, colored `bg-emerald-400`/`bg-blue-400`) + text labels — NOT per-stage glyphs [VERIFIED]. So D-08's "match the prototype where one exists" resolves to: no canonical exists → Claude picks glyphs.

Follow the existing inline-SVG idiom (wave logo `header.html:17-21`, theme icons `base.html:237-247`): 24×24 `viewBox="0 0 24 24" fill="none" stroke="currentColor"` heroicons-style stroke paths, `class="w-5 h-5"`, `aria-hidden="true"`. One glyph per navigable node: discover, metadata, fingerprint, analyze, trackid, tracklist, propose, rename, tagwrite, move, dedupe, cue (12), plus audit/agents plain links. The node's existing text label (kept as `sr-only` when collapsed) is the accessible name; the SVG is `aria-hidden`.

**Overlay check (D-07):** the ⌘K palette (`fixed inset-0`, centered, `cmdk_modal.html:33`) and record slide-in (`fixed inset-y-4 right-4 w-[760px] max-w-[94vw]`, `record_host.html:65`) are viewport-anchored overlays independent of the rail width — they remain usable at narrow width. Verify visually but no structural change expected.

## Architecture Patterns

### Cutover flow (data/render path — no change, just deletion)

```
Browser GET /proposals/ (bookmark, non-HX)
        │
        ▼
proposals.py:130  HX-Request != "true"?  ──YES──► 302 RedirectResponse("/s/propose")
        │ (NO = HX filter)                                    │
        ▼                                                     ▼
proposals.py:165 return proposal_content.html          shell.py /s/propose
        │                                              renders shell.html + propose_workspace.html
        ▼                                                     │
proposals.py:167 return proposals/list.html  ◄── UNREACHABLE (delete this + the template)
```

### Pattern: dead-code deletion keeps the guard as the proof

**What:** For each legacy wrapper, delete (a) the dead `return TemplateResponse(...wrapper.html)` line, (b) the wrapper template file, (c) any partial only that wrapper referenced.
**When:** CUT-02, last in the phase (dependency-strict).
**Verify:** `uv run pytest tests/test_dead_template_guard.py -q` (filesystem-only, green) + `uv run pytest tests/test_redirect_resolution.py` (needs DB — see Environment Availability).

### Anti-Patterns to Avoid
- **Deleting a template but leaving its router literal** → guard follows a literal to a missing file (harmless) BUT the intent (remove old-UI code) is unmet; delete both.
- **Deleting a router literal but leaving the file** → guard flags an orphan (RED). Delete both together.
- **Relaxing the guard closure to force green** → explicitly forbidden by CONTEXT. Only `_ALLOWLIST` may change (to empty).
- **`max-lg:hidden` on rail labels** → strips them from the a11y tree when collapsed. Use `max-lg:sr-only` for the label so screen readers keep it (D-08).
- **Committing `src/phaze/static/css/app.css`** → gitignored; source of truth is `assets/src/app.css`.
- **Adding a JS toggle / localStorage for the collapse** → D-07 rejects it; pure CSS only.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Detect orphaned templates | A bespoke grep script | The existing `test_dead_template_guard.py` closure | It's the CONTEXT-designated arbiter; already handles ternary/positional literals + dynamic-include `None`. |
| Focus trap in dialogs | Custom keydown Tab-cycling JS | `@alpinejs/focus` `x-trap.inert.noscroll` (already loaded, `shell.html:41`) | Already the standard here; adds inert + scroll-lock. |
| Responsive collapse | Media-query JS / ResizeObserver | Tailwind `max-lg:` variants | Pure CSS, no JS, matches D-07. |
| Icon set | npm icon library | Inline SVG (heroicons-style paths) | No-dep ethos; matches wave-logo/theme-toggle idiom. |
| A11y verification | axe-core/pa11y browser run | pytest rendered-HTML structural guards | D-01 rejects the browser dep. |

## Common Pitfalls

### Pitfall 1: Assuming CUT-02 needs new redirects
**What goes wrong:** Planning a "convert GET to redirect" task for all 6 routers.
**Why:** CONTEXT D-03 phrasing ("convert each non-HX top-level GET to a shell redirect") reads as undone work.
**Reality:** Phase 57 already added every redirect (`proposals.py:130` etc.). Only the dead render tail remains.
**How to avoid:** The CUT-02 tasks are *deletions*, not conversions. `test_redirect_resolution.py` already asserts all 8 redirects.

### Pitfall 2: The cascade — deleting a wrapper orphans its private partials
**What goes wrong:** Delete `tags/list.html`, guard goes RED on `tags/partials/pagination.html`.
**How to avoid:** Delete the 6 cascade partials in the same commit (list above). Re-run the guard after each router's deletion.

### Pitfall 3: Removing HX branches re-triggers the cascade
**What goes wrong:** If the planner also deletes the legacy HX filter branches (D-03 discretion), more partials orphan (e.g. `proposals/partials/proposal_content.html` and its children).
**How to avoid:** If removing HX branches, re-run the simulation script (`scratchpad/reach.py` pattern) to recompute the full orphan set before deleting. The minimal cut (keep HX branches as thin dead paths) needs only the 6-partial cascade.

### Pitfall 4: base.html is NOT dead
**What goes wrong:** Treating `base.html` as legacy and deleting it.
**Why:** `admin/agents.html` + `execution/audit_log.html` (D-04 KEPT) still extend it.
**How to avoid:** Only remove the tab-bar `<nav>` block (`base.html:161-251` legacy links); keep the file, the logo home link, and the theme toggle so audit/agents pages aren't dead-ends.

### Pitfall 5: SRI test guards BOTH base.html and shell.html
**What goes wrong:** Editing `shell.html` (CUT-01/CUT-04) near the `<script>` tags could drift an SRI hash.
**How to avoid:** `test_base_html_sri.py` covers both templates (`_ALL_TEMPLATES`, line 50). Don't touch the pinned `<script integrity=...>` lines; the network test is `integration`-marked and skips offline.

## Runtime State Inventory

This is a rename/removal-adjacent phase (deleting templates + routes), so the inventory applies:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — no DB keys/collections reference template names or routes. | None. |
| Live service config | None — routes are code; no external service stores `/proposals` etc. | None. |
| OS-registered state | None. | None. |
| Secrets/env vars | None. | None. |
| Build artifacts | `src/phaze/static/css/app.css` (gitignored) is regenerated by `just tailwind` / Docker build after CUT-04 adds `max-lg:` classes. Stale local `app.css` won't reflect the collapse until rebuilt. | Run `just tailwind` locally; Docker regenerates on build. |
| Bookmarks / external refs | Old URLs (`/proposals/` etc.) — preserved by the existing 302 redirects (SHELL-05). Deleting the dead render tail does NOT remove the routes or redirects. | None — redirects stay. |

**Verified:** the 8 legacy *routes* remain registered and redirecting after CUT-02 (only the dead render branch is removed, not the route). `test_redirect_resolution.py::test_legacy_routes_registered` enforces this.

## Testing & Regression Surface

### Tests that will be affected

| Test | Impact | Action |
|------|--------|--------|
| `tests/test_dead_template_guard.py` | `_ALLOWLIST` drained to empty (lines 56-77). | Edit the allowlist to `frozenset()`; keep closure logic. Must stay GREEN. |
| `tests/test_redirect_resolution.py` | Already asserts all 8 redirects + registration + HX-not-redirected. | Should keep passing unchanged (routes/redirects unchanged). Needs DB fixture. |
| `tests/test_base_html_sri.py` | Guards base.html + shell.html scripts. | Don't touch SRI lines; edits to nav block / a11y are safe. |
| `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` | References `cross_fs_fingerprint_notice.html` — a **cascade-deleted** partial. | Update/delete this test when the partial is removed. **Verify what it asserts before deleting.** |
| `tests/test_dag_canvas_render.py` | Renders `dag_canvas.html` — a **cascade-deleted** partial. | Update/delete when dag_canvas is removed. |
| `tests/test_routers/test_{proposals,tags,cue,tracklists,duplicates,preview}.py`, `test_pipeline.py` | May assert the full-page (non-HX) render path or wrapper content. | Audit each: any assertion on `list.html`/`tree.html`/`dashboard.html` content or non-HX 200-with-body must change to expect the 302 (or be removed). Since redirects are already live, most already use HX headers or `follow_redirects`. |
| New: `tests/test_a11y_guards.py` | CUT-01 structural guards. | Create (filesystem-only, no DB). |

### Test infra facts (from STATE.md / conftest)
- `client` fixture (`conftest.py:169`) uses `AsyncClient` + a DB session → **integration** (auto-marked, `conftest.py:134`). Redirect/route tests need the test DB.
- Test-DB env (memory): Postgres `5433`, Redis `6380`; UAT recipe uses fresh `phaze_uat` DB + `PHAZE_AUTO_MIGRATE`; app boot needs `PHAZE_QUEUE_URL=<postgres DSN>` (SAQ queue is Postgres since Phase 36), **not** redis.
- **85% coverage gate** (CLAUDE.md). Deleting dead code branches *raises* coverage (removes unexercised lines); adding a11y guard tests adds covered assertions. Net positive.
- Filesystem-only guards (`test_dead_template_guard.py`, `test_base_html_sri.py` static test, new `test_a11y_guards.py`) run without the DB — the fast verification lane for CUT-01/CUT-02.

## Validation Architecture

`workflow.nyquist_validation: true` — section required.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (existing) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`, markers at line 127) |
| Quick run command | `uv run pytest tests/test_dead_template_guard.py tests/test_a11y_guards.py -q` (filesystem-only, no DB) |
| Full suite command | `uv run pytest` (needs test DB at 5433/6380) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CUT-01 | Skip link/rail/palette/record ARIA present | unit (filesystem) | `uv run pytest tests/test_a11y_guards.py -q` | ❌ Wave 0 |
| CUT-02 | No orphan templates; `_ALLOWLIST` empty | unit (filesystem) | `uv run pytest tests/test_dead_template_guard.py -q` | ✅ (drain allowlist) |
| CUT-02 | Legacy routes still redirect (bookmarks) | integration | `uv run pytest tests/test_redirect_resolution.py` | ✅ |
| CUT-03 | Docs describe new IA | manual review | — (no automated test; markdown content) | N/A |
| CUT-04 | Rail carries `max-lg:` collapse classes + per-stage icons + `sr-only` labels | unit (filesystem) | `uv run pytest tests/test_a11y_guards.py -q` (extend) | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_dead_template_guard.py tests/test_a11y_guards.py -q`
- **Per wave merge:** `uv run pytest -m "not integration"` then full `uv run pytest` with DB up
- **Phase gate:** full suite green + `pre-commit run --all-files` (ruff/mypy/bandit) before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_a11y_guards.py` — covers CUT-01 (+ CUT-04 class assertions). New file, filesystem-only.
- [ ] Audit `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` + `tests/test_dag_canvas_render.py` — will break when their partials are cascade-deleted; plan their update/removal.
- [ ] No framework install needed (pytest already present).

## Security Domain

`security_enforcement` not explicitly disabled → include. This is a presentation-only phase; the surface is small but two real items:

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation / Output Encoding | yes | Jinja2 autoescape (on). In Alpine JS-attribute contexts use `|tojson`, NOT `|e` (see threat below). |
| V14 Config | yes | SRI pins on CDN scripts (`test_base_html_sri.py`) — don't drift when editing shell.html. |
| V2/V3/V4/V6 | no | No auth/session/access-control/crypto change in this phase. |

### Known Threat Patterns
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| XSS via filename in Alpine `x-data` (JS context) | Tampering/Info-disclosure | Use `|tojson` not `|e` — MEMORY records a HIGH XSS fixed in Phase 60 `_diff_row.html` where `|e` broke Alpine on apostrophe filenames ("Guns N' Roses"). New a11y/record edits must not reintroduce `|e` in JS-attribute context. `record_host.html:74` correctly uses `textContent` for the aria-label refinement (XSS-safe). |
| SRI hash drift on CDN scripts | Tampering | `test_base_html_sri.py` guards both base.html + shell.html; leave `integrity=` lines untouched. |
| Non-https URL in SRI | Tampering | `test_base_html_sri.py:144` rejects non-https. |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Legacy tabbed UI (per-page full renders) | DAG-centric shell (`/s/<stage>` HTMX swaps) | Phases 57-61 (v7.0) | CUT-02 removes the now-dead tab-era wrappers. |
| Tailwind in-browser compiler (`@tailwindcss/browser`) | Standalone v4.3.2 binary → `/static/css/app.css` | v7.0 | CUT-04 classes compile via `@source` scan; no CDN, no Node. |
| `@media (max-width: ...)` hand-written | Tailwind `max-lg:` variant | Tailwind 3.2+ / v4 | CUT-04 uses `max-lg:`. |
| Basic focus management | `@alpinejs/focus` `x-trap` | Phase 61 | Dialogs already trap focus; CUT-01 audits it. |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Keeping legacy HX filter branches (as thin dead paths) is acceptable for D-03's "full purge" intent; minimal cascade = 6 partials. | CUT-02 Inventory | If the planner/user wants HX branches removed too, the orphan cascade grows — re-run the simulation. Low risk: the reach script is documented. |
| A2 | `docs/architecture.md`, `docs/project-structure.md`, `docs/quick-start.md` exist and contain UI/IA sections to refresh. | CUT-03 | If a doc is missing, D-06 scope shrinks; verify file presence during planning (not yet read this session). |
| A3 | The prototype has no per-stage icon set to match, so D-08 glyphs are Claude's choice. | CUT-04 / D-08 | Verified prototype uses dots — low risk. |
| A4 | Removing the 8 tab links from base.html nav won't break audit/agents page tests. | base.html reconciliation | Audit `tests/test_routers/test_execution.py` + admin_agents tests for nav-link assertions before editing. |

## Open Questions (RESOLVED)

1. **Remove legacy HX branches, or leave as thin dead paths?** (D-03 discretion)
   - Known: minimal cut (leave HX branches) → 6-partial cascade, guard green, less churn.
   - Unclear: whether "no orphaned dead code" intent wants the HX branches gone too.
   - Recommendation: leave HX branches (they're small, harmless, and their partials are shared with record/shell in some cases); if removed, re-run the reach simulation first.
   - **RESOLVED (2026-07-01) → keep the HX branches.** Confirmed via source that the per-router HX branches are **LIVE** (they serve the shell workspaces' pagination/filter/sort fragments — `proposals.py:164-165` → `proposal_content.html`, and identically tags/cue/tracklists/duplicates), NOT dead. See corrected **CONTEXT.md D-03b**: delete only the dead `return ...list.html` tails + `pipeline.py`'s genuinely-dead dashboard branch; KEEP the live HX branches (stripping them would break live shell functionality — REQUIREMENTS.md line 82). This recommendation was the correct one; the interim "strip them" framing was a mis-scope, now reverted.

2. **Does `docs/quick-start.md` exist and need nav corrections?** (D-06 discretion)
   - Recommendation: planner reads `docs/` at plan time; correct inline only if it has now-wrong nav steps.
   - **RESOLVED → per CONTEXT.md D-06:** `quick-start.md` legacy-nav steps are corrected inline; `architecture.md` + `project-structure.md` get an added UI/IA section (they have none today). Handled by plan 62-03.

3. **base.html nav: strip to logo+theme only, or add a shell "back" affordance for audit/agents?**
   - Recommendation: keep logo home link (`href="/"`) + theme toggle; delete the 8 tab links. No new capability.
   - **RESOLVED → per CONTEXT.md D-04a:** strip to logo home link + theme toggle only (no separate "back" affordance). Handled by plan 62-04.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.14 + uv | all | ✓ | 3.14.5 | — |
| jinja2 | dead-template guard | ✓ | 3.1.6 | — |
| Tailwind standalone binary | CUT-04 local build | ⚠️ downloaded on demand by `just tailwind` (not in repo until run) | v4.3.2 | Docker build regenerates; structural tests don't need it |
| PostgreSQL (test DB) | redirect/route integration tests | ✗ (not running in this session) | 5433 | Filesystem-only guards (dead-template, a11y) run without it; run DB for full suite |
| Redis (test) | some integration tests | ✗ this session | 6380 | Not needed for CUT-* filesystem guards |

**Missing dependencies with no fallback:** none for the core CUT-* work (a11y + cutover guards are filesystem-only).
**Missing dependencies with fallback:** test Postgres/Redis — needed only for the redirect integration tests; bring up the test-DB env (5433/6380) for full-suite verification.

## Sources

### Primary (HIGH confidence)
- Codebase (this session, file:line verified): `tests/test_dead_template_guard.py`, `tests/test_base_html_sri.py`, `tests/test_redirect_resolution.py`, `tests/conftest.py`, `src/phaze/routers/{search,proposals,tracklists,tags,cue,duplicates,preview,pipeline,shell}.py`, `src/phaze/templates/shell/{shell.html,partials/rail.html,partials/cmdk_modal.html,partials/record_host.html,partials/header.html}`, `src/phaze/templates/record/record_body.html`, `src/phaze/templates/base.html`, `assets/src/app.css`, `justfile`, `.gitignore`, `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html`.
- Reachability simulation (`scratchpad/reach.py`, ran against the guard's exact closure) — confirmed 0 orphans post-deletion.
- Context7 `/tailwindlabs/tailwindcss.com` — `max-lg:` variant + breakpoint values (v4).

### Secondary (MEDIUM confidence)
- W3C WAI-ARIA APG Combobox + Dialog patterns [CITED: w3.org/WAI/ARIA/apg/patterns/combobox].

### Tertiary (LOW confidence)
- MEMORY.md (test-DB env ports, prior XSS incident) — operational context, not re-verified this session.

## Metadata

**Confidence breakdown:**
- CUT-02 inventory: HIGH — every mapping verified at file:line; deletion simulated green against the real guard.
- CUT-01 a11y: HIGH — current ARIA state audited in source; APG pattern cited.
- CUT-04: HIGH — Tailwind `max-lg:` verified via Context7; build chain verified.
- CUT-03: MEDIUM — docs files not yet opened (A2); scope confirmed by CONTEXT.

**Research date:** 2026-07-01
**Valid until:** 2026-07-31 (stable in-repo scope; re-verify router line numbers if intervening commits land)

---

*Sources:*
- [W3C APG Combobox Pattern](https://www.w3.org/WAI/ARIA/apg/patterns/combobox/)
- [W3C APG Listbox Pattern](https://www.w3.org/WAI/ARIA/apg/patterns/listbox/)
- [Tailwind CSS Responsive Design](https://tailwindcss.com/docs/responsive-design)
