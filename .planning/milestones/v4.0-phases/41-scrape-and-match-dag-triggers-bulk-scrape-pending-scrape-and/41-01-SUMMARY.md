---
phase: 41-scrape-and-match-dag-triggers
plan: 01
subsystem: pipeline-ui
tags: [fastapi, htmx, alpinejs, saq, sqlalchemy, controller-queue, dag-trigger]

# Dependency graph
requires:
  - phase: 39-tracklist-name-search-dag-trigger
    provides: bulk controller-routed trigger pattern (search-tracklists), get_search_busy_count degrade-safe SAVEPOINT scan, enqueue_button gating, *Busy seeding through _build_dag_context + the 5s OOB poll
  - phase: 40-tracklist-fingerprint-scan-dag-node
    provides: sibling tracklist-head trigger pattern, base.html store-seed-in-Task-1 ordering, count_active_agents degrade pattern
  - phase: 30-default-queue-misrouting-fix
    provides: enqueue_router.resolve_queue_for_task controller routing + the {("controller", task)} capture-assertion discipline
provides:
  - POST /pipeline/scrape-tracklists + POST /pipeline/match-tracklists controller-routed bulk HTMX triggers
  - get_scrape_busy_count / get_match_busy_count degrade-safe in-flight gates
  - get_scrape_pending_tracklists / get_match_pending_tracklists eligible-set complements of get_stage_progress
  - scrapeBusy + matchBusy DAG store keys riding dag.items() + the 5s OOB poll
  - scrape + match DAG nodes promoted from display-only chips to bulk trigger buttons with existence/pending/busy gates
  - trigger_tracklist_response.html tracklist-unit HTMX response partial
affects: [42-recovery-pass, pipeline-dashboard, dag-canvas]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Bulk-over-pending controller trigger: eligible set = the exact complement of the get_stage_progress done-count, background-enqueued one job per pending row, deterministic-key dedup, NO explicit key="
    - "Existence/pending/busy three-tier gate: blocked = total===0 || busy>0 || (total-done)<=0, with busy ordered BEFORE the nothing-pending check so a running batch stays visible"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/trigger_tracklist_response.html
  modified:
    - src/phaze/services/pipeline.py
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/dag_canvas.html
    - src/phaze/templates/base.html
    - docs/api.md

key-decisions:
  - "Existence gate uses scrapeTotal/matchTotal (COUNT(tracklists)), NOT tracklistDone — a null-file_id tracklist still needs scrape/match and pending = total - done must use the same total (CONTEXT decision 3)"
  - "Eligible-set queries placed in services/pipeline.py beside get_stage_progress so the complement SQL is co-located and independently unit-testable"
  - "No layout recompute: scrape/match are the last nodes in their columns; the ~46px button growth clears everything below and stays under the 1340px canvas — NODE_LAYOUT / EDGES / canvas height UNCHANGED (10 nodes / 10 edges)"
  - "Tracklist-unit response uses a NEW partial (trigger_tracklist_response.html) instead of editing the shared file-unit trigger_response.html, so the 6 existing endpoints + their copy-asserting tests stay untouched"

patterns-established:
  - "Controller-task bulk trigger has NO no_active_agent branch (never raises NoActiveAgentError) — distinct from the per-agent scan trigger"
  - "Busy reads reuse the static _STAGE_BUSY_SQL grouped scan inside session.begin_nested(); no operator input interpolated (T-41-01)"

requirements-completed: [REQ-41-1, REQ-41-2, REQ-41-3, REQ-41-4]

# Metrics
duration: ~55min
completed: 2026-06-14
---

# Phase 41 Plan 01: Scrape and Match DAG Triggers Summary

**The DAG's Scrape and Match nodes became operator-triggerable bulk-over-pending controllers: Scrape enqueues `scrape_and_store_tracklist` for every versionless tracklist and Match enqueues `match_tracklist_to_discogs` for every un-linked tracklist, both routed to the controller queue with deterministic-key dedup, existence/pending/busy gates, and degrade-safe busy reads — no layout change, full suite green at 97.60% coverage.**

## Performance

- **Duration:** ~55 min
- **Started:** 2026-06-14T19:30Z (approx)
- **Completed:** 2026-06-14T20:25Z (approx)
- **Tasks:** 3 of 3
- **Files modified:** 5 (+1 created)

