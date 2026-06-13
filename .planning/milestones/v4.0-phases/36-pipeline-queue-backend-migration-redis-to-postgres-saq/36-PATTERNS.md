<!-- GSD:PATTERNS -->
# Phase 36: Pipeline Queue Backend Migration (Redis to Postgres SAQ) - Pattern Map

**Mapped:** 2026-06-12
**Files analyzed:** 11 (4 modified construction sites + 3 cache-redis readers + 1 new factory + config + 3 new/updated test files)
**Analogs found:** 11 / 11 (every modified file is its own best self-analog; every new file has a strong existing analog)

> This is a backend-swap phase, not a greenfield feature. For the four queue-construction
> sites and the three `queue.redis` readers, the closest analog **is the file itself** —
> the planner edits the existing call in place rather than copying a sibling. The genuinely
> new artifacts are `queue_factory.py` and the three test files; their analogs are named
> explicitly below.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/tasks/_shared/queue_factory.py` **(NEW)** | utility (factory) | event-driven (enqueue) | `src/phaze/tasks/_shared/queue_defaults.py` + `agent_task_router.py:80-107` | role-match (both register the same hooks on a `Queue`) |
| `src/phaze/tasks/controller.py:134` | config (SAQ settings module) | event-driven | itself (in-place swap) | exact |
| `src/phaze/tasks/agent_worker.py:180` | config (SAQ settings module) | event-driven | itself (in-place swap) | exact |
| `src/phaze/main.py:101` | provider (FastAPI lifespan) | event-driven | itself (in-place swap) | exact |
| `src/phaze/services/agent_task_router.py:86` | service (per-agent enqueuer) | event-driven | itself (in-place swap) | exact |
| `src/phaze/tasks/proposal.py:66` | task (SAQ function) | request-response (LLM) | itself — repoint `ctx["queue"].redis`→`ctx["redis"]` | exact (HARD BREAK fix) |
| `src/phaze/tasks/_shared/deterministic_key.py:109,130` | utility (before/after hooks) | event-driven | itself — repoint `getattr(job.queue,"redis",None)` | exact (soft regression fix) |
| `src/phaze/routers/pipeline.py:83` | route (dashboard) | request-response | itself — drop dead `controller_queue.redis` fallback | exact |
| `src/phaze/config.py` (+`queue_url` field) | config (pydantic-settings) | config | `config.py:143-156` (`database_url`/`redis_url` Field) | exact |
| `tests/test_queue_factory.py` **(NEW)** | test (unit) | event-driven | `tests/test_web/test_saq_mount.py` + `tests/test_deterministic_key.py` | role-match |
| `tests/integration/test_pg_queue_priority.py` **(NEW)** | test (integration, real PG) | event-driven | `tests/test_migrations/test_migration_018.py` (+ conftest) | role-match |
| `tests/integration/test_pg_dedup.py` **(NEW)** | test (integration, real PG) | event-driven | `tests/test_migrations/test_migration_018.py` + `tests/test_queue_fakes_dedup.py` | role-match |
| `tests/_queue_fakes.py` (update `FakeRedis`/`.redis`) | test (fixtures) | — | itself | exact |
| `tests/test_task_split.py` (add `PHAZE_QUEUE_URL` env) | test (subprocess boundary) | — | itself | exact |

## Pattern Assignments

### `src/phaze/tasks/_shared/queue_factory.py` (NEW — utility/factory)

**Analogs:** `src/phaze/tasks/_shared/queue_defaults.py` (module shape + hook docstring conventions) and `src/phaze/services/agent_task_router.py:80-107` (the canonical "construct a Queue, then register BOTH hooks" block, repeated verbatim at all four sites today).

**The hook-registration pattern to centralize** (currently duplicated at `controller.py:140-142`, `agent_worker.py:186-190`, `main.py:104-108`, `agent_task_router.py:100-105`):
```python
queue.register_before_enqueue(apply_project_job_defaults)   # Phase 27 UAT Gap 1
queue.register_before_enqueue(apply_deterministic_key)      # Phase 35 (D-05)
```

**Recommended factory body** (from RESEARCH §"Single queue factory", lines 259-271 — swap `Queue`→`PostgresQueue`, add conservative pool sizing per Pitfall 4):
```python
# src/phaze/tasks/_shared/queue_factory.py
from saq.queue.postgres import PostgresQueue
from phaze.tasks._shared.deterministic_key import apply_deterministic_key
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults

