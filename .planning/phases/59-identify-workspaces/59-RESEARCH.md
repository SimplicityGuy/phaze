# Phase 59: Identify workspaces — Research

**Researched:** 2026-06-30
**Domain:** Presentation-only HTMX/Jinja2 stage-workspace wiring over existing FastAPI routers/services (no backend behavior change)
**Confidence:** HIGH (all claims verified against the live source tree this session)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions (D-01..D-08 — authoritative, do not re-litigate)

- **D-01: Per-engine status badges, no invented score.** Each Track-ID file row shows two badges — `audfprint` and `Panako` — each rendering the file's `FingerprintResult.status` for that engine: **done** (`completed`/`success`), **failed**, or **pending** (no row). Read straight from `fingerprint_results`. No numeric match score exists in the model, so none is fabricated.
- **D-02: Requirement-wording reconciliation.** IDENT-01 "fingerprint match/score" resolves to per-engine **match state** (D-01 badges) + tracklist **confidence** (D-04). `FingerprintResult` persists only `engine` + `status` + `error_message`. Treat "score" as "state"; do NOT build a fingerprint scoring/match-query path (that is deferred IDENT-03).
- **D-03: One combined per-file table** (not two sub-sections). Columns: file · `audfprint` badge · `Panako` badge · tracklist match state · tracklist confidence. Mirrors Phase 58 D-03 to keep the single-poll OOB fanout to one fragment.
- **D-04: Tracklist confidence = linked tracklist's, fallback to best candidate.** Show the linked/auto-linked `Tracklist.match_confidence`; if none linked, show the highest `match_confidence` among candidate tracklists (`list_tracklists` already orders `match_confidence desc nulls_last`).
- **D-05: Three sequential step cards** — Search · Scrape · Match — each showing its existing pending/done count + state, following the Phase 58 Analyze **lane-card visual pattern**. Backed by the existing `scan_search`/`scrape`/`match` task stages + `get_*_pending_*` count helpers. NOT a horizontal stepper.
- **D-06: Per-step ALL trigger buttons; "one surface" = co-located, NOT a chain orchestrator.** Each step card carries its own ALL trigger (SEARCH ALL / SCRAPE ALL / MATCH ALL) wired verbatim to the existing per-step endpoints. There is NO single "run chain" button (no backend endpoint runs all three; adding one would break the no-backend-change rule).
- **D-07: Per-set progress = track-level coverage.** Each set shows **N/M tracks confident** within its linked tracklist, derived from `TracklistTrack.confidence`.
- **D-08: Per-set table below the 3 step cards.** Step cards (aggregate) on top, a table of sets/files below carrying each set's match progress (D-07) + state. Parallels Phase 58 Analyze (lane cards + file table).

### Inherited / carried-forward (do not re-litigate)
- **No backend behavior change** — IA/template rewrite only. No new enqueue, task, mutation, payload, or schema change. (Read-only assembly queries to surface existing data are in scope — see Architecture note below; Phase 58 `get_analyze_stage_files` precedent.)
- **Inert-but-present rows** — stable target id/markup + hover, click UNBOUND in Phase 59 (row-click → record is Phase 61 / RECORD-01).
- **Single-poll discipline** — both workspaces ride the existing `/pipeline/stats` 5s poll + `hx-swap-oob` behind the `oob_counts` gate. Do NOT add a second poll loop.
- **Supersede-in-place** — leave the dead-template AST guard green by superseding the placeholders, not deleting legacy templates (CUT-02 is Phase 62).

### Claude's Discretion
- Exact OOB id additions for the two new workspace fragments (must ride the single `/pipeline/stats` poll + `oob_counts` gate; no second loop). **Research finding: NO new OOB id or store key is required — all needed targets already exist (see Asset Inventory §4).**
- Reuse existing `tracklists/partials/` templates vs. fresh fragments — pick whichever keeps the workspace-table/card contract cleanest. **UI-SPEC locks this: reuse the Phase-58 generic partials (`_workspace_scaffold.html`, `_file_table.html`, `_lane_card.html`); do NOT reuse the legacy `confidence_badge.html`/`status_badge.html` pills (they violate the two-weight + C3 color contract).**
- Empty-state and trigger-response wiring detail (UI-SPEC locks empty-state copy).
- Whether failed-engine `error_message` is surfaced (e.g. tooltip) on Track-ID badges — optional, not required by D-01.

### Deferred Ideas (OUT OF SCOPE)
- **AcoustID acoustic-fingerprint lookup + MusicBrainz recording resolution** — IDENT-03, future milestone. The prototype's "AcoustID→MusicBrainz" Track-ID label and its `IDENTIFY PENDING` trigger are DROPPED (that backend does not exist).
- **Single "run chain" trigger** for Search→Scrape→Match — needs backend orchestration; cut per D-06.
- **Row-click → rich per-file record/pane** — Phase 61 (RECORD-01).
- **Numeric fingerprint match scoring** — depends on the IDENT-03 match-query backend; not buildable presentation-only.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| IDENT-01 | Track-ID workspace shows each file's existing identity signals — audfprint + Panako fingerprint state + rapidfuzz tracklist-match confidence — as match state + confidence. | `FingerprintResult.status` per `(file_id, engine)` (Asset §1) + `Tracklist.match_confidence` linked/best (Asset §2). Rendered as ONE combined table via reused `_file_table.html` (Pattern A). |
| IDENT-02 | Tracklist workspace presents Search→Scrape→Match as a visible 3-step with per-set match progress, triggerable from one surface. | 3 step cards over existing stage map + `get_*_pending_*` helpers (Asset §3) with per-step ALL triggers to existing endpoints (Asset §3); per-set table over `Tracklist`/`TracklistTrack` reads (Pattern C). |
</phase_requirements>

