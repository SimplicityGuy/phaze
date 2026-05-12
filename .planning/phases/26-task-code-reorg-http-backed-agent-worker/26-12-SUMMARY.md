---
phase: 26-task-code-reorg-http-backed-agent-worker
plan: 12
subsystem: api
tags: [python, fastapi, lifespan, wiring, agent-task-router, redis, integration]

# Dependency graph
requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker (Plan 04)
    provides: AgentTaskRouter -- per-agent SAQ enqueuer (constructor + close + enqueue_for_agent)
  - phase: 26-task-code-reorg-http-backed-agent-worker (Plan 05)
    provides: agent_identity router -- GET /api/internal/agent/whoami
  - phase: 26-task-code-reorg-http-backed-agent-worker (Plan 06)
    provides: agent_analysis router -- PUT /api/internal/agent/analysis/{file_id}
  - phase: 26-task-code-reorg-http-backed-agent-worker (Plan 07)
    provides: agent_tracklists router -- POST /api/internal/agent/tracklists (reads request.app.state.redis)
  - phase: 26-task-code-reorg-http-backed-agent-worker (Plan 08)
    provides: agent_proposals router -- PATCH /api/internal/agent/proposals/{id}/state
  - phase: 26-task-code-reorg-http-backed-agent-worker (Plan 03)
    provides: ExtractMetadataPayload schema for the auto-enqueue refactor
provides:
  - "All 10 /api/internal/agent/* endpoints reachable from phaze.main.create_app()"
  - "app.state.task_router -- lifespan-wired AgentTaskRouter, closed on shutdown (D-20)"
  - "app.state.redis -- lifespan-wired async Redis client with decode_responses=True for tracklists idempotency cache (D-27)"
  - "agent_files.upsert_files refactored: no more inline Queue construction; uses app.state.task_router.enqueue_for_agent"
  - "FileRecord.original_path returned by the upsert RETURNING clause for typed payload construction"
affects:
  - 26-13 (docker-compose / deployment work -- this plan finalizes the controller-side HTTP surface)
  - 27 (watcher service consumes /api/internal/agent/files which now routes via AgentTaskRouter)
  - 29 (Agents admin page uses /whoami via the now-wired router)

# Tech tracking
tech-stack:
  added:
    - "redis.asyncio (already a transitive of saq[redis]; first direct import in src/phaze/main.py)"
  patterns:
    - "FastAPI lifespan owns long-lived per-agent SAQ enqueuer (AgentTaskRouter) instead of per-request Queue construction"
    - "Shared async Redis client lifespan-owned, with decode_responses=True for str-typed get/set"
    - "Handler reads app.state.task_router via request.app.state.task_router instead of constructing per call"
    - "Test fixtures install AsyncMock at app.state.task_router on smoke-app to exercise the production handler in isolation"

key-files:
  created: []
  modified:
    - "src/phaze/main.py (74 -> 103 lines) -- 4 new include_router calls, AgentTaskRouter + redis_async wired into lifespan, reverse-order shutdown"
    - "src/phaze/routers/agent_files.py (124 -> 138 lines) -- inline Queue replaced with app.state.task_router; original_path added to RETURNING; ExtractMetadataPayload import"
    - "tests/test_routers/test_agent_files.py (197 -> 207 lines) -- fixture migrated from patch(Queue) to AsyncMock at app.state.task_router; payload assertions inspect ExtractMetadataPayload fields"

key-decisions:
  - "D-15 honored: agent_identity router (whoami) included after Phase 25 agent routers (consistent grouping)"
  - "D-20 honored: AgentTaskRouter wired exactly once in lifespan; shutdown calls close() before redis.aclose() and queue.disconnect() (reverse construction order)"
  - "D-21 honored: agent_files.upsert_files no longer owns Queue lifecycle; reads app.state.task_router; original_path added to RETURNING"
  - "D-26/D-27/D-28 honored: agent_analysis, agent_tracklists, agent_proposals routers registered in the production app"
  - "T-26-12-D mitigation accepted as-is: shutdown failures are surfaced (no swallow) -- container restart is the recovery path"

