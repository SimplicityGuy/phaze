---
phase: 26-task-code-reorg-http-backed-agent-worker
plan: 07
subsystem: routers
tags: [python, fastapi, redis, idempotency, postgres, http-api]

# Dependency graph
requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker
    provides: "TracklistCreatePayload + TracklistTrackPayload (max_length=2000) + TracklistCreateResponse (Plan 26-03 schemas)"
  - phase: 25-internal-agent-http-api-bearer-auth
    provides: "get_authenticated_agent dependency + HTTPBearer scheme (401/403 contract)"
provides:
  - "POST /api/internal/agent/tracklists endpoint (D-27) -- idempotent atomic Tracklist+Version+Tracks create"
  - "Three-path Redis idempotency primitive (fast-path / concurrent-writer / owner-path) with 1h TTL"
  - "Pattern: request.app.state.redis pass-through dep for handler-Redis decoupling (Plan 26-12 lifespan wiring contract)"
affects:
  - "Plan 26-11 (scan_live_set SAQ task rewrite -- calls PhazeAgentClient.create_tracklist which targets this endpoint)"
  - "Plan 26-12 (main.py lifespan must wire app.state.redis = Redis.from_url(..., decode_responses=True) + include this router)"

# Tech tracking
tech-stack:
  added: []  # redis 6.4.0 already transitively available via saq[redis]
  patterns:
    - "Stripe-style request-id idempotency via Redis SET NX EX (atomic lock + 1h TTL)"
    - "Bounded-wait concurrent-writer guard (10 * 50ms = 500ms) returning 409 on timeout"
    - "request.app.state.redis pass-through dep -- handler-Redis decoupling for smoke-app testability"
    - "Single-session multi-row write (UPSERT + INSERT version + INSERT N tracks + UPDATE pointer + commit)"
    - "sqlalchemy.update(Model)... in lieu of Model.__table__.update() (mypy-friendly, FromClause attr-defined-clean)"

key-files:
  created:
    - "src/phaze/routers/agent_tracklists.py (168 lines)"
    - "tests/test_routers/test_agent_tracklists.py (279 lines, 7 integration tests)"
  modified:
    - "src/phaze/services/agent_client.py (chore: removed 4 dead `# type: ignore[import-not-found]` comments; self-deleting tripwire armed by Plan 26-02 + tripped by Plan 26-03 schema landing)"

# Decisions made
decisions:
  - "Did NOT add payload-hash check on cached replays (T-26-07-T accept). Single-operator trust model; if future evidence reveals a problem, RESEARCH Pitfall 4's 5-line hash-and-compare addition can land in a follow-up plan."
  - "Used `sqlalchemy.update(Tracklist)` rather than `Tracklist.__table__.update()` for the latest_version_id pointer write -- mypy complains FromClause has no .update() on the Table-via-DeclarativeBase path."
  - "Smoke-app fixture sets app.state.redis directly (rather than via app.dependency_overrides[_get_redis])."
  - "Redis fixture is local to the test module (not in conftest.py) -- only this test needs a real Redis, and confining the fixture keeps the rest of the suite Redis-free."

# Metrics
metrics:
  duration: "14m 31s (1778621801 -> 1778622672 epoch)"
  completed: "2026-05-12T21:51:12Z"
  commits: 3
  tasks_completed: 2
  files_created: 2
  files_modified: 1
---

# Phase 26 Plan 07: POST /tracklists with Redis Idempotency Summary

Built `src/phaze/routers/agent_tracklists.py` -- a FastAPI router that atomically writes a Tracklist + new TracklistVersion + N TracklistTrack rows in one transaction, gated by Stripe-style request-id idempotency in Redis. All 7 integration tests pass against real Postgres + Redis, 88.24% router coverage, mypy/ruff/ruff-format clean.

## What shipped

