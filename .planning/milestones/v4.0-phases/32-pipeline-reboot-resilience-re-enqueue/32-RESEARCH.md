# Phase 32: Pipeline Reboot Resilience & Re-enqueue - Research

**Researched:** 2026-06-11
**Domain:** SAQ deterministic-key dedup + controller-worker startup/cron re-enqueue (Python 3.14, saq==0.26.4, async SQLAlchemy)
**Confidence:** HIGH

## Summary

The locked design is fully implementable with the installed code — no new dependencies, no SAQ-version surprises. The single load-bearing primitive (a deterministic SAQ job key that no-ops a re-enqueue while the file's job is still incomplete) is **confirmed against installed `saq==0.26.4` source**, not training data: `Queue.enqueue("process_file", key="process_file:<id>", **payload)` works, and `RedisQueue._enqueue` returns `None` (a true no-op) when a job with the same `job.id` is still in the queue's `incomplete` sorted set. The key, timeout, and retries all coexist with the `ProcessFilePayload` fields because SAQ routes any kwarg matching a `Job` dataclass field to the Job and everything else into `job.kwargs`.

Everything else is wiring that mirrors patterns already in the repo. The controller's `startup(ctx)` already builds an async sessionmaker (`ctx["async_session"]`) exactly like `reap_stalled_scans` consumes, so a new re-enqueue task queries Postgres identically. Routing `process_file` to the active agent without `app.state` is a two-line reuse: `select_active_agent(session)` (takes only a session, raises `NoActiveAgentError`) + `AgentTaskRouter(redis_url).queue_for(agent.id)`. The cleanest factoring is a FastAPI-free shared helper (the deterministic-key + `ProcessFilePayload` build) that both `routers/pipeline.py::_enqueue_analysis_jobs` and the new controller task import — keeping FastAPI out of the worker import graph.

**Primary recommendation:** Add a FastAPI-free shared helper `enqueue_process_file(queue, file, agent_id, models_path)` (and `process_file_job_key(file_id)`) in a `services/` module; have BOTH the dashboard path and a new `tasks/reenqueue.py` call it; register the re-enqueue as both a `startup`-hook call and a `CronJob("*/5 * * * *")` on the controller; route via `select_active_agent` + a startup-stashed `AgentTaskRouter`; catch `NoActiveAgentError` → warn + return 0.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Detect DISCOVERED files with no live job | Controller worker (Postgres) | — | Controller is the only worker with direct DB access (`ctx["async_session"]`); agent worker is HTTP-backed and Postgres-free (`test_task_split.py`) |
| Deterministic dedup of re-enqueues | SAQ / Redis | — | `RedisQueue._enqueue` Lua script enforces per-queue key uniqueness over the `incomplete` set |
| Route `process_file` to active agent | Controller worker | enqueue_router + AgentTaskRouter | `process_file` is an AGENT_TASK; must land on `phaze-agent-<id>` queue a real agent consumes |
| Build complete `ProcessFilePayload` + key | Shared service helper (FastAPI-free) | — | Single source of truth shared by dashboard router + controller task; must not pull FastAPI into the worker |
| Periodic + boot-time trigger | Controller SAQ `startup` + `CronJob` | — | Startup = immediate post-reboot recovery; cron = mid-run stall recovery |

## Standard Stack

No new packages. Everything required is already installed and in use.

| Library | Version | Purpose | Status |
|---------|---------|---------|--------|
| saq | 0.26.4 | Deterministic-key enqueue + dedup, `CronJob`, controller `startup` | `[VERIFIED: installed source]` `.venv/lib/python3.14/site-packages/saq/__init__.py:17` (`__version__ = "0.26.4"`) |
| SQLAlchemy (async) | 2.0.x | Postgres query for `FileState.DISCOVERED` | Already used by controller |
| pydantic | 2.x | `ProcessFilePayload` (`extra="forbid"`) | `schemas/agent_tasks.py:28` |
| structlog | — | Worker logging | Already used in `scan_reaper.py` / `controller.py` |

## Package Legitimacy Audit

> Not applicable — this phase installs **no external packages**. All code reuses libraries already locked in `pyproject.toml`/`uv.lock`. No slopcheck run required.

## Architecture Patterns

### System Architecture Diagram

```
HOST REBOOT / CONTAINER RESTART
        │  (Redis empty — no AOF; Postgres FileState is the source of truth)
        ▼
┌─────────────────────────── Controller worker (PHAZE_ROLE=control) ───────────────────────────┐
│  saq phaze.tasks.controller.settings                                                          │
│                                                                                               │
│  startup(ctx) ──► ctx["async_session"] (sessionmaker)                                          │
│             └───► ctx["task_router"] = AgentTaskRouter(cfg.redis_url)   [NEW: stash + close]   │
│             └───► reenqueue_discovered(ctx)  ◄── ALSO called once on boot  [NEW]               │
│                                                                                               │
│  CronJob("*/5 * * * *", reenqueue_discovered)  [NEW]  ── mid-run stall recovery                │
│  CronJob("* * * * *",  reap_stalled_scans)     existing                                        │
└───────────────────────────────────────────────┬───────────────────────────────────────────────┘
                                                 │  reenqueue_discovered(ctx)
                                                 ▼
        ┌──────────────── async with ctx["async_session"]() as session ───────────────┐
        │  1. SELECT FileRecord WHERE state == DISCOVERED                              │
        │  2. agent = select_active_agent(session)   ──► NoActiveAgentError? ──► warn, │
        │                                                  return {"reenqueued": 0}     │
        │  3. queue = ctx["task_router"].queue_for(agent.id)   (phaze-agent-<id>)       │
        │  4. for f in discovered:                                                      │
        │        job = await enqueue_process_file(queue, f, agent.id, models_path)      │
        │        # key=f"process_file:{f.id}"  + ProcessFilePayload + timeout/retries   │
        │        if job is None: skipped += 1   (SAQ no-op: key already incomplete)     │
        │        else:            enqueued += 1                                          │
        └──────────────────────────────────┬───────────────────────────────────────────┘
                                            ▼
                       SAQ RedisQueue._enqueue (Lua):  ZSCORE incomplete:<job.id> ?
                       member → return nil (NO-OP)  ·  absent → SET+ZADD+RPUSH → job

  SHARED KEY FORMAT (single source of truth):
      routers/pipeline.py::_enqueue_analysis_jobs ─┐
                                                   ├─► enqueue_process_file(...)  [services/, FastAPI-free]
      tasks/reenqueue.py::reenqueue_discovered ────┘     key = process_file:<file_id>
```

### Component Responsibilities

| File | Responsibility | Change |
|------|----------------|--------|
| `services/<new or pipeline>.py` | `process_file_job_key(file_id)` + `enqueue_process_file(queue, file, agent_id, models_path)` — build key + `ProcessFilePayload` + enqueue with `timeout=14400, retries=2` | NEW shared helper (FastAPI-free) |
| `routers/pipeline.py::_enqueue_analysis_jobs` | Dashboard "Run Analysis" path | REFACTOR to call shared helper (adds the key it currently lacks) |
| `tasks/reenqueue.py` | `reenqueue_discovered(ctx)` controller task: query DISCOVERED, pick agent, enqueue each via shared helper, count enqueued/skipped | NEW (mirror `tasks/scan_reaper.py`) |
| `tasks/controller.py` | Register new `CronJob` + call `reenqueue_discovered` once from `startup`; stash/close `ctx["task_router"]` | EDIT |
| `services/enqueue_router.py::select_active_agent` | Active-agent selection (session-only) | REUSE as-is |
| `services/agent_task_router.py::queue_for` | Per-agent cached Queue (hook applied) | REUSE as-is |

### Pattern 1: Enqueue with a deterministic key (the load-bearing primitive)

```python
# Source: installed saq/queue/base.py:314-357 (Queue.enqueue) + saq/job.py:120 (Job.key field)
# key / timeout / retries are Job dataclass fields -> set as Job properties.
# ProcessFilePayload fields are NOT Job fields -> routed into job.kwargs (the worker payload).
job = await queue.enqueue(
    "process_file",
    key=f"process_file:{file_id}",   # deterministic -> dedup
    timeout=14400,
    retries=2,
    **payload.model_dump(mode="json"),
)
if job is None:
    # SAQ no-op: a job with this key is still incomplete (queued/active/scheduled)
    skipped += 1
else:
    enqueued += 1
```

### Pattern 2: Controller task that queries Postgres + routes to an agent

```python
# Source: tasks/scan_reaper.py:38-78 (ctx["async_session"] usage)
#       + services/enqueue_router.py:93-117 (select_active_agent)
#       + services/agent_task_router.py:68-77 (queue_for)
async def reenqueue_discovered(ctx: dict[str, Any]) -> dict[str, int]:
    cfg = get_settings()
    async with ctx["async_session"]() as session:
        files = (await session.execute(
            select(FileRecord).where(FileRecord.state == FileState.DISCOVERED)
        )).scalars().all()
        if not files:
            return {"reenqueued": 0, "skipped": 0}
        try:
            agent = await select_active_agent(session)
        except NoActiveAgentError:
            logger.warning("reenqueue skipped: no active agent", discovered=len(files))
            return {"reenqueued": 0, "skipped": 0}
        queue = ctx["task_router"].queue_for(agent.id)
        # ... loop + enqueue_process_file(...) ...
```

### Anti-Patterns to Avoid

- **Importing `routers/pipeline.py` from the controller task.** It imports FastAPI (`routers/pipeline.py:9`). Dragging it into the worker pollutes the worker import graph. Put the shared helper in a FastAPI-free `services/` module instead.
- **Reusing `resolve_queue_for_task` in the controller.** It requires `app_state` (`enqueue_router.py:120-141`); the controller worker has no `app.state`. Use `select_active_agent` + `AgentTaskRouter` directly.
- **Constructing a new `AgentTaskRouter` per cron tick.** Each holds a per-agent Redis connection pool. Stash one in `ctx` at `startup` and `close()` it in `shutdown` (mirror `ctx["discogs_client"]` lifecycle, `controller.py:70,99-101`).
- **Passing only `file_id`.** The v4.0.8 incident: enqueuing only `file_id` dead-letters every job against `ProcessFilePayload`'s `extra="forbid"`. Build the complete 5-field payload.
- **Adding `key` without also passing `timeout=14400, retries=2`.** Without explicit values, the `apply_project_job_defaults` hook would set `retries` to `worker_max_retries=4` (`config.py:195`), reviving the 4× re-analysis churn Phase 31 killed.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| "Skip files that already have a live job" | A pre-query of Redis / a custom in-flight set | SAQ deterministic `key` + native `_enqueue` dedup | Atomic Lua check over the `incomplete` set; race-free; returns `None` on duplicate (`redis.py:447-471`) |
| Active-agent selection | A new `SELECT ... ORDER BY last_seen_at` | `enqueue_router.select_active_agent(session)` | Already the single source of truth used by the dashboard; identical semantics keep both producers aligned |
| Per-agent Queue construction | Inline `Queue.from_url(...)` | `AgentTaskRouter.queue_for(agent_id)` | Caches the connection pool AND registers `apply_project_job_defaults` (the quick-260609-f96 10s-timeout regression guard, `agent_task_router.py:99`) |

**Key insight:** Every "did this file already get queued?" check you might hand-roll is already implemented correctly and atomically by SAQ's enqueue Lua script. The phase's entire dedup correctness reduces to "use the same key on the same queue."

## Load-Bearing Findings (RESEARCH question answers)

### Q1 — SAQ deterministic key + dedup semantics (saq==0.26.4) `[VERIFIED: installed source]`

**Custom key API.** Use `Queue.enqueue("process_file", key=..., **kwargs)` — you do NOT need to build a `Job` manually. `Queue.enqueue` signature: `async def enqueue(self, job_or_func: str | Job, **kwargs) -> Job | None` (`base.py:314`). Kwarg routing (`base.py:332-338`):

```python
for k, v in kwargs.items():
    if k in Job.__dataclass_fields__:
        job_kwargs[k] = v            # -> Job property
    else:
        job_kwargs.setdefault("kwargs", {})[k] = v   # -> job.kwargs (worker payload)
```

`key` is a `Job` dataclass field with `default_factory=get_default_job_key` (`job.py:120`), so `key=` becomes a Job property. `timeout`, `retries`, `ttl` are likewise Job fields. The 5 `ProcessFilePayload` fields (`file_id`, `original_path`, `file_type`, `agent_id`, `models_path`) are NOT Job fields → they land in `job.kwargs`. **No collision** — confirmed none of the payload names appear in `Job.__dataclass_fields__` (`job.py:117-140`).

**Dedup / no-op-on-duplicate-incomplete.** `RedisQueue._enqueue` (`redis.py:447-471`) runs this Lua script:

```lua
if not redis.call('ZSCORE', KEYS[1], KEYS[2]) and redis.call('EXISTS', KEYS[4]) == 0 then
    redis.call('SET', KEYS[2], ARGV[1])
    redis.call('ZADD', KEYS[1], ARGV[2], KEYS[2])
    if ARGV[2] == '0' then redis.call('RPUSH', KEYS[3], KEYS[2]) end
    return 1
else
    return nil
end
```
- `KEYS[1]` = `self._incomplete` (`saq:<queue>:incomplete` sorted set)
- `KEYS[2]` = `job.id` = `f"saq:job:{name}:{key}"` (`redis.py:96-97,45`)
- `KEYS[4]` = `job.abort_id`

If `job.id` is **already a member of the `incomplete` set** (i.e. a job with this key on this queue is queued/active/scheduled), `ZSCORE` is truthy → the script returns `nil` → `_enqueue` returns `None` (`redis.py:467-468`) → `Queue.enqueue` returns `None`. **No raise, no overwrite — a clean no-op.** A second guard: if the job is mid-abort (`abort_id` exists) enqueue also no-ops. The job is removed from `incomplete` on completion (`_finish` → `zrem(self._incomplete, job_id)`, `redis.py:434`), so after a file's job finishes the same key can be enqueued again — exactly the desired "re-run a finished/failed file" behavior.

**Return value contract for the re-enqueue loop:** `job is None` ⇒ deduped (count as skipped); a `Job` ⇒ newly enqueued.

**kwargs coexist with key:** YES (proven above). The existing `_enqueue_analysis_jobs` already passes `timeout=` / `retries=` alongside `**payload` (`routers/pipeline.py:72-83`) and works; adding `key=` is the same mechanism.

**Existing default key:** `Job.key` default = `get_default_job_key()` = `uuid1()` (`job.py:22-23,120`); `job_id = f"saq:job:{name}:{key}"` (`redis.py:45,96-97`). This reproduces the live `saq:job:phaze-agent-nox:<uuid>` — queue name `phaze-agent-nox`, key a uuid1. Today `_enqueue_analysis_jobs` passes **no** `key` (`routers/pipeline.py:72`), so every job gets a random uuid → zero dedup. Overriding with `key="process_file:<file_id>"` is the entire fix.

**Per-queue scope (accepted edge case):** `job.id` embeds the queue name. The same key on two different agent queues = two different `job.id`s = NO cross-queue dedup. Matches CONTEXT's single-agent assumption (`32-CONTEXT.md` Specifics). Not a blocker.

### Q2 — Controller startup-hook + cron context `[VERIFIED: source]`

`startup(ctx)` (`controller.py:44-89`) populates `ctx` with: `async_session` (an `async_sessionmaker`, `controller.py:66`), `task_engine`, `discogs_client`, `proposal_service`, and `queue` (the **module-level controller queue**, name `"controller"`, `controller.py:88,106`). A controller task gets a DB session exactly as `reap_stalled_scans` does:

```python
# scan_reaper.py:49
async with ctx["async_session"]() as session:
    ...
```

Register the cron + startup call in the `settings` dict (`controller.py:115-133`): append `reenqueue_discovered` to `functions`, add `CronJob(reenqueue_discovered, cron="*/5 * * * *")` to `cron_jobs`, and call `await reenqueue_discovered(ctx)` at the end of `startup`. Also stash `ctx["task_router"] = AgentTaskRouter(cfg.redis_url)` in `startup` and `await ctx["task_router"].close()` in `shutdown` (`controller.py:91-101`).

> ⚠️ The module-level `ctx["queue"]` is the **controller** queue (name `"controller"`), which **no agent consumes** for `process_file`. Do NOT enqueue `process_file` onto it — that recreates the consumer-less-queue class of bug. Route to a `phaze-agent-<id>` queue via the task_router (Q3).

### Q3 — Active-agent routing WITHOUT app.state `[VERIFIED: source]`

`select_active_agent(session: AsyncSession) -> Agent` (`enqueue_router.py:93-117`) takes **only a session** — directly reusable in a controller task. It selects `revoked_at IS NULL AND last_seen_at IS NOT NULL ORDER BY last_seen_at DESC LIMIT 1`, and **raises `NoActiveAgentError`** (`enqueue_router.py:78-79,114-116`) when none qualify.

Construct the queue: `AgentTaskRouter(redis_url)` (`agent_task_router.py:64`) then `.queue_for(agent.id)` (`agent_task_router.py:68-77`) — returns the cached `phaze-agent-<id>` `saq.Queue` with `apply_project_job_defaults` already registered.

Zero-active-agent path: wrap `select_active_agent` in `try/except NoActiveAgentError`, log a `logger.warning`, return `{"reenqueued": 0, ...}` — never let it propagate out of a `startup` hook or cron (would crash the worker boot / abort the tick).

### Q4 — Shared enqueue helper refactor `[VERIFIED: source]`

`_enqueue_analysis_jobs` (`routers/pipeline.py:44-83`) builds the `ProcessFilePayload` + `timeout=14400` + `retries=2`. Factor the payload-build + deterministic key into one helper so both producers agree on the key format.

**Where it must live:** a **FastAPI-free** module. `routers/pipeline.py` imports FastAPI (`routers/pipeline.py:9`); the controller worker must not transitively import it. `services/pipeline.py` is already FastAPI-free (imports only `sqlalchemy` + `models.file`, `services/pipeline.py:1-13`) and is a clean home, or add a dedicated `services/analysis_enqueue.py`.

**Import-boundary landmines:**
- The worker import graph must not pull in FastAPI. The shared helper imports only `saq` (type-only), `schemas.agent_tasks.ProcessFilePayload`, and `models.file.FileRecord` — all FastAPI-free.
- `test_task_split.py` enforces that `phaze.tasks.agent_worker` (and the watcher) stay free of `phaze.database` / `sqlalchemy.ext.asyncio`. The **controller** is exempt (it owns the DB), so the new `tasks/reenqueue.py` using async SQLAlchemy is fine — but it must be registered ONLY in `controller.py`, never imported by `agent_worker.py` (same rule `scan_reaper.py:6-9` documents).

**Suggested helper shape (FastAPI-free):**
```python
def process_file_job_key(file_id: uuid.UUID) -> str:
    return f"process_file:{file_id}"

async def enqueue_process_file(queue, file, agent_id: str, models_path: str):
    payload = ProcessFilePayload(
        file_id=file.id, original_path=file.original_path,
        file_type=file.file_type, agent_id=agent_id, models_path=models_path,
    )
    return await queue.enqueue(
        "process_file",
        key=process_file_job_key(file.id),
        timeout=14400, retries=2,
        **payload.model_dump(mode="json"),
    )
```

### Q5 — Testing approach `[VERIFIED: source]`

**Controller-task / cron tests (the pattern to copy):** `test_scan_reaper.py` builds a SAQ-shaped ctx with `_make_ctx(async_engine)` → `{"async_session": async_sessionmaker(async_engine, ...)}` (`test_scan_reaper.py:50-53`), seeds rows via the real Postgres `session` fixture, calls the task directly, asserts the returned counts + DB state + WARNING logs via `caplog`. The new re-enqueue task tests mirror this exactly.

**Active-agent selection tests:** `test_enqueue_router.py` seeds agents and asserts `select_active_agent` returns the most-recently-seen / raises `NoActiveAgentError` (`test_enqueue_router.py:95-139`). Reuse these helpers for the controller task's zero-agent skip.

**SAQ enqueue capture — `tests/_queue_fakes.py::FakeQueue`:** `enqueue` splits kwargs into `captured` (payload only) and `captured_policy` (Job-control fields), mirroring SAQ (`_queue_fakes.py:78-91`). Because `_JOB_CONTROL_FIELDS = frozenset(Job.__dataclass_fields__)` (`_queue_fakes.py:53`) and `key` IS a Job dataclass field, **`FakeQueue` DOES capture the deterministic key** — it appears in `queue.captured_policy[i]["key"]`, not in `captured`. So asserting "both producers emit `process_file:<id>`" is possible **today** with the existing fake. ✅ (Not a Wave-0 capture gap.)

**Wave-0 HARNESS GAP — dedup not modeled.** `FakeQueue.enqueue` always appends and returns a fresh `MagicMock` job (`_queue_fakes.py:88-91`); it never returns `None` for a duplicate incomplete key. The "re-enqueue of an in-flight file is a no-op" behavior therefore **cannot** be asserted against `FakeQueue` as-is. Two options:
1. **Dedup-aware fake (recommended for fast unit coverage):** a small `DedupFakeQueue` that tracks live keys in a set and returns `None` when a key is re-enqueued before being "finished." Lets the no-op assertion run without Redis.
2. **Integration test (true SAQ semantics):** there is **no `fakeredis`** — `test_agent_task_router.py:5-12` documents that SAQ's `Queue.from_url` is incompatible with fakeredis at `saq>=0.26`, so real-Redis tests are gated `@pytest.mark.integration` and skip cleanly when Redis is absent. Add one integration test that enqueues the same key twice and asserts the second returns `None`.

Recommend BOTH: the dedup-aware fake for deterministic unit coverage of the no-op path, plus one `@pytest.mark.integration` test pinning the real SAQ behavior end-to-end.

## Runtime State Inventory

> This phase manipulates runtime queue state (re-enqueue), so the inventory is relevant even though it is not a rename.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | Postgres `files.state == 'discovered'` is the source of truth (`models/file.py:34`). Re-enqueue reads it; it is NOT mutated by enqueue (state advances only when the agent finishes the job). | None — read-only by this phase |
| Live service config | SAQ jobs live in Redis (`saq:job:phaze-agent-<id>:<key>`, `saq:phaze-agent-<id>:incomplete`). Redis is disposable (no AOF) — empty after reboot, which is precisely why startup re-enqueue is needed. | None — Redis state is intentionally ephemeral |
| OS-registered state | None — no OS scheduler; the cron is a SAQ `CronJob` registered in `controller.settings` (`controller.py:125-130`). | None |
| Secrets/env vars | `redis_url` (`config.py:153`), `models_path` (`config.py:187`), `worker_max_retries` (`config.py:195`) — all read, none renamed. | None |
| Build artifacts | None — no package rename; pure additive code. | None |

**Existing-key collision check:** today `process_file` jobs carry random uuid1 keys (`saq:job:phaze-agent-nox:<uuid>`). After deploy, both producers switch to `process_file:<file_id>`. A re-enqueue running while a uuid-keyed legacy job is still in-flight for the same file would NOT dedup against it (different key) — a one-time, post-deploy transient that resolves on the first full cron cycle. Worth a one-line note in the plan; not a correctness risk (per-file re-run is idempotent — `put_analysis` replaces window rows, CONTEXT §domain).

## Common Pitfalls

### Pitfall 1: Routing process_file to the controller queue
**What goes wrong:** Enqueuing onto `ctx["queue"]` (name `"controller"`) strands every job — no agent consumes it (the v4.0.6 class of bug).
**Why it happens:** The module-level queue is the natural thing to grab in a controller task.
**How to avoid:** Always route `process_file` via `select_active_agent` + `task_router.queue_for(agent.id)`.
**Warning signs:** Jobs visible in `saq:controller:*` instead of `saq:phaze-agent-<id>:*`; DISCOVERED count never drops after a re-enqueue tick.

### Pitfall 2: Dropping the explicit timeout/retries when adding the key
**What goes wrong:** `apply_project_job_defaults` sets `retries = worker_max_retries = 4`, reviving the 4× re-analysis churn Phase 31 fixed.
**Why it happens:** The hook only fills values still at the SAQ default (`queue_defaults.py:80-85`); omitting `retries` leaves it at the default `1`, which the hook then clobbers to 4.
**How to avoid:** Pass `timeout=14400, retries=2` on every `process_file` enqueue (the shared helper guarantees this for both producers).
**Warning signs:** A bad/long file analyzed 4× before dead-lettering.

### Pitfall 3: Unbounded re-enqueue loop crashing the boot
**What goes wrong:** A raised exception inside `startup` aborts controller boot; an unhandled `NoActiveAgentError` mid-loop stops the whole re-enqueue.
**Why it happens:** `select_active_agent` raises when no agent is live (common right after a cold full-host reboot where the agent hasn't checked in yet).
**How to avoid:** Catch `NoActiveAgentError` → warn → return 0. The next cron tick (≤5 min) retries once the agent checks in.
**Warning signs:** Controller container restart-looping after a reboot; "no active agent" never followed by a later successful tick.

### Pitfall 4: AgentTaskRouter connection-pool leak across cron ticks
**What goes wrong:** Constructing a new `AgentTaskRouter` (or `Queue.from_url`) per tick accretes Redis connections.
**How to avoid:** Build once in `startup`, stash in `ctx["task_router"]`, `close()` in `shutdown`.
**Warning signs:** Redis `CLIENT LIST` growth over hours; connection-exhaustion warnings.

## Code Examples

All examples are reproduced from the installed/in-repo sources cited inline above (Patterns 1–2 and Q1–Q5). No external/un-verified snippets were used.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual "Run Analysis" re-click after reboot | Controller `startup` + cron auto re-enqueue | This phase | No human action to resume after reboot |
| Random uuid1 job keys (no dedup) | Deterministic `process_file:<file_id>` key | This phase | Re-enqueue is idempotent; cron safe to run frequently |
| Per-stage enqueue scattered with/without policy | Shared FastAPI-free helper applies key + timeout + retries | This phase | Single source of truth; both producers cannot drift |

**Deprecated/outdated:** none relevant — `saq==0.26.4` is the installed/locked version; semantics confirmed against its source, not training data.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `services/pipeline.py` (or a new `services/` module) is the right home for the shared helper | Q4 / Components | LOW — any FastAPI-free module works; planner may pick another location. Verified `services/pipeline.py` is FastAPI-free (`services/pipeline.py:1-13`) |
| A2 | Cron cadence `*/5 * * * *` | Decisions (Claude's discretion) | LOW — CONTEXT explicitly leaves cadence to discretion and recommends 5 min |

**Note:** Both items are explicitly within CONTEXT's "Claude's Discretion." No locked decision is assumed. All load-bearing SAQ claims (Q1–Q3, Q5) are `[VERIFIED: installed source]`, not assumed.

## Open Questions

1. **One-time legacy-key transient at deploy.**
   - What we know: in-flight uuid-keyed `process_file` jobs won't dedup against the new deterministic key for the same file.
   - What's unclear: whether to actively drain/ignore (purge Redis is already the homelab state per MEMORY — worker stopped, Redis purged).
   - Recommendation: no special handling; per-file re-run is idempotent. Plan should note it in one line. Given the homelab is currently paused with Redis purged, the first deploy starts clean anyway.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| saq | Enqueue/dedup/cron | ✓ | 0.26.4 | — |
| Redis | SAQ broker (runtime + integration tests) | runtime: prod; tests: optional | 7+ | Integration tests skip via `@pytest.mark.integration` when absent; unit tests use FakeQueue / dedup-aware fake |
| PostgreSQL | DISCOVERED query (runtime + tests) | ✓ test fixture | 16+ | — |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** Redis for fast tests — covered by `FakeQueue` (key capture) + a proposed dedup-aware fake (no-op assertion).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (8.x), `asyncio_mode` per repo config |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_tasks/test_reenqueue.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements → Test Map
| Behavior (locked) | Test Type | Automated Command | File Exists? |
|-------------------|-----------|-------------------|-------------|
| Deterministic-key dedup: re-enqueue of an in-flight file is a NO-OP | unit (dedup-aware fake) | `uv run pytest tests/test_tasks/test_reenqueue.py::test_reenqueue_inflight_file_is_noop -x` | ❌ Wave 0 (new test + dedup-aware fake) |
| Deterministic-key dedup: true SAQ semantics end-to-end | integration | `PHAZE_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration tests/test_tasks/test_reenqueue.py -x` | ❌ Wave 0 |
| Startup re-enqueues all DISCOVERED | unit | `uv run pytest tests/test_tasks/test_reenqueue.py::test_startup_reenqueues_all_discovered -x` | ❌ Wave 0 |
| Cron re-enqueues stragglers (subset still DISCOVERED) | unit | `uv run pytest tests/test_tasks/test_reenqueue.py::test_cron_reenqueues_stragglers -x` | ❌ Wave 0 |
| Zero active agent → skip gracefully (returns 0, warns, no raise) | unit | `uv run pytest tests/test_tasks/test_reenqueue.py::test_no_active_agent_skips -x` | ❌ Wave 0 |
| Complete `ProcessFilePayload` built (all 5 fields) | unit | `uv run pytest tests/test_tasks/test_reenqueue.py::test_payload_is_complete -x` | ❌ Wave 0 |
| Shared key format identical across BOTH producers | unit | `uv run pytest tests/test_routers/test_pipeline.py::test_analyze_uses_deterministic_key tests/test_tasks/test_reenqueue.py::test_reenqueue_uses_deterministic_key -x` | ❌ Wave 0 (assert via `FakeQueue.captured_policy[i]["key"]`) |
| CronJob + startup call registered on controller | unit | `uv run pytest tests/test_tasks/test_controller_reenqueue_registration.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_tasks/test_reenqueue.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (≥85% gate, CLAUDE.md)
- **Phase gate:** full suite green + `@pytest.mark.integration` dedup test green against a real Redis before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_tasks/test_reenqueue.py` — controller task: startup/cron/no-op/zero-agent/payload (mirror `test_scan_reaper.py` ctx pattern)
- [ ] Dedup-aware fake queue (small `DedupFakeQueue`, or extend `tests/_queue_fakes.py`) so the no-op path is unit-testable without Redis
- [ ] One `@pytest.mark.integration` real-Redis test pinning SAQ's `enqueue`-returns-`None`-on-duplicate behavior
- [ ] `tests/test_tasks/test_controller_reenqueue_registration.py` — asserts the new `CronJob` + `functions` entry + `startup` call
- [ ] Shared-key assertion added to the existing dashboard pipeline test (both producers emit `process_file:<id>`)
- [ ] No framework install needed — pytest/pytest-asyncio already present

## Security Domain

This phase is internal worker/queue plumbing with no new external input surface.

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | yes (already covered) | `ProcessFilePayload` `extra="forbid"` (`schemas/agent_tasks.py:31`) validates the job payload as strictly as an HTTP body — unchanged by this phase |
| V2 Auth / V3 Session / V4 Access Control / V6 Crypto | no | No new endpoints, sessions, authz, or crypto; the re-enqueue runs inside the trusted controller worker |

| Pattern | STRIDE | Mitigation |
|---------|--------|-----------|
| Redis key injection via file id | Tampering | `file_id` is a server-generated UUID (`models/file.py:50`), interpolated into the key as a UUID string — no untrusted free-text in the key |
| DoS via runaway re-enqueue | Denial of Service | Deterministic key + native dedup bounds duplicate jobs to one-per-file-per-queue; cron cadence (5 min) bounds churn |

## Sources

### Primary (HIGH confidence — installed source / in-repo)
- `.venv/lib/python3.14/site-packages/saq/queue/base.py:314-357` — `Queue.enqueue` signature + kwarg→Job-field routing
- `.venv/lib/python3.14/site-packages/saq/queue/redis.py:45,96-97,434,447-471` — `job_id` format, `_enqueue` dedup Lua script, incomplete-set lifecycle
- `.venv/lib/python3.14/site-packages/saq/job.py:22-23,45-71,117-140` — `get_default_job_key` (uuid1), `CronJob`, `Job` dataclass `key` field
- `src/phaze/tasks/controller.py:44-133` — startup ctx assembly + settings/cron registration
- `src/phaze/tasks/scan_reaper.py:38-78` — controller-task ctx["async_session"] + return-count pattern
- `src/phaze/services/enqueue_router.py:78-117` — `select_active_agent` + `NoActiveAgentError`
- `src/phaze/services/agent_task_router.py:64-101` — `AgentTaskRouter` / `queue_for`
- `src/phaze/routers/pipeline.py:44-83` — `_enqueue_analysis_jobs` (current no-key path)
- `src/phaze/tasks/_shared/queue_defaults.py:54-88` — `apply_project_job_defaults` (key never touched; retries-default clobber)
- `src/phaze/schemas/agent_tasks.py:28-37` — `ProcessFilePayload` (`extra="forbid"`, 5 fields)
- `src/phaze/models/file.py:34,50,62-66` — `FileState.DISCOVERED`, `FileRecord` fields
- `tests/_queue_fakes.py:53,78-91` — `FakeQueue` capture split (key → `captured_policy`)
- `tests/test_tasks/test_scan_reaper.py:50-53` — ctx test pattern
- `tests/test_services/test_enqueue_router.py:95-187` — active-agent + routing tests
- `tests/test_services/test_agent_task_router.py:5-12` — no fakeredis; integration-gated real Redis
- `tests/test_task_split.py:1-80` — import-boundary invariant (worker stays Postgres/FastAPI-clean)
- `.planning/.../32-CONTEXT.md` — locked decisions

### Secondary / Tertiary
- None — all findings verified against installed source or repo files; no WebSearch/training-data claims.

## Metadata

**Confidence breakdown:**
- SAQ deterministic key + dedup (Q1): HIGH — read the actual Lua script + dataclass fields in installed `saq==0.26.4`
- Controller ctx + routing (Q2/Q3): HIGH — direct source citations; reuses shipped Phase 30 primitives
- Shared helper / import boundary (Q4): HIGH — FastAPI-free home verified; boundary test semantics confirmed
- Testing (Q5): HIGH — existing patterns located; the one genuine gap (FakeQueue doesn't model dedup) is explicitly flagged with two mitigations
- Security: HIGH — no new external surface; existing `extra="forbid"` validation noted

**Research date:** 2026-06-11
**Valid until:** 2026-07-11 (stable — pinned `saq==0.26.4`; revisit only on a SAQ major bump)
