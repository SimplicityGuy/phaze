# Research Summary — phaze v7.0 UI Redesign (DAG-Centric Hybrid Console)

**Project:** phaze v7.0
**Domain:** Server-rendered admin console IA/template rewrite (FastAPI + Jinja2 + HTMX + Tailwind + Alpine)
**Researched:** 2026-06-29
**Confidence:** HIGH (all findings grounded in actual `src/phaze/` code; one scoped gap at MEDIUM)

---

## Executive Summary

v7.0 is a presentation rewrite, not a product feature: it collapses ~10 legacy tabs into a three-column DAG-centric shell (rail · workspace · per-file pane) over an entirely unchanged backend. The approach is conservative by design — every stage workspace maps to an existing router and service, the stack gains exactly one new CDN dependency (`@alpinejs/focus@3.15.12` for the ⌘K focus trap), and three of the four existing CDN libraries get patch-level bumps only. Reuse is aggressive: the `HX-Request` full-vs-fragment split already exists in 8 routers, the single-poll/OOB-fanout architecture that must power all live rail counts is already proven in `stats_bar.html`, and the `$store.pipeline` Alpine store already carries every count the rail needs. The redesign's job is to give those existing components a better home, not to replace them.

Three convergent findings across all four research files define the mandatory architecture: (1) There is ONE poll — `GET /pipeline/stats` every 5 seconds — and all live rail counts, header status dots, and workspace counts must fan out from it via `hx-swap-oob` behind the existing `oob_counts` gate. The repo already has ~15 `every Ns` triggers; adding independent per-region pollers is the single fastest path to production pain. (2) Every stage endpoint must branch on `HX-Request`: fragment for HTMX swaps, full shell for direct navigation/refresh. The pattern exists in 8 routers and must be standardized across all 12 new workspace fragments. (3) Phase 57 (Shell & rail) is uniquely load-bearing — it must lock the swap-target contract, the OOB id registry, cross-swap `$store` survival, and the theme/brand scaffolding that all later phases depend on. Cutting corners in Phase 57 means every later phase inherits structural debt.

The single concrete scope risk is **IDENT-01**: the REQUIREMENTS.md text says "AcoustID→MusicBrainz match state" but `grep -ri 'acoustid|musicbrainz' src/phaze` returns empty — the capability does not exist. Three of the four research agents independently surfaced this gap. Building AcoustID/MusicBrainz would be a net-new backend integration and would violate the milestone's explicit no-backend-change boundary. **This must be resolved before Phase 59 planning**, not during it. The recommended resolution is to re-scope IDENT-01 to surface the existing identity signals (audfprint+panako fingerprint match/score + rapidfuzz tracklist match confidence) and defer AcoustID/MusicBrainz to a future milestone.

---

## Key Findings

### Recommended Stack

The v7.0 stack is the locked existing stack with minimal version bumps. No new frameworks, no build step, no Node. The only addition is one official Alpine plugin.

**Core technologies (bump only — already in production):**
- **htmx 2.0.10** (was 2.0.7) — rail→workspace stage swaps, deep-link history, OOB rail/header updates; stay on the 2.0.x line — htmx 4.0.0 is in beta and must not be adopted mid-rewrite
- **Alpine.js 3.15.12** (was 3.15.9) — local UI state: ⌘K open/close, theme store, per-row selection, slide-in panel
- **@tailwindcss/browser 4.3.2** (was 4.3.0) — keep self-vendoring in `static/vendor/`; CDN minification diverges per-edge and breaks SRI (the repo already hit this bug)
- **htmx-ext-sse 2.2.4** — unchanged; stays scoped to the one bounded execution-progress stream it already serves; do NOT expand its use to rail/workspace live updates

**The ONE new dependency:**
- **@alpinejs/focus 3.15.12** — `x-trap.inert.noscroll` for ⌘K and the slide-in record panel; the only correctly-implemented focus trap approach in a no-build stack; 3KB, official, CDN-delivered; version must exactly match Alpine core

