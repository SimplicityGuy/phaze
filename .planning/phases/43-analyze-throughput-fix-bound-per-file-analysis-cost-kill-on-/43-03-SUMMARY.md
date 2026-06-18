---
phase: 43-analyze-throughput-fix
plan: 03
subsystem: api
tags: [fastapi, sqlalchemy, alembic, postgres, pydantic, agent-internal-api, state-machine]

# Dependency graph
requires:
  - phase: 43-02
    provides: "analyze_file's five-field coverage contract (fine/coarse windows analyzed/total, sampled) that this plan persists"
provides:
  - "Alembic rev 021 adding five all-nullable coverage columns to the analysis table"
  - "FileState.ANALYSIS_FAILED enum member (code-only, no enum migration)"
  - "AnalysisWritePayload extended with the five coverage fields"
  - "AnalysisFailurePayload + AnalysisFailureResponse schemas"
  - "put_analysis advances files.state to ANALYZED on a non-empty PUT (fixes re-enqueue-all latent bug)"
  - "POST /api/internal/agent/analysis/{file_id}/failed -> FileState.ANALYSIS_FAILED behind agent auth"
  - "PhazeAgentClient.report_analysis_failed worker-callable client method"
affects: [43-analyze-throughput-fix worker plans, pipeline status/UI, recovery automation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Coverage fields hit dedicated columns by registering names in _ANALYSIS_COLUMN_FIELDS (avoids features JSONB overflow funnel)"
    - "Worker terminal-state reporting via a path-only authenticated POST endpoint mirroring put_analysis"
    - "State-advance inside the same upsert transaction, gated on a non-empty write"

key-files:
  created:
    - alembic/versions/021_add_analysis_coverage_columns.py
  modified:
    - src/phaze/models/analysis.py
    - src/phaze/models/file.py
    - src/phaze/schemas/agent_analysis.py
    - src/phaze/routers/agent_analysis.py
    - src/phaze/services/agent_client.py
    - tests/test_schemas/test_agent_analysis.py
    - tests/test_routers/test_agent_analysis.py
    - tests/test_services/test_agent_client_endpoints.py

key-decisions:
  - "State-advance gated on post-funnel `dumped` truthiness: an empty-body PUT ({}) is a no-op (state preserved); any real aggregate/coverage write advances to ANALYZED"
  - "Coverage columns are all nullable with no data migration — pre-43 rows simply carry NULL coverage"
  - "Added a structured warning log on the failure endpoint (uses the body's reason/error) — observability plus it satisfies the body-param usage"

patterns-established:
  - "Register new real columns in _ANALYSIS_COLUMN_FIELDS in the same change that adds them to the payload (Pitfall 3 guard)"
  - "Terminal-state worker endpoints take file_id from the path only and the agent from get_authenticated_agent (AUTH-01)"

requirements-completed: [ANALYZE-STATE-MACHINE, ANALYZE-COVERAGE-PERSIST, ANALYZE-FAILED-ENDPOINT]

# Metrics
duration: ~12min
completed: 2026-06-17
---

# Phase 43 Plan 03: Persist Analysis State + Coverage on the Control Plane Summary

**Successful analysis PUTs now advance files.state to ANALYZED and persist five coverage columns (Alembic rev 021); a new authenticated POST /{file_id}/failed sets ANALYSIS_FAILED, with a matching PhazeAgentClient.report_analysis_failed worker method.**

## Performance

- **Duration:** ~12 min (task commits 21:30 -> 21:39 local)
- **Started:** 2026-06-18T04:27:00Z (approx, colima/test-db bring-up)
- **Completed:** 2026-06-18T04:40:00Z
- **Tasks:** 3
- **Files modified:** 8 (1 created, 7 modified)

## Accomplishments
- Fixed the latent re-enqueue-all bug: a non-empty analysis PUT now sets `files.state = 'analyzed'` in the same transaction, so analyzed files leave `discovered` and re-triggers stop re-enqueuing the whole archive.
- Coverage persists durably: migration 021 adds `fine_windows_analyzed/total`, `coarse_windows_analyzed/total` (Integer) and `sampled` (Boolean) to `analysis`; the router routes them to real columns, never the `features` JSONB overflow (Pitfall 3 guarded by a test asserting `features` stays NULL).
- Terminal-failure path: `FileState.ANALYSIS_FAILED` (code-only) plus `POST /api/internal/agent/analysis/{file_id}/failed` behind `get_authenticated_agent`, with bounded `AnalysisFailurePayload` (Literal reason, `error` max_length=2000, `extra="forbid"`).
- Worker client: `PhazeAgentClient.report_analysis_failed` POSTs the failure through the `_request` tenacity funnel (5xx retried, 4xx surfaces immediately).

## Task Commits

Each task was committed atomically:

1. **Task 1: Migration 021 + model columns + ANALYSIS_FAILED enum** - `8d85805` (feat)
2. **Task 2: Extend payloads + put_analysis state-advance + coverage columns** - `44fe34c` (feat)
3. **Task 3: Failure endpoint + report_analysis_failed client method** - `f55d4ad` (feat)

_TDD note: tasks were implemented test-alongside-code; verification ran against a live ephemeral Postgres (colima + `just test-db`, port 5433)._

## Files Created/Modified
- `alembic/versions/021_add_analysis_coverage_columns.py` - rev 021, down_revision 020; adds/drops the five coverage columns (round-trips clean).
- `src/phaze/models/analysis.py` - five `Mapped[int|None]`/`Mapped[bool|None]` coverage columns on `AnalysisResult`; imports `Boolean`.
- `src/phaze/models/file.py` - `FileState.ANALYSIS_FAILED = "analysis_failed"` (no enum migration; state is `String(30)`).
- `src/phaze/schemas/agent_analysis.py` - five coverage fields on `AnalysisWritePayload`; new `AnalysisFailurePayload` + `AnalysisFailureResponse`.
- `src/phaze/routers/agent_analysis.py` - coverage names added to `_ANALYSIS_COLUMN_FIELDS`; ANALYZED state-advance in `put_analysis`; new `report_analysis_failed` POST handler with structured warning log.
- `src/phaze/services/agent_client.py` - `report_analysis_failed` client method + TYPE_CHECKING imports.
- `tests/test_schemas/test_agent_analysis.py` - coverage-field + `AnalysisFailurePayload` validation tests.
- `tests/test_routers/test_agent_analysis.py` - state-advance, empty-PUT no-op, coverage-columns-not-features, failure-endpoint state/422/auth tests.
- `tests/test_services/test_agent_client_endpoints.py` - respx tests: correct POST path/body + 4xx-not-retried.

## Decisions Made
- State-advance gated on `dumped` truthiness so the existing empty-body no-op contract is preserved while any real write advances to ANALYZED.
- Coverage columns all nullable, no data migration (pre-43 rows carry NULL coverage).
- Added a `logger.warning` on the failure endpoint recording `reason`/`error` — genuine observability for terminal failures and a legitimate use of the validated body.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Added structured failure logging on the failure endpoint**
- **Found during:** Task 3 (failure endpoint)
- **Issue:** The `body: AnalysisFailurePayload` param exists for FastAPI validation (422 path) but was otherwise unreferenced, which ruff ARG001 flagged; more importantly a terminal-failure write with no log leaves the outcome invisible in operator logs.
- **Fix:** Added a module `structlog` logger (matching the `routers/execution.py` pattern) and a `logger.warning("analysis_failed reported", ...)` recording `file_id`, `agent_id`, `reason`, `error`.
- **Files modified:** src/phaze/routers/agent_analysis.py
- **Verification:** ruff clean; failure-endpoint router tests pass.
- **Committed in:** f55d4ad (Task 3 commit)

---

**Total deviations:** 1 auto-fixed (1 missing-critical/observability)
**Impact on plan:** Adds value (terminal-failure observability) and resolves a lint block; no scope creep, no architectural change.

## Issues Encountered
- No database or Docker daemon was running at start. Resolved by starting colima and the ephemeral test DB via `just test-db` (Postgres on 5433, `phaze_test` + `phaze_migrations_test`), then exporting `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` for the DB-backed router and migration round-trip verification. NOTE for the orchestrator: the ephemeral test DB/colima were left running; tear down via `just test-db-down` if desired.

## Threat Flags
None — no security surface beyond the planned threat register (T-43-05/06/07 all mitigated and tested).

## Next Phase Readiness
- Control plane now records analysis outcomes durably (analyzed / sampled / failed) and exposes a worker-callable failure path — the worker plans can wire `put_analysis` coverage + `report_analysis_failed` into the bounded analysis loop.
- No blockers.

## Self-Check: PASSED

- Created files verified present: `alembic/versions/021_add_analysis_coverage_columns.py`, `43-03-SUMMARY.md`.
- Task commits verified in git: `8d85805`, `44fe34c`, `f55d4ad`; metadata commit `fb2cff6`.

---
*Phase: 43-analyze-throughput-fix*
*Completed: 2026-06-17*
