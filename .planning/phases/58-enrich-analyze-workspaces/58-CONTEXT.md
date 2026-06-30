# Phase 58: Enrich + Analyze workspaces - Context

**Gathered:** 2026-06-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Replace Phase 57's **bridged** stage content (D-01) with the four redesigned **Enrich + Analyze stage workspaces** — Discover · Metadata · Fingerprint · Analyze — rendered as fragments into the locked `#stage-workspace` swap target. Each workspace surfaces its existing queue/state and its existing manual trigger; the Analyze workspace adds the three execution-lane cards (local / A1 / k8s) with live capacity and the k8s Kueue quota-wait vs. Inadmissible distinction, plus a per-file table showing each file's lane and windowed progress. Delivers WORK-01..05.

**Hard constraint: Phase 58 makes NO backend behavior change.** Phase 58 is an IA/template rewrite over the *existing* routers/services/endpoints. It consumes the existing `/pipeline/stats` 5s poll (fanned out via `hx-swap-oob`, no second poll loop), the existing `$store.pipeline` keys, the existing trigger endpoints, and the existing analysis row/window data — it does not add new query paths, payloads, or stage semantics.

**Upstream dependency — Phase 57.1 (`depends_on`).** WORK-04's *in-flight* windowed-progress signal does not exist in the current backend (window rows are written only at completion). The one backend change v7.0 needs for this is isolated in **Phase 57.1** (PROG-01..03: incremental window persistence + read-only mid-flight signal), which must ship before Phase 58. Phase 58 only *reads* that read-only signal — see D-04. This keeps Phase 58 presentation-only while still delivering real in-flight progress.

**The 58-UI-SPEC.md is approved and locks** spacing, color, typography, copywriting, and the four workspace visual patterns (workspace scaffold, file table, 3 Analyze lane cards, per-file lane badge + windowed-progress cell). This CONTEXT does NOT re-litigate the UI-SPEC — it captures the data-wiring and scope-edge decisions the UI-SPEC deliberately left to planning, plus two reconciliations the discussion produced against the approved UI-SPEC.

**Explicitly NOT this phase:** the rich per-file record slide-in, ⌘K palette, Agents rebuild (all **Phase 61**); Identify workspaces (**Phase 59**); Review & Apply (**Phase 60**); a11y depth, narrow-rail collapse, dead-template removal/CUT-02 (**Phase 62**).
</domain>

<decisions>
## Implementation Decisions

### Metadata / Fingerprint trigger granularity (WORK-02)
- **D-01: ALL-only triggers — honor "no backend behavior change."** Ship only `EXTRACT ALL` / `FINGERPRINT ALL`, wired verbatim to the existing UI endpoints `POST /pipeline/extract-metadata` and `POST /pipeline/fingerprint` (`src/phaze/routers/pipeline.py:959`, `:1045`). Those endpoints enqueue **all** metadata-/fingerprint-pending files via `get_metadata_pending_files` (no file-id subset param). Zero backend change.
- **D-02: Drop `EXTRACT SELECTED` and any per-file row-selection/checkbox model from Phase 58.** The approved 58-UI-SPEC shows an `EXTRACT SELECTED` button (Metadata workspace) — that affordance is **consciously cut** for this phase because it would require an extended/new endpoint (new query path + payload validation), bending the milestone's no-backend-change rule. **Planner reconciliation required:** add a one-line note to 58-UI-SPEC.md recording that `EXTRACT SELECTED` + row-checkboxes are deferred (see Deferred Ideas). There is therefore **no row-selection state** anywhere in Phase 58.

### Analyze workspace file list (WORK-04)
- **D-03: One table of ALL in-stage files, below the three lane cards.** The Analyze workspace renders a single file table covering every file in the Analyze stage (queued · running · awaiting-cloud · done), NOT only in-flight files and NOT per-lane mini-tables. Each row carries a **status column + a local/A1/k8s lane badge** (UI-SPEC Pattern 4). This keeps the single-poll OOB fanout simple (one table fragment) while giving the full operational picture.
- **D-04 (REVISED 2026-06-29): Windowed progress = a simple %/windows-done bar reading the mid-flight signal from Phase 57.1.** Each row shows a compact progress bar / `N/M windows` indicator. **Research finding (`58-RESEARCH.md`):** `analysis_window` rows + `fine_windows_analyzed/total` were written *atomically at completion* — in-flight files had **no** window rows, so a live in-flight bar was impossible without a backend change. **The user approved expanding scope** (2026-06-29) to make in-flight progress real, **structured as a separate upstream phase: Phase 57.1** (PROG-01..03) adds incremental window persistence + a read-only mid-flight signal. **Phase 58 therefore depends on 57.1** and simply *reads* that read-only signal (`fine_windows_analyzed/total` on the in-progress row) for the in-flight bar; completed files show full coverage from the aggregate. **Phase 58 itself makes NO backend change** — all backend work lives in 57.1. No inline BPM sparkline and no multi-lane timeline in Phase 58 (that is the Phase 61 record).

