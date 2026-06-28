<!-- GSD:DOC -->
# UI Redesign — DAG-Centric Hybrid Console (v7.0)

**Date:** 2026-06-28
**Status:** Validated design — approved in brainstorming, ready for `/gsd:new-milestone` (v7.0)
**Topic owner:** Robert
**Reference artifacts:** `docs/superpowers/specs/2026-06-28-ui-redesign-assets/`
- `prototype.html` — full interactive prototype (the canonical reference; rail-as-nav, every stage, ⌘K, Agents, full record, empty/scan)
- `aesthetic-C3-evolved.html` — chosen aesthetic, isolated
- `alt-A-mission-control.html`, `alt-B-file-workbench.html` — the two rejected IA directions (kept for rationale)

---

## 1. Problem

The current admin UI is functional but reads as an MVP ("v1-ish"). Concrete complaints from the owner:

- The main page lives at `/pipeline` and the app **lands on the second nav tab** (`/` redirects to `/pipeline`; Search is tab 1).
- The IA is a **flat row of ~10 sibling tabs** (Search · Pipeline · Proposals · Preview · Duplicates · Tracklists · Tags · Cue · Audit · Admin/Agents). Real work requires **constant jumping between tabs** even though it's all one pipeline.
- The **DAG exists but isn't the center of gravity**, and it does not richly integrate the cloud (A1) and Kubernetes (Kueue burst) execution targets.
- The mental model is a single spine, but the UI fragments it:

  > **Discover** files → unlocks **Metadata** + **deep Analysis** (local / cloud / k8s) → goal is per-file **full metadata + analysis + track-ID + tracklist** → **approval** steps for rename / tag-write / move / dedupe.

## 2. Goals / Non-goals

**Goals**
- Make the **DAG the home and the navigation spine** — `/` is the pipeline, no `/pipeline` URL, no landing on tab 2.
- **Collapse the tab sprawl** into the pipeline. Every pipeline concern becomes a stage; Search becomes a ⌘K command bar; Agents/health move to a status strip + a dedicated page.
- **Richly integrate local / A1 / k8s** as three Analyze execution *lanes* with live capacity, quota-wait, and Inadmissible state.
- Keep the **human-in-the-loop approval** model (rename / tag-write / move / dedupe) but unify it behind one consistent **before→after diff** pattern.
- Look like a matured product, not an MVP, **without discarding the existing brand** (Jura headings, blue accent, wave logo, dark theme + light toggle).

**Non-goals**
- No change to the backend stack: stays FastAPI + Jinja2 + HTMX + Tailwind + Alpine (server-rendered, no SPA build step).
- No change to the analysis/identify/proposal *logic* — this is an IA + presentation rewrite that **reuses existing routers and services**.
- Not a visual-identity replacement — C3 evolves the current language, it does not invent a new one.

## 3. Approaches considered

Three IA directions were prototyped (see assets). The differentiator is *what the home screen is centered on*.

| | A · Mission Control | B · File Workbench | **C · Hybrid Console (chosen)** |
|---|---|---|---|
| Home is… | corpus-wide DAG canvas | the file collection (rows w/ per-file steppers) | **DAG rail (spine+nav) + stage workspace + file pane** |
| Primary object | the flow | the file | both, side by side |
| Strength | "where is work stuck" at a glance | per-file human review | both without mode-switching |
| Weakness | per-file review is secondary | global flow is secondary | densest layout (3 columns) |

**Decision: C — Hybrid Console.** The owner's two goals pull apart — "centered on the DAG" (a *flow* view) vs. "per-file full metadata/analysis/track-ID/tracklist" (an *object* view). A and B each privilege one. C makes the **DAG rail a permanent spine that doubles as navigation** (the flow is always visible and you click *through* it instead of jumping tabs), while the right pane carries B's rich per-file workspace.

## 4. Aesthetic

Three visual treatments of the C layout were rendered (C1 dense-dark-console, C2 clean-light, C3 evolved-phaze).

