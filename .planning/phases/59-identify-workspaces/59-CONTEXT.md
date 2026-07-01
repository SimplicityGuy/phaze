# Phase 59: Identify workspaces - Context

**Gathered:** 2026-06-30
**Status:** Ready for planning

<domain>
## Phase Boundary

Replace the two `_STAGE_PLACEHOLDER` stages in the v7.0 shell (`trackid`, `tracklist` in `src/phaze/routers/shell.py:69-70`) with their two redesigned **Identify stage workspaces**, rendered as fragments into the locked `#stage-workspace` swap target:

- **Track-ID workspace** — surfaces each file's **existing** identity signals: per-engine fingerprint **state** (audfprint + Panako) and rapidfuzz **tracklist-match confidence**. (IDENT-01)
- **Tracklist workspace** — presents the Search→Scrape→Match sub-chain inline as a visible 3-step with per-set match progress, triggerable from one surface. (IDENT-02)

Delivers IDENT-01 and IDENT-02. This is the same presentation-only workspace pattern Phase 58 established (header + counts + action + table/cards; fragment into `#stage-workspace`; single `/pipeline/stats` 5s poll + `hx-swap-oob`; `/s/<stage>` routing).

**Hard constraint: Phase 59 makes NO backend behavior change.** IA/template rewrite over the *existing* routers/services/endpoints, models, and task stages. No new query paths, payloads, identity backend, or match/scoring logic. (Milestone-wide rule; D-01/D-02 precedent from Phase 58.)

**IDENT-01 re-scope (carried forward, 2026-06-29):** the prototype's "AcoustID→MusicBrainz" label is dropped — that lookup backend does not exist (`grep -ri 'acoustid|musicbrainz' src/phaze` is empty) and building it would violate the no-backend-change boundary. Deferred to **IDENT-03** (future milestone). This phase ships the existing fingerprint + tracklist signals only.

**Explicitly NOT this phase:** row-click → rich per-file record/pane (Phase 61, RECORD-01 — rows are inert-but-present here, per Phase 58 D-06); Review & Apply (Phase 60); AcoustID/MusicBrainz identity backend (IDENT-03, deferred); a11y depth / narrow-rail collapse / dead-template removal (Phase 62).

**UI contract:** No 59-UI-SPEC.md exists yet. This CONTEXT captures data-wiring and scope-edge decisions; the visual contract (spacing/color/type/copy + workspace visual patterns) is produced next by `/gsd:ui-phase 59`, inheriting the 57/58 design system verbatim.
</domain>

<decisions>
## Implementation Decisions

### Track-ID — fingerprint signal (IDENT-01)
- **D-01: Per-engine status badges, no invented score.** Each file row shows two badges — `audfprint` and `Panako` — each rendering the file's `FingerprintResult.status` for that engine: **done** (`completed`/`success`), **failed**, or **pending** (no row). Read straight from the existing `fingerprint_results` table. There is **no numeric match score** in the data model, so none is fabricated.
- **D-02: Requirement-wording reconciliation.** IDENT-01 / the roadmap say "fingerprint **match/score**." Verified at discuss time: `FingerprintResult` persists only `engine` + `status` + `error_message` (`src/phaze/models/fingerprint.py`) — fingerprints are *ingested into* the external audfprint/Panako indexes; no per-file match-query or score is stored. So "match/score" resolves to per-engine **match state** (the badges in D-01) plus tracklist **confidence** (D-04). **Planner note:** treat "score" as "state"; do not build a fingerprint scoring/match-query path (that is the deferred IDENT-03 territory).

### Track-ID — workspace layout (IDENT-01)
- **D-03: One combined per-file table** (not two sub-sections). Single table over all in-stage files, columns: file · `audfprint` badge · `Panako` badge · tracklist match state · tracklist confidence. Mirrors Phase 58 D-03 ("one table of all in-stage files") — keeps the single-poll OOB fanout to one fragment.
- **D-04: Tracklist confidence shown = the linked tracklist's, fallback to best candidate.** Each row shows the **linked/auto-linked** `Tracklist.match_confidence` for that file; if no tracklist is linked yet, show the **highest** `match_confidence` among candidate tracklists (the existing `list_tracklists` query already orders `match_confidence desc nulls_last`). Reflects the actual identity-decision state, not an implied match.

### Tracklist — 3-step rendering (IDENT-02)
- **D-05: Three sequential step cards** — Search · Scrape · Match — each showing its existing pending/done count + state, following the Phase 58 Analyze **lane-card visual pattern**. The three backend task stages already exist (`scan_search` / `scrape` / `match`, with `get_*_pending_*` count helpers — see `src/phaze/routers/pipeline.py:89-91` and the `get_match_pending_tracklists` / `get_scrape_pending_tracklists` service fns), so the per-step counts map cleanly to cards. Not a horizontal stepper (new component, not in the design system).
- **D-06: Per-step ALL trigger buttons; "one surface" = co-located, NOT a chain orchestrator.** Each step card carries its own ALL trigger (SEARCH ALL / SCRAPE ALL / MATCH ALL), wired **verbatim** to the existing per-step endpoints (`POST /pipeline/search-tracklists` and the scrape/match equivalents). "Triggerable from one surface" is satisfied by all three triggers living on the one Tracklist workspace — there is **no** single "run chain" button, because no backend endpoint runs all three and adding one would break the no-backend-change rule (Phase 58 ALL-only precedent).

