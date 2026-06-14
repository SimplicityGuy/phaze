# Phase 41: Scrape and Match DAG Triggers - Context

**Gathered:** 2026-06-14
**Status:** Ready for planning
**Source:** Inline operator discussion (2026-06-14)

<domain>
## Phase Boundary

Give the **Scrape** and **Match** DAG nodes real manual triggers (today both are display-only chips gated on `tracklistDone === 0`). Add a bulk **Scrape** trigger (`scrape_and_store_tracklist` for every tracklist missing a scraped version) and a bulk **Match** trigger (`match_tracklist_to_discogs` for every tracklist not yet linked to Discogs). Both are "bulk over pending" — skip already-done rows — and disabled until ≥1 tracklist exists.

**In scope:** Scrape + Match bulk trigger endpoints + buttons on their DAG nodes + gating + busy counts + tests.
**Out of scope:** Search node (Phase 39, shipped), Fingerprint-Scan node (Phase 40, shipped), recovery automation (Phase 42), the `trigger_scan` file_id-only dead-letter fix (separate follow-up PR).
</domain>

<decisions>
## Implementation Decisions

### Trigger mechanism (LOCKED — "bulk over pending")
- **Scrape** button bulk-enqueues `scrape_and_store_tracklist` (keyed by `tracklist_id`) for every tracklist that has NO scraped version yet (the pending set = `scrapeTotal − scrapeDone`: tracklists not in `tracklist_versions`).
- **Match** button bulk-enqueues `match_tracklist_to_discogs` (keyed by `tracklist_id`) for every tracklist NOT yet linked to Discogs (pending = `matchTotal − matchDone`).
- Both skip already-complete rows. Deterministic keys (`scrape_and_store_tracklist:<tracklist_id>`, `match_tracklist_to_discogs:<tracklist_id>`) dedup in-flight replays.

### Routing (LOCKED — Phase 30 rule)
- Both tasks are CONTROLLER-side. Route via `enqueue_router.resolve_queue_for_task(...)` to the **controller** queue (mirror the existing per-tracklist `rescrape` / `match_discogs` routes in `routers/tracklists.py`). Never default.

### Prerequisite gate (LOCKED)
- Both buttons disabled until **≥1 tracklist exists** (`tracklistDone > 0` / `scrapeTotal > 0`). Keep the existing "Needs tracklist" reason when no tracklist.
- Recommend additionally surfacing a "nothing pending" state when tracklists exist but the pending set is empty (e.g. disabled with "All scraped" / "All matched") — confirm copy during planning. Add `scrapeBusy` / `matchBusy` in-flight counts (mirror Phase-39 `get_search_busy_count` degrade-safe SAVEPOINT scan) to disable while a batch runs ("Scraping…" / "Matching…").

### Eligible-set computation (Claude's Discretion — recommend)
- Compute the pending set server-side from the same queries that back `scrapeDone`/`matchDone` in `get_stage_progress` (tracklists NOT in `tracklist_versions`; tracklists NOT reachable from `discogs_links`). Background-enqueue one job per pending tracklist. Confirm exact queries against the models during planning.

### DAG layout (Claude's Discretion — guidance)
- The Scrape (`x760,y858,h114`) and Match (`x1128,y858,h114`) nodes are currently compact display chips with NO enqueue button. Adding the `enqueue_button` macro grows them ~46px (like Phase-39 grew `scan_search`). Both sit low in their columns with large vertical gaps above (proposals/execute), so growth is downward — verify against the current canvas height (1340 after Phase 40) and bump if needed. Recompute via the anchor-derived contract; do NOT hand-edit edges. Update the `< sm` `<ol>` text-equivalent and the layout guard tests.
- Preserve edge honesty (header comment): the chain is `scan_search → scrape → match`; do not add new edges.

### Manual-only principle (LOCKED, theme-wide)
- No auto-trigger. Automatic enqueue is reserved for the Phase 42 recovery pass.
</decisions>

<specifics>
## Specific Ideas

- Existing per-tracklist triggers to mirror: `routers/tracklists.py` — `rescrape_tracklist` (`resolve_queue_for_task("scrape_and_store_tracklist", ...)`) and `match_discogs` (`match_tracklist_to_discogs`). Both controller-routed, keyed by tracklist_id.
- Tasks: `tasks/tracklist.py::scrape_and_store_tracklist` (re-scrape → new version), `tasks/discogs.py` (or wherever) `match_tracklist_to_discogs`.
- Stats backing the pending sets: `services/pipeline.py::get_stage_progress` — `scrape.done = COUNT(DISTINCT TracklistVersion.tracklist_id)`, `match.done = match_done_stmt` (distinct tracklist_id reachable from discogs_links), both `total = tracklist_total`.
- Phase 39/40 patterns to reuse verbatim: bulk endpoint shape (`POST /pipeline/search-tracklists`, `/pipeline/scan-live-sets` — background enqueue, HTMX partial, loading/error slot), `get_search_busy_count` degrade-safe busy count, `enqueue_button` macro + node gating, busy/`*Busy` seeding through `_build_dag_context` + the `dag.items()` 5s OOB poll.
- Deterministic keys: `tasks/_shared/deterministic_key.py` — `scrape_and_store_tracklist`/`match_tracklist_to_discogs` → tracklist_id.

</specifics>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### DAG UI + Phase-39/40 patterns
- `src/phaze/templates/pipeline/partials/dag_canvas.html` — scrape/match nodes (currently display-only), enqueue_button macro, nodes-getter gating, NODE_LAYOUT/EDGES contract, `<ol>` a11y.
- `src/phaze/routers/pipeline.py` — `_build_dag_context`, the Phase-39/40 bulk endpoints to mirror, `*Busy` seeding, `/pipeline/stats`.
- `src/phaze/services/pipeline.py` — `get_stage_progress` (scrape/match done+total = pending derivation), `get_search_busy_count`, `get_scan_busy_count`.

### Scrape/Match tasks + routing
- `src/phaze/routers/tracklists.py` — `rescrape_tracklist`, `match_discogs` (per-tracklist controller-routed triggers to mirror).
- `src/phaze/tasks/tracklist.py` — `scrape_and_store_tracklist`; the match task module (`match_tracklist_to_discogs`).
- `src/phaze/services/enqueue_router.py` — `resolve_queue_for_task` (controller routing).
- `src/phaze/tasks/_shared/deterministic_key.py` — tracklist_id keys.
- `src/phaze/models/tracklist.py` — `Tracklist`, `TracklistVersion`; `discogs_link` model for match.

### Tests to keep green / extend
- `tests/test_dag_canvas_render.py`, `tests/test_pipeline_dag_context.py`, `tests/test_services/test_pipeline.py`, `tests/test_routers/test_pipeline.py`.
</canonical_refs>

<deferred>
## Deferred Ideas

- Recovery-only automation → Phase 42.
- `trigger_scan` file_id-only dead-letter fix → separate follow-up PR (found in Phase 40).
</deferred>

---

*Phase: 41-scrape-and-match-dag-triggers*
*Context gathered: 2026-06-14 via inline operator discussion*
