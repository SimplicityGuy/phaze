---
phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker
plan: 04
subsystem: queue-control-plane-api
tags: [fastapi, saq, postgres, pipeline-control, endpoints, pause, priority, resume]

# Dependency graph
requires:
  - phase: 37-02-helpers
    provides: "set_stage_priority / pause_stage / resume_stage raw saq_jobs UPDATE helpers (allowlist-guarded, bound-param, no-commit)"
  - phase: 37-01-substrate
    provides: "PipelineStageControl ORM model + STAGE_TO_FUNCTION allowlist constants (migration 020 seeds the 3 rows)"
  - phase: 37-03-integration-tests
    provides: "real-PG proof of the helpers (priority-0 floor, drain-pause, sentinel-guarded resume) + the column-vs-blob finding driving the control-row response shape"
provides:
  - "POST /pipeline/stages/{stage}/{priority,pause,resume} -- the 3 operator control endpoints, mounted on the API app"
  - "StagePriorityDelta request schema (signed delta, UI step +/-10)"
  - "control row + live saq_jobs backlog mutated in ONE transaction; {stage, priority, paused} returned from the durable control row"
  - "endpoint unit tests (validation / clamp at both bounds / persistence / return-shape)"
  - "README documentation of the control plane + the locked Open-Q defaults"
affects: [38-pipeline-dag-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "control endpoint returns {stage, priority, paused} from the PipelineStageControl row (durable intent), NEVER a serialized job's priority (Plan-03 column-vs-blob finding)"
    - "allowlist-validate stage -> 422 BEFORE any backlog filter; clamp priority delta to [0,100] before the row + backlog are touched"
    - "control-row ORM mutation + service-helper backlog UPDATE land in a single session.commit() so durable intent and live backlog never diverge"

key-files:
  created:
    - src/phaze/schemas/pipeline_stages.py
    - src/phaze/routers/pipeline_stages.py
    - tests/test_routers/test_stage_endpoints.py
  modified:
    - src/phaze/main.py
    - README.md

key-decisions:
  - "Response priority/paused come from the control row, not a job blob -- a raw saq_jobs priority UPDATE reorders the dequeue column but leaves the serialized Job.priority stale (Plan-03 finding), so the control row is the authoritative response source"
  - "_load_control_row creates the row at defaults (priority=50, paused=False) if absent -- a Rule-2 defensive backstop that keeps a fresh/partially-migrated DB from 500ing AND gives the type checker a non-null return; migration 020 means production always finds the row"
  - "unknown stage -> HTTPException(422) (the helpers' ValueError is pre-empted by the router's own allowlist check so the 422 carries a clean detail)"
  - "no app-layer auth added (consistent with /pipeline/* and /saq) -- reverse-proxy internal-realm auth is the sole access control (T-37-04, accept)"

requirements-completed: [REQ-37-1, REQ-37-2, REQ-37-3]

# Metrics
duration: ~6min
completed: 2026-06-13
---

# Phase 37 Plan 04: Per-Stage Pause/Priority Control Endpoints Summary

**The three operator-facing control endpoints (`POST /pipeline/stages/{stage}/{priority,pause,resume}`) that validate the stage against the allowlist (422 on unknown), clamp a priority delta to `[0,100]`, mutate the durable `pipeline_stage_control` row + the live `saq_jobs` backlog in a single transaction, and return `{stage, priority, paused}` from the control row -- plus httpx unit tests and the README documentation of the locked Open-Q defaults.**

## Performance

- **Duration:** ~6 min
- **Completed:** 2026-06-13
- **Tasks:** 3
- **Files modified:** 5 (3 created, 2 modified)

## Accomplishments

- **`StagePriorityDelta` schema** (`schemas/pipeline_stages.py`): a one-field `{delta: int}` request body; the docstring records the ±10 UI step and the lower=sooner / clamp-to-`[0,100]` semantics.
- **`pipeline_stages` router** (`routers/pipeline_stages.py`): three POST endpoints. Each validates `stage in STAGE_TO_FUNCTION` first (`raise HTTPException(422, "unknown stage")` otherwise — T-37-01), loads the control row, mutates it + calls the matching Plan-02 service helper (`set_stage_priority` / `pause_stage` / `resume_stage`), `await session.commit()`s the pair atomically, and returns `{stage, priority, paused}` from the row. `priority` clamps `row.priority + delta` to `[0,100]` (T-37-02). No app-layer auth (T-37-04, accept).
- **Registration** (`main.py`): added `pipeline_stages` to the routers import block and `app.include_router(pipeline_stages.router)` next to `pipeline.router`.
- **Endpoint tests** (`tests/test_routers/test_stage_endpoints.py`): 5 httpx `AsyncClient` tests — unknown→422, clamp-high→100, clamp-low→0, valid delta persists the new absolute priority, pause→resume flip + persist `paused`. A minimal empty `saq_jobs` table lets the helpers' raw UPDATE no-op (real backlog reorder/park is proven by the Plan-03 integration tests).
- **README**: a new "🎚️ Per-Stage Pause & Priority" subsection documents all three endpoints, the lower=sooner direct-SAQ-mapping semantic, the drain-pause behavior, and the four adopted Open-Q defaults (5s cache TTL, pause-persists-reboot, resume-unpark-only, delta ±10).

## Task Commits

1. **Task 1: schema + router (3 endpoints) + registration** — `ccbaed7` (feat)
2. **Task 2: endpoint validation / clamp / persistence / return-shape tests** — `46cded9` (test)
3. **Task 3: document the control endpoints + Open-Q defaults** — `2f0a5db` (docs)

## Files Created/Modified

- `src/phaze/schemas/pipeline_stages.py` *(created)* — `StagePriorityDelta` request body.
- `src/phaze/routers/pipeline_stages.py` *(created)* — the 3 control endpoints + `_validate_stage` / `_load_control_row` / `_response` helpers.
- `tests/test_routers/test_stage_endpoints.py` *(created)* — 5 endpoint unit tests + an empty-`saq_jobs` mirror so the helper UPDATEs no-op.
- `src/phaze/main.py` *(modified)* — import + `include_router(pipeline_stages.router)`.
- `README.md` *(modified)* — Per-Stage Pause & Priority docs + locked defaults.

## Decisions Made

- **Control row is the response authority** — Plan-03 proved a raw `saq_jobs` priority UPDATE reorders the dequeue column but does NOT rewrite the serialized `job` BYTEA, so a later-dequeued `Job.priority` is the stale enqueue-time stamp. The endpoint therefore returns `{stage, priority, paused}` from the `PipelineStageControl` row (the durable intent), never from a job.
- **Single-transaction mutation** — the ORM row update and the service-helper backlog UPDATE share one `session.commit()`, so durable intent and the live backlog can never diverge (the helpers deliberately do not commit — the endpoint owns the txn, per Plan 02).
- **Defensive row auto-create** — `_load_control_row` creates the row at defaults if `session.get` returns `None`. Production always finds the migration-020 seed; this backstop keeps a fresh DB from 500ing and yields a non-null return for mypy strict.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - missing critical functionality] Defensive control-row auto-create**
- **Found during:** Task 1
- **Issue:** The RESEARCH contract used `row = await session.get(PipelineStageControl, stage)` and then `row.priority`, but `session.get` returns `PipelineStageControl | None`; mypy strict rejects the attribute access, and a missing seed row would raise an unhandled `AttributeError` (500).
- **Fix:** `_load_control_row` returns the row, creating it at defaults (`priority=50, paused=False`) when absent. Non-null return satisfies the type checker and a fresh/partially-migrated DB no longer 500s on first control action.
- **Files modified:** `src/phaze/routers/pipeline_stages.py`
- **Commit:** `ccbaed7`