## Accomplishments
- Two controller-routed bulk HTMX endpoints (`/pipeline/scrape-tracklists`, `/pipeline/match-tracklists`) with capture-asserted `{("controller", task)}` routing (never default), tracklist_id dedup, and a 200 empty-state when nothing is pending.
- Four new degrade-safe service reads: `get_scrape_busy_count` / `get_match_busy_count` (SAVEPOINT in-flight gates) and `get_scrape_pending_tracklists` / `get_match_pending_tracklists` (exact complements of `get_stage_progress`'s scrape.done / match.done).
- Scrape + Match DAG nodes promoted to rose trigger buttons with the LOCKED existence/pending/busy gate copy (Needs tracklist / Scraping…|Matching… / All scraped|All matched), with NO node/edge/height/canvas change (10 nodes / 10 edges preserved).

## Task Commits

Each task was committed atomically (TDD: test → feat):

1. **Task 1: endpoints + busy/pending reads + dag/store seeds + partial** — `a5e220f` (test) → `35143ad` (feat)
2. **Task 2: scrape + match enqueue buttons + existence/pending/busy gates** — `696d634` (test) → `5c6bd31` (feat)
3. **Task 3: full-suite regression + coverage gate + docs** — `6ecea88` (docs)

_Plan metadata (SUMMARY/STATE/ROADMAP) committed separately by the orchestrator._

## Files Created/Modified
- `src/phaze/services/pipeline.py` - Added `get_scrape_busy_count`, `get_match_busy_count` (with `_SCRAPE_BUSY_FUNCTION` / `_MATCH_BUSY_FUNCTION` constants reusing `_STAGE_BUSY_SQL`) and the eligible-set helpers `get_scrape_pending_tracklists` (`~exists(version)`) / `get_match_pending_tracklists` (`.not_in(discogs-walk subquery)`).
- `src/phaze/routers/pipeline.py` - Added `_enqueue_scrape_jobs` / `_enqueue_match_jobs`, the two `POST /pipeline/{scrape,match}-tracklists` endpoints (controller-routed, background-enqueued, no key=), and the `scrapeBusy` / `matchBusy` seeds in `_build_dag_context`.
- `src/phaze/templates/pipeline/partials/dag_canvas.html` - Replaced the scrape/match display-only sublabels with `enqueue_button` macros, rewrote both gate predicates (existence on scrapeTotal/matchTotal, busy before nothing-pending), and documented the no-recompute verification. No NODE_LAYOUT/EDGES/height change.
- `src/phaze/templates/pipeline/partials/trigger_tracklist_response.html` - New tracklist-unit HTMX response fragment (no no_active_agent branch).
- `src/phaze/templates/base.html` - Added `scrapeBusy: 0, matchBusy: 0` to the `$store.pipeline` literal.
- `docs/api.md` - Added the two endpoint rows, a "Bulk scrape + match (Phase 41)" paragraph, and the `scrapeBusy` / `matchBusy` DAG store keys.

## Deviations from Plan

None - plan executed exactly as written. One trivial in-flight correction: an ASCII-vs-Unicode minus sign (`−`) in a router comment tripped ruff `RUF003` and was replaced with `-` before the Task 1 GREEN commit (not a behavioral change).

## Tests
- `tests/test_services/test_pipeline.py` - busy-count bucket/zero/degrade/non-poison tests for both new reads + DB-backed eligible-set tests (versioned excluded; discogs-reachable excluded).
- `tests/test_routers/test_pipeline.py` - controller-routing capture, done-row exclusion, and zero-pending empty-state for both endpoints + the end-to-end dashboard render (both `hx-post` targets + "Needs tracklist").
- `tests/test_pipeline_dag_context.py` / `tests/test_dag_canvas_render.py` - `scrapeBusy`/`matchBusy` added to the store-key contract; 8-endpoint trigger surface; LOCKED gate copy; response slots; busy-predicate guards. 10-node/10-edge/10×240px/`<ol>`-10-row guards remain green.

## Verification
- `uv run pytest --cov` → 1792 passed, **97.60% coverage** (≥85% gate).
- `uv run ruff check .` + `uv run ruff format --check` → clean.
- `uv run mypy .` → no issues in 155 source files (services/pipeline.py 98.79%, only pre-existing get_stage_controls lines uncovered).
- `pre-commit run --all-files` → all hooks pass (no `--no-verify`).

## Known Stubs
None.

## Threat Flags
None — both new endpoints are parameterless controller triggers with server-computed pending sets; all SQL is static `_STAGE_BUSY_SQL` / pure-ORM `~exists` / `.not_in(subquery)` with no operator input (T-41-01), routed to the controller queue (T-41-04), busy reads degrade inside SAVEPOINTs (T-41-03). No new trust boundary beyond those enumerated in the plan's threat register.

## Self-Check: PASSED
- All 6 key files present on disk.
- All 5 task commits present in `git log` (a5e220f, 35143ad, 696d634, 5c6bd31, 6ecea88).
