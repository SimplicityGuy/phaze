---
phase: 40-tracklist-fingerprint-scan-dag-node
plan: 01
subsystem: ui
tags: [fastapi, htmx, alpinejs, saq, postgres, dag, per-agent-routing, scan_live_set]

# Dependency graph
requires:
  - phase: 39-tracklist-search-dag-node
    provides: scan_search trigger node, get_search_busy_count degrade-safe pattern, eligible-set (no-tracklist) query, enqueue_button macro + dag.items() OOB seed loop
  - phase: 30-default-queue-misrouting
    provides: enqueue_router.resolve_queue_for_task per-agent routing + NoActiveAgentError empty-state
provides:
  - POST /pipeline/scan-live-sets bulk per-agent fingerprint-scan trigger (complete ScanLiveSetPayload, no-agent empty-state)
  - get_scan_busy_count + count_active_agents degrade-safe SAVEPOINT service reads
  - scanBusy + agentOnline DAG store keys seeded through _build_dag_context + the 5s OOB poll
  - 10th DAG node (fingerprint_scan, "Identify Set", indigo) gated on discovered/agentOnline/scanBusy
affects: [41-scrape-match-triggers, 42-recovery-automation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-agent bulk trigger mirroring _enqueue_fingerprint_jobs (complete payload, never file_id-only) instead of the buggy single-file trigger_scan"
    - "Online-agent count (count_active_agents) reusing select_active_agent's exact liveness rule, degrade-to-0 fail-safe"

key-files:
  created: []
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/dag_canvas.html
    - src/phaze/templates/base.html
    - docs/api.md
    - tests/test_services/test_pipeline.py
    - tests/test_routers/test_pipeline.py
    - tests/test_pipeline_dag_context.py
    - tests/test_dag_canvas_render.py

key-decisions:
  - "Mirror the correct full-payload producer (_enqueue_fingerprint_jobs) — NOT the file_id-only trigger_scan — so no scan_live_set job dead-letters on extra=forbid (v4.0.8 class)"
  - "count_active_agents reuses select_active_agent's exact WHERE clause (revoked_at IS NULL AND last_seen_at IS NOT NULL) — no new liveness rule (CONTEXT decision 2)"
  - "No fingerprint_scan->scrape edge: scan_search is the canonical tracklist head carrying tracklist->scrape->match; tracklistDone is the union of both producers (edge honesty)"
  - "base.html store seed (scanBusy/agentOnline) landed in Task 1 (not Task 2) so the shared _NEW_STORE_KEYS store-literal test stays green within Task 1's verify gate"

patterns-established:
  - "Degrade-safe per-prefix saq_jobs busy count for a per-agent task (get_scan_busy_count) reusing the static _STAGE_BUSY_SQL grouped scan inside session.begin_nested()"
  - "Fail-safe online-agent gate: agentOnline degrades to 0, leaving the node blocked 'Needs agent' on any liveness-read outage"

requirements-completed: [REQ-40-1, REQ-40-2, REQ-40-3, REQ-40-4]

# Metrics
duration: ~55min
completed: 2026-06-14
---

# Phase 40 Plan 01: Tracklist Fingerprint-Scan DAG Node Summary

**Added the sibling tracklist head — a per-agent "Identify Set" DAG node whose bulk `POST /pipeline/scan-live-sets` enqueues `scan_live_set` with the complete `ScanLiveSetPayload` over eligible files, routed to the per-agent queue (never default/controller), gated on discovered files + an online agent + in-flight scan count, with a visible no-agent empty-state.**

## Performance

- **Duration:** ~55 min
- **Started:** 2026-06-14T18:35Z (approx)
- **Completed:** 2026-06-14T19:30Z (approx)
- **Tasks:** 3
- **Files modified:** 9

## Accomplishments
- `POST /pipeline/scan-live-sets` routes `scan_live_set` to the per-agent queue (`phaze-agent-<id>`) with the COMPLETE `ScanLiveSetPayload` (file_id + original_path + agent_id); a no-active-agent POST renders the empty-state at status 200, never 500.
- `get_scan_busy_count` + `count_active_agents` added as degrade-safe SAVEPOINT reads; both ride the 5s poll via `_build_dag_context` (`scanBusy` / `agentOnline` ints) and fail closed.
- 10th DAG node `fingerprint_scan` (indigo, "Identify Set") inserted with the exact recomputed `NODE_LAYOUT` (discovery y=611, fingerprint_scan y=1128, canvas 1060→1340, one new `discovery→fingerprint_scan` edge = 10 edges), gated `No files discovered` / `Needs agent` / `Scan busy`, plus the 10-row `<ol>` text-equivalent.
- Phase-39 Search node, the existing `fingerprint` (dedup) node, and all prior guard tests remain intact.

## Task Commits

Each task was committed atomically (TDD: failing tests → implementation in one task commit each):

1. **Task 1: Bulk scan endpoint (per-agent, full payload) + scanBusy/agentOnline service + context seed** - `feaa03a` (feat)
2. **Task 2: 10th DAG node (Identify Set) + layout recompute + discovered/agentOnline/scanBusy gate** - `782ca4e` (feat)
3. **Task 3: End-to-end dashboard assertion + docs/api.md** - `6007309` (test/docs)

_Plan-docs metadata commit (SUMMARY/STATE/ROADMAP) is handled by the orchestrator._

## Files Created/Modified
- `src/phaze/services/pipeline.py` - Added `get_scan_busy_count` (scan_live_set in-flight prefix count) + `count_active_agents` (online-agent liveness count), both degrade-safe in a SAVEPOINT.
- `src/phaze/routers/pipeline.py` - Added `_enqueue_scan_jobs` (complete payload producer), `POST /pipeline/scan-live-sets` (`trigger_scan_live_sets_ui`), and seeded `scanBusy`/`agentOnline` in `_build_dag_context`.
- `src/phaze/templates/pipeline/partials/dag_canvas.html` - New `fingerprint_scan` node (chip, gate, edge, layout recompute, `<ol>` row, header comments).
- `src/phaze/templates/base.html` - Seeded `scanBusy: 0` and `agentOnline: 0` in the Alpine `$store.pipeline` literal.
- `docs/api.md` - Endpoint row + "Bulk fingerprint scan (Phase 40)" paragraph + `scanBusy`/`agentOnline` poll keys.
- `tests/test_services/test_pipeline.py` - Co-located tests for both new service reads (bucket / zero / db-error degrade / non-poisoning).
- `tests/test_routers/test_pipeline.py` - Per-agent routing + complete-payload capture, tracklist exclusion, no-agent empty-state, zero-eligible, end-to-end dashboard render.
- `tests/test_pipeline_dag_context.py` - Added `scanBusy`/`agentOnline` to `_NEW_STORE_KEYS`.
- `tests/test_dag_canvas_render.py` - 10-node/10-edge/10×240px/5-node col-1 guards, 6 enqueue targets, gate copy, response slot, `<ol>` count, predicate assertions, em-dash slice advisory.

## Decisions Made
- Mirrored the correct full-payload producer to avoid the v4.0.8 dead-letter class; explicitly did NOT copy `tracklists.trigger_scan`.
- `count_active_agents` reuses `select_active_agent`'s exact liveness predicate (no new rule).
- Kept edges honest (no second producer→scrape edge); `tracklistDone` is the union both producers feed.
- Applied the plan-checker advisory: `test_integration_dashboard_scan_search_em_dash` now slices `node-scan_search` → `node-fingerprint_scan` so it tests only the scan_search chip after the new node's DOM insertion.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking issue] base.html store seed moved into Task 1**
- **Found during:** Task 1
- **Issue:** Task 1's verify gate includes `tests/test_pipeline_dag_context.py`, whose shared `_NEW_STORE_KEYS` drives `test_store_seeds_every_new_per_node_key_to_zero` (reads `base.html`). The plan assigned the `base.html` store seed to Task 2, which would have left Task 1's verify red.
- **Fix:** Added `scanBusy: 0, agentOnline: 0` to the `base.html` Alpine store literal in Task 1 (the same single edit the plan specifies for Task 2). Task 2 therefore did not need to touch `base.html` again.
- **Files modified:** `src/phaze/templates/base.html`
- **Commit:** `feaa03a`

## Known Stubs
None — the node is fully wired (real endpoint, real per-agent routing, real DB-backed busy/agent-online reads).

## Self-Check: PASSED
