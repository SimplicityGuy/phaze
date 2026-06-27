---
phase: 49-duration-routing-backfill
plan: 03
subsystem: api
tags: [fastapi, htmx, saq, routing, scheduling-ledger, jinja2]

# Dependency graph
requires:
  - phase: 49-duration-routing-backfill
    plan: 01
    provides: "cloud_route_threshold_sec, FileState.AWAITING_CLOUD, count_backfill_candidates, get_backfill_candidates"
  - phase: 49-duration-routing-backfill
    plan: 02
    provides: "_route_discovered_by_duration per-file router (reused verbatim for backfill)"
  - phase: 45
    provides: "scheduling_ledger.insert_ledger_if_absent (ON CONFLICT DO NOTHING)"
provides:
  - "POST /pipeline/backfill-cloud — re-drives ANALYSIS_FAILED ∧ duration>=threshold through the shared duration router"
  - "'Backfill to cloud' global DAG-header button (not a per-stage node)"
  - "backfill_response.html count-confirmed response partial"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Backfill reuses _route_discovered_by_duration verbatim so the manual backfill and Run-Analysis paths cannot drift (compute/held fork + cross-path deterministic-key dedup)"
    - "Held-only ledger seeding: insert_ledger_if_absent is called ONLY for AWAITING_CLOUD-held files (no compute agent); the enqueued branch's row is owned by the before_enqueue hook (no double-write, RESEARCH Open-Q3)"
    - "Held files detected in-memory via file.state == AWAITING_CLOUD after the router mutates them (expire_on_commit=False preserves attribute values across the router's commit)"
    - "Double-click no-op is structural: the first backfill moves candidates out of ANALYSIS_FAILED, so the explicit filter selects nothing on a second click — never a whole-backlog sweep (D-10)"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/backfill_response.html
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/dag_canvas.html
    - tests/test_routers/test_pipeline.py
    - tests/test_dag_canvas_render.py

key-decisions:
  - "Held-branch ledger payload records agent_id='' (no compute agent assigned while held); the real agent is stamped at release time by enqueue_process_file's before_enqueue ON CONFLICT DO UPDATE (Plan 04 cron). The full ProcessFilePayload field set is stored so a forced recover_orphaned_work replay re-validates cleanly (extra=forbid) rather than dead-lettering (T-45-10)"
  - "backfill_response.html created under Task 1 (the endpoint renders it) rather than Task 2, because the endpoint cannot return a 200 without it; Task 2 added only the button + render tests"
  - "The 'Backfill to cloud' button is a pipeline-LEVEL header action mirroring Recover, NOT a per-stage DAG node — NODE_LAYOUT/EDGES stay 10 nodes / 10 edges; the gating-triggers target-set test pins it separately"

requirements-completed: [CLOUDROUTE-04]

# Metrics
duration: 30min
completed: 2026-06-25
---

# Phase 49 Plan 03: Backfill-to-Cloud Action Summary

**A `POST /pipeline/backfill-cloud` endpoint + global DAG-header button that selects EXACTLY the timed-out long files (`ANALYSIS_FAILED ∧ duration >= cloud_route_threshold_sec`), resets them to `DISCOVERED` (committed), and re-drives them through the SAME per-file duration router as Run-Analysis — compute if a compute agent is online, else held in `AWAITING_CLOUD` with an explicit scheduling-ledger row — with an explicit filter + deterministic-key dedup that make a double-click a no-op and never a whole-backlog over-enqueue.**

## Performance
- **Duration:** ~30 min
- **Completed:** 2026-06-25
- **Tasks:** 2 (Task 1 TDD)
- **Files:** 5 (1 created, 4 modified)

## Accomplishments
- `POST /pipeline/backfill-cloud` (`trigger_backfill_cloud`): `count_backfill_candidates` short-circuits to the empty fragment when zero; otherwise `get_backfill_candidates` -> reset each to `DISCOVERED` -> explicit `await session.commit()` (RESEARCH Pitfall 3) -> route via `_route_discovered_by_duration` (reused verbatim from Plan 02). Since every backfill candidate is long, the router only ever produces `cloud` or `awaiting` — never local/skipped.
- **Held-only ledger seeding (D-09):** for files the router HELD in `AWAITING_CLOUD` (never enqueued, so no `before_enqueue` hook fired) the endpoint calls `insert_ledger_if_absent(key=process_file:<id>, function="process_file", timeout=7200, retries=2)`; the enqueued (cloud) branch is NOT double-written (its row is owned by the hook). Held files are detected on the in-memory candidate records (`file.state == AWAITING_CLOUD`) after the router mutates them.
- **Over-enqueue class closed (D-10):** the explicit `ANALYSIS_FAILED ∧ duration>=threshold` filter (not a backlog sweep) + the `process_file:<id>` deterministic key. A second click selects nothing (the candidates already left `ANALYSIS_FAILED`), so it is a no-op; short and never-failed files are never touched.
- **`backfill_response.html`** — count-confirmed copy ("Backfilled N long files: M cloud, K awaiting cloud" / zero-candidate message), plain ints through autoescape.
- **"Backfill to cloud" button** in `dag_canvas.html` mirroring the Recover button: neutral slate header action, `hx-post="/pipeline/backfill-cloud"`, distinct aria-label, `hx-indicator` spinner, Alpine error flag — a pipeline-level header action, NOT a per-stage node (node count unchanged).

