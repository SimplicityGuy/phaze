# Phase 57: Shell & DAG rail - Context

**Gathered:** 2026-06-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Build the **load-bearing three-column "Hybrid Console" shell** that becomes phaze's home and navigation spine: header (wave logo · ⌘K affordance · agent status strip · Agents link) · left **DAG rail** (pipeline stages, live counts, HTMX nav) · center `#stage-workspace` · right per-file pane. `/` renders the shell with **Analyze selected by default** (no `/pipeline` redirect, no landing on a secondary tab). The legacy top tab-bar is removed; old per-tab routes resolve into the shell in ≤1 hop. Brand (Jura/blue/wave) and the auto/dark/light theme survive verbatim.

This is the **load-bearing foundation** for v7.0 (Phases 57→62 are dependency-strict). It locks the cross-cutting contracts that 58–62 all build on: the stable swap-target id, fragment-only stage responses, the OOB stats fanout, `$store.pipeline` survival, history re-init, focus/ARIA, and theme preservation.

**Explicitly NOT this phase (lands in 58–62):** the redesigned rich stage workspaces — Analyze lane cards (local/A1/k8s), the unified Review & Apply diff/approve gate, the per-file full-record slide-in, the functional ⌘K palette, the Agents page rebuild, the a11y/responsive polish, and CUT-02 dead-template removal. Phase 57 ships the shell as a **bridge** over existing content (see D-01).
</domain>

<decisions>
## Implementation Decisions

### Stage-workspace bridge strategy
- **D-01: Embed existing content as fragments.** Each rail node renders the matching legacy template's content as a fragment inside `#stage-workspace`. The app stays **fully usable through the entire cutover** — Phases 58–61 then swap each node's fragment for its redesigned workspace one at a time. This is the concrete meaning of the ROADMAP's "old tab routes render into the shell" + "dead-template guard watched green through cutover." Implication for the planner: each bridged legacy route must be able to return **just its content block** as a fragment when requested via HTMX (no `extends base.html`), and the full shell on direct/bookmark navigation. The default **Analyze** node in Phase 57 likewise embeds the existing pipeline-dashboard content (the redesigned lane cards are Phase 58).

### Canonical stage-selection URL scheme
- **D-02: Path segment `/s/<stage>`.** `/` = the shell with Analyze (the default, bare root — no stage suffix). Other stages are `/s/discover`, `/s/proposals`, `/s/metadata`, etc. Rail clicks `hx-get` the stage fragment and `hx-push-url` the `/s/<stage>` path; the `htmx:historyRestore` re-init handler (locked in ROADMAP) re-binds against this scheme. One handler owns stage resolution + per-stage validation. This is the redirect target for D-03.

### Legacy bookmark / route resolution (SHELL-05)
- **D-03: Redirect to the canonical shell URL.** The 6 "render-in-shell" legacy routes (`/proposals`, `/tracklists`, `/tags`, `/cue`, `/duplicates`, `/preview`) **302-redirect** to their canonical shell URL (`/s/<stage>`), which renders the shell with that rail node pre-selected. This keeps a single canonical URL per view (clean `hx-push-url` history, no two-URLs-one-view ambiguity) and resolves in ≤1 hop. The two true renames stay as already locked in ROADMAP: `/pipeline` → `/` and `/search` → ⌘K, both via `RedirectResponse` on the trailing-slash canonical form (`redirect_slashes=True`). The redirect-loop test asserts every one of the 8 legacy routes lands on a 200 with the matching rail node pre-selected.

### ⌘K affordance (Phase 57 scope only)
- **D-04: Skeleton modal.** The header shows a ⌘K button and the keybinding opens an **empty/placeholder palette modal** (Alpine-driven) — establishing the affordance + keybinding + open/close contract early. **No search wiring in Phase 57**; the full unified palette (files/tracklists/artists + commands) is Phase 61. The `/search` → ⌘K rename redirect (D-03 / ROADMAP) still applies, but in Phase 57 the modal it lands users in is the skeleton.