**What NOT to use:** htmx 4.0.0-beta; any SPA framework; graph libraries (mermaid/cytoscape/d3) for the rail; command-palette libraries (cmdk/kbar are React-only; ninja-keys pulls in lit); `htmx-ext-head-support`; WebSockets; Tailwind via bare CDN URL (SRI divergence risk).

All version bumps require SRI hash recomputation (`curl | openssl dgst -sha384`). A stale hash blocks the script silently.

### Expected Features

v7.0 has 25 locked requirements across 6 categories. "Must have" means required for the milestone to ship; "defer" items are already named and catalogued.

**Must have — the 25 locked requirements (SHELL/WORK/IDENT/REVIEW/RECORD/CUT):**
- Three-column shell with DAG rail-as-nav: rail-click → center swap via HTMX without full-page reload (SHELL-01/02)
- Per-stage live counts on the rail, header agent-status strip, legacy route redirects, theme/brand preserved (SHELL-03/04/05)
- Discover/Metadata/Fingerprint/Analyze workspaces with lane cards (local/A1/k8s) and Kueue quota-wait vs. Inadmissible (WORK-01 through WORK-05)
- Track-ID workspace surfacing **existing** fingerprint+tracklist signals (NOT AcoustID/MusicBrainz — see gap); Tracklist Search→Scrape→Match 3-step (IDENT-01/02)
- Unified before→after diff/approve gate across Rename/Tag/Move; bulk high-confidence approval server-predicate evaluated; dedupe keeper-select; cue preview; audit+undo (REVIEW-01 through REVIEW-05)
- Full per-file record (slide-in); ⌘K command palette over existing FTS; Agents with ephemeral k8s identity; first-run empty state (RECORD-01 through RECORD-04)
- Keyboard nav/focus/ARIA baseline; dead-code removal; docs update; narrow rail-collapse (CUT-01 through CUT-04)

**Defer to v7.x:**
- REVIEW-06: per-stage configurable confidence thresholds (ship a fixed threshold in v7.0)
- WORK-06: `cloud_phase`-driven admission-state sub-states (v6.0 KROUTE-06 deferred)
- RECORD-05: full first-class C3 light theme treatment (dark is primary for v7.0)

**Defer to v8+:**
- AcoustID/MusicBrainz Track-ID — net-new backend integration, out of v7.0 scope
- SHELL-06: mobile/touch layout
- XAGENT-01: cross-file-server fingerprint identity

**Anti-features confirmed off the table:** drag-to-reorder rail; animated DAG graph canvas as home (rejected in design); per-stage historical charts; auto-applying high-confidence without a human click; Approve-with-no-undo fast path.

### Architecture Approach

The architecture is an integration rewrite, not a ground-up build. A new `shell.html` defines the three-column flex layout and a new thin `routers/shell.py` owns `/`, `/shell/{stage}`, `/command`, and `/record/{file_id}`. All existing routers and services are unchanged in data logic; they gain (or have expanded) an `HX-Request` fragment branch. The 5-second `/pipeline/stats` poll and `$store.pipeline` Alpine store — already proven in the dashboard — become the live-data spine for the entire console; the rail is just a new presentation of the existing store, consuming it rather than reinventing it.

**Major components (new templates + thin routing only):**
1. `shell.html` + `shell/` template subtree — three-column shell, rail, header, ⌘K overlay, file pane, all 12 workspace fragments; the full `base.html` `<head>` is reused verbatim
2. `routers/shell.py` — thin new router calling existing services read-only; the only genuinely new server file
3. Stage workspace fragments (`shell/workspaces/*.html`) — one per rail node; each `{% include %}`d by the full-shell route and returned bare by the HTMX route (one partial, two callers — no duplication)
4. `record/detail.html` — full-record slide-in composing existing `AnalysisWindow`, `FileRecord`, `tag_comparison`, `audit_log` partials
5. Existing UI routers (proposals/tags/duplicates/cue/tracklists/execution) — modified only to return `shell.html` on direct navigation and the workspace fragment on `HX-Request`; data logic untouched
6. `services/*` + `models/*` + `base.html <head>` + `$store.pipeline` — entirely unchanged