def build_pipeline_queue(name: str, url: str, *, min_size: int = 1, max_size: int = 4) -> PostgresQueue:
    q = PostgresQueue.from_url(url, name=name, min_size=min_size, max_size=max_size)
    q.register_before_enqueue(apply_project_job_defaults)
    q.register_before_enqueue(apply_deterministic_key)
    return q
```
**Import note:** `from saq.queue.postgres import PostgresQueue` — NOT `from saq import Queue`. The top-level `from saq import Queue` import at `controller.py:26`, `agent_worker.py:48`, `agent_task_router.py:24`, `main.py:10` must change (or coexist for `CronJob`).

**Module conventions to copy from `queue_defaults.py`:** `from __future__ import annotations` (line 38), `import structlog` + `logger = structlog.get_logger(__name__)` (lines 42/51), `TYPE_CHECKING`-guarded `from saq import Job` (lines 47-48), explicit `__all__` (line 88).

---

### `src/phaze/tasks/controller.py:134` (config — SAQ control settings)

**Analog:** itself. In-place swap.

**Current construction** (lines 132-142):
```python
queue = Queue.from_url(get_settings().redis_url, name="controller")
queue.register_before_enqueue(apply_project_job_defaults)
queue.register_before_enqueue(apply_deterministic_key)
```
**Becomes:** `queue = build_pipeline_queue("controller", get_settings().queue_url, min_size=2, max_size=8)` (control consume pool — slightly larger than per-agent).

**Decouple cache-redis (Pitfall 1+2 fix).** Today the startup hook stashes the queue itself as the cache handle:
- `ctx["queue"] = queue` (line 91) — was the LLM-rpm Redis source via `.redis`.
- `ctx["task_router"] = AgentTaskRouter(cfg.redis_url)` (line 98) — stays on `redis_url`.

Add a dedicated cache client in `startup` mirroring the `discogs_client` create/close lifecycle (created line 73, closed `shutdown` line 121-123):
```python
import redis.asyncio as redis_async
ctx["redis"] = redis_async.Redis.from_url(cfg.redis_url)   # cache plane only
```
Close it in `shutdown` (lines 113-129) alongside `task_engine.dispose()` / `discogs_client.close()` / `task_router.close()`.

**Log-line note:** `logger.info("...redis=%s", cfg.redis_url)` (line 60) — the broker is now Postgres; update or add `queue=%s, cfg.queue_url` but **never log the full `queue_url`** (it carries DB creds — Security Domain V6). Mirror the token-preview discipline at `agent_worker.py:90`.

**Unchanged carry-over:** `after_process: increment_completed` (line 150), `cron_jobs` (160-170), reenqueue-on-boot try/except (106-110) — dedup contract verified identical on Postgres (RESEARCH Pitfall 3).

---

### `src/phaze/tasks/agent_worker.py:180` (config — SAQ agent settings)

**Analog:** itself. In-place swap.

**Current** (lines 174-190): `_queue_name` from `PHAZE_AGENT_QUEUE` env (keep the required-env guard at 175-179), then:
```python
queue = Queue.from_url(get_settings().redis_url, name=_queue_name)
queue.register_before_enqueue(apply_project_job_defaults)
queue.register_before_enqueue(apply_deterministic_key)
```
**Becomes:** `queue = build_pipeline_queue(_queue_name, get_settings().queue_url, min_size=1, max_size=4)`.

**Import-boundary contract (Pitfall 5/6 — CRITICAL):** the `open=False` PostgresQueue pool means **no connection at import** (preserved). But `tests/test_task_split.py` forbids `sqlalchemy.ext.asyncio` in the import graph — `psycopg`/`psycopg_pool` are NOT in the forbidden set, so the boundary still passes. The planner must verify `from saq.queue.postgres import PostgresQueue` does not transitively pull `sqlalchemy.ext.asyncio`.

**Decouple cache-redis:** the agent `startup` (lines 74-148) has NO `ctx["queue"]`/`ctx["redis"]` today — but the `after_process: increment_completed` hook (line 197) reads `job.queue.redis`. Add `ctx["redis"] = redis_async.Redis.from_url(cfg.redis_url)` in `startup`, close in `shutdown` (lines 151-167), and ensure the counter hook can reach it (see Shared Patterns → Cache-Redis decoupling for the chosen mechanism).

---

### `src/phaze/main.py:101` (provider — FastAPI lifespan)

**Analog:** itself. In-place swap.

**Current** (lines 98-108):
```python
_app.state.controller_queue = Queue.from_url(settings.redis_url, name="controller")
_app.state.controller_queue.register_before_enqueue(apply_project_job_defaults)
_app.state.controller_queue.register_before_enqueue(apply_deterministic_key)
```
**Becomes:** `_app.state.controller_queue = build_pipeline_queue("controller", settings.queue_url, min_size=2, max_size=8)`.

**Already-decoupled cache:** `_app.state.redis = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)` (line 113) is the independent client the research calls out as unaffected. Shutdown reverse-order (lines 149-153): `task_router.close()` → `redis.aclose()` → `controller_queue.disconnect()` → `engine.dispose()` — keep; `PostgresQueue.disconnect()` closes the psycopg3 pool.

**`/saq` mount carries over unchanged** (lines 141-146): `build_saq_app([controller_queue, *agent_queues])` reads `.info()`, which `PostgresQueue` implements (REQ-36-4). The `task_router.queue_for(agent.id)` per-agent queues (line 145) are now PostgresQueue instances via the same factory.

**Pool fan-out budget (Pitfall 4):** the API process holds `controller` + one PostgresQueue per non-revoked agent (line 145) + the task_router's per-agent cache. Each opens its own psycopg3 pool. With `min_size=1, max_size=4` per agent and N agents, budget total = control pool + Σ per-agent + SQLAlchemy engine pools ≤ Postgres `max_connections`. Document for the homelab prompt.

---

### `src/phaze/services/agent_task_router.py:86` (service — per-agent enqueuer)

**Analog:** itself. In-place swap, plus rename the constructor param.

**Current** (lines 65-107): `__init__(self, redis_url: str)` stores `self._redis_url`; `_queue_for` constructs:
```python
queue = Queue.from_url(self._redis_url, name=f"phaze-agent-{agent_id}")
queue.register_before_enqueue(apply_project_job_defaults)
queue.register_before_enqueue(apply_deterministic_key)
```
**Becomes:** rename `redis_url`→`queue_url` (and `self._redis_url`→`self._queue_url`); body → `queue = build_pipeline_queue(f"phaze-agent-{agent_id}", self._queue_url, min_size=1, max_size=4)`. Drop the duplicated hook registration (now inside the factory) — keep the per-`agent_id`-first-construction caching (line 85 `if agent_id not in self._queues`).

**Caller update:** the two `AgentTaskRouter(...)` constructions pass `redis_url` today — `controller.py:98` (`AgentTaskRouter(cfg.redis_url)`) and `main.py:110` (`AgentTaskRouter(redis_url=settings.redis_url)`) — both must pass `queue_url`/`settings.queue_url`.

**`close()` carries over** (lines 178-182): `await queue.disconnect()` closes the psycopg3 pool per cached queue; idempotent.

---

### `src/phaze/tasks/proposal.py:66` (task — HARD BREAK fix)

**Analog:** itself. One-line repoint.

**Current** (line 66): `await check_rate_limit(ctx["queue"].redis, settings.llm_max_rpm)`. `PostgresQueue` has no `.redis` → **unwrapped `AttributeError` crashes every `generate_proposals` job** (Pitfall 1).
**Becomes:** `await check_rate_limit(ctx["redis"], settings.llm_max_rpm)` — reads the dedicated cache client stashed in `controller.startup` (see that file's assignment). The `check_rate_limit` import (lines 14-20) and call signature are unchanged; only the handle source moves.

---

### `src/phaze/tasks/_shared/deterministic_key.py:109,130` (utility — soft regression fix)

**Analog:** itself. Repoint the best-effort Redis handle.

**Current** — both hooks read the queue's Redis (lines 108-114 enqueued, 129-135 completed):
```python
redis = getattr(job.queue, "redis", None)
if redis is not None:
    await incr_enqueued(redis, job.function)   # / incr_completed
