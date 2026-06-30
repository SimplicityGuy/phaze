# Architecture Research — v7.0 UI Redesign (DAG-Centric Hybrid Console)

**Domain:** Server-rendered admin UI rewrite (FastAPI + Jinja2 + HTMX + Tailwind + Alpine) over an existing two-host distributed backend
**Researched:** 2026-06-29
**Confidence:** HIGH (every integration point below is grounded in a real file under `src/phaze/`; one capability gap flagged at MEDIUM)

> **Scope discipline.** v7.0 is an **IA + presentation rewrite**. The non-negotiable constraint (PROJECT.md, REQUIREMENTS.md "Out of Scope") is **no backend behavior change**: routers' data logic and all `services/` stay unchanged. This document is therefore an *integration* architecture — it maps new templates/routes onto the existing router/service surface and is explicit about **new vs. modified vs. unchanged** at every step.

---

## 1. The integration model in one picture

```
┌────────────────────────────────────────────────────────────────────────┐
│ NEW: shell.html  (replaces base.html's nav row; keeps base.html's        │
│      <head>: theme store, fonts, HTMX/Alpine, phaze-bg tokens)           │
│  ┌──────────┬─────────────────────────────────┬──────────────────────┐  │
│  │ DAG RAIL │ CENTER WORKSPACE (#workspace)    │ FILE PANE (#file-pane)│  │
│  │ (#rail)  │  ← HTMX-swapped per stage        │  ← HTMX-swapped on row │  │
│  │ live     │                                  │     select             │  │
│  │ counts   │  renders a per-stage FRAGMENT    │                       │  │
│  └────┬─────┴──────────────┬──────────────────┴───────────┬───────────┘  │
└───────┼────────────────────┼──────────────────────────────┼──────────────┘
        │ hx-get             │ hx-get (HX-Request branch)     │ hx-get
        ▼                    ▼                                ▼
  NEW shell router      EXISTING routers (UNCHANGED logic)   NEW record router
  GET /                 pipeline · proposals · tracklists ·  GET /record/{id}
  GET /shell/{stage}    tags · cue · duplicates · execution
  GET /pipeline/stats ──┘ (reused 5s poll, UNCHANGED)
        │
        ▼
  services/ (pipeline_counters, search_queries, analysis, …) — UNCHANGED
```

**Core mechanism (already proven in this codebase):** an endpoint inspects `request.headers.get("HX-Request") == "true"` and returns a **fragment** for HTMX swaps, or a **full page** otherwise. Eight existing routers already do exactly this (`search.py:74`, `proposals.py:157`, `tracklists.py:152`, `tags.py:201`, `cue.py:228`, `duplicates.py:105`, `execution.py:372`, `admin_agents.py` helper). v7.0 **standardizes on the same pattern** and wraps it in a shell.

---

## 2. Component responsibilities

| Component | New / Modified / Unchanged | Responsibility | Backed by |
|-----------|---------------------------|----------------|-----------|
| `shell.html` | **NEW** (replaces `base.html` nav block) | Three-column flex shell: header (logo · ⌘K trigger · agent status dots · Agents link), `#rail`, `#workspace`, `#file-pane`, footer breadcrumb | reuses `base.html` `<head>` verbatim |
| DAG rail (`shell/_rail.html`) | **NEW** | Nav spine + live per-stage counts/dots; each node is an `hx-get` into `#workspace` | counts from existing `get_pipeline_stats` / `_build_dag_context` |
| Stage workspace fragments | **NEW templates** | One fragment per rail node (Discover/Metadata/Fingerprint/Analyze/Track-ID/Tracklist/Propose/Rename/Tag/Move/Dedupe/Cue) | render over **existing** router data |
| File pane (`shell/_file_pane.html`) | **NEW** | Right-column summary for the selected file (windowed sparkline + journey + "open full record") | `AnalysisWindow`, `FileRecord` |
| Full record (`record/detail.html`) | **NEW** | Slide-in over the shell: identity · metadata diff · multi-lane windowed timeline · this file's pending approvals · history | assembled from existing endpoints (§7) |
| ⌘K palette (`shell/_command_palette.html`) | **NEW** | Search input + quick commands; results from the existing search service | `services/search_queries.search` |
| Shell router (`routers/shell.py`) | **NEW thin router** | `GET /` (Analyze default) and stage-state assembly; the only genuinely new server route file | calls existing services read-only |
| Existing UI routers | **UNCHANGED logic; ADD an `HX-Request` fragment branch where missing** | Serve the same data; return a shell-shaped fragment for HTMX | themselves |
| `services/*` | **UNCHANGED** | All query/business logic | themselves |
| `base.html` `<head>` | **UNCHANGED** (theme store, fonts, HTMX/Alpine/SSE scripts, `phaze-bg`/`phaze-panel` tokens, Jura/Inter) | SHELL-04 brand/theme preservation comes free by reusing it | itself |