## Task Commits
1. **Task 1: POST /pipeline/backfill-cloud endpoint (TDD)** — `4db1d20` (test RED), `9958641` (feat GREEN)
2. **Task 2: "Backfill to cloud" header button + render tests (D-08)** — `4f0c9eb` (feat)

_No REFACTOR commit needed — the GREEN implementation was already minimal/clean._

## Files Created/Modified
- `src/phaze/routers/pipeline.py` — added `trigger_backfill_cloud` + `_held_backfill_ledger_payload`; imported `process_file_job_key`, `ProcessFilePayload`, `insert_ledger_if_absent`, `count_backfill_candidates`, `get_backfill_candidates`
- `src/phaze/templates/pipeline/partials/backfill_response.html` — new count-confirmed partial
- `src/phaze/templates/pipeline/partials/dag_canvas.html` — added the global "Backfill to cloud" header button
- `tests/test_routers/test_pipeline.py` — 6 backfill endpoint tests (+ `_persist_failed_with_duration` helper)
- `tests/test_dag_canvas_render.py` — pinned the new `/pipeline/backfill-cloud` header target in `test_gating_triggers_post_only_to_existing_endpoints`; added button + response-partial render tests

## Decisions Made
- **Held-branch ledger payload uses `agent_id=""`.** A held file has no compute agent assigned (that is why it is held); the real agent is stamped at release time by `enqueue_process_file`'s `before_enqueue` ON CONFLICT DO UPDATE (Plan 04 cron). Storing the complete `ProcessFilePayload` field set keeps the row a valid, replayable `process_file` payload so a forced `recover_orphaned_work` replay re-validates under `extra="forbid"` rather than dead-lettering (T-45-10).
- **`backfill_response.html` was created in Task 1** (the endpoint renders it and cannot return 200 without it); Task 2 added only the button + render tests. See frontmatter `key-decisions`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Pinned the new header target in the DAG render guard**
- **Found during:** Task 2
- **Issue:** `tests/test_dag_canvas_render.py::test_gating_triggers_post_only_to_existing_endpoints` enumerates the EXACT set of `hx-post` targets and asserts the total count; adding the `/pipeline/backfill-cloud` header button would fail it.
- **Fix:** Added a `backfill_targets` assertion mirroring the existing `recover_targets` pin (a pipeline-level header action excluded from the per-stage enqueue surface) and included it in the total-count assertion. Preserves the test's intent (no net-new PER-STAGE trigger).
- **Files modified:** `tests/test_dag_canvas_render.py`
- **Commit:** `4f0c9eb`

## Threat Model Compliance
- **T-49-05 (backfill double-click detonates the queue):** mitigated + tested — `test_backfill_double_click_enqueues_nothing_new` asserts a second click enqueues nothing new (the explicit `ANALYSIS_FAILED` filter + deterministic key, NOT a backlog sweep).
- **T-49-06 (injection in the candidate query):** mitigated — the candidate set is the server-side `count_backfill_candidates` / `get_backfill_candidates` ORM query with a bound int threshold; no operator free-text enters the endpoint.

## Issues Encountered
- DB-backed router tests require the ephemeral test stack (Postgres :5433, Redis :6380) with the matching `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` env vars. Standard local-test setup, not a code change.

## Verification
- `uv run pytest tests/test_routers/test_pipeline.py -x` — 72 passed (incl. 6 new backfill tests)
- `uv run pytest tests/test_routers/test_pipeline.py tests/test_dag_canvas_render.py` — 123 passed
- `uv run pytest tests/test_task_split.py tests/test_tasks/test_recovery.py tests/test_services/test_pipeline.py tests/test_services/test_scheduling_ledger.py` — 125 passed (import boundary + recovery + ledger intact)
- `uv run ruff check src/phaze tests` — All checks passed
- `uv run mypy src/phaze` — Success: no issues found in 137 source files
- **Manual-only (49-VALIDATION / RESEARCH A1):** before trusting the "144" figure, run `SELECT count(*) FROM files f JOIN metadata m ON m.file_id=f.id WHERE f.state='analysis_failed' AND m.duration >= 5400;` against the live DB and confirm it matches the button-label count. Not runnable in CI (no live data).

## Next Phase Readiness
- CLOUDROUTE-04 is observable: backfill enqueues exactly the long failed set through the shared router, held files get a ledger row, a double-click is a no-op, and unrelated files are untouched.
- The full phase 49 surface (Plans 01-04) is complete: primitives, per-file router + held card, backfill, and the held-file release cron. No blockers for the v5.0 rsync push pipeline (Plan 50).

## Self-Check: PASSED
- Created file exists: `src/phaze/templates/pipeline/partials/backfill_response.html`.
- All modified files present on disk.
- Commits present in git history: `4db1d20`, `9958641`, `4f0c9eb`.

---
*Phase: 49-duration-routing-backfill*
*Completed: 2026-06-25*
