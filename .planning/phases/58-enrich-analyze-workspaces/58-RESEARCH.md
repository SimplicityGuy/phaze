# Phase 58: Enrich + Analyze workspaces - Research

**Researched:** 2026-06-29
**Domain:** Server-rendered FastAPI + Jinja2 + HTMX + Alpine UI presentation over existing pipeline routers (NO backend behavior change)
**Confidence:** HIGH (all sources are in-repo code, read this session)

<user_constraints>
## User Constraints (from 58-CONTEXT.md)

### Locked Decisions
- **D-01: ALL-only triggers.** Ship only `EXTRACT ALL` / `FINGERPRINT ALL`, wired verbatim to existing `POST /pipeline/extract-metadata` (`pipeline.py:959`) and `POST /pipeline/fingerprint` (`pipeline.py:1045`). Those enqueue ALL pending files via `get_metadata_pending_files` / `get_fingerprint_pending_files` (no file-id subset param). Zero backend change.
- **D-02: Drop `EXTRACT SELECTED` and ALL per-file row-selection/checkbox state from Phase 58.** It would require a new subset-enqueue endpoint (new query path + payload validation) → bends the no-backend-change rule. **Planner reconciliation required:** add a one-line note to 58-UI-SPEC.md recording `EXTRACT SELECTED` + row-checkboxes are deferred. No row-selection state anywhere in Phase 58.
- **D-03: One table of ALL in-stage Analyze files** below the three lane cards (queued · running · awaiting-cloud · done), NOT only in-flight and NOT per-lane mini-tables. Each row carries a status column + a local/A1/k8s lane badge (UI-SPEC Pattern 4).
- **D-04: Windowed progress = a simple %/windows-done bar** derived from existing `analysis_window` rows (Phase 31). No inline BPM sparkline, no multi-lane timeline. Satisfied by the bar + lane badge.
- **D-05: Always render all three lane cards; label the unavailable state.** A down lane renders greyed with an explicit label + 0 capacity: `offline` (A1 has no online compute agent) or `not configured` (no k8s/Kueue set up). Do NOT hide down/unconfigured lanes. *Planner discretion:* whether to copy-distinguish `offline` (configured-but-down) from `not configured` (never set up).
- **D-06: Inert-but-present rows (strict R-1).** Rows ship the stable target id/markup + hover affordance, but the click is UNBOUND in Phase 58 — no selected-state, no placeholder pane, no record fetch. Row-click → per-file record is Phase 61 (RECORD-01).

### Claude's Discretion
- Discover "recent scans" surface (WORK-01): reuse existing `recent_scans_table.html` + `pipeline_scans.py` data, restyled to the C3 workspace-table pattern. Sensible default = reuse + restyle.
- Reuse of v6.0 cloud-state partials (`admission_state_card.html`, `analyzing_cloud_card.html`, `awaiting_cloud_card.html`) for k8s/A1 lane sub-states vs. fresh markup — pick whichever keeps the lane-card capacity contract cleanest.
- Exact OOB id additions for the new workspace fragments (must ride the single `/pipeline/stats` poll + `oob_counts` gate; no second poll loop).
- Empty-state and trigger-response wiring detail (UI-SPEC already locks empty-state copy).