---

## 3. Template decomposition (avoid duplication; one fragment per stage)

The existing tree already uses the **page + `partials/` fragment** convention per feature (e.g. `proposals/list.html` extends `base.html`; `proposals/partials/proposal_content.html` is the HTMX swap body). v7.0 keeps this idea but introduces a **shell layer above it**.

Proposed `templates/` additions (NEW), existing dirs UNCHANGED until cutover:

```
templates/
├── base.html                 # UNCHANGED <head>; nav block retired in Phase 62
├── shell.html                # NEW — defines #rail / #workspace / #file-pane (reuses base.html <head>)
├── shell/
│   ├── _rail.html            # NEW — rail nodes + live counts (reuses dag context keys)
│   ├── _header.html          # NEW — logo · ⌘K trigger · status dots · Agents link
│   ├── _command_palette.html # NEW — ⌘K overlay (Alpine open/close + hx-get results)
│   ├── _file_pane.html       # NEW — right column
│   └── workspaces/
│       ├── discover.html     # NEW fragment — reuses pipeline scan/recent-scans data
│       ├── metadata.html     # NEW fragment — reuses /pipeline/extract-metadata trigger
│       ├── fingerprint.html  # NEW fragment — reuses /pipeline/fingerprint trigger
│       ├── analyze.html      # NEW fragment — 3 lane cards + in-flight queue
│       ├── trackid.html      # NEW fragment — SEE GAP §10 (IDENT-01)
│       ├── tracklist.html    # NEW fragment — Search→Scrape→Match 3-step
│       ├── propose.html      # NEW fragment — reuses proposals list data
│       └── review/{rename,tag,move,dedupe,cue}.html  # NEW fragments — reuse existing diff/approve partials
└── record/detail.html        # NEW — full-record slide-in
```

**Full-page vs. fragment from the same endpoint (the standardized pattern):**

```python
# Pattern already used in search.py:74, proposals.py:157, tags.py:201, etc.
context = {...}  # UNCHANGED data assembly
if request.headers.get("HX-Request") == "true":
    return templates.TemplateResponse(request=request, name="shell/workspaces/<stage>.html", context=context)
return templates.TemplateResponse(request=request, name="shell.html", context={**context, "active_stage": "<stage>"})
```

- **Direct visit / bookmark** (no `HX-Request`) → full `shell.html` with the rail rendered and the requested workspace inlined into `#workspace` (a `{% include %}` of the same fragment). This is what makes SHELL-05 bookmarks work without a client round-trip.
- **Rail click** (`HX-Request: true`) → fragment only, swapped into `#workspace`; the rail's active-state + live counts updated via **out-of-band swap** (§6).

**Anti-duplication rule:** the workspace fragment is the single source for a stage's body and is `{% include %}`-d by the full-page branch — never copy-pasted. This mirrors how `dashboard.html` already `{% include %}`s `stats_bar.html` and the cards.

---

## 4. HTMX fragment routing — map onto REAL existing routers

**Decision: reuse existing tab endpoints with an added/expanded `HX-Request` fragment branch; add ONE new thin router (`shell.py`) for `/`, the stage shell, and cross-cutting reads.** Do **not** duplicate query logic into new endpoints — that would risk drifting backend behavior.