```
On Postgres `getattr(...,"redis",None)` is `None` → counters silently stop (Pitfall 2). The try/except masks it.

**Fix (decide ONE mechanism, apply at all four construction sites):**
- **Option (a)** attach `queue.cache_redis` at construction (in `build_pipeline_queue`) so hooks read `getattr(job.queue, "cache_redis", None)` — minimal hook change, keeps the best-effort `getattr` shape.
- **Option (b)** thread a module/`ctx`-level redis client into the hook — `increment_completed` already has `ctx` (line 117), but `apply_deterministic_key` (line 86) only has `job`, so (a) is the lower-friction choice for the `before_enqueue` half.

Update the docstrings (lines 95-114 reference "`job.queue.redis`" explicitly) to the chosen handle. `_KEY_BUILDERS` (74-83) and the key format are UNCHANGED — dedup carries over.

---

### `src/phaze/routers/pipeline.py:83` (route — dead-fallback removal)

**Analog:** itself.

**Current** (lines 80-87): primary reads `app_state.redis` (the independent client, line 81); fallback `redis = app_state.controller_queue.redis` (line 83) is dead on Postgres.
**Becomes:** drop the `controller_queue.redis` fallback; `app_state.redis` is always present in production (set at `main.py:113`). The degrade-to-`{}` except (85-87) already covers the test-client-no-lifespan case. Update the docstring line 78 ("`controller_queue.redis` is the fallback handle").

---

### `src/phaze/config.py` (config — new `queue_url` field)

**Analog:** `config.py:143-156` — the `database_url` / `redis_url` `Field` + `validation_alias=AliasChoices(...)` pattern, and `SECRET_FILE_FIELDS` at line 79.

**Existing DSN field shape to copy** (lines 143-146):
```python
database_url: str = Field(
    default="postgresql+asyncpg://phaze:phaze@postgres:5432/phaze",
    validation_alias=AliasChoices("PHAZE_DATABASE_URL", "DATABASE_URL", "database_url"),
)
```
**New field** (RESEARCH Code Examples lines 245-256 — libpq DSN, NOT `+asyncpg`):
```python
queue_url: str = Field(
    default="postgresql://phaze:phaze@postgres:5432/phaze",
    validation_alias=AliasChoices("PHAZE_QUEUE_URL", "queue_url"),
    description="libpq DSN for the SAQ Postgres broker (psycopg3, NOT +asyncpg).",
)

