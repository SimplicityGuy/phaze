# Phase 60: Review & Apply - Context

**Gathered:** 2026-06-30
**Status:** Ready for planning

<domain>
## Phase Boundary

Replace the five (→ six, see D-01) `_STAGE_PLACEHOLDER` stages in the v7.0 shell (`propose`, `rename`, `tagwrite`, `move`, `dedupe`, `cue` in `src/phaze/routers/shell.py:88-93`) with their redesigned **Review & Apply gate** workspaces, rendered as fragments into the locked `#stage-workspace` swap target. This collapses five legacy tabs (Proposals/Preview/Tags/Cue/Duplicates) into one gate with one consistent interaction:

- **Rename/Path · Tag-write · Move-files** — each presents pending changes as a **before→after diff** with per-file **Approve / Edit / Skip**, over the three existing data sources, and a bulk **"approve all high-confidence"** action. (REVIEW-01, REVIEW-02)
- **Dedupe** — duplicate groups with radio keeper-selection (others archived) + a bulk auto-keep-highest-quality action. (REVIEW-03)
- **Cue** — generated `.cue` preview + approve, gated on a matched tracklist. (REVIEW-04)
- **Audit + reversible** — every applied change (rename/tag/move/dedupe) writes its existing audit row and is reversible. (REVIEW-05)

Delivers REVIEW-01..05. Same presentation-only workspace pattern Phases 58/59 established (header + counts + action + table/cards; fragment into `#stage-workspace`; single `/pipeline/stats` 5s poll + `hx-swap-oob`; `/s/<stage>` routing).

**SCOPE BOUNDARY — the key difference from Phases 58/59.** Phase 60 is **NOT strictly no-backend-change.** The milestone rule ("v7.0 is an IA + presentation rewrite; analysis, identify, proposal, and execution **LOGIC** are unchanged" — REQUIREMENTS.md line 82) still holds for the *logic*, but Phase 60 is **explicitly sanctioned to add two thin UI-serving endpoints** the roadmap's REVIEW-02 note demands:
1. A **server-evaluated bulk-approve predicate** endpoint (D-02) — required by REVIEW-02, which the existing client-id-list `/proposals/bulk` cannot satisfy.
2. An **inline Edit PATCH** endpoint (D-05) — required by REVIEW-01's per-file "Edit" affordance, which has no existing endpoint.

Both are **thin routes over unchanged apply/generation logic** (they call the existing status-update / proposal-write paths). Prior phases cut anything needing a new endpoint (58 D-02, 59 D-06); **Phase 60 does the opposite for exactly these two — do NOT cut them.** Everything else (dedupe, cue, audit, reversibility) rides existing endpoints with zero backend change.

**Explicitly NOT this phase:** row-click → rich per-file record/pane slide-in (Phase 61, RECORD-01 — Review rows carry in-row Approve/Edit/Skip controls, but the row-click→record interaction is Phase 61); ⌘K / Agents (Phase 61); a unified below-the-line Audit rail node (deferred, see Deferred); per-stage configurable thresholds + override UI (REVIEW-06, deferred — v7.0 ships fixed thresholds); a11y depth / narrow-rail collapse / dead-template removal (Phase 62, CUT-02).

**UI contract:** No 60-UI-SPEC.md exists yet. This CONTEXT captures data-wiring and scope-edge decisions; the visual contract (spacing/color/type/copy + the unified diff partial + keeper-select + cue-preview patterns) is produced next by `/gsd:ui-phase 60`, inheriting the 57/58/59 design system verbatim.
</domain>

<decisions>
## Implementation Decisions