| Rail stage | Workspace data source (EXISTING router:endpoint) | Trigger/action endpoints (EXISTING, UNCHANGED) | Router file |
|------------|--------------------------------------------------|------------------------------------------------|-------------|
| Discover | `pipeline.py:434 GET /pipeline/` context (`recent_scans`, backlog) | `pipeline_scans.py POST /pipeline/scans`; `pipeline.py:1280 /pipeline/recover` | `pipeline.py`, `pipeline_scans.py` |
| Metadata | new fragment over `get_pipeline_stats` | `pipeline.py:937 POST /pipeline/extract-metadata` | `pipeline.py` |
| Fingerprint | new fragment over stats + `fingerprint` model | `pipeline.py:1023 POST /pipeline/fingerprint`, `:1015 GET /api/v1/fingerprint/progress` | `pipeline.py` |
| Analyze | `pipeline.py` dashboard cloud-lane context (`pushing_count`, `analyzing_cloud_count`, `inadmissible_count`, `cloud_phase_counts`, lane cards) | `pipeline.py:625 POST /pipeline/analyze`, `:692 /pipeline/backfill-cloud`, `:800 /pipeline/files/{id}/deepen`; pause/priority via `pipeline_stages.py` | `pipeline.py`, `pipeline_stages.py` |
| Track-ID | **GAP — no backend (§10)** | — | — |
| Tracklist | `tracklists.py:78 GET /tracklists/` + `:158 /scan` + `:274 /scan/status` | `pipeline.py:1074 /search-tracklists`, `:1202 /scrape-tracklists`, `:1233 /match-tracklists`; `tracklists.py` approve/reject/link | `tracklists.py`, `pipeline.py` |
| Propose | `proposals.py:140 GET /proposals/` (already returns `proposal_content.html` on `HX-Request`) | `pipeline.py:857 /pipeline/proposals`; `proposals.py` approve/undo | `proposals.py`, `pipeline.py` |
| Review·Rename/Path | `proposals.py` (diff rows) + `preview.py /preview/` (tree) | `proposals.py PATCH /{id}/approve`; `execution.py:83 /execution/start` | `proposals.py`, `execution.py`, `preview.py` |
| Review·Tag write | `tags.py:140 GET /tags/` (HX branch at `:201`) | `tags.py:304 POST /{file_id}/write`, `:236 edit/{field}` | `tags.py` |
| Review·Move files | `execution.py` dispatch + `:269 progress/{batch_id}` | `execution.py:83 /execution/start` | `execution.py` |
| Review·Dedupe | `duplicates.py:79 GET /duplicates/` (HX branch at `:105`) | `:139 /{group}/resolve`, `:194 /resolve-all`, `:164/:228 undo` | `duplicates.py` |
| Review·Cue | `cue.py:176 GET /cue/` (HX branch at `:228`) | `:234 /{tracklist_id}/generate`, `:323 /generate-batch` | `cue.py` |
| Audit log | `execution.py:350 GET /audit/` | — (read-only + undo) | `execution.py` |
| Agents | `admin_agents.py GET /admin/agents` + `/_table` poll | — | `admin_agents.py` |

**Naming convention for the new shell entry points (NEW, in `shell.py`):**
- `GET /` → full shell, `active_stage="analyze"` (SHELL-01).
- `GET /shell/{stage}` → `HX-Request`→ workspace fragment; direct → full shell with that stage active (used for the `pipeline.py`-owned stages: discover/metadata/fingerprint/analyze).
- `GET /command?q=…` → ⌘K results fragment (delegates to `search_queries.search`).
- `GET /record/{file_id}` → full-record slide-in fragment.

**Recommended:** point rail nodes for stages that already own a canonical route at that route (`/proposals/`, `/tags/`, `/duplicates/`, `/cue/`, `/tracklists/`, `/audit/`) with `hx-get` + `hx-target="#workspace" hx-push-url="true"`, and use `shell.py GET /shell/{stage}` only for the four `pipeline.py`-owned Enrich/Analyze stages that have no single owning route today. This minimizes new surface and makes SHELL-05 redirects trivial — a bookmarked `/proposals/` simply renders inside the shell.

---

## 5. Redirect strategy (SHELL-05) — bookmarks must survive

Eight legacy routes must keep working: `/pipeline`, `/proposals`, `/tracklists`, `/tags`, `/cue`, `/duplicates`, `/search`, `/preview`.

**Two viable mechanisms — recommend the hybrid:**