**The standardized full-vs-fragment pattern** (already present in 8 routers): `if request.headers.get("HX-Request") == "true"` → return the workspace fragment; else → return `shell.html` with that stage's workspace inlined via `{% include %}` and the rail node pre-selected. This handles direct navigation, bookmarks, browser back/forward, and HTMX swaps from one branch point.

**SHELL-05 redirect strategy (hybrid):** Routes with a clean canonical URL (`/proposals/`, `/tags/`, `/cue/`, `/duplicates/`, `/tracklists/`) render-in-shell (the URL is already the stage state — no redirect needed). True renames (`/pipeline/` → `/`, `/search/` → `/?cmd=1`) use `RedirectResponse`. Account for FastAPI's `redirect_slashes=True` — redirect the canonical trailing-slash form, not the bare path, to avoid 2-hop chains.

### Critical Pitfalls

Ten pitfalls documented; top five by severity and phase impact:

1. **Poll storms — independent per-region pollers** — the repo already has ~15 `every Ns` triggers; a three-column console naively adding rail/workspace/pane pollers independently can easily fan out to 3-5 concurrent DB-hitting loops. Prevention: one console-level poll (`/pipeline/stats` every 5s) fans out to all live regions via `hx-swap-oob`; no second poll loop; add a `visibilitychange` guard so polls shed when the tab is backgrounded. Address in Phase 58.

2. **IDENT-01 scope violation** — the prototype labels Track-ID "AcoustID→MusicBrainz" but that capability is absent from the codebase. Building it would add a new API client, network dependency, and rate-limit surface — a net-new backend feature that violates the milestone boundary. Re-scope to existing signals or defer. Resolve before Phase 59 planning begins.

3. **REVIEW-02 stale bulk approval** — today's bulk approve serializes a client-side `selectedRows` Set into hidden inputs and submits without server re-validation. In a live-polling console the render→submit window can be many seconds; an operator can approve a stale set, leading to renames/tag-writes/file-moves on wrong rows. Prevention: "approve all high-confidence" must send a server-evaluated predicate (action + threshold), not a client-built id list. The server re-queries pending rows above threshold at submit time. Address in Phase 60.

4. **Rail swap clobbers the shell (wrong hx-target)** — if a rail node targets too broad a region (`body`, `#shell`), the swap destroys the header, rail, and pane. Stage endpoints returning full `extends base.html` pages inject nested `<nav>`, duplicate `$store`, and duplicate theme toggles into the center column. Prevention: one stable `#stage-workspace` id; all stage endpoints return fragments only; the shell renders once on `GET /`. Lock this contract in Phase 57 before building any stage.

5. **URL/history breakage and redirect loops** — `hx-push-url` snapshots the DOM on forward navigation; back-button restores a potentially-dead (Alpine-uninitiated) snapshot. SHELL-05 redirects that target `/proposals` instead of `/proposals/` (the canonical trailing-slash form) create 2-hop chains with FastAPI's `redirect_slashes=True`. Prevention: every stage URL is server-resolvable (full shell + stage pre-selected); add a redirect-loop test asserting each legacy path resolves in ≤1 hop to a 200; handle `htmx:historyRestore` to re-init Alpine. Address in Phase 57.

