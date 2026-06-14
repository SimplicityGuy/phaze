# Phase 40: Tracklist Fingerprint-Scan DAG Node - Context

**Gathered:** 2026-06-14
**Status:** Ready for planning
**Source:** Inline operator discussion (2026-06-14) + Phase 39 split decision

<domain>
## Phase Boundary

Add the SECOND, independent tracklist-discovery node to the DAG: a **Fingerprint Scan** node whose bulk trigger enqueues `scan_live_set` (agent-side audio-fingerprint identification of a live set) over discovered files. Phase 39 turned the head into a name-**Search** node; this phase adds its sibling. Both produce tracklists; operator wants BOTH run over all files with **no fallback** between them.

**In scope:** the fingerprint-scan path (`scan_live_set`) bulk trigger node + its prerequisite gate + per-stage busy gating + tests.
**Out of scope:** name-search node (shipped Phase 39); Scrape/Match triggers (Phase 41); recovery automation (Phase 42).
</domain>

<decisions>
## Implementation Decisions

### Trigger mechanism (LOCKED)
- New DAG node with a bulk trigger button that enqueues `scan_live_set` over discovered files. `scan_live_set` is a **PER-AGENT** task — it fingerprints the audio on the file-server agent and POSTs back a resolved tracklist.
- Runs INDEPENDENTLY of the Phase 39 Search node. No fallback; both run over all files.

### Routing (LOCKED — Phase 30 rule)
- Route via `enqueue_router.resolve_queue_for_task("scan_live_set", ...)` → per-agent queue (active-agent selection), exactly like the existing `routers/tracklists.py::trigger_scan`. NOT the controller queue, NOT default.

### Prerequisite gate (LOCKED)
- The Fingerprint-Scan button is **disabled unless there are discovered files AND an online agent**. `scan_live_set` needs an agent to run; with zero agents the enqueue raises `NoActiveAgentError`. Surface a clear disabled state / reason (e.g. "Needs agent" when no agent online, "No files discovered" when discovered === 0).
- The dag context must expose an "agent online" signal (e.g. a boolean / active-agent count) so the gate can react. `select_active_agent` (enqueue_router) defines "online" = `revoked_at IS NULL AND last_seen_at IS NOT NULL`. Reuse that definition; do NOT invent a new liveness rule.
- Add a per-stage "scan busy" count (in-flight `scan_live_set` jobs) to disable the button while a batch runs, mirroring the Phase-39 `searchBusy` / `get_search_busy_count` pattern (degrade-safe SAVEPOINT scan of `saq_jobs` by deterministic-key prefix). NOTE: `scan_live_set` jobs live on per-agent queues but the same `saq_jobs` table (Postgres backend), so the same key-prefix scan works.

### Eligible set (Claude's Discretion — recommend)
- Recommend: enqueue `scan_live_set` for discovered files that do NOT already have a tracklist (skip already-matched). Confirm against models during planning. Deterministic key `scan_live_set:<file_id>` dedups in-flight replays.

### DAG layout (Claude's Discretion — guidance)
- This ADDS a node (10th) to NODE_LAYOUT. The current head node key is `scan_search` (the Phase-39 Search node, name-search). Add a sibling fingerprint-scan node — pick a clear key (e.g. `fingerprint_scan`) and a distinct color. Both discovery→{search, fingerprint_scan} edges, and both should feed the downstream tracklist chain (Scrape gates on "tracklist exists", which either producer satisfies). The planner decides exact x/y placement.
- MANDATORY: preserve the DAG layout contract documented in `dag_canvas.html` header comments — anchor-derived edges (never hand-typed coords), 240px chip width, no-overlap guards (vertical AND horizontal), the em-dash denominator convention for counter-only nodes, and the `< sm` `<ol>` text-equivalent in lockstep. Recompute canvas/SVG width/height + column anchors as needed and update the guard tests.
- IMPORTANT naming caveat: there is ALSO an existing **"Fingerprint"** node (`fingerprint_file` — acoustic fingerprint for DEDUP, via pyacoustid/chromaprint). That is a DIFFERENT concept from `scan_live_set` (live-set identification). Name the new node clearly to avoid confusion (e.g. "Fingerprint Scan" / "Identify Set") and do NOT touch the existing `fingerprint` node.

### Manual-only principle (LOCKED, theme-wide)
- No auto-trigger. Automatic enqueue is reserved for the Phase 42 recovery pass.
</decisions>

<specifics>
## Specific Ideas

- Existing per-agent scan trigger to mirror: `routers/tracklists.py::trigger_scan` (`POST /tracklists/scan`) — `resolve_queue_for_task("scan_live_set", ...)`, catches `NoActiveAgentError` → renders a visible "no active agent" empty-state, enqueues nothing.
- Task: `tasks/scan.py::scan_live_set` (agent-side) — fingerprint-queries a live-set file, POSTs the resolved tracklist via the internal HTTP API (`create_tracklist`).
- Phase-39 patterns to reuse verbatim: `POST /pipeline/search-tracklists` (bulk trigger endpoint shape, background enqueue, HTMX partial response), `get_search_busy_count` (degrade-safe busy count), the `enqueue_button` macro + node gating in `dag_canvas.html`, `searchBusy` seeding through `_build_dag_context` + the `dag.items()` 5s OOB poll.
- Agent-online: `enqueue_router.select_active_agent` / the agents query in `services/pipeline.py::queue_activity` (iterates `Agent.revoked_at IS NULL`).
- Deterministic key: `tasks/_shared/deterministic_key.py` — `scan_live_set` → file_id.
</specifics>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### DAG UI + Phase-39 patterns (the template to follow)
- `src/phaze/templates/pipeline/partials/dag_canvas.html` — NODE_LAYOUT/EDGES contract, enqueue_button macro, nodes-getter gating, the Phase-39 `scan_search` Search node + `searchBusy` gate (mirror this), `<ol>` a11y equivalent.
- `src/phaze/routers/pipeline.py` — `_build_dag_context`, `/pipeline/stats` poll, `POST /pipeline/search-tracklists` (the bulk endpoint to mirror), `searchBusy` seeding.
- `src/phaze/services/pipeline.py` — `get_search_busy_count`, `get_stage_busy_counts`, `queue_activity` (agent enumeration), `get_stage_progress`.

### Fingerprint-scan task + routing
- `src/phaze/routers/tracklists.py` — `trigger_scan` (per-agent scan_live_set, 0-agent handling).
- `src/phaze/tasks/scan.py` — `scan_live_set`.
- `src/phaze/services/enqueue_router.py` — `resolve_queue_for_task`, `select_active_agent`, `NoActiveAgentError`.
- `src/phaze/tasks/_shared/deterministic_key.py` — `scan_live_set:<file_id>` key.

### Tests to keep green / extend
- `tests/test_dag_canvas_render.py`, `tests/test_pipeline_dag_context.py`, `tests/test_services/test_pipeline.py`, `tests/test_routers/test_pipeline.py`.
</canonical_refs>

<deferred>
## Deferred Ideas

- Scrape + Match bulk triggers → Phase 41.
- Recovery-only automation → Phase 42.
</deferred>

---

*Phase: 40-tracklist-fingerprint-scan-dag-node*
*Context gathered: 2026-06-14 via inline operator discussion*
