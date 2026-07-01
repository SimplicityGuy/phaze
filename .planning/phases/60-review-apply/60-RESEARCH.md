<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01 — Propose folds into the Rename/Path gate.** `propose` node = thin *generation* view (proposals list + model + confidence + **GENERATE ALL** wired verbatim to the existing batch proposal-generation path). `rename` node = before→after review diff over the **same `RenameProposal` rows**. Two rail nodes over ONE `RenameProposal` source. `/proposals/` already `RedirectResponse("/s/propose")`.
- **D-02 — NEW server-predicate bulk-approve endpoint, `RenameProposal.confidence ≥ 0.9` (fixed).** Mirrors `tracklists.py:632` `reject-low`: server re-queries pending rows at submit, NEVER accepts a client id-list. Applies to Rename/Path and Move queues (both `RenameProposal.confidence`). Generation + execution logic unchanged.
- **D-03 — Tag-write "high-confidence" = zero discrepancies.** Bulk re-queries pending tag-write files whose comparison has zero discrepancies and writes them; discrepancy rows stay per-file only. Server-evaluated predicate (no client id-list). *(See Open Question OQ-1 — the "discrepancy" concept needs disambiguation against live code.)*
- **D-04 — Per-stage undo over existing endpoints + reuse existing `/audit/` view.** Do NOT build new audit/undo logic. Rename/Move → `ExecutionLog` + `execution.py:/execution/start` + proposal `undo`. Tag → `TagWriteLog`. Dedupe → `duplicates.py` resolve/resolve-all + undo/undo-all. Existing `/audit/` (`execution.py:350`) is the unified log. No new Audit rail node.
- **D-05 — Inline Edit = new small PATCH updating `proposed_filename`/`proposed_path`** before approve. Mirrors the existing status-PATCH pattern. Edits the persisted row; does NOT re-run the LLM. Second sanctioned backend addition — do not cut.
- **D-06 — One Jinja diff partial over existing sources; Rename & Move share `RenameProposal`.** Rename facet = `proposed_filename`; Move facet = `proposed_path`; Tag facet = computed tag comparison. Rename/Path and Move are TWO separate rail-node workspaces over one source (LOCKED in UI-SPEC).
- **D-07 — Dedupe = existing duplicate-group resolve/keeper-select + auto-keep bulk.** Wire keeper radio → `resolve`, bulk → `resolve-all`, undo/undo-all for reversibility. Read via `list_duplicates`/`compare_group`. Zero backend change.
- **D-08 — Cue = existing preview + approve, gated on a matched tracklist.** Wire to `cue.py` (`_get_eligible_tracklist_query` gate + `_build_cue_tracks` preview). Approve over the existing cue-generation path. Zero backend change.