| Legacy route | Strategy | Result |
|--------------|----------|--------|
| `/pipeline/` | **301/302 → `/`** (Analyze is the new home; SHELL-01 says no `/pipeline` URL) | `RedirectResponse` in `pipeline.py` dashboard, or drop `dashboard()` and let `shell.py GET /` own it |
| `/proposals/`, `/tags/`, `/cue/`, `/duplicates/`, `/tracklists/` | **Render-in-shell** — keep the route; on a non-`HX-Request` GET, return `shell.html` with that stage active (existing fragment inlined). No redirect needed; the URL *is* the stage state. | Bookmark lands on the shell with the right workspace |
| `/search/` | **301 → `/`** + auto-open ⌘K (search became the command palette, SHELL-03) | `RedirectResponse("/?cmd=1")`; shell reads `cmd` query and opens the palette |
| `/preview/` | **Render-in-shell** under the Rename/Move review stage (preview tree is a Move sub-view) | Bookmark lands on Review→Move |

**Why hybrid over pure redirects:** the design's IA *is* "URL = stage state," so the cleanest SHELL-05 implementation is to make the existing feature routes render inside the shell when hit directly (no `HX-Request`), and reserve `RedirectResponse` for true *renames* (`/pipeline`→`/`, `/search`→⌘K). Use a query param for sub-state where a stage has tabs (`/?stage=rename`, `/tracklists/?step=match`). This avoids breaking deep links and needs zero new redirect table. **No `RedirectResponse` exists in the codebase today** (grep confirms) — these are net-new, isolated, side-effect-free additions (allowed: routing, not behavior).

---

## 6. Live data flow + out-of-band rail/header updates

**Reuse the existing 5s poll verbatim.** `dashboard.html:50` already does:
```html
<div id="pipeline-stats" hx-get="/pipeline/stats" hx-trigger="every 5s" hx-swap="innerHTML">
```
and `pipeline.py:549 /pipeline/stats` returns `stats_bar.html`, which carries a **rich OOB seed block** (`hx-swap-oob="true"` paragraphs that write into the `$store.pipeline` Alpine store — see `stats_bar.html` and the store definition at `base.html:106`). Every count the rail needs (discovered/analyzed/metadata/fingerprint/proposals/approved/executed, per-stage busy, cloud-phase admission, inadmissible, localqueue-unreachable) is **already produced** by `pipeline.py:549` and `_build_dag_context`.

**v7.0 reuse plan (no new poll loop, no new query):**
1. Mount the same `hx-get="/pipeline/stats" hx-trigger="every 5s"` on a hidden node inside `shell.html`.
2. The rail counts/dots, header status dots, and Analyze lane cards bind to `$store.pipeline.*` (the store already exists and is OOB-fed). The rail is **just a new presentation of the existing store** — no backend change.
3. **OOB swap alongside a workspace swap:** when a rail click swaps `#workspace`, return the workspace fragment **plus** an `hx-swap-oob="true"` rail-active-state node (and, if desired, a fresh rail-count node) in the same response — exactly the OOB idiom `stats_bar.html` documents. The 5s poll independently keeps counts live via the store; the click only needs to flip the selected node's highlight.
4. Workspace-internal progress (scan progress `pipeline_scans` poll, fingerprint `/api/v1/fingerprint/progress`, execution `/execution/progress/{batch_id}`, tracklist `/scan/status`) all **already exist** and drop into the workspace fragment unchanged.

**Key reuse target to name for the roadmapper:** `pipeline.py:549 pipeline_stats_partial` + `templates/pipeline/partials/stats_bar.html` + the `$store.pipeline` store at `base.html:106`. The rail is a re-skin of this store; do not invent a parallel counts endpoint.

---

## 7. Per-file full record (RECORD-01) — assembled from existing endpoints/services

The record slide-in is a **new template composing existing data**; every section has a real backend source:

| Record section | Existing source (router / service / model) | New / Unchanged |
|----------------|---------------------------------------------|-----------------|
| Identity (name/path/format/size/sha256/lane) | `FileRecord` (`models/file.py`) | UNCHANGED model |
| Multi-lane **windowed analysis timeline** (BPM/key/energy over windows) | `AnalysisWindow` + `AnalysisResult` (`models/analysis.py`, Phase 31/43); rendered today by `proposals/partials/analysis_timeline.html` (`bpm_points`, `has_windows`, `fine_windows_*`) | UNCHANGED data; REUSE the existing timeline partial |
| Metadata diff (before→after) | `FileMetadata` (`models/metadata.py`) + `tags.py:207 /{file_id}/compare` (`tag_comparison.html`) | REUSE existing compare fragment |
| Identity / tracklist match / proposed name | `tracklists.py` link state + `RenameProposal` (`models/proposal.py`) | UNCHANGED |
| This file's **pending approvals (inline-approvable)** | `proposals.py PATCH /{id}/approve`, `tags.py POST /{file_id}/write` | REUSE existing approve endpoints (the diff/approve partials drop straight in) |
| History | `ExecutionLog` / audit (`execution.py:350`), `tag_write_log` (`models/tag_write_log.py`) | UNCHANGED |

