<!-- GSD:RESEARCH -->
# Phase 36: Pipeline Queue Backend Migration (Redis to Postgres SAQ) - Research

**Researched:** 2026-06-12
**Domain:** SAQ task-queue backend swap (Redis → Postgres), psycopg3 pooling, deterministic-key dedup carryover
**Confidence:** HIGH (all critical claims verified against installed `saq==0.26.4` source + project code)

## Summary

Phase 36 swaps the SAQ broker from Redis to Postgres so SAQ's Postgres-only native `priority` + `scheduled` dequeue ordering becomes available for Phases 37–38. The mechanical swap (`saq[redis]`→`saq[postgres]`, `Queue.from_url`→`PostgresQueue.from_url`, new `PHAZE_QUEUE_URL`) is small and the two before-enqueue hooks carry over unchanged because they live on the backend-agnostic base `Queue`. **The dedup that Phase 32/35 depend on carries over cleanly** — verified in `saq/queue/postgres.py::_enqueue` (`ON CONFLICT (key) DO UPDATE ... WHERE status IN ('aborted','complete','failed') AND new.scheduled > old.scheduled RETURNING 1`), which returns `None` for an in-flight duplicate key exactly like the Redis ZSCORE/incomplete-set check. The reenqueue `job is None → skipped` accounting is unaffected.

**The single highest-risk surface is not the queue swap itself — it is the code that reaches into `queue.redis`.** `PostgresQueue` has no `.redis` attribute. Three call sites read it today: (1) `tasks/proposal.py:66` `check_rate_limit(ctx["queue"].redis, ...)` — **unwrapped, hard `AttributeError` that crashes `generate_proposals`**; (2) the Phase-35 counter hooks in `_shared/deterministic_key.py` read `getattr(job.queue, "redis", None)` → silently `None` → enqueued/completed counters **stop incrementing** (soft observability regression); (3) `routers/pipeline.py:83` fallback `controller_queue.redis` (primary path uses the independent `app.state.redis`, so this only loses a dead fallback). Redis stays running as a cache, and `app.state.redis` is already an independent client — so the fix is to give the worker contexts and the counter hooks a dedicated Redis handle instead of borrowing the queue's.

**Primary recommendation:** Treat this as two coupled changes, not one. (A) Mechanical: swap the dependency + all four `Queue.from_url` sites to `PostgresQueue.from_url`, add `PHAZE_QUEUE_URL` as a **raw libpq DSN** (NOT `postgresql+asyncpg://`). (B) Decouple cache-Redis from queue-Redis: stash a dedicated `ctx["redis"]` in both worker startups and repoint `proposal.py` + the counter hooks at it. Land (B) in the same PR or the LLM-proposal path and Phase-35 counters break the moment the queue stops being Redis-backed.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Job broker (enqueue/dequeue, dedup, priority, scheduled park) | Database (Postgres `saq_jobs`) | — | Migration target; SAQ runs its own psycopg3 pool against Postgres |
| LLM rate-limit counter (`phaze:llm:rpm`) | Cache (Redis) | — | Stays on Redis; must stop borrowing `queue.redis` |
| Pipeline per-function counters (`phaze:pipeline:*`) | Cache (Redis) | DB reconcile (truth) | Stays on Redis; D-03 says DB is truth, counters are a cache |
| Tracklists idempotency cache, execution SSE | Cache (Redis via `app.state.redis`) | — | Already an independent Redis client — unaffected |
| SAQ `/saq` monitoring dashboard | API (FastAPI) | DB (via `PostgresQueue.info()`) | Reads `q.info()`, backend-agnostic |
| Reboot re-enqueue resilience | API/Control worker | DB (Postgres queue) | Dedup semantics carry over; routing logic unchanged |

## User Constraints (from CONTEXT.md)

No `36-CONTEXT.md` exists yet (research is standalone, ahead of discuss-phase). Constraints below are drawn from the ROADMAP scope and project CLAUDE.md, and must be treated as locked inputs by the planner:

### Locked Decisions (from ROADMAP Phase 36 scope + STATE)
- Swap dependency `saq[redis]` → `saq[postgres]` (pulls `psycopg[pool]>=3.2.0`).
- SAQ runs its **own** psycopg3 async pool, separate from the SQLAlchemy/asyncpg engine. SAQ auto-manages `saq_jobs`.
- New setting `PHAZE_QUEUE_URL` (Postgres DSN, defaults derived from existing Postgres config).
- `controller.py` + `agent_worker.py` build `PostgresQueue.from_url(...)`.
- Redis container **stays** for cache/rate-limiting only — no longer the broker.
- Carry over both before-enqueue hooks unchanged (`queue_defaults`, `deterministic_key`).
- Scope is the queue substrate only; per-stage pause/priority controls are Phases 37–38.
- Deliverable Step D (homelab change-prompt) is produced in **planning**, not research.