### Header status strip (Phase 57 scope only)
- **D-05: Minimal — agent status dots + Agents link.** The strip shows agent online/total status dots and an "Agents" link, fed by the **single existing `/pipeline/stats` 5s poll** fanned out via `hx-swap-oob` behind the `oob_counts` gate (no new poll loop). Lane-capacity detail (local/A1/k8s capacity cards) is **deferred to the Analyze workspace in Phase 58** — keep it out of the Phase 57 strip.

### Claude's Discretion
- Exact rail-node→count mapping and which DAG nodes show a live count vs. a static label in Phase 57 (drive from the existing `/pipeline/stats` payload + the existing `$store.pipeline` keys; do not redefine the store).
- The precise fragment-extraction mechanism for bridged legacy routes (e.g., `hx-request` header branch vs. a shared `_shell_or_fragment` helper) — pick the pattern that keeps each legacy router minimally touched.
- Skeleton-modal visual treatment (must read as C3 / Jura-blue, but contents are placeholder).
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Design & IA (authoritative)
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` — the validated v7.0 design: three-column Hybrid Console IA (§5), pipeline/rail model & order (§5), stage workspaces (§6), C3 "Evolved phaze" aesthetic decision (§4), technical approach / reuse-routers constraint (§11), and open questions (§13).
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` — **the canonical interactive reference** (rail-as-nav, every stage, ⌘K, Agents, full record, empty/scan). Match this layout/behavior.
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/aesthetic-C3-evolved.html` — the chosen aesthetic isolated.

### Requirements & roadmap
- `.planning/REQUIREMENTS.md` — SHELL-01..05 (the 5 requirements this phase delivers).
- `.planning/ROADMAP.md` § "Phase 57: Shell & DAG rail" — Goal, Success Criteria (1–5), and the **Notes** block, which LOCKS the cross-cutting contracts below. Treat the Notes as binding.

### Locked cross-cutting contracts (from ROADMAP Notes — do not re-litigate)
- Single stable swap-target id **`#stage-workspace`**; **fragment-only** stage responses (never `extends base.html`).
- OOB id registry + **`oob_counts` gate**; one `/pipeline/stats` 5s poll fanned out via `hx-swap-oob` (no per-region poll loops).
- **`$store.pipeline` is consumed, not redefined** (see `src/phaze/templates/base.html:106` + `pipeline/partials/stats_bar.html`).
- `htmx:historyRestore` re-init handler; focus-to-heading + **skip-link → `#stage-workspace`** baseline.
- Stack version bumps: **htmx→2.0.10 / Alpine→3.15.12 / Tailwind→4.3.2**, and **recompute every `integrity=` SRI hash** (a stale hash silently blocks the script). Stay on htmx 2.0.x (4.0 is beta).
- SHELL-05 hybrid routing per D-02/D-03 above; `redirect_slashes=True`.
- Seed the **dead-template AST guard test** and keep it green through cutover.

### In-repo code to reuse / preserve (verified during scout)
- `src/phaze/templates/base.html` — current nav row (to be replaced by header + rail), the theme store machinery (`<head>` no-FOUC script + `Alpine.store('theme')`, lines ~54–79), Jura/wave brand + `phaze-bg`/`phaze-panel` tokens + `@custom-variant dark` (lines ~140–269), and **vendored Tailwind** at `/static/vendor/tailwindcss-browser-4.3.0.min.js` (htmx/Alpine load from CDN with SRI — these are the scripts to bump + re-SRI).
- `src/phaze/routers/pipeline.py` — `/pipeline/` dashboard (`:434`), `/pipeline/stats` partial (`:549`), the per-DAG-node store-key context builder (`:132`), and `oob_counts=True` emission (`:606`). The new `/` + `/s/<stage>` shell handlers live alongside these.
- `src/phaze/templates/pipeline/partials/stats_bar.html` — the existing OOB `$store.pipeline` seed pattern (`analyze-files-ready`, `agent-busy-seed`, etc.) the new rail counts + status strip must reuse, not duplicate.
- Legacy routers to bridge (D-01) / redirect (D-03): `proposals.py`, `tracklists.py`, `tags.py`, `cue.py`, `duplicates.py`, `preview.py`, `search.py`, plus `admin_agents.py` (Agents link target). All mounted in `src/phaze/main.py` (`:185–227`).

