# Requirements: Phaze — v7.0 UI Redesign (DAG-Centric Hybrid Console)

**Status:** ACTIVE — milestone v7.0 started 2026-06-29 (v6.0 shipped and archived). Originally scoped 2026-06-28 via the `/gsd:new-milestone` scope-only path; activated as the v7.0 `REQUIREMENTS.md` with STATE.md switched to v7.0 and the v6.0 phase dirs archived.

**Defined:** 2026-06-28
**Design source:** `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` (+ interactive prototype in the co-located `2026-06-28-ui-redesign-assets/`).
**Core Value:** Get 200K messy music/concert files properly named, organized, deduplicated, with rich metadata — human-in-the-loop approval. v7.0 replaces the MVP tab-sprawl UI with a **DAG-centric hybrid console**: the pipeline is the home and the navigation spine, the local/A1/k8s execution targets are first-class, and every human approval unifies behind one before→after diff/approve gate.

## Design spine (locked at milestone definition)

- **Direction:** "Hybrid Console" — three-column shell (DAG rail = spine + nav + live status · stage workspace · per-file pane). The rail IS the navigation; clicking a stage swaps the center workspace (no tab-jumping, no full-page nav).
- **Home:** `/` renders the shell with **Analyze** selected by default. No `/pipeline` URL; no landing on a secondary tab.
- **Full tab collapse:** the ~10 legacy sibling tabs become pipeline stages; Search → ⌘K command bar; Agents/health → header status strip + Agents page.
- **Aesthetic:** C3 "Evolved phaze" — preserve the existing brand (Jura headings, blue accent, wave logo, dark `phaze-bg` theme + light toggle). Evolve, don't reskin.
- **Approvals:** one consistent before→after diff + per-file Approve/Edit/Skip + bulk "approve all high-confidence" across Rename/Tag/Move; keeper-select for Dedupe; preview for Cue.
- **Stack unchanged:** FastAPI + Jinja2 + HTMX + Tailwind + Alpine, server-rendered, no SPA build. This is an IA/template rewrite that **reuses existing routers and services** — no backend behavior change, **with one deliberate scoped exception: Phase 57.1** (incremental window persistence) makes the single analysis-pipeline change v7.0 needs so WORK-04's in-flight progress is real; it is isolated to its own phase so every UI phase stays presentation-only (approved 2026-06-29).
- **Depends on v6.0:** visualizes the local / A1 / k8s routing targets that v6.0 (Phases 52–56) delivers; does not modify v6.0 backend behavior.

## v7.0 Requirements

Each maps to exactly one roadmap phase (Traceability below).

### Application shell & DAG rail (SHELL)

- [x] **SHELL-01**: Visiting `/` renders the new DAG-centric home (three-column shell) with the Analyze stage selected by default — no redirect to `/pipeline`, no landing on a secondary tab.
- [x] **SHELL-02**: A persistent left DAG rail lists every pipeline stage (Discover; Enrich = Metadata/Fingerprint/Analyze; Identify = Track-ID/Tracklist; Propose; Review & Apply = Rename/Tag/Move/Dedupe/Cue) with live counts, and clicking a stage swaps the center workspace via HTMX without a full-page navigation.
- [x] **SHELL-03**: The legacy top tab-bar is removed; global search becomes a ⌘K command bar in the header and compute/agent status moves to a header status strip.
- [x] **SHELL-04**: The existing auto/dark/light theme toggle and the Jura/blue/wave-logo brand language are preserved in the new shell.
- [x] **SHELL-05**: Old per-tab routes (`/pipeline`, `/proposals`, `/tracklists`, `/tags`, `/cue`, `/duplicates`, `/search`, `/preview`) redirect into the corresponding shell stage state so existing bookmarks do not break.

### Live analyze progress (PROG) — Phase 57.1 (scoped backend exception)

