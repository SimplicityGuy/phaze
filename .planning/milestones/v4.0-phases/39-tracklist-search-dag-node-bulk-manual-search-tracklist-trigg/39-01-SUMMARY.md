---
phase: 39-tracklist-search-dag-node
plan: 01
subsystem: ui
tags: [htmx, alpinejs, jinja2, saq, enqueue-router, pipeline-dag, tracklist-search]

# Dependency graph
requires:
  - phase: 30-default-queue-misrouting
    provides: enqueue_router.resolve_queue_for_task controller-queue routing rule
  - phase: 35-pipeline-dag-canvas
    provides: dag_canvas.html NODE_LAYOUT/EDGES contract, _build_dag_context store-key seed loop
  - phase: 38-stage-pause-priority
    provides: per-stage busy gating pattern (get_stage_busy_counts + "busy" disable)
provides:
  - POST /pipeline/search-tracklists bulk name-search trigger (controller-routed)
  - get_search_busy_count degrade-safe in-flight count for search_tracklist jobs
  - searchBusy store key on the 5s OOB poll
  - triggerable Search node on the DAG canvas gated on metadataDone/searchBusy
affects: [phase-40-fingerprint-scan-node, phase-41-scrape-match-triggers, phase-42-recovery-automation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Controller-task bulk trigger: parameterless POST builds server-side eligible set, background-enqueues via enqueue_router to the controller queue"
    - "Single-prefix degrade-safe busy count reusing the shared _STAGE_BUSY_SQL grouped scan inside a SAVEPOINT"

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
  - "Kept the scan_search NODE_LAYOUT/EDGES/stats key (no rename to `search`) — Phase 40 decides any split; renaming now would churn NODE_LAYOUT, EDGES, _NODE_COMPLETED_FNS and ~6 tests for zero functional gain"
  - "Eligible set = music/video files with NO linked Tracklist (~exists), so re-runs are cheap/idempotent; deterministic key search_tracklist:<file_id> dedups in-flight replays"
  - "get_search_busy_count reuses the existing static _STAGE_BUSY_SQL grouped scan rather than adding a second text() literal (T-39-01 static-SQL discipline)"
  - "base.html searchBusy:0 seed landed in Task 1 (not Task 2) so the shared _NEW_STORE_KEYS-driven store-literal test stays green per commit"

patterns-established:
  - "Controller bulk trigger mirrors manual_search routing but background-enqueues like trigger_extraction_ui to avoid HTTP timeout on large eligible counts"
  - "New per-node store key rides the existing dag.items() seed + OOB poll with no stats_bar.html edit"

requirements-completed: [REQ-39-1, REQ-39-2, REQ-39-3, REQ-39-4]

# Metrics
duration: ~35min
completed: 2026-06-14
---

# Phase 39 Plan 01: Tracklist Search DAG Node Summary

**The DAG's display-only Scan / Search head becomes a manual bulk trigger: a controller-routed `POST /pipeline/search-tracklists` enqueues `search_tracklist` over files without a tracklist, gated disabled until Metadata has produced tags and "busy" while a search batch is in flight.**

## Performance

- **Duration:** ~35 min
- **Started:** 2026-06-14T17:25Z
- **Completed:** 2026-06-14T18:05Z
- **Tasks:** 3 completed
- **Files modified:** 9

## Accomplishments
- Added `get_search_busy_count` — a degrade-safe (SAVEPOINT, never-500) in-flight count for `search_tracklist` jobs, reusing the shared static grouped-key scan (T-39-01/T-39-03).
- Added `POST /pipeline/search-tracklists`: a parameterless bulk trigger that computes the eligible set server-side (music/video files with no linked tracklist) and background-enqueues one `search_tracklist` job per file onto the **controller** queue via `enqueue_router` (never the consumer-less default queue — Phase-30 rule, proven by a capture assertion).
- Turned the `scan_search` canvas node into a working trigger button gated on `metadataDone`/`searchBusy` with the LOCKED reason copy "Needs metadata" / "Search busy", seeded `searchBusy` through the existing `dag.items()` + 5s OOB poll, and grew the canvas height 1000→1060 so the button does not clip.

## Task Commits

Each task was committed atomically:

1. **Task 1: Bulk search endpoint + searchBusy service + context seed** - `54b892e` (feat)
2. **Task 2: Search node trigger button + metadataDone/searchBusy gate on the canvas** - `1f6ed2b` (feat)
3. **Task 3: Full-suite regression, coverage gate, and docs** - `688d7c7` (test)

_Plan metadata (SUMMARY/STATE/ROADMAP) is committed by the orchestrator._

## Files Created/Modified
- `src/phaze/services/pipeline.py` - Added `get_search_busy_count(session) -> int` (degrade-safe single-prefix in-flight count for `search_tracklist`, reusing `_STAGE_BUSY_SQL`).
- `src/phaze/routers/pipeline.py` - Added `_enqueue_search_jobs` background coroutine + `POST /pipeline/search-tracklists` (`trigger_search_ui`), seeded `dag["searchBusy"]` in `_build_dag_context`, imported `Tracklist`, `MUSIC_VIDEO_TYPES`, `get_search_busy_count`.
- `src/phaze/templates/pipeline/partials/dag_canvas.html` - `scan_search` gate moved to `metadataDone === 0 || searchBusy > 0`; added the rose enqueue trigger button (slot `search-tracklists-response`); canvas/SVG height 1000→1060; inline NODE_LAYOUT/scan_search comments updated.
- `src/phaze/templates/base.html` - Seeded `searchBusy: 0` in the Alpine `pipeline` store literal.
- `docs/api.md` - Documented the `/pipeline/search-tracklists` endpoint, its controller routing + gate, and the `searchBusy` DAG key.
- `tests/test_services/test_pipeline.py` - 4 `get_search_busy_count` tests (bucket-by-prefix, zero-when-no-rows, exploding-session degrade, real-session SAVEPOINT degrade leaves session usable).
- `tests/test_routers/test_pipeline.py` - controller-routing capture, tracklist-exclusion, zero-eligible, and end-to-end dashboard Search-trigger assertions.
- `tests/test_pipeline_dag_context.py` - added `searchBusy` to `_NEW_STORE_KEYS`.
- `tests/test_dag_canvas_render.py` - added `searchBusy` to `_DAG_KEYS`; 5-element enqueue-target list; "Needs metadata"/"Search busy" reason copy; `search-tracklists-response` slot.

## Deviations from Plan

### Adjustments (within auto-fix scope)

**1. [Rule 3 - Blocking] base.html `searchBusy: 0` seed landed in Task 1 instead of Task 2**
- **Found during:** Task 1
- **Issue:** `_NEW_STORE_KEYS` in `tests/test_pipeline_dag_context.py` is shared by the store-literal seed test (which reads `base.html`) and is part of Task 1's verify command (`pytest ... tests/test_pipeline_dag_context.py`). Leaving the base.html seed to Task 2 would have left Task 1's own verify (and the suite between commits) RED.
- **Fix:** Added `searchBusy: 0` to the `base.html` Alpine store in the Task 1 commit so every per-task commit is green. Task 2 still owns the canvas gate/button/render-test work the plan assigned to it.
- **Files modified:** `src/phaze/templates/base.html`
- **Commit:** `54b892e`

**2. [Rule 2 - Docs/correctness] api.md updated for the new trigger surface**
- **Found during:** Task 3
- **Issue:** The plan's Task 3 instructs updating any doc that references the DAG trigger surface (project "docs up to date" rule). `docs/api.md` enumerated the 4 pipeline triggers and the DAG OOB seed keys but not the new Search trigger / `searchBusy`.
- **Fix:** Added a `/pipeline/search-tracklists` row, a "Bulk tracklist search (Phase 39)" paragraph, and `searchBusy` to the DAG key list.
- **Files modified:** `docs/api.md`
- **Commit:** `688d7c7`

No bugs (Rule 1) or architectural changes (Rule 4) were required. No authentication gates occurred. No packages were installed.

## Verification

- `uv run pytest --cov=phaze --cov-report=term-missing` → **1763 passed**, total coverage **97.56%** (≥85%).
- `uv run ruff format .` → 336 files unchanged; `uv run ruff check .` → All checks passed.
- `uv run mypy .` → Success: no issues found in 155 source files.
- `pre-commit run --all-files` → all hooks Passed (no `--no-verify`).
- Capture assertion proves `search_tracklist` lands on `{("controller","search_tracklist")}` (never default).
- `GET /pipeline/` renders `hx-post="/pipeline/search-tracklists"` and the "Needs metadata" gate copy.
- Canvas guard tests green: 5 enqueue targets, 9 anchor-derived edges, 240px chip width, no column overlap, em-dash denominator preserved.

## Threat Model Compliance

- **T-39-01 (Tampering/Injection):** mitigated — `get_search_busy_count` reuses the fixed `_STAGE_BUSY_SQL` (no operator input); the eligible-set query uses ORM `.in_(MUSIC_VIDEO_TYPES)` + `~exists(...)` bound params.
- **T-39-02 (DoS, bulk enqueue):** accepted — background enqueue avoids HTTP timeout; deterministic key dedups in-flight re-runs; routed to a real consumer (controller queue).
- **T-39-03 (DoS, searchBusy poll):** mitigated — SAVEPOINT degrade to 0, never raises, no ORM-expiring rollback; the 5s poll keeps serving 200.
- **T-39-04 (EoP, routing):** mitigated — capture assertion pins `search_tracklist` to the controller queue.
- **T-39-SC:** no new packages installed.

No new threat surface beyond the plan's `<threat_model>` was introduced.

## Known Stubs

None. The Search node is fully wired end-to-end (endpoint → controller queue → DB-computed eligible set → gated button).

## Self-Check: PASSED

- Commits exist: `54b892e`, `1f6ed2b`, `688d7c7` (verified via `git log --oneline`).
- `get_search_busy_count` present in `src/phaze/services/pipeline.py`; `/pipeline/search-tracklists` present in `src/phaze/routers/pipeline.py` and `dag_canvas.html`; `searchBusy` present in `base.html`.
- Full suite green (1763 passed) at ≥85% coverage; mypy + ruff + pre-commit clean.

## TDD Gate Compliance

Tasks 1 and 2 were authored test-first (RED tests written and the failing state confirmed against the missing `get_search_busy_count` / `searchBusy` keys before implementation). For commit economy each task's RED tests and GREEN implementation were folded into a single `feat(39-01)` commit rather than separate `test`/`feat` commits; the RED→GREEN ordering was preserved during authoring. Task 3 is a non-TDD regression/docs task committed as `test(39-01)`.
