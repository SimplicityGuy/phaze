<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01 — Wide slide-in overlay only.** The full record is a single wide, right-anchored slide-in over the shell (backdrop dim, `x-trap.inert.noscroll`), opened from a file row and from ⌘K. It is the ONLY per-file surface — the existing empty 350px right `<aside>` (`shell.html:164`) is not used (its removal is Phase 62). Serve the record as an HTMX **fragment** (no `extends base.html`). Planner picks the route shape (`/record/{file_id}` or `/s/…` analog).
- **D-02 — Open record is a snapshot whose COUNTS/STATUS ride the existing poll.** The record body renders once on open (no self-refresh request). A few OOB bits — stage chips / lane badge / pending-approval count — update off the same `/pipeline/stats` 5s fanout via `hx-swap-oob` behind the `oob_counts` gate. **No new loop.** Never re-render the operator's in-progress subtree (an approval selection inside the open record). New OOB ids register in the existing registry behind the gate.
- **D-03 — Search groups = Files / Tracklists / Artists; four quick commands.** Typed search funnels through the existing `search_queries.search()`. Quick commands (all four): **Scan** (existing `pipeline.py` scan trigger via `enqueue_router`), **Jump to a stage** (rail nav to `/s/<stage>`), **Jump to a review queue** (a Phase 60 gate), **Open Agents**.
- **D-04 — Full arrow-nav + grouped palette.** Labeled groups (Files / Tracklists / Artists / Commands); `↑`/`↓` move the active row **across** groups, `Enter` activates, `Esc` closes and returns focus to `#cmdk-trigger`. Focus-trap via `@alpinejs/focus` `x-trap.inert.noscroll`.
- **D-05 — Artists group = read-only distinct-artist query; Enter filters files by that artist.** The search service has no `artist` result_type — artist is a field/filter. The Artists group is populated by a read-only `SELECT DISTINCT` of `FileMetadata.artist` / `Tracklist.artist` matching the query. `Enter` filters via the existing `artist=` query param. **The one sanctioned additive backend touch** — a read query. Planner: confirm the distinct query is cheap/indexed; keep it read-only.
- **D-06 — Two sections — heartbeating Agents + ephemeral Compute lanes.** Section 1 = local/A1 (`admin_agents.py` table + `agent_liveness.classify()` / `sort_key`). Section 2 = a distinct "Compute / burst lanes" section for k8s, driven by `CloudJob` in-flight workload counts (not `Agent` rows / not `last_seen_at`).
- **D-07 — k8s liveness = Active / Waiting / Idle (never DEAD), from `CloudJob`.** ACTIVE when ≥1 workload `status=running`; WAITING (quota) when submitted-but-`inadmissible=true`; IDLE when no in-flight workloads. **Never DEAD.** Show the in-flight count when Active. Read query/aggregation over `CloudJob`.
- **D-08 — Agent-roots-only guide — no new path input, no directory browser.** When file count == 0, show a centered card listing each registered agent + its configured `scan_roots`, a "Scan {agent}" button (existing scan trigger via `enqueue_router`), and a "Configure roots →" link. Zero new input surface — no free-text path field, no directory-browsing endpoint. Live scan progress rides the existing `/pipeline/stats` poll.

### Claude's Discretion
- The exact route shape for the record fragment (`/record/{file_id}` vs `/s/…` analog) and the OOB ids registered for the open record (must ride the single poll behind `oob_counts` — no second loop).
- Whether Discogs-release results appear in ⌘K or the palette is limited to the three named groups (Files/Tracklists/Artists) + Commands.
- The empty-state placement (home/Analyze workspace vs a dedicated fragment) and its copy (the UI-SPEC locks it).
- Whether "history" in the record reads from `ExecutionLog` + `TagWriteLog` directly or via the existing `/audit/` view scoped to the file — pick whichever composes cleanest read-only.
- Reuse vs restyle of any legacy per-file partials (`proposals/partials/row_detail.html`, `tracklists/partials/track_detail.html`) into the record's sections — supersede-in-place; legacy templates stay until CUT-02.

### Deferred Ideas (OUT OF SCOPE)
- Empty 350px right `<aside>` removal (`shell.html:164`) — Phase 62 (CUT-02).
- Full a11y depth for the record + palette (keyboard-rail parity, skip-link, DAG ARIA, visible-focus parity) — Phase 62 (CUT-01).
- Narrow-width / responsive rail-collapse of the slide-in + palette — Phase 62 (CUT-03).
- Free-text path field / server-side directory browser for the empty state — explicitly rejected (attack surface).
- Per-artist entity pages / artist as a first-class result_type — beyond the distinct-artist filter facet (D-05).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| RECORD-01 | Opening a file (row or ⌘K) shows a full per-file record: identity, metadata diff, windowed multi-lane analysis timeline, this file's pending approvals (inline-approvable), and history | Compose existing reads: `AnalysisWindow`/`AnalysisResult` timeline is already assembled by `proposals.py:/{id}/timeline` + `analysis_timeline.html` (re-scope by `file_id`); metadata diff + inline approvals reuse `_diff_row.html` + the SAME proposals/tags approve/edit/undo routes (Phase 60); identity via `get_proposal_with_file` + `row_detail.html`; history via `ExecutionLog`/`TagWriteLog` or `/audit/` scoped to file. Slide-in served as a **fragment** into a persistent chrome host (see §Architecture Patterns Pattern 1). |
| RECORD-02 | ⌘K searches files / tracklists / artists + quick commands (scan, jump to a stage or review queue) | Fill the Phase 57 `cmdk_modal.html` skeleton; results fetched via a grouped HX endpoint over `search_queries.search()` (files+tracklists+discogs already returned) + a NEW read-only `distinct_artists()` query (D-05). Roving nav state lives in the modal `x-data`; result rows are static `role="option"` markup. Commands wire to `POST /pipeline/scan-live-sets`, `/s/<stage>`, and `/admin/agents`. See §Verified References + §Pitfalls. |
| RECORD-03 | Agents page: local/A1 heartbeating + k8s as ephemeral Job-based identity (never perpetually-DEAD) | Section 1 reuses `_load_agents` + `agent_liveness.classify/sort_key` verbatim. Section 2 = a NEW read-only CloudJob aggregation (Active/Waiting/Idle) — **strong precedent exists**: `services/pipeline.py:1117` already counts `CloudJob.inadmissible + status in (submitted,running)`; `:1162` already does four grouped CloudJob counts. Existing `/admin/agents` page already carries a STATIC k8s note (Phase 56); this phase makes it LIVE. See §Open Questions for shell-vs-standalone placement. |
| RECORD-04 | First-run empty state guides the operator to point phaze at a directory + live scan progress | Branch on file-count==0 in the home/Analyze workspace; list each `Agent.scan_roots`; "Scan {agent}" posts the **DISCOVERY** scan `POST /pipeline/scans` (agent_id + scan_root form) — NOT the parameterless fingerprint `scan-live-sets`. Live progress rides the existing `/pipeline/stats` poll. See §Pitfall 2 (the scan-endpoint landmine). |
</phase_requirements>

# Phase 61: Full record + ⌘K + Agents - Research

**Researched:** 2026-07-01
**Domain:** FastAPI + Jinja2 + HTMX 2.0.10 + Alpine 3.15.12 server-rendered UI; composing four additive surfaces over the live v7.0 shell; adding one first-party Alpine CDN plugin + one read-only distinct-artist query + one read-only CloudJob aggregation
**Confidence:** HIGH on all in-repo seams (every file:line read against the working tree 2026-07-01); MEDIUM on two integration decisions flagged in §Open Questions (Agents shell-vs-standalone placement; per-file OOB on a file-agnostic global poll)