## Summary

Phase 59 replaces the two `_STAGE_PLACEHOLDER` entries (`trackid`, `tracklist`) in `STAGE_PARTIALS` (`src/phaze/routers/shell.py:69-70`) with two redesigned workspace fragments, exactly as Phase 58 did for `discover`/`metadata`/`fingerprint`/`analyze`. Every endpoint, query helper, model, task stage, OOB seed, and store key the two workspaces need **already exists**. This is a pure IA/template wiring job plus (per the Phase 58 `get_analyze_stage_files` precedent) at most two small **read-only** assembly helpers to gather per-row presentation data.

Two findings change the implementation plan materially:

1. **There is no existing single helper that returns the Track-ID row shape** (per-file: audfprint status + Panako status + tracklist match-state + confidence) or the **Tracklist per-set row shape** (per set: match-state + N/M track coverage + linked state). The planner must add read-only assembly — either a new degrade-safe service helper (modeled on `get_analyze_stage_files`, "pure read; no enqueue, no schema change") or inline composition in `_render_stage`'s new `trackid`/`tracklist` branches. This is consistent with "no backend behavior change" (Phase 58 added exactly such read-only SELECT helpers).

2. **The persisted `FingerprintResult.status` values are `"success"` and `"failed"` — NOT `"completed"`.** The badge "done" mapping must match `"success"` (the value actually written by the engine adapters via `put_fingerprint`), tolerating `"completed"` defensively. Do NOT derive the per-engine badge from `get_stage_progress` (which filters `status == "completed"` and would render every engine pending). See Pitfall 1.

**Primary recommendation:** Mirror the Phase 58 `metadata`/`fingerprint` wiring verbatim: add `trackid`/`tracklist` DB-context branches in `_render_stage`, point `STAGE_PARTIALS` at two new fragments composing `_workspace_scaffold.html` + `_file_table.html` (+ `_lane_card.html`-shaped step cards), wire the three existing bulk endpoints with the R-4 guard, and read `FingerprintResult.status == "success"` for the done badge.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Stage routing (`/s/trackid`, `/s/tracklist`) | Frontend Server (FastAPI shell router) | — | `shell.py` already owns whitelisted stage resolution + fragment-vs-full fork (T-57-01). |
| Per-row presentation data assembly | API / Backend (read-only service helper) | Frontend Server (`_render_stage` inline) | Read-only SELECTs over existing tables; no mutation. Phase 58 `get_analyze_stage_files` precedent. |
| Fingerprint + tracklist identity state (the data) | Database (existing tables) | — | `fingerprint_results`, `tracklists`, `tracklist_versions`, `tracklist_tracks` — all already populated by existing tasks. |
| Bulk Search/Scrape/Match triggers | API / Backend (existing endpoints) | — | `POST /pipeline/search-tracklists` / `scrape-tracklists` / `match-tracklists` already exist; wired verbatim. |
| Live count refresh | Frontend Server (single `/pipeline/stats` poll) → Browser ($store.pipeline OOB) | — | Existing chrome poll + `_workspace_poll_seeds.html` targets; no new loop. |
| Step-card counts (render-time) | API / Backend (`get_stage_progress` + `get_*_pending_*`) | — | Server-rendered at fragment render, like Phase 58 k8s `capacity_value`. |

## Standard Stack

No new dependencies. Phase 59 is template + read-only-router work on the existing stack (FastAPI + Jinja2 + HTMX + Alpine + Tailwind v4 pre-compiled). **No `## Package Legitimacy Audit` section applies — this phase installs nothing.** Confirmed against `CLAUDE.md` Technology Stack and the live `src/phaze` tree.

## Verified Asset Inventory (the core deliverable)

> Everything the two workspaces wire. All paths/line numbers verified this session.

### §1 — Track-ID: fingerprint signal (IDENT-01 / D-01)

**Model — `src/phaze/models/fingerprint.py` `FingerprintResult`** [VERIFIED: source]
- Columns: `id`, `file_id` (FK→files.id), `engine` (String(30)), `status` (String(20)), `error_message` (Text, nullable). **No score column.**
- Unique index `ix_fprint_file_engine` on `(file_id, engine)` — exactly one row per file per engine.

