---
phase: 49-duration-routing-backfill
plan: 02
subsystem: api
tags: [fastapi, htmx, saq, routing, jinja2]

# Dependency graph
requires:
  - phase: 49-duration-routing-backfill
    plan: 01
    provides: "cloud_route_threshold_sec, FileState.AWAITING_CLOUD, kind-filtered select_active_agent, get_discovered_files_with_duration, get_awaiting_cloud_count, seed_active_agent(kind=...)"
provides:
  - "_route_discovered_by_duration(app_state, session, files_with_duration, threshold_sec, models_path) -> dict — the reusable per-file duration router (Plan 03 backfill composes against it)"
  - "trigger_analysis / trigger_analysis_ui forked to per-file duration routing with split-count reporting"
  - "awaiting_cloud_card.html — the 'Awaiting cloud' held-file count card (inline + 5s OOB)"
affects: [49-03 backfill]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Resolve BOTH kind-scoped agents (fileserver/compute) INDEPENDENTLY in separate try/except NoActiveAgentError blocks ONCE before the per-file loop (never resolve_queue_for_task per file)"
    - "Per-file duration fork reuses enqueue_process_file verbatim for local AND cloud so the identical deterministic key drives cross-path dedup (D-10)"
    - "Explicit await session.commit() for the AWAITING_CLOUD held-state UPDATE before backgrounding enqueues (get_session does not auto-commit)"
    - "Held-not-local safety: >=threshold + no compute -> AWAITING_CLOUD, NEVER the fileserver queue (T-49-03)"
    - "no-active-agent surfaced ONLY when BOTH agent kinds are absent (a missing fileserver alone no longer aborts the run)"

key-files:
  created:
    - src/phaze/templates/pipeline/partials/awaiting_cloud_card.html
  modified:
    - src/phaze/routers/pipeline.py
    - src/phaze/templates/pipeline/partials/trigger_response.html
    - src/phaze/templates/pipeline/dashboard.html
    - src/phaze/templates/pipeline/partials/stats_bar.html
    - tests/test_routers/test_pipeline.py

key-decisions:
  - "_route_discovered_by_duration returns an extra no_active_agent 0/1 flag (alongside local/cloud/awaiting/skipped) because counts alone cannot distinguish both-kinds-absent from compute-online-fileserver-offline-all-short (both yield local=cloud=0); the caller surfaces the no-active-agent response ONLY on that flag"
  - "trigger_response.html gains a split_counts branch (ordered after no_active_agent, before the legacy count>0 branch) so the shared partial keeps serving metadata/fingerprint/scan/search endpoints unchanged"
  - "awaiting_cloud_card mirrors straggler_failed_card's exact OOB contract (same id on both renders + {% if oob %}hx-swap-oob{% endif %}) and uses the degrade-safe service-owns-degrade wiring (no router try/except)"

requirements-completed: [CLOUDROUTE-01, CLOUDROUTE-02, CLOUDROUTE-03]

# Metrics
duration: 35min
completed: 2026-06-25
---

# Phase 49 Plan 02: Per-File Duration Router + Awaiting-Cloud Card Summary

**Replaced the all-files-to-one-queue analyze enqueue with a per-file duration router: long files route to a compute agent, short/null files stay local unchanged, and >=threshold files with no compute agent online are held in AWAITING_CLOUD (never silently analyzed locally) — with split counts reported and the held count surfaced as a live "Awaiting cloud" card.**

## Performance

- **Duration:** ~35 min
- **Tasks:** 2 (Task 1 TDD)
- **Files:** 6 (1 created, 5 modified)