## Summary

Phase 61 is exceptionally well-scoped by two LOCKED, approved documents (61-CONTEXT.md D-01..D-08, 61-UI-SPEC.md). This is an **IA/presentation phase composing existing partials and endpoints** — my job was to verify every referenced seam against live code and surface the implementation landmines a planner needs. The verification result: the CONTEXT's model/endpoint references are accurate, and there are **three high-value corrections + two genuine integration decisions** the planner must not miss.

The three corrections: (1) **The SRI test guards `base.html` only, but the live v7.0 shell that runs the ⌘K + slide-in focus-traps is `shell.html` — which carries its OWN duplicate `<head>` script block.** Adding `@alpinejs/focus` to `base.html` alone leaves the test green while the traps stay broken in the real shell. The plugin must land in BOTH files and the SRI test should be extended to cover `shell.html`. (2) **The empty-state "Scan {agent}" needs the DISCOVERY scan `POST /pipeline/scans` (agent_id + scan_root form), not the parameterless fingerprint `POST /pipeline/scan-live-sets`** the CONTEXT points at for ⌘K "Scan" — with 0 files there is nothing to fingerprint. (3) **The record body contains Alpine `x-data` islands (`_diff_row.html` inline-edit), so after the record fragment swaps in, `Alpine.initTree()` MUST run on it** — the existing `htmx:afterSwap` handler only re-inits `#stage-workspace`, not a record host.

The good news: nearly every hard part already exists. The multi-lane windowed timeline is a ready-made partial (`proposals/partials/analysis_timeline.html` + the `/{id}/timeline` route assembling fine/coarse `AnalysisWindow` rows + the sampled badge); the CloudJob liveness aggregation has two degrade-safe precedents in `services/pipeline.py`; the ⌘K skeleton already owns open/close/`$nextTick`-focus/`?palette=1`/Esc-return; the record's metadata diff + inline approvals reuse `_diff_row.html` and the Phase 60 approve/edit/undo routes verbatim.

**Primary recommendation:** Mount the slide-in and the (existing) palette as **persistent chrome host elements** in `shell.html` (siblings of `cmdk_modal.html`, OUTSIDE `#stage-workspace`) so `x-trap` initializes at page load and survives rail swaps; HTMX-swap only the record BODY into the host and re-run `Alpine.initTree()` on it. Add `@alpinejs/focus@3.15.12` to BOTH `shell.html` and `base.html` with the computed SRI (below) and extend the SRI test to cover `shell.html`. Add exactly two read-only queries (distinct-artist for ⌘K, CloudJob liveness for Agents) — no logic changes.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Record fragment assembly (identity/diff/timeline/approvals/history) | Frontend Server (new record route + Jinja partials over existing reads) | Database (read-only) | Composes existing per-file reads; no new persistence |
| Record focus-trap + open/close + focus-return | Browser (Alpine `x-trap.inert.noscroll` on a persistent chrome host) | — | Overlay behavior is client concern; host lives in shell chrome |
| Record inline approvals | API/Backend (existing Phase 60 proposals/tags PATCH/POST routes) | — | Reuse verbatim; logic unchanged |
| ⌘K grouped search | Frontend Server (grouped HX results endpoint) | Database (existing `search()` + NEW distinct-artist read) | Server owns the query + fragment; roving nav is client-side |
| ⌘K roving arrow-nav + ARIA listbox | Browser (Alpine `x-data` in the persistent modal) | — | Active-index over a flat ordered list; rows are static `option`s |
| ⌘K commands (scan / jump / open agents) | Frontend Server (HTMX nav) + API/Backend (scan enqueue) | — | Nav swaps `/s/<stage>`; Scan posts an existing enqueue route |
| Agents heartbeating section | Frontend Server (`_load_agents` + `agent_liveness.classify`) | Database | Reuse verbatim (Phase 29) |
| Agents compute-lane liveness | API/Backend (NEW read-only CloudJob aggregation) | Database | Server-side classify → inject on the lane card (mirrors `agent_liveness` shape) |
| First-run empty state + scan trigger | Frontend Server (count==0 branch + `scan_roots` render) | API/Backend (`POST /pipeline/scans`) | Reuses the Discover scan form; no new input surface |
| Live count refresh | Frontend Server (single `/pipeline/stats` poll) | Browser (`$store.pipeline` x-text) | Counts-only OOB; snapshot body never re-rendered (D-02) |

## Standard Stack

**No new pip/uv packages.** Phase 61 uses the CLAUDE.md-locked, already-installed stack plus exactly one first-party CDN Alpine plugin.

| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| FastAPI | project-pinned | New record fragment route; grouped ⌘K results route; Agents compute section | Mirror `shell.py` / `search.py` fragment-vs-full fork |
| SQLAlchemy (async) + asyncpg | project-pinned | Distinct-artist read (D-05); CloudJob liveness aggregation (D-07) | Mirror `get_summary_counts` (`search_queries.py:167`) + `services/pipeline.py:1117/1162` |
| Jinja2 + HTMX 2.0.10 + Alpine 3.15.12 | CDN (SRI-pinned) | Record slide-in, palette, agents, empty-state fragments | HTMX 2.0.10 supports `hx-on::after-swap` for per-host initTree |
| **`@alpinejs/focus`** | **3.15.12** (must equal Alpine core) | `x-trap.inert.noscroll` for the slide-in + palette | **The one new dep.** Load `<script defer>` BEFORE Alpine core in BOTH `shell.html` and `base.html`; SRI SHA-384 computed below |
| Tailwind v4 (pre-compiled) | build artifact | New record/palette/agents/empty-state utility classes | Requires a `just tailwind` rebuild of `/static/css/app.css` (gitignored); partials must live under a `@source`-covered path (`shell/partials/`, `pipeline/partials/` already covered) |
| pytest + pytest-asyncio + httpx AsyncClient | project-pinned | Validation (see §Validation Architecture) | `uv run pytest` only |

**CDN load contract (verified against `shell.html:33-39` and `base.html:33-39`):** both files currently load, in order, HTMX 2.0.10 (unpkg), htmx-ext-sse 2.2.4, then Alpine 3.15.12 (`<script defer>`, jsdelivr). The focus plugin must be inserted as a `<script defer>` **immediately before** the Alpine core line in BOTH files. Alpine plugins must load before core so core registers them on init. [VERIFIED: codebase — both files pin `alpinejs@3.15.12`]

## Package Legitimacy / CDN Audit

No `npm`/`pip`/`cargo` install occurs — the only external asset is a CDN `<script>` guarded by Subresource Integrity. slopcheck / registry-existence gates are not applicable to a browser CDN script; the analogous gate here is the SRI SHA-384 enforced by `tests/test_base_html_sri.py`.

| Asset | Source | Publisher | Version | Integrity gate | Disposition |
|-------|--------|-----------|---------|----------------|-------------|
| `@alpinejs/focus` | `cdn.jsdelivr.net/npm/@alpinejs/focus@3.15.12/dist/cdn.min.js` | Alpine core team (same publisher as `alpinejs`) | 3.15.12 (pinned == core) | SRI SHA-384 (must extend the test to `shell.html`) | **Approved** — first-party Alpine plugin, full-semver pinned, SRI-guarded |

**Computed SRI (verified this session — fetched with `Accept-Encoding: identity`, 26,051 bytes):**

```
integrity="sha384-ysJcnHb6oCzqAGKdoTm+IqKqmPKgxHT+ApZCawkyWOJfMq15WvzW3RRmHl7tWpEY"
```

Full tag to insert (before the Alpine core line) in BOTH `shell.html` and `base.html`:

```html
<!-- Alpine focus plugin (x-trap) — MUST load before Alpine core; version == core (3.15.12) -->
<script defer src="https://cdn.jsdelivr.net/npm/@alpinejs/focus@3.15.12/dist/cdn.min.js" integrity="sha384-ysJcnHb6oCzqAGKdoTm+IqKqmPKgxHT+ApZCawkyWOJfMq15WvzW3RRmHl7tWpEY" crossorigin="anonymous"></script>
```

> The `test_cdn_sri_hashes_match_served_content` integration test (network-gated) will re-verify this hash against jsdelivr at test time; the offline `test_every_cdn_script_pins_a_specific_version` test requires the full-semver `@3.15.12` form (satisfied). **Recompute recipe if it drifts:** `curl -fsSL -H "Accept-Encoding: identity" <url> | openssl dgst -sha384 -binary | openssl base64 -A`.

## Verified References (live-code audit)

Every reference read against the working tree on 2026-07-01.

### The one new dep + the SRI gate (highest-value finding)
| Symbol | Verified location | Reality | Provenance |
|--------|------------------|---------|-----------|
| Shell script block | `shell/shell.html:32-39` | Full `<head>` with HTMX 2.0.10 → htmx-ext-sse 2.2.4 → **Alpine 3.15.12 `<script defer>`**. `shell.html` does NOT extend `base.html` — it is a standalone full page. **This is where ⌘K + slide-in actually run.** | VERIFIED: codebase |
| Legacy script block | `base.html:32-39` | Identical script block; `base.html` is the LEGACY page wrapper (old tab nav, `:168-216`) that Phase 62/CUT-02 removes | VERIFIED: codebase |
| SRI test scope | `tests/test_base_html_sri.py:44` | `_BASE_HTML = … "base.html"` — the test reads **ONLY base.html**. `shell.html`'s scripts are UNGUARDED. | VERIFIED: codebase |
| Version-pin test | `tests/test_base_html_sri.py:69` | Rejects `@<major>` / `@<major>.<minor>`; requires full semver `@3.15.12` (or 40-char sha). Reads base.html only. | VERIFIED: codebase |
| ⌘K trigger id | `shell/partials/header.html:27` | `id="cmdk-trigger"` `@click="$dispatch('cmdk:open')"` — the Esc focus-return target (D-04) | VERIFIED: codebase |

### Full-record slide-in (RECORD-01)
| Symbol | Verified location | Reality | Provenance |
|--------|------------------|---------|-----------|
| Windowed timeline route (reuse) | `proposals.py:257` `proposal_timeline` (`GET /{id}/timeline`) | Resolves proposal→`file_id`, selects `AnalysisWindow` ordered by tier+window_index, splits fine/coarse, fetches the 1:1 `AnalysisResult` for the sampled badge. Renders `proposals/partials/analysis_timeline.html`. **Re-scope by `file_id` for the record.** | VERIFIED: codebase |
| Timeline partial (reuse) | `proposals/partials/analysis_timeline.html` | Multi-lane SVG timeline over fine/coarse windows + sampled badge + deepen button (`POST /pipeline/files/{file_id}/deepen`, `pipeline.py:856`) | VERIFIED: codebase |
| `AnalysisResult` | `models/analysis.py:13` | `bpm`, `musical_key`, `mood`, `style`, `fine_windows_analyzed/total`, `coarse_windows_analyzed/total`, `sampled`, `analysis_completed_at`. **Coverage cols = migration 021** (not 018); `analysis_completed_at` = migration 028. | VERIFIED: codebase |
| `AnalysisWindow` | `models/analysis.py:41` | 1:many, `file_id` indexed (not unique), ON DELETE CASCADE; fine tier → bpm/key, coarse → mood/style/danceability. **Table = migration 018.** | VERIFIED: codebase |
| Metadata diff + inline approvals (reuse) | `pipeline/partials/_diff_row.html` | ONE shared before→after row; caller passes `approve_url`/`skip_url`/`undo_url`/`edit_url` + `approve_method`. **Contains `x-data='{editing:false,...}'` — an Alpine island → the record body needs `Alpine.initTree` after swap (Pitfall 3).** All cells autoescaped (T-60-XSS). | VERIFIED: codebase |
| Per-file proposal detail (reuse) | `proposals.py:243` `row_detail` + `get_proposal_with_file` | Renders `proposals/partials/row_detail.html` for identity; scope by proposal→file | VERIFIED: codebase |
| History source | `models/execution.py` `ExecutionLog` + `models/tag_write_log.py` `TagWriteLog` + `execution.py:350` `/audit/` | Append-only per-file trails (rename/move + tag writes) — read-only for the History section | VERIFIED: codebase (per 60-RESEARCH) |
| Approve/edit/undo routes (reuse) | `proposals.py:168/193/218` (PATCH approve/reject/undo), `proposals.py:/{id}/edit` (Phase 60 D-05 PATCH), `tags.py:309` (`POST /tags/{id}/write`), `tags.py` undo | Same routes Phase 60 wired into `_diff_row.html`; the record's pending-approval cluster reuses them verbatim | VERIFIED: codebase (per 60-RESEARCH) |

### ⌘K command palette (RECORD-02)
| Symbol | Verified location | Reality | Provenance |
|--------|------------------|---------|-----------|
| Skeleton modal | `shell/partials/cmdk_modal.html` | Persistent chrome (`{% include %}` at `shell.html:172`, OUTSIDE `#stage-workspace`). Owns open/close, `?palette=1` auto-open, `$nextTick` input focus, `@keydown.escape` → `#cmdk-trigger`. Currently uses **basic focus mgmt** (no `x-trap`). Body is a static placeholder — no fetch, no groups, no roving nav. | VERIFIED: codebase |
| Search service | `services/search_queries.py:37` `search()` | Returns `SearchResult(result_type ∈ {file, tracklist, discogs_release})` via `union_all`; supports `artist=` ilike filter, FTS `concat_ws(original_filename, artist, title, genre)`. **No `artist` result_type — artist is a field.** | VERIFIED: codebase |
| Search HX branch | `search.py:39-82` | Non-HX GET → `RedirectResponse("/?palette=1")`; HX GET renders `search/partials/results_content.html` (FLAT results). **A grouped palette fragment is new** — add a grouped template + a distinct-artist read. | VERIFIED: codebase |
| Distinct-artist query (NEW, D-05) | does not exist yet | Add `async def distinct_artists(session, query, *, limit)` in `search_queries.py` (mirror `get_summary_counts:167`). `SELECT DISTINCT` over `FileMetadata.artist` / `Tracklist.artist` with ilike. **Neither column is indexed** (Pitfall 4). | VERIFIED: codebase (absence confirmed) |
| Artist filter param (reuse) | `search.py:23` `artist` Query param → `search(artist=…)` | `Enter` on an artist navigates the file list filtered by `artist=` | VERIFIED: codebase |
| Scan command target | `pipeline.py:1187` `POST /pipeline/scan-live-sets` | Parameterless bulk fingerprint scan over eligible files via `enqueue_router`; `NoActiveAgentError` → empty-state, never 500. **Correct for ⌘K "Scan" (no params needed).** | VERIFIED: codebase |
| Stage/queue nav | `shell.py:247` `GET /s/{stage}` (whitelist `STAGE_PARTIALS`) | ⌘K "Jump to stage / review queue" = HTMX nav to `/s/<stage>` (e.g. `/s/rename`, `/s/dedupe`) | VERIFIED: codebase |