**Persisted vocabulary (CRITICAL — verified by tracing the write path):** [VERIFIED: source]
- `engine` values are lowercase **`"audfprint"`** and **`"panako"`** (from `AudfprintAdapter.name` / `PanakoAdapter.name`, `src/phaze/services/fingerprint.py:90,141`). UI label is "Panako" but the **stored value is `"panako"`**.
- `status` values written are **`"success"`** or **`"failed"`** only (`IngestResult(status="success"|"failed")`, `services/fingerprint.py:102-105,153-156`), persisted verbatim by `PUT /api/internal/agent/fingerprints/{file_id}/{engine}` (`routers/agent_fingerprint.py:21-56`, `excluded.status`). The string `"completed"` is **never written** by this path.
- **Badge mapping (D-01):** done ⟺ `status == "success"` (tolerate `"completed"` defensively); failed ⟺ `status == "failed"`; pending ⟺ no row for `(file_id, engine)`.

**Count helper for header sub-count:** `get_fingerprint_progress(session)` → `{total, completed, failed}` (`services/fingerprint.py:256-295`). Note `completed` here = files in `FINGERPRINTED` state (a file-state count), and `failed` = `COUNT(fingerprint_results WHERE status='failed')`. The UI-SPEC sub-count binds `$store.pipeline.fingerprintDone` (already seeded), so this helper is optional.

### §2 — Track-ID: tracklist match state + confidence (IDENT-01 / D-04)

**Model — `src/phaze/models/tracklist.py`** [VERIFIED: source]
- `Tracklist`: `file_id` (nullable FK), `match_confidence` (Integer, nullable — rapidfuzz, per file/candidate), `auto_linked` (Boolean), `status` (String, default `"approved"`), `source` (default `"1001tracklists"`), `latest_version_id` (nullable), `external_id`, `artist`, `event`, `date`.
- `TracklistVersion`: `tracklist_id`, `version_number`, `tracks` relationship.
- `TracklistTrack`: `version_id`, `position`, `confidence` (Float, nullable — basis for D-07 N/M coverage), `artist`/`title`/`label`/`timestamp`.

**Match-state derivation (D-04):** linked ⟺ `Tracklist.file_id IS NOT NULL` → "matched" + show `match_confidence`; else best candidate → "candidate" + highest candidate `match_confidence`; else "no match". The existing ordering `Tracklist.match_confidence.desc().nulls_last()` (`routers/tracklists.py:101,862`) gives "best candidate" for free.

**Aggregate counts helper:** `_get_tracklist_stats(session)` → `{total, matched, unmatched, proposed}` (`routers/tracklists.py:51-62`). `matched` = `COUNT(file_id IS NOT NULL)`. Reusable for the Tracklist header sub-count (the UI-SPEC binds `$store.pipeline.tracklistDone`).

### §3 — Tracklist: 3-step chain (IDENT-02 / D-05 / D-06)

**Task-stage map (`scan_search` / `scrape` / `match`)** — `_NODE_COMPLETED_FNS` in `routers/pipeline.py:85-92` [VERIFIED: source]:
`scan_search → (scan_live_set, search_tracklist)`, `scrape → (scrape_and_store_tracklist,)`, `match → (match_tracklist_to_discogs,)`.

**Done/total counts (render-time, server-rendered)** — `get_stage_progress(session)` (`services/pipeline.py:294-403`) returns per node `{"done": int, "total": int|None}`:
- `scan_search`: done = `COUNT(DISTINCT Tracklist.file_id)`, **total = None** (counter-only — render `done / —`).
- `scrape`: done = `COUNT(DISTINCT TracklistVersion.tracklist_id)`, total = `COUNT(Tracklist)`.
- `match`: done = `COUNT(DISTINCT tracklist_id` reachable from `discogs_links)`, total = `COUNT(Tracklist)`.

**Per-step pending-count helpers** [VERIFIED: source]:
- Search pending = `len(get_untracked_files(session))` (`services/pipeline.py:1183`) — music/video files with no tracklist (the exact set `POST /pipeline/search-tracklists` enqueues).
- Scrape pending = `len(get_scrape_pending_tracklists(session))` (`services/pipeline.py:668-679`) — tracklists with no `tracklist_versions` row.
- Match pending = `len(get_match_pending_tracklists(session))` (`services/pipeline.py:682-699`) — tracklists not reachable from `discogs_links`.

**Trigger endpoints to wire VERBATIM (D-06)** [VERIFIED: source — `routers/pipeline.py`]:

| Card | Method + Route | Pending set | Response partial | Returns into |
|------|----------------|-------------|------------------|--------------|
| 🔎 SEARCH ALL | `POST /pipeline/search-tracklists` (`:1130`) | `get_untracked_files` | `pipeline/partials/trigger_response.html` (action=`"tracklist search"`) | `#…-trigger-response` sink |
| 📄 SCRAPE ALL | `POST /pipeline/scrape-tracklists` (`:1258`) | `get_scrape_pending_tracklists` | `pipeline/partials/trigger_tracklist_response.html` (action=`"scraping"`) | sink |
| 🔗 MATCH ALL | `POST /pipeline/match-tracklists` (`:1289`) | `get_match_pending_tracklists` | `pipeline/partials/trigger_tracklist_response.html` (action=`"matching"`) | sink |