### Tracklist — per-set match progress (IDENT-02)
- **D-07: Per-set progress = track-level coverage.** Each set (file) shows **N/M tracks confident** within its linked tracklist, derived from `TracklistTrack.confidence` over the tracklist's tracks. This is the substantive "how well is this set identified?" signal and the per-track data already exists. (Chain-step position is already conveyed by the aggregate step cards in D-05, so it is not duplicated per-row.)
- **D-08: Per-set table below the 3 step cards.** Step cards (aggregate) on top, a table of sets/files below carrying each set's match progress (D-07) + state. Parallels Phase 58 Analyze (lane cards + file table) — and gives IDENT-02's "per-set match progress" a concrete home.

### Inherited / carried-forward (do not re-litigate)
- **No backend behavior change** — IA/template rewrite only (milestone rule).
- **Inert-but-present rows** — file rows ship stable target id/markup + hover affordance but the click is **unbound** in Phase 59; row-click → rich record is Phase 61 (RECORD-01), per Phase 58 D-06.
- **Single-poll discipline** — both workspaces ride the existing `/pipeline/stats` 5s poll + `hx-swap-oob` behind the `oob_counts` gate; do **not** add a second poll loop (Phase 57 contract).
- **Supersede-in-place** — leave the dead-template AST guard green by superseding the placeholders, not deleting legacy templates (CUT-02 is Phase 62).