### Agents page (RECORD-03)
| Symbol | Verified location | Reality | Provenance |
|--------|------------------|---------|-----------|
| Heartbeating section (reuse) | `admin_agents.py:59` `_load_agents` + `:80` `page` + `:108` `_table` | Loads `Agent` rows, injects `_status = classify(a, now)`, sorts via `sort_key`. `/admin/agents` full page (extends base.html) + `/admin/agents/_table` **its own 5s self-poll** (`hx-trigger="every 5s"`). | VERIFIED: codebase |
| Liveness classify (reuse) | `services/agent_liveness.py:68` `classify` / `:89` `sort_key` | Pure functions: revoked→never→alive/stale/dead by `AGENT_LIVENESS_*` thresholds. **Server-side classify → inject on row** is the shape to mirror for compute lanes (D-07). | VERIFIED: codebase |
| Existing k8s note | `admin/agents.html` (Phase 56) | Already renders a STATIC ephemeral-k8s note (neutral gray). Phase 61 makes it a LIVE second section. | VERIFIED: codebase |
| CloudJob liveness (aggregation precedents) | `services/pipeline.py:1117` (`inadmissible` + `status in (submitted,running)` count) + `:1162` (four grouped CloudJob counts, KROUTE-06) | Both degrade-safe (`try/except → 0`). **D-07's Active/Waiting/Idle is a near-identical read** — reuse/extend these, don't hand-roll. | VERIFIED: codebase |
| `CloudJob` model | `models/cloud_job.py:65` | `status` (`CloudJobStatus`: uploading/uploaded/submitted/running/succeeded/failed), `inadmissible` (bool), `kueue_workload`, `cloud_phase`. ACTIVE = ≥1 `status=running`; WAITING = ≥1 `submitted` + `inadmissible=true`; IDLE = none in-flight. | VERIFIED: codebase |

### First-run empty state (RECORD-04)
| Symbol | Verified location | Reality (⚠ = correction) | Provenance |
|--------|------------------|---------|-----------|
| Discovery scan (empty-state target) | `pipeline_scans.py:305` `POST /pipeline/scans` | ⚠ Requires `agent_id: Form` + `scan_root: Form` (+ optional `subpath`). Validates scan_root against `agent.scan_roots`, creates a RUNNING `ScanBatch`, enqueues `scan_directory`, returns `scan_progress_card.html`. **This — not `scan-live-sets` — is the correct empty-state trigger** (0 files → discovery, not fingerprint). | VERIFIED: codebase |
| Discover scan form (reuse) | `pipeline/partials/discover_workspace.html:39` | The reused Trigger Scan form already posts `/pipeline/scans`; `agent_roots_swap` (`pipeline_scans.py:150`, `GET /pipeline/scans/agent-roots`) provides the roots dropdown | VERIFIED: codebase |
| Agent scan_roots | `models/agent.py:29` | `scan_roots: JSONB` (list of strings); `kind ∈ {fileserver, compute}` | VERIFIED: codebase |
| Live scan progress | `shell.html:187` `#pipeline-stats` poll + `pipeline_scans.py:200` `/{batch_id}` progress card | Existing single 5s poll; scan progress rides it (D-08) | VERIFIED: codebase |

### The shell fragment + re-init contract (load-bearing)
| Symbol | Verified location | Reality | Provenance |
|--------|------------------|---------|-----------|
| Swap target + host | `shell.html:160` `#stage-workspace` (rail-swap target); `:164` empty right `<aside>` (NOT used, D-01); `:172` `{% include cmdk_modal %}` | Persistent chrome (header, rail, cmdk, `#pipeline-stats`) lives OUTSIDE `#stage-workspace`, so a rail swap never touches it | VERIFIED: codebase |
| afterSwap re-init | `shell.html:236-247` | `htmx:afterSwap` runs `syncRailSelection` + `_focusStageHeading` **only when `tgt.id === 'stage-workspace'`**. `htmx:historyRestore` (`:230`) runs `Alpine.initTree(ws)` on `#stage-workspace` only. **Neither re-inits a record host** — the record body's Alpine islands would stay dead (Pitfall 3). | VERIFIED: codebase |

## Architecture Patterns

### System Architecture Diagram

```
                     shell.html  (persistent chrome — loaded ONCE)
  ┌───────────────────────────────────────────────────────────────────────┐
  │ header (#cmdk-trigger)   rail (/s/<stage>)   #pipeline-stats 5s poll    │
  │                                                                         │
  │  ┌───────────────┐   ┌──────────────────────┐   ┌────────────────────┐ │
  │  │ #stage-workspace │  │ #cmdk-host (modal)   │   │ #record-host       │ │
  │  │ (rail-swap tgt)  │  │ x-trap on OPEN       │   │ x-trap on OPEN     │ │
  │  │ empty-state when │  │ (D-04)               │   │ (D-01)             │ │
  │  │ file_count==0    │  └─────────┬────────────┘   └─────────┬──────────┘ │
  │  └──────┬───────────┘            │ HX GET grouped results   │ HX GET     │
  └─────────┼──────────────────────┼──────────────────────────┼────────────┘
            │ /s/<stage>            │ /search palette-group     │ /record/{file_id}
            ▼                       ▼                            ▼
   shell.py _render_stage   search.py grouped branch     NEW record route
   (existing whitelist)     search() + distinct_artists  compose existing reads:
            │               (NEW read, D-05)             timeline(file_id) · _diff_row
            ▼                       │                     · pending approvals · history
   analyze_workspace with          ▼ static option rows    │ body has x-data islands →
   count==0 → empty-state   roving index in modal x-data   ▼ Alpine.initTree(host) after swap
            │                       │ Enter → open record / run cmd / nav
            ▼                       ▼
   POST /pipeline/scans     POST /pipeline/scan-live-sets  reuse Phase 60 approve/edit/undo
   (agent_id + scan_root)   (parameterless, enqueue_router)  routes (proposals/tags)

   Agents surface (RECORD-03):  _load_agents + classify  (Section 1, heartbeating)
                                NEW CloudJob aggregation  (Section 2, Active/Waiting/Idle)
                                → its own single 5s /admin/agents/_table poll (see OQ-1)

   D-02 live discipline: record body = SNAPSHOT (renders once). Any live badge binds to a
   GLOBAL $store.pipeline key via the ONE /pipeline/stats poll. NO new loop, NO file-aware poll,
   NEVER re-render the in-progress approval subtree.
```

### Pattern 1: Persistent chrome host + swap-the-body (the load-bearing overlay pattern)
**What:** Mount both overlays as persistent chrome elements in `shell.html` (siblings of `cmdk_modal.html`), each carrying its own `x-data` (open state + the opener element for focus-return) and `x-trap.inert.noscroll` gated on `open`. HTMX swaps only the *body content* into the host's inner container.
**Why:** Alpine scans the DOM once at load (from `<body>` down). An `x-trap` placed on a persistent host initializes at load and works immediately. A trap placed on an HTMX-swapped fragment would never initialize (Alpine does not auto-init swapped DOM). This is exactly how the existing `cmdk_modal.html` already works — extend the same shape to the record.
**Contract:**
- The `cmdk_modal.html` already IS a persistent host — add `x-trap.inert.noscroll="open"` to its panel and the grouped-results + roving-nav state to its `x-data`. Its results BODY is static `role="option"` markup (no Alpine islands) → **no initTree needed** for the palette.
- The record needs a NEW persistent `#record-host` in `shell.html` (a sibling include, e.g. `shell/partials/record_host.html`) holding `x-data` (open/close, opener ref) + `x-trap.inert.noscroll="open"`. The record BODY (fetched via `/record/{file_id}`) contains `_diff_row.html` Alpine islands → **`Alpine.initTree()` MUST run on the host after the body swaps in** (Pitfall 3).