Note the **asymmetry**: Search returns `trigger_response.html` (it shares the file-unit partial), while Scrape/Match return `trigger_tracklist_response.html` (tracklist-unit, no `no_active_agent` branch — both are controller tasks that never raise `NoActiveAgentError`). All three are background-enqueued and idempotent via deterministic keys (double-click safe), but the R-4 confirm + busy-disable guard is still required.

### §4 — Shell wiring the workspaces ride (Phase 57/58 contracts)

- **`STAGE_PARTIALS`** (`routers/shell.py:55-77`): replace `"trackid": _STAGE_PLACEHOLDER` and `"tracklist": _STAGE_PLACEHOLDER` with two new static-literal partial paths (T-57-01: `stage` is never spliced into a path). [VERIFIED: source]
- **`_render_stage`** (`routers/shell.py:80-133`): add `elif stage == "trackid":` and `elif stage == "tracklist":` branches that inject the read-only DB context — exactly the pattern the `metadata`/`fingerprint` branches (`:116-129`) use. `oob_counts` stays `False` on the stage render. [VERIFIED: source]
- **Fragment-vs-full fork** (`:131-133`): `HX-Request: true` → `shell/_stage_fragment.html` (`{% include stage_partial %}`, bare); else full `shell/shell.html`. Both render byte-identical center content. [VERIFIED: source]
- **Reusable Phase-58 partials** [VERIFIED: source — `src/phaze/templates/pipeline/partials/`]:
  - `_workspace_scaffold.html` — `{% macro workspace(title, subcount='', actions='', x_data='', cloud_cards=false) %}`; emits exactly ONE `<h1 tabindex="-1">` focus target, optional `subcount` (x-text JS expr against `$store.pipeline`), `actions` slot, body via `caller()`, and always includes `_workspace_poll_seeds.html`. Import as `ws`, `{% call ws.workspace(...) %}`.
  - `_file_table.html` — generic table. Context: `columns` (list[str]); `rows` (list of cell-dict lists `{text, mono?, title?, color?}` — `text` is ALWAYS autoescaped, never `| safe`); `empty_heading`/`empty_body`; `row_id_prefix` (inert-but-present rows: `cursor-pointer` + stable id, click UNBOUND); optional `table_id`.
  - `_lane_card.html` — the visual template Pattern B (step cards) follows: `rounded-xl border bg-…panel p-4`, title row + mono `font-medium` numeral. Pattern B extends it with a per-step ALL trigger button.
  - `_workspace_poll_seeds.html` — hidden OOB seed-target host. **Already provides every target Phase 59 needs:** `dag-seed-searchBusy` (`:34`), `dag-seed-scrapeBusy`/`dag-seed-matchBusy` (`:41-42`), `dag-seed-tracklistDone` (`:50`), `dag-seed-scrapeDone`/`Total` (`:51-52`), `dag-seed-matchDone`/`Total` (`:53-54`), `dag-seed-fingerprintDone` (`:45`). **No new seed id required.**