### Claude's Discretion
- Exact OOB id additions for the new fragments (must ride the single `/pipeline/stats` 5s poll + `oob_counts` gate — no second loop; diff-list poll updates COUNTS ONLY, never re-render the operator's in-progress selection subtree).
- Rename/Path vs Move split — **already LOCKED by UI-SPEC as two separate rail-node workspaces**.
- Whether "Skip" maps to existing `reject` status or a distinct skip status (existing `reject` is the sensible default — **UI-SPEC locks reject**).
- Empty-state / trigger-response copy (locked by 60-UI-SPEC; mirror 58/59).
- Reuse/restyle legacy partials vs fresh fragments (supersede-in-place; legacy templates stay until CUT-02).
- Surfacing failed-apply `error_message` on rows — optional nicety, not required.

### Deferred Ideas (OUT OF SCOPE)
- REVIEW-06 — per-stage configurable thresholds + override UI. v7.0 ships fixed thresholds (0.9 / no-discrepancy).
- Unified below-the-line Audit rail node (reuse existing `/audit/`).
- Row-click → rich per-file record slide-in (Phase 61, RECORD-01). Rows carry in-row controls only.
- Configurable per-queue keeper-quality heuristics for Dedupe.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REVIEW-01 | Rename/Path, Tag-write, Move-files each present pending changes as before→after diff with per-file Approve/Edit/Skip | One diff partial over `RenameProposal` (rename+move) + computed tag comparison. Per-file APPROVE/SKIP/UNDO = existing PATCH routes `proposals.py:168/193/218` (rename/move) + `POST /tags/{id}/write` + `PUT /tags/{id}/edit/{field}` (tag). EDIT = NEW D-05 PATCH (rename/move). See §Verified References + §Landmines. |
| REVIEW-02 | Bulk "approve all high-confidence" gated by a confidence threshold, server-evaluated | NEW D-02 endpoint mirroring `tracklists.py:632` `reject-low` (verified: `threshold` query param, server re-queries + acts). Fixed 0.9 for `RenameProposal.confidence`; no-discrepancy analog for tags (OQ-1). |
| REVIEW-03 | Dedupe duplicate groups + keeper-selection + bulk auto-keep-highest-quality | `duplicates.py` resolve (`:144`, `canonical_id`), resolve-all (`:199`, page-scoped), undo (`:169`), undo-all (`:233`). Zero backend change. See §Landmines (group_hash, canonical_id, file_states blob). |
| REVIEW-04 | Cue-sheet review with preview + approve, gated on matched tracklist | `cue.py` `_get_eligible_tracklist_query` (`:31`), `_build_cue_tracks` (`:120`), `list_cue` (`:176`). **Approve = `POST /cue/{tracklist_id}/generate` (`:239`) — no `/approve` endpoint exists.** See §Landmines. |
| REVIEW-05 | Every applied change recorded in audit log and reversible | `ExecutionLog` (rename/move, written by agent execution after `/execution/start`), `TagWriteLog` (tag write), dedupe resolution + undo. Existing `/audit/` (`:350`). "One audit row per apply" = assertion on existing writes. |
</phase_requirements>

# Phase 60: Review & Apply - Research

**Researched:** 2026-07-01
**Domain:** FastAPI + HTMX + Alpine server-rendered review/approval UI; wiring six DAG-rail workspaces to existing routers + adding exactly two thin routes
**Confidence:** HIGH (all cited file:line refs verified against live code; two open questions flagged)

## Summary

Phase 60 is exceptionally well-scoped by CONTEXT.md and 60-UI-SPEC.md — both are LOCKED and approved. My job was to **verify every file:line reference against the live code** and surface implementation landmines. The verification result: **the endpoint line numbers in CONTEXT are all accurate**, but the UI-SPEC's illustrative HTML sketches contain **several method/param/path mismatches against the real routes** that will silently break if copied literally. These are the highest-value findings in this document.

The pattern to mirror is fully established by Phases 58/59: a `workspace(...)` Jinja macro (`pipeline/partials/_workspace_scaffold.html`) imported as `ws`, called with the body in `{% call %}`, plus a `_render_stage` DB-context branch per stage in `shell.py`, plus the single `/pipeline/stats` 5s poll fanning out `dag-seed-*` OOB targets into `$store.pipeline`. Six `_STAGE_PLACEHOLDER` entries (`shell.py:88-93`) get replaced with static-literal template paths (T-57-01).

Two thin backend additions are sanctioned and must NOT be cut: the D-02 server-predicate bulk-approve and the D-05 inline-Edit PATCH. Everything else rides existing endpoints.

**Primary recommendation:** Copy the 58/59 scaffold/branch pattern verbatim; wire against the **verified** routes in §Verified References (NOT the UI-SPEC's sketch HTML); resolve OQ-1 (tag "discrepancy" predicate) at plan time before implementing the Tag bulk action.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Stage → fragment resolution | Frontend Server (shell.py `_render_stage`) | — | Whitelist dict + fragment-vs-full fork already owns this (Phase 57) |
| Diff / keeper / cue rendering | Frontend Server (Jinja partials) | — | Server-rendered HTML; no client build |
| Per-file approve/skip/undo | API/Backend (existing PATCH routes) | — | Status transitions + FileRecord.state, unchanged logic |
| Bulk approve-high-confidence | API/Backend (NEW D-02 route) | — | Server MUST own the predicate re-query (REVIEW-02 correctness) |
| Inline Edit persist | API/Backend (NEW D-05 route) | — | Edits persisted row before approve |
| Physical apply (move) + audit | API/Backend (`/execution/start` → agent SAQ jobs) | — | ExecutionLog written by agent, async — NOT per-row |
| Tag write + audit | API/Backend (`POST /tags/{id}/write`) | — | TagWriteLog written synchronously |
| Live count refresh | Frontend Server (single `/pipeline/stats` poll) | Browser (Alpine `$store.pipeline` x-text) | Counts-only OOB; row/card subtree never re-rendered (R-2) |

## Standard Stack

No new packages. Phase 60 uses the already-installed, CLAUDE.md-locked stack:

| Library | Version (installed) | Purpose | Notes |
|---------|--------------------|---------|-------|
| FastAPI | project-pinned | Routes for the 2 new thin endpoints | `@router.patch(...)` for D-02/D-05, mirroring existing PATCH routes |
| SQLAlchemy (async) + asyncpg | project-pinned | Re-query predicate (D-02), edit persist (D-05) | Mirror `update_proposal_status` (`proposal_queries.py:152`) |
| Jinja2 + HTMX 2.x + Alpine 3.x | CDN, unchanged | Diff partial, keeper cards, cue preview, inline-edit island | **No new CDN script** — `@alpinejs/focus` is a Phase-61 dep; use plain `.focus()` |
| Tailwind v4 (pre-compiled) | build artifact | New diff/keeper/cue utility classes | Require a `just tailwind` rebuild of `/static/css/app.css` (gitignored). Partials must live under a `@source`-covered path (`pipeline/partials/` already covered) |
| pytest + pytest-asyncio + httpx AsyncClient | project-pinned | Validation (see §Validation Architecture) | `uv run pytest` only |

## Package Legitimacy Audit

**No external packages installed this phase.** Section not applicable — all work is application code + templates over the existing stack. No `npm`/`pip`/`cargo` install occurs.

## Verified References (live-code audit)

Every reference below was read against the working tree on 2026-07-01. **Line numbers in CONTEXT are accurate. The corrections column flags where the UI-SPEC sketch HTML diverges from the real route.**

### Proposals (Rename/Move source + the 2 new endpoints land here)
| Symbol | Verified location | Signature reality | [VERIFIED] |
|--------|------------------|-------------------|-----------|
| approve | `proposals.py:168` | **`@router.patch("/{proposal_id}/approve")`** → sets `APPROVED` + `FileRecord.state`. Returns `approve_response.html`. | VERIFIED: codebase |
| reject (= Skip) | `proposals.py:193` | **PATCH** `/{id}/reject` → `REJECTED`. | VERIFIED: codebase |
| undo | `proposals.py:218` | **PATCH** `/{id}/undo` → back to `PENDING`. | VERIFIED: codebase |
| bulk_action (anti-pattern) | `proposals.py:309` | **PATCH** `/bulk`, `proposal_ids: list[str] = Form(...)` — the client-id-list D-02 replaces. | VERIFIED: codebase |
| `/proposals/` redirect | `proposals.py:129` | Non-HX GET → `RedirectResponse("/s/propose", 302)`. | VERIFIED: codebase |
| Service to mirror for D-05 | `services/proposal_queries.py:152` `update_proposal_status` | `select(...).options(selectinload(RenameProposal.file))` → mutate → commit → re-fetch. D-05 mutates `proposed_filename`/`proposed_path` (NOT status; leave file state PENDING). | VERIFIED: codebase |

### The D-02 template (`reject-low`)
| Symbol | Verified location | Reality | [VERIFIED] |
|--------|------------------|---------|-----------|
| reject_low_confidence | `tracklists.py:632` | `@router.post("/{tracklist_id}/reject-low")`, `threshold: int = Query(50)`, **server issues `delete(...).where(confidence < threshold)`** then re-loads. Scoped to one tracklist_id. | VERIFIED: codebase |

> **D-02 adaptation:** `reject-low` is per-tracklist and takes a `threshold` param. The D-02 bulk-approve is **global (all pending proposals)** with a **fixed** 0.9 threshold — so it needs **no threshold param and no id**; the server does `select(RenameProposal).where(status=='pending', confidence >= 0.9)` then bulk-updates to APPROVED (reuse `bulk_update_status` after collecting matched ids, or issue a single `update(...).where(...)` + the FileRecord.state transition — mirror `bulk_update_status` at `proposal_queries.py:177`, which already does both). Return a count for the confirm/toast.

### Tags (Tag-write facet)
| Symbol | Verified location | Reality | [VERIFIED] |
|--------|------------------|---------|-----------|
| `_build_comparison` | `tags.py:102` | Emits per-CORE_FIELD `{field,label,current,proposed,changed}`. **`changed` is a pre-write boolean, NOT "discrepancies".** | VERIFIED: codebase |
| `_count_changes` | `tags.py:128` | Count of `changed==True`. | VERIFIED: codebase |
| `list_tags` (pending set) | `tags.py:140` | Queries **`FileRecord.state == EXECUTED`** only; non-HX GET → `RedirectResponse("/s/tagwrite")`. Builds per-file `comparison`. | VERIFIED: codebase |
| `compare_tags` | `tags.py:212` | GET `/{file_id}/compare`. | VERIFIED: codebase |
| Existing inline edit | `tags.py:242/274` | GET + **PUT** `/{file_id}/edit/{field}` (`save_tag_field`). | VERIFIED: codebase |
| Per-row APPROVE (tag) | `tags.py:309` | **`POST /tags/{file_id}/write`** — accepts form CORE_FIELDS (or falls back to computed). Writes `TagWriteLog`, `source="manual_edit"` when edited. | VERIFIED: codebase |
| `_get_tag_stats` | `tags.py:41` | pending/completed/discrepancy counts. | VERIFIED: codebase |
| `compute_proposed_tags`, `CORE_FIELDS` | `services/tag_proposal.py` | imported at `tags.py:21`. | VERIFIED: codebase |

### Execution + Audit
| Symbol | Verified location | Reality | [VERIFIED] |
|--------|------------------|---------|-----------|
| start_execution | `execution.py:83` | **`POST /execution/start`** — dispatches **ALL approved proposals** as per-agent SAQ sub-jobs (batch). NOT per-row. ExecutionLog written later by the agent. | VERIFIED: codebase |
| audit view | `execution.py:350` | `GET /audit/` (full page + `audit_content.html` HX fragment). | VERIFIED: codebase |
| `ExecutionLog` | `models/execution.py` | `proposal_id` FK, `operation`, `source_path`, `destination_path`, `sha256_verified` (bool), `status`, `error_message`, `executed_at`. Append-only. | VERIFIED: codebase |
| `TagWriteLog` | `models/tag_write_log.py` | `file_id` FK, `before_tags`, `after_tags`, `source`, `status` (completed/failed/**discrepancy**), `discrepancies` (JSONB, **post-write**), `error_message`, `written_at`. | VERIFIED: codebase |

### Dedupe
| Symbol | Verified location | Reality (⚠ = UI-SPEC sketch mismatch) | [VERIFIED] |
|--------|------------------|---------|-----------|
| list_duplicates | `duplicates.py:79` | GET `/`. | VERIFIED: codebase |
| compare_group | `duplicates.py:116` | GET `/{group_hash}/compare`. | VERIFIED: codebase |
| resolve | `duplicates.py:144` | **`POST /{group_hash}/resolve`, `canonical_id: uuid = Form(...)`.** ⚠ UI-SPEC sketch used `{group_id}` + `keeper_id` — WRONG. Path param is the **sha256 `group_hash` string**; form field is **`canonical_id`**. Returns `resolve_response.html` carrying `resolved_file_states` JSON. | VERIFIED: codebase |
| undo | `duplicates.py:169` | **`POST /{group_hash}/undo`, `file_states: str = Form(...)`** (the JSON blob from the resolve response). ⚠ Undo is **stateful** — needs the prior `file_states` round-tripped. | VERIFIED: codebase |
| resolve-all | `duplicates.py:199` | **`POST /resolve-all`, `page`/`page_size` Form.** ⚠ **Page-scoped**, auto-picks canonical via `score_group`. "Auto-keep highest quality" bulk resolves the *current page's* groups, not literally all. | VERIFIED: codebase |
| undo-all | `duplicates.py:233` | `POST /undo-all`, `file_states` + page. | VERIFIED: codebase |

### Cue
| Symbol | Verified location | Reality (⚠ = UI-SPEC sketch mismatch) | [VERIFIED] |
|--------|------------------|---------|-----------|
| router prefix | `cue.py:28` | `prefix="/cue"`. | VERIFIED: codebase |
| `_get_eligible_tracklist_query` | `cue.py:31` | eligibility gate (status approved + EXECUTED + timestamped tracks). | VERIFIED: codebase |
| `_build_cue_tracks` | `cue.py:120` | preview track build. | VERIFIED: codebase |
| list_cue | `cue.py:176` | GET `/`; non-HX → `RedirectResponse("/s/cue")`. | VERIFIED: codebase |
| **APPROVE** | `cue.py:239` | **`POST /cue/{tracklist_id}/generate`** — ⚠ **there is NO `/cue/{id}/approve` endpoint.** The UI-SPEC sketch's `hx-post="/cue/{id}/approve"` is WRONG. "Approve" = generate the `.cue` file. Re-validates gates (EXECUTED, approved, timestamps) and returns a toast/row; **branches on `HX-Target` starting with `"tracklist-"`** — the new workspace target won't match, so verify the fall-through branch renders acceptably. | VERIFIED: codebase |

### Preview (Move facet)
| Symbol | Verified location | Reality | [VERIFIED] |
|--------|------------------|---------|-----------|
| preview redirect | `preview.py:36/46` | GET `/preview/`, non-HX → `RedirectResponse("/s/move", 302)`. | VERIFIED: codebase |

### Pipeline (Propose "GENERATE ALL" + stats fanout)
| Symbol | Verified location | Reality | [VERIFIED] |
|--------|------------------|---------|-----------|
| **GENERATE ALL** target | `pipeline.py:913` `trigger_proposals_ui` | **`POST /pipeline/proposals`** — enqueues `generate_proposals` batches via `_enqueue_proposal_jobs` (`:364`), returns `trigger_response.html`. This is the HTMX wiring for D-01 GENERATE ALL. (`/api/v1/proposals/generate` at `:425` is the API twin — use the `/pipeline/*` one for the UI.) | VERIFIED: codebase |
| stats fanout keys | `pipeline.py:150-172` | `dag` dict includes `proposalsDone/Total`, `approved` (=approved-proposal count via `total("execute")`), `executedDone/Total`. **No** pending-rename / pending-tag / dupe-group / eligible-cue key exists yet. | VERIFIED: codebase |
| poll route | `pipeline.py:604` `/pipeline/stats` | renders `stats_bar.html` with `oob_counts=True`; the ONLY live loop. | VERIFIED: codebase |

### RenameProposal model
- `models/proposal.py`: `proposed_filename` (Text, NOT NULL), `proposed_path` (Text, **nullable**), `confidence` (Float, **NULLABLE**), `status`, `context_used` (JSONB), `reason` (Text). Partial-unique index: one PENDING per file. [VERIFIED: codebase]

## Architecture Patterns

### System Architecture Diagram

```
GET /s/{stage}  (HTMX rail swap OR direct nav)
        │
        ▼
 shell.py _render_stage(stage, session)          [Frontend Server]
        │  builds STAGE_PARTIALS[stage] (static literal)  ── T-57-01
        │  + per-stage DB context branch (mirror 58/59)
        ├── HX-Request:true ──► shell/_stage_fragment.html  (content-only, R-5)
        └── direct nav      ──► shell/shell.html  (full chrome)
                                        │
                                        ▼
                    pipeline/partials/<stage>_workspace.html
                    {% import _workspace_scaffold.html as ws %}
                    {% call ws.workspace(title, subcount, actions) %}
                        <body: diff rows | keeper cards | cue cards>
                    {% endcall %}   ──includes _workspace_poll_seeds.html
                                        │
        ┌───────────────────────────────┼────────────────────────────────┐
        ▼ per-row / per-card actions     ▼ header bulk action              ▼ live counts
  PATCH /proposals/{id}/approve     NEW PATCH /proposals/bulk-approve   ONE /pipeline/stats 5s poll
  PATCH /proposals/{id}/reject       (server re-queries conf>=0.9)      → stats_bar.html emits
  PATCH /proposals/{id}/undo        NEW PATCH /proposals/{id}/edit       hx-swap-oob dag-seed-*
  POST  /tags/{id}/write            POST /tags/... (no-discrepancy)      → $store.pipeline (x-text)
  POST  /duplicates/{hash}/resolve  POST /duplicates/resolve-all         COUNTS ONLY (R-2) —
  POST  /cue/{id}/generate          (canonical_id=Form)                  never touches row subtree
        │                                        │
        ▼ apply + audit                          ▼
  POST /execution/start (batch, ALL approved)  ExecutionLog / TagWriteLog / dedupe resolution
  → per-agent SAQ jobs → ExecutionLog (async)  → reversible via undo routes → /audit/ page
```

### Pattern 1: The workspace scaffold macro (mirror verbatim)
**What:** `_workspace_scaffold.html` declares `{% macro workspace(title, subcount='', actions='', x_data='', cloud_cards=false) %}`. Callers import it as `ws` and wrap their body in `{% call ws.workspace(...) %}...{% endcall %}`.
**Contract (from the macro source):**
- EXACTLY ONE `<h1 tabindex="-1">` (the focus landing target).
- Stage-action buttons use the Phase-57 **secondary** control style (blue primary is reserved for rail "+ Scan").
- `subcount` is a **JS expression string** bound via `x-text` against `$store.pipeline`. `actions` is captured markup (a `{% set actions %}...{% endset %}` block).
- The macro auto-includes `_workspace_poll_seeds.html`, so OOB seeds have landing targets.
- Do NOT emit `<html>`/`<head>`/`<header`/`{% extends %}` (R-5; a `<header>` substring trips the bare-fragment guard — the scaffold uses a `<div>` header deliberately).

**Example (verified from `tracklist_workspace.html`):**
```jinja
{% import "pipeline/partials/_workspace_scaffold.html" as ws %}
{% set subcount = '`${$store.pipeline.tracklistDone} sets matched · 1001Tracklists`' %}
{% call ws.workspace(title="TRACKLIST", subcount=subcount) %}
    ... body ... {{ tracklist_search_pending }} pending ...
{% endcall %}
```
Note the dual pattern: **live** counts bind to a `$store.pipeline` key via `x-text`; **step-specific** counts render as **static server-side numerals** (`{{ tracklist_search_pending }}`) passed from the `_render_stage` context. Review sub-counts should follow the same split (see Pattern 3).

### Pattern 2: The `_render_stage` DB-context branch (mirror 58/59)
Add `elif stage == "rename": context["..."] = await ...` branches in `shell.py:_render_stage` (currently ends at `tracklist`, `:167`). Keep reads **degrade-safe at the service layer** (no router try/except — the 58/59 helpers return `[]`/`0` on DB error). `oob_counts` stays `False` on the stage render (never emit the OOB "files ready" paragraphs on initial render — Pitfall 5). Replace the six `_STAGE_PLACEHOLDER` values (`shell.py:88-93`) with **static string literals** (T-57-01), e.g. `"rename": "pipeline/partials/rename_workspace.html"`.

### Pattern 3: Counts-only OOB (R-2 — the load-bearing correctness constraint)
The ONLY live loop is the chrome `/pipeline/stats` 5s poll. Its `stats_bar.html` emits hidden `hx-swap-oob` paragraphs that land on pre-existing `dag-seed-<key>` ids in `_workspace_poll_seeds.html`, re-pushing `$store.pipeline` keys. To live-refresh a Review sub-count you have two options:
- **(a) Reuse an existing store key** where one fits (`proposalsTotal` for Propose "ready"; `approved` for approved count). Bind via `x-text` in `subcount`. Cheapest, no backend touch.
- **(b) Add a new count** (dedupe groups, eligible cues, pending tag files, pending renames): server-render it **statically** at fragment render (like `tracklist_search_pending`). This satisfies R-2 (counts-only) and avoids adding new dag keys. Adding a new `dag-seed-*` + dag-dict key + base.html store key is the full-fidelity option but is heavier and only needed if a count must tick every 5s. **Recommendation: prefer static server-render (b-static) for the new Review counts** — matches 59, avoids store surface growth, and the CONTEXT marks OOB ids as discretion.

**Hard rule (R-2):** No `hx-swap-oob` on any diff-row/card container; no `hx-trigger="every Ns"` inside any Review fragment; no `setInterval`. The poll must never re-render the diff list, keeper cards, cue cards, or an in-progress inline-edit/radio subtree.

### Pattern 4: The two NEW thin endpoints (land in `proposals.py`)
```python
# D-02 — server-predicate bulk approve (mirror bulk_update_status at proposal_queries.py:177)
@router.patch("/bulk-approve-high-confidence")   # id-less, threshold-less; predicate is fixed server-side
async def bulk_approve_high_confidence(request, session=Depends(get_session)):
    # server RE-QUERIES at submit — never a client id-list (REVIEW-02)
    stmt = select(RenameProposal.id).where(
        RenameProposal.status == ProposalStatus.PENDING.value,
        RenameProposal.confidence >= 0.9,   # NULL confidence excluded by SQL — see Pitfall 2
    )
    ids = (await session.execute(stmt)).scalars().all()
    count = await bulk_update_status(session, list(ids), ProposalStatus.APPROVED)
    # return count for the confirm/toast + OOB stats

# D-05 — inline edit (mirror update_proposal_status at proposal_queries.py:152; mutate fields not status)
@router.patch("/{proposal_id}/edit")
async def edit_proposal(proposal_id, proposed: str = Form(...), session=Depends(get_session)):
    # update proposed_filename (rename) / proposed_path (move); leave status PENDING; re-render the row
```
> Both are **PATCH** to match the existing verb family. Autoescape all rendered values (never `| safe`) — same DB→render trust boundary the router already documents (T-31-06). Validate the edited `proposed` value server-side (non-empty; the model column is `Text`).

### Anti-Patterns to Avoid
- **`hx-post` to approve/reject/undo** — they are **PATCH**. Use `hx-patch`. (The UI-SPEC sketch shows `hx-post`; it is illustrative, not literal.)
- **`hx-post="/cue/{id}/approve"`** — no such route. Use `POST /cue/{tracklist_id}/generate`.
- **`keeper_id` / `{group_id}` for dedupe** — the route is `POST /duplicates/{group_hash}/resolve` with `canonical_id`.
- **Client-built `proposal_ids` id-list for bulk approve** — the whole point of D-02 is to eliminate this (REVIEW-02).
- **A second poll / `hx-trigger="every"` inside a Review fragment** — violates single-poll R-2.
- **Deleting legacy templates** — supersede-in-place; removal is CUT-02/Phase 62 (keeps the dead-template AST guard green).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Bulk approve selection | Client-side row checkbox accumulation + id-list POST | NEW server-predicate re-query (D-02) | REVIEW-02 correctness; a client id-list goes stale under the poll |
| Audit rows / reversibility | New audit table or undo logic | Existing `ExecutionLog`/`TagWriteLog`/dedupe resolution + `undo`/`undo-all` (D-04) | Append-only trails already exist and are wired |
| Tag before/after diff | Custom comparison | `_build_comparison` + `compute_proposed_tags` (`tags.py`) | Already computes per-field current/proposed/changed |
| Cue eligibility gate | Re-derive "matched tracklist" logic | `_get_eligible_tracklist_query` (`cue.py:31`) | Encodes the approved+EXECUTED+timestamped rule |
| Duplicate scoring / canonical pick | Custom "highest quality" heuristic | `score_group` + `resolve-all` (`duplicates.py:199`) | Auto-keep-highest-quality already implemented |
| Workspace chrome / focus / OOB seeds | New scaffold | `_workspace_scaffold.html` + `_workspace_poll_seeds.html` | Locked 58/59 patterns; guarantees R-5 + focus + seed targets |
| Proposal generation trigger | New enqueue path | `POST /pipeline/proposals` (`:913`) | Deterministic batch keys; dedup-safe |

**Key insight:** Phase 60 adds exactly two thin routes; everything else is *wiring* to code that already handles the hard parts (audit, reversibility, scoring, eligibility). The risk is not missing functionality — it is wiring the UI to the *wrong method/param/path* because the UI-SPEC sketches are illustrative.

## Common Pitfalls

### Pitfall 1: Copying the UI-SPEC sketch HTML literally
**What goes wrong:** `hx-post` on PATCH routes, `/cue/{id}/approve`, `keeper_id`, `{group_id}` — all silently 404/405 or mis-submit.
**How to avoid:** Wire against §Verified References, not the sketch. The sketch conveys layout/behavior; the routes are authoritative in the router files.
**Warning signs:** 405 Method Not Allowed, 404 on cue approve, 422 on dedupe resolve (missing `canonical_id`).

### Pitfall 2: `RenameProposal.confidence` is NULLABLE
**What goes wrong:** `confidence >= 0.9` in SQL excludes rows where confidence IS NULL (NULL comparison → not-true). Files with no confidence score are silently never bulk-approved.
**Why it happens:** The column is `Float, nullable=True`.
**How to avoid:** This is the *conservative-correct* behavior for an irreplaceable archive (D-02 intent) — but make it explicit in the plan and in the confirm-copy count ("{n} match now"). Do not `COALESCE(confidence, 0)` — that would be equally conservative but hides the null case; better to leave NULL-confidence rows for per-file review.

### Pitfall 3: The Tag queue only shows EXECUTED files
**What goes wrong:** `list_tags` filters `FileRecord.state == EXECUTED`. Tag-write review is downstream of Move — a file must be renamed/moved (executed) before its tags appear. An empty tag queue when files are still pending-move is *correct*, not a bug.
**How to avoid:** Mirror the exact query in the `tagwrite` `_render_stage` branch; the empty-state copy already reflects this ("Approve rename proposals to queue moves" upstream).

### Pitfall 4: Dedupe undo is stateful (`file_states` blob)
**What goes wrong:** `undo`/`undo-all` require the `file_states` JSON returned by the *resolve* response. If the workspace re-renders (loses that blob) the undo can't fire.
**How to avoid:** Carry `resolved_file_states` from the resolve response into the resolved card markup (hidden field / `hx-vals`) so UNDO can post it back. Mirror the existing `duplicates/partials/resolve_response.html` → `undo` round-trip.

### Pitfall 5: `oob_counts=True` at initial fragment render
**What goes wrong:** Emitting the OOB "files ready" paragraphs on the stage render collides on duplicate ids with the DAG canvas seeds (documented in `_render_stage` docstring).
**How to avoid:** Keep `oob_counts=False` in every Review `_render_stage` branch; live counts arrive only via the real `/pipeline/stats` swap.

### Pitfall 6: "Approve" ≠ "Applied" for Rename/Move
**What goes wrong:** Per-row APPROVE only sets `RenameProposal.status = APPROVED` (+ `FileRecord.state`). The physical move + `ExecutionLog` happens later via `POST /execution/start` (a batch dispatching ALL approved proposals to agents). There is no per-row "apply".
**How to avoid:** The row lifecycle `pending → approved → applied ✓` derives `applied` from **ExecutionLog presence** (a read concern). Confirm at plan time where `/execution/start` is triggered in the v7.0 shell (likely a Move-workspace or Analyze affordance) — see OQ-4.

## Code Examples

### Verified inline-edit island (Alpine local state, no @alpinejs/focus)
```jinja
{# per-row island — SAVE EDIT targets ONLY its own row (R-6) #}
<div id="rename-row-{{ p.id }}" x-data="{ editing:false, val:'{{ p.proposed_filename|e }}' }">
  <button x-show="!editing" hx-patch="/proposals/{{ p.id }}/approve"
          hx-target="#rename-row-{{ p.id }}" hx-swap="outerHTML">APPROVE</button>
  <button x-show="!editing" @click="editing=true; $nextTick(()=>$refs.edit.focus())">EDIT</button>
  <button x-show="editing"  hx-patch="/proposals/{{ p.id }}/edit"
          hx-include="closest div" hx-target="#rename-row-{{ p.id }}" hx-swap="outerHTML">SAVE EDIT</button>
  <input x-show="editing" x-ref="edit" x-model="val" name="proposed">
</div>
```
Source: adapted from 60-UI-SPEC Pattern 1 with verified verbs/paths (`proposals.py:168`, D-05 `/edit`).

### Verified test pattern (from `tests/test_shell_routes.py`)
```python
@pytest.mark.asyncio
async def test_s_rename_renders_diff(client: AsyncClient) -> None:
    r = await client.get("/s/rename", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text and "<head" not in r.text   # R-5 bare fragment
    assert 'id="stage-workspace"' not in r.text               # fragment, not full shell
```

## State of the Art

Not applicable — no library/version churn this phase. The "current approach" is the locked 58/59 workspace pattern; nothing is deprecated.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The Propose "Model" column should render the configured `settings.llm_model` (`config.py:363`), because `RenameProposal` does NOT persist a per-row model id (`context_used` holds LLM *input* context, not the model name). | Verified References / OQ-2 | Low — a display value; if a per-row model is desired later it needs a schema change (out of scope). Tagged ASSUMED because it changes the UI-SPEC "Model" column semantics. |
| A2 | Cue "approve" returning through the non-`tracklist-` `HX-Target` branch of `generate_cue` renders acceptably in the new workspace. | Verified References (cue) | Medium — must verify the fall-through branch's template; may need the workspace to send `HX-Target` matching an existing branch or accept the default row/toast. |

## Open Questions (RESOLVED)

> **Resolution status (recorded at plan time, 2026-07-01):** OQ-1 is resolved in Plan `60-01` Task 3 — the locked D-03/OQ-1 predicate (below, "≥1 `changed` field AND never blanks an existing tag") is documented verbatim in the tag-bulk route docstring and enforced by `test_tag_bulk_no_discrepancy_predicate`. OQ-2/OQ-3 resolved inline below and implemented in the plans. OQ-4 is **out of Phase 60 scope** (see the OQ-4 note).

1. **OQ-1 (RESOLVED — Plan 60-01/T3) — What is the Tag "no discrepancies" predicate against live code?**
   - What we know: D-03 says bulk-write "pending tag-write files whose comparison has zero discrepancies." But `_build_comparison` (`tags.py:102`) emits a `changed` boolean (pre-write field diff), and `TagWriteLog.discrepancies` (`tag_write_log.py:44`) is a **post-write** re-read mismatch (JSONB) — it does not exist until *after* a write. So there is **no pre-write "discrepancies" signal** to gate on.
   - What's unclear: "zero discrepancies" literally would mean `_count_changes == 0` = *nothing to write* (useless as a bulk-write target).
   - Recommendation: Define the Tag bulk predicate as **pending (EXECUTED, no completed `TagWriteLog`) files whose computed comparison has ≥1 `changed` field AND no field whose proposed value is null-but-current-non-null** (i.e., a bulk write never *erases* an existing tag). Files that would blank an existing tag, or have conflicting/partial proposals, stay per-file Approve/Edit/Skip. This is the conservative "high-confidence" analog and is implementable from existing data. **Confirm this in discuss-phase / at plan time before implementing the Tag bulk endpoint.**

2. **OQ-2 — Propose "Model" column source.** Resolved to `settings.llm_model` (A1). Confirm the planner renders the configured model, not a per-row value.

3. **OQ-3 — Do the new Review sub-counts need live 5s refresh, or is static server-render sufficient?** Recommendation: static server-render (Pattern 3b) for dedupe-groups / eligible-cues / pending-tag / pending-rename; reuse `proposalsTotal`/`approved` store keys where they fit. Matches 59; avoids store surface growth. (CONTEXT marks OOB ids as discretion.)

4. **OQ-4 (OUT OF PHASE 60 SCOPE) — Where is `POST /execution/start` triggered in the v7.0 shell?** Approve only queues; the physical move + ExecutionLog is a separate batch. **Resolution:** Phase 60 delivers the per-row **Approve** affordance only — it sets `status=APPROVED`; it does NOT add or relocate the execution trigger (that affordance is an existing/prior-phase concern, per Pitfall 6 "Approve ≠ Applied"). The `pending → approved → applied ✓` lifecycle's final `applied` transition remains owned outside this phase; no Phase 60 plan wires `/execution/start`. Not a new endpoint — a wiring question deferred to whichever phase owns the "apply approved" batch trigger.

## Environment Availability

No new external dependencies. All work is application code + templates over the running stack (Postgres, Redis, FastAPI). `just tailwind` must be runnable to regenerate `/static/css/app.css` for the new utility classes (build-time only; the glob `@source "../../src/phaze/templates"` already covers `pipeline/partials/`). No probe needed beyond the standard `uv sync` / `just` toolchain already in use.

## Validation Architecture

nyquist_validation is **enabled** (config.json). Framework: **pytest + pytest-asyncio + httpx AsyncClient** (`uv run pytest`). Existing conftest provides an async `client` fixture (see `tests/test_shell_routes.py`, `tests/conftest.py`).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio + httpx AsyncClient |
| Config | `pyproject.toml` (project-standard) |
| Quick run | `uv run pytest tests/test_shell_routes.py -x` |
| Full suite | `uv run pytest --cov --cov-report=term-missing` (85% floor) |

### Phase Requirements → Test Map
| Req | Behavior | Type | Automated command | File |
|-----|----------|------|-------------------|------|
| REVIEW-01 | `/s/rename`, `/s/tagwrite`, `/s/move` render a bare before→after diff fragment with per-row Approve/Edit/Skip | unit (route+template) | `uv run pytest tests/test_review_workspaces.py::test_diff_fragments_bare -x` | ❌ Wave 0 |
| REVIEW-01 | Inline Edit PATCH (`/proposals/{id}/edit`) updates `proposed_filename`/`proposed_path`, returns only the row, leaves status PENDING | unit | `... ::test_edit_patch_updates_row -x` | ❌ Wave 0 |
| REVIEW-02 | Bulk approve **re-queries** `confidence>=0.9` at submit and ignores any client-sent id-list; NULL-confidence rows excluded | unit (behavioral, the correctness core) | `... ::test_bulk_approve_server_predicate -x` | ❌ Wave 0 |
| REVIEW-02 | Tag bulk writes only the OQ-1 predicate set; discrepancy/erasing rows untouched | unit | `... ::test_tag_bulk_no_discrepancy_predicate -x` | ❌ Wave 0 (gated on OQ-1) |
| REVIEW-03 | `/s/dedupe` renders keeper-group cards; radio → `POST /duplicates/{hash}/resolve` (`canonical_id`); resolve-all + undo/undo-all wired | unit | `... ::test_dedupe_keeper_and_undo -x` | ❌ Wave 0 |
| REVIEW-04 | `/s/cue` renders eligible preview cards + APPROVE→`/cue/{id}/generate`; ineligible cards gated (no approve control) | unit | `... ::test_cue_preview_and_gate -x` | ❌ Wave 0 |
| REVIEW-05 | Applying writes exactly one audit row (`ExecutionLog`/`TagWriteLog`/dedupe resolution) and is reversible | integration | `uv run pytest tests/integration/test_review_audit.py -x` | ❌ Wave 0 |
| R-2 (cross-cutting) | Review fragments contain no `<html>`/`<head>`/`<header`, no `hx-trigger="every"`, no `setInterval`, no `hx-swap-oob` on row/card containers | unit (fragment guard) | `... ::test_fragments_single_poll_clean -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_review_workspaces.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (85% floor; pre-commit hooks + mypy strict must pass — never `--no-verify`)
- **Phase gate:** full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_review_workspaces.py` — route+template assertions for all six workspaces + the two new endpoints (covers REVIEW-01..04, R-2)
- [ ] `tests/integration/test_review_audit.py` — one-audit-row-per-apply + reversibility (covers REVIEW-05)
- [ ] Fixtures: seed pending `RenameProposal` rows (mixed confidence incl. NULL), EXECUTED files with metadata for tag comparison, a duplicate group, an eligible + an ineligible cue set. Extend `tests/conftest.py` factories.
- [ ] The server-predicate test (REVIEW-02) MUST assert that a client-supplied `proposal_ids` field is *ignored* and the server re-query drives the result — this is the load-bearing correctness test.

## Security Domain

security_enforcement is not disabled (absent = enabled). Scope is narrow: two new thin routes over unchanged logic, server-rendered HTML.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | **yes** | D-02: no user threshold/id accepted (fixed server predicate) — inherently injection-safe. D-05: validate `proposed` (non-empty string; persisted to a `Text` column via ORM parameter binding — no string interpolation). `proposal_id`/`group_hash`/`tracklist_id` are typed path params (UUID / str) — FastAPI validates. |
| V4 Access Control | yes | Single-user admin tool on a private network (project constraint); same trust boundary as all existing routers. Scope reads by the path id (the routers already do; e.g. `proposal_timeline` scopes by `file_id`, T-31-06-02). |
| V1/V2/V3 (auth/session/crypto) | no | No auth/session/crypto changes; no secrets touched. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Template-path injection via `stage` | Tampering | STAGE_PARTIALS static-literal whitelist (T-57-01) — extend, never interpolate |
| XSS via DB→HTML (filenames, tags, paths, cue text) | Tampering | Jinja2 autoescape; **never `| safe`** on any before/after/file/cue value (router already documents this boundary) |
| Stale-selection mass-apply (REVIEW-02 core) | Tampering/EoP | Server re-queries the predicate at submit; client id-list rejected; counts-only poll (R-2) so selection can't corrupt |
| SQL injection in the bulk predicate | Tampering | SQLAlchemy parameterized `select(...).where(confidence >= 0.9)` — no raw SQL |
| Destructive-red confusion / accidental mass apply | — | Amber-attention + mandatory `hx-confirm` (predicate + live count) + `:disabled` busy-gate (R-4); every apply is reversible (D-04) |

## Sources

### Primary (HIGH confidence)
- Live codebase (read 2026-07-01): `src/phaze/routers/{shell,proposals,tracklists,tags,execution,duplicates,cue,preview,pipeline}.py`; `src/phaze/models/{proposal,execution,tag_write_log}.py`; `src/phaze/services/{proposal_queries,proposal}.py`; `src/phaze/config.py`; `src/phaze/templates/pipeline/partials/{_workspace_scaffold,_workspace_poll_seeds,tracklist_workspace}.html`; `tests/test_shell_routes.py`; `.planning/config.json`.
- `.planning/phases/60-review-apply/60-CONTEXT.md` (LOCKED decisions D-01..D-08).
- `.planning/phases/60-review-apply/60-UI-SPEC.md` (APPROVED UI contract).
- `.planning/ROADMAP.md` §Phase 60 + `.planning/REQUIREMENTS.md` §REVIEW.

### Secondary
- Phase 58/59 PLAN.md + SUMMARY files under `.planning/phases/58-*/` and `.planning/phases/59-*/` (scaffold-fragment precedent).

## Metadata

**Confidence breakdown:**
- Verified references: HIGH — every file:line read against the working tree; corrections noted where the UI-SPEC sketch diverges.
- Architecture patterns: HIGH — the 58/59 scaffold/branch/poll pattern is the direct, verified precedent.
- Pitfalls: HIGH — all six derived from live code (not training data).
- Tag bulk predicate (OQ-1): LOW until resolved — the CONTEXT's "discrepancy" concept has no pre-write source in the live code; needs a plan-time decision.

**Research date:** 2026-07-01
**Valid until:** 2026-07-31 (stable internal codebase; re-verify line numbers if `proposals.py`/`tags.py`/`duplicates.py`/`cue.py` change before planning)
