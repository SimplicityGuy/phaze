# Phase 60: Review & Apply - Pattern Map

**Mapped:** 2026-07-01
**Files analyzed:** ~13 (9 new templates, 2 modified routers, 1 modified/─new service helper set, 1 new test file)
**Analogs found:** 11 / 13 exact-or-role match · 2 genuinely-new interaction shapes (diff row, cue preview) tokenized in 60-UI-SPEC

> **Phase character:** presentation rewrite of SIX placeholder stages (`propose`/`rename`/`tagwrite`/`move`/`dedupe`/`cue`) into the Review & Apply gate — the Phase-58/59 workspace pattern applied SIX more times — PLUS the milestone's *only* sanctioned backend additions: **two** thin endpoints (D-02 server-predicate bulk-approve, D-05 inline-Edit PATCH) in `proposals.py`. Everything else rides existing endpoints.
>
> **CRITICAL — the UI-SPEC sketch HTML contains three wiring stubs that the real routers CONTRADICT. Trust the endpoints below, not the sketch.** These are called out inline as ⚠️ CORRECTION.

---

## Verified Endpoint Corrections (read before planning — the UI-SPEC sketch is wrong on these)

| Surface | UI-SPEC sketch shows | ✅ Real endpoint (verified this session) |
|---------|----------------------|------------------------------------------|
| Per-row Approve / Skip / Undo (Rename/Move) | `hx-post=".../approve"` | **`hx-patch`** — `PATCH /proposals/{id}/approve` · `/reject` (=Skip) · `/undo` (`proposals.py:168/193/218`) |
| Cue per-card Approve | `hx-post="/cue/{id}/approve"` | **NO `/approve` route exists.** Approve = generate: `POST /cue/{tracklist_id}/generate` (`cue.py:239`). Batch = `POST /cue/generate-batch` (`:328`). Card key = `tracklist.id`. |
| Dedupe keeper radio | `hx-post="/duplicates/{group_id}/resolve"` `hx-vals='{"keeper_id":…}'` | **`POST /duplicates/{group_hash}/resolve`** with Form field **`canonical_id`** (uuid) (`duplicates.py:144`). Group key = `group["sha256_hash"]`, NOT a `group_id`. |
| Tag row Approve | (implies proposal approve) | **`POST /tags/{file_id}/write`** (`tags.py:309`) — the write IS the apply; there is NO tag pending→approved status and NO tag proposal row (tags are *computed*). |