**Additional pitfalls to hold in mind:** lost Alpine `x-data` state after swaps (hoist cross-swap state to `$store`; Pitfall 2 addressed in Phase 57 + 60); OOB id collisions from un-gated OOB blocks at initial render (the `oob_counts` gate is mandatory; Pitfall 3); dead-code cutover breaking surviving `{% include %}` references (Pitfall 7; seed the dead-template test in Phase 57, execute CUT-02 in Phase 62); theme/brand regression from porting the prototype `<head>` wholesale (Pitfall 9; preserve `base.html`'s `_applyTheme`, pre-flash IIFE, `@custom-variant dark`, vendored Tailwind).

---

## Implications for Roadmap

The phase structure is already defined in REQUIREMENTS.md (phases 57–62). The research validates and constrains it — the dependency order is strict and non-negotiable.

### Phase 57 — Shell & Rail (SHELL-01..05)

**Rationale:** Every other phase renders into the shell. Phase 57 is the load-bearing risk phase: it must establish all cross-cutting contracts that later phases depend on. Do not under-scope it.

**Delivers:** `shell.html` three-column layout; `_rail.html` nav spine with live count bindings; `_header.html`; `shell.py` thin router; `GET /` (Analyze default); `HX-Request` full-vs-fragment convention; all SHELL-05 redirects and render-in-shell branches; theme/brand reused from existing `base.html <head>` verbatim.

**Must lock in this phase (not deferred):**
- The single stable swap target id (`#stage-workspace`)
- Fragment-only stage responses (no `extends "base.html"` in swap targets)
- `$store.pipeline` consumption pattern (not redefinition)
- OOB id registry + `oob_counts` gate (no OOB at initial render)
- Stage-resolvable `/` + `hx-push-url` + `htmx:historyRestore` handler
- SHELL-05 redirect-loop test (≤1 hop to 200 for each of 8 legacy routes)
- Focus/ARIA contract: `aria-current="page"` on active rail node, focus-to-heading after swap, skip link targeting `#stage-workspace`
- Theme smoke test: FOUC absent, `dark:` utilities work, vendored Tailwind, phaze accent
- Dead-template AST guard test (seeded green now; watched green through cutover)

**Research flag:** No phase research needed. All patterns are in-repo.

### Phase 58 — Enrich & Analyze Workspaces (WORK-01..05)

**Rationale:** First phase that populates the shell with real workspace content. The Analyze workspace is the densest live region and owns the critical "single poll" consolidation decision (WORK-05).

**Delivers:** Discover/Metadata/Fingerprint/Analyze workspace fragments; 3 lane cards (local/A1/k8s) with capacity, utilization bar, and Kueue quota-wait vs. Inadmissible; per-file lane badge + windowed progress; live refresh via the existing `/pipeline/stats` 5s poll + `$store.pipeline` OOB seeds.

**Key constraint:** No second poll loop. WORK-05 is not a feature to build — it is a discipline to maintain. Verify one request/5s in the network tab.

**Research flag:** No phase research needed. All data sources in `pipeline.py`, `pipeline_scans.py`, `pipeline_stages.py`; lane-card partials exist from v6.0.

### Phase 59 — Identify Workspaces (IDENT-01..02)

**Rationale:** Simpler in implementation scope but blocked by the IDENT-01 scope decision. Do not plan this phase until the AcoustID/MusicBrainz question is resolved.

**Delivers:** Tracklist workspace with Search→Scrape→Match 3-step chips; Track-ID workspace surfacing existing identity signals.

**BLOCKING PREREQUISITE — choose one before Phase 59 planning:**
- **Option 1 (recommended):** Re-label Track-ID to surface audfprint+panako fingerprint match/score + rapidfuzz tracklist match confidence. No backend change. Ship.
- Option 2: Defer IDENT-01 to v7.x; ship only Tracklist in Phase 59.
- Option 3: Treat AcoustID/MusicBrainz as an explicit deliberate backend exception. Requires scope amendment; adds API client + network dep + rate-limit surface.

**Research flag:** No phase research needed for options 1 or 2. Option 3 requires targeted research before planning.

### Phase 60 — Review & Apply (REVIEW-01..05)

**Rationale:** The most correctness-sensitive phase. Collapses the highest-stakes user interaction (approvals on an irreplaceable archive) into a unified diff gate.

**Delivers:** Unified before→after diff component across Rename/Tag/Move (one Jinja partial over three existing data sources); dedupe keeper-select; cue preview + approve; per-file Approve/Edit/Skip; bulk "approve all high-confidence" gated by a server-evaluated predicate.

**Key constraints:**
- REVIEW-02 bulk must send a predicate (threshold + action), not a client-built id list.
- Pick a fixed server-side confidence threshold at Phase 60 plan time (REVIEW-06 deferred).
- Every apply action must POST to existing endpoints that write `ExecutionLog`. Assert an audit row per apply.
- The 5s diff-list poll must OOB-update counts only — never re-render the operator's in-progress selection subtree.

**Research flag:** No phase research needed. All endpoints exist; pattern is standard `HX-Request` branch + existing approve/undo routers.

### Phase 61 — Full Record + ⌘K + Agents (RECORD-01..04)

**Rationale:** Additive features over the already-live shell and workspaces. The Agents page carries v6.0 KDEPLOY-04 intent (ephemeral k8s identity) into the new UI.

**Delivers:** Full-record slide-in composing existing `analysis_timeline.html` + tag comparison + audit history; ⌘K palette (Alpine `x-data` + `@alpinejs/focus` `x-trap` + debounced `hx-get="/command"`); Agents page with ephemeral k8s burst identity; first-run empty state.

**Key constraints:**
- `@alpinejs/focus@3.15.12` introduced here — load plugin `<script defer>` before Alpine core; version must match Alpine core exactly.
- Per-file pane and record must ride the existing single poll, not add a new one.
- ⌘K quick commands must funnel through the same router endpoints and `enqueue_router` guards.

**Research flag:** No phase research needed. Stack patterns documented in STACK.md; RECORD-01 composition mapped in ARCHITECTURE.md.

### Phase 62 — Polish & Cutover (CUT-01..04)

**Rationale:** Dead-code removal is only safe after every legacy route is served and every workspace has replaced its old page. CUT-02 must be last.

**Delivers:** Full keyboard/ARIA audit (CUT-01 parity sign-off); dead-code removal with three-way grep + dead-template test green (CUT-02); docs/README update (CUT-03); narrow rail-collapse to icons (CUT-04).

**What to delete in CUT-02:** `proposals/list.html`, `tags/list.html`, `duplicates/list.html`, `cue/list.html`, `tracklists/list.html`, `search/page.html`, `preview/tree.html`, `pipeline/dashboard.html`, and the nav block in `base.html`. Keep all `partials/` — they are the shell's fragments.

**Research flag:** No phase research needed.

### Phase Ordering Rationale

- 57 must be first — all later phases render into the shell.
- 58 before 59 — the workspace pattern (header + counts + action + table) is established in 58 and reused in 59.
- 58 before 60 — the file-row→pane plumbing used in Review is established in 58.
- 61 after 58–60 — the record slide-in links into workspace fragments (lane badges, pending approval rows) that must exist first.
- 62 last — CUT-02 deletes the legacy page wrappers; removing them before 57–61 are complete breaks the SHELL-05 redirects.

### Research Flags Summary

| Phase | Research needed? | Reason |
|-------|-----------------|--------|
| 57 — Shell & rail | No | All patterns in-repo |
| 58 — Enrich + Analyze | No | All data sources in `pipeline.py`; lane-card partials from v6.0 |
| 59 — Identify | CONDITIONAL | Only if IDENT-01 option 3 chosen; options 1/2 need no research |
| 60 — Review & Apply | No | All endpoints exist; standard pattern |
| 61 — Full record + ⌘K | No | Alpine focus + HTMX search pattern fully documented |
| 62 — Polish & cutover | No | Standard cleanup; dead-template test seeded in 57 |

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All versions verified live on npm 2026-06-29; every pattern grounded in existing `src/phaze/` code; one new dep is official and CDN-delivered |
| Features | HIGH | 25 requirements are locked; every feature traced to a present router/partial; IDENT-01 gap confirmed by grep |
| Architecture | HIGH | Every integration point grounded in a real file under `src/phaze/`; `HX-Request` pattern verified in 8 routers; IDENT-01 backend gap flagged explicitly |
| Pitfalls | HIGH | All 10 pitfalls anchored to specific file+line; HTMX history/OOB behavior verified against current docs via Context7 |

**Overall confidence:** HIGH

### Gaps to Address

- **IDENT-01 backend gap (resolve before Phase 59):** `grep -ri 'acoustid|musicbrainz' src/phaze` returns empty. Also verify what `models/fingerprint.py` + `routers/agent_fingerprint.py` actually persist — if pyacoustid lookup results are stored there, IDENT-01 option 1 can surface them directly. Resolve before Phase 59 planning.

- **REVIEW-02 confidence threshold value:** Research confirms "ship a fixed threshold" but does not specify the value. Determine at Phase 60 plan time; check `tracklists.py` `reject-low` endpoint as a reference point.

- **⌘K "artists" search facet:** Verify whether `search_queries.search` surfaces an artist dimension or whether "artists" maps to file/tracklist artist fields. Verify at Phase 61 plan time; no backend change either way.

- **SRI hash recomputation:** Every version bump requires recomputing the `integrity=` hash. Mechanical but must happen in Phase 57; a stale hash silently blocks the script.

---

## Sources

### Primary (HIGH confidence)

- `src/phaze/templates/base.html` — theme store, `$store.pipeline`, brand tokens, pre-flash IIFE, vendored Tailwind rationale, tab nav to retire
- `src/phaze/templates/pipeline/{dashboard.html,partials/stats_bar.html}` — single-poll/OOB-fanout architecture, `oob_counts` gate, `dag-seed-<key>` id contract
- `src/phaze/routers/pipeline.py:434,549,625,800,857,937,1023,1074,1202,1233,1280` — all stage trigger endpoints + `pipeline_stats_partial`
- `src/phaze/routers/{search.py:74,proposals.py:157,tags.py:201,duplicates.py:105,cue.py:228,tracklists.py:152,execution.py:372,admin_agents.py}` — the `HX-Request` full-vs-fragment pattern in 8 routers
- `src/phaze/templates/proposals/list.html` + `partials/bulk_actions.html` — `selectedRows` client-id bulk (the anti-pattern REVIEW-02 must fix)
- `src/phaze/main.py:185-229` — router registration; no bare `/` handler; no `RedirectResponse` today
- `src/phaze/models/analysis.py` + `templates/proposals/partials/analysis_timeline.html` — windowed timeline backing RECORD-01
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` + `2026-06-28-ui-redesign-assets/prototype.html` — locked design spine
- `.planning/REQUIREMENTS.md` — 25 locked requirements, phase 57–62 traceability
- npm registry `registry.npmjs.org` (live, 2026-06-29) — htmx.org 2.0.10, alpinejs 3.15.12, @alpinejs/focus 3.15.12, @tailwindcss/browser 4.3.2
- Context7 `/bigskysoftware/htmx` — `hx-push-url` DOM-snapshot behavior, `htmx:historyRestore`/`historyCacheMiss`, `hx-swap-oob` multi-target, `every Ns` polling semantics

### Secondary (MEDIUM confidence)

- `grep -ri 'acoustid|musicbrainz' src/phaze` — returns empty; IDENT-01 backend absence confirmed (absence-of-evidence; `models/fingerprint.py` not deeply inspected for pyacoustid persistence)
- [Command Palette Pattern — UX Patterns for Developers](https://uxpatterns.dev/patterns/advanced/command-palette) — ⌘K table-stakes conventions
- [GitLab CI/CD pipelines](https://docs.gitlab.com/ci/pipelines/) — rail-as-nav prior art

---
*Research completed: 2026-06-29*
*Ready for roadmap: yes — pending IDENT-01 scope decision before Phase 59 planning*