- [x] **PROG-01**: `analyze_file` bumps a progress **count** (`analysis.fine_windows_analyzed`/`fine_windows_total`) incrementally as each window completes during the run — not only atomically at completion — so an in-flight file exposes a real per-window progress count. (Counter-only per 57.1-CONTEXT D-01: the `analysis_window` **detail** rows continue to land atomically at completion via `put_analysis`; they are NOT written incrementally.)
- [x] **PROG-02**: The incremental counter is idempotent and safe under Phase 32 reboot/re-enqueue: a file interrupted mid-analysis leaves only a partial `analysis` row whose counter a re-run overwrites cleanly (reusing the `put_analysis` file_id-keyed replace), with no duplicate/orphaned state and no change to the final aggregates, the `analysis_window` rows, or the `ANALYZED` state flip.
- [x] **PROG-03**: The incremental progress is readable as a per-file, read-only mid-flight signal (`fine_windows_analyzed`/`fine_windows_total` on the file's in-progress `analysis` row) that the Phase 58 Analyze workspace can surface without any further backend change — and a partial in-progress row must NOT be treated as a completed analysis by proposals/search/sort (gated on a completion discriminator, not bare row existence).

### Enrich & Analyze workspaces (WORK)

- [x] **WORK-01**: Selecting Discover shows recent scans and the count of discovered-but-not-yet-enriched files, with a scan trigger.
- [x] **WORK-02**: Selecting Metadata or Fingerprint shows that stage's file queue with its manual trigger (metadata stays manual per the Phase 35 decision), backed by the existing endpoints.
- [x] **WORK-03**: The Analyze workspace shows three execution-lane cards — local / A1 / k8s — each with live capacity, and the k8s lane surfaces Kueue quota-wait vs. Inadmissible state.
- [x] **WORK-04**: Each in-flight Analyze file shows which lane (local/A1/k8s) it is running on and its windowed progress. (In-flight windowed progress reads the read-only mid-flight signal delivered by PROG-03 / Phase 57.1; completed files show full window coverage from the aggregate.)
- [x] **WORK-05**: Stage workspaces refresh live via the existing stats-poll pattern (no manual reload to see progress).

### Identify workspaces (IDENT)

- [x] **IDENT-01**: The Track-ID workspace shows each file's **existing** identity signals — audfprint + Panako fingerprint match/score and rapidfuzz tracklist-match confidence — surfaced as match state and confidence. (Re-scoped 2026-06-29 from the prototype's "AcoustID→MusicBrainz" label: that lookup backend does not exist, so building it is out of this presentation-only milestone — deferred to IDENT-03 below. Confirmed by research: `grep -ri 'acoustid|musicbrainz' src/phaze` is empty.)
- [x] **IDENT-02**: The Tracklist workspace presents the Search→Scrape→Match sub-chain inline as a visible 3-step with per-set match progress, triggerable from one surface.

### Review & Apply (REVIEW)

- [x] **REVIEW-01**: Rename/Path, Tag-write, and Move-files each present pending changes as a before→after diff with per-file Approve / Edit / Skip.
- [x] **REVIEW-02**: Each of those queues offers a bulk "approve all high-confidence" action gated by a confidence threshold.
- [x] **REVIEW-03**: Dedupe presents duplicate groups with keeper-selection (others archived) and a bulk auto-keep-highest-quality action.
- [x] **REVIEW-04**: Cue-sheet generation is reviewable with a preview and approve, gated on a matched tracklist.
- [x] **REVIEW-05**: Every applied change (rename, tag-write, move, dedupe) is recorded in the audit log and is reversible.

### Full record, command palette & agents (RECORD)

- [ ] **RECORD-01**: Opening a file (from a row or ⌘K) shows a full per-file record: identity, metadata diff, windowed multi-lane analysis timeline, this file's pending approvals (inline-approvable), and history.
- [ ] **RECORD-02**: ⌘K opens a command palette searching files / tracklists / artists and offering quick commands (scan, jump to a stage or review queue).
- [ ] **RECORD-03**: The Agents page shows local and A1 as heartbeating agents and the k8s burst lane as an ephemeral, Job-based identity (liveness derived from in-flight Kueue workloads) rather than a perpetually-DEAD agent — carrying v6.0 KDEPLOY-04's intent into the new UI.
- [ ] **RECORD-04**: When no files exist, a first-run empty state guides the operator to point phaze at a directory and shows live scan progress.

### Polish & cutover (CUT)

- [ ] **CUT-01**: The redesigned UI meets baseline accessibility — keyboard navigation for the rail and ⌘K, visible focus states, a skip link, and ARIA on the DAG — at parity with or better than today.
- [ ] **CUT-02**: Dead templates, routers, and partials from the old tabbed UI are removed once superseded (no orphaned dead code).
- [ ] **CUT-03**: User-facing docs and the per-service README are updated to describe the new information architecture.
- [ ] **CUT-04**: The shell degrades reasonably at narrow widths (the rail collapses to icons) for the single-user desktop tool.

## Future Requirements (deferred)