**Decision: C3 — Evolved phaze.** Preserves the existing design language — **Jura** (tracked uppercase) for section/stage headers, **Inter** body, the **blue accent** palette, the **wave logo**, and the **dark `phaze-bg` theme with the light/dark toggle**. It answers the "v1-ish" complaint by restructuring the IA while reading as *evolution*, not a reskin. (C2 was rejected for dropping the dark theme, which fights the existing toggle/brand.)

## 5. Information architecture

**Three-column application shell** (single screen, no content tabs):

```
┌──────────────────────────────────────────────────────────────┐
│ HEADER: wave logo · ⌘K search · agent status dots · Agents     │
├──────────┬───────────────────────────────┬───────────────────┤
│ DAG RAIL │ STAGE WORKSPACE               │ PER-FILE PANE     │
│ (spine + │ (the selected rail node's     │ (windowed analysis│
│  nav +   │  file queue / lane summary /  │  + journey +      │
│  live    │  approval diffs)              │  facts + actions) │
│  status) │                               │                   │
├──────────┴───────────────────────────────┴───────────────────┤
│ FOOTER: breadcrumb + hint                                      │
└──────────────────────────────────────────────────────────────┘
```

- **Rail = nav.** Clicking a rail node swaps the center workspace and updates the right pane. This is the mechanism that kills tab-jumping. The rail is also live status (counts + dots per stage).
- **Full record** opens as a slide-in panel over the shell (from a file row or ⌘K).
- `/` renders this shell with **Analyze** selected by default.

### Pipeline model (rail order)

```
Discover
Enrich (parallel):  Metadata  ·  Fingerprint  ·  Analyze ─┬─ 🖥️ local lane
                                                          ├─ ☁️ A1 lane
                                                          └─ ⎈ k8s burst lane
Identify:           Track-ID  ·  Tracklist (Search → Scrape → Match)
Propose
Review & Apply:     Rename/Path · Tag write · Move files · Dedupe · Cue sheets
(below the line):   Audit log · Compute/Agents
```

This is a faithful re-grouping of today's stages — no new pipeline capabilities, just IA. (Metadata stays a **manual-trigger** stage per the Phase 35 decision.)

## 6. Stage workspaces (center pane)

Each rail node loads a workspace with a Jura header (title + live sub-count + stage actions) and a file table; rows open the full record.

- **Discover** — recent scans table (path/found/new/status/when) + the "not yet enriched" backlog count. Actions: Scan, Recover.
- **Metadata** — pending/extracted counts; manual `Extract selected / all`. Reuses existing `/pipeline/extract-metadata`.
- **Fingerprint** — chromaprint/AcoustID status. Reuses `/pipeline/fingerprint`.
- **Analyze** — three **lane summary cards** (local 8/8 · A1 2/4 · k8s 12-pending with Kueue quota note) + the in-flight file queue with a per-file lane badge. Actions: Route rules, Pause stage. Surfaces the Phase 54 **Inadmissible** alert inline.
- **Track-ID** — AcoustID → MusicBrainz recording match + confidence.
- **Tracklist** — the 1001Tracklists sub-chain shown as an inline 3-step (Search ✓ → Scrape ✓ → Match ⏳) + per-set match progress.
- **Propose** — AI rename/path proposals with model + confidence.

## 7. Review & Apply (the approval gate)

Collapses five legacy tabs (Proposals/Preview/Tags/Cue/Duplicates) into one gate with **one consistent interaction**:

- **Rename/Path, Tag write, Move files** — each row is a **before → after diff** (struck-through current vs. highlighted proposed) with **Approve / Edit / Skip**, plus a header **"Approve all high-confidence"** bulk action (confidence-thresholded). *(Approved model: per-file diff + bulk high-conf.)*
- **Dedupe** — duplicate groups with radio keeper-selection (others archived); `Auto-keep highest quality` bulk.
- **Cue sheets** — generated `.cue` preview with Approve/Edit; gated on a matched tracklist.

