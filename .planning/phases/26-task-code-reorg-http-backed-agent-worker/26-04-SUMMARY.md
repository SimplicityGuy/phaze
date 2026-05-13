---
phase: 26-task-code-reorg-http-backed-agent-worker
plan: 04
subsystem: infra

tags: [python, saq, redis, async, task-queue]

# Dependency graph
requires:
  - phase: 26
    provides: AgentSettings (Plan 01), PhazeAgentClient (Plan 02), ExtractMetadataPayload + other SAQ payload schemas (Plan 03)
provides:
  - "AgentTaskRouter service: lazy per-agent SAQ Queue cache with two enqueue surfaces (by agent_id, by FileRecord)"
  - "Reusable enqueue API consumable by future controller-side routers (Plan 12 wires it into FastAPI lifespan)"
affects: [26-12 (lifespan wiring + agent_files.py refactor), 27 (user-initiated scan), 28 (batch execution dispatch)]

# Tech tracking
tech-stack:
  added: []   # No new dependencies; uses pre-existing saq + pydantic
  patterns:
    - "Lazy per-agent Queue cache: dict[str, Queue] with `_queue_for(agent_id)` lookup-or-construct"
    - "Idempotent close(): disconnect every cached Queue then `cache.clear()`"
    - "Two enqueue surfaces (by agent_id, by FileRecord) sharing one underlying cache"

key-files:
  created:
    - src/phaze/services/agent_task_router.py
    - tests/test_services/test_agent_task_router.py
  modified:
    - src/phaze/services/agent_client.py  # Rule 3 unblocker -- retired stale type:ignore tripwires

key-decisions:
  - "Internal cache impl: plain `dict[str, Queue]` (chosen over functools.cache and LRU). Simple, no locking required for the single-process FastAPI app, and avoids the FD-leak risk an LRU eviction would create (evicting without `.disconnect()`)."
  - "Integration tests use a real Redis (no fakeredis fallback) per D-30; tests are marked `@pytest.mark.integration` and skip cleanly when Redis is unreachable."

patterns-established:
  - "Per-agent SAQ queue naming: `phaze-agent-<agent_id>` (D-18 invariant). Agent_id slug guarantees Redis-safe key chars via Phase 24 CHECK constraint."
  - "Lifecycle ownership: service instantiated once at FastAPI lifespan startup, `close()` on shutdown -- Plan 12 wires it."

requirements-completed:
  - DIST-03
  - TASK-02

# Metrics
duration: 5min
completed: 2026-05-12
---

# Phase 26 Plan 04: Task Code Reorg & HTTP-Backed Agent Worker -- AgentTaskRouter Summary

**Controller-side per-agent SAQ enqueuer with lazy `Queue.from_url` cache and `enqueue_for_agent` / `enqueue_for_file` surfaces, replacing the inline `Queue.from_url + try/finally: disconnect()` block in `agent_files.py`.**

## Performance

- **Duration:** 5 min 23 sec
- **Started:** 2026-05-12T21:36:02Z
- **Completed:** 2026-05-12T21:41:25Z
- **Tasks:** 2 (both autonomous, both TDD)
- **Files modified:** 3 (2 in-scope, 1 deviation unblocker)

## Accomplishments

- `AgentTaskRouter` class (`src/phaze/services/agent_task_router.py`) with:
  - `__init__(redis_url)` storing URL + empty `dict[str, Queue]` cache.
  - `_queue_for(agent_id)` lazy `Queue.from_url(self._redis_url, name=f"phaze-agent-{agent_id}")` construction; second call for same agent_id returns the SAME Queue instance.
  - `enqueue_for_agent(*, agent_id, task_name, payload: BaseModel)` -> serializes payload via `model_dump(mode="json")` and forwards to `queue.enqueue(task_name, **kwargs)`.
  - `enqueue_for_file(*, file_record, task_name, payload)` -> derives `agent_id` from `file_record.agent_id` and delegates.
  - `close()` -> awaits `queue.disconnect()` for every cached Queue then clears the cache (idempotent).
- 4 integration tests against a real Redis (cache identity, two-agent isolation, close-empties-cache, enqueue_for_file delegation). All pass against `redis://localhost:6379/0` (phaze-redis-tests container).
- Module opted into mypy strict via the pre-existing `[[tool.mypy.overrides]]` block from Plan 01.

## Task Commits

1. **Task 0 (deviation unblocker): retire stale `# type: ignore[import-not-found]` tripwires in `agent_client.py`** -- `a6dc604` (chore)
2. **Task 1: write failing integration tests for AgentTaskRouter (RED)** -- `1eb3e83` (test)
3. **Task 2: implement AgentTaskRouter with lazy per-agent Queue cache (GREEN)** -- `de1def8` (feat)

_Final docs/state commit pending (`docs(26-04): complete plan ...`)._

## Files Created/Modified