- **RECORD-05**: Light-theme gets a full first-class C3 treatment (v7.x — dark is primary for v7.0).
- **SHELL-06**: Touch-input support for the three-column shell down to tablet form factor (iPad is the smallest target — **no phone UI**): tap targets, touch-friendly DAG rail + ⌘K, and the approve/edit/skip review gate usable without a pointer. Excludes phone layouts; desktop pointer remains primary.
- **REVIEW-06**: Per-stage configurable confidence thresholds + override UI for "approve all high-confidence" (v7.0 ships a sensible fixed threshold).
- **WORK-06**: Pipeline admission-state cards driven by `cloud_phase` (the v6.0 deferred KROUTE-06) surfaced as Analyze-lane sub-states.
- **IDENT-03**: AcoustID acoustic-fingerprint lookup + MusicBrainz recording resolution as a new identity backend, then surfaced in the Track-ID workspace (a future milestone — requires net-new backend, out of v7.0's presentation-only scope; IDENT-01 ships the existing fingerprint + tracklist signals instead).

## Out of Scope

- **Backend/behavior changes** — v7.0 is an IA + presentation rewrite over existing routers/services; analysis, identify, proposal, and execution logic are unchanged.
- **Replacing the server-rendered stack** — no SPA / React / build pipeline; stays Jinja2 + HTMX + Tailwind + Alpine.
- **New visual identity** — C3 evolves the existing Jura/blue/wave-logo/dark language; it is not a brand replacement.
- **New pipeline capabilities** — every stage in the rail maps to an existing capability; no new analysis/identify features are added by this milestone.
- **Changing v6.0 cloud/k8s routing** — v7.0 visualizes the local/A1/k8s targets; it does not alter how routing decisions are made.

## Traceability

Each v7.0 requirement maps to exactly one phase. **Coverage: 28/28 — no orphans, no duplicates.** ROADMAP.md (created 2026-06-29) carries the per-phase goal, dependency order (57→57.1→58→59→60→61→62), and 2-5 success criteria for each phase. **PROG-01..03 / Phase 57.1 added 2026-06-29** — the scoped backend exception that makes WORK-04's in-flight progress real (see Design spine note). Note: **IDENT-01 was re-scoped 2026-06-29** to surface only the existing audfprint+Panako fingerprint + rapidfuzz tracklist signals — the prototype's AcoustID→MusicBrainz label is dropped (that backend does not exist; building it is out of this presentation-only milestone, deferred to IDENT-03).

| Requirement | Phase | Status |
|-------------|-------|--------|
| SHELL-01 | Phase 57 — Shell & DAG rail | Done |
| SHELL-02 | Phase 57 — Shell & DAG rail | Done |
| SHELL-03 | Phase 57 — Shell & DAG rail | Done |
| SHELL-04 | Phase 57 — Shell & DAG rail | Done |
| SHELL-05 | Phase 57 — Shell & DAG rail | Done |
| PROG-01 | Phase 57.1 — Incremental window persistence & live analyze progress signal | Done |
| PROG-02 | Phase 57.1 — Incremental window persistence & live analyze progress signal | Done |
| PROG-03 | Phase 57.1 — Incremental window persistence & live analyze progress signal | Done |
| WORK-01 | Phase 58 — Enrich + Analyze workspaces | Done |
| WORK-02 | Phase 58 — Enrich + Analyze workspaces | Done |
| WORK-03 | Phase 58 — Enrich + Analyze workspaces | Done |
| WORK-04 | Phase 58 — Enrich + Analyze workspaces | Done |
| WORK-05 | Phase 58 — Enrich + Analyze workspaces | Done |
| IDENT-01 | Phase 59 — Identify workspaces | Planned |
| IDENT-02 | Phase 59 — Identify workspaces | Planned |
| REVIEW-01 | Phase 60 — Review & Apply | Planned |
| REVIEW-02 | Phase 60 — Review & Apply | Planned |
| REVIEW-03 | Phase 60 — Review & Apply | Planned |
| REVIEW-04 | Phase 60 — Review & Apply | Planned |
| REVIEW-05 | Phase 60 — Review & Apply | Planned |
| RECORD-01 | Phase 61 — Full record + ⌘K + Agents | Planned |
| RECORD-02 | Phase 61 — Full record + ⌘K + Agents | Planned |
| RECORD-03 | Phase 61 — Full record + ⌘K + Agents | Planned |
| RECORD-04 | Phase 61 — Full record + ⌘K + Agents | Planned |
| CUT-01 | Phase 62 — Polish & cutover | Planned |
| CUT-02 | Phase 62 — Polish & cutover | Planned |
| CUT-03 | Phase 62 — Polish & cutover | Planned |
| CUT-04 | Phase 62 — Polish & cutover | Planned |
| RECORD-05 | Future (deferred — v7.x) | Deferred |
| SHELL-06 | Future (deferred — v7.x) | Deferred |
| REVIEW-06 | Future (deferred — v7.0 ships fixed threshold) | Deferred |
| WORK-06 | Future (deferred — future milestone) | Deferred |
| IDENT-03 | Future (deferred — future milestone, net-new backend) | Deferred |