**OQ-1 (tag-predicate ambiguity — flagged in the phase prompt):** the D-03 Tag "APPROVE ALL WITH NO DISCREPANCIES" bulk has **no existing endpoint** and does **not** fit in `proposals.py` (tags aren't `RenameProposal` rows). CONTEXT names only "two thin endpoints" (D-02, D-05, both in `proposals.py`). The tag bulk predicate is therefore either (a) a **third** new endpoint in `tags.py` (server re-queries EXECUTED files whose `_build_comparison` has `changed==0`, loops `execute_tag_write`), or (b) deferred. **Planner must resolve.** See § No Analog Found.

**Tag reversibility gap (D-04):** `tags.py` has **no undo route** — `TagWriteLog` is append-only with no reverse path. The UI-SPEC shows in-row UNDO on Tag rows, but only Rename/Move (`proposals.py:218`) and Dedupe (`duplicates.py` undo/undo-all) have real reverse endpoints. Planner must decide whether Tag UNDO ships (would be a further new endpoint, out of the sanctioned two) or is omitted.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/routers/shell.py` (MODIFY: 6 `STAGE_PARTIALS` values + 6 `_render_stage` branches) | router | request-response (read) | its own `metadata`/`fingerprint`/`trackid` branches (`:133-167`) | exact (same file) |
| `src/phaze/routers/proposals.py` (MODIFY: +D-02 bulk-predicate, +D-05 edit PATCH) | router | request-response (write) | `reject-low` (`tracklists.py:632`) + `approve_proposal` PATCH (`:168`) + `bulk_action` (`:309`) | role-match (exact shape) |
| `src/phaze/services/proposal_queries.py` (MODIFY: +predicate-select helper, +field-update helper) | service | CRUD (SELECT+UPDATE) | `update_proposal_status` (`:152`) + `bulk_update_status` (`:177`) | exact |
| `src/phaze/services/pipeline.py` (MODIFY?: read-only cue/tag/dedupe assembly helpers for `_render_stage`) | service | CRUD (read-only) | `get_analyze_stage_files` (`:768-835`) degrade-safe pattern | role-match |
| `pipeline/partials/_diff_row.html` (CREATE — shared, D-06) | component (Jinja) | request-response render | `_file_table.html` cell/trust model + `tags/partials/tag_comparison.html` + prototype `diffRow()` | new (tokenized in UI-SPEC Pattern 1) |
| `pipeline/partials/propose_workspace.html` (CREATE, D-01) | component | request-response render | `fingerprint_workspace.html` (scaffold + ONE trigger + `_file_table`) | exact |
| `pipeline/partials/rename_workspace.html` (CREATE) | component | request-response render | `fingerprint_workspace.html` scaffold + `_diff_row` loop + bulk header | role-match |
| `pipeline/partials/move_workspace.html` (CREATE) | component | request-response render | `rename_workspace.html` sibling (same `_diff_row`, `proposed_path` facet) | exact (sibling) |
| `pipeline/partials/tagwrite_workspace.html` (CREATE) | component | request-response render | `rename_workspace.html` + `_diff_row` (tag facet) | role-match |
| `pipeline/partials/dedupe_workspace.html` + `_dupe_group.html` (CREATE, D-07) | component | request-response render | `analyze_workspace.html` lane-card grid (`:38-50`) + `_lane_card.html` shape | role-match |
| `pipeline/partials/cue_workspace.html` + `_cue_preview.html` (CREATE, D-08) | component | request-response render | `analyze_workspace.html` grid + prototype `cue()` | new (preview `<pre>` — tokenized in UI-SPEC Pattern 5) |
| `tests/test_review_apply_workspaces.py` (CREATE) | test | request-response assertions | `tests/test_identify_workspaces.py` / `test_enrich_analyze_workspaces.py` | exact |

**Existing endpoints/helpers wired VERBATIM (reference only, zero new code):**
`PATCH /proposals/{id}/approve|reject|undo` (`proposals.py:168/193/218`); `POST /pipeline/proposals` GENERATE ALL (`pipeline.py:913`, `_enqueue_proposal_jobs` `:364`); `POST /tags/{file_id}/write` (`tags.py:309`); `POST /duplicates/{group_hash}/resolve|undo` + `/resolve-all|/undo-all` (`duplicates.py:144/169/199/233`); `POST /cue/{tracklist_id}/generate` + `/generate-batch` (`cue.py:239/328`).

---

## Pattern Assignments

### `src/phaze/routers/shell.py` (router) — MODIFY

**Analog:** its own `metadata`/`fingerprint`/`trackid`/`tracklist` entries + branches (added Phases 58/59, same file).

**STAGE_PARTIALS** — replace the six `_STAGE_PLACEHOLDER` values (`shell.py:88-93`) with **static string literals** (T-57-01 — `stage` NEVER spliced into a path). Keep the Phase-58/59 comment convention (cite phase + REVIEW id + T-57-01 + supersede-in-place):
```python
    "propose": "pipeline/partials/propose_workspace.html",
    "rename": "pipeline/partials/rename_workspace.html",
    "tagwrite": "pipeline/partials/tagwrite_workspace.html",
    "move": "pipeline/partials/move_workspace.html",
    "dedupe": "pipeline/partials/dedupe_workspace.html",
    "cue": "pipeline/partials/cue_workspace.html",
```

**`_render_stage` DB-context branches** — append six `elif` branches mirroring the `metadata`/`fingerprint`/`trackid` shape (`shell.py:133-167`). `oob_counts` stays `False` (Pitfall 5); reads degrade-safe at the service layer (no router try/except). The exact shape to copy:
```python
    elif stage == "fingerprint":
        context["fingerprint_files"] = await get_fingerprint_pending_files(session)
    elif stage == "trackid":
        context["trackid_files"] = await get_trackid_stage_files(session)
    # Phase 60 adds (read-only assembly — no enqueue, no commit):
    #   propose  → pending RenameProposal rows (model + confidence)       — get_proposals_page(status="pending")
    #   rename   → pending RenameProposal rows (filename facet)           — same source, diff-row shape
    #   move     → pending RenameProposal rows (proposed_path facet)      — same source
    #   tagwrite → EXECUTED files + computed comparison (before→after)    — compute_proposed_tags + _build_comparison
    #   dedupe   → find_duplicate_groups_with_metadata + score_group      — canonical_id per group
    #   cue      → _get_eligible_tracklist_query + in-memory cue preview  — _build_cue_tracks + generate_cue_content (NO write)
```
> **Assembly-source note:** the read helpers for `tagwrite`/`dedupe`/`cue` currently live as **router-private `_`-prefixed functions** (`tags._build_comparison`, `duplicates._compute_best_values`, `cue._build_cue_tracks`). Importing router privates into `shell.py` is ugly; the Phase-58/59 precedent (`get_analyze_stage_files`, `get_trackid_stage_files`) is to factor a **degrade-safe service helper** in `services/pipeline.py` (or a new `services/review.py`). Planner decides; behavior identical.

**Fragment-vs-full fork** (`shell.py:169-171`) — UNCHANGED; all six new fragments ride it automatically.

---

### `src/phaze/routers/proposals.py` — MODIFY (the TWO sanctioned additions)

#### D-02 — server-predicate bulk-approve-high-confidence (NEW)

**Analog for the server-predicate discipline:** `reject_low_confidence` (`tracklists.py:632-669`) — takes a threshold, the server itself SELECTs the matching rows and acts; **never** a client id-list. **Analog for verb+response shape:** `approve_proposal` PATCH (`proposals.py:168`) + `bulk_action` (`:309`).

`reject-low` is a `POST`; the proposals router uses **PATCH for every state mutation** (`:168/193/218/309`) — keep proposals consistent (**PATCH**). The load-bearing difference from the existing `bulk_action` (`:309`, which takes `proposal_ids: list[str]` — the REVIEW-02 hazard) is: **NO Form id-list**; the server SELECTs pending rows at submit:

```python
@router.patch("/bulk-approve-high-confidence", response_class=HTMLResponse)
async def bulk_approve_high_confidence(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-02 (D-02): approve every PENDING proposal with confidence ≥ 0.9.

    Server-evaluated predicate — the fixed 0.9 threshold is re-queried at submit
    (NEVER a client-built id-list, unlike bulk_action). Mirrors tracklists.reject_low_confidence.
    """
    count = await approve_pending_above_confidence(session, threshold=0.9)  # NEW helper, see below
    stats = await get_proposal_stats(session)
    return templates.TemplateResponse(request=request, name="proposals/partials/approve_response.html",
        context={"request": request, "proposal": None, "stats": stats,
                 "action_label": "approved", "toast_message": f"{count} proposals approved.", "is_bulk": True})
```
- **Threshold = 0.9 fixed** (D-02, REVIEW-06 defers configurability). Same route serves the **Rename/Path AND Move** queues (both are `RenameProposal.confidence`).
- **Zero rows matched** → return the "Nothing matched — no pending rows meet the ≥90% confidence predicate right now" copy (UI-SPEC Copywriting § Bulk predicate returned zero).

#### D-05 — inline Edit PATCH (NEW)

**Analog:** `update_proposal_status` (`proposal_queries.py:152`) for the select→mutate→re-select-with-file shape; `approve_proposal` (`proposals.py:168`) for the router wrapper. Instead of `.status`, set `.proposed_filename` / `.proposed_path` from Form data, then **return the diff-row partial** so `hx-swap="outerHTML"` replaces only that row (R-6):
```python
@router.patch("/{proposal_id}/edit", response_class=HTMLResponse)
async def edit_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    proposed: str = Form(...),        # the `name="proposed"` input from _diff_row.html
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-01 (D-05): persist an operator edit to the proposal BEFORE approve.

    Thin write over the persisted row — does NOT re-run the LLM (generation logic untouched).
    """
    proposal = await update_proposal_fields(session, proposal_id, proposed_filename=proposed)  # NEW helper
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return templates.TemplateResponse(request=request, name="pipeline/partials/_diff_row.html",
        context={"request": request, ...facet mapping for the row...})
```
> **Move facet:** the same endpoint (or a `?facet=path` param) updates `proposed_path` instead — planner picks one route vs. a facet param; the **UI shape is identical** (UI-SPEC Pattern 1 Inline Edit). Tag inline-Edit is out of the initial cut (UI-SPEC: "unless trivially symmetric") — note the *existing* `PUT /tags/{file_id}/edit/{field}` (`tags.py:274`) already renders an inline field but returns a display span and does **not** persist (tags are computed, not stored).

---

### `src/phaze/services/proposal_queries.py` — MODIFY (two helpers for the above)

**Analog:** `update_proposal_status` (`:152`) and `bulk_update_status` (`:177`).

- **`approve_pending_above_confidence(session, threshold)`** — SELECT pending `RenameProposal.id` where `confidence >= threshold`, then reuse the existing `bulk_update_status(session, ids, ProposalStatus.APPROVED)` (`:177`) verbatim (it already cascades `FileRecord.state = APPROVED`, APR-02). Return the count. The predicate SELECT is the D-02 "server re-queries" contract.
- **`update_proposal_fields(session, proposal_id, *, proposed_filename=None, proposed_path=None)`** — copy `update_proposal_status` (`:152`) exactly, but assign the text field(s) instead of `.status`; keep the **re-select-with-`selectinload(file)`** tail (`:170-174`) so the returned row can render the diff. No `FileState` transition (edit is pre-approve).

---

### `pipeline/partials/_diff_row.html` (component, shared) — CREATE

**Analogs:** `_file_table.html:42-44` (the `cursor-pointer` + stable-id + autoescape-never-`|safe` trust boundary); `tags/partials/tag_comparison.html` (before/after field pairing); prototype `diffRow()`. **No single existing partial matches** — it is the new authoritative Review interaction, fully tokenized in **60-UI-SPEC § NEW Pattern 1** (copy that block).

**Load-bearing contract:**
- ONE partial, THREE facets (D-06). Per-facet `before`/`after`/source mapping is the UI-SPEC Pattern 1 table:
  | Queue | `before` | `after` | Approve wires to |
  |-------|----------|---------|------------------|
  | rename | `original_filename` | `proposed_filename` | `PATCH /proposals/{id}/approve` |
  | move | `source_path` | `proposed_path` | `PATCH /proposals/{id}/approve` |
  | tagwrite | current tags (`_build_comparison[].current`) | proposed tags (`.proposed`) | `POST /tags/{file_id}/write` ⚠️ different verb/route |
- `hx-patch` (not `hx-post`) for Rename/Move Approve/Skip/Undo. SKIP → `PATCH …/reject` (D discretion: reject IS skip). ⚠️
- Inline Edit = Alpine LOCAL `x-data="{editing,val}"`; SAVE EDIT `hx-patch` to D-05, `hx-target="#{row_id_prefix}-{id}"` `hx-swap="outerHTML"` (R-6 — own row only). DISCARD = no request.
- Every `before`/`after`/file cell autoescaped, NEVER `| safe` (ASVS V5). Row-body click UNBOUND (R-1 — record is Phase 61).

---

### `pipeline/partials/propose_workspace.html` (component, D-01) — CREATE

**Analog:** `fingerprint_workspace.html` — scaffold + ONE header trigger + `_file_table.html`. **Not a diff** (D-01: Propose is a thin generation view over the SAME `RenameProposal` source as Rename).

- **Header action GENERATE ALL** — copy the `fingerprint_workspace.html:23-33` `actions` block VERBATIM, swap endpoint→**`POST /pipeline/proposals`** (`pipeline.py:913`, the existing UI trigger returning `trigger_response.html`), target `#propose-trigger-response`, busy-gate `:disabled="$store.pipeline.proposalsBusy > 0"`, `hx-confirm` per Copywriting ("Generate AI proposals for all {n} pending files?"). ⚠️ Note: `proposalsDone` is a BATCH counter (`pipeline.py:79-82`) — surfaces batches-done, not files; use the DB `proposalsTotal` for the sub-count numeral.
- **Table** — reuse `_file_table.html` with columns `["File","Proposed name","Proposed path","Model","Conf"]` (UI-SPEC Pattern 2). `Conf` = `mono {n}%` tier-colored (≥90 emerald / 70-89 amber / <70 gray). Model = `settings.llm_model` (the configured value, NOT a per-row field — `RenameProposal.context_used` holds the LLM *input context*, not the model name; see RESEARCH.md OQ-2 resolution and `config.py:363`). Rows inert (`row_id_prefix="propose-row"`).

---

### `pipeline/partials/rename_workspace.html` + `move_workspace.html` (component) — CREATE

**Analog:** `fingerprint_workspace.html` scaffold + a `_diff_row.html` loop (instead of `_file_table`) + the bulk header action. `move` is the **sibling** of `rename` (LOCKED two-node split, UI-SPEC Pattern 1) over one `RenameProposal` source — `proposed_path` facet.

- **Bulk header action** — `_workspace_scaffold.html` `actions` slot, **amber-attention** treatment (UI-SPEC Color §), copy "APPROVE ALL ≥90% CONFIDENCE" → `hx-patch="/proposals/bulk-approve-high-confidence"` (D-02) with mandatory `hx-confirm` + `:disabled` busy-gate (R-4). The button carries **no id-list / no threshold param** — the predicate is fixed server-side (that is why the counts-only poll R-2 can't corrupt it).
- **Body** — `{% for p in rename_proposals %}{% include "pipeline/partials/_diff_row.html" %}{% endfor %}` passing the facet vars (`before`/`after`/`row_id_prefix="rename-row"` vs `"move-row"`).
- Titles/sub-counts/empty-states per UI-SPEC Copywriting §.

---

### `pipeline/partials/tagwrite_workspace.html` (component) — CREATE

**Analog:** `rename_workspace.html` + `_diff_row.html` (tag facet, from `_build_comparison`). ⚠️ **Different apply wiring:** row APPROVE → `POST /tags/{file_id}/write` (`tags.py:309`), NOT a proposals PATCH. Bulk header → OQ-1 (no existing endpoint; see § No Analog). Tag UNDO → no endpoint (see gap note). Sub-count "no discrepancies"/"N discrepancies" derived from `_count_changes(comparison)` (`tags.py:128`).

---

### `pipeline/partials/dedupe_workspace.html` + `_dupe_group.html` (component, D-07) — CREATE

**Analog:** `analyze_workspace.html` grid-card structure (`:38-50` `grid grid-cols-3 gap-4 p-6`) + `_lane_card.html` card shape; prototype `dupeGroup()` (UI-SPEC Pattern 4). NOT the diff partial.

⚠️ **Verified wiring (UI-SPEC sketch is wrong):**
- Keeper radio → `hx-post="/duplicates/{{ group.sha256_hash }}/resolve"` with Form field **`canonical_id`** = the selected `file_id`. (`duplicates.py:144` signature: `canonical_id: uuid.UUID = Form(...)`.) The response is `duplicates/partials/resolve_response.html` carrying `resolved_file_states` (a `json.dumps` string).
- **Auto-keep bulk** → `hx-post="/duplicates/resolve-all"` (Form `page`/`page_size`) (`:199`). `score_group(group)` (`:98`) sets `group["canonical_id"]` = highest-quality choice.
- **UNDO reversibility (D-04)** → `hx-post="/duplicates/{{ group.sha256_hash }}/undo"` (`:169`) with Form **`file_states`** = the JSON string round-tripped from the resolve response. `undo-all` (`:233`) needs the aggregated `file_states`. **This round-trip is load-bearing** — the undo endpoints reconstruct prior state from that JSON, not from a fresh query.
- **Group dict keys** (from `find_duplicate_groups_with_metadata` + `score_group`): `sha256_hash`, `canonical_id`, `files[]` (each `{id, file_size, bitrate, duration, tag_filled, ...}`). KEEP/archive text tags never hue-only (WCAG 1.4.1).

---

### `pipeline/partials/cue_workspace.html` + `_cue_preview.html` (component, D-08) — CREATE

**Analog:** `analyze_workspace.html` grid + prototype `cue()` (UI-SPEC Pattern 5). The `<pre>` preview is genuinely new.

⚠️ **Verified wiring:**
- **Eligibility gate** = `_get_eligible_tracklist_query` (`cue.py:31`): `status=="approved"` + `Tracklist.file_id` set + `FileRecord.state==EXECUTED` + ≥1 timestamped track. Ineligible sets render the `opacity-60` "awaiting tracklist match…" card with NO Approve.
- **Preview `<pre>`** — there is **no persisted preview** and **no dry-run endpoint**; `POST /cue/{id}/generate` builds AND writes to disk in one call (`cue.py:266-277`). To show a preview without writing, the `_render_stage` cue branch must build cue text **in-memory** via `_build_cue_tracks(session, latest_version_id)` (`:120`) + `generate_cue_content(...)` (imported `cue_generator`) — a **read-only** assembly (no `write_cue_file`). Flag for planner: this is a read-time reuse of generation *helpers*, not the write endpoint.
- **APPROVE** → ⚠️ `hx-post="/cue/{{ tracklist.id }}/generate"` (`cue.py:239`) — this is the write. There is NO `/approve`. Optional per-card EDIT: omit (no endpoint). No bulk (REVIEW-04 doesn't require it; the prototype `EXPORT APPROVED` has no endpoint — omit).
- Card key = `tracklist.id`; two-column grid `grid grid-cols-2 gap-4 p-6`; `<pre class="mono text-[11px] … bg-phaze-bg">`.

---

### `tests/test_review_apply_workspaces.py` (test) — CREATE

**Analog:** `tests/test_identify_workspaces.py` — copy its whole shape (module-level async `_seed_*` ORM helpers; `_WORKSPACE_STAGES = ["propose","rename","tagwrite","move","dedupe","cue"]`; foundation tests filled now, workspace-behavior tests as `xfail` stubs converted by their owning plan).

**Foundation tests (fill immediately — pass against placeholders today, guard the contract):**
- `test_review_fragments_are_bare` → R-5: `/s/{stage}` HX responses have no `<html`/`<head`; exactly one `tabindex="-1"`.
- `test_review_single_poll_discipline` → R-2/WORK-05: shell fires exactly one `hx-get="/pipeline/stats"`; NO fragment carries `hx-trigger="every"` / `setInterval`; **NO `hx-swap-oob` on any diff-row / keeper-group / cue card container** (the counts-only constraint).

**Workspace tests (per plan):**
| Test | Mirrors | Key assertions |
|------|---------|----------------|
| `test_diff_row_before_after` | (new) | rose struck `before` + emerald `after`; `hx-patch` on Approve; inline-edit `x-data`; `name="proposed"` input |
| `test_bulk_approve_high_confidence_server_predicate` | `test_metadata_trigger_all_wired` | button posts `PATCH /proposals/bulk-approve-high-confidence` with NO id-list; endpoint approves only `confidence>=0.9` pending rows (seed one 0.95 + one 0.5 → 1 approved) |
| `test_edit_patch_targets_own_row` | (new) | `PATCH /proposals/{id}/edit` updates `proposed_filename`; `hx-target` = row id, `hx-swap="outerHTML"` (R-6) |
| `test_dedupe_keeper_resolve_wiring` | `test_tracklist_step_cards` | radio posts `/duplicates/{sha256}/resolve` with `canonical_id`; UNDO round-trips `file_states` |
| `test_cue_gate_and_preview` | (new) | ineligible set → `opacity-60`, no Approve; eligible → `<pre>` preview + `POST /cue/{id}/generate` |

**Existing guards (green once `STAGE_PARTIALS` points at real fragments — do NOT modify):** `tests/test_dead_template_guard.py`, `tests/test_shell_routes.py::test_rail_nodes_wired`.

---

## Shared Patterns

### Workspace scaffold (the spine every one of the six composes)
**Source:** `pipeline/partials/_workspace_scaffold.html:24-42`
**Apply to:** all six new workspace templates.
- `{% import … as ws %}` then `{% call ws.workspace(title=…, subcount=…, actions=…) %}`. Emits EXACTLY ONE `<h1 tabindex="-1">` (R-5). `actions` = block-set (the amber bulk header / GENERATE ALL). `subcount` = JS expr string `x-text` against `$store.pipeline`. Auto-includes `_workspace_poll_seeds.html`.

### R-4 bulk / mass-action guard
**Source:** `fingerprint_workspace.html:25-32` (the `_btn` string + `hx-confirm` + `:disabled` busy-gate)
**Apply to:** all four mass actions — the three bulk APPROVE ALL, AUTO-KEEP, GENERATE ALL. `hx-confirm` names the predicate + live count; `:disabled` binds the relevant `$store.pipeline.*Busy` key. Per-row Approve/keeper-radio/cue-Approve are single+undoable → no confirm (SKIP gets a light confirm since it rejects).

### Counts-only single-poll (R-2 — the load-bearing correctness constraint)
**Source:** `shell.py:117` `oob_counts=False` + `pipeline.py:156-175` stats fanout (`proposalsDone/Total` already there)
**Apply to:** all six fragments. NO `hx-swap-oob` on any row/card container; NO second poll. Live sub-counts ride the existing `/pipeline/stats` fanout behind the `oob_counts` gate as counts-only seed spans (new seed ids `#review-*-count` extend the existing fanout — executor discretion, R-2 is the hard contract). An in-progress inline-edit / selected keeper radio / partial scroll is NEVER clobbered.

### Degrade-safe read helper (if service helpers factored for `_render_stage`)
**Source:** `services/pipeline.py:768-835` (`get_analyze_stage_files`)
**Apply to:** any new `tagwrite`/`dedupe`/`cue` assembly helper. `async with session.begin_nested()` SAVEPOINT; `except Exception: logger.warning(...); return []`. Return plain dicts, not ORM objects. No commit, no enqueue, no schema change.

### Two-weight / no-legacy-pill discipline (carried from Phase 58/59)
**Source:** UI-SPEC Typography § + 59-PATTERNS anti-pattern note
**Apply to:** all six. Do NOT reuse `proposals/partials/*`, `tags/partials/*` `font-semibold` colored pills, `tracklists/partials/confidence_badge.html`. Render confidence/discrepancy/apply state as colored **status words + mono numerals** in the two-weight (400/500) emerald/amber/blue/rose/gray contract. Supersede-in-place — legacy templates stay reachable until CUT-02/Phase 62 (dead-template guard).

---

## No Analog Found

| File / Concern | Role | Data Flow | Reason |
|----------------|------|-----------|--------|
| **D-03 Tag bulk "no-discrepancies" predicate** | router endpoint | request-response (write) | OQ-1 — no existing endpoint; tags aren't `RenameProposal` rows so it can't reuse D-02 in `proposals.py`. Closest *shape* is `reject-low` (server-predicate) + a loop over `execute_tag_write` (`tag_writer.py`). Would be a **third** new endpoint in `tags.py` (server re-queries EXECUTED files whose `_build_comparison` has `changed==0`). **Planner must decide** whether it ships or defers — CONTEXT sanctions only two. |
| **Tag row UNDO (D-04 reversibility)** | router endpoint | request-response (write) | `tags.py` has no reverse route; `TagWriteLog` is append-only. UI-SPEC shows in-row UNDO on Tag rows but only Rename/Move + Dedupe have real undo endpoints. Needs a planner decision (new endpoint = beyond the sanctioned two, or omit Tag UNDO in v7.0). |
| **`_diff_row.html`** | component | render | Genuinely new interaction, but **fully tokenized** in UI-SPEC Pattern 1 (copy that block) — cell-trust model from `_file_table.html`, field pairing from `tags/partials/tag_comparison.html`. |
| **Cue preview `<pre>`** | component | render | No persisted preview / dry-run endpoint; must reuse `_build_cue_tracks` + `generate_cue_content` in-memory (no `write_cue_file`) at render time. Tokenized in UI-SPEC Pattern 5. |

---

## Metadata

**Analog search scope:** `src/phaze/routers/{shell,proposals,tags,duplicates,cue,tracklists,execution,pipeline}.py`; `src/phaze/services/proposal_queries.py`; `src/phaze/models/{proposal,tag_write_log,execution}.py`; `src/phaze/templates/pipeline/partials/{_workspace_scaffold,_file_table,fingerprint_workspace,analyze_workspace}.html`; `tests/test_identify_workspaces.py`.
**Files scanned:** 20 source/template/test files (all line numbers verified this session).
**Note:** No `60-RESEARCH.md` exists in the phase dir — the verified endpoint corrections in the phase prompt were re-confirmed directly against the routers this session (§ Verified Endpoint Corrections).
**Pattern extraction date:** 2026-07-01