### Analyze lane cards — degraded/offline states (WORK-03)
- **D-05: Always render all three lane cards; label the unavailable state.** local / A1 / k8s cards are **always present** for a stable, predictable layout that shows the full routing topology. A lane that is down renders **greyed with an explicit state label and 0 capacity**: `offline` (A1 has no online compute agent) or `not configured` (no k8s/Kueue cluster set up in this homelab). Do NOT hide lanes that are merely down or unconfigured. (The per-file "awaiting cloud — no compute agent online" copy already locked in the UI-SPEC is the file-level companion to this card-level state.)
  - *Discretion left to planner:* whether to visually/copy-distinguish `offline` (configured-but-down, recoverable) from `not configured` (never set up) — the user chose "always show 3, label the state"; the finer down-vs-unconfigured copy split is acceptable but not required.

### File-row click wiring (UI-SPEC R-1)
- **D-06: Inert-but-present rows (strict R-1).** File rows ship with the stable target id/markup and hover affordance the UI-SPEC's R-1 requires, but the **click is unbound** in Phase 58 — no selected-state, no placeholder pane, no record fetch. The actual row-click → per-file record interaction is wired wholly in **Phase 61** (RECORD-01). This guarantees no half-built record/pane work leaks into Phase 58 and respects Phase 57's deferral of the record slide-in.

### Claude's Discretion
- Discover "recent scans" surface (WORK-01): reuse the existing `src/phaze/templates/pipeline/partials/recent_scans_table.html` partial, restyled to the C3 workspace-table pattern as needed; row count/fields per the existing scan data (`pipeline_scans.py`). Not separately discussed — sensible default is reuse + restyle.
- Reuse of the v6.0 cloud-state partials (`admission_state_card.html`, `analyzing_cloud_card.html`, `awaiting_cloud_card.html`) for the k8s/A1 lane sub-states vs. fresh markup — pick whichever keeps the lane-card capacity contract cleanest (UI-SPEC already preserves their color/role/copy contracts).
- Exact OOB id additions for the new workspace fragments (must ride the single `/pipeline/stats` poll + `oob_counts` gate from Phase 57 — do not add a second poll loop).
- Empty-state and trigger-response wiring detail (UI-SPEC already locks the empty-state copy per workspace).
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### This phase's contracts
- `.planning/phases/58-enrich-analyze-workspaces/58-UI-SPEC.md` — **approved** visual + interaction contract (spacing/color/type/copy + 4 workspace patterns + lane cards + Kueue states + single-poll discipline). NOTE D-02: its `EXTRACT SELECTED` affordance is cut this phase — add the reconciliation note.
- `.planning/REQUIREMENTS.md` § "Enrich & Analyze workspaces (WORK)" — WORK-01..05 (the 5 requirements this phase delivers). WORK-06 (cloud_phase admission-state sub-states) is **deferred** — not this phase.
- `.planning/ROADMAP.md` § "Phase 58: Enrich + Analyze workspaces" + the v7.0 milestone Notes (no-backend-change; dependency-strict 57→62; stack version bumps already done in 57).

### Design & IA (authoritative, inherited from v7.0)
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` — v7.0 Hybrid Console IA, stage workspaces (§6), C3 aesthetic (§4), reuse-routers constraint (§11).
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` — canonical interactive reference for every stage workspace + Analyze lane cards. Match layout/behavior.