### Propose node — fold into the Rename/Path gate (scope edge)
- **D-01: Light Propose workspace + Rename/Path review workspace, sharing one source.** The `propose` DAG node (SHELL-02, still a placeholder, owned by no prior phase's goal) is superseded with a **thin generation view** — the proposals list showing model + confidence + a **"Generate ALL"** trigger wired verbatim to the existing `generate_proposals` batch path (`src/phaze/routers/pipeline.py:_enqueue_proposal_jobs`, `POST` proposal-generation endpoint). The `rename` node is superseded with the **before→after review diff** over the **same `RenameProposal` rows**. Both placeholders become real fragments — nothing is left broken. `/proposals/` already `RedirectResponse("/s/propose")` (`proposals.py:129`, SHELL-05), so `/s/propose` is the canonical shell state for generation. **Planner note:** Propose (generate) and Rename/Path (review) are two rail nodes over one `RenameProposal` source, not two data sources.

### Bulk "approve all high-confidence" — server-evaluated predicate (REVIEW-02)
- **D-02: NEW server-predicate endpoint, `RenameProposal.confidence ≥ 0.9`.** Add a thin `POST` bulk-approve-high-confidence endpoint that **mirrors the `tracklists.py` `reject-low` pattern** (`src/phaze/routers/tracklists.py:632` — takes a `threshold`, the server itself queries+acts on the matching rows). At submit time the server **re-queries pending `RenameProposal` rows with `confidence ≥ 0.9`** and approves them — it **never** accepts a client-built `selectedRows`/`proposal_ids` id-list. This directly fixes the REVIEW-02 stale-bulk hazard that the existing `/proposals/bulk` (`proposals.py:309`, takes `proposal_ids: list[str]`) embodies. **Threshold = 0.9 fixed** (conservative for an irreplaceable archive; REVIEW-06 defers making it configurable). Applies to the Rename/Path and Move queues (both `RenameProposal.confidence`). Proposal-generation and execution **logic unchanged** — only a new UI-serving route + query.

### Tag-write bulk predicate — no-discrepancies = high-confidence (REVIEW-02)
- **D-03: Tag-write "high-confidence" = zero discrepancies.** Tag-write has **no confidence score** — it is computed per-file (`compute_proposed_tags` + `_build_comparison` in `src/phaze/routers/tags.py`, before=current metadata / after=proposed, with a `discrepancies` notion). The bulk "approve all high-confidence" for the Tag queue **server-re-queries pending tag-write files whose comparison has zero discrepancies** and writes them; **discrepancy rows stay per-file Approve/Edit/Skip only.** This is the natural analog to a confidence gate and keeps the same server-evaluated-predicate discipline as D-02 (no client id-list). The bulk endpoint's predicate is `action=tag-write, discrepancies==0`.

### Edit action — inline PATCH before approve (REVIEW-01)
- **D-05: Inline Edit is a new small PATCH endpoint updating the proposal.** SC#1's per-file **Edit** has no existing endpoint (only approve/reject/undo exist — `proposals.py:168/193/218`). Add a thin `PATCH` that **updates `proposed_filename` / `proposed_path`** (and re-derives the Move/Tag target from the edited value) **before** the operator approves. It **mirrors the existing status-PATCH pattern** and leaves proposal-**generation** logic untouched — it edits the persisted proposal row, it does not re-run the LLM. This is the **second sanctioned backend addition** (with D-02); do not cut it to "Skip + manual".

### Audit + reversibility surface (REVIEW-05)
- **D-04: Per-stage undo over existing endpoints + reuse the existing `/audit/` view.** Each apply already writes its audit row and is reversible via existing paths — **do not build new audit/undo logic**:
  - Rename/Move → `ExecutionLog` (`src/phaze/models/execution.py` — `proposal_id`, `operation`, `source_path`, `destination_path`, `sha256_verified`, `status`); execute via `execution.py:/execution/start`; reverse via the existing proposal `undo` (`proposals.py:218`).
  - Tag-write → `TagWriteLog` (`src/phaze/models/tag_write_log.py` — `before_tags`/`after_tags`/`discrepancies`/`status`).
  - Dedupe → `duplicates.py` `resolve` / `resolve-all` + `undo` / `undo-all` (`duplicates.py:144/199/169/233`).
  Each Review workspace surfaces its own **undo affordance** over these; the existing **`/audit/`** page (`execution.py:350`) is the unified log. **No new Audit rail node this phase** — the design's below-the-line "Audit log" node is deferred (see Deferred). "One audit row per apply" (SC#5) = assert exactly one `ExecutionLog`/`TagWriteLog`/dedupe-resolution row is written per applied change.

### Data sources for the unified diff (REVIEW-01, SC#1)
- **D-06: One Jinja diff partial over the existing sources — Rename & Move share `RenameProposal`.** Rename/Path (filename facet) and Move-files (destination-path/tree facet) both derive from **`RenameProposal`** (`proposed_filename` + `proposed_path`, `src/phaze/models/proposal.py`); `/preview/` already `RedirectResponse("/s/move")` (`preview.py:46`). Tag-write derives from the **computed** proposed-tags comparison (`tags.py`). So the "three data sources" = `RenameProposal` (rename facet) · `RenameProposal.proposed_path` (move facet) · computed tag comparison — surfaced through **one** before→after diff partial (struck-through current vs highlighted proposed). Dedupe (keeper-select) and Cue (preview) are distinct interaction shapes, not the diff partial.

### Dedupe & Cue (REVIEW-03, REVIEW-04)
- **D-07: Dedupe = existing duplicate-group resolve/keeper-select + auto-keep-highest-quality bulk.** Wire the keeper radio + archive-others to `duplicates.py` `resolve` (`:144`) and the bulk auto-keep to `resolve-all` (`:199`), with `undo`/`undo-all` for reversibility. Read groups via `list_duplicates` (`:79`) / `compare_group` (`:116`). Zero backend change.
- **D-08: Cue = existing preview + approve, gated on a matched tracklist.** Wire the cue workspace to `cue.py` (`_get_eligible_tracklist_query` gates on `status=="approved"` + EXECUTED file + ≥1 timestamped track; `_build_cue_tracks` builds the preview). Approve over the existing cue generation path. Zero backend change.

### Claude's Discretion
- Exact OOB id additions for the new workspace fragments (must ride the single `/pipeline/stats` 5s poll + `oob_counts` gate — no second loop; **and per the roadmap, the diff-list poll must OOB-update COUNTS ONLY — never re-render the operator's in-progress selection subtree**).
- Whether Rename/Path and Move-files are two separate rail-node workspaces over one `RenameProposal` source, or one workspace with a name/path toggle — pick whichever keeps the one diff partial cleanest (D-06 fixes the source; the surface split is discretion).
- Whether "Skip" maps to the existing `reject` status or a distinct skip status (existing `reject` is the sensible default).
- Empty-state and trigger-response copy (the `/gsd:ui-phase 60` UI-SPEC will lock it; mirror Phase 58/59's locked-copy approach).
- Whether to reuse/restyle the legacy partials (`proposals/partials/*`, `tags/partials/*`, `duplicates/*`, `cue/*`) into the workspace diff pattern vs. fresh fragments — pick whichever keeps the unified-diff contract cleanest (supersede-in-place; legacy templates stay until CUT-02).
- Surfacing of failed-apply `error_message` (ExecutionLog/TagWriteLog) on rows — optional diagnostic nicety, not required.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### This phase's contracts
- `.planning/ROADMAP.md` § "Phase 60: Review & Apply" — goal, 5 success criteria, and the **Notes** (REVIEW-02 stale-bulk fix, "pick a fixed server-side confidence threshold at plan time," the `reject-low` reference-value pointer, and the "OOB-update counts ONLY" poll constraint).
- `.planning/REQUIREMENTS.md` § "Review & Apply (REVIEW)" — REVIEW-01..05 (the 5 requirements this phase delivers). **REVIEW-06** (per-stage configurable thresholds + override UI) is **deferred** — v7.0 ships the fixed thresholds locked in D-02/D-03. Line 82 = the milestone "logic unchanged" rule (scoped to logic, not endpoints — see this phase's SCOPE BOUNDARY).

### Design & IA (authoritative, inherited from v7.0)
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` — §7 Review & Apply gate (the approved per-file-diff + bulk-high-conf model), §6 Propose node, §5 rail grouping, §4 C3 aesthetic, §11 reuse-routers constraint.
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` — canonical interactive reference for the Review & Apply diff/approve gate, keeper-select, and cue preview. Match layout/behavior.

### Pattern to reuse (Phases 58 & 59 — the immediate predecessors)
- `.planning/phases/59-identify-workspaces/59-CONTEXT.md` — the most recent workspace data-wiring/scope-edge precedent (supersede-in-place, single-poll OOB discipline, ALL-trigger wiring, inert-but-present rows for the record).
- `.planning/phases/58-enrich-analyze-workspaces/58-CONTEXT.md` + `58-UI-SPEC.md` — workspace scaffold + file-table + single-poll contract the 60 UI-SPEC inherits.

### Inherited foundation (Phase 57 — do not re-litigate)
- `.planning/phases/57-shell-dag-rail/57-CONTEXT.md` — locked cross-cutting contracts: `#stage-workspace` swap target, fragment-only stage responses, single `/pipeline/stats` 5s poll + `hx-swap-oob` + `oob_counts` gate, `$store.pipeline` consumed-not-redefined, `/s/<stage>` scheme (T-57-01: `stage` never spliced into a template path — use static literals in `STAGE_PARTIALS`), theme/brand preservation.
- `.planning/phases/57-shell-dag-rail/57-UI-SPEC.md` — baseline design system (spacing/type/color/chrome tokens) the 60 UI-SPEC inherits verbatim.

### Data model + endpoints verified at discuss time (the data this phase surfaces + wires)
- `src/phaze/models/proposal.py` — `RenameProposal` = `file_id` + `proposed_filename` + `proposed_path` + `confidence` (float) + `status` (`ProposalStatus`); unique pending-per-file index. **Source for Rename/Path + Move diffs (D-06) and the D-02 confidence predicate.**
- `src/phaze/models/tag_write_log.py` — `TagWriteLog` = `before_tags`/`after_tags`/`discrepancies`/`status` (the tag-write audit row, D-04).
- `src/phaze/models/execution.py` — `ExecutionLog` = `proposal_id` + `operation` + `source_path`/`destination_path` + `sha256_verified` + `status` (the rename/move audit row, D-04).
- `src/phaze/routers/proposals.py` — existing `approve`/`reject`/`undo` per-proposal (`:168/193/218`), `bulk_action` client-id-list endpoint (`:309` — the REVIEW-02 hazard D-02 replaces), `/proposals/`→`/s/propose` redirect (`:129`).
- `src/phaze/routers/tags.py` — `compute_proposed_tags` + `_build_comparison` + `compare_tags` (`:212`), `_get_tag_stats` (pending/completed/discrepancies). **Source for the tag diff + the D-03 no-discrepancies predicate.**
- `src/phaze/routers/execution.py` — `/execution/start` (`:83`) applies approved proposals; `/audit/` (`:350`) is the existing unified audit view (D-04).
- `src/phaze/routers/duplicates.py` — `list_duplicates` (`:79`), `compare_group` (`:116`), `resolve` (`:144`), `undo` (`:169`), `resolve-all` (`:199`), `undo-all` (`:233`). **Wires Dedupe (D-07).**
- `src/phaze/routers/cue.py` — `_get_eligible_tracklist_query` (matched-tracklist gate), `_build_cue_tracks`, `list_cue` (`:176`). **Wires Cue (D-08).**
- `src/phaze/routers/preview.py` — `/preview/`→`/s/move` redirect (`:46`) — Move is the destination-path/tree facet of `RenameProposal` (D-06).
- `src/phaze/routers/tracklists.py:632` — `reject-low` (threshold-query, server re-queries) — the **server-predicate template** D-02 mirrors.
- `src/phaze/routers/shell.py:88-93` — the `propose`/`rename`/`tagwrite`/`move`/`dedupe`/`cue` `_STAGE_PLACEHOLDER` entries this phase supersedes; `_render_stage` (`:97`) is where the DB-context branches are added (mirror the 58/59 branches).
- `src/phaze/routers/pipeline.py` — `_enqueue_proposal_jobs` / `generate_proposals` batch trigger (Propose "Generate ALL", D-01); `proposalsDone/Total` in the stats fanout (`:169-170`); `/pipeline/stats` 5s poll + `oob_counts` gate.
- `src/phaze/services/tag_proposal.py` (`compute_proposed_tags`, `CORE_FIELDS`), `src/phaze/services/proposal.py`, `src/phaze/services/cue_generator.py` — the unchanged apply/generation logic these thin endpoints call over.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **Proposal endpoints**: `approve`/`reject`/`undo` (`proposals.py:168/193/218`) — wire per-file Approve/Skip/undo verbatim. `bulk_action` (`:309`) is the **anti-pattern** D-02 replaces (client id-list).
- **`reject-low` server-predicate** (`tracklists.py:632`): the exact template for the D-02 bulk-approve-high-confidence endpoint (threshold query, server re-queries — no client ids).
- **Tag comparison**: `compute_proposed_tags` + `_build_comparison` + `compare_tags` (`tags.py`) — the before→after tag diff and the D-03 discrepancy predicate.
- **Execution + audit**: `/execution/start` applies proposals; `ExecutionLog`/`TagWriteLog` are the audit rows; `/audit/` is the existing unified log (D-04).
- **Dedupe**: `duplicates.py` `resolve`/`resolve-all`/`undo`/`undo-all` + `compare_group` (D-07).
- **Cue**: `cue.py` eligible-tracklist gate + `_build_cue_tracks` preview (D-08).
- **`/pipeline/stats` 5s poll + `$store.pipeline`** — the single live-refresh mechanism all Review workspaces ride (no second loop; counts-only OOB on the diff list).
- **Legacy partials** (`proposals/partials/*`, `tags/partials/*`, `duplicates/*`, `cue/*`) — restyle to the unified workspace diff pattern (discretion).

### Established Patterns
- **Fragment-vs-full-page rendering** (Phase 57): each stage route returns its content block as a fragment on HTMX rail swaps, full shell on direct nav. Supersede placeholders one stage at a time; app usable at every commit.
- **OOB `hx-swap-oob` fanout** off the one `/pipeline/stats` response behind the `oob_counts` gate — the only live-update mechanism. **Diff-list poll updates COUNTS ONLY** — never re-render the operator's in-progress selection subtree (roadmap constraint; prevents stale-selection corruption during review).
- **Server-evaluated predicate** (`reject-low`): bulk actions carry an action + fixed threshold; the server re-queries at submit — never a client-built row-id list (REVIEW-02).
- **One workspace table per stage + one unified diff partial** (58 D-03 / 59 D-03) — keeps the single-poll OOB fanout to one fragment.

### Integration Points
- The six redesigned workspace fragments replace the `_STAGE_PLACEHOLDER` values for `propose`/`rename`/`tagwrite`/`move`/`dedupe`/`cue` in `STAGE_PARTIALS` (`shell.py:88-93`), with their `_render_stage` DB-context branches (mirror the 58/59 metadata/fingerprint/trackid/tracklist branches). Rail nodes + `/s/<stage>` routing already exist from Phase 57. Use **static string literals** for the template paths (T-57-01).
- The two new thin endpoints (D-02 bulk-approve-predicate, D-05 inline Edit PATCH) land in `proposals.py` alongside the existing approve/reject/undo routes.
- Leave the dead-template AST guard green by superseding-in-place, not deleting legacy templates (removal = CUT-02 / Phase 62).
</code_context>

<specifics>
## Specific Ideas

- **prototype.html + design §7 are the canonical visual/behavioral target** for the Review & Apply gate — match the per-file before→after diff + Approve/Edit/Skip + bulk-high-conf model, keeper-select, and cue preview.
- **This is the most correctness-sensitive phase** (irreplaceable archive). Favor the conservative default everywhere: fixed 0.9 confidence threshold (D-02), no-discrepancies-only tag bulk (D-03), server-evaluated predicates (never client id-lists), and counts-only diff-list polling so an in-progress operator selection is never clobbered.
- Phase 60's two sanctioned endpoint additions (D-02, D-05) are the **deliberate exception** to the 58/59 "cut anything needing an endpoint" rule — because REVIEW-01/02 require them. Add them as thin routes over unchanged logic; do not cut, and do not expand beyond these two.
</specifics>

<deferred>
## Deferred Ideas

- **REVIEW-06 — per-stage configurable confidence thresholds + override UI** for "approve all high-confidence." v7.0 ships the fixed thresholds (0.9 for proposals, no-discrepancies for tags); making them user-configurable is a future requirement. (Already a documented deferred requirement.)
- **Unified below-the-line Audit rail node** (design §5 "Audit log · Compute/Agents") — Phase 60 reuses the existing `/audit/` page; promoting Audit to a first-class shell rail node is a later phase, not this one.
- **Row-click → rich per-file record/pane slide-in** for Review rows — Phase 61 (RECORD-01). Phase 60 rows carry in-row Approve/Edit/Skip controls but the row→record interaction is Phase 61.
- **Configurable / per-queue keeper-quality heuristics for Dedupe** beyond the existing auto-keep-highest-quality — not requested; existing behavior wired as-is.

None of these arose as scope creep — they are deferred requirements or conscious later-phase boundaries.
</deferred>

---

*Phase: 60-review-apply*
*Context gathered: 2026-06-30*