1. **`POST /api/internal/agent/tracklists`** with the three-path idempotency model:
   - Fast path: `GET tracklist_resp:{request_id}` hit -> return cached `TracklistCreateResponse` JSON, zero DB work.
   - Concurrent-writer path: `SET tracklist_req:{request_id} 1 NX EX 3600` race lost -> poll `tracklist_resp:{request_id}` for up to 500ms (10 polls * 50ms), then 409.
   - Owner path: race won -> 1 single SQLAlchemy transaction:
     - UPSERT `Tracklist` by `external_id` (preserves row id across replays with new request_ids; `file_id` + `source` are last-write-wins on conflict).
     - `SELECT max(version_number) WHERE tracklist_id=...` then `INSERT TracklistVersion` with `version_number = max + 1` (or 1 for first).
     - `INSERT N TracklistTrack` rows (or skip when empty).
     - `UPDATE Tracklist.latest_version_id = version.id` pointer.
     - `session.commit()` exactly once.
   - `SET tracklist_resp:{request_id} <response_json> EX 3600` caches the response for the 1h TTL window.

2. **7 integration tests** (`@pytest.mark.integration`, real Redis + Postgres):
   - `test_tracklist_create_happy_path` -- POST creates Tracklist + 1 Version + N Tracks; response has correct counts.
   - `test_tracklist_idempotent_replay_returns_cached` -- replay with same request_id returns same response body; DB has no duplicates.
   - `test_tracklist_replay_with_new_request_id_creates_new_version` -- same `external_id` + new `request_id` -> same `tracklist_id`, `version=2`.
   - `test_tracklist_extra_field_422` -- `extra="forbid"` rejects `agent_id` spoofing (AUTH-01).
   - `test_tracklist_too_many_tracks_422` -- 2001-track payload rejected at body parse (T-26-07-DoS).
   - `test_tracklist_missing_auth_returns_401` / `test_tracklist_unknown_token_returns_403` -- auth contract from Phase 25.

3. **Tests use a module-local `redis_client` fixture** that connects to `PHAZE_REDIS_URL` (default `redis://localhost:6379/0`) with `decode_responses=True`, and cleans up `tracklist_req:*` / `tracklist_resp:*` keys via `scan_iter` after each test for deterministic re-runs.

## Discretion areas (per plan §action)

| Topic | Decision | Why |
|---|---|---|
| Payload-hash collision check on cached replays | **Not added** (T-26-07-T accept) | Trust model: single operator, agents are operator-controlled processes; the cached-response-for-mismatched-payload scenario is benign in practice. RESEARCH Pitfall 4's 5-line addition remains available as a drop-in if future evidence surfaces a real concern. |
| Tracks `max_length` cap | **Lives in the schema** (Plan 26-03), not the router | `TracklistCreatePayload.tracks: Field(max_length=2000)` is already in place. Boundary parse rejects oversized payloads (W7 / T-26-07-DoS); router never sees them. |
| Redis pass-through dep | **`request.app.state.redis` via thin `_get_redis` dep** | Keeps Plan 26-12's lifespan wiring as the single source-of-truth for the Redis client lifecycle; smoke-app fixtures set `app.state.redis` directly with no extra `dependency_overrides`. |
| Module-local Redis fixture | **Test-module-local** (not conftest.py) | Only this test suite needs a real Redis. Adding a project-wide fixture would either force every test to require Redis or require `pytest.skip(...)` plumbing that adds maintenance cost. |

## Test fixture insights