Every applied change lands in the **Audit log** (reversible).

## 8. Per-file full record

Slide-in over the shell. Sections: header (name / path / format / size / sha256 / lane) · **windowed multi-lane analysis timeline** (BPM/key/energy over the set's windows — builds on Phase 31) · metadata diff · identity (track-ID / tracklist match / proposed name) · **this file's pending approvals** (inline approve) · history. This is the "per-file full metadata/analysis/track-ID/tracklist" goal made concrete.

## 9. Global surfaces

- **⌘K command palette** — unified search over files / tracklists / artists + commands (scan, jump to a Review queue, open Agents). Replaces the Search tab.
- **Agents / Compute page** — local (nox) and A1 as healthy heartbeating agents; the **k8s burst lane is an ephemeral, Job-based identity** whose liveness derives from in-flight Kueue workloads — **never shown as perpetually-DEAD**. (Directly satisfies v6.0 KDEPLOY-04; this redesign is where that UI treatment properly lands.)
- **Empty / first-run + scan** — when there are no files, a centered "point phaze at your music" with a directory picker + agent selector + live scan progress.

## 10. Cloud / k8s integration

The three Analyze lanes are first-class throughout: lane badges on every in-flight file, lane capacity cards, Kueue quota-wait vs. Inadmissible surfacing, and the ephemeral k8s agent identity. The redesign assumes v6.0 (Phases 52–56) is shipped, so local/A1/k8s all exist as routing targets.

## 11. Technical approach

- **Keep the stack.** Server-rendered Jinja2 partials, HTMX for the rail-driven workspace swaps and the live polls (reuse the existing `/pipeline/stats` 5s poll pattern), Alpine for local UI state, Tailwind (CDN) with the existing `phaze-bg`/`phaze-panel` tokens + Jura/Inter fonts.
- **Reuse routers/services.** The stage workspaces are new templates over **existing** endpoints (scan, extract-metadata, fingerprint, analyze dispatch, tracklist search/scrape/match, proposals, execution/tags/cue/duplicates). This is an IA/template rewrite, not a backend rewrite.
- **Routing change.** `/` renders the new shell (Analyze default); old tab routes either redirect into the shell's stage state or are retired. The `base.html` nav row is replaced by the header + rail.
- **Theme.** Preserve the auto/dark/light store and the `.dark` class machinery already in `base.html`.

## 12. Proposed phase decomposition (for `/gsd:new-milestone` v7.0)

A full IA rewrite is milestone-sized. Suggested phase spine (the roadmapper will finalize):

1. **Shell & rail** — three-column app shell, header, ⌘K skeleton, DAG rail as nav, `/` route, theme preserved. Old nav retired behind the new shell.
2. **Enrich + Analyze workspaces** — Discover/Metadata/Fingerprint/Analyze stage views incl. the three lane cards + live polls.
3. **Identify workspaces** — Track-ID + Tracklist (Search→Scrape→Match inline).
4. **Review & Apply** — the unified diff/approve gate (rename/tag/move) + Dedupe + Cue, with per-file + bulk-high-conf.
5. **Full record + ⌘K + Agents** — the per-file slide-in, the command palette, the Agents page incl. ephemeral k8s identity, empty/scan first-run.
6. **Polish & cutover** — a11y, responsive/density pass, retire dead templates/routes, docs.

## 13. Open questions / deferred

- Exact confidence threshold + override for "approve all high-confidence" (per stage?).
- Keyboard-navigation depth for ⌘K (jump-to-file vs. run-command parity).
- Whether the light theme gets a full C3 treatment or stays a lower-priority variant (dark is primary).
- Mobile/narrow-width behavior (single-user desktop tool — likely low priority; collapse rail to icons).

## 14. Scope boundary

This milestone is **net-new** relative to v6.0 and **ships after** v6.0 (Phases 52–56) completes. It depends on v6.0 having delivered the local/A1/k8s routing targets it visualizes. It does **not** modify v6.0's backend behavior.