### Inherited foundation (Phase 57 — do not re-litigate)
- `.planning/phases/57-shell-dag-rail/57-CONTEXT.md` — the locked cross-cutting contracts Phase 58 builds on: `#stage-workspace` stable swap-target, fragment-only stage responses, single `/pipeline/stats` 5s poll + `hx-swap-oob` + `oob_counts` gate, `$store.pipeline` consumed-not-redefined, `/s/<stage>` URL scheme, theme/brand preservation. D-01 (bridge) is the thing Phase 58 replaces for these four stages.
- `.planning/phases/57-shell-dag-rail/57-UI-SPEC.md` — the **baseline design system** the 58-UI-SPEC inherits verbatim (spacing/type/color/chrome tokens).
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Trigger endpoints (D-01):** `POST /pipeline/extract-metadata` (`src/phaze/routers/pipeline.py:959`) and `POST /pipeline/fingerprint` (`:1045`) — HTMX endpoints returning `pipeline/partials/trigger_response.html`; enqueue ALL pending files via `get_metadata_pending_files`. Wire the workspace ALL-buttons to these verbatim.
- **`recent_scans_table.html`** (`src/phaze/templates/pipeline/partials/`) + `pipeline_scans.py` — existing Discover recent-scans surface (WORK-01). Reuse/restyle.
- **v6.0 cloud-lane partials:** `admission_state_card.html`, `analyzing_cloud_card.html`, `awaiting_cloud_card.html` — existing Kueue/A1 state cards (capacity, quota-wait vs. Inadmissible, awaiting-cloud copy). Source for the Analyze lane-card sub-states (WORK-03).
- **Per-file progress signal (`fine_windows_analyzed`/`fine_windows_total`):** the data source for the windowed-progress bar (D-04). For *completed* files this is the existing aggregate; for *in-flight* files it is the read-only mid-flight signal **Phase 57.1 (PROG-03)** delivers. Phase 58 only reads it — no new schema/query in this phase.
- **`/pipeline/stats` 5s poll + `$store.pipeline`** (`pipeline.py:549`, `oob_counts=True` at `:606`; `base.html:106`; `stats_bar.html`): the single poll all four workspaces ride for live refresh (WORK-05). Do not add a second loop.

### Established Patterns
- **Fragment-vs-full-page rendering** (Phase 57 D-01): each stage route returns just its content block as a fragment on HTMX requests; full shell on direct nav. Phase 58 swaps each bridged fragment for its redesigned workspace one stage at a time, keeping the app usable at every commit.
- **OOB `hx-swap-oob` fanout** off the one `/pipeline/stats` response behind the `oob_counts` gate — the only live-update mechanism (WORK-05).

### Integration Points
- The four redesigned workspace fragments replace the bridged legacy content for Discover/Metadata/Fingerprint/Analyze inside `#stage-workspace`; rail nodes + `/s/<stage>` routing already exist from Phase 57.
- Dead-template AST guard (seeded Phase 57): removing the now-superseded bridged templates is **CUT-02 / Phase 62**, not Phase 58 — Phase 58 leaves the guard green by superseding-in-place, not deleting.
</code_context>

<specifics>
## Specific Ideas

- The **prototype.html is the canonical visual/behavioral target** for each workspace — match it rather than reinterpreting.
- Stay strictly within the no-backend-change rule: when a UI-SPEC affordance (e.g. `EXTRACT SELECTED`) would require a backend change, cut it from this phase and defer rather than bend the rule (the D-01/D-02 precedent).
</specifics>

<deferred>
## Deferred Ideas

- **Per-file selection for Metadata/Fingerprint** (`EXTRACT SELECTED` + row-checkboxes + subset-enqueue endpoint) — cut from Phase 58 per D-02; a future enhancement that needs a backend endpoint change. Surfaced from the approved UI-SPEC, parked here so it isn't silently lost.
- **Inline BPM sparkline / multi-lane windowed timeline per file** — the rich windowed-analysis visualization lands in the **Phase 61** full per-file record (RECORD-01); Phase 58 ships only the simple %/windows-done bar (D-04).
- **Row-click → rich per-file record/pane** — wired in **Phase 61** (RECORD-01); Phase 58 ships inert-but-present rows only (D-06).
- **WORK-06** (cloud_phase admission-state cards as Analyze-lane sub-states) — explicitly a future/deferred requirement, not Phase 58.

None of these arose as scope creep — they are the planned downstream phases or conscious no-backend-change cuts.
</deferred>

---

*Phase: 58-enrich-analyze-workspaces*
*Context gathered: 2026-06-29*