**Example (record host — persistent chrome, body swapped in):**
```html
{# shell/partials/record_host.html — included ONCE in shell.html, sibling of cmdk_modal #}
<div id="record-host"
     x-data="{ open:false, opener:null,
                show(el){ this.opener=el; this.open=true; },
                hide(){ this.open=false; if(this.opener) this.opener.focus(); } }"
     @record:open.window="show($event.detail.el)"
     @keydown.escape.window="if(open) hide()">
  <div x-show="open" style="display:none" class="fixed inset-0 z-40"
       role="dialog" aria-modal="true" :aria-label="'File record'"
       x-trap.inert.noscroll="open">
    <div class="absolute inset-0 bg-black/60" @click="hide()"></div>
    <div class="absolute inset-y-4 right-4 w-[760px] max-w-[94vw] bg-white dark:bg-phaze-panel
                border rounded-2xl shadow-2xl overflow-y-auto">
      {# HTMX swaps the record body HERE; re-init Alpine on the swapped subtree #}
      <div id="record-body" hx-on::after-swap="if(window.Alpine) Alpine.initTree(this)"></div>
    </div>
  </div>
</div>
```
A file row opens it with: `hx-get="/record/{{ file_id }}" hx-target="#record-body" hx-swap="innerHTML" @click="$dispatch('record:open', {el: $el})"`.
Source: adapted from `cmdk_modal.html` (persistent-host precedent) + `_diff_row.html` (Alpine island → initTree requirement).

### Pattern 2: ⌘K grouped results + roving index (ARIA listbox)
**What:** Extend `cmdk_modal.html`'s `x-data` with `{ q, results, activeIndex, items[] }`; a debounced `x-on:input` fires `hx-get` to a grouped-results endpoint that swaps ONLY the results body. On new results, reset `activeIndex` to the first selectable row. `↑/↓` move `activeIndex` over the FLAT ordered list (group headers `role="presentation"`, skipped); `Enter` activates `items[activeIndex]`.
**ARIA:** input `role="combobox" aria-expanded aria-controls="{listbox}" aria-activedescendant="{active row id}"`; results container `role="listbox"`; each selectable row `role="option"` `:aria-selected`. Dialog wrapper `role="dialog" aria-modal aria-label="Command palette"` (already present).
**Server:** a grouped fragment over `search()` (files/tracklists, optionally discogs) + the NEW `distinct_artists()` read + a static Commands group. Rows are static markup carrying `data-action` / `hx-get` so `Enter` (or click) dispatches the right effect.

### Pattern 3: Server-side liveness classification for compute lanes (mirror `agent_liveness`)
**What:** Add a read-only `classify_compute_lanes(session)` (in a service, e.g. `services/agent_liveness.py` or `services/pipeline.py`) returning `("ACTIVE"|"WAITING"|"IDLE", in_flight_count)` from CloudJob counts — reusing the degrade-safe `try/except → default` shape at `services/pipeline.py:1117/1162`. Render Section 2 of the Agents page from it. This mirrors the "classify server-side, inject on the row" precedent — never a client-side derivation, never a DEAD state.

### Pattern 4: Snapshot record body + counts-only global OOB (D-02 / R-2 discipline)
The record body renders once. The ONLY live loop is the chrome `/pipeline/stats` 5s poll, which fans out `dag-seed-*` OOB paragraphs into `$store.pipeline`. Any "live" badge in the record binds to a GLOBAL `$store.pipeline` key via `x-text` (e.g. a global pending count) — it does NOT get a per-file OOB update (the poll is file-agnostic; see OQ-2). **Never** put `hx-swap-oob` on the approval-row subtree, `hx-trigger="every"` inside the record, or a `setInterval`. An in-progress inline edit / approval selection must survive the poll (Phase 60 R-2, verbatim).

### Anti-Patterns to Avoid
- **Adding `@alpinejs/focus` to `base.html` only.** The shell runs on `shell.html`; the traps stay broken there while the SRI test passes (Pitfall 1).
- **Placing `x-trap` on an HTMX-swapped fragment.** It never initializes — put it on the persistent host (Pattern 1).
- **Wiring the empty-state "Scan {agent}" to `POST /pipeline/scan-live-sets`.** That is the parameterless fingerprint scan; with 0 files it does nothing. Use `POST /pipeline/scans` (agent_id + scan_root) (Pitfall 2).
- **A second poll for the open record / palette / agents.** Everything rides the existing single loop (or, for the Agents page, its existing single `_table` self-poll — see OQ-1).
- **Making `/pipeline/stats` file-aware to push per-file OOB.** The poll is global chrome; keep the record a snapshot (OQ-2).
- **Deleting/relocating legacy per-file partials.** Supersede-in-place; `proposals/partials/row_detail.html` etc. stay until CUT-02.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Multi-lane windowed timeline | New SVG assembler over `AnalysisWindow` | `proposals/partials/analysis_timeline.html` + the `/{id}/timeline` assembly (re-scope by `file_id`) | Already renders fine/coarse lanes + sampled badge + deepen; verified working (Phase 44) |
| Metadata diff + inline approvals | New diff row / approve buttons | `_diff_row.html` + Phase 60 approve/edit/undo routes | The parameterized diff row + verified routes exist; the record just supplies the URLs |
| Compute-lane liveness | New CloudJob state machine | Extend `services/pipeline.py:1117/1162` degrade-safe counts | Two grouped CloudJob-count precedents already exist |
| Agent heartbeat classification | New liveness logic | `_load_agents` + `agent_liveness.classify/sort_key` | Section 1 is reuse-verbatim |
| Focus trap / inert / scroll-lock | Hand-rolled `keydown` cycling | `@alpinejs/focus` `x-trap.inert.noscroll` | The one sanctioned dep; battle-tested |
| Empty-state scan form | New path input / directory browser | The Discover scan form → `POST /pipeline/scans` over `scan_roots` | D-08 forbids a new input surface (attack surface) |
| Search / FTS | New query | `search_queries.search()` | Files+tracklists+discogs + facets already returned |

**Key insight:** Phase 61 adds exactly one CDN plugin + two read-only queries. Everything else is *composition*. The risk is not missing functionality — it is (a) breaking the focus-trap by mis-placing the plugin/trap, and (b) wiring the empty-state scan to the wrong endpoint.

## Common Pitfalls

### Pitfall 1: The SRI test guards `base.html`, but the shell runs on `shell.html`
**What goes wrong:** You add `@alpinejs/focus` to `base.html`, `test_base_html_sri.py` passes, but the v7.0 shell (`shell.html`, a separate full page with its own `<head>`) still has no focus plugin → `x-trap` is undefined → BOTH the ⌘K palette and the record slide-in traps fail open (no focus containment, no `inert`, no scroll-lock). A stale/absent hash in `shell.html` is likewise invisible to the test.
**Why it happens:** `shell.html` and `base.html` carry DUPLICATE `<head>` script blocks; the SRI test reads only `base.html` (`test_base_html_sri.py:44`).
**How to avoid:** Add the plugin (with the computed SRI) to BOTH files, AND **extend the SRI test to scan `shell.html`** (parametrize `_extract_cdn_scripts` over both templates, or add a `_SHELL_HTML` path with the same two assertions). Flag this as a required task, not a nicety.
**Warning signs:** ⌘K opens but Tab escapes the modal; background scrolls behind the slide-in; `Alpine.raw is not a function` / `x-trap` directive warnings in the console.