- `src/phaze/services/agent_task_router.py` (108 lines) -- new module implementing `AgentTaskRouter`.
- `tests/test_services/test_agent_task_router.py` (99 lines) -- 4 integration tests against real Redis with `PHAZE_REDIS_URL` env override.
- `src/phaze/services/agent_client.py` (-8 / +5 lines) -- deviation unblocker only; removed the 4 `# type: ignore[import-not-found]` markers that the Wave 2 author explicitly designed as self-deleting tripwires for the Plan 03 merge.

## Decisions Made

- **Internal cache implementation:** plain `dict[str, Queue]`. Rejected `functools.cache` (adds unneeded layer for single-instance service) and LRU (eviction without `.disconnect()` leaks Redis connections; bounded growth not needed for the realistic 1-5 agent v4.0 scale).
- **No fakeredis fallback in tests:** SAQ's `Queue.from_url` does not interoperate cleanly with `fakeredis` at our pinned `saq>=0.26.3`. Per D-30, integration tests use a real Redis instance and skip when unreachable (Phaze's project conftest auto-tags fixture-based integration tests; we add the explicit `@pytest.mark.integration` marker on each test).
- **Queue cache key vs queue name:** cache key is the raw `agent_id` (kebab-case slug); the `Queue.name` attribute is the prefixed `phaze-agent-<agent_id>`. This matches the Phase 25 D-22 hard-coded inline pattern and Phase 26 D-18 invariant.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Retired stale `# type: ignore[import-not-found]` markers in `agent_client.py`**
- **Found during:** Task 1 (test commit was blocked by pre-commit's `mypy .` hook).
- **Issue:** The Wave 2 (Plan 02) implementation of `PhazeAgentClient` included 4 `# type: ignore[import-not-found]` comments on Phase 26 schema imports as a parallelization-debt mitigation. The Wave 2 author's comment explicitly stated they were a "self-deleting tripwire" intended to be removed once Plan 03 merged. Plan 03 has now merged, the schema modules exist, and `warn_unused_ignores` flags the now-stale markers -- producing 4 errors that block every commit on this branch.
- **Fix:** Removed the 4 `# type: ignore[import-not-found]` comments verbatim (and the surrounding 3-line parallelization-debt comment), as the original author designed.
- **Files modified:** `src/phaze/services/agent_client.py`
- **Verification:** `uv run mypy .` -- "Success: no issues found in 102 source files".
- **Committed in:** `a6dc604` (separated from Plan 26-04 substantive commits for clarity).
- **Scope justification:** This file is not in Plan 26-04's `files_modified`. However, every commit on the branch is gated by `mypy .` as a pre-commit hook, and the SDK config forbids `--no-verify`. The fix is exactly what the Wave 2 author scripted for the Plan 03-merge state we are now in. Touch surface is minimal (5 lines removed, no semantic change).

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Zero scope creep -- the deviation only removes already-stale comments the original author designed to be removed at this exact point. AgentTaskRouter implementation itself follows the plan verbatim.

## Issues Encountered

None. The 4 integration tests passed on first GREEN run; mypy + ruff clean on first format-check.

## Threat Model Adherence

Plan's `<threat_model>` was inspected. All mitigations are either:
- Inherited (T-26-04-T uses Phase 24's `CHECK(id ~ '...')` constraint, not router-side validation).
- Deferred to other plans (T-26-04-D shutdown-leak -> Plan 12 lifespan wiring; T-26-04-E body-trust -> Plan 12 router refactor + caller code review).

No new threats introduced beyond those already enumerated.

## User Setup Required

None - no external service configuration required for this plan. Operators running tests need a local Redis (`docker compose up redis` or `redis-server` on port 6379) for the integration tests; the test conftest setup is unchanged.

## Next Phase Readiness

- **Plan 12 (`agent_files.py` refactor + lifespan wiring):** ready to consume `AgentTaskRouter` from `app.state.task_router`. Public API surface: `enqueue_for_agent(agent_id=, task_name=, payload=)` / `enqueue_for_file(file_record=, ...)` / `close()`.
- **Plans 27 / 28 (user-initiated scan, batch execution dispatch):** can call `request.app.state.task_router.enqueue_for_file(...)` once Plan 12 ships the lifespan wiring.
- No blockers carried forward.

## Self-Check: PASSED

- File `src/phaze/services/agent_task_router.py` exists -- FOUND.
- File `tests/test_services/test_agent_task_router.py` exists -- FOUND.
- Commit `a6dc604` (chore unblocker) -- FOUND.
- Commit `1eb3e83` (RED test) -- FOUND.
- Commit `de1def8` (GREEN impl) -- FOUND.
- 4 integration tests pass against real Redis (`redis://localhost:6379/0`).
- `uv run mypy .`: 102 source files, 0 errors.
- `uv run ruff check .`: All checks passed.
- `uv run ruff format --check .`: 170 files already formatted.
- `pre-commit run --all-files`: All hooks pass.

---
*Phase: 26-task-code-reorg-http-backed-agent-worker*
*Plan: 04*
*Completed: 2026-05-12*
