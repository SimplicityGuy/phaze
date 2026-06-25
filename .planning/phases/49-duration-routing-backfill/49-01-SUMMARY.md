---
phase: 49-duration-routing-backfill
plan: 01
subsystem: api
tags: [pydantic-settings, sqlalchemy, saq, routing, postgres]

# Dependency graph
requires:
  - phase: 48-compute-agent-type
    provides: "Agent.kind column (fileserver/compute) + DB CHECK constraint"
provides:
  - "ControlSettings.cloud_route_threshold_sec config knob (default 5400, alias PHAZE_CLOUD_ROUTE_THRESHOLD_SEC, bounded gt=0/lt=86400)"
  - "FileState.AWAITING_CLOUD code-only held-state member (value 'awaiting_cloud', no migration)"
  - "kind-filtered select_active_agent(session, kind=...) selector (D-13)"
  - "get_discovered_files_with_duration / get_awaiting_cloud_count / count_backfill_candidates / get_backfill_candidates pipeline helpers"
  - "kind-aware seed_active_agent test helper"
affects: [49-02 per-file router, 49-03 backfill, 49-04 release cron]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Bounded pydantic int Field mirroring straggler_threshold_sec for operator-tunable thresholds (fail-fast at startup)"
    - "Code-only FileState member over String(30) (no enum migration) following the ANALYSIS_FAILED precedent"
    - "In-memory duration capture via explicit SELECT + outerjoin because FileRecord.file_metadata is lazy=noload"
    - "Explicit ANALYSIS_FAILED + duration>=threshold INNER JOIN predicate (NOT a bare ANALYSIS_FAILED count) to close the over-enqueue class"

key-files:
  created:
    - tests/test_config/test_cloud_route_threshold.py
  modified:
    - src/phaze/config.py
    - src/phaze/models/file.py
    - src/phaze/services/enqueue_router.py
    - src/phaze/services/pipeline.py
    - tests/_queue_fakes.py
    - tests/test_services/test_enqueue_router.py
    - tests/test_services/test_pipeline.py

key-decisions:
  - "cloud_route_threshold_sec lives on ControlSettings (control plane owns routing) and is bounded gt=0/lt=86400 like straggler_threshold_sec (T-49-01)"
  - "AWAITING_CLOUD is code-only ('awaiting_cloud' is 14 chars, fits String(30)) — no Alembic migration"
  - "select_active_agent gains an optional kind param (minimal-change route, RESEARCH Pattern 1) rather than a sibling helper, keeping resolve_queue_for_task unchanged"
  - "Backfill candidates use an explicit duration JOIN + >= filter — deliberately NOT get_analysis_failed_count, which over-counts short/null-duration failures (RESEARCH Pitfall 5)"

patterns-established:
  - "Duration-routing read helpers JOIN metadata and capture duration in-memory before any background task (lazy=noload safety)"
  - "Degrade-safe counts via _safe_count for any dashboard-facing count helper"

requirements-completed: [CLOUDROUTE-01, CLOUDROUTE-02, CLOUDROUTE-03, CLOUDROUTE-04]

# Metrics
duration: 25min
completed: 2026-06-25
---

# Phase 49 Plan 01: Duration-Routing Primitives Summary

**Routing primitives for cloud-burst analysis: a bounded cloud_route_threshold_sec knob, the AWAITING_CLOUD held-state, a kind-filtered active-agent selector, and three duration-join service helpers (discovered, awaiting-cloud count, backfill candidates).**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-06-25T20:55:00Z
- **Completed:** 2026-06-25T21:05:00Z
- **Tasks:** 3 (all TDD)
- **Files modified:** 8 (1 created, 7 modified)

## Accomplishments
- `ControlSettings.cloud_route_threshold_sec` (default 5400s, env alias `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC`, bounded `gt=0`/`lt=86400`) — the single knob Plans 02/03/04 compare against (D-07)
- `FileState.AWAITING_CLOUD = "awaiting_cloud"` code-only member over the existing `String(30)` column — no migration (D-01)
- `select_active_agent(session, kind=...)` scopes selection to `Agent.kind` while preserving the any-kind default and all existing callers (D-13)
- Three `services/pipeline.py` read helpers: `get_discovered_files_with_duration` (outerjoin), `get_awaiting_cloud_count` (degrade-safe, D-05), and `count_backfill_candidates` / `get_backfill_candidates` (ANALYSIS_FAILED + duration>=threshold INNER JOIN, D-09/D-10)
- `seed_active_agent(..., kind=...)` test helper so downstream waves can seed a compute agent (RESEARCH A3 / Wave 0)

## Task Commits

Each task was committed atomically (TDD RED → GREEN):

1. **Task 1: config knob + AWAITING_CLOUD + seed kind param** — `ddcc57c` (test), `a7c0a9e` (feat)
2. **Task 2: kind filter on select_active_agent (D-13)** — `33ff904` (test), `c174262` (feat)
3. **Task 3: duration-join / awaiting-cloud / backfill helpers** — `6b28f4a` (test), `1f04466` (feat)

_No REFACTOR commits were needed — GREEN implementations were already minimal/clean._

## Files Created/Modified
- `src/phaze/config.py` — added `cloud_route_threshold_sec` Field to `ControlSettings`
- `src/phaze/models/file.py` — added `AWAITING_CLOUD` to the `FileState` StrEnum
- `src/phaze/services/enqueue_router.py` — added `kind` param to `select_active_agent`
- `src/phaze/services/pipeline.py` — added 4 duration-routing read helpers (+ a private statement builder)
- `tests/_queue_fakes.py` — `seed_active_agent` now accepts `kind` (default `fileserver`)
- `tests/test_config/test_cloud_route_threshold.py` — new: config default/alias/bounds + AWAITING_CLOUD member
- `tests/test_services/test_enqueue_router.py` — added 4 kind-scoping tests
- `tests/test_services/test_pipeline.py` — added 7 duration/awaiting/backfill tests

## Decisions Made
None beyond the plan — followed the plan's `<interfaces>` contracts exactly. See frontmatter `key-decisions` for the rationale carried from the plan.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- The DB-backed tests (`test_enqueue_router.py`, `test_pipeline.py`) require a live PostgreSQL. Started the project's ephemeral test stack via `just test-db` (Postgres on :5433, Redis on :6380) and ran pytest with the matching `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL` env vars. This is standard local-test setup, not a code change.

## Verification
- `uv run pytest tests/test_config/ tests/test_services/test_enqueue_router.py tests/test_services/test_pipeline.py` — 130 passed
- `uv run ruff check src/phaze tests` — All checks passed
- `uv run mypy src/phaze` — Success: no issues found in 136 source files
- `alembic/versions/` unchanged at 24 files — confirms AWAITING_CLOUD is code-only (no migration)

## Next Phase Readiness
- All primitives Plans 02 (per-file router), 03 (backfill), and 04 (release cron) compose against now exist with passing unit tests.
- No blockers.

---
*Phase: 49-duration-routing-backfill*
*Completed: 2026-06-25*