### Claude's Discretion
- Exact OOB id additions for the two new workspace fragments (must ride the single `/pipeline/stats` poll + `oob_counts` gate; no second loop).
- Whether to reuse the existing `tracklists/partials/` templates (`tracklist_list.html`, `tracklist_card.html`, `track_detail.html`, `fingerprint_track_detail.html`, `trigger_tracklist_response.html`) restyled to the workspace pattern, vs. fresh fragments — pick whichever keeps the workspace-table/card contract cleanest.
- Empty-state and trigger-response wiring detail (the `/gsd:ui-phase 59` UI-SPEC will lock empty-state copy; mirror Phase 58's locked-copy approach).
- Whether failed-engine `error_message` is surfaced (e.g. tooltip) on Track-ID badges — optional diagnostic nicety, not required by D-01.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### This phase's contracts
- `.planning/ROADMAP.md` § "Phase 59: Identify workspaces" — goal, success criteria, and the IDENT-01 re-scope note (verify `models/fingerprint.py` at plan time — done; see D-02).
- `.planning/REQUIREMENTS.md` § "Identify (IDENT)" — IDENT-01, IDENT-02 (the 2 requirements this phase delivers). IDENT-03 (AcoustID/MusicBrainz backend) is **deferred** — not this phase.

### Pattern to reuse (Phase 58 — the immediate predecessor)
- `.planning/phases/58-enrich-analyze-workspaces/58-CONTEXT.md` — the workspace data-wiring/scope-edge decisions Phase 59 mirrors: one-table-of-all-in-stage-files (D-03→our D-03), lane-card pattern (→our D-05), ALL-only triggers / no-backend-change cuts (D-01/D-02→our D-06), inert-but-present rows (D-06→carried forward).
- `.planning/phases/58-enrich-analyze-workspaces/58-UI-SPEC.md` — the approved visual contract for the workspace scaffold, file table, and lane cards the Phase 59 UI-SPEC should inherit.

### Inherited foundation (Phase 57 — do not re-litigate)
- `.planning/phases/57-shell-dag-rail/57-CONTEXT.md` — locked cross-cutting contracts: `#stage-workspace` swap target, fragment-only stage responses, single `/pipeline/stats` 5s poll + `hx-swap-oob` + `oob_counts` gate, `$store.pipeline` consumed-not-redefined, `/s/<stage>` URL scheme (with `stage` never spliced into a template path — T-57-01), theme/brand preservation.
- `.planning/phases/57-shell-dag-rail/57-UI-SPEC.md` — baseline design system (spacing/type/color/chrome tokens) the 59 UI-SPEC inherits verbatim.

### Design & IA (authoritative, inherited from v7.0)
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` — v7.0 Hybrid Console IA, stage workspaces (§6), C3 aesthetic (§4), reuse-routers constraint (§11).
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` — canonical interactive reference for the stage workspaces. Match layout/behavior for the Track-ID + Tracklist workspaces (using the re-scoped fingerprint+tracklist signals, NOT the prototype's AcoustID/MusicBrainz label).

### Data model + endpoints verified at discuss time (the data this phase surfaces)
- `src/phaze/models/fingerprint.py` — `FingerprintResult` = `file_id` + `engine` + `status` + `error_message` only. **No score column** (basis for D-01/D-02).
- `src/phaze/models/tracklist.py` — `Tracklist.match_confidence` (int, rapidfuzz, per file/candidate), `Tracklist.file_id`/`auto_linked`/`status`/`source`; `TracklistVersion`; `TracklistTrack.confidence` (float, per track — basis for D-07).
- `src/phaze/routers/shell.py:69-70` — the `trackid` / `tracklist` `_STAGE_PLACEHOLDER` entries this phase replaces (supersede-in-place).
- `src/phaze/routers/pipeline.py` — 3-step task-stage map (`scan_search`/`scrape`/`match`, ~`:89-91`), `get_match_pending_tracklists` / `get_scrape_pending_tracklists` imports, `POST /pipeline/search-tracklists` bulk trigger (~`:1130`).
- `src/phaze/routers/tracklists.py` — `_get_tracklist_stats` (total/matched/unmatched/proposed), `list_tracklists` (orders `match_confidence desc nulls_last`), `link_tracklist`, `get_tracks`.
- `src/phaze/services/fingerprint.py` — per-engine ingest result + count helpers (`status` semantics: success/failed/completed).
- `src/phaze/tasks/tracklist.py` — `search_tracklist` / `scrape_and_store_tracklist` (the chain task bodies; confirms search+scrape+match are distinct stages with real per-step state).
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Tracklist templates** (`src/phaze/templates/tracklists/partials/`): `tracklist_list.html`, `tracklist_card.html`, `track_detail.html`, `fingerprint_track_detail.html`, and `pipeline/partials/trigger_tracklist_response.html` — restyle to the workspace table/card pattern (Claude's discretion).
- **Per-step count helpers**: `get_match_pending_tracklists`, `get_scrape_pending_tracklists` (+ the `scan_search`/`scrape`/`match` stage map in `pipeline.py`) feed the three step-card counts (D-05).
- **Trigger endpoints**: `POST /pipeline/search-tracklists` (bulk Search ALL) and the scrape/match equivalents — wire the per-step ALL buttons verbatim (D-06).
- **Tracklist stats**: `_get_tracklist_stats` (total/matched/unmatched/proposed) for workspace header counts.
- **Fingerprint read path**: `fingerprint_results` (`FingerprintResult`) per `(file_id, engine)` for the Track-ID badges (D-01); `services/fingerprint.py` count helpers for header counts.
- **`/pipeline/stats` 5s poll + `$store.pipeline`** — the single live-refresh mechanism both workspaces ride (no second loop).

### Established Patterns
- **Fragment-vs-full-page rendering** (Phase 57): each stage route returns its content block as a fragment on HTMX rail swaps, full shell on direct nav. Phase 59 swaps each placeholder for its workspace one stage at a time, app usable at every commit.
- **OOB `hx-swap-oob` fanout** off the one `/pipeline/stats` response behind the `oob_counts` gate — the only live-update mechanism.
- **Analyze lane-card pattern** (Phase 58) — the visual template the Tracklist 3 step cards follow (D-05).
- **One table of all in-stage files** (Phase 58 D-03) — the Track-ID combined table follows it (D-03).

### Integration Points
- The two redesigned workspace fragments replace the `_STAGE_PLACEHOLDER` values for `trackid` / `tracklist` in `STAGE_PARTIALS` (`shell.py`), with their `_render_stage` DB-context branches (mirror the metadata/fingerprint branches added in Phase 58). Rail nodes + `/s/<stage>` routing already exist from Phase 57.
- Leave the dead-template AST guard green by superseding-in-place, not deleting legacy templates (removal = CUT-02 / Phase 62).
</code_context>

<specifics>
## Specific Ideas

- **prototype.html is the canonical visual/behavioral target** for the Identify workspaces — match it, but render the **re-scoped** fingerprint + tracklist signals (not the prototype's AcoustID/MusicBrainz label).
- Stay strictly within the no-backend-change rule: when an affordance would need a backend change (e.g. a fingerprint score, a single chain-orchestration trigger, or per-file subset enqueue), **cut and defer** rather than bend the rule (D-02/D-06; Phase 58 D-01/D-02 precedent).
</specifics>

<deferred>
## Deferred Ideas

- **AcoustID acoustic-fingerprint lookup + MusicBrainz recording resolution** (a real identity backend, then surfaced in Track-ID) — **IDENT-03**, a future milestone. Out of v7.0's presentation-only scope; the reason "fingerprint score" can't be shown today (D-02).
- **Single "run chain" trigger** for Search→Scrape→Match — needs backend orchestration; cut per D-06. A future enhancement if a chain endpoint is ever added.
- **Row-click → rich per-file record/pane** for Track-ID rows — Phase 61 (RECORD-01); Phase 59 ships inert-but-present rows only.
- **Numeric fingerprint match scoring** — depends on the IDENT-03 match-query backend; not buildable presentation-only.

None of these arose as scope creep — they are deferred requirements or conscious no-backend-change cuts.
</deferred>

---

*Phase: 59-identify-workspaces*
*Context gathered: 2026-06-30*