## Accomplishments
- `_route_discovered_by_duration` — the module-level per-file router that Plan 03 (backfill) will reuse. Resolves the fileserver and compute agents in SEPARATE `try/except NoActiveAgentError` blocks ONCE before the loop, obtains each queue via `task_router.queue_for(agent.id)` (never the default queue, Phase-30 invariant), routes each `(file, duration)` tuple per D-06/D-11, commits the `AWAITING_CLOUD` held-state with an explicit `await session.commit()` before backgrounding the enqueues, and reuses `enqueue_process_file` verbatim for both local and cloud (cross-path dedup, D-10).
- `trigger_analysis` (JSON `/api/v1/analyze`) and `trigger_analysis_ui` (HTMX `/pipeline/analyze`) forked to call `get_discovered_files_with_duration` -> `_route_discovered_by_duration`, returning the split counts (D-12). The no-active-agent response is surfaced ONLY when BOTH agent kinds are absent, so the degenerate "compute online, fileserver offline" topology still routes long files to compute and reports short/null files as skipped without aborting the run.
- `trigger_response.html` gained a `split_counts` branch rendering "N local, M cloud, K awaiting cloud … S skipped (no local agent)" while preserving the both-kinds-absent and zero-files branches that the shared partial still serves for metadata/fingerprint/scan/search.
- `awaiting_cloud_card.html` (D-05) — mirrors `straggler_failed_card.html`'s OOB contract; wired into both `dashboard()` (inline first-load) and `pipeline_stats_partial()` (5s OOB push) via the degrade-safe `get_awaiting_cloud_count`.

## Task Commits

1. **Task 1: Per-file duration router fork + split-count response (TDD)** — `5770713` (test RED), `8362ab7` (feat GREEN)
2. **Task 2: Awaiting-cloud count card (D-05)** — `e82ba1e` (feat)

_No REFACTOR commit needed — the GREEN implementation was already minimal/clean._

## Files Created/Modified
- `src/phaze/routers/pipeline.py` — added `_route_discovered_by_duration`; forked `trigger_analysis` / `trigger_analysis_ui`; wired `awaiting_cloud_count` into `dashboard()` + `pipeline_stats_partial()`; swapped the `get_files_by_state` import for `get_discovered_files_with_duration` + `get_awaiting_cloud_count`
- `src/phaze/templates/pipeline/partials/trigger_response.html` — added the `split_counts` branch
- `src/phaze/templates/pipeline/partials/awaiting_cloud_card.html` — new card (D-05)
- `src/phaze/templates/pipeline/dashboard.html` — inline include of the card
- `src/phaze/templates/pipeline/partials/stats_bar.html` — OOB include of the card on the 5s poll
- `tests/test_routers/test_pipeline.py` — 8 router tests for the duration fork + 2 card tests

## Decisions Made
- **`no_active_agent` flag on the helper return.** Counts alone cannot distinguish "both kinds absent" from "compute online, fileserver offline, all-short" (both produce `local=cloud=0`), so `_route_discovered_by_duration` returns a `no_active_agent` 0/1 flag the caller checks to decide whether to surface the no-active-agent response. Keeps the return a `dict[str, int]`.
- See frontmatter `key-decisions` for the `split_counts` branch and card-mirroring rationale.

## Deviations from Plan

None — plan executed as written. (The `<interfaces>` contract documented the helper as returning `{local, cloud, awaiting, skipped}`; the implementation additionally returns a `no_active_agent` 0/1 flag, which is a strict superset required to drive the both-kinds-absent fragment decision the plan's `<action>` text specifies. Treated as a faithful expansion, not a deviation.)

## Threat Model Compliance
- **T-49-03 (held files silently analyzed locally and time out):** mitigated + tested — `test_analyze_long_file_no_compute_holds_awaiting_cloud` asserts a >=threshold file with no compute agent ends `state==AWAITING_CLOUD` with NO `process_file` capture.
- **T-49-04 (enqueue onto the consumer-less default queue):** mitigated — both branches obtain their queue only via `task_router.queue_for(agent.id)`; `test_analyze_long_file_routes_to_compute_queue` asserts the compute capture lands on `phaze-agent-cloud`, never `default`.

## Issues Encountered
- DB-backed router tests require the ephemeral test stack (Postgres :5433, Redis :6380) with the matching `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` env vars. Standard local-test setup, not a code change.

## Verification
- `uv run pytest tests/test_routers/test_pipeline.py -x` — 78 passed
- `uv run ruff check src/phaze tests` — All checks passed
- `uv run mypy src/phaze` — Success: no issues found in 136 source files

## Next Phase Readiness
- `_route_discovered_by_duration` is the reusable routing path Plan 03 (backfill) composes against — long ANALYSIS_FAILED files re-route through the SAME helper.
- No blockers.

## Self-Check: PASSED

All 6 plan files exist on disk and all 3 commits (`5770713`, `8362ab7`, `e82ba1e`) are present in git history.

---
*Phase: 49-duration-routing-backfill*
*Completed: 2026-06-25*