### Project Constraints (from CLAUDE.md)
- Python 3.14 exclusively; `uv` only — never bare `pip`/`python`/`pytest`/`mypy`, always `uv run`.
- 85% min coverage, Codecov with service flags.
- Pre-commit must pass (frozen SHAs); never `--no-verify`.
- PR per phase, worktree branch, no direct main commits.
- Update affected service READMEs + `scripts/update-project.sh` alongside code.
- mypy strict (excludes tests/); ruff line length 150, `target-version = py313`.

### Deferred / Out of Scope
- Per-stage pause (drain) + priority API/table/hooks → Phase 37.
- DAG pause/priority UI + Rescan-button removal → Phase 38.
- Any cross-file-server fingerprint matching (XAGENT-01, permanently deferred).

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-36-1 | Queue backend on Postgres | `PostgresQueue.from_url` at all 4 construction sites; `saq[postgres]` dep; `PHAZE_QUEUE_URL` setting (Standard Stack, Code Examples) |
| REQ-36-2 | Native priority + scheduled-park available | Verified `saq/queue/postgres.py:653-662` dequeue `WHERE now>=scheduled AND priority BETWEEN .. ORDER BY priority, scheduled`; `_enqueue` writes `priority`+`scheduled` columns (Pattern 2) |
| REQ-36-3 | No regression in reboot re-enqueue (Phase 32) | Dedup ON CONFLICT returns `None` for in-flight dup ⇒ `skipped` accounting intact (Pitfall 3, Regression Surfaces) |
| REQ-36-4 | No regression in SAQ `/saq` UI (Phase 33) | `saq_web` reads `q.info()`; `PostgresQueue.info()` present (postgres.py:245) — backend-agnostic (Regression Surfaces) |
| REQ-36-5 | No regression in determinism/idempotency (Phase 35) | Deterministic-key dedup carries over; **but** counter hooks lose `queue.redis` — must repoint to dedicated Redis (Pitfall 1, Don't Hand-Roll) |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| saq[postgres] | >=0.26.4 | Async task queue on Postgres backend | Already the project queue (migrated from arq). Postgres extra adds native priority+scheduled. `[VERIFIED: installed saq 0.26.4]` |
| psycopg[pool] | >=3.2.0 | psycopg3 async driver + `AsyncConnectionPool` for the SAQ Postgres backend | Pulled by `saq[postgres]` extra. `[VERIFIED: saq Requires-Dist `psycopg[pool]>=3.2.0; extra=="postgres"`]` |

**Verified extra resolution** (`importlib.metadata` on installed saq):
- `psycopg[pool]>=3.2.0; extra == "postgres"`  `[VERIFIED: installed metadata]`
- `redis<8.0,>=4.2; extra == "redis"` (current) — kept for cache via the separate `redis` client, but the **queue extra** moves to postgres.

### Supporting (unchanged, stays for cache)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| redis (async) | existing | `app.state.redis`, LLM rpm cache, pipeline counters | Stays. Decouple from queue. `[VERIFIED: main.py:113 `redis_async.Redis.from_url`]` |
| asyncpg | >=0.31.0 | SQLAlchemy ORM engine (NOT the queue) | Unchanged — separate pool from SAQ's psycopg3 pool. `[VERIFIED: pyproject]` |

### Dependency change (pyproject.toml line 9)
```diff
- "saq[redis]>=0.26.4",
+ "saq[postgres]>=0.26.4",
+ "redis>=4.2,<8.0",   # keep an explicit redis dep — it's no longer pulled by saq[postgres]
```
**Critical:** dropping `saq[redis]` removes the `redis` transitive dependency. The project still imports `redis.asyncio` directly (`main.py`, `routers/*`, `services/proposal.py`). **Add `redis` as a first-class dependency** or those imports break at runtime even though Redis the service still runs.

**Installation:**
```bash
uv add 'saq[postgres]>=0.26.4'
uv add 'redis>=4.2,<8.0'     # explicit; previously transitive via saq[redis]
uv remove saq                # then re-add to drop the [redis] extra cleanly, OR edit pyproject directly
uv sync
```
**Version verification:**
```bash
uv run python -c "import saq, psycopg, psycopg_pool; print(saq.__version__)"
pip index versions psycopg     # confirm psycopg 3.2.x present
```

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| saq | PyPI | 5+ yrs | high | github.com/tobymao/saq | not run (offline) | Approved — already a project dep, installed 0.26.4 |
| psycopg | PyPI | mature | very high | github.com/psycopg/psycopg | not run | Approved — declared by saq[postgres] extra, authoritative |
| psycopg-pool | PyPI | mature | high | github.com/psycopg/psycopg | not run | Approved — `[pool]` extra of psycopg |
| redis | PyPI | mature | very high | github.com/redis/redis-py | not run | Approved — already imported across codebase |

**Packages removed due to slopcheck [SLOP] verdict:** none.
**Packages flagged as suspicious [SUS]:** none.

*slopcheck was not run in this session (no network install). All four packages are already in the resolved dependency tree (`saq`/`redis` installed; `psycopg`/`psycopg_pool` declared by the official `saq[postgres]` extra), so they are authoritative-source-discovered, not training-suggested. Planner may still gate the install behind a `uv sync` verification task.*

## Architecture Patterns

### System Architecture Diagram

```
BEFORE (Redis broker):
  API enqueue ─┐
  Control wkr ─┼─► Queue.from_url(redis_url) ──► REDIS ◄── Agent worker dequeue
  Agent router┘        │ .redis ──────────────► (also serves rpm cache + counters + app.state.redis)

AFTER (Postgres broker):
  API enqueue ─┐
  Control wkr ─┼─► PostgresQueue.from_url(PHAZE_QUEUE_URL) ──► POSTGRES (saq_jobs/saq_stats/saq_versions)
  Agent router┘        │  (psycopg3 pool, separate from SQLAlchemy/asyncpg engine)        ▲
                       │                                                                  │
                       └─ NO .redis attribute ── X                          Agent worker dequeue
                                                                            (LISTEN/NOTIFY saq:<name>)
  Cache plane (UNCHANGED, still Redis):
    app.state.redis ─► REDIS ◄─ rpm cache (proposal.py) ◄─ pipeline counters (deterministic_key hooks)
                                  ▲                            ▲
                                  └── MUST be repointed here ──┘  (were reading queue.redis)
```

### Recommended approach: one queue factory
Introduce a single `phaze.tasks._shared.queue_factory.build_pipeline_queue(name, settings)` that returns a `PostgresQueue.from_url(settings.queue_url, name=name, min_size=..., max_size=...)` with the two hooks registered. All four current `Queue.from_url` sites then call it, so the backend choice and pool sizing live in exactly one place. This also gives Phase 37 a single seam to extend.

### Construction sites that MUST change (all four)
| File:line | Current | Becomes |
|-----------|---------|---------|
| `tasks/controller.py:134` | `Queue.from_url(get_settings().redis_url, name="controller")` | `PostgresQueue.from_url(get_settings().queue_url, name="controller", ...)` |
| `tasks/agent_worker.py:180` | `Queue.from_url(get_settings().redis_url, name=_queue_name)` | `PostgresQueue.from_url(get_settings().queue_url, name=_queue_name, ...)` |
| `main.py:101` | `Queue.from_url(settings.redis_url, name="controller")` (app.state.controller_queue) | `PostgresQueue.from_url(settings.queue_url, name="controller", ...)` |
| `services/agent_task_router.py:86` | `Queue.from_url(self._redis_url, name=f"phaze-agent-{agent_id}")` | per-agent `PostgresQueue.from_url(self._queue_url, name=...)` |

`saq_mount.build_saq_app` needs no change — it only wraps the instances it's handed (`[controller_queue, *agent_queues]`).

### Pattern 1: PostgresQueue construction + first-boot DDL
**What:** `PostgresQueue.from_url(url, **kwargs)` → `__init__(url=..., name=, jobs_table="saq_jobs", stats_table="saq_stats", versions_table="saq_versions", min_size=4, max_size=20, saq_lock_keyspace=0, priorities=(0,32767))`. The pool is created with `open=False`; **no connection at import time** (important — preserves the agent_worker module-import contract). On first `connect()`, `init_db()` takes `pg_try_advisory_lock(keyspace,0)`, then `CREATE TABLE IF NOT EXISTS saq_versions/saq_jobs/saq_stats` + indexes, idempotently.
**When to use:** every queue construction.
**DB permission requirement:** the Postgres role must be able to `CREATE TABLE`/`CREATE INDEX` in its schema and use `LISTEN`/`NOTIFY` (channel `saq:<name>`). The existing `phaze` role owns its database, so this is satisfied in dev/compose; flag it explicitly for the homelab prompt. `[VERIFIED: saq/queue/postgres.py:152-227, postgres_migrations.py]`
```python
# Source: saq/queue/postgres.py (installed 0.26.4)
async def connect(self) -> None:
    if self._connected: return
    if self._manage_pool_lifecycle:
        await self.pool.open()
        await self.pool.resize(min_size=self.min_size, max_size=self.max_size)
    await self.init_db()          # CREATE TABLE IF NOT EXISTS saq_jobs/... under advisory lock
    await super().connect()
    self._connected = True
```

### Pattern 2: native priority + scheduled (the whole point)
**What:** Postgres dequeue is `... WHERE status='queued' AND queue=%(queue)s AND now>=scheduled AND priority BETWEEN plow AND phigh ORDER BY priority, scheduled ... FOR UPDATE SKIP LOCKED`. Lower `priority` integer = dequeued sooner. `scheduled` in the future parks a job (the Phase-37 "drain/pause via scheduled=SENTINEL" mechanism). `[VERIFIED: postgres.py:653-668]`
**When to use:** Phases 37–38 will set `priority`/`scheduled` via `UPDATE saq_jobs`. Phase 36 only needs to make the columns exist and be honored — which they are by construction.

### Anti-Patterns to Avoid
- **Passing a SQLAlchemy DSN to PostgresQueue:** `postgresql+asyncpg://` is a SQLAlchemy dialect string. psycopg3's `AsyncConnectionPool` needs a **raw libpq DSN** (`postgresql://user:pass@host:port/db`). Passing the `+asyncpg` form will fail to connect. Derive `PHAZE_QUEUE_URL` by stripping the `+asyncpg` driver suffix from `database_url`, or set it explicitly.
- **Borrowing `queue.redis` for the cache plane:** dead on Postgres. Use a dedicated Redis client.
- **Calling `build_saq_app`/`saq_web` more than once:** unchanged hazard (clobbers module-global registry) — already documented in `saq_mount.py`.
- **Letting each per-agent queue open a 20-connection pool:** with N agents + control + API, `max_size=20` per queue can exhaust Postgres `max_connections`. Size pools down (see Pitfall 4).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Job dedup on Postgres | Custom "is this key already queued" SELECT | SAQ's `ON CONFLICT (key) DO UPDATE ... WHERE status IN terminal RETURNING 1` | Already atomic + race-safe; returns `None` for in-flight dup, matching the Redis contract the reenqueue path relies on |
| `saq_jobs` schema / migrations | Hand-written Alembic migration for the queue table | SAQ `init_db()` auto-DDL under advisory lock | SAQ owns its schema + version table; an Alembic table would collide |
| psycopg3 pool wiring | New `AsyncConnectionPool` per call site | `PostgresQueue.from_url(url, min_size=, max_size=)` | SAQ manages open/resize/lifecycle and enforces `autocommit=True` |
| Reaching Redis from a `before_enqueue` hook | `job.queue.redis` | A module/`ctx`-level dedicated `redis.asyncio` client | Decouples cache from broker; survives backend swap |

**Key insight:** the queue and the cache were the *same* Redis only by accident of the Redis backend. The migration's real work is finishing that decoupling, not the `from_url` swap.

## Runtime State Inventory

This is a backend/infra migration with live runtime state implications. All five categories answered explicitly.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | Existing in-flight jobs live in **Redis** (the old broker). After cutover the new broker is empty `saq_jobs` in Postgres. The 2026-06 incidents show ~11k `process_file` jobs can be live on `phaze-agent-nox`. | Operational drain/cutover decision (planning): either drain Redis queue to empty before deploy, OR accept that Phase 32 boot re-enqueue + cron repopulate `saq_jobs` from DB-truth (DISCOVERED files). The reenqueue path makes a cold empty queue self-healing — **no data migration of jobs needed**, but the cutover must be sequenced. |
| **Live service config** | Homelab control + agent compose services pass `PHAZE_REDIS_URL` as the broker. Agents (`datum@nox`, `datum@lux`) currently need Redis reachability for the queue. | Homelab prompt (Step D, planning): add `PHAZE_QUEUE_URL` to control + agent services; agents must now reach **Postgres** over the network (new firewall/topology requirement — see Open Q1). |
| **OS-registered state** | None — no systemd/Task Scheduler entries embed the broker URL. | None — verified by grep (broker URL only in env/config). |
| **Secrets/env vars** | `PHAZE_REDIS_URL` (stays, now cache-only). New `PHAZE_QUEUE_URL` carries Postgres credentials → belongs in `SECRET_FILE_FIELDS` (`<VAR>_FILE` convention) like `database_url`/`redis_url`. | Add `PHAZE_QUEUE_URL` to `BaseSettings.SECRET_FILE_FIELDS`; document in `.env.example`. |
| **Build artifacts / installed packages** | `saq[redis]` resolved `redis` transitively; `saq[postgres]` does not. `psycopg`/`psycopg_pool` not yet installed. | `uv sync` after pyproject edit; add explicit `redis` dep so direct `redis.asyncio` imports keep resolving. Docker image rebuild needed (psycopg3 is a pure-ish wheel but confirm it builds on the slim base — see Environment Availability). |

**The canonical question:** after every file is updated, what still points at Redis as a broker? Answer: the homelab compose env (`PHAZE_REDIS_URL` used as broker) and any operator runbook — both handled in the Step D prompt. In code, the four `from_url` sites + the three `queue.redis` cache reads are the complete surface.

## Common Pitfalls

### Pitfall 1: `queue.redis` AttributeError crashes LLM proposals (HARD BREAK)
**What goes wrong:** `tasks/proposal.py:66` calls `check_rate_limit(ctx["queue"].redis, settings.llm_max_rpm)`. `PostgresQueue` has no `.redis`. This is **not** wrapped in try/except → `AttributeError` → every `generate_proposals` job fails.
**Why it happens:** the controller startup stashes the queue as `ctx["queue"]` and proposal code borrowed its Redis client.
**How to avoid:** in `controller.startup`, construct a dedicated `ctx["redis"] = redis_async.Redis.from_url(cfg.redis_url)` and change `proposal.py` to read `ctx["redis"]`. Close it in `shutdown`.
**Warning signs:** `generate_proposals` 100% failure post-deploy; `AttributeError: 'PostgresQueue' object has no attribute 'redis'`.

### Pitfall 2: Phase-35 counters silently stop (SOFT regression)
**What goes wrong:** `_shared/deterministic_key.py` `apply_deterministic_key` + `increment_completed` read `getattr(job.queue, "redis", None)`. On Postgres that's `None`, so the `enqueued`/`completed` INCRs are skipped (best-effort, only a warning log). The dashboard's counter backstop goes stale — DB-truth still renders, but D-02's maintained counters silently die.
**Why it happens:** same root cause as Pitfall 1, but masked by the best-effort try/except.
**How to avoid:** give the hooks a Redis handle not derived from the queue. Cleanest: pass a module-level/`ctx` redis client into the counter functions, or attach a `.cache_redis` to the queue instances at construction so the hooks can read `getattr(job.queue, "cache_redis", None)`. Decide one mechanism and apply at all construction sites (controller, agent_worker, main.py, agent_task_router).
**Warning signs:** `pipeline enqueued-counter increment failed` warnings; counters frozen at pre-deploy values while DB-truth `done` keeps moving.

### Pitfall 3: dedup/reenqueue accounting (verified NOT a regression — but verify in tests)
**What goes wrong (feared):** that the Postgres dedup behaves differently and the reenqueue `skipped` count or Phase-32 resilience changes.
**Reality:** `_enqueue` returns `None` for an in-flight duplicate key (ON CONFLICT WHERE status IN terminal fails to update). `reenqueue_discovered` counts `job is None → skipped`. Identical contract to Redis. `[VERIFIED: postgres.py:700-755 vs redis.py _enqueue]`
**How to avoid regressions:** add/keep an integration test that enqueues the same `process_file:<id>` key twice against a real Postgres and asserts the second returns `None`.

### Pitfall 4: connection-pool fan-out exhausts Postgres
**What goes wrong:** each `PostgresQueue` opens its own psycopg3 pool (default `min_size=4, max_size=20`). The API process holds `controller` + one queue per non-revoked agent (for the `/saq` mount) + the task_router caches one per agent. With several agents this multiplies into dozens-to-hundreds of Postgres connections, colliding with the SQLAlchemy engine's own pool (`pool_size=10, max_overflow=5` in controller; API engine separately) against Postgres `max_connections` (often 100).
**How to avoid:** set conservative `min_size`/`max_size` per queue in the single factory (e.g. `min_size=1, max_size=4` for per-agent/monitoring queues; modest for the worker's own consume queue). Budget total = (worker consume pool) + (API control pool) + (Σ per-agent pools) + (SQLAlchemy engine pools) ≤ Postgres `max_connections`. Document the budget for the homelab prompt.
**Warning signs:** `psycopg.pool.PoolTimeout`, `FATAL: too many connections for role`.

### Pitfall 5: agent worker now requires Postgres reachability (architecture shift)
**What goes wrong:** Phase 26 D-25 designed the agent role to run with **no Postgres reachability** (import-boundary test forbids `sqlalchemy.ext.asyncio`/`phaze.database`). Moving the broker to Postgres means the agent's psycopg3 queue pool **must** reach Postgres over the network. The import-boundary test still passes (psycopg3 isn't in the forbidden set), but the network/security topology changes: file-server agents now open a Postgres connection to the control host.
**How to avoid:** this is an accepted, intentional consequence — but it must be called out in the homelab prompt (firewall: agents→Postgres:5432) and in agent docs. Consider whether the production Redis-password guard (`_enforce_redis_password_in_production`) needs a Postgres-credential analog for the new `queue_url`.
**Warning signs:** agent worker hangs/fails at `connect()` with `psycopg.OperationalError` on hosts that could previously only see the API + Redis.

### Pitfall 6: import-time + test env must provide PHAZE_QUEUE_URL
**What goes wrong:** `tests/test_task_split.py` imports `phaze.tasks.agent_worker` in a subprocess with `PHAZE_REDIS_URL` set; the module builds the queue at import. With `PostgresQueue`, the pool is created `open=False` so **no connection** is attempted at import (safe), but the module still needs `PHAZE_QUEUE_URL` (or a default) to construct. The test env must set it, or `get_settings().queue_url` must default sanely.
**How to avoid:** give `queue_url` a default derived from `database_url` (strip `+asyncpg`); update the import-boundary test env and `tests/_queue_fakes.py` (the `FakeQueue.redis` attribute comment + any counter assertions) to the new cache-redis mechanism.
**Warning signs:** import-boundary test fails with missing-env or pool-construction error.

## Code Examples

### Deriving the psycopg3 DSN from the existing asyncpg DSN
```python
# Source: pattern for phaze.config — convert SQLAlchemy dialect DSN to libpq DSN
# database_url default: "postgresql+asyncpg://phaze:phaze@postgres:5432/phaze"
# psycopg3 needs:       "postgresql://phaze:phaze@postgres:5432/phaze"
queue_url: str = Field(
    default="postgresql://phaze:phaze@postgres:5432/phaze",
    validation_alias=AliasChoices("PHAZE_QUEUE_URL", "queue_url"),
    description="libpq DSN for the SAQ Postgres broker (psycopg3, NOT +asyncpg).",
)

@field_validator("queue_url", mode="before")
@classmethod
def _strip_sqlalchemy_driver(cls, v: str) -> str:
    # Accept a +asyncpg/+psycopg form and normalize to a libpq DSN for psycopg3.
    return v.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")
```
And add `"queue_url"` to `BaseSettings.SECRET_FILE_FIELDS` (it carries credentials).

### Single queue factory (recommended)
```python
# Source: new phaze/tasks/_shared/queue_factory.py
from saq.queue.postgres import PostgresQueue
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults
from phaze.tasks._shared.deterministic_key import apply_deterministic_key

def build_pipeline_queue(name: str, url: str, *, min_size: int = 1, max_size: int = 4) -> PostgresQueue:
    q = PostgresQueue.from_url(url, name=name, min_size=min_size, max_size=max_size)
    q.register_before_enqueue(apply_project_job_defaults)
    q.register_before_enqueue(apply_deterministic_key)
    return q
```

### Verified dedup contract (no code change — for the regression test)
```python
# Source: saq/queue/postgres.py:_enqueue (installed 0.26.4)
# ON CONFLICT (key) DO UPDATE ... WHERE status IN ('aborted','complete','failed')
#                                   AND %(scheduled)s > saq_jobs.scheduled RETURNING 1
# -> in-flight (queued/active) duplicate key: no row updated -> fetchone() None -> returns None
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| arq | SAQ (Redis) | pre-Phase-30 (auto-memory) | Project queue standard |
| SAQ Redis broker | SAQ Postgres broker | Phase 36 | Unlocks native priority+scheduled for Phases 37–38 |
| Cache borrows `queue.redis` | Dedicated `redis.asyncio` client for cache plane | Phase 36 | Required by the backend swap |

**Deprecated/outdated:**
- Reading `queue.redis` for the LLM rpm cache and pipeline counters — must be removed; no equivalent on `PostgresQueue`.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `PHAZE_QUEUE_URL` should default by stripping `+asyncpg` from `database_url` (same DB, same creds) | Standard Stack / Code Examples | If the homelab wants a *separate* Postgres for the queue, default is wrong — confirm in discuss-phase |
| A2 | Operator accepts agents gaining Postgres network reachability (D-25 spirit relaxed) | Pitfall 5 | If unacceptable, the whole Postgres-broker approach for agents is blocked — must confirm before planning |
| A3 | Existing in-flight Redis jobs may be dropped at cutover (reenqueue self-heals from DB-truth) | Runtime State Inventory | If jobs must be preserved, a drain/migration step is needed — confirm cutover policy |
| A4 | `redis` must become an explicit dependency (dropped from `saq[postgres]`) | Standard Stack | If left transitive-only, direct `redis.asyncio` imports break at runtime |
| A5 | Conservative per-queue pool sizes (min 1 / max 4) fit Postgres `max_connections` | Pitfall 4 | Wrong sizing → connection exhaustion or under-throughput; tune against homelab Postgres config |

## Open Questions

1. **Agent → Postgres network reachability.** Phase 26 D-25 deliberately kept agents Postgres-free at the network level. Moving the broker to Postgres requires agents to open a psycopg3 connection to the control-host Postgres. Confirm the homelab firewall/topology allows it and whether a production credential guard (analogous to `_enforce_redis_password_in_production`) is wanted for `queue_url`.
   - Known: import-boundary test still passes (psycopg3 not forbidden).
   - Unclear: operator's intent on the network boundary.
   - Recommendation: surface as the top discuss-phase question.
2. **Cutover sequencing of live jobs.** ~11k jobs can be live on Redis at deploy time. Reenqueue makes a cold Postgres queue self-healing, but confirm whether to drain Redis first or rely on boot re-enqueue.
3. **Counter-Redis injection mechanism.** Two viable shapes (a) `ctx["redis"]` + change `proposal.py`, plus pass-through to counter functions; (b) attach `queue.cache_redis` at construction so hooks read `getattr(job.queue, "cache_redis", None)`. Pick one in planning for consistency across all four construction sites and the test fakes.
4. **Pool sizing budget.** Needs the homelab Postgres `max_connections` value to set safe per-queue `min/max`.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| saq | queue | ✓ | 0.26.4 | — |
| psycopg (3) | SAQ Postgres backend | ✗ (not yet installed) | needs >=3.2.0 | `uv sync` after dep swap |
| psycopg_pool | SAQ Postgres pool | ✗ | needs >=3.2.0 | pulled by `psycopg[pool]` |
| redis (py) | cache plane | ✓ (transitive today) | existing | make explicit dep |
| PostgreSQL server | broker + ORM | ✓ (compose) | 16+ | — |
| Redis server | cache only | ✓ (compose) | 7+ | — |

**Missing dependencies with fallback:** `psycopg`/`psycopg_pool` install via `uv add 'saq[postgres]'` + `uv sync`. Confirm the psycopg3 wheel installs on the Docker slim base (it ships binary wheels via `psycopg[binary]`; the `[pool]` extra is pure-Python — verify `psycopg[pool]` resolves the C/binary impl or that `libpq` is present in the image, mirroring the Phase-30 essentia apt-layer lesson).

**Missing dependencies with no fallback:** none.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_deterministic_key.py tests/test_task_split.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| REQ-36-1 | Queues construct as PostgresQueue at all 4 sites | unit | `uv run pytest tests/test_queue_factory.py -x` | ❌ Wave 0 |
| REQ-36-2 | priority+scheduled columns honored on enqueue/dequeue | integration (real PG) | `uv run pytest tests/integration/test_pg_queue_priority.py -x` | ❌ Wave 0 |
| REQ-36-3 | duplicate deterministic key returns None (reenqueue skip) on Postgres | integration | `uv run pytest tests/integration/test_pg_dedup.py -x` | ❌ Wave 0 |
| REQ-36-4 | `saq_web`/`info()` renders against PostgresQueue | unit/integration | `uv run pytest tests/test_saq_mount.py -x` | ⚠️ exists, extend for PG |
| REQ-36-5 | counters still increment via dedicated cache-redis (not queue.redis) | unit | `uv run pytest tests/test_deterministic_key.py -x` | ⚠️ exists, update fakes |
| REQ-36-5 | generate_proposals reads cache-redis, not queue.redis | unit | `uv run pytest tests/test_proposal_task.py -x` | ⚠️ verify/extend |
| REQ-36 | agent_worker import boundary still clean + PHAZE_QUEUE_URL handling | subprocess | `uv run pytest tests/test_task_split.py -x` | ⚠️ update env |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_deterministic_key.py tests/test_task_split.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** full suite green (incl. the new PG integration tests against the dedicated integration DB — recipe `just integration-test` / `just test-db` exists per quick task 260520-bcl) before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_queue_factory.py` — covers REQ-36-1 (factory returns PostgresQueue with both hooks)
- [ ] `tests/integration/test_pg_queue_priority.py` — covers REQ-36-2 (priority/scheduled ordering)
- [ ] `tests/integration/test_pg_dedup.py` — covers REQ-36-3 (ON CONFLICT dedup returns None)
- [ ] Update `tests/_queue_fakes.py` — `FakeQueue.redis` → reflect the new dedicated cache-redis mechanism (Pitfall 2 decision)
- [ ] Update `tests/test_task_split.py` env — provide `PHAZE_QUEUE_URL`, assert psycopg3 import does NOT pull `sqlalchemy.ext.asyncio`
- [ ] Extend `tests/test_saq_mount.py` — assert mount works over PostgresQueue `.info()`

## Security Domain

`security_enforcement` is not configured in `.planning/config.json` (treat as enabled).

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | Queue backend swap; no auth surface change |
| V3 Session Management | no | — |
| V4 Access Control | yes (infra) | `/saq` dashboard remains behind reverse-proxy internal-realm auth (LOCKED, T-33-03); no new port |
| V5 Input Validation | yes | `PHAZE_QUEUE_URL` is operator-supplied — validate/normalize DSN; `queue_url` joins `SECRET_FILE_FIELDS` |
| V6 Cryptography | yes | Queue DSN carries DB credentials — store via `<VAR>_FILE`/SOPS like `database_url`; never log it |

### Known Threat Patterns for SAQ-Postgres
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Postgres credentials in env/logs | Information Disclosure | `SecretStr`/`<VAR>_FILE`; never log `queue_url`; mirror `redis_url` handling |
| Agents gain a Postgres attack path (new network edge) | Elevation/Spoofing | Firewall agents→Postgres:5432; least-priv DB role (only its own schema); consider production credential guard analog (Open Q1) |
| Unbounded pool fan-out (DoS via connection exhaustion) | Denial of Service | Conservative per-queue pool sizing; total-connection budget vs `max_connections` (Pitfall 4) |
| SAQ DDL on first boot under shared schema | Tampering | Advisory-lock-guarded `init_db()` is idempotent; ensure role can CREATE only in its own schema |

## Sources

### Primary (HIGH confidence)
- Installed `saq==0.26.4` source: `saq/queue/postgres.py` (lines 54-227 constructor/connect/init_db; 653-668 dequeue; 700-755 `_enqueue` dedup; 245 `info()`), `saq/queue/redis.py` (`_enqueue`), `saq/queue/base.py` (80-355 enqueue + `register_before_enqueue`/`_before_enqueue`), `saq/queue/postgres_migrations.py` (DDL), `saq/web/starlette.py` (`q.info()` rendering)
- Project code: `tasks/controller.py`, `tasks/agent_worker.py`, `services/agent_task_router.py`, `main.py`, `tasks/_shared/deterministic_key.py`, `tasks/_shared/queue_defaults.py`, `services/pipeline_counters.py`, `tasks/proposal.py`, `services/proposal.py`, `routers/pipeline.py`, `web/saq_mount.py`, `config.py`, `tests/test_task_split.py`, `tests/_queue_fakes.py`
- `importlib.metadata` on installed saq: extras `psycopg[pool]>=3.2.0; extra=="postgres"`, `redis<8.0,>=4.2; extra=="redis"`
- `.planning/ROADMAP.md` (Phase 36 §238-256), `.planning/STATE.md` (Phases 36/37/38 accumulated context)

### Secondary (MEDIUM confidence)
- Project CLAUDE.md technology-stack + constraints; auto-memory `project_stage_pause_priority_design`, `project_arq_to_saq`

### Tertiary (LOW confidence)
- None — all critical claims verified against installed source or project code.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — extras + versions read from installed saq metadata
- Queue API surface / dedup / priority: HIGH — read directly from installed `saq/queue/postgres.py`
- Break surfaces (`queue.redis`): HIGH — grepped + read every call site
- Pool sizing / connection budget: MEDIUM — defaults verified; homelab `max_connections` unknown (Open Q4)
- Cutover / network topology: MEDIUM — depends on operator decisions (Open Q1–Q3)

**Research date:** 2026-06-12
**Valid until:** 2026-07-12 (stable; re-verify if saq is bumped past 0.26.x)
