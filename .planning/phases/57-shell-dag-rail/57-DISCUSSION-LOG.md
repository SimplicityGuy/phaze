# Phase 57: Shell & DAG rail - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-29
**Phase:** 57-shell-dag-rail
**Areas discussed:** Stage-workspace bridge, ⌘K affordance, Header status strip, Legacy bookmark resolution, Canonical URL scheme

---

## Stage-workspace bridge (what a rail click shows in Phase 57)

| Option | Description | Selected |
|--------|-------------|----------|
| Embed existing content | Each rail node renders the matching legacy template's content as a fragment inside `#stage-workspace`; app stays fully usable through cutover; 58–61 swap each fragment for the redesigned one | ✓ |
| Stub placeholders | Non-Analyze nodes show "coming soon"; functionality only via legacy URLs until replaced | |
| Hybrid | Wire high-traffic nodes now, stub the rest | |

**User's choice:** Embed existing content
**Notes:** Matches the ROADMAP's "old tab routes render into the shell" + "dead-template guard watched green through cutover." Keeps the app continuously usable across the v7.0 cutover.

---

## ⌘K affordance (Phase 57 scope)

| Option | Description | Selected |
|--------|-------------|----------|
| Skeleton modal | ⌘K button + keybinding opens an empty/placeholder palette modal; no search wiring; full palette in Phase 61 | ✓ |
| Interim search | ⌘K opens a minimal modal running the existing /search backend | |
| Affordance only | Non-functional ⌘K hint, no modal/keybinding until Phase 61 | |

**User's choice:** Skeleton modal
**Notes:** Establishes the affordance + keybinding + open/close contract early; contents filled in Phase 61.

---

## Header status strip (Phase 57 scope)

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal dots + link | Agent online/total status dots + "Agents" link, fed by the single /pipeline/stats poll | ✓ |
| Dots + queue summary | Adds a compact in-flight/queued count to the strip | |
| You decide | Planner picks minimal strip satisfying SHELL-03 | |

**User's choice:** Minimal dots + link
**Notes:** Lane-capacity cards (local/A1/k8s) stay deferred to the Analyze workspace in Phase 58.

---

## Legacy bookmark resolution (the 6 render-in-shell routes)

| Option | Description | Selected |
|--------|-------------|----------|
| Redirect to shell URL | /proposals etc. 302-redirect to a canonical shell URL that renders the shell with the rail node pre-selected | ✓ |
| Render shell in place | /proposals itself returns the full shell, no redirect (two URLs, one view) | |
| You decide | Planner chooses per the ≤1-hop + hx-push-url contract | |

**User's choice:** Redirect to shell URL
**Notes:** Single canonical URL per view; clean hx-push-url history; ≤1 hop. `/pipeline`→`/` and `/search`→⌘K renames stay as locked in ROADMAP.

---

## Canonical stage-selection URL scheme

| Option | Description | Selected |
|--------|-------------|----------|
| Query param ?stage= | `/` = Analyze, `/?stage=proposals`, etc. | |
| Path segment /s/ | `/` = Analyze, `/s/proposals`, `/s/discover`, etc.; redirect targets become `/s/<stage>` | ✓ |
| You decide | Planner picks per redirect-loop + historyRestore contract | |

**User's choice:** Path segment `/s/<stage>`
**Notes:** Ties together the bridge, the bookmark redirects (D-03), and the hx-push-url/historyRestore contract. `/` (bare root) = Analyze default with no stage suffix.

---

## Claude's Discretion

- Rail-node→count mapping (which nodes show a live count vs. static label), driven from the existing `/pipeline/stats` payload + `$store.pipeline` keys.
- Fragment-extraction mechanism for bridged legacy routes (hx-request branch vs. shared helper).
- Skeleton-modal visual treatment (C3 / Jura-blue, placeholder contents).

## Deferred Ideas

- Phase 58: redesigned Discover/Metadata/Fingerprint/Analyze workspaces incl. lane cards.
- Phase 59: Identify workspaces (Track-ID, Tracklist inline chain).
- Phase 60: unified Review & Apply diff/approve gate + Dedupe + Cue.
- Phase 61: functional ⌘K palette, per-file full-record slide-in, Agents page rebuild, empty/first-run scan.
- Phase 62: a11y/responsive/density polish, dead-template/route removal (CUT-02), docs/README IA rewrite.
- Design §13 open questions deferred to owning phases (60/61/62).