**2. [Rule 3 - blocking lint] Targeted noqa + ASCII docstring**
- **Found during:** Task 1
- **Issue:** ruff `TC001` wanted the `StagePriorityDelta` import moved into a `TYPE_CHECKING` block — but it is a FastAPI request-body annotation resolved at runtime by Pydantic (exactly the rewrite CLAUDE.md warns breaks FastAPI). ruff `RUF002` flagged the en-dash in the schema docstring.
- **Fix:** `# noqa: TC001` on the runtime import with an inline reason; replaced `±`/`–` with ASCII `+/-`/`-` in the schema docstring.
- **Files modified:** `src/phaze/routers/pipeline_stages.py`, `src/phaze/schemas/pipeline_stages.py`
- **Commit:** `ccbaed7`

### Plan note (helper-vs-queue test wiring)
The plan's test interface said to use the `tests/_queue_fakes.py` fake queues so "the saq_jobs UPDATE issued by the helpers no-ops". In fact the Plan-02 helpers run their UPDATE against the SQLAlchemy `session`, not the SAQ queue, so the queue fakes do not intercept them. Instead the test creates a minimal empty `saq_jobs` table (mirroring the Plan-03 integration conftest) so the real helper SQL runs and no-ops — exercising the endpoint→helper→SQL wiring end-to-end. No functional impact; the validation/clamp/return-shape focus is unchanged.

## Known Stubs

None — the endpoints are fully wired to the durable control row and the live-backlog service helpers.

## Threat Flags

None — no new trust-boundary surface beyond the plan's threat model. The endpoints sit behind the same reverse-proxy internal-realm auth as the rest of `/pipeline/*`.

## Issues Encountered

None blocking. The two ruff findings above were fixed before the Task 1 commit. All pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on every commit without `--no-verify`.

## Verification

- `TEST_DATABASE_URL=...:5433/phaze_test PHAZE_REDIS_URL=...:6380/0 uv run pytest tests/test_routers/test_stage_endpoints.py -q` → **5 passed**.
- Regression + coverage: `uv run pytest tests/test_routers/test_stage_endpoints.py tests/test_routers/test_pipeline.py` → **43 passed**; `routers/pipeline_stages.py` **95.74%**, `schemas/pipeline_stages.py` **100.00%** (≥85% gate met; the 2 uncovered lines are the defensive auto-create backstop).
- `uv run python -c "from phaze.main import create_app; ..."` → the 3 routes (`/pipeline/stages/{stage}/{priority,pause,resume}`) are mounted.
- `uv run mypy src/phaze` → no issues (135 files); `uv run ruff check .` → all checks passed.
- README: `grep` confirms `/pipeline/stages/`, the "lower" priority semantic, and "drain" pause behavior are documented.

## Next Phase Readiness

- Phase 38 can wire the DAG-node pause toggle + priority stepper to these endpoints (▲Higher decrements the number, ▼Lower increments) and re-render from the returned `{stage, priority, paused}`. The "Rescan Files" anchor removal is also a Phase 38 item.

## Self-Check: PASSED

- Created/modified files present: `src/phaze/schemas/pipeline_stages.py`, `src/phaze/routers/pipeline_stages.py`, `tests/test_routers/test_stage_endpoints.py`, `src/phaze/main.py`, `README.md` — all FOUND.
- Commits present: `ccbaed7`, `46cded9`, `2f0a5db` — all FOUND in git log.

---
*Phase: 37-per-stage-pause-and-priority-control-plane-table-api-worker*
*Completed: 2026-06-13*