patterns-established:
  - "Lifespan resource ordering: construct (queue -> task_router -> redis); shutdown reverse (task_router -> redis -> queue). Documented inline as a maintenance signpost."
  - "Smoke-app fixture exposes both client and mock_router as a tuple so tests can assert against enqueue calls without patching the import path"

requirements-completed: [DIST-03]

# Metrics
duration: 7m 25s
completed: 2026-05-12
---

# Phase 26 Plan 12: FastAPI Wiring Integration Summary

**Wires the four new Phase 26 agent routers (whoami, analysis, tracklists, proposals) into `create_app()`, installs `AgentTaskRouter` + async Redis client at `app.state.task_router` / `app.state.redis` in the FastAPI lifespan, and refactors `agent_files.upsert_files` off the inline `Queue.from_url` pattern onto the lifespan-wired router.**

## Performance

- **Duration:** 7m 25s
- **Started:** 2026-05-12T22:39:23Z
- **Completed:** 2026-05-12T22:46:48Z
- **Tasks:** 2
- **Files modified:** 3
- **Files created:** 0

## Accomplishments

- `phaze.main.create_app()` registers all four new routers: `agent_identity`, `agent_analysis`, `agent_tracklists`, `agent_proposals` (in addition to the 5 Phase 25 routers, 12 Phase <=24 routers).
- `app.state.task_router = AgentTaskRouter(redis_url=settings.redis_url)` wired in lifespan startup (D-20). Handlers read it via `request.app.state.task_router`.
- `app.state.redis = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)` wired in lifespan startup (D-27). `agent_tracklists.py` reads it via the `_get_redis` dependency (already in place from Plan 07).
- Lifespan shutdown closes resources in reverse construction order: `await task_router.close()` then `await redis.aclose()` then `await queue.disconnect()` then `await engine.dispose()`. Order matters because `task_router` shares the Redis pool with cached per-agent SAQ Queues.
- `agent_files.upsert_files` refactored:
  - Handler signature gains `request: Request` (positional, before `body`).
  - `from saq import Queue` import removed.
  - `from fastapi import Request` and `from phaze.schemas.agent_tasks import ExtractMetadataPayload` added.
  - UPSERT `RETURNING(...)` clause extended with `FileRecord.original_path` so the typed payload can be built without re-querying.
  - The inline `Queue.from_url(settings.redis_url, name=...)` + `try/finally: queue.disconnect()` block replaced with `await task_router.enqueue_for_agent(agent_id=..., task_name="extract_file_metadata", payload=ExtractMetadataPayload(...))`. No `finally` block -- lifecycle is owned by the FastAPI lifespan.
  - Best-effort enqueue semantics preserved: exceptions are logged + counted as not-enqueued; the DB upsert is committed before the enqueue loop runs.
- Existing test suite at `tests/test_routers/test_agent_files.py` migrated:
  - Smoke-app fixture splits into `_make_smoke_app(session) -> (app, AsyncMock)` and a new `smoke_app_and_router` pytest fixture exposing both the client and the mock router.
  - `test_auto_enqueue_only_for_inserts` and `test_no_enqueue_for_updates` now inspect `mock_router.enqueue_for_agent.await_args_list` instead of patching `phaze.routers.agent_files.Queue`. Payload assertions verify `ExtractMetadataPayload` attributes (file_id is UUID, file_type == "mp3", agent_id == auth agent.id, original_path starts with `/test/music/`).
  - All 9 existing tests pass; no behavioral regressions.
- Verifications green:
  - `uv run mypy .` -> Success: no issues found in 109 source files
  - `uv run ruff check .` -> All checks passed
  - `uv run pytest tests/test_routers/test_agent_files.py tests/test_services/test_agent_upsert.py -x -q --no-cov` -> 10 passed in ~2s
  - `uv run pytest tests/test_routers/ --deselect tests/test_routers/test_agent_tracklists.py -q --no-cov` -> 259 passed (tracklists tests are marked `@pytest.mark.integration` and require an external Redis instance -- pre-existing state).
  - Runtime smoke check enumerates exactly 10 sorted unique `/api/internal/agent/*` paths from `create_app()`.