- **Shell store seeds** (`shell/shell.html:105-125`): `Alpine.store('pipeline', {...})` already seeds `searchBusy:0` (`:113`), `scrapeBusy:0, matchBusy:0` (`:119`), `fingerprintDone:0` (`:121`), `tracklistDone:0` (`:123`), `scrapeDone/Total` (`:124`), `matchDone/Total` (`:125`). **No new store key required — no undefined-flash risk** (unlike Phase 58's `notYetEnriched`/`computeOnline`). [VERIFIED: source]
- **Single poll**: the one `hx-get="/pipeline/stats"` element lives in shell chrome; `pipeline_stats_partial` (`routers/pipeline.py:604-678`) re-pushes `dag_ctx` (which includes `searchBusy`/`scrapeBusy`/`matchBusy`/`tracklistDone`/`scrapeDone`/`matchDone` via `_build_dag_context:164-226`) OOB behind `oob_counts=True`. [VERIFIED: source]

### §5 — Existing test patterns to mirror

- **`tests/test_enrich_analyze_workspaces.py`** — the Phase 58 model to copy. Async httpx `client` fixture; `/s/<stage>` HX-fragment assertions; module-level `_seed_*` ORM helpers (inserts only, no backend change); `test_stage_fragment_is_bare`, `test_single_poll_discipline`, per-workspace behavioral tests. Asserts: exactly one `tabindex="-1"`; verbatim `hx-post="…"` endpoints; `hx-confirm` + `$store.pipeline.<busy>` guard; absence of `EXTRACT SELECTED`/`type="checkbox"`; **no** `hx-trigger="every"` / `setInterval`; OOB seed-id presence; scoped table-body substring assertions via `body.index('id="…-file-table"')`.
- **`tests/test_dead_template_guard.py`** — `test_no_orphan_templates`: every `templates/**/*.html` must be reachable from a router via the `extends`/`include`/`import` closure. New fragments become reachable the moment `STAGE_PARTIALS` points at them. **Supersede-in-place**: do NOT delete the legacy `tracklists/partials/*` templates (still reachable via `routers/tracklists.py`); removal is CUT-02/Phase 62. Adding new partials is safe; the guard stays green automatically.
- **`tests/test_shell_routes.py`** — `_RAIL_STAGES` includes `trackid`/`tracklist`; `test_rail_nodes_wired` already asserts `hx-get="/s/trackid"` and `hx-get="/s/tracklist"` exist (rail wiring is Phase 57, unchanged). `test_stage_fragment_is_bare` is the bare-fragment contract.

## Architecture Patterns

### Data flow (per workspace)

```
rail node click ─hx-get /s/<stage>─▶ shell.shell_stage()
                                       └▶ _render_stage(stage)
                                            ├ STAGE_PARTIALS[stage] → fragment path
                                            ├ DB context branch (read-only assembly):
                                            │    trackid  → per-file: fp status (audfprint/panako) + tracklist match/conf
                                            │    tracklist→ step counts (done/total/pending) + per-set rows (N/M coverage)
                                            └ HX? → _stage_fragment.html ({% include stage_partial %})  [bare]
                                                else→ shell.html (full chrome)
                                       ▼
                         fragment composes ws.workspace(...) + _file_table.html (+ step cards)
                                       ▼
   live counts ◀─ single /pipeline/stats 5s poll ─▶ stats_bar.html OOB ─▶ _workspace_poll_seeds targets ─▶ $store.pipeline
   triggers   ── SEARCH/SCRAPE/MATCH ALL ─▶ existing POST endpoints ─▶ trigger(_tracklist)_response.html ─▶ local sink
```

### Pattern A — Track-ID combined table (D-01/D-03/D-04)
Compose `_workspace_scaffold.html` (title `TRACK-ID`, sub-count bound to `$store.pipeline.fingerprintDone`/`tracklistDone`, **empty actions slot** — read-only view, no trigger). Body = ONE `_file_table.html` with columns `["File","audfprint","Panako","Tracklist","Confidence"]`, `row_id_prefix="trackid-row"`. Each cell is a `{text, color}` dict; status cells carry a word + color (never hue-only). Rows inert (no `hx-get`).

### Pattern B — Tracklist step cards (D-05/D-06)
`grid grid-cols-3 gap-4 p-6` of three `_lane_card.html`-shaped cards (Search/Scrape/Match), each with: title (`N · 🔎 SEARCH`), server-rendered `done/total` (or `done / —` for Search), live busy pill `x-show="$store.pipeline.{search|scrape|match}Busy > 0"`, and an ALL trigger button (`h-9 px-3` secondary style) with R-4 guard posting to the existing endpoint into a local `*-trigger-response` sink.

### Pattern C — Tracklist per-set table (D-07/D-08)
Below the step cards: one `_file_table.html` with columns `["Set","Tracklist","Tracks","Matched to file"]`, `row_id_prefix="tracklist-row"`. "Tracks" cell = mono `N/M` from `TracklistTrack.confidence` over the set's linked tracklist (full=emerald, partial=blue, none="—" gray).

### Anti-Patterns to Avoid
- **Reusing `tracklists/partials/confidence_badge.html` / `status_badge.html`** — they render `font-semibold` green/yellow/red pills, violating the two-weight + C3 emerald/amber/blue/gray contract. Render status as colored words / mono numerals through `_file_table.html` cells instead (UI-SPEC Typography).
- **Adding a second poll loop** (`hx-trigger="every"` / `setInterval`) inside either fragment — violates R-2/WORK-05. All live values ride the one chrome poll.
- **Deriving the per-engine done badge from `get_stage_progress`** (`status=='completed'`) — would render every engine "pending". Read `FingerprintResult.status` directly (`=='success'`).
- **Building a "run chain" trigger or per-file subset enqueue** — needs new backend; cut per D-06.
- **Emitting a fragment containing `<html>`/`<head>`/`<header`/`{% extends %}`** — breaks R-5 + the bare-fragment guard.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Workspace header + focus target + OOB seed host | Custom `<section>`/`<h1>` markup | `_workspace_scaffold.html` macro | Guarantees one `tabindex="-1"`, seeds host, secondary-button contract. |
| File/queue table | Custom `<table>` | `_file_table.html` | Autoescape trust boundary, inert-row contract, empty-state, C3 tokens. |
| Step card visual | New stepper component | `_lane_card.html` shape | D-05 explicitly chose cards over a stepper (not in design system). |
| Pending counts | New SQL | `get_untracked_files` / `get_scrape_pending_tracklists` / `get_match_pending_tracklists` | Already the exact complement of each stage's `done`; shared with the triggers (anti-drift). |
| Bulk triggers | New endpoints | `POST /pipeline/{search-tracklists,scrape-tracklists,match-tracklists}` | Exist, idempotent, deterministic-keyed, Phase-30 routed. |
| Best-candidate ordering | New ranking | `match_confidence.desc().nulls_last()` | Already the ordering `list_tracklists` uses (D-04). |

**Key insight:** The only genuinely new code is read-only row assembly (Track-ID per-file shape; Tracklist per-set shape). Everything else is composition of existing, tested parts.

## Runtime State Inventory

Not applicable — Phase 59 is a presentation/template phase with no rename, migration, or data mutation. **No stored data, live service config, OS-registered state, secrets/env vars, or build artifacts are touched. Verified: no enqueue, no `session.commit`, no schema change in scope.**

## Common Pitfalls

### Pitfall 1: Fingerprint status vocabulary mismatch
**What goes wrong:** Badge logic keys "done" on `"completed"` (matching `get_stage_progress`), so every engine renders "pending" even for fully-fingerprinted files.
**Why:** Engine adapters write `IngestResult(status="success"|"failed")`; `put_fingerprint` persists verbatim. `"completed"` is never written by this path; `get_stage_progress` filters `status=='completed'` (a separate, possibly-latent concern, out of scope to fix).
**How to avoid:** Map done ⟺ `status == "success"` (tolerate `"completed"`), failed ⟺ `"failed"`, pending ⟺ no row. Use engine values `"audfprint"`/`"panako"` (lowercase) as the query keys.
**Warning signs:** A test that seeds `FingerprintResult(status="completed")` passes but a real-data file shows all-pending.

### Pitfall 2: Per-engine pending = absence of a row
**What goes wrong:** Treating a missing `(file_id, engine)` row as an error or skipping the file.
**Why:** D-01 defines pending as the *absence* of a `FingerprintResult` row for that engine (the unique `(file_id, engine)` index means at most one row).
**How to avoid:** LEFT-join / dict-lookup per engine; default to "pending" when no row.

### Pitfall 3: OOB target / store-key assumptions
**What goes wrong:** Adding a new `dag-seed-*` placeholder or a new `$store.pipeline` key for the step cards.
**Why:** All needed keys (`searchBusy`/`scrapeBusy`/`matchBusy`/`tracklistDone`/`scrapeDone`/`Total`/`matchDone`/`Total`/`fingerprintDone`) are already seeded in `shell.html` and already have OOB targets in `_workspace_poll_seeds.html`. Step counts are server-rendered, not store-bound.
**How to avoid:** Bind busy pills to existing keys; render done/total/pending server-side. Add nothing to the seed host or store.

### Pitfall 4: Search vs Scrape/Match response partial asymmetry
**What goes wrong:** Pointing SCRAPE/MATCH at `trigger_response.html` (which expects `no_active_agent` + file-unit copy) or SEARCH at `trigger_tracklist_response.html`.
**Why:** Search uses `trigger_response.html` (action=`"tracklist search"`); Scrape/Match use `trigger_tracklist_response.html` (tracklist-unit, no agent branch).
**How to avoid:** Match each trigger's `hx-target` to its endpoint's actual response partial (Asset §3 table). The endpoints already pick the partial; the fragment just needs a sink id per card.

### Pitfall 5: `_render_stage` had no DB context for these stages
**What goes wrong:** Assuming `trackid`/`tracklist` already carry context (they don't — both are `_STAGE_PLACEHOLDER`, which needs only `stage`).
**How to avoid:** Add explicit `elif` branches injecting the read-only assembled context, mirroring the `metadata`/`fingerprint` branches (`shell.py:116-129`). This is the Phase-58 "Pitfall 5" recurrence noted in CONTEXT.

## Code Examples

### `_render_stage` branch (mirror metadata/fingerprint, `shell.py:116-129`)
```python
# Source: routers/shell.py:116-129 (existing pattern to mirror)
elif stage == "metadata":
    context["metadata_files"] = await get_metadata_pending_files(session)
elif stage == "fingerprint":
    context["fingerprint_files"] = await get_fingerprint_pending_files(session)
# Phase 59 adds (read-only assembly — no enqueue, no commit):
#   elif stage == "trackid":   context["trackid_files"] = await get_trackid_stage_files(session)
#   elif stage == "tracklist": context["tracklist_steps"], context["tracklist_sets"] = ...
```

### `_file_table.html` cell contract (`_file_table.html:9-23`)
```jinja
{# rows = list of [ {text, mono?, title?, color?}, ... ];  text ALWAYS autoescaped #}
{% set _ = ns.rows.append([
    {'text': f.filename, 'mono': True, 'title': f.path},   {# helper row keys: filename / path (match Plan 01 dict shape) #}
    {'text': af_word, 'color': af_color},      {# audfprint status word #}
    {'text': pk_word, 'color': pk_color},      {# panako status word #}
    {'text': match_word, 'color': match_color},
    {'text': conf_text, 'mono': True, 'color': conf_color},
]) %}
```

### Verbatim ALL-trigger button with R-4 guard (from `fingerprint_workspace.html:25-31`)
```jinja
<button type="button"
        hx-post="/pipeline/scrape-tracklists" hx-target="#scrape-trigger-response" hx-swap="innerHTML"
        hx-confirm="Enqueue scraping for all pending tracklists?"
        :disabled="$store.pipeline.scrapeBusy > 0"
        class="{{ _btn }}">SCRAPE ALL</button>
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Tracklist management on `/tracklists/` legacy page (`tracklists/list.html` + `confidence_badge`/`status_badge` pills) | v7.0 shell stage workspace at `/s/tracklist` (C3 status words via `_file_table.html`) | Phase 59 | Legacy page stays reachable (redirect at `tracklists.py:89-90`) until CUT-02; new fragment supersedes-in-place. |
| Per-engine fingerprint state shown only implicitly via state machine | Surfaced per-file as audfprint/Panako badges | Phase 59 | First UI surface of `FingerprintResult.status` per engine. |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | A new read-only assembly helper (Track-ID rows / Tracklist per-set rows) is within "no backend behavior change" scope, per the Phase-58 `get_analyze_stage_files` precedent. | Summary / Arch Map | If the planner reads "no new query paths" strictly, assembly must be done inline in `_render_stage` instead of a service helper. Either is read-only; outcome equivalent. Recommend confirming the helper-vs-inline choice at plan time (does not change behavior). |

*All other claims are `[VERIFIED: source]` against the live tree this session.*

## Open Questions (RESOLVED)

1. **Helper vs inline assembly for the two new row shapes.**
   - Known: no existing helper returns either shape; both are pure read-only SELECTs over existing tables.
   - Unclear: whether to add `get_trackid_stage_files` / `get_tracklist_set_rows` service helpers (degrade-safe, unit-testable, `get_analyze_stage_files` precedent) or compose inline in `_render_stage`.
   - Recommendation: **add degrade-safe service helpers** (matches Phase 58, gives unit-testable read paths for the Validation Architecture), but flag as A1 — either satisfies the no-behavior-change rule.
   - RESOLVED: service helpers chosen — Plan 59-01 creates both `get_trackid_stage_files` and `get_tracklist_set_rows` in `src/phaze/services/pipeline.py`.

2. **Which file set scopes the Track-ID table?**
   - Known: Phase 58 Fingerprint shows the *pending* queue; Track-ID is a *consolidated identity view* over files that carry a fingerprint or tracklist signal.
   - Unclear: exact membership (all music/video files? only files with ≥1 `FingerprintResult` or a tracklist?). UI-SPEC empty-state copy ("No discovered files carry a fingerprint or tracklist signal yet") implies the signal-bearing set.
   - Recommendation: scope to music/video files that have at least one `FingerprintResult` row OR a linked/candidate `Tracklist`; confirm against UI-SPEC empty-state intent at plan time. Read-only either way.
   - RESOLVED: scoped to music/video files with ≥1 `FingerprintResult` OR a linked/candidate `Tracklist` — implemented in Plan 59-01 Task 2 (`get_trackid_stage_files`).

## Environment Availability

Not applicable — no external tools, services, or runtimes are introduced. All work is in-repo Python/Jinja within the existing FastAPI app; tests run under the existing `uv run pytest` harness.

## Validation Architecture

> nyquist_validation is enabled (no `workflow.nyquist_validation: false` found). This section is REQUIRED.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (async httpx `AsyncClient` fixture; in-repo Postgres-backed `session` fixture) |
| Config file | `pyproject.toml` (`[tool.pytest...]`); per-project Codecov flags |
| Quick run command | `uv run pytest tests/test_identify_workspaces.py -x` (new file, mirrors `test_enrich_analyze_workspaces.py`) |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (≥85% coverage gate) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Automated Command | File Exists? |
|-----|----------|-----------|-------------------|-------------|
| R-5 | `/s/trackid` & `/s/tracklist` HX responses are bare fragments (no `<html>`/`<head>`) | router/render | `uv run pytest tests/test_identify_workspaces.py::test_identify_fragments_are_bare -x` | ❌ Wave 0 |
| WORK-05/R-2 | neither fragment carries `hx-trigger="every"` / `setInterval`; shell still fires exactly one `/pipeline/stats` | router/render | `…::test_identify_single_poll_discipline -x` | ❌ Wave 0 |
| IDENT-01 | Track-ID table renders audfprint/Panako status words from `FingerprintResult.status` (done⟸"success", failed, pending⟸no row) + tracklist match/confidence; one combined table; inert rows | router + seeded ORM | `…::test_trackid_table_signals -x` | ❌ Wave 0 |
| IDENT-01 (neg) | a file with `status="success"` rows renders "done" (NOT "pending") — guards Pitfall 1 | router + seeded ORM | `…::test_trackid_success_renders_done -x` | ❌ Wave 0 |
| IDENT-02 | three step cards (Search/Scrape/Match) with done/total + pending counts; per-step ALL buttons post to the three existing endpoints with R-4 (`hx-confirm` + `:disabled` on `*Busy`); no chain button | router/render | `…::test_tracklist_step_cards_and_triggers -x` | ❌ Wave 0 |
| IDENT-02 | per-set table renders N/M track coverage from `TracklistTrack.confidence`; inert rows | router + seeded ORM | `…::test_tracklist_per_set_coverage -x` | ❌ Wave 0 |
| Dead-template | new fragments reachable; no legacy template deleted | AST guard | `uv run pytest tests/test_dead_template_guard.py -x` | ✅ exists |
| Rail wiring | `/s/trackid` & `/s/tracklist` still reachable; bare-fragment contract | router | `uv run pytest tests/test_shell_routes.py -x` | ✅ exists |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_identify_workspaces.py tests/test_dead_template_guard.py -x`
- **Per wave merge:** `uv run pytest tests/test_identify_workspaces.py tests/test_shell_routes.py tests/test_dead_template_guard.py`
- **Phase gate:** full suite green + ≥85% coverage before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_identify_workspaces.py` — new file mirroring `test_enrich_analyze_workspaces.py` (async `client`; module-level `_seed_file` / `_seed_fingerprint_result(file_id, engine, status)` / `_seed_tracklist(file_id, match_confidence, …)` / `_seed_tracklist_track(version_id, confidence)` ORM helpers, inserts only). Covers IDENT-01, IDENT-02, R-5, WORK-05/R-2.
- [ ] (If a service helper is chosen, A1) unit tests for `get_trackid_stage_files` / `get_tracklist_set_rows` degrade-safety (return `[]` on DB error) — mirror existing `services/pipeline.py` `_safe_count` test style.
- No framework install needed — pytest/pytest-asyncio + fixtures already present.

## Security Domain

> `security_enforcement` default-enabled (no `false` in config found). Phase 59 is read-only presentation; the relevant surface is narrow.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation / Output Encoding | **yes** | All DB-sourced cell text (`original_filename`, `original_path`, artist/title) rendered via `_file_table.html` autoescape — **never `| safe`** (the partial enforces this; DB→render trust boundary). |
| V5 Path/template injection | **yes** | `stage` is matched against the static `STAGE_PARTIALS` whitelist and never spliced into a template path (T-57-01); the two new entries are static string literals. |
| V2/V3/V4 Auth/Session/Access | no | No auth/session/access-control change; trigger endpoints already exist with their own routing/guards. |
| V6 Cryptography | no | None introduced. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Stored XSS via filename/artist/path in a table cell | Tampering/Info-disclosure | `_file_table.html` autoescape; no `| safe`; status words are caller-controlled class strings (not user data). |
| Template-path injection via `stage` | Tampering | Static `STAGE_PARTIALS` whitelist; unknown stage 404s (`shell.py:149-151`). |
| Double bulk-enqueue (over-enqueue / Phase-34 doubling) | DoS / resource | R-4 guard (`hx-confirm` + `:disabled` on `*Busy`) on all three ALL triggers; endpoints are deterministic-keyed + idempotent. |
| ORM operator injection in pending-count queries | Tampering | Existing helpers use pure ORM `~exists`/`.not_in(subquery)` with no interpolated operator input (T-41-01); reused verbatim. |

## Sources

### Primary (HIGH confidence — verified this session)
- `src/phaze/routers/shell.py` — `STAGE_PARTIALS`, `_render_stage`, fragment fork.
- `src/phaze/models/fingerprint.py`, `src/phaze/models/tracklist.py` — column shapes.
- `src/phaze/services/fingerprint.py` (adapters `status="success"|"failed"`, names `audfprint`/`panako`), `src/phaze/routers/agent_fingerprint.py` (verbatim status persistence) — Pitfall 1 chain.
- `src/phaze/services/pipeline.py` — `get_stage_progress`, `get_scrape_pending_tracklists`, `get_match_pending_tracklists`, `get_untracked_files`.
- `src/phaze/routers/pipeline.py` — `_NODE_COMPLETED_FNS`, `/pipeline/{search,scrape,match}-tracklists`, `_build_dag_context`, `pipeline_stats_partial`.
- `src/phaze/routers/tracklists.py` — `_get_tracklist_stats`, `list_tracklists` ordering, `get_tracks`.
- `src/phaze/templates/pipeline/partials/{_workspace_scaffold,_file_table,_lane_card,_workspace_poll_seeds,fingerprint_workspace,analyze_workspace,trigger_tracklist_response}.html` — reuse contract.
- `src/phaze/templates/shell/{shell.html,_stage_fragment.html,partials/_stage_placeholder.html}` — store seeds + fragment include.
- `tests/{test_enrich_analyze_workspaces,test_dead_template_guard,test_shell_routes}.py` — test patterns to mirror.
- `.planning/phases/59-identify-workspaces/{59-CONTEXT,59-UI-SPEC}.md`, `.planning/REQUIREMENTS.md` — decisions + requirements.

### Secondary / Tertiary
- None required — no external libraries or web sources; all findings are in-repo and tool-verified.

## Metadata

**Confidence breakdown:**
- Asset inventory (endpoints/models/helpers/templates): **HIGH** — every path + line verified in source.
- Fingerprint status vocabulary (Pitfall 1): **HIGH** — traced adapter → API handler → DB write.
- Architecture (helper-vs-inline assembly): **MEDIUM** — read-only either way; flagged A1/Open-Q1.
- Track-ID file-set membership: **MEDIUM** — flagged Open-Q2 (confirm against UI-SPEC empty-state at plan time).

**Research date:** 2026-06-30
**Valid until:** ~2026-07-30 (stable; in-repo source, no fast-moving external deps).