### Deferred Ideas (OUT OF SCOPE)
- Per-file selection for Metadata/Fingerprint (`EXTRACT SELECTED` + checkboxes + subset-enqueue endpoint) — needs a backend endpoint change.
- Inline BPM sparkline / multi-lane windowed timeline per file — Phase 61 (RECORD-01).
- Row-click → rich per-file record/pane — Phase 61 (RECORD-01).
- WORK-06 (cloud_phase admission-state cards as Analyze-lane sub-states) — future/deferred.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| WORK-01 | Discover shows recent scans + count of discovered-but-not-yet-enriched, with scan trigger | Reuse `recent_scans_table.html` (fed by `build_recent_scans`, `pipeline_scans.py:229`); "discovered" count = `$store.pipeline.discovered`; "not yet enriched" derivable from `discovered − metadataExtracted` (read-only) or a new derived seed |
| WORK-02 | Metadata/Fingerprint show stage queue + manual trigger over existing endpoints | `POST /pipeline/extract-metadata` (`:959`), `POST /pipeline/fingerprint` (`:1045`); both return `trigger_response.html`; queues read `get_metadata_pending_files`/`get_fingerprint_pending_files`. Queue listing reads `get_files_by_state` |
| WORK-03 | Analyze: 3 lane cards (local/A1/k8s) w/ live capacity + Kueue quota-wait vs Inadmissible | local ← `agentOnline`/`analyzeBusy`/`analyzeActive` store keys (aggregate; needs kind-split derivation); A1 ← cloud-window counts; k8s ← cloud_phase counts + `inadmissible_card.html`/`localqueue_card.html`; "not configured" ← `settings.cloud_target` read |
| WORK-04 | Each in-flight Analyze file shows lane + windowed progress | Window coverage = `analysis.fine_windows_analyzed/fine_windows_total` (aggregate columns). **Post-57.1 (PR #184, MERGED):** `fine_windows_analyzed` increments DURING flight, so in-flight rows show a live `N/M windows` indicator + `running`; completed rows show full coverage (see Pitfall 1) |
| WORK-05 | Stage workspaces refresh live via the existing stats-poll (no manual reload) | Single `/pipeline/stats` 5s poll fanned out via `hx-swap-oob` behind `oob_counts` gate (`stats_bar.html`); poll element lives in persistent shell chrome, not in any swappable fragment |
</phase_requirements>

## Summary

Phase 58 is a **pure presentation/IA rewrite** — four redesigned stage workspaces rendered as content-only fragments into the Phase-57 `#stage-workspace` swap target, over the *existing* routers, services, endpoints, partials, and `/pipeline/stats` poll. There are **no new dependencies and no new backend behavior**. The entire research value is the precise data-shape → endpoint → partial → store-key mapping the planner must wire, plus the read-only derivations that are permitted (in the route/template context) versus the backend changes that are forbidden.

Three findings dominate planning:

1. **The v6.0 cloud lane data is NOT in `$store.pipeline`.** Unlike the DAG-node counts (which ride OOB `x-init` store writes via the `dag` dict), the A1/k8s cloud counts (`awaiting_cloud`, `pushing`, `analyzing_cloud`, `queued_behind_quota`/`admitted`/`running`/`finished`, `inadmissible`, `localqueue_unreachable`) are surfaced as **pre-rendered card partials swapped whole via `hx-swap-oob`** (id-per-card). The lane cards (WORK-03) therefore have two clean reuse paths: (a) place the existing card partials below/within the lane grid (they already ride the poll), and/or (b) add **new derived `dag`-dict seed keys** in the stats context builder for the lane-card capacity numerals. Both are read-only and allowed; (b) is required if a lane numeral must bind via `x-text` to `$store.pipeline`.

2. **Windowed progress (WORK-04) IS live mid-flight (post-57.1).** Phase 57.1 (PR #184, MERGED to main) added incremental window persistence: `analysis.fine_windows_analyzed` now increments DURING flight (< `fine_windows_total`), not only atomically at completion. An in-flight file HAS an `analysis` row carrying a partial coverage count. The Analyze table MUST render this live `N/M windows` indicator alongside `running` for in-flight rows, and full coverage for completed (ANALYZED) rows. Honor D-04 by reading the aggregate columns and rendering the mid-flight count — do NOT collapse in-flight rows to a bare `running` (that is the superseded pre-57.1 behavior). Phase 58 only READS this signal; no schema/query change.

3. **Per-file lane and "not configured" are derivations, not stored columns.** There is no `cloud_target` column on the file model — `cloud_target` is a single deployment-wide setting (`config.py:406`, `Literal["local","a1","k8s"]`). Per-file lane is derived from the `cloud_job` sidecar (no row → local; row with `cloud_phase IS NULL` → A1; row with `cloud_phase` set → k8s). "Not configured" lane state is derived from `settings.cloud_target`. All read-only.

**Primary recommendation:** Build a shared workspace-scaffold partial + a file-table partial, then four thin stage fragments. Reuse the existing cloud card partials verbatim under the Analyze lane grid. For lane-card capacity numerals that must be reactive, extend the existing `dag` dict in `_build_dag_context` with new server-computed int keys (kind-split agent counts, k8s admission counts) — a read-only context derivation that rides the existing OOB loop with zero new poll and zero new backend semantics. Wire Metadata/Fingerprint ALL buttons verbatim to the two existing trigger endpoints. Keep rows inert (D-06).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Stage workspace fragment render | Frontend Server (FastAPI route + Jinja2) | — | `/s/<stage>` already forks fragment-vs-shell in `shell.py`; Phase 58 swaps placeholder partials for real workspace partials |
| Live count refresh | Frontend Server (`/pipeline/stats` route) → Browser (Alpine `$store.pipeline`) | — | Single existing poll fans out via OOB; no client timers |
| Lane capacity / cloud state derivation | API/Backend service reads (existing, degrade-safe) | Frontend Server (context assembly) | All reads already exist in `services/pipeline.py`; Phase 58 only assembles + presents them |
| Manual trigger (Extract/Fingerprint ALL) | API/Backend (existing enqueue endpoints) | — | `POST /pipeline/extract-metadata` / `/pipeline/fingerprint` enqueue ALL pending; no change |
| Windowed progress | Database (`analysis` aggregate columns) | Frontend Server (present) | Post-57.1 the aggregate increments mid-flight; read-only present (in-flight N/M + completed full coverage) |
| Row-click → record | — (DEFERRED Phase 61) | — | D-06: inert rows only |

## Standard Stack

**No new dependencies.** Phase 58 uses the already-installed, already-configured stack (CLAUDE.md). Verified present and in use in the repo this session:

| Library | Role in this phase | Source |
|---------|--------------------|--------|
| FastAPI + `Jinja2Templates` | Stage routes return `TemplateResponse` fragments | `shell.py`, `pipeline.py` |
| HTMX 2.0.x | `hx-get` rail swaps, `hx-post` triggers, `hx-swap-oob` fanout | `stats_bar.html`, bumped in Phase 57 |
| Alpine 3.15.x | `$store.pipeline` store, `x-text`/`x-init` bindings, `:disabled` gates | `base.html:106` |
| Tailwind v4.3.2 (**pre-compiled at build time**, PR #181) | C3 utility classes + `dark:` variants | `assets/src/app.css` (`@theme` + `@source` templates) → `src/phaze/static/css/app.css` via `just tailwind`; `base.html` links `/static/css/app.css`. **New workspace utility/arbitrary-value classes only render after a `just tailwind` rebuild** — the `@source` glob already covers `pipeline/partials/`, so no input-css edit is needed, only the rebuild; `app.css` is a gitignored build artifact (never committed). |
| SQLAlchemy 2.0 async + asyncpg | All reads via existing degrade-safe service functions | `services/pipeline.py` |

**Installation:** none. `uv sync` already provides everything. All commands run via `uv run` per CLAUDE.md.

## Package Legitimacy Audit

**N/A — Phase 58 installs no external packages.** It is a template/route presentation rewrite over existing code. No registry interaction, no slopcheck needed.

## Key Data-Wiring Map (the core deliverable)

### Existing endpoints (reuse verbatim — WORK-02, WORK-01)

| Endpoint | Line | Returns | Pending-set query | Semantics |
|----------|------|---------|-------------------|-----------|
| `POST /pipeline/extract-metadata` | `pipeline.py:959` | `trigger_response.html` (`action="metadata extraction"`, `count`, `no_active_agent`) | `get_metadata_pending_files` (ALL music/video files, `pipeline.py:1038`) | ALL-only; idempotent dedup via `extract_file_metadata:<file_id>` key |
| `POST /pipeline/fingerprint` | `pipeline.py:1045` | `trigger_response.html` (`action="fingerprinting"`) | `get_fingerprint_pending_files` (METADATA_EXTRACTED ∪ failed-retry, deduped, `pipeline.py:1052`) | ALL-only |
| `GET /pipeline/scans/recent` | `pipeline_scans.py` | `recent_scans_table.html` (last 10 non-LIVE ScanBatches) | `build_recent_scans` (`pipeline_scans.py:229`) | Discover surface (WORK-01); note: this partial currently self-polls `every 5s` — see Pitfall 4 |
| (existing scan trigger) | `POST /pipeline/scans` | scan response partials | — | Discover "SCAN"/"RECOVER" CTAs wire here (already exists) |

`trigger_response.html` branches: `no_active_agent` → amber held-count copy; `split_counts` → per-lane analysis split; `count > 0` → green "Enqueued N"; else gray "No files ready". The Metadata/Fingerprint paths hit only the `no_active_agent` / `count > 0` / else branches.

### `$store.pipeline` keys available today (`base.html:106`)

Seeded to 0, refreshed every 5s via the `dag` dict OOB loop in `stats_bar.html` (`dag-seed-<key>`):

`discovered, analyzed, metadataExtracted, agentBusy, controllerBusy, metadataBusy, analyzeBusy, fingerprintBusy, searchBusy, scanBusy, agentOnline, scrapeBusy, matchBusy, metadataDone, metadataTotal, fingerprintDone, fingerprintTotal, analyzeDone, analyzeTotal, analyzeActive, tracklistDone, scrapeDone, scrapeTotal, matchDone, matchTotal, proposalsDone, proposalsTotal, approved, executedDone, executedTotal, metadataPaused/Priority, analyzePaused/Priority, fingerprintPaused/Priority`

**Sub-count bindings (UI-SPEC):** Discover→`discovered`; Metadata→`metadataDone`/`metadataTotal`; Fingerprint→`fingerprintDone`; Analyze→`analyzeActive`. All exist. `not_yet_enriched` (Discover sub-count) does NOT exist as a key — derive `discovered − metadataExtracted` client-side via `x-text`, or add one new derived seed key.

### Cloud/lane state — surfaced as OOB card partials, NOT store keys

These ride `/pipeline/stats` as whole-partial `hx-swap-oob` fragments (each has a stable id; `oob=True` flips the partial to emit the OOB attr). Counts come from degrade-safe service reads in both `build_dashboard_context` (first load) and `pipeline_stats_partial` (poll):

| Partial | id | Count source (service) | Lane mapping (UI-SPEC) |
|---------|-----|------------------------|------------------------|
| `awaiting_cloud_card.html` | `#awaiting-cloud-card` | `get_awaiting_cloud_count` (`:807`) | A1 — files held, no compute agent (sky) |
| `staged_pushing_card.html` | `#staged-pushing-card` | `get_pushing_count` (`:901`) | A1 — push-in-progress (amber) |
| `analyzing_cloud_card.html` | `#analyzing-cloud-card` | `get_pushed_count` (`:918`) | A1/cloud in-analysis (violet) |
| `admission_state_card.html` | `#admission-state-card` | `get_cloud_phase_counts` (`:864`) → queued_behind_quota/admitted/running/finished | k8s — quota-wait & admission progression (gray/blue/violet/green; **no alert role** — healthy) |
| `inadmissible_card.html` | `#inadmissible-card` | `get_inadmissible_count` (`:821`) | k8s **fault** — amber `role="alert"` banner |
| `localqueue_card.html` | `#localqueue-card` | `get_localqueue_unreachable(redis)` | k8s **fault** — amber `role="alert"` banner |

These partials carry the **carrier-always / body-conditional** pattern: the outer `<section id=...>` always renders (so the OOB swap has a stable target and a cleared state collapses to empty), the body renders only when count > 0. This is exactly the WORK-03 "quota-wait vs Inadmissible" load-bearing distinction: healthy progression (`admission_state_card`) has no alert role; faults (`inadmissible_card`/`localqueue_card`) use `role="alert"` + ⚠. **Reuse these verbatim — do not restyle** (UI-SPEC: they already satisfy the C3 dark contract).

### Lane-card capacity numerals (WORK-03) — the one derivation gap

The lane-card capacity numbers (`8 / 8` local, `2 / 4` A1, `{n} pending` k8s) are NOT all available as store keys today:

| Lane | "used" available? | "total/slots" available? | Recommended read-only derivation |
|------|-------------------|--------------------------|----------------------------------|
| 🖥️ local | partial — `analyzeBusy`/`analyzeActive` are aggregate (all kinds) | NO — worker concurrency (`worker_max_jobs=8`) is not surfaced | Use `analyzeBusy`/`analyzeActive` as "in flight"; if a slot denominator is wanted, add a derived seed from config. `agentOnline` counts ALL kinds (`count_active_agents`, `:702`) |
| ☁️ A1 | yes via cloud-window counts (`pushing`+`analyzing_cloud`) | online via compute-agent presence | A1 "online" ← `select_active_agent(session, kind="compute")` try/except (`enqueue_router.py:96`); a kind-split COUNT does not exist — add a derived `computeOnline` seed if a numeral is required |
| ⎈ k8s | yes via admission counts | quota-gated (no fixed slots) | k8s "{n} pending / running" ← `queued_behind_quota`+`admitted` / `running` from `get_cloud_phase_counts` |

**"Not configured" derivation (D-05):** `settings.cloud_target` (`config.py:406`) is `Literal["local","a1","k8s"]`, deployment-wide. A1 lane is "not configured" when `cloud_target != "a1"`; k8s lane is "not configured" when `cloud_target != "k8s"`. "offline" = configured-but-no-online-agent (A1) or `localqueue_unreachable` True (k8s). These config/flag reads are read-only and may be added to the stats context.

**Per-file lane badge (D-03/WORK-04):** no `cloud_target` file column. Derive per file: no `cloud_job` row → 🖥️ local; `cloud_job` with `cloud_phase IS NULL` → ☁️ A1; `cloud_job` with `cloud_phase` set → ⎈ k8s. The Analyze file table needs a query joining `FileRecord` (Analyze-stage states) LEFT JOIN `cloud_job`. `get_files_by_state` (`:727`) returns files for one state; the D-03 "all in-stage files" table spans multiple states (running/awaiting/analyzed) and needs either several `get_files_by_state` calls or one new read-only multi-state query (a SELECT, no behavior change).

### Windowed progress (WORK-04) — exact data shape

- Aggregate (1:1, `analysis` table, migration 021): `fine_windows_analyzed`, `fine_windows_total`, `coarse_windows_analyzed`, `coarse_windows_total`, `sampled` (all nullable). This is the cheapest source for `window {done}/{total}` — read `fine_windows_analyzed/fine_windows_total` directly off the `analysis` row.
- Per-window (1:many, `analysis_window` table, `analysis.py:35`): `file_id` (indexed, not unique), `tier`, `window_index`, `start_sec`, `end_sec`, per-tier fields. Counting rows per `file_id` also yields N, but the aggregate columns are simpler and pre-computed.
- **Write timing (UPDATED post-57.1, PR #184):** the `analysis.fine_windows_analyzed` aggregate now increments DURING flight (incremental window persistence). → present the live `N/M windows` count from the aggregate for in-flight files (alongside `running`) and full coverage for completed (ANALYZED) files. The pre-57.1 atomic-only write is HISTORICAL.

## Architecture Patterns

### System data-flow (Phase 58 live refresh)

```
                    ┌─────────────────────────────────────────────┐
   Browser          │  Persistent shell chrome (Phase 57)         │
   (Alpine)         │  #pipeline-stats  ──hx-get every 5s──┐      │
       │            └──────────────────────────────────────┼──────┘
       │ x-text / :disabled bindings                       ▼
       ▼                                          GET /pipeline/stats
  $store.pipeline  ◄───hx-swap-oob (dag-seed-<key>, x-init writes)─┐
       ▲                                                            │
       │ whole-partial hx-swap-oob (card id targets)                │
  cloud cards ◄──────────────────────────────────────────────┐     │
  (#awaiting-cloud-card, #admission-state-card, ...)          │     │
                                                              │     │
                                          stats_bar.html (oob_counts=True)
                                                              │
                              ┌───────────────────────────────┴───┐
                              │ pipeline_stats_partial():          │
                              │  get_queue_activity, _build_dag_   │
                              │  context, get_*_count (degrade-safe)│
                              └───────────────────────────────────┘

  Rail click ──hx-get /s/<stage>──► shell._render_stage ──HX? fragment : shell──►
       swaps workspace partial into #stage-workspace (NO new poll element inside it)
```

### Pattern 1: Shared workspace scaffold + file-table partials
**What:** One `{% include %}`-able scaffold (header: `<h1 tabindex="-1">` focus target + live sub-count + secondary action buttons; body slot) and one reusable file-table partial, per UI-SPEC Patterns 1–2.
**When:** All four workspaces. The fragment root is content-only — NEVER `{% extends "base.html" %}`, NEVER `<html>`/`<head>`/second skip-link (R-5; the dead-template AST guard from Phase 57 stays green).
**Wiring:** Stage fragments are dispatched by `shell.py STAGE_PARTIALS` (currently `_STAGE_PLACEHOLDER` for discover/metadata/fingerprint; `dag_canvas.html` for analyze). Phase 58 replaces these four map values with the new workspace partials. Analyze already gets DB context via `build_dashboard_context`; the other three currently get none — they will need their own context (queue lists + counts) added to `_render_stage` or a small per-stage context helper.

### Pattern 2: Reuse cloud card partials under the lane grid
**What:** Place `awaiting_cloud_card.html` / `staged_pushing_card.html` / `analyzing_cloud_card.html` (A1) and `admission_state_card.html` / `inadmissible_card.html` / `localqueue_card.html` (k8s) below the 3-card lane grid in the Analyze workspace.
**Why:** They already (a) ride the OOB poll, (b) encode the quota-wait-vs-Inadmissible alert semantics, (c) satisfy the C3 dark contract. The lane *cards* are new markup; the *sub-state detail* is reuse.

### Anti-Patterns to Avoid
- **Adding a second poll loop** (`hx-trigger="every Ns"` or `setInterval` inside any workspace fragment). R-2: exactly one `/pipeline/stats` request per 5s for the whole shell. The poll element must live in persistent chrome, not a swappable fragment.
- **Re-rendering trigger buttons on the poll.** The OOB seeds deliberately write to `$store.pipeline` (hidden `x-init` paragraphs / whole cards), never into the button subtree, so an in-flight click's loading state is preserved. New workspace OOB ids must follow the same hidden-seed pattern.
- **Adding store keys when a card partial already carries the data.** Cloud counts are already delivered as OOB card swaps; only add a new `dag`-dict seed key when a *reactive numeral binding* genuinely needs it.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Metadata/Fingerprint enqueue | A new subset/selected endpoint | `POST /pipeline/extract-metadata` / `/pipeline/fingerprint` verbatim (D-01) | New endpoint = backend change (forbidden); D-02 cuts SELECTED |
| Recent-scans table | Fresh scan list query/markup | `recent_scans_table.html` + `build_recent_scans` | Already live, paginated to 10, with elapsed/stall attrs |
| Kueue quota-wait vs Inadmissible UI | New alert components | `admission_state_card.html` (healthy) + `inadmissible_card.html`/`localqueue_card.html` (fault) | Encodes the exact WORK-03 distinction + `role="alert"` semantics |
| Cloud capacity counts | New SQL/aggregation | `get_awaiting_cloud_count`/`get_pushing_count`/`get_pushed_count`/`get_cloud_phase_counts`/`get_inadmissible_count`/`get_localqueue_unreachable` | All degrade-safe (never-500), already wired into the poll |
| Windowed coverage | Counting `analysis_window` rows live | `analysis.fine_windows_analyzed/total` columns | The aggregate already increments mid-flight (57.1); read it directly — no need to count rows |
| Compute-agent-online check | New liveness rule | `select_active_agent(session, kind="compute")` try/except (`enqueue_router.py:96`) | Matches the canonical liveness definition; don't invent a second one |

**Key insight:** Almost every datum the four workspaces need already exists and already rides the 5s poll. The phase is plumbing + presentation, not computation. The only *new* server work is read-only context assembly (multi-state file list for the Analyze table; optional derived seed keys for lane numerals and `not_yet_enriched`).

## Common Pitfalls

### Pitfall 1: (RECONCILED post-57.1) — render the mid-flight signal, do NOT collapse it to `running`
**Updated 2026-06-30:** Phase 57.1 (PR #184, MERGED to main) shipped incremental window persistence. `analysis.fine_windows_analyzed` now increments DURING flight, so the pre-57.1 "no live N/M" reality below is HISTORICAL.
**What used to be true (pre-57.1):** `analysis_window` rows + the aggregate were written in a single transaction at completion (`agent_analysis.py:205-220`); in-flight files had no window rows, so only `running` could be shown.
**What is true NOW (post-57.1):** an in-flight file HAS an `analysis` row whose `fine_windows_analyzed` increments mid-flight (< `fine_windows_total`). The Analyze table MUST render this live `N/M windows` indicator alongside `running` for in-flight rows; completed (ANALYZED) rows show full coverage. Phase 58 only READS this signal (D-04) — no schema/query change.
**New failure mode to avoid:** implementing the pre-57.1 "running only" behavior silently under-delivers the load-bearing mid-flight progress Phase 57.1 was built for. Always render `running` + `{fine_windows_analyzed}/{fine_windows_total} windows` for in-flight; full coverage for completed.

### Pitfall 2: Cloud counts are not in `$store.pipeline`
**What goes wrong:** Binding a lane numeral to a non-existent store key (e.g. `$store.pipeline.awaitingCloud`).
**Why:** Cloud state is delivered as whole-partial OOB card swaps, not `dag` store writes.
**How to avoid:** Either render the card partial (no binding needed) or add an explicit new `dag`-dict int key in `_build_dag_context` so it gets a `dag-seed-<key>` OOB write + a `base.html` store default. Don't assume the key exists.

### Pitfall 3: `oob_counts` gate and duplicate ids
**What goes wrong:** Emitting `hx-swap-oob` seeds on the initial full render → stray nodes + duplicate-id DOM collision with in-place seeds.
**Why:** `hx-swap-oob` is honored only during an HTMX swap; `oob_counts` is False on first render and True only on the `/pipeline/stats` poll (`pipeline.py:628`). `shell._render_stage` sets `oob_counts=False`.
**How to avoid:** Any new OOB seed for a workspace MUST be gated behind `{% if oob_counts %}` in `stats_bar.html` and have a matching in-place seed/default elsewhere. Mirror the existing `dag-seed-*` pattern exactly.

### Pitfall 4: `recent_scans_table.html` carries its own `hx-trigger="every 5s"`
**What goes wrong:** Reusing the partial as-is inside the Discover workspace adds a SECOND poll loop (it self-polls `GET /pipeline/scans/recent`), violating R-2's single-poll discipline.
**Why:** The partial was authored for the legacy dashboard with an independent refresh.
**How to avoid:** When restyling for the Discover workspace, either drop the `hx-get/hx-trigger="every 5s"` self-poll (fold the scan-row refresh into the single `/pipeline/stats` OOB fanout) or consciously document/keep it. **Planner must decide** — this is a real conflict between "reuse `recent_scans_table.html`" (discretion) and R-2 (locked). Recommended: strip the self-poll, swap via the main poll's OOB or accept slightly staler scan rows.

### Pitfall 5: Non-Analyze workspaces have no DB context today
**What goes wrong:** Building a Metadata/Fingerprint queue table with no data because `shell._render_stage` only populates context for `stage == "analyze"`.
**Why:** Phase 57 placeholders need no data; Phase 58 queue tables do.
**How to avoid:** Extend `_render_stage` (or add a per-stage context builder) to load the queue list (`get_files_by_state` / pending helpers) + counts for discover/metadata/fingerprint, mirroring how `analyze` calls `build_dashboard_context`. Keep these reads degrade-safe.

## State of the Art

| Old (Phase 57 bridge) | New (Phase 58) | Impact |
|------------------------|----------------|--------|
| `_STAGE_PLACEHOLDER` for discover/metadata/fingerprint | Real workspace partials in `STAGE_PARTIALS` | Replace map values; add per-stage context |
| Analyze = `dag_canvas.html` (full DAG) | Analyze = lane-card workspace + file table | Replace map value; reuse `build_dashboard_context` reads |
| Cloud cards only on legacy dashboard | Cloud cards surfaced inside Analyze workspace | Place existing partials; same OOB ids |

**Deprecated/outdated:** none new. The bridged placeholder/dashboard templates are superseded-in-place, NOT deleted (CUT-02/Phase 62 owns removal; the dead-template guard stays green by reachability).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio + httpx `AsyncClient` (CLAUDE.md; 85% min coverage) |
| Config | `pyproject.toml` (`[tool.pytest...]`) |
| Quick run | `uv run pytest tests/test_shell_routes.py -x` |
| Full suite | `uv run pytest --cov --cov-report=term-missing` |

### Existing test seams
- `tests/test_shell_routes.py` — fragment-vs-shell fork, rail-node wiring, `$store.pipeline`/theme preservation (SHELL-01..04). Add Phase-58 workspace-render assertions here or a sibling `tests/test_enrich_analyze_workspaces.py`.
- `tests/test_pipeline_dag_context.py` — `_build_dag_context` / store-seed int invariants. Extend for any new derived `dag` keys.
- `tests/test_pipeline_counters.py`, existing pipeline-route tests — trigger-endpoint behavior (already covered; assert the workspace buttons POST to the unchanged endpoints).

### Phase Requirements → Test Map
| Req | Behavior to validate | Type | Command (illustrative) | File |
|-----|----------------------|------|------------------------|------|
| WORK-01 | `/s/discover` fragment renders recent-scans table + discovered/not-yet-enriched sub-count; SCAN/RECOVER present | route/render | `uv run pytest tests/test_enrich_analyze_workspaces.py::test_discover_workspace -x` | ❌ Wave 0 |
| WORK-02 | Metadata/Fingerprint workspaces render queue + ALL button posting to existing endpoint; `count`/`no_active_agent` branches of `trigger_response.html` | route/render | `...::test_metadata_trigger_all_wired` | ❌ Wave 0 |
| WORK-03 | All 3 lane cards always render; `not configured` (cloud_target) vs `offline` (no agent / localqueue_unreachable) labels; Inadmissible carries `role="alert"`, admission card does NOT | render + state | `...::test_lane_cards_states` | ❌ Wave 0 |
| WORK-04 | In-flight row shows lane badge + `running`; completed row shows `window {analyzed}/{total}` from aggregate; per-file lane derived from cloud_job | render | `...::test_analyze_file_table_lane_and_windows` | ❌ Wave 0 |
| WORK-05 | Workspace fragment contains NO `hx-trigger="every"`/`setInterval`; live values update via OOB; exactly one `/pipeline/stats` request per cycle | structural assert | `...::test_single_poll_discipline` (assert no second poll element in any workspace fragment) | ❌ Wave 0 |
| R-5 | Workspace fragments are bare (no `<html>`/`extends`/second skip-link); dead-template guard green | structural | reuse Phase-57 fragment-bareness assertion + AST guard | ✅ exists (extend) |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_shell_routes.py tests/test_enrich_analyze_workspaces.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (≥85%)
- **Phase gate:** full suite green + `uv run ruff check . && uv run mypy .` before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_enrich_analyze_workspaces.py` — covers WORK-01..05 (new file; or extend `test_shell_routes.py`)
- [ ] Reuse existing `conftest.py` fixtures (`AsyncClient`, seeded files/agents/cloud_job rows). Confirm fixtures can seed `cloud_job` (cloud_phase variants) + `analysis` aggregate rows for lane/window assertions.
- [ ] Single-poll structural assertion helper (grep rendered fragment for `hx-trigger="every"` / `setInterval` → must be absent).

## Security Domain

`security_enforcement` not disabled; `ui_safety_gate: true`. Phase 58 introduces no new input surface (no operator free-text, no new query params, no new endpoints).

| ASVS Category | Applies | Control |
|---------------|---------|---------|
| V5 Input Validation / Output Encoding | yes | All interpolated values are server-computed ints or static strings via Jinja autoescape (the existing card partials already note this: "no operator free-text or PII is interpolated"). Stage resolution stays whitelisted in `shell.py STAGE_PARTIALS` — `stage` is never spliced into a template path (T-57-01) |
| V4 Access Control | no (unchanged) | Single-user admin tool, private network; no new authz surface |
| V6 Cryptography | no | none |

| Threat | STRIDE | Mitigation |
|--------|--------|------------|
| Template-path injection via stage name | Tampering | Static `STAGE_PARTIALS` whitelist (existing); 404 on unknown stage |
| Stored XSS via file path / tag in new file table | Tampering/Info-disclosure | Jinja autoescape; render paths in `font-mono` text nodes (as `recent_scans_table.html` already does with `title=`/truncate); no `| safe` |

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | Per-file lane is reliably derivable as (no cloud_job→local / cloud_phase NULL→A1 / cloud_phase set→k8s) | Per-file lane badge | If a cloud_job row can exist transiently for a file that ultimately ran local, badges could mislabel. Planner should confirm the cloud_job lifecycle vs FileState during the plan (read `release_awaiting_cloud.py` / `cloud_staging.py`) |
| A2 | `worker_max_jobs`/local slot total is not surfaced and a "used/total" local numeral needs a derived seed or config read | Lane-card capacity | If the prototype's `8 / 8` is load-bearing, planner must add a read-only derived denominator; otherwise show in-flight count only |
| A3 | Stripping the self-poll from `recent_scans_table.html` is acceptable (vs keeping a 2nd loop) | Pitfall 4 | If stale scan rows are unacceptable, planner needs a different reuse approach |

**These three need confirmation during planning/discuss before being locked.** All other claims are VERIFIED against in-repo code read this session.

## Open Questions (RESOLVED)

1. **In-flight windowed progress (WORK-04) — RESOLVED by 57.1 (PR #184).**
   - Know: post-57.1 the `analysis.fine_windows_analyzed` aggregate increments DURING flight (incremental window persistence).
   - Resolution: the UI-SPEC's `window 14/41` IS satisfied live mid-flight. Render lane badge + `running` + the mid-flight `N/M windows` count for in-flight rows, full coverage for completed. Phase 58 only reads the signal — no backend change.

2. **Lane-card capacity denominators (A2).**
   - RESOLVED (Plan 04): the A1 lane numeral binds to a read-only derived `computeOnline` seed added to the `dag` dict in `_build_dag_context` (with a pre-mounted `dag-seed-computeOnline` placeholder so the OOB seed lands); local binds to existing aggregate keys (`analyzeBusy`/`analyzeActive`/`agentOnline`) and k8s to the existing admission counts. No new poll, no new backend behavior.
   - Recommendation (historical): if the prototype numerals are load-bearing, add read-only derived seeds (`computeOnline`, k8s admission counts) to the `dag` dict; otherwise bind to existing aggregate keys and show in-flight only.

3. **Discover recent-scans self-poll (A3, Pitfall 4).**
   - RESOLVED (Plan 02): the Discover workspace reuses the recent-scans markup with its `hx-get`/`hx-trigger="every 5s"`/`hx-swap` self-poll STRIPPED (single `/pipeline/stats` chrome poll only, R-2); scan rows are accepted as ≤5s static-staleness per the locked R-2 single-poll tradeoff. `test_single_poll_discipline` asserts the fragment carries no `hx-trigger="every"`.
   - Recommendation (historical): strip the partial's `hx-trigger="every 5s"` on reuse to honor R-2; refresh scan rows via the single poll or accept ≤5s staleness.

## Environment Availability

**Skipped — no external dependencies.** Phase 58 is code/template changes over the running app; all reads use existing in-process services. No new tools, services, or runtimes required beyond the already-provisioned `uv`/Postgres/Redis dev stack.

## Sources

### Primary (HIGH confidence — in-repo, read 2026-06-29)
- `src/phaze/routers/shell.py` — Phase-57 fragment-vs-shell fork, `STAGE_PARTIALS` whitelist, `_render_stage` context
- `src/phaze/routers/pipeline.py` — `build_dashboard_context` (`:434`), `pipeline_stats_partial` (`:571`, `oob_counts=True` at `:628`), `_build_dag_context` (`:131`), trigger endpoints (`:959`, `:1045`)
- `src/phaze/templates/pipeline/partials/stats_bar.html` — OOB fanout, `oob_counts` gate, `dag-seed-<key>` pattern, all cloud-card OOB includes
- `src/phaze/templates/pipeline/partials/{awaiting_cloud,staged_pushing,analyzing_cloud,admission_state,inadmissible,localqueue}_card.html` — carrier-always/body-conditional + alert semantics
- `src/phaze/templates/pipeline/partials/{recent_scans_table,trigger_response}.html` — Discover/trigger surfaces
- `src/phaze/templates/base.html:106` — `$store.pipeline` keys
- `src/phaze/models/{analysis,cloud_job,file,agent}.py` — AnalysisWindow/aggregate, CloudJob/CloudPhase, FileState, Agent.kind
- `src/phaze/routers/agent_analysis.py:195-225` — atomic window write + state-advance (window-timing finding)
- `src/phaze/services/pipeline.py` — `get_queue_activity`/`queue_progress_percent`/`count_active_agents`/`get_*_count`/pending helpers/`get_files_by_state`
- `src/phaze/services/enqueue_router.py:96` — `select_active_agent(kind=...)` (compute-online seam)
- `src/phaze/config.py:406` — `cloud_target` deployment-wide setting
- `tests/test_shell_routes.py`, `tests/test_pipeline_dag_context.py` — test seams

### Secondary
- `.planning/phases/58-{CONTEXT,UI-SPEC}.md`, `57-CONTEXT.md`, `.planning/REQUIREMENTS.md`, `.planning/STATE.md`

## Metadata

**Confidence breakdown:**
- Data-wiring map (endpoints/store/partials): HIGH — read directly from current source
- Lane capacity derivation: HIGH for sources, MEDIUM for the exact numeral denominators (A2/A3 need a planning decision)
- Windowed-progress timing: HIGH — post-57.1 (PR #184) the aggregate increments mid-flight; in-flight rows carry a live N/M count (pre-57.1 atomic-only write is historical)
- No-backend-change boundary: HIGH — every recommended read is an existing degrade-safe service call or a config/flag read

**Research date:** 2026-06-29
**Valid until:** stable while the v6.0/v7.0 routers are unchanged (~30 days); re-verify line numbers if `pipeline.py` is refactored.