### Pitfall 2: Empty-state "Scan {agent}" needs the DISCOVERY scan, and it needs form params
**What goes wrong:** CONTEXT D-08 and the ⌘K "Scan" both reference "the existing scan trigger (`pipeline.py:1187`)", which is `POST /pipeline/scan-live-sets` — a **parameterless fingerprint** bulk over already-discovered files. In the empty state (file_count==0) there is nothing to fingerprint; the correct action is a DISCOVERY scan `POST /pipeline/scans`, which **requires `agent_id` + `scan_root` form fields** validated against `agent.scan_roots`.
**Why it happens:** Two different "scan" endpoints; the CONTEXT line reference points at the fingerprint one.
**How to avoid:** Empty-state "Scan {agent}" → `POST /pipeline/scans` with `agent_id` + a chosen `scan_root`. Since an agent may have multiple `scan_roots`, render one button per (agent, root) OR post `scan_roots[0]` per agent — a planner decision (mirror `discover_workspace.html`'s reused form + `GET /pipeline/scans/agent-roots`). Keep the ⌘K "Scan" command on the parameterless `scan-live-sets` (that reference IS correct for ⌘K).
**Warning signs:** 422 Unprocessable Entity (missing `agent_id`/`scan_root`) on the empty-state button; or a scan that "succeeds" but discovers nothing because it fingerprinted an empty archive.

### Pitfall 3: The record body has Alpine islands → it needs `Alpine.initTree` after swap
**What goes wrong:** The record's pending-approval rows reuse `_diff_row.html`, which carries `x-data='{editing:false,...}'`. HTMX-swapping the record body into a host does NOT initialize that Alpine state (Alpine only auto-inits at page load). The inline EDIT/SAVE/DISCARD controls are dead.
**Why it happens:** The existing `htmx:afterSwap` handler (`shell.html:236`) re-inits Alpine ONLY for `#stage-workspace`, not a record host.
**How to avoid:** Put `hx-on::after-swap="if(window.Alpine) Alpine.initTree(this)"` on the record-body container (HTMX 2.0.10 supports `hx-on::`), OR broaden the shell's `afterSwap` handler to also `Alpine.initTree` the record host. (The palette results, by contrast, are static `option` markup with roving state in the modal `x-data` → no initTree needed.)
**Warning signs:** Clicking EDIT in the record does nothing; console shows no error (the directive simply never bound).

### Pitfall 4: `FileMetadata.artist` and `Tracklist.artist` are UNINDEXED
**What goes wrong:** The D-05 `SELECT DISTINCT artist … ILIKE '%q%'` runs a sequential scan on every keystroke. Over a ~200K-file archive this is a real per-keystroke cost.
**Why it happens:** `metadata.artist` (Text, nullable) has no index; `tracklists` indexes only `file_id`/`external_id`/`source`/`status` (`models/tracklist.py:46-49`) — not `artist`. The existing FTS also computes `to_tsvector` inline (no stored GIN), so the palette's Artists query is no worse than the existing search, but it is not "cheap" in the absolute sense.
**How to avoid:** Debounce the input (≥150–250ms), `LIMIT` the distinct results (e.g. top 10–20 by match), and gate the query on a minimum query length (≥2 chars). A real trigram/`GIN(lower(artist))` index or a materialized distinct-artist view would help but is a schema change — **out of the presentation-only scope**; note it as a follow-up (see Assumptions A2). D-05's "confirm the distinct query is cheap/indexed" resolves to: it is NOT indexed — mitigate with LIMIT + debounce, defer the index.
**Warning signs:** Palette input lag on a large archive; slow query logs on `metadata`/`tracklists` seq scans.

### Pitfall 5: `oob_counts=True` at initial fragment render collides on duplicate ids
**What goes wrong:** Emitting OOB "files ready" paragraphs on a stage/record render collides on duplicate ids with the DAG canvas seeds (documented in `shell.py:_render_stage`).
**How to avoid:** Keep `oob_counts=False` on every new fragment render (record, empty-state, agents). Live counts arrive only via the real `/pipeline/stats` swap. Mirror the Phase 58/60 branches exactly.

### Pitfall 6: The record must autoescape every DB-sourced cell (XSS)
**What goes wrong:** Filenames/paths/tags/artist names flow DB→HTML in the record. A raw/`| safe` render is an XSS sink — and Phase 60 already had a live XSS (apostrophe filenames like "Guns N' Roses" broke out of an Alpine JS-context; fixed with `|tojson` not `|e`).
**How to avoid:** Autoescape all values; in Alpine JS attribute contexts (`x-data`, `:aria-label`) use `|tojson`, never `|e`. `_diff_row.html` already does this (`{{ after|tojson }}`). Apply the same to the record header/facts/history and the palette rows.

## Code Examples

### Distinct-artist read (NEW, D-05 — mirror `get_summary_counts`)
```python
# services/search_queries.py — the ONE sanctioned additive read query (D-05)
async def distinct_artists(session: AsyncSession, query: str, *, limit: int = 20) -> list[str]:
    """Read-only distinct artist facet for the ⌘K Artists group. UNINDEXED columns —
    caller must debounce + gate on len(query) >= 2 (Pitfall 4)."""
    like = f"%{query}%"
    fm = select(FileMetadata.artist).where(FileMetadata.artist.is_not(None), FileMetadata.artist.ilike(like))
    tl = select(Tracklist.artist).where(Tracklist.artist.is_not(None), Tracklist.artist.ilike(like))
    rows = await session.execute(select(union_all(fm, tl).subquery().c.artist).distinct().limit(limit))
    return [a for (a,) in rows if a]
```
Source: shape mirrors `search_queries.py:167 get_summary_counts`; columns verified at `models/metadata.py:19`, `models/tracklist.py:35`.

### Compute-lane liveness (NEW, D-07 — mirror the degrade-safe count precedent)
```python
# Active / Waiting / Idle from CloudJob (read-only). Reuse the try/except → default shape
# from services/pipeline.py:1117. status members verified at models/cloud_job.py:38-46.
async def classify_compute_lanes(session: AsyncSession) -> tuple[str, int]:
    try:
        running = (await session.execute(
            select(func.count(CloudJob.id)).where(CloudJob.status == CloudJobStatus.RUNNING.value))).scalar() or 0
        waiting = (await session.execute(
            select(func.count(CloudJob.id)).where(
                CloudJob.status == CloudJobStatus.SUBMITTED.value, CloudJob.inadmissible.is_(True)))).scalar() or 0
    except SQLAlchemyError:
        return ("IDLE", 0)              # degrade-safe: never a DEAD/red state (KDEPLOY-04)
    if running:  return ("ACTIVE", running)
    if waiting:  return ("WAITING", waiting)
    return ("IDLE", 0)
```

### Fragment-correctness test (mirror `tests/test_shell_routes.py`)
```python
@pytest.mark.asyncio
async def test_record_fragment_is_bare(client: AsyncClient, seeded_file_id) -> None:
    r = await client.get(f"/record/{seeded_file_id}", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text and "<head" not in r.text     # R-5 bare fragment
    assert 'role="dialog"' in r.text or 'id="record-body"' not in r.text  # body-only swap
```

## State of the Art

Not applicable — no library/version churn. The stack is CLAUDE.md-locked; the one addition (`@alpinejs/focus@3.15.12`) is a first-party plugin pinned to the already-installed Alpine core version. Nothing is deprecated.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The computed SRI `sha384-ysJcnHb6oCzqAGKdoTm+IqKqmPKgxHT+ApZCawkyWOJfMq15WvzW3RRmHl7tWpEY` matches jsdelivr's served `@alpinejs/focus@3.15.12/dist/cdn.min.js`. | Package/CDN Audit | Low — verified this session (26,051 bytes, `Accept-Encoding: identity`). The integration SRI test re-verifies at test time; if jsdelivr re-minifies, recompute with the recipe given. |
| A2 | The unindexed distinct-artist query is acceptable for a single-user tool with debounce + LIMIT; a real index is out of presentation-only scope. | Pitfall 4 | Medium — if the archive's distinct-artist count is very large, the palette Artists group may lag. Mitigation (debounce/LIMIT/min-length) is in-scope; the index is a deferred follow-up. |
| A3 | The Agents page keeps its own single `/admin/agents/_table` 5s self-poll (refreshing both sections), satisfying D-07's "no manual reload" intent, rather than being re-homed onto `/pipeline/stats`. | OQ-1 | Medium — if the planner instead promotes Agents into the shell, the poll/seed wiring differs. Both are single-loop; recommend the standalone path for scope discipline. |
| A4 | The record's live badges bind to GLOBAL `$store.pipeline` keys (or accept staleness until reopened); `/pipeline/stats` is NOT made file-aware. | OQ-2 | Medium — D-02's "this file's … pending-approval count" has no clean per-file source on a global poll. Snapshot-only is the faithful reading; a per-file OOB would require a file-aware poll (rejected). |

## Open Questions (RESOLVED)

1. **OQ-1 — Is the Agents page a shell surface or the existing standalone `/admin/agents` page?**
   - **RESOLVED:** restyle the standalone `/admin/agents` in place (two-section C3 layout on its existing `_table` self-poll) — adopted by plan 61-04.
   - What we know: `/admin/agents` is a standalone full page (extends `base.html`) with its OWN single `/admin/agents/_table` 5s self-poll, and already carries a static k8s note (Phase 56). It is NOT in `STAGE_PARTIALS`; the header status strip links to `/admin/agents`. The ⌘K "Open Agents" command (D-03) would navigate there.
   - What's unclear: whether RECORD-03 restyles the standalone page in place, or promotes Agents into the shell chrome (new `STAGE_PARTIALS` entry / route riding the single `/pipeline/stats` poll).
   - Recommendation: **restyle the standalone `/admin/agents` in place** (two-section C3 layout; Section 2 = live CloudJob liveness on the SAME existing `_table` poll). Lowest risk, faithful to "no manual reload / no second loop", keeps backend untouched. Phase 62 (CUT) can re-home it into the shell if desired. D-07's "/pipeline/stats" phrasing reads as intent, not a literal requirement.

2. **OQ-2 — How do the open record's "counts/status" bits (D-02) get live updates from a file-agnostic global poll?**
   - **RESOLVED:** keep the record a true snapshot binding only global `$store.pipeline` keys; register no new per-file OOB ids — adopted by plan 61-02.
   - What we know: `/pipeline/stats` fans out `dag-seed-*` → `$store.pipeline` (global). `stats_bar.html` does not know which file's record is open.
   - What's unclear: how "this file's stage chips / pending-approval count" tick off a global poll without a file-aware request.
   - Recommendation: keep the record a true **snapshot**; bind only bits that map to a GLOBAL `$store.pipeline` key (e.g. a global pending count), and let per-file specifics be point-in-time (refreshed on reopen). Register NO new per-file OOB ids unless a clean global mapping exists. This preserves R-2/no-new-loop and matches "snapshot body renders once." The CONTEXT marks the record's OOB ids as discretion — this is the safe resolution.

3. **OQ-3 — Record route shape (discretion).** **RESOLVED:** dedicated `GET /record/{file_id}` (typed UUID, file_id-scoped, 404 friendly fragment) — adopted by plan 61-02. Recommendation: a dedicated `GET /record/{file_id}` (typed UUID path param, FastAPI-validated; scope all reads by that `file_id` — broken-access-control mitigation, mirroring `proposals.py:257` T-31-06-02). A missing/de-duplicated file → 404 with the friendly fragment (UI-SPEC copy), rendered inside the host; the close/focus-return contract still applies. Cleaner than overloading `/s/…` (which is the rail-stage whitelist).

## Environment Availability

No new external services. All work is application code + templates over the running stack (Postgres, Redis, FastAPI). Two build/tooling notes:

| Dependency | Required by | Available | Fallback |
|------------|------------|-----------|----------|
| `just tailwind` (standalone Tailwind v4 binary) | New record/palette/agents/empty-state utility classes → `/static/css/app.css` | ✓ (existing toolchain) | none needed; partials must live under a `@source`-covered path (`shell/partials/`, `pipeline/partials/` already covered) |
| jsdelivr CDN reachable (test time) | `test_cdn_sri_hashes_match_served_content` (network-gated `@pytest.mark.integration`) | ✓ (verified this session) | test self-skips offline; SRI computed above |
| `uv` + `uv run pytest` | Validation | ✓ | none |

**Missing dependencies with no fallback:** none. **Missing with fallback:** none.

## Validation Architecture

`nyquist_validation` is **enabled** (`.planning/config.json`). Framework: **pytest + pytest-asyncio + httpx AsyncClient** (`uv run pytest`), mirroring Phases 57–60. Existing async `client` fixture in `tests/conftest.py`; route+template assertion precedent in `tests/test_shell_routes.py`; SRI gate in `tests/test_base_html_sri.py`.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio + httpx AsyncClient |
| Config file | `pyproject.toml` (project-standard) |
| Quick run command | `uv run pytest tests/test_record_palette_agents.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (85% floor) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Automated command | File exists? |
|-----|----------|-----------|-------------------|-------------|
| RECORD-01 | `GET /record/{file_id}` returns a BARE fragment (no `<html>`/`<head>`) with header/facts/timeline/diff/identity/pending-approvals/history sections; scoped strictly by `file_id` | unit (route+template) | `uv run pytest tests/test_record_palette_agents.py::test_record_fragment_bare_and_scoped -x` | ❌ Wave 0 |
| RECORD-01 | Missing/de-duplicated file → 404 friendly fragment (not 500), close/focus contract intact | unit | `... ::test_record_missing_file_404_fragment -x` | ❌ Wave 0 |
| RECORD-01 | Record body carries `_diff_row.html` approval rows wired to the existing proposals/tags routes (approve/edit/undo URLs present) | unit | `... ::test_record_pending_approvals_wired -x` | ❌ Wave 0 |
| RECORD-02 | ⌘K grouped results endpoint returns Files/Tracklists/Artists/Commands groups over `search()` + `distinct_artists()`; rows are `role="option"`, headers `role="presentation"` | unit | `... ::test_cmdk_grouped_results -x` | ❌ Wave 0 |
| RECORD-02 | `distinct_artists()` returns DISTINCT `FileMetadata.artist`/`Tracklist.artist` matching the query, LIMIT-bounded, no None | unit | `... ::test_distinct_artists_query -x` | ❌ Wave 0 |
| RECORD-02 | Artists `Enter` navigates to the file list with `artist=` param; Scan command posts `/pipeline/scan-live-sets` | unit | `... ::test_cmdk_commands_and_artist_nav -x` | ❌ Wave 0 |
| RECORD-03 | Agents page renders Section 1 (heartbeating, `classify`/`sort_key`) + Section 2 (compute lanes) with Active/Waiting/Idle — **never a DEAD/rose state** | unit | `... ::test_agents_two_sections_never_dead -x` | ❌ Wave 0 |
| RECORD-03 | `classify_compute_lanes` → ACTIVE(running), WAITING(submitted+inadmissible), IDLE(none); degrades to IDLE on DB error | unit | `... ::test_compute_lane_liveness_states -x` | ❌ Wave 0 |
| RECORD-04 | file_count==0 renders the empty-state guide listing each agent + `scan_roots`; "Scan {agent}" posts `POST /pipeline/scans` (agent_id + scan_root), NOT `scan-live-sets`; no free-text path input | unit | `... ::test_empty_state_agent_roots_scan -x` | ❌ Wave 0 |
| RECORD-04 | file_count>0 does NOT render the empty state (branch correctness) | unit | `... ::test_empty_state_suppressed_when_files_exist -x` | ❌ Wave 0 |
| Dep/SRI (load-bearing) | `@alpinejs/focus@3.15.12` present in BOTH `shell.html` AND `base.html`, `<script defer>` before Alpine core, full-semver pinned, SRI matches | unit (extended SRI guard) | `uv run pytest tests/test_base_html_sri.py -x` (extended to scan `shell.html`) | ⚠ EXTEND existing |
| Focus/fragment (cross-cutting) | Record + palette + empty-state fragments are bare (no `<html>`/`<head>`); no `hx-trigger="every"`/`setInterval`/`hx-swap-oob` on approval-row subtrees | unit (fragment guard) | `... ::test_new_fragments_single_poll_clean -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_record_palette_agents.py tests/test_base_html_sri.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (85% floor; pre-commit hooks + mypy strict must pass — never `--no-verify`)
- **Phase gate:** full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_record_palette_agents.py` — route+template assertions for the record fragment, ⌘K grouped results, Agents two sections, and the empty-state branch (covers RECORD-01..04 + fragment guard)
- [ ] `tests/test_base_html_sri.py` — **EXTEND** `_extract_cdn_scripts` to also scan `shell.html` (parametrize over both templates), so the focus-plugin hash is guarded where the shell actually loads it (Pitfall 1)
- [ ] Fixtures: seed a file with `AnalysisResult` + `AnalysisWindow` rows (fine+coarse), a pending `RenameProposal` + tag comparison for the record's approvals, `FileMetadata`/`Tracklist` rows with distinct artists, `CloudJob` rows in running / submitted+inadmissible / none states, and an empty-DB case (file_count==0). Extend `tests/conftest.py` factories.
- [ ] Framework install: none — existing pytest infra covers all of this.

## Security Domain

`security_enforcement` is not disabled (absent = enabled). Scope: one CDN plugin + two read-only queries + server-rendered fragments over unchanged logic.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | **yes** | `file_id` is a typed UUID path param (FastAPI-validated); the ⌘K query is a bound ILIKE parameter (SQLAlchemy parameterized — no interpolation); the empty-state scan reuses `POST /pipeline/scans`'s existing `..`-traversal + `scan_roots` prefix validation (`pipeline_scans.py:319`). No new free-text path surface (D-08). |
| V4 Access Control | yes | Single-user admin tool on a private LAN (project constraint). Record reads scope strictly by `file_id` (mirror `proposals.py:257` T-31-06-02 broken-access-control mitigation). |
| V6 Cryptography / Supply chain | **yes** | The new CDN plugin is SRI SHA-384-guarded; the hash MUST be verified where the shell loads it (`shell.html`) — the current test only covers `base.html` (Pitfall 1). No secrets/crypto logic touched. |
| V1/V2/V3 (auth/session) | no | No auth/session changes. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| XSS via DB→HTML (filenames, artist names, paths, tags in record + palette) | Tampering | Jinja2 autoescape; `|tojson` (NOT `|e`) in Alpine JS-attribute contexts — the Phase 60 apostrophe-filename XSS class (`_diff_row.html` already correct) |
| Supply-chain (compromised CDN plugin) | Tampering | SRI SHA-384 pin + full-semver URL + first-party publisher; test extended to `shell.html` |
| Filesystem enumeration via empty-state | Info Disclosure | No directory-browse endpoint; scan reuses `scan_roots` prefix validation (D-08) |
| Broken access control (record for arbitrary file_id) | EoP | Typed UUID + scope all reads by `file_id`; 404 friendly fragment on miss |
| Focus-trap failing open (inert bypass) | — | Non-security functional risk, but Pitfall 1 makes it a correctness gate; validated by the extended SRI test + a focus-containment UAT check |

## Sources

### Primary (HIGH confidence — live codebase, read 2026-07-01)
- `src/phaze/templates/shell/shell.html` (script block `:32-39`, swap target `:160-164`, cmdk include `:172`, poll `:187`, afterSwap re-init `:230-247`); `src/phaze/templates/base.html` (`:32-39`, legacy nav `:168-216`); `src/phaze/templates/shell/partials/cmdk_modal.html`; `src/phaze/templates/shell/partials/header.html` (`#cmdk-trigger`)
- `src/phaze/routers/shell.py` (`_render_stage`, `STAGE_PARTIALS`, fragment-vs-full fork); `src/phaze/routers/search.py`; `src/phaze/services/search_queries.py`
- `src/phaze/routers/admin_agents.py`; `src/phaze/services/agent_liveness.py`; `src/phaze/models/agent.py`
- `src/phaze/models/cloud_job.py`; `src/phaze/services/pipeline.py:1117/1162` (CloudJob count precedents); `src/phaze/services/cloud_staging.py`
- `src/phaze/models/analysis.py`; `src/phaze/routers/proposals.py:243/257` (`row_detail` + `proposal_timeline`); `src/phaze/templates/proposals/partials/analysis_timeline.html`
- `src/phaze/templates/pipeline/partials/_diff_row.html`; `src/phaze/routers/pipeline.py:1165/1187` (`_enqueue_scan_jobs` + `scan-live-sets`); `src/phaze/routers/pipeline_scans.py:305` (`POST /pipeline/scans`) + `:150` (agent-roots)
- `src/phaze/models/metadata.py`, `src/phaze/models/tracklist.py` (artist columns, index audit); `alembic/versions/` (018 windows, 021 coverage, 028 completed_at)
- `tests/test_base_html_sri.py` (SRI gate scope); `.planning/config.json` (`nyquist_validation: true`)
- SRI computed this session: `curl … @alpinejs/focus@3.15.12/dist/cdn.min.js` → 26,051 bytes → `sha384-ysJcnHb6oCzqAGKdoTm+IqKqmPKgxHT+ApZCawkyWOJfMq15WvzW3RRmHl7tWpEY`

### Secondary (context)
- `.planning/phases/61-full-record-k-agents/61-CONTEXT.md` (D-01..D-08) + `61-UI-SPEC.md` (approved)
- `.planning/phases/60-review-apply/{60-RESEARCH,60-PATTERNS,60-VALIDATION}.md` (scaffold/fragment/OOB/R-2 precedent); `.planning/phases/57-shell-dag-rail/57-RESEARCH.md` (shell/OOB/fragment/history-reinit contracts)
- `.planning/ROADMAP.md` §Phase 61 + Notes; `.planning/REQUIREMENTS.md` §RECORD (lines 58-63, 114-117, milestone rule line 82)

## Metadata

**Confidence breakdown:**
- In-repo seams (endpoints/models/partials/routes): HIGH — every file:line read against the working tree; three corrections surfaced (SRI scope, scan endpoint, initTree).
- The one new dep (`@alpinejs/focus`) + SRI: HIGH — version cross-checked (both files pin 3.15.12), hash computed + will be re-verified by the integration test.
- Two read-only queries (distinct-artist, CloudJob liveness): HIGH — direct precedents exist (`get_summary_counts`, `services/pipeline.py:1117/1162`).
- Two integration decisions (Agents shell-vs-standalone; per-file OOB): MEDIUM — resolved with recommendations in §Open Questions; the planner/discuss should confirm.

**Research date:** 2026-07-01
**Valid until:** 2026-07-31 (stable internal codebase; re-verify line numbers if `shell.py`/`search_queries.py`/`admin_agents.py`/`cloud_job.py`/`base.html`/`shell.html` change before planning)