**Implication:** RECORD-01 is the lowest-risk requirement — pure composition of partials that already render elsewhere. The "Deepen analysis" action (`pipeline.py:800`) and `sampled_badge` are already wired in `analysis_timeline.html` and carry into the record for free.

---

## 8. ⌘K command palette (RECORD-02)

**A search service already exists — reuse it, do not build a new one.** `routers/search.py` delegates to `services/search_queries.search` + `get_summary_counts`, a **three-entity UNION ALL search over file / tracklist / discogs** (PROJECT.md v3.0). It already returns an `HX-Request` fragment (`search/partials/results_content.html`).

**Palette wiring (NEW template + thin route, UNCHANGED service):**
- `shell/_command_palette.html`: Alpine `x-data` open/close; `⌘K`/`Ctrl-K` keydown handler (mirror the prototype's `keydown` listener; Escape closes); an input with `hx-get="/command" hx-trigger="keyup changed delay:200ms" hx-target="#cmd-results"`.
- `GET /command` (NEW, in `shell.py`): calls `search_queries.search(q)` and renders a palette-shaped fragment (files/tracklists/artists sections) — thin adapter over the **existing** query, no new SQL.
- **Quick commands** (scan / jump-to-stage / open Agents) are static client-side entries that dispatch via `hx-get` to the corresponding rail route (`/shell/discover`, `/proposals/`, `/admin/agents`) targeting `#workspace`, or open the scan modal. The prototype's `go(id)` map is the reference; in production each command is an `hx-get` link.

**Risk:** "artists" as a first-class search facet — verify `search_queries.search` surfaces an artist dimension; if it only does file/tracklist/discogs, "artists" maps to file/tracklist artist fields (no backend change), not a new index.

---

## 9. Suggested build order (phases 57–62) and integration dependencies

The dependency spine is strict: **the shell must exist before any workspace can be swapped into it, and dead-code removal must be last** (it can only be safe once every legacy route is either rendered-in-shell or redirected).

| Phase | Theme | Builds | Depends on | New / Modified / Unchanged |
|-------|-------|--------|-----------|----------------------------|
| **57** | Shell & rail (SHELL-01..05) | `shell.html`, `_rail.html`, `_header.html`, ⌘K *skeleton*, `GET /`, the `HX-Request`/full-page branch convention, **render-in-shell + redirect mapping for all 8 legacy routes**, theme reused from `base.html` `<head>` | nothing (foundation) | NEW shell templates + `shell.py`; ADD redirect/render-in-shell branches to existing routers (routing only); `base.html <head>` UNCHANGED |
| **58** | Enrich + Analyze (WORK-01..05) | Discover/Metadata/Fingerprint/Analyze fragments; 3 lane cards; reuse the `/pipeline/stats` 5s poll + `$store.pipeline` OOB seeds | 57 (shell + `#workspace`) | NEW fragments over `pipeline.py`/`pipeline_scans.py`/`pipeline_stages.py` data; backend UNCHANGED |
| **59** | Identify (IDENT-01..02) | Tracklist Search→Scrape→Match 3-step fragment (reuses `tracklists.py` + `pipeline.py` triggers); Track-ID fragment **— resolve §10 GAP first** | 57; shares poll plumbing with 58 | NEW fragments; **IDENT-01 may require a scoped backend addition — flag** |
| **60** | Review & Apply (REVIEW-01..05) | Unified before→after diff gate for Rename/Tag/Move + Dedupe keeper-select + Cue preview, per-file + bulk-high-conf | 57; reuses 58's file-row→pane plumbing | NEW review fragments wrapping EXISTING `proposals`/`tags`/`execution`/`duplicates`/`cue` approve endpoints; approve logic UNCHANGED |
| **61** | Full record + ⌘K + Agents (RECORD-01..04) | Record slide-in (§7 composition), full ⌘K over `search_queries`, Agents page incl. ephemeral k8s identity, first-run empty state | 58–60 (record links into their fragments); 57 (⌘K skeleton) | NEW record/palette templates; REUSE `search`, `admin_agents`, `analysis_timeline`; backend UNCHANGED |
| **62** | Polish & cutover (CUT-01..04) | a11y (keyboard rail/⌘K, focus, skip link, ARIA on DAG), narrow-width rail-collapse, docs/README, **CUT-02 dead-code removal** | **ALL of 57–61** | **DELETE** retired `list.html` page wrappers + `base.html` nav block + any now-unused routes; partials kept if reused as fragments |

**Where CUT-02 (dead-code cutover) safely happens — and why last:** removal is only safe once (a) every legacy bookmark is served by a render-in-shell branch or a `RedirectResponse` (Phase 57), and (b) every workspace/record/palette has replaced its old page (Phases 58–61). At that point the deletable set is: the `*/list.html` / `*/page.html` **full-page wrappers** (`proposals/list.html`, `tags/list.html`, `duplicates/list.html`, `cue/list.html`, `tracklists/list.html`, `search/page.html`, `preview/tree.html`, `pipeline/dashboard.html`) and the **nav block in `base.html`**. **Keep** the reusable `partials/` (proposal rows, tag comparison, diff bodies, `analysis_timeline.html`, `stats_bar.html`) — they become the shell's fragments. A static guard test (the repo already favors AST guards, e.g. the enqueue-router AST test) should assert no `{% extends "base.html" %}` page wrapper and no legacy nav route remain after 62.

---

## 10. Critical gap / risk for the roadmapper

### GAP — IDENT-01 "Track-ID (AcoustID → MusicBrainz)" has no backend (MEDIUM confidence)
`grep` across `src/phaze/` finds **zero** `acoustid` / `musicbrainz` / recording-match code. The existing "fingerprint" capability is **chromaprint + audfprint + Panako** (per-agent dedup fingerprinting, `models/fingerprint.py`), **not** an AcoustID→MusicBrainz *recording identification* pipeline. The design doc (§6) and prototype show a Track-ID workspace with "AcoustID → MusicBrainz recording match + confidence."

**This collides with the "no backend behavior change" constraint.** Options for Phase 59, in order of scope-safety:
1. **Re-scope IDENT-01 to existing data** — surface the existing fingerprint/AcoustID-lookup *match state* the fingerprint stage already persists (verify what `models/fingerprint.py` + `routers/agent_fingerprint.py` store) rather than a new MusicBrainz join.
2. **Defer IDENT-01** to a v7.x backend milestone and ship only the Tracklist half of Identify in v7.0.
3. **Treat IDENT-01 as a (small, explicit) backend addition** — a deliberate exception to the no-backend rule, planned as such.

**Action:** the roadmapper must resolve this before Phase 59 planning. It is the single requirement whose UI cannot be a pure presentation rewrite over today's services.

### Lesser risks
- **OOB id collisions.** `stats_bar.html` documents that `hx-swap-oob` ids must be emitted **only** on poll responses (`oob_counts` gate) to avoid duplicate-id DOM at full-page load. The shell must preserve this discipline (rail count seeds and the dashboard seeds must not both render the same id at initial load).
- **`$store.pipeline` is large and load-bearing.** Every `:disabled` button binding and DAG count reads it (`base.html:106`). The rail should *consume* it, not redefine it; redefining keys risks `undefined` reads before the first poll tick (the store comments warn about this explicitly).
- **Two-host reality is invisible to the UI but real.** Triggers route through `enqueue_router.resolve_queue_for_task` (per-agent queues); a 0-agent state returns 503/empty-state. Workspace fragments must keep rendering those empty/needs-agent states (the store already carries `agentOnline`).

---

## 11. New vs. Modified vs. Unchanged — explicit summary

**NEW (templates + thin routing only):**
- `shell.html` + `shell/` template subtree (rail, header, command palette, file pane, all workspace fragments, record detail).
- `routers/shell.py` — `GET /`, `GET /shell/{stage}` wrappers (for the `pipeline.py`-owned stages), `GET /command`, `GET /record/{file_id}`. All call existing services read-only.
- `RedirectResponse` for `/pipeline`→`/` and `/search`→`/?cmd=1` (no redirects exist today; net-new, side-effect-free).

**MODIFIED (presentation/routing branch only — NO data-logic change):**
- Existing UI routers gain (or have expanded) an `HX-Request` fragment branch + a render-in-shell full-page branch. `proposals.py`, `tags.py`, `duplicates.py`, `cue.py`, `tracklists.py`, `search.py`, `execution.py` already have the `HX-Request` half; the change is which template they return and adding the shell wrapper.
- `base.html`: nav block retired in Phase 62; `<head>` (theme store, fonts, scripts, tokens) **stays**.

**UNCHANGED (hard constraint):**
- All of `services/` (`pipeline_counters`, `search_queries`, `analysis`, `pipeline`, `collision`, etc.).
- All models, migrations, the `/api/internal/agent/*` surface, SAQ tasks, `enqueue_router`, the v6.0 local/A1/k8s routing, `pipeline.py:549 /pipeline/stats` data, `_build_dag_context`.
- The 5s poll contract and the `$store.pipeline` store shape.

---

## 12. Patterns to follow (grounded in this repo)

1. **`HX-Request` full-vs-fragment branch** — `search.py:74`, `proposals.py:157`. Standardize every shell route on it.
2. **Page `{% include %}`s its own fragment** — `dashboard.html` includes `stats_bar.html` + cards. Never duplicate a fragment's markup into the page branch.
3. **OOB store-seed on the 5s poll** — `stats_bar.html` + `$store.pipeline`. Reuse for rail/header live counts; gate OOB ids behind a poll-only flag.
4. **Per-feature `partials/` directory** — keep new fragments under `shell/workspaces/`; keep reusable rows (diff/approve) where they are and `{% include %}` them.
5. **Service-owns-degrade** — every dashboard count service returns 0/False on DB/Redis error so the poll never 500s (`pipeline.py:480-520` comments). The shell inherits this for free by reusing those services.

## 13. Anti-patterns to avoid

- **Duplicating query logic into new shell endpoints** — would fork backend behavior and violate the no-backend rule. Always delegate to the existing service/router.
- **A second polling loop** — reuse `/pipeline/stats`; do not add a `/rail/stats`.
- **Redefining `$store.pipeline` keys** — consume the existing store; new keys risk `undefined`-before-first-poll bugs the store comments warn about.
- **Hard redirects for stages that have a home route** — render-in-shell instead, so the URL stays the stage state (cleaner SHELL-05, deep-link-safe).
- **Removing `partials/` during cutover** — only the page *wrappers* and nav are dead; the fragments are the shell's body.

## Sources

- `src/phaze/routers/search.py:74`, `proposals.py:157`, `tags.py:201`, `duplicates.py:105`, `cue.py:228`, `tracklists.py:152`, `execution.py:372`, `admin_agents.py` — the `HX-Request` full-vs-fragment pattern (HIGH, primary code).
- `src/phaze/routers/pipeline.py:434,549,625,800,857,937,1023,1074,1202,1233,1280` — dashboard + stage triggers + the reused 5s `/pipeline/stats` poll (HIGH).
- `src/phaze/templates/base.html:54-138,178-269` — theme store, `$store.pipeline`, nav block to retire, `phaze-bg`/Jura tokens (HIGH).
- `src/phaze/templates/pipeline/{dashboard.html,partials/stats_bar.html}` — poll trigger + OOB seed idiom (HIGH).
- `src/phaze/models/analysis.py` (`AnalysisResult`, `AnalysisWindow`) + `templates/proposals/partials/analysis_timeline.html` — windowed timeline backing RECORD-01 (HIGH).
- `src/phaze/main.py:182-229` — full router registration list; no `/` route today, no `RedirectResponse` anywhere (HIGH).
- `grep acoustid|musicbrainz src/phaze/` → **no matches** — IDENT-01 backend gap (MEDIUM; absence-of-evidence, verify `pyacoustid` persistence in `fingerprint.py`/`agent_fingerprint.py`).
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` + `…-assets/prototype.html` — locked design spine, rail/⌘K/record structure (HIGH, design authority).
- `.planning/REQUIREMENTS.md` (SHELL/WORK/IDENT/REVIEW/RECORD/CUT, phase 57–62 traceability) (HIGH).
