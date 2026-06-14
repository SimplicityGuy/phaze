# Phase 39: Tracklist Search DAG Node - Context

**Gathered:** 2026-06-14
**Status:** Ready for planning
**Source:** Inline operator discussion (AskUserQuestion rounds, 2026-06-14)

<domain>
## Phase Boundary

Make the DAG the manual control surface for **name-based** tracklist discovery. Today the tracklist head ("Scan / Search") is a display-only chip on the pipeline DAG — its only triggers live on the Tracklists "Scan" tab and the Proposals page (`POST /tracklists/search`). And the Tracklists empty-state's promise that search "starts automatically" after metadata is **unwired** (`metadata_extraction` does not chain to `search_tracklist`; no cron sweeps unmatched files).

This phase adds a **Search** node to the DAG with a real bulk trigger button (same chrome as the Phase-38 agent stages) that enqueues `search_tracklist` over eligible files, gated disabled until Metadata has produced tags. Manual only — no auto-trigger added.

**In scope:** the name-search ("1001Tracklists") path only.
**Out of scope:** fingerprint-scan path (`scan_live_set`) → Phase 40; Scrape/Match triggers → Phase 41; recovery automation → Phase 42.
</domain>

<decisions>
## Implementation Decisions

### Trigger mechanism (LOCKED)
- The Search node's button drives a **bulk `search_tracklist`** enqueue over all eligible files. (`search_tracklist` already scrapes + stores inline on a hit, so a successful search produces a tracklist WITH a version.)
- This runs **independently** of the fingerprint-scan path (Phase 40). Operator wants BOTH paths run over all files, **no fallback** between them.

### Prerequisite gate (LOCKED)
- The Search button is **disabled until Metadata has produced tags** (`metadataDone > 0`), because `search_tracklist` builds its query from the artist tag (or a parseable filename). The current DAG gates `scan_search` on `discovered`; this phase moves the Search node's gate to the metadata signal.
- Reuse the per-stage busy gating pattern shipped in quick-task 260613-t7k / Phase 38 (`get_stage_busy_counts`-style + "busy" disable). A "search busy" count should disable the button while a search batch is in flight.

### Routing (LOCKED — Phase 30 rule)
- The bulk endpoint MUST route through `enqueue_router` to the **controller** queue (`search_tracklist` is a controller-side task, NOT a per-agent task). Never the default queue. Mirror the existing `POST /tracklists/search` (`manual_search`) routing via `resolve_queue_for_task("search_tracklist", ...)`.

### Eligible set (Claude's Discretion — recommend)
- Recommend: enqueue `search_tracklist` for files that do NOT already have a tracklist (skip files already matched), so re-runs are cheap and idempotent. Deterministic key `search_tracklist:<file_id>` already dedups in-flight replays (`deterministic_key.py`). Confirm the exact "eligible" query during planning against the `tracklists` / `FileRecord` models.

### UI placement (Claude's Discretion)
- The DAG currently has a single rose "Scan / Search" node (`scan_search`). For Phase 39, turn that node (or its Search half) into a triggerable node with the standard enqueue button + state pill + count. Phase 40 adds the sibling Fingerprint-Scan node. Planner decides whether to rename `scan_search`→`search` now or keep one node and split visually in 40 — keep the NODE_LAYOUT/edge-derivation contract intact (see dag_canvas.html header comments) and the `< sm` `<ol>` text-equivalent in lockstep.

### Manual-only principle (LOCKED, theme-wide)
- Do NOT add any auto-trigger (no chaining off metadata completion, no cron sweep). Automatic enqueue is reserved for the Phase 42 recovery pass.
</decisions>

<specifics>
## Specific Ideas

- Existing single-file trigger to mirror: `routers/tracklists.py::manual_search` (`POST /tracklists/search?file_id=...`) → `resolve_queue_for_task("search_tracklist", ...)` → `routed.queue.enqueue("search_tracklist", file_id=...)`.
- Task: `tasks/tracklist.py::search_tracklist(ctx, *, file_id)` — parses filename first (`parse_live_set_filename`), falls back to `file_metadata.artist`; returns `no_query` when no artist; scrapes + stores inline on a hit; auto-links at confidence ≥ 90.
- DAG stats source: `services/pipeline.py::get_stage_progress` — `scan_search.done = COUNT(DISTINCT Tracklist.file_id)`, `total = None`. `_build_dag_context` in `routers/pipeline.py` folds sources into the `dag` map that seeds `$store.pipeline` and re-pushes on the 5s `/pipeline/stats` OOB poll.
- Per-stage busy pattern just shipped: `services/pipeline.py::get_stage_busy_counts` (reads `saq_jobs` by deterministic-key function prefix, degrade-safe via SAVEPOINT). Search busy = count of in-flight `search_tracklist:*` jobs.
- Template: `templates/pipeline/partials/dag_canvas.html` — `enqueue_button` macro, `nodes` getter gating, NODE_LAYOUT anchor-derived edges, `<ol>` text-equivalent.
</specifics>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### DAG UI + gating pattern
- `src/phaze/templates/pipeline/partials/dag_canvas.html` — node chrome, enqueue_button macro, nodes-getter gating, NODE_LAYOUT/edge contract, `<ol>` a11y equivalent.
- `src/phaze/routers/pipeline.py` — `_build_dag_context`, `/pipeline/stats` poll, existing `/pipeline/extract-metadata|analyze|fingerprint` trigger endpoints (the pattern a bulk search endpoint should mirror).
- `src/phaze/services/pipeline.py` — `get_stage_progress`, `get_stage_busy_counts`, `queue_activity`.

### Tracklist task + routing
- `src/phaze/routers/tracklists.py` — `manual_search`, `trigger_scan` (per-agent), `rescrape`, `match_discogs`.
- `src/phaze/tasks/tracklist.py` — `search_tracklist`, `scrape_and_store_tracklist`.
- `src/phaze/services/enqueue_router.py` — `resolve_queue_for_task` / `NoActiveAgentError` (Phase 30 routing rule).
- `src/phaze/tasks/_shared/deterministic_key.py` — `search_tracklist:<file_id>` key contract.

### Tests to keep green / extend
- `tests/test_dag_canvas_render.py`, `tests/test_pipeline_dag_context.py`, `tests/test_services/test_pipeline.py`, `tests/test_routers/` (pipeline + tracklists).
</canonical_refs>

<deferred>
## Deferred Ideas

- Fingerprint-scan node (`scan_live_set` bulk trigger) → Phase 40.
- Scrape + Match bulk triggers → Phase 41.
- Recovery-only automation (gate `reenqueue_discovered`) → Phase 42.
</deferred>

---

*Phase: 39-tracklist-search-dag-node*
*Context gathered: 2026-06-14 via inline operator discussion*