@field_validator("queue_url", mode="before")
@classmethod
def _strip_sqlalchemy_driver(cls, v: str) -> str:
    return v.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")
```
**Place on `BaseSettings`** (both roles construct queues). `field_validator` import already present (line 18); follow the existing `@field_validator("audfprint_url", "panako_url")` precedent (line 204).

**SECRET_FILE_FIELDS** — `queue_url` carries DB creds. Add to the base frozenset (line 79):
```python
SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = frozenset({"database_url", "redis_url", "queue_url"})
```
The subclass unions (`ControlSettings`:302, `AgentSettings`:331) inherit it automatically.

**Production-guard analog (Open Q1):** `_enforce_redis_password_in_production` (lines 464-482) is the template if the planner adds a `queue_url`-credential guard — `@model_validator(mode="after")`, `urlparse`, raise `ValueError` on missing password when `agent_env == "production"`.

---

### `tests/test_queue_factory.py` (NEW — unit, REQ-36-1)

**Analogs:** `tests/test_web/test_saq_mount.py` (constructs queues, asserts on the built object with no Redis/DB) and `tests/test_deterministic_key.py` (drives the hooks with `FakeRedis`).

**Pattern to copy from `test_saq_mount.py`:** the no-lifespan, no-pool unit shape — assert `build_pipeline_queue(...)` returns a `PostgresQueue`, has the two `before_enqueue` hooks registered, and constructs `open=False` (no connection). Use the construction-token / AST-inspection style `test_saq_mount.py` uses to prove "no pool opened" at import (it greps the source for construction tokens around lines 55+).

**Hook-presence assertion source:** SAQ stores callbacks on `queue._before_enqueues` (base `Queue`); assert both `apply_project_job_defaults` and `apply_deterministic_key` are registered. Mirror `test_deterministic_key.py`'s direct-hook-invocation style (it imports `_KEY_BUILDERS`, `apply_deterministic_key`, `increment_completed` and the `FakeRedis` double from `tests._queue_fakes`).

---

### `tests/integration/test_pg_queue_priority.py` (NEW — integration, REQ-36-2)

**Analog:** `tests/test_migrations/test_migration_018.py` + `tests/test_migrations/conftest.py` — the only existing real-Postgres-against-`localhost` test pattern.

**Patterns to copy:**
- Real-PG engine/connection from a dedicated test DSN. `test_migration_018.py:28-32` imports `MIGRATIONS_TEST_DATABASE_URL` + `create_async_engine` from `tests.test_migrations.conftest`; the global `tests/conftest.py:19` uses `TEST_DATABASE_URL = "postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test"`. For SAQ you need the **libpq** form (`postgresql://...`) for `PostgresQueue.from_url`.
- The `just test-db` / `just integration-test` recipes (justfile:68-74) spin an ephemeral Postgres on port **5433** (`PHAZE_TEST_DB_PORT`). Point the integration DSN at that.
- Auto-`integration` marker: `tests/conftest.py:109-122` auto-marks any test consuming a DB fixture OR under `test_migrations`. A new `tests/integration/` dir must be added to that path rule (or use a DB-backed fixture) so `pytest -m 'not integration'` still excludes it.