**No phase research needed** — the ROADMAP states all patterns are in-repo; this CONTEXT + the design doc + prototype are sufficient inputs for planning.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Theme system** (`base.html` `<head>` script + `Alpine.store('theme')`): auto/dark/light with localStorage + no-FOUC pre-Alpine class application. Lift verbatim into the new shell `<head>` — SHELL-04 requires it survives unchanged.
- **`$store.pipeline`** (`base.html:106`) + the OOB seed paragraphs in `stats_bar.html`: the single source of truth for stage counts / agent-busy gating. Rail counts and the status strip consume these keys; do not add a parallel store.
- **`/pipeline/stats` 5s poll** (`pipeline.py:549`, `oob_counts=True` at `:606`): the one poll that fans out via `hx-swap-oob`. The status strip (D-05) and rail counts ride this same response.
- **C3 design tokens** already present: `phaze-bg`/`phaze-panel`, `.font-jura`, blue accent, `@custom-variant dark` — the shell restyle works within these.

### Established Patterns
- **Fragment-vs-full-page rendering**: legacy routes currently `extends base.html` (full pages). The bridge (D-01) and shell-render (D-03) require a content-block-only fragment path triggered on HTMX requests — this is the central new pattern Phase 57 introduces and 58–61 reuse.
- **Router prefixes**: most UI routers carry a `/<name>` prefix (`proposals.py` → `/proposals`); `pipeline.py` is prefix-less (`tags=["pipeline"]`). New `/` + `/s/<stage>` shell handlers fit naturally in the prefix-less pipeline router or a dedicated shell router.

### Integration Points
- `src/phaze/main.py:185–227` — router include order; the new shell handlers and the `redirect_slashes=True` behavior plug in here. No new `/` route exists today (root currently has no explicit handler), so Phase 57 ADDS the canonical `/` shell route.
- The dead-template AST guard is a NEW test artifact this phase seeds (no existing analog) — it asserts no template is orphaned as workspaces migrate.
</code_context>

<specifics>
## Specific Ideas

- The **prototype.html is the canonical visual/behavioral target** — when in doubt about layout, rail order, or interaction, match the prototype, not a fresh interpretation.
- The shell must read as **evolution, not reskin** (C3): keep Jura/blue/wave/dark — answer the "v1-ish" complaint via IA restructure, not a new visual identity.
- The app must stay **fully usable at every commit** through the v7.0 cutover — the bridge (D-01) exists specifically so no functionality regresses while workspaces are migrated phase by phase.
</specifics>

<deferred>
## Deferred Ideas

- Redesigned rich workspaces (Analyze lane cards w/ live capacity + Kueue quota-wait/Inadmissible; Discover/Metadata/Fingerprint views) — **Phase 58** (WORK-01..05).
- Identify workspaces (Track-ID, Tracklist Search→Scrape→Match inline) — **Phase 59**.
- Unified Review & Apply diff/approve gate (rename/tag/move + Dedupe + Cue, per-file + bulk high-confidence) — **Phase 60**.
- Functional ⌘K palette (unified search over files/tracklists/artists + commands), per-file full-record slide-in, Agents page rebuild (incl. ephemeral k8s identity), empty/first-run scan — **Phase 61**.
- a11y depth, responsive/narrow rail-collapse-to-icons, density pass, **dead-template/route removal (CUT-02)**, docs/README IA rewrite — **Phase 62** (necessarily last).
- Design §13 open questions (confidence threshold for "approve all high-confidence"; ⌘K keyboard-nav depth; full C3 light-theme treatment) — surface in their owning phases (60/61/62), not here.

None of these arose as scope creep — the discussion stayed within the Phase 57 shell boundary; these are the planned downstream phases.
</deferred>

---

*Phase: 57-shell-dag-rail*
*Context gathered: 2026-06-29*