## Task Commits

Each task was committed atomically on `worktree-agent-a75fd56a889960506`:

1. **Task 1: Wire new routers + AgentTaskRouter + Redis into main.py lifespan** -- `71696c8` (feat)
2. **Task 2: Refactor agent_files.upsert_files to use app.state.task_router** -- `6899d8a` (refactor)

## Files Created/Modified

- `src/phaze/main.py` (modified, +32 / -3) -- four new include_router calls, AgentTaskRouter + redis_async lifespan wiring, reverse-order shutdown.
- `src/phaze/routers/agent_files.py` (modified, +30 / -33) -- inline Queue replaced with `app.state.task_router`; `original_path` added to RETURNING; `ExtractMetadataPayload` import added; `Request` parameter added to handler.
- `tests/test_routers/test_agent_files.py` (modified, +90 / -52) -- fixture refactored to inject AsyncMock at `app.state.task_router`; payload-shape assertions migrated to `ExtractMetadataPayload` attributes.

## Decisions Made

None of note beyond strict adherence to D-15, D-20, D-21, D-26, D-27, D-28. The plan body specified the changes verbatim; the only judgment call was the docstring wording fix (see Deviations) and the test-fixture migration shape (split `authenticated_client` into a `smoke_app_and_router` tuple-fixture so tests that need the mock router can introspect it while tests that don't need it consume the thin `authenticated_client` wrapper). Both choices align with the plan's "agent_files.py existing tests may need updating to patch app.state.task_router instead" guidance.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Docstring still referenced removed `Queue.from_url` symbol**
- **Found during:** Task 2 acceptance-criterion grep.
- **Issue:** The plan's acceptance criterion `grep -c "Queue.from_url" src/phaze/routers/agent_files.py` must return `0`. After removing the inline construction, the file's module docstring still contained the phrase "replaces the inline `Queue.from_url` pattern", which counted as a hit.
- **Fix:** Reworded the docstring to "replaces the inline per-handler Queue pattern" -- semantically equivalent but does not contain the `Queue.from_url` literal token.
- **Files modified:** `src/phaze/routers/agent_files.py` (docstring only; no behavior change).
- **Verification:** `grep -c "Queue.from_url" src/phaze/routers/agent_files.py` -> 0; `grep -c "from saq import Queue" src/phaze/routers/agent_files.py` -> 0; `grep -c "await queue.disconnect" src/phaze/routers/agent_files.py` -> 0.
- **Committed in:** `6899d8a` (Task 2 commit, before commit).

---

**Total deviations:** 1 auto-fixed (1 cosmetic docstring rewording to satisfy acceptance grep).
**Impact on plan:** Zero behavioral effect. Pure documentation wording.

## Test Fixture Migration Notes

The existing `tests/test_routers/test_agent_files.py` used `with patch("phaze.routers.agent_files.Queue") as MockQueue: MockQueue.from_url.return_value = AsyncMock()` in seven places. After the Plan 26-12 refactor, the `Queue` symbol is no longer imported by `agent_files.py`, so that patch target is invalid. Two clean migration shapes were considered:

1. **Patch the AgentTaskRouter at the import site** (`patch("phaze.routers.agent_files.task_router", ...)` -- doesn't work because the handler reads it from `app.state` at call time, not at module load time).
2. **Inject an AsyncMock at `app.state.task_router` on the smoke-app fixture** (matches how the production lifespan wires it; tests exercise the real handler against a mock dependency).

Option 2 was chosen because it (a) keeps the production handler under test verbatim and (b) mirrors the smoke-app pattern already used by Plan 25-02 `tests/test_routers/test_agent_auth.py`. The fixture now exposes both the test client and the mock router as a tuple (`smoke_app_and_router`) so tests that need to assert against enqueue calls have access to the mock; tests that only check HTTP responses consume a thin `authenticated_client` wrapper that drops the tuple's second element.

## Pre-existing Integration-Marker Tests

`tests/test_routers/test_agent_tracklists.py` ships 7 tests all marked `@pytest.mark.integration` -- they require a real Redis at `redis://localhost:6379/0` (the default of `PHAZE_REDIS_URL`). These tests fail in the worktree environment because no Redis container is running locally. This is pre-existing state from Plan 26-07 and is out of scope for Plan 26-12. All 259 router tests *excluding* `test_agent_tracklists.py` pass.

## Issues Encountered

- The worktree was created from `main` before Phase 26 wave work landed. Required a one-time `git merge gsd/phase-26-task-code-reorg-http-backed-agent-worker` into the worktree branch as a setup step so that all Wave 2-4 outputs (PhazeAgentClient, AgentTaskRouter, the four new routers, controller.py) were present for `main.py` to import. After the merge, all imports resolved cleanly.

## User Setup Required

None for Plan 26-12 itself. To run the full router test suite green, a local Redis is needed for the `@pytest.mark.integration` tests in `test_agent_tracklists.py` -- typically `docker compose up -d redis` from the project root. The non-integration tests (the 259 that pass) need only Postgres (already in conftest).

## Smoke Test (Manual Validation Beyond Automated Verify)

```
$ uv run python -c "from phaze.main import create_app; app = create_app(); paths = sorted({str(r.path) for r in app.routes if '/api/internal/agent' in str(r.path)}); print(len(paths), 'agent routes'); [print(' ', p) for p in paths]"
10 agent routes
  /api/internal/agent/analysis/{file_id}
  /api/internal/agent/execution-log
  /api/internal/agent/execution-log/{execution_log_id}
  /api/internal/agent/files
  /api/internal/agent/fingerprints/{file_id}/{engine}
  /api/internal/agent/heartbeat
  /api/internal/agent/metadata/{file_id}
  /api/internal/agent/proposals/{proposal_id}/state
  /api/internal/agent/tracklists
  /api/internal/agent/whoami
```

Matches the plan's expected output exactly (10 paths in sorted order).

## Next Phase Readiness

- **Plan 26-13 (docker-compose flip + legacy worker.py delete):** The controller-side HTTP surface is now complete and serving all 10 agent endpoints. Plan 13 can land the deployment update without further router work.
- **Phase 27 (file watcher service):** Plan 27 will enqueue via `AgentTaskRouter`, which now lives at `app.state.task_router` and is already invoked by `agent_files.upsert_files`. The pattern is established.
- **No blockers.** mypy + ruff + format + pre-commit all green; 259 router tests pass; smoke check confirms 10 agent routes.

## Self-Check: PASSED

Verified:
- `src/phaze/main.py` modified (32 inserts, 3 deletes) -- present in HEAD~1 (`71696c8`)
- `src/phaze/routers/agent_files.py` modified (30 inserts, 33 deletes) -- present in HEAD (`6899d8a`)
- `tests/test_routers/test_agent_files.py` modified (90 inserts, 52 deletes) -- present in HEAD (`6899d8a`)
- Commit `71696c8` (Task 1) found in `git log --oneline`
- Commit `6899d8a` (Task 2) found in `git log --oneline`
- `uv run mypy .` -> Success: no issues found in 109 source files
- `uv run ruff check .` -> All checks passed
- `uv run pytest tests/test_routers/test_agent_files.py tests/test_services/test_agent_upsert.py -x -q --no-cov` -> 10 passed in 2.05s
- `uv run pytest tests/test_routers/ --deselect tests/test_routers/test_agent_tracklists.py -q --no-cov` -> 259 passed in 52.5s
- Runtime smoke check confirms 10 unique `/api/internal/agent/*` paths from `create_app()`

---
*Phase: 26-task-code-reorg-http-backed-agent-worker*
*Completed: 2026-05-12*