**Behavior:** enqueue jobs with differing `priority` against a real `PostgresQueue`; assert lower-`priority` integer dequeues first, and a future `scheduled` parks (RESEARCH Pattern 2, postgres.py:653-668).

---

### `tests/integration/test_pg_dedup.py` (NEW — integration, REQ-36-3)

**Analogs:** `tests/test_migrations/test_migration_018.py` (real-PG harness, same as above) + `tests/test_queue_fakes_dedup.py` / `tests/_queue_fakes.py:198-242` (`DedupFakeQueue` — the documented model of SAQ's dedup-no-op contract).

**Behavior:** against a real `PostgresQueue`, enqueue the same deterministic key (`process_file:<id>`) twice; assert the second returns `None` (in-flight ON CONFLICT no-op), then `finish`/complete and assert it enqueues again — exactly the contract `DedupFakeQueue` models (`_queue_fakes.py:223-242`) and that `reenqueue_discovered` counts as `skipped` (Pitfall 3).

---

### Test updates (not new files)

- **`tests/_queue_fakes.py`** — `FakeRedis` (line 56) and the `.redis`-attribute model on `DedupFakeQueue` (lines 56-76 docstring references `job.queue.redis`) must reflect the chosen cache-redis mechanism (Pitfall 2 decision: `.cache_redis` vs `ctx["redis"]`). `FakeQueue.info()` (lines 148-166) already matches `PostgresQueue.info()`'s shape — no change for REQ-36-4.
- **`tests/test_task_split.py`** — the subprocess env block (lines 50-60) sets `PHAZE_REDIS_URL`; add `PHAZE_QUEUE_URL` (or rely on the `queue_url` default). Keep asserting `sqlalchemy.ext.asyncio` is NOT imported (psycopg3 is allowed).
- **`tests/test_web/test_saq_mount.py`** — extend to assert the mount renders over a `PostgresQueue`'s `.info()` (REQ-36-4); currently uses `FakeQueue.info()`.

## Shared Patterns

### Cache-Redis decoupling (the real work of this phase)
**Source mechanisms:** dedicated `redis.asyncio.Redis.from_url(cfg.redis_url)` client.
**Apply to:** `controller.py` startup (add `ctx["redis"]`), `agent_worker.py` startup (add `ctx["redis"]`), `proposal.py:66` (read `ctx["redis"]`), `deterministic_key.py:109,130` (read `getattr(job.queue,"cache_redis",None)` or threaded handle), `pipeline.py:83` (drop `controller_queue.redis`).
**Reference (already-correct) client:** `main.py:113` `redis_async.Redis.from_url(settings.redis_url, decode_responses=True)`.
**Pick ONE handle mechanism** for the hooks (RESEARCH Open Q3) and apply it identically at all four construction sites and the test fakes.
```python
# decouple: cache stays on redis_url, broker moves to queue_url
import redis.asyncio as redis_async
ctx["redis"] = redis_async.Redis.from_url(cfg.redis_url)   # controller/agent startup
# close in shutdown, mirroring discogs_client (controller.py:121-123)
```

### Queue construction (single seam)
**Source:** new `build_pipeline_queue` in `queue_factory.py`.
**Apply to:** all four construction sites (`controller.py:134`, `agent_worker.py:180`, `main.py:101`, `agent_task_router.py:86`). After this phase there is exactly one `PostgresQueue.from_url` call in the codebase — the seam Phase 37 extends for per-stage pause/priority.

### before_enqueue hooks (carry over unchanged)
**Source:** `queue_defaults.py:apply_project_job_defaults`, `deterministic_key.py:apply_deterministic_key`.
**Apply to:** every queue via the factory. Both hooks live on the backend-agnostic base `Queue`, so they register identically on `PostgresQueue` (RESEARCH Summary). Only the counter hook's Redis-handle read changes.

### Secret-bearing DSN settings
**Source:** `config.py:143-156` (`Field` + `AliasChoices`) and `config.py:79` (`SECRET_FILE_FIELDS`).
**Apply to:** the new `queue_url` field — `<VAR>_FILE` convention, never logged (Security Domain V6, mirrors `redis_url`/`database_url`).

### Real-Postgres integration test harness
**Source:** `tests/test_migrations/conftest.py` (`MIGRATIONS_TEST_DATABASE_URL`, engine helpers) + `tests/conftest.py:109-122` (auto-`integration` marker) + `justfile:68-74` (`just test-db` ephemeral PG on 5433).
**Apply to:** both new `tests/integration/test_pg_*.py` files. Register `tests/integration/` in the auto-marker path rule so `pytest -m 'not integration'` stays green offline.

## No Analog Found

None. Every modified file is its own in-place analog, and all three new files have strong existing analogs (saq_mount/deterministic_key tests for the unit factory test; migration-018 + queue-fakes-dedup for the PG integration tests). The factory itself reuses the exact hook-registration block duplicated at four current sites.

## Metadata

**Analog search scope:** `src/phaze/tasks/`, `src/phaze/tasks/_shared/`, `src/phaze/services/`, `src/phaze/routers/`, `src/phaze/web/`, `src/phaze/config.py`, `src/phaze/main.py`, `tests/`, `tests/test_migrations/`, `justfile`
**Files scanned:** controller.py, agent_worker.py, agent_task_router.py, main.py, proposal.py, deterministic_key.py, queue_defaults.py, pipeline.py, config.py, _queue_fakes.py, test_deterministic_key.py, test_task_split.py, test_saq_mount.py, conftest.py, test_migration_018.py
**Pattern extraction date:** 2026-06-12