- **`PHAZE_REDIS_URL` env var with localhost default** -- lets CI override to a service-link hostname (e.g. `redis://redis:6379/0`) without changing test code.
- **`decode_responses=True`** -- required to match Plan 26-12's eventual lifespan wiring and keep `model_validate_json` parsing strings rather than bytes.
- **`scan_iter(match="tracklist_req:*", count=100)`** -- memory-safe alternative to `KEYS *` for the test-cleanup step; pinned `count=100` to avoid SCAN cursor over-eagerness on a small dataset.
- **`await client.aclose()`** -- correct shutdown for `redis.asyncio.Redis` (the `.close()` deprecation alias would emit a DeprecationWarning under pytest's filterwarnings).

## Coverage

| File | Stmts | Miss | Cover | Missing |
|---|---|---|---|---|
| `src/phaze/routers/agent_tracklists.py` | 51 | 6 | **88.24%** | 96-101 |

Lines 96-101 are the **concurrent-writer 409 branch** -- the `for _ in range(_CONCURRENT_POLL_MAX_ATTEMPTS)` poll loop. Testing this branch deterministically requires forcing two coroutines to win/lose the `SET NX` race, which is fragile without a synchronization barrier. Threat model accepts this branch as best-effort (T-26-07-E mitigate: bounded retry covers the fast crash-restart case; 1h TTL bounds the window).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking Pre-existing] Removed 4 dead `# type: ignore[import-not-found]` comments in `src/phaze/services/agent_client.py`**
- **Found during:** Task 1 commit pre-commit hook (mypy)
- **Issue:** Plan 26-02 placed deliberate self-deleting tripwires (per their inline comment) that fail mypy once Plan 26-03 schema modules land. Plan 26-03 merged in Wave 2 already, but the cleanup was never executed; the dead ignores blocked every subsequent plan's commits.
- **Fix:** Surgical removal of the 4 ignore-comments and the now-stale comment block explaining the tripwire mechanism. No semantic change to imports.
- **Files modified:** `src/phaze/services/agent_client.py`
- **Commit:** `5fe9561` (chore commit, separated from plan content per scope boundary rule)

**2. [Rule 1 - Bug] `mypy` complained `FromClause has no attribute "update"`**
- **Found during:** Task 2 verify step (`uv run mypy src/phaze/routers/agent_tracklists.py`)
- **Issue:** Initial draft used `Tracklist.__table__.update()...` (lifted from old `scan.py` patterns). mypy sees `__table__` as `FromClause` (the abstract parent) rather than `Table`; `FromClause` doesn't expose `.update()`.
- **Fix:** Switched to `sqlalchemy.update(Tracklist).where(...).values(...)` -- the standard 2.0 imperative form, type-clean and equivalent semantically.
- **Commit:** `3c1dea1`

### Deferred Issues (out-of-scope per scope-boundary rule)

**Pre-existing test isolation regression in full-suite runs.** Running `uv run pytest tests/test_routers/` produces ~131 errors of the form `UniqueViolationError: Key (id)=(legacy-application-server) already exists`. The same tests pass in small groups and individually. Root cause: the shared test Postgres DB + the `legacy-application-server` seed in `async_engine` fixture colliding under sequential test reuse. Not introduced by Plan 26-07 (occurs against unrelated routers like `test_tracklists.py`, `test_proposals.py`, etc.). Logged in `.planning/phases/26-task-code-reorg-http-backed-agent-worker/deferred-items.md`. Suggested fixes: per-test transaction rollback, per-test DB schema, or seed the legacy agent with `INSERT ... ON CONFLICT DO NOTHING`.

## Self-Check: PASSED

- `[FOUND]` `src/phaze/routers/agent_tracklists.py` (168 lines)
- `[FOUND]` `tests/test_routers/test_agent_tracklists.py` (279 lines, 7 tests)
- `[FOUND]` commit `5fe9561` (chore: dead type-ignore removal)
- `[FOUND]` commit `cd54715` (test RED)
- `[FOUND]` commit `3c1dea1` (feat GREEN)
- `[VERIFIED]` 7/7 integration tests pass: `uv run pytest tests/test_routers/test_agent_tracklists.py -x -q --no-cov -m integration`
- `[VERIFIED]` mypy clean on router: `uv run mypy src/phaze/routers/agent_tracklists.py`
- `[VERIFIED]` ruff clean on both files
- `[VERIFIED]` ruff format clean on both files
- `[VERIFIED]` 88.24% coverage on the router (above 85% bar)
