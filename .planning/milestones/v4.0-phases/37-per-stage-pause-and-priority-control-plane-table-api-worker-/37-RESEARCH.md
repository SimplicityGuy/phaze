<!-- GSD:RESEARCH -->
# Phase 37: Per-Stage Pause and Priority Control Plane (table, API, worker hooks) - Research

**Researched:** 2026-06-12
**Domain:** SAQ Postgres-backed `saq_jobs` raw-UPDATE control plane; before-enqueue priority/pause stamping; Alembic app-table + FastAPI control endpoints
**Confidence:** HIGH (saq_jobs schema, dequeue semantics, dedup, and the stage→key mapping all read directly from installed `saq==0.26.4` source + project code)

## Summary

Phase 37 adds operator controls to pause (drain) and reprioritize the three agent pipeline stages by writing the Postgres-backed `saq_jobs` table that Phase 36 establishes. The mechanism is sound and verified against SAQ source: the Postgres dequeue is `WHERE status='queued' AND queue=:q AND now>=scheduled AND priority BETWEEN plow AND phigh ... ORDER BY priority, scheduled ... FOR UPDATE SKIP LOCKED`. Lower `priority` integer dequeues sooner (no inversion needed), and a far-future `scheduled` value removes a job from dequeue eligibility (the "park" mechanism for pause). Resume restores eligibility by resetting `scheduled` to a past value.

**The single most important correction to the phase scope: `saq_jobs` has NO `function` column.** The verified schema (from `saq/queue/postgres_migrations.py`) is `key TEXT PK, lock_key SERIAL, job BYTEA, queue TEXT, status TEXT, priority SMALLINT, group_key TEXT, scheduled BIGINT, expire_at BIGINT`. The task/function name lives only inside the serialized `job` BYTEA blob, which is not SQL-filterable. Every "`WHERE function=<stage>`" in the scope must instead filter on the `key` column prefix. This is workable *because* Phase 35 made keys deterministic as `<function>:<natural_id>` — so the three stages map to `key LIKE 'process_file:%'`, `key LIKE 'extract_file_metadata:%'`, `key LIKE 'fingerprint_file:%'`. That mapping is exact and verified in `_KEY_BUILDERS`.

**The no-double-pickup guarantee is already provided by Postgres row locking** — the admin UPDATE and the worker dequeue contend on the same row lock, and the `status='queued'` guard on the admin UPDATE plus `FOR UPDATE SKIP LOCKED` on the dequeue make it impossible to mutate a job that is being (or has been) picked up. No application-level locking is needed.

**Primary recommendation:** (1) Create `pipeline_stage_control` as a normal Alembic-managed app table (migration 020, seeded with 3 rows) — keep it entirely separate from SAQ's auto-managed `saq_jobs`. (2) Add a third before-enqueue concern that stamps `job.priority` (and `job.scheduled = SENTINEL` when paused) by reading a short-TTL in-process cache of the 3-row control table; read it through the queue's own psycopg3 pool so the hook stays import-boundary-safe on the agent. (3) Implement priority/pause/resume as FastAPI endpoints that update the control row via the ORM and issue the `saq_jobs` backlog UPDATE via `session.execute(text(...))` on the same async session/transaction. Use a fixed `SENTINEL = 9999999999` (BIGINT epoch, year 2286) for parking.

## User Constraints

No `37-CONTEXT.md` exists yet (this research is standalone, ahead of discuss-phase). The constraints below are drawn from the ROADMAP Phase 37 scope, STATE.md, the approved 2026-06-12 inline design (auto-memory `project_stage_pause_priority_design`), and project CLAUDE.md. The planner MUST treat these as locked inputs.

### Locked Decisions (from ROADMAP Phase 37 + approved inline design)
- Scope = the **three agent stages only**: `metadata` (`extract_file_metadata`), `analyze` (`process_file`), `fingerprint` (`fingerprint_file`). NOT tracklist/proposals/execute.
- `pipeline_stage_control` table: `stage` PK ∈ {metadata, analyze, fingerprint}, `paused` bool, `priority` int **default 50, range 0–100, LOWER = higher priority = sooner**, maps directly to SAQ `priority` with **no inversion**, plus `updated_at`.
- Enqueue hook stamps every new job with its stage's current `priority`; if the stage is paused, ALSO sets `scheduled = SENTINEL` so the job parks on enqueue.
- Priority endpoint `POST /pipeline/stages/{stage}/priority` is a **delta** operation: update the control row, then `UPDATE saq_jobs SET priority=:n WHERE status='queued' AND <stage>` — reorders the queued backlog live.
- Pause endpoint `POST /pipeline/stages/{stage}/pause`: `paused=true`, `UPDATE saq_jobs SET scheduled=SENTINEL WHERE status='queued' AND <stage>`. Active jobs finish (**drain semantics**).
- Resume: `paused=false`, `UPDATE saq_jobs SET scheduled=0 WHERE status='queued' AND <stage> AND scheduled=SENTINEL` — **sentinel-guarded** so genuine retry backoffs are never clobbered.
- Depends on Phase 36 (Postgres queue backend with `saq_jobs`, the `build_pipeline_queue` factory, `PHAZE_QUEUE_URL`, the `cache_redis`-attach pattern).
- Phase 38 consumes this: DAG pause toggle + priority stepper, `/pipeline/stats` extended to return per-stage `{paused, priority}`. UI work is OUT OF SCOPE for 37.

### Project Constraints (from CLAUDE.md)
- Python 3.14 exclusively; `uv` only — never bare `pip`/`python`/`pytest`/`mypy`, always `uv run`.
- SQLAlchemy 2.0 (`Mapped`/`mapped_column`) + asyncpg ORM; Alembic migrations; FastAPI + Jinja2/HTMX.
- 85% min coverage, Codecov with service flags. Pre-commit must pass (frozen SHAs); never `--no-verify`.
- PR per phase, worktree branch, no direct main commits. Update affected READMEs + `scripts/update-project.sh`.
- mypy strict (excludes tests/); ruff line length 150, `target-version = py313`.
- Commit frequently during execution, not batched at the end.

### Deferred / Out of Scope
- DAG pause/priority UI + Rescan-button removal + `/pipeline/stats` extension → Phase 38.
- Priority/pause for non-agent stages (tracklist sub-chain, generate_proposals, execute) → not in this feature.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REQ-37-1 | Drain-style pause per agent stage (active jobs finish, queued backlog parks) | `scheduled=SENTINEL` parks queued rows; `status='queued'` guard leaves active rows to drain (Pattern 2, Pitfall 1) — verified vs `postgres.py:644-662` dequeue + `count()` |
| REQ-37-2 | Live backlog reprioritization per agent stage | `UPDATE saq_jobs SET priority=:n WHERE status='queued' AND key LIKE '<fn>:%'` reorders via `ORDER BY priority, scheduled` (Pattern 2, Stage→Key Map) |
| REQ-37-3 | Retry backoffs preserved | Resume guarded by `WHERE scheduled=SENTINEL`; retry sets `scheduled=now+delay` (≠ SENTINEL) so it is never clobbered (Pattern 3, Pitfall 3) — verified vs `postgres.py:_retry` |
| REQ-37-4 | No double-pickup | Admin UPDATE vs dequeue contend on the same row lock; `status='queued'` guard + `FOR UPDATE SKIP LOCKED` make a being-picked-up job unmutatable (Concurrency Safety section) — verified vs `postgres.py:_dequeue` |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `pipeline_stage_control` state (paused/priority per stage) | Database (app schema, ORM+Alembic) | — | Durable operator intent; an app table with a model, NOT part of SAQ's auto-managed schema |
| Pause/priority/resume endpoints | API (FastAPI control router) | DB (raw `saq_jobs` UPDATE) | Operator actions; mirror the existing `/pipeline/*` POST endpoints; behind reverse-proxy internal-realm auth |
| Live backlog mutation (`saq_jobs` UPDATE) | Database (Postgres) | — | Raw SQL against SAQ's table; row-lock concurrency owned by Postgres |
| New-job priority/park stamping | Queue layer (before_enqueue hook) | DB read of control table | Runs at every enqueue; must be import-boundary-safe (no SQLAlchemy on agent) |
| Drain semantics (active jobs finish) | Worker (SAQ dequeue/process) | — | Unchanged; pause never touches `status='active'` rows |

## Verified `saq_jobs` Column Contract (the load-bearing fact)

Source: `saq/queue/postgres_migrations.py` migrations 1–3 (installed `saq==0.26.4`). This is the table the phase issues raw UPDATEs against.

```sql
CREATE TABLE saq_jobs (
    key       TEXT PRIMARY KEY,                 -- deterministic "<function>:<natural_id>" (Phase 35)
    lock_key  SERIAL NOT NULL,                  -- SAQ-internal advisory-lock id; do NOT touch
    job       BYTEA NOT NULL,                   -- JSON-serialized Job dict; function name lives HERE, not a column
    queue     TEXT NOT NULL,                    -- e.g. "phaze-agent-<id>" or "controller"
    status    TEXT NOT NULL,                    -- 'new'|'deferred'|'queued'|'active'|'aborting'|'aborted'|'complete'|'failed'
    priority  SMALLINT NOT NULL DEFAULT 0,      -- range -32768..32767; LOWER dequeues first; app uses 0..100
    group_key TEXT,
    scheduled BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),  -- epoch SECONDS; "park" = far-future value
    expire_at BIGINT
);
-- indexes:
--   saq_jobs_status_queue_group_key_idx          (status, queue, group_key)
--   saq_jobs_status_queue_priority_scheduled_idx (status, queue, priority, scheduled)   <- dequeue index
```

| Column | Type | Phase-37 relevance | Verified note |
|--------|------|--------------------|---------------|
| `key` | TEXT PK | **The stage filter.** No `function` column exists — filter `key LIKE '<fn>:%'` | `migrations[1]` DDL |
| `priority` | **SMALLINT** | Set 0–100; **never go below 0** or `priority BETWEEN 0 AND 32767` excludes it from dequeue | default `priorities=(0,32767)` in `PostgresQueue.__init__` |
| `scheduled` | **BIGINT epoch seconds** | Park = far-future; resume = past. `now_seconds()` is the comparison basis | dequeue uses `%(now)s >= scheduled` |
| `status` | TEXT | UPDATE guard `status='queued'` is what makes drain + no-double-pickup safe | dequeue flips `'queued'→'active'` |
| `queue` | TEXT | Stages do NOT map to queues (all 3 share a per-agent queue), so do NOT filter by queue | — |

**`scheduled=0` means "run now":** dequeue requires `now >= scheduled`; epoch 0 (1970) ⇒ always eligible. The raw resume UPDATE setting `scheduled=0` is correct. (Note: SAQ's own `_enqueue` does `job.scheduled or int(now_seconds())` — the `or` only applies at enqueue time; a raw UPDATE writes the literal `0` and dequeue's `now>=0` makes it immediately eligible.)

**Safe SENTINEL value:** `9999999999` (epoch = 2286-11-20). It is comfortably within BIGINT (`max 9.2e18`), far beyond any legitimate `scheduled` (retry backoffs are `now + small delay`; cron jobs are `now + interval`), and visually distinctive in the table. Define it as a single named constant shared by the hook, the pause endpoint, and the resume guard. Do not compute it per-call.

## Stage → Registered-Function-Name → Key-Prefix Map (exact, verified)

Source: `_KEY_BUILDERS` in `src/phaze/tasks/_shared/deterministic_key.py` and the task definitions.

| Stage label | Registered SAQ function name (`job.function`) | Deterministic key form | `saq_jobs` filter |
|-------------|----------------------------------------------|------------------------|-------------------|
| `metadata` | `extract_file_metadata` | `extract_file_metadata:<file_id>` | `key LIKE 'extract_file_metadata:%'` |
| `analyze` | `process_file` | `process_file:<file_id>` | `key LIKE 'process_file:%'` |
| `fingerprint` | `fingerprint_file` | `fingerprint_file:<file_id>` | `key LIKE 'fingerprint_file:%'` |

- Verified function names: `tasks/metadata_extraction.py:32` `async def extract_file_metadata`, `tasks/functions.py:114` `async def process_file`, `tasks/fingerprint.py:31` `async def fingerprint_file`. Each appears in `_KEY_BUILDERS` keyed by `str(k["file_id"])`.
- In the **enqueue hook**, use `job.function` directly (it equals the registered name) to look up the stage — clean and exact.
- In the **raw UPDATE**, you must use the key prefix (no function column). Because `key` is the global PRIMARY KEY, a given `<fn>:<file_id>` exists at most once across all per-agent queues, so the prefix filter correctly catches every agent's jobs for that stage with no `queue` scoping needed.

**Recommended canonical mapping object** (single source of truth, used by hook + endpoints + tests):
```python
STAGE_TO_FUNCTION = {"metadata": "extract_file_metadata", "analyze": "process_file", "fingerprint": "fingerprint_file"}
```

### Index/collation caveat for `key LIKE '<fn>:%'`
The `key` PRIMARY-KEY btree uses the database's default collation, so `LIKE 'prefix%'` will generally NOT use the index for the prefix (Postgres needs `text_pattern_ops` or a C-collation index for that optimization). The admin UPDATE will instead use `saq_jobs_status_queue_priority_scheduled_idx` for `status='queued'` and then filter `key LIKE` over that bounded candidate set. For an operator-triggered admin action over a backlog (even ~11k rows) this is acceptable. If a faster path is ever wanted, options are: (a) add a `text_pattern_ops` expression index on `key`, or (b) use `split_part(key, ':', 1) = :fn`. **Recommendation:** ship plain `key LIKE '<fn>:%'`; do not add an index in Phase 37 (premature). Flag as a tunable.

## Concurrency Safety: the "no double-pickup" argument (REQ-37-4)

Verified against `PostgresQueue._dequeue` (`postgres.py:636-698`). The dequeue runs in one transaction:
```sql
WITH locked_job AS (
  SELECT key FROM saq_jobs
  WHERE status='queued' AND queue=:q AND :now>=scheduled AND priority BETWEEN :plow AND :phigh
    AND group_key NOT IN (...active...)
  ORDER BY priority, scheduled LIMIT :limit
  FOR UPDATE SKIP LOCKED
)
UPDATE saq_jobs SET status='active' FROM locked_job WHERE saq_jobs.key=locked_job.key RETURNING job;
```

The admin mutations are `UPDATE saq_jobs SET <priority|scheduled> WHERE status='queued' AND key LIKE '<fn>:%'`. Two interleavings, both safe:

1. **Worker locks the row first.** Dequeue holds a `FOR UPDATE` row lock and is about to flip `status='queued'→'active'`. The admin UPDATE's `WHERE status='queued'` matches the row, so it tries to acquire the row lock and **blocks** until the dequeue transaction commits. After commit the row is `status='active'`, so the admin UPDATE re-evaluates its `WHERE status='queued'` and **skips the row** (it no longer qualifies). Net: the admin change never lands on a job that has been picked up. ✔
2. **Admin locks the row first.** The admin UPDATE holds the row lock. The worker's `FOR UPDATE SKIP LOCKED` **skips** the locked row (does not block) and dequeues a different eligible job. After the admin commits, the row is still `status='queued'` with its new priority/scheduled and is dequeued normally on a later poll. ✔

No deadlock (the worker never blocks — it skips), no lost update (the `status='queued'` guard is re-checked under lock), no double-pickup (a job is either dequeued-and-active or queued-and-mutable, never both). **Pause drain** falls out of the same guard: a job already `status='active'` is invisible to the pause UPDATE's `status='queued'` filter, so it runs to completion. This is the entire safety story — document it; do not add application locks.

## Enqueue-Hook Insertion Point + Control-State Caching Decision

### The seam (from Phase 36)
Phase 36 introduces `build_pipeline_queue(name, url, *, cache_redis_url, min_size, max_size)` (`tasks/_shared/queue_factory.py`) which registers two before-enqueue hooks (`apply_project_job_defaults`, `apply_deterministic_key`) and attaches `q.cache_redis`. Phase 37 adds a **third** before-enqueue concern: stamp priority/park. SAQ runs every registered `before_enqueue` callback in `Queue._before_enqueue(job)` immediately before `_enqueue` persists the row (`base.py:355`), and mutations to `job.priority` / `job.scheduled` are written by `_enqueue` (verified: `_enqueue` INSERTs `%(priority)s` and `%(scheduled)s` from the job).

**Insertion point:** register a new hook `apply_stage_control` in `build_pipeline_queue`, AFTER `apply_deterministic_key` (so `job.key`/`job.function` are final). The hook:
1. `fn = job.function`; if `fn` not in `STAGE_TO_FUNCTION.values()`, return (non-stage jobs untouched).
2. Read `(paused, priority)` for that stage from a short-TTL cache.
3. `job.priority = priority`; if `paused`, `job.scheduled = SENTINEL`.

Mirror the existing hooks' best-effort discipline (`queue_defaults.py` / `deterministic_key.py`): any failure logs a warning and returns without mutating — a control-table hiccup must never block an enqueue (the job then enqueues at default priority 50-equivalent / unpaused, which is the safe failure mode).

### Where does the read come from? (import-boundary-aware)
Both contexts that enqueue these three stage jobs have a DB available:
- **API process** (`routers/pipeline.py` → `enqueue_router` → `AgentTaskRouter` per-agent queues) — has SQLAlchemy.
- **Controller worker** (Phase 32 re-enqueue cron → `enqueue_process_file`) — has `ctx["async_session"]`.
- The **agent worker** registers the same hooks (`agent_worker.py:186-190`) but **never enqueues** these stage jobs (it only dequeues/processes), AND is forbidden from importing `sqlalchemy.ext.asyncio` / `phaze.database` (`tests/test_task_split.py`).

Therefore the hook must NOT import the SQLAlchemy engine. **Recommended read path: the queue's own psycopg3 pool** — `pipeline_stage_control` lives in the SAME Postgres database as `saq_jobs` (both reached via `PHAZE_QUEUE_URL`), so the hook can `async with job.queue.pool.connection() as conn` and run a raw `SELECT paused, priority FROM pipeline_stage_control WHERE stage=:s`. This is psycopg3 (not forbidden), works in every process, and needs no SQLAlchemy threading into the factory. The pool is already open by enqueue time (SAQ's `_enqueue` itself uses it).

### Caching: read-through with a short TTL (recommended)
The hook runs on every enqueue, including bulk runs (~11,428 files). A naive read-through adds one `SELECT` per enqueue. Use a tiny in-process TTL cache (a module-level dict `{stage: (paused, priority)}` with a single `expires_at` monotonic timestamp; default **TTL ≈ 3–5s**). A bulk enqueue then collapses to ~1 SELECT per stage per TTL window. Staleness is bounded and harmless: the pause/priority endpoints already issue the bulk `saq_jobs` UPDATE for the EXISTING backlog, so the hook's job is only to stamp NEW jobs — a 3–5s lag before new jobs pick up a just-changed priority is operationally invisible.

- **Cross-process note:** the endpoint (API process) and the hot enqueue paths (API + controller processes) are separate OS processes, so pure in-process invalidation cannot be shared. A short TTL is the robust choice precisely because it does not rely on cross-process invalidation.
- **Alternative considered (NOT recommended for 37):** attach a `stage_control` provider object to the queue at construction (control-side only), mirroring the `cache_redis` attach. Rejected as the primary because reading via `job.queue.pool` is simpler, needs no factory-signature change beyond what 36 already adds, and degrades identically. Document the TTL value as a tunable.

## Architecture Patterns

### System flow

```
OPERATOR (Phase 38 UI / curl)
  │  POST /pipeline/stages/{stage}/priority  {delta}
  │  POST /pipeline/stages/{stage}/pause
  │  POST /pipeline/stages/{stage}/resume
  ▼
FastAPI control router (API process)
  ├─ ORM: UPDATE pipeline_stage_control SET priority|paused, updated_at  (same async session/txn)
  └─ raw: session.execute(text("UPDATE saq_jobs SET ... WHERE status='queued' AND key LIKE :pfx"))
                                   │
                                   ▼
                            POSTGRES  saq_jobs  ◄───────────────┐
                                   ▲                            │  dequeue: ORDER BY priority, scheduled
   NEW enqueue (API + controller)  │                            │          WHERE now>=scheduled
   build_pipeline_queue hooks:     │                            │          FOR UPDATE SKIP LOCKED
     apply_project_job_defaults    │                       Agent worker (drains active jobs)
     apply_deterministic_key  ─────┤  job.key = "<fn>:<id>"
     apply_stage_control      ─────┘  job.priority = ctrl.priority
        reads pipeline_stage_control      job.scheduled = SENTINEL if ctrl.paused
        via job.queue.pool (TTL cache)
```

### Pattern 1: `pipeline_stage_control` model + seeded migration
**What:** A normal app table with a SQLAlchemy model (register in `models/__init__.py` for autogenerate) and an Alembic migration `020` that `revises 019`, creates the table, and **seeds the 3 stage rows** in `upgrade()` (one `op.bulk_insert` / `op.execute(INSERT ...)` with defaults `paused=false, priority=50`). Mirror `scan_batch.py` model conventions and the `019` migration header/`down_revision` pattern.
```python
# models/pipeline_stage_control.py  (mirror scan_batch.py)
class PipelineStageControl(TimestampMixin, Base):
    __tablename__ = "pipeline_stage_control"
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)      # metadata|analyze|fingerprint
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("50"))
    # updated_at comes from TimestampMixin (onupdate=func.now())
```
Use `SmallInteger` to match `saq_jobs.priority` (SMALLINT). Add a CHECK constraint `priority BETWEEN 0 AND 100` (`ck_pipeline_stage_control_priority_range`) so the DB enforces the range even if endpoint clamping is bypassed.

### Pattern 2: priority endpoint (delta, clamp 0–100)
**What:** `POST /pipeline/stages/{stage}/priority` body `{delta: int}` (Phase 38: ▲Higher = -, ▼Lower = +). In one transaction: read current `priority`, compute `new = clamp(current + delta, 0, 100)`, UPDATE the control row, then `UPDATE saq_jobs SET priority=:new WHERE status='queued' AND key LIKE :pfx`. Return `{stage, priority, paused}` (so Phase 38 can re-render). Validate `stage ∈ STAGE_TO_FUNCTION` (404/422 otherwise).
**Why clamp matters:** `priority < 0` falls outside SAQ's `priority BETWEEN 0 AND 32767` dequeue window → the job would silently never dequeue. Clamping at 0 is a correctness guard, not cosmetic.

### Pattern 3: pause / resume (sentinel-guarded)
```sql
-- pause: park the queued backlog; active jobs drain
UPDATE saq_jobs SET scheduled = :SENTINEL
 WHERE status='queued' AND key LIKE :pfx;
-- resume: only un-park what pause parked; never touch a fresh retry backoff
UPDATE saq_jobs SET scheduled = 0
 WHERE status='queued' AND key LIKE :pfx AND scheduled = :SENTINEL;
```
Retry sets `scheduled = time.time() + next_retry_delay` (`postgres.py:_retry`) — a near-future value, never equal to SENTINEL — so the `scheduled=:SENTINEL` guard on resume structurally protects retry backoffs (REQ-37-3). Set `paused` on the control row in the same transaction as the bulk UPDATE.

### Anti-Patterns to Avoid
- **Adding/altering columns on `saq_jobs` or writing an Alembic migration for it.** SAQ owns that table via `init_db()` + its own `saq_versions`. An Alembic migration touching it will collide. `pipeline_stage_control` is a separate app table.
- **Filtering by a non-existent `function` column.** Always `key LIKE '<fn>:%'`.
- **Setting `priority` below 0** (silently un-dequeueable) or above the queue's `priorities` ceiling.
- **Reading the control table via SQLAlchemy inside the before-enqueue hook** — breaks the agent import boundary. Use the queue's psycopg pool.
- **Computing SENTINEL per call / using `now()+huge`** — use one fixed constant so the resume `scheduled=:SENTINEL` guard is exact.
- **Scoping the `saq_jobs` UPDATE by `queue`** — stages span all per-agent queues; key prefix already disambiguates globally.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| No-double-pickup mutual exclusion | App-level lock / advisory-lock dance around admin UPDATE | Postgres row lock via `status='queued'` guard + dequeue's `FOR UPDATE SKIP LOCKED` | Already atomic & deadlock-free; verified vs source |
| Queue backlog reordering | Re-enqueue / delete+insert jobs to change order | Plain `UPDATE saq_jobs SET priority` | Dequeue `ORDER BY priority, scheduled` re-reads live every poll; no requeue needed |
| Pausing a stage | Stop/start the worker, or a custom skip-flag in task code | `scheduled=SENTINEL` park (dequeue gates on `now>=scheduled`) | Drain semantics for free; active jobs finish; no worker restart |
| `saq_jobs` table/migrations | Hand-written DDL for the queue table | SAQ `init_db()` auto-DDL (Phase 36) | SAQ owns its schema + version table |
| Control-table read in hook | New psycopg pool / SQLAlchemy session in the hook | The queue's existing `job.queue.pool` + a small TTL cache | Reuses the open broker pool; import-boundary-safe |

**Key insight:** Phase 37 is almost entirely *data writes against an existing engine*. The hard parts (atomicity, ordering, dedup) are already solved by SAQ's Postgres dequeue. The phase's real surface area is: one app table + one hook + three thin endpoints + the exact key-prefix contract.

## Runtime State Inventory

This phase introduces durable control state and mutates a live queue table. All five categories answered explicitly.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | New `pipeline_stage_control` (3 seeded rows) in the app schema. Live `saq_jobs` rows (status/priority/scheduled) are mutated by the endpoints — but `saq_jobs` is ephemeral broker state (Phase 32 re-enqueue rebuilds it from DB-truth). | Migration 020 creates+seeds the table. No data migration of `saq_jobs` (operator-driven runtime mutation only). |
| **Live service config** | Whether a stage is paused/at-what-priority is now persistent operator intent surviving restarts. On a fresh `saq_jobs` (post-reboot, Phase 32 re-enqueue), newly enqueued jobs re-pick the persisted priority/park via the hook — so pause/priority **survive a queue rebuild** automatically. | None beyond the table; document that pause state persists across reboots and re-applies to re-enqueued jobs. |
| **OS-registered state** | None — no systemd/cron entry embeds stage state. | None — verified by grep (control state lives only in the new table). |
| **Secrets/env vars** | None new. No new DSN/secret; reuses `PHAZE_QUEUE_URL`/`database_url` from Phase 36. | None. |
| **Build artifacts / installed packages** | None — no new dependency (uses existing saq, SQLAlchemy, Alembic, FastAPI). | None. No `Package Legitimacy Audit` needed (no external install). |

**The canonical question:** after the endpoints run, what runtime state still reflects old priority/pause? Answer: only `saq_jobs` rows that the bulk UPDATE's `status='queued'` filter could not reach (already-active jobs — intentionally, they drain) and brand-new jobs (covered by the enqueue hook). Both are by-design. Persisted intent lives in `pipeline_stage_control`.

## Common Pitfalls

### Pitfall 1: paused jobs disappear from Phase-34 `count("queued")` (semantic interaction, not a bug)
**What goes wrong:** `PostgresQueue.count("queued")` is `WHERE status='queued' AND now>=scheduled`. A parked job (`scheduled=SENTINEL`) fails `now>=scheduled`, so it is NOT counted as queued — it falls into `info()`'s "scheduled" bucket (`incomplete - queued - active`). Phase 34's `get_queue_activity` / dashboard "N queued" and the `agent_busy` button-gating will see the parked backlog vanish from "queued."
**Why it happens:** parking via `scheduled` is exactly how SAQ models "scheduled-for-later," which `count("queued")` deliberately excludes.
**How to avoid:** treat this as expected and surface it in Phase 38's UI (show paused count separately). For Phase 37, add a regression assertion that pausing a stage drops its `count("queued")` to 0 while `count("incomplete")` is unchanged — encodes the semantic so a future change can't silently break it.
**Warning signs:** dashboard shows "0 queued" right after a pause despite a large backlog; operator thinks work vanished.

### Pitfall 2: priority below 0 makes jobs un-dequeueable
**What goes wrong:** a delta that drives `priority` negative leaves the job outside `priority BETWEEN 0 AND 32767` (default `priorities`), so the worker silently never picks it up.
**How to avoid:** clamp to `[0, 100]` in the endpoint AND add the DB CHECK constraint. Test the lower bound explicitly.
**Warning signs:** a stage stalls with a full backlog and idle workers after a "▲ Higher priority" spam.

### Pitfall 3: resume clobbering a retry backoff (prevented by the guard — keep it)
**What goes wrong (feared):** resume sets `scheduled=0` and accidentally cancels a job that was legitimately backing off after a failure.
**Reality/avoid:** the `WHERE scheduled=:SENTINEL` guard means resume only touches pause-parked rows; retry backoffs use `now+delay ≠ SENTINEL`. **Do not** simplify resume to "set scheduled=0 for all queued" — that would clobber backoffs. Add a test: enqueue → fail-with-retry (scheduled=now+delay) → pause → resume → assert the retry-backoff job's `scheduled` is unchanged (still future), while a separately parked job is un-parked.
**Warning signs:** failed jobs retrying instantly/repeatedly after a resume.

### Pitfall 4: the enqueue hook breaking the agent import boundary
**What goes wrong:** importing `phaze.database` / a SQLAlchemy session into `apply_stage_control` makes `agent_worker` import it transitively → `tests/test_task_split.py` fails CI.
**How to avoid:** read the control table via `job.queue.pool` (psycopg3) only; keep the hook module free of `sqlalchemy.ext.asyncio` / `phaze.database` imports. Add the new hook module to the import-boundary test's covered surface.
**Warning signs:** `test_agent_worker_does_not_import_phaze_database` fails.

### Pitfall 5: stamping at enqueue races a just-issued endpoint change (bounded, acceptable)
**What goes wrong:** the TTL cache serves a stale priority/paused value for up to TTL seconds, so a job enqueued moments after a pause might not be parked at insert.
**Reality/avoid:** the pause endpoint's bulk `UPDATE ... WHERE status='queued'` will park it on the NEXT operator action, and for a fresh pause the window is ≤ TTL. If stricter is needed, lower the TTL or have the pause/priority endpoint also bump a cheap in-DB `updated_at` the hook checks — but for a single-user admin tool the bounded staleness is acceptable. Document TTL as a tunable; do NOT add cross-process invalidation machinery.
**Warning signs:** a handful of just-enqueued jobs run despite a pause issued <TTL ago.

## Code Examples

### The exact raw UPDATEs (from a SQLAlchemy async session)
```python
# Source: derived from saq/queue/postgres.py dequeue/_retry contract (verified)
from sqlalchemy import text

SENTINEL = 9999999999  # epoch 2286-11-20; far beyond any real `scheduled`
STAGE_TO_FUNCTION = {"metadata": "extract_file_metadata", "analyze": "process_file", "fingerprint": "fingerprint_file"}

async def set_stage_priority(session, stage: str, new_priority: int) -> None:
    pfx = f"{STAGE_TO_FUNCTION[stage]}:%"
    await session.execute(
        text("UPDATE saq_jobs SET priority = :p WHERE status = 'queued' AND key LIKE :pfx"),
        {"p": new_priority, "pfx": pfx},
    )

async def pause_stage(session, stage: str) -> None:
    pfx = f"{STAGE_TO_FUNCTION[stage]}:%"
    await session.execute(
        text("UPDATE saq_jobs SET scheduled = :s WHERE status = 'queued' AND key LIKE :pfx"),
        {"s": SENTINEL, "pfx": pfx},
    )

async def resume_stage(session, stage: str) -> None:
    pfx = f"{STAGE_TO_FUNCTION[stage]}:%"
    await session.execute(
        text("UPDATE saq_jobs SET scheduled = 0 WHERE status = 'queued' AND key LIKE :pfx AND scheduled = :s"),
        {"pfx": pfx, "s": SENTINEL},
    )
```
`:pfx` / `:p` / `:s` are bound parameters (no SQL injection; `stage` is validated against `STAGE_TO_FUNCTION` before building `pfx`).

### The before-enqueue stamping hook (import-boundary-safe)
```python
# Source: new tasks/_shared/stage_control.py — mirrors deterministic_key.py best-effort style
async def apply_stage_control(job: "Job") -> None:
    stage = _FUNCTION_TO_STAGE.get(job.function)          # inverse of STAGE_TO_FUNCTION
    if stage is None:
        return                                            # non-stage jobs untouched
    try:
        paused, priority = await _read_stage_control(job.queue, stage)   # TTL cache over job.queue.pool
    except Exception:
        logger.warning("stage-control read failed; enqueuing unpaused/default", function=job.function, exc_info=True)
        return
    job.priority = priority
    if paused:
        job.scheduled = SENTINEL
```

### Endpoint skeleton (mirrors routers/pipeline.py conventions)
```python
@router.post("/pipeline/stages/{stage}/priority")
async def stage_priority(stage: str, body: StagePriorityDelta, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if stage not in STAGE_TO_FUNCTION:
        raise HTTPException(status_code=422, detail="unknown stage")
    row = await session.get(PipelineStageControl, stage)
    new_priority = max(0, min(100, row.priority + body.delta))
    row.priority = new_priority
    await set_stage_priority(session, stage, new_priority)
    await session.commit()
    return {"stage": stage, "priority": new_priority, "paused": row.paused}
```
(Phase 38 wires HTMX; Phase 37 ships the endpoints + a JSON return. Keep both an `/api/...`-style JSON and/or HTMX-fragment variant consistent with the existing dual pattern in `pipeline.py`, but the scope only requires the control endpoints.)

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Redis broker (no per-job priority/scheduled control) | Postgres broker with native `priority` + `scheduled` | Phase 36 | Makes Phase 37's raw UPDATEs possible at all |
| Random-uuid job keys | Deterministic `<function>:<file_id>` keys | Phase 35 | Enables the `key LIKE '<fn>:%'` stage filter (no function column) |
| Worker stop/start to pause work | `scheduled=SENTINEL` park + drain | Phase 37 | Per-stage, online, drain-safe pause |

**Deprecated/outdated:** none specific to this phase. Note `tasks/proposal.py`'s `queue.redis` borrow and the counter hooks' `getattr(job.queue,"redis",...)` are a Phase 36 concern (already covered there), not Phase 37.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Stage filter via `key LIKE '<fn>:%'` is correct because Phase 35 deterministic keys are universally applied at the enqueue chokepoint | Stage→Key Map | If any stage job slips an undeterministic key (pre-Phase-35 leftover in the queue), it escapes the filter. Phase 35 + 36 cold queue make this safe, but confirm no legacy-key jobs persist post-cutover |
| A2 | `SENTINEL = 9999999999` never collides with a legitimate `scheduled` (retry/cron) | SENTINEL value | If a future feature schedules jobs beyond year 2286 (implausible), resume's guard could misfire. Negligible |
| A3 | Reading `pipeline_stage_control` via `job.queue.pool` inside the hook is acceptable on the hot enqueue path (with TTL cache) | Caching decision | If enqueue throughput is far higher than expected, even cached reads could add latency — TTL tunable mitigates. Confirm acceptable in discuss-phase |
| A4 | A 3–5s control-state staleness window for NEW enqueues is operationally acceptable | Pitfall 5 | If the operator expects instantaneous pause of in-flight enqueues, lower TTL or add updated_at check |
| A5 | Pause persisting across reboots (re-applied to Phase-32 re-enqueued jobs) is the desired behavior | Runtime State Inventory | If the operator expects pause to reset on restart, the hook re-parking re-enqueued jobs would surprise them. Confirm intent |
| A6 | The before-enqueue hook should live in all 4 construction sites but only fire meaningfully where DB is reachable (API + controller); agent never enqueues stage jobs | Insertion Point | If a future agent-side enqueue of a stage task is added, the hook reads via the agent's queue pool (still works, since agent reaches Postgres post-Phase-36) — verify |

## Open Questions (RESOLVED)

> Resolved during planning (2026-06-12). The user chose "continue without context" (no discuss-phase); each was settled by adopting the recommended default and documenting it in the plans. Back-annotated per plan-checker Dimension 11.
>
> - **Q1 (priority endpoint):** RESOLVED — Plan 37-04: delta-based `POST /pipeline/stages/{stage}/priority` with body `{delta: int}`, default step ±10, clamp `[0,100]`, returns new absolute value.
> - **Q2 (cache TTL):** RESOLVED — Plan 37-02: in-process TTL of 5s on the `pipeline_stage_control` read in `apply_stage_control`.
> - **Q3 (pause persists across reboots):** RESOLVED — YES. Intent persists in `pipeline_stage_control`; the enqueue hook re-parks Phase-32 re-enqueued jobs (Plan 37-02 + README note).
> - **Q4 (resume restore priority?):** RESOLVED — un-park only (`scheduled` reset), priority left as-stamped — matches scope (Plan 37-02/37-04).

1. **Priority endpoint contract: delta vs absolute, and request shape.**
   - What we know: ROADMAP says "delta"; Phase 38 UI has ▲Higher (decrement) / ▼Lower (increment) steppers.
   - What's unclear: step size (±1? ±5? ±10?), whether the body is `{delta}` or the path encodes direction, and whether an absolute-set endpoint is also wanted.
   - Recommendation: ship `POST /pipeline/stages/{stage}/priority` with `{delta: int}`, clamp `[0,100]`, return the new absolute value. Confirm step size with operator (default ±10 gives 10 discrete levels across 0–100).

2. **Control-state cache TTL.**
   - What we know: a short TTL (3–5s) collapses bulk-enqueue reads and bounds staleness.
   - What's unclear: exact value; whether the operator wants near-instant new-job parking on pause.
   - Recommendation: default TTL 5s, expose as a config knob; revisit only if UAT shows lag complaints.

3. **Does pause persist across reboots and re-apply to re-enqueued jobs?** (A5)
   - Recommendation: YES (intent persists in `pipeline_stage_control`; the hook re-parks re-enqueued jobs). Confirm this is the desired operator model in discuss-phase; it is the more useful default for a long-running archive job.

4. **Should resume restore the original priority on parked jobs, or just un-park?**
   - What we know: scope says resume sets `scheduled=0` (un-park) only; priority was already stamped at enqueue and is independently adjustable.
   - Recommendation: un-park only (as scoped). Priority is orthogonal — no action needed. Note for the planner.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (broker + ORM, same DB) | `saq_jobs` UPDATEs + `pipeline_stage_control` | ✓ (compose) | 16+ | — |
| saq (Postgres backend) | `saq_jobs` schema + dequeue semantics | ✓ | 0.26.4 | — (Phase 36 delivers the Postgres backend) |
| SQLAlchemy + Alembic | new model + migration 020 + raw `text()` UPDATE | ✓ | 2.0.x / 1.18.x | — |
| FastAPI | control endpoints | ✓ | existing | — |
| psycopg3 (queue pool) | hook reads control table via `job.queue.pool` | ✓ (after Phase 36) | >=3.2.0 | — |

**Hard dependency on Phase 36:** this phase cannot land before Phase 36 (it assumes `saq_jobs` exists on Postgres, the `build_pipeline_queue` factory, and the per-agent queues are `PostgresQueue` with reachable pools). The `key LIKE` filter, the `scheduled` park, and the hook's `job.queue.pool` read all require the Postgres backend.

**Missing dependencies with no fallback:** none (all satisfied once Phase 36 ships).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_stage_control.py tests/test_task_split.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |
| Integration DB | `just integration-test` / `just test-db` (dedicated local PG, quick task 260520-bcl) — required for the real-PG UPDATE/dequeue tests |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| REQ-37-1 | pause parks queued backlog (`scheduled=SENTINEL`); active job drains (untouched) | integration (real PG) | `uv run pytest tests/integration/test_stage_pause.py -x` | ❌ Wave 0 |
| REQ-37-1 | paused stage `count("queued")`→0 while `count("incomplete")` unchanged (Pitfall 1 semantic) | integration | `uv run pytest tests/integration/test_stage_pause.py -x` | ❌ Wave 0 |
| REQ-37-2 | priority UPDATE reorders dequeue (lower picked first); clamp `[0,100]` | integration | `uv run pytest tests/integration/test_stage_priority.py -x` | ❌ Wave 0 |
| REQ-37-3 | resume un-parks only SENTINEL rows; a retry-backoff (`scheduled=now+delay`) job is untouched | integration | `uv run pytest tests/integration/test_stage_resume.py -x` | ❌ Wave 0 |
| REQ-37-4 | concurrent admin UPDATE vs dequeue: no double-pickup, no deadlock (status guard + SKIP LOCKED) | integration | `uv run pytest tests/integration/test_stage_concurrency.py -x` | ❌ Wave 0 |
| REQ-37-1/2 | enqueue hook stamps priority + parks when paused; non-stage jobs untouched; best-effort on read failure | unit (direct hook call, fake queue/pool) | `uv run pytest tests/test_stage_control.py -x` | ❌ Wave 0 |
| guard | stage endpoints validate `stage ∈ {metadata,analyze,fingerprint}`; delta clamps; returns `{stage,priority,paused}` | unit (httpx AsyncClient) | `uv run pytest tests/test_routers/test_stage_endpoints.py -x` | ❌ Wave 0 |
| guard | new hook module does NOT pull `phaze.database`/`sqlalchemy.ext.asyncio` into agent_worker | subprocess | `uv run pytest tests/test_task_split.py -x` | ⚠️ extend covered surface |
| schema | migration 020 upgrade creates table + seeds 3 rows + CHECK; downgrade drops cleanly | migration | `uv run pytest tests/test_migrations/test_020.py -x` (or `alembic upgrade/downgrade` smoke) | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_stage_control.py tests/test_task_split.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` plus the real-PG integration tests via `just integration-test`.
- **Phase gate:** full suite + integration green before `/gsd:verify-work`; ≥85% coverage on the new modules.

### Wave 0 Gaps
- [ ] `src/phaze/models/pipeline_stage_control.py` + register in `models/__init__.py`
- [ ] `alembic/versions/020_add_pipeline_stage_control.py` — table + 3 seed rows + `priority` CHECK
- [ ] `src/phaze/tasks/_shared/stage_control.py` — `apply_stage_control` hook + `STAGE_TO_FUNCTION` + TTL-cached `_read_stage_control(queue, stage)` + `SENTINEL`
- [ ] `src/phaze/services/stage_control.py` (or in the router) — `set_stage_priority` / `pause_stage` / `resume_stage` raw-UPDATE helpers
- [ ] `src/phaze/routers/pipeline_stages.py` (or extend `routers/pipeline.py`) — the 3 endpoints
- [ ] `tests/test_stage_control.py` — hook unit tests (fake queue/pool)
- [ ] `tests/integration/test_stage_{pause,priority,resume,concurrency}.py` — real-PG semantics
- [ ] `tests/test_routers/test_stage_endpoints.py` — endpoint validation/clamp/return-shape
- [ ] Extend `tests/test_task_split.py` to cover the new hook module's import boundary
- [ ] Register `apply_stage_control` in `build_pipeline_queue` (touches the Phase 36 factory) — and confirm all 4 construction sites inherit it

## Security Domain

`security_enforcement` is not configured in `.planning/config.json` (treat as enabled).

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No new auth surface; control endpoints sit behind the same reverse-proxy internal-realm auth as the rest of `phaze-api` (LOCKED, T-33-03) |
| V3 Session Management | no | — |
| V4 Access Control | yes (infra) | Endpoints are operator-only via the reverse proxy; no app-layer auth added (consistent with existing `/pipeline/*`) |
| V5 Input Validation | yes | `stage` validated against `STAGE_TO_FUNCTION` (reject unknown → 422); `delta` is an int, result clamped `[0,100]`; all `saq_jobs` UPDATEs use bound params (no string interpolation of user input into SQL) |
| V6 Cryptography | no | No secrets handled in this phase |

### Known Threat Patterns for the raw-UPDATE control plane
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via `stage`/`delta` into `saq_jobs` UPDATE | Tampering | Validate `stage` against an allowlist before building the `key LIKE` prefix; bind all params via SQLAlchemy `text(...)` params (mirror `019` migration's "no user input interpolated" note) |
| Priority/pause used to silently stall the pipeline (mis-set) | Denial of Service | Clamp `[0,100]` + DB CHECK so priority stays dequeueable; Pitfall-1 regression test ensures pause is observable, not silent; Phase 38 UI surfaces paused state |
| Mutating a job mid-dequeue (corruption / double-run) | Tampering | Postgres row lock + `status='queued'` guard + `FOR UPDATE SKIP LOCKED` (Concurrency Safety) — no app lock needed |
| Hook import pulling DB layer into the agent | Elevation/boundary break | Read control table via `job.queue.pool` only; covered by `test_task_split.py` |

## Sources

### Primary (HIGH confidence)
- Installed `saq==0.26.4` source: `saq/queue/postgres_migrations.py` (full `saq_jobs`/`saq_stats` DDL — verified column names/types/indexes), `saq/queue/postgres.py` (`_dequeue` 636-698 ORDER BY priority,scheduled + FOR UPDATE SKIP LOCKED; `count` 303-351 `now>=scheduled` gate; `_enqueue` 700-755 priority/scheduled write + ON CONFLICT dedup; `_retry` 816-823 `scheduled=now+delay`; `info` 245-301; `__init__` 90-150 `priorities=(0,32767)`), `saq/queue/base.py` (`enqueue`/`_before_enqueue` 314-360, 529-531), `saq/job.py` (Job fields: `priority:int=0`, `scheduled:int=0`, `function:str`, `key`)
- Project code: `src/phaze/tasks/_shared/deterministic_key.py` (`_KEY_BUILDERS` — stage→key mapping), `tasks/_shared/queue_defaults.py` (hook conventions), `routers/pipeline.py` (endpoint + enqueue conventions), `services/agent_task_router.py` (per-agent queue construction), `tasks/controller.py` (ctx session/queue), `tasks/agent_worker.py` (import boundary), `models/scan_batch.py` + `models/base.py` (model conventions), `models/__init__.py` (registration), `alembic/versions/019_*` (migration header/seed/`down_revision` pattern), `tests/test_task_split.py` (forbidden imports), `src/phaze/config.py` (SECRET_FILE_FIELDS, settings)
- Phase 36 artifacts: `36-RESEARCH.md` (queue substrate, `build_pipeline_queue`, `cache_redis` attach, `PHAZE_QUEUE_URL`), `36-01-PLAN.md` (factory signature `build_pipeline_queue(name, url, *, cache_redis_url, min_size, max_size)`)
- `.planning/ROADMAP.md` Phase 37 §271-289, `.planning/STATE.md` (Phases 36/37/38 accumulated context), auto-memory `project_stage_pause_priority_design`

### Secondary (MEDIUM confidence)
- Project CLAUDE.md technology stack + constraints; auto-memory entries (`project_arq_to_saq`, `project_observability_incident_5pr`, `project_default_queue_misrouting`)

### Tertiary (LOW confidence)
- None — every critical claim verified against installed SAQ source or project code.

## Metadata

**Confidence breakdown:**
- `saq_jobs` schema + dequeue/park/priority semantics: HIGH — read directly from `postgres_migrations.py` + `postgres.py`
- Stage→function→key-prefix mapping: HIGH — `_KEY_BUILDERS` + task defs cross-checked
- No-double-pickup concurrency argument: HIGH — derived from `_dequeue` FOR UPDATE SKIP LOCKED + status guard
- Enqueue-hook insertion point + import-boundary safety: HIGH — factory seam (36-01-PLAN) + `test_task_split.py` confirmed
- Caching TTL / endpoint delta-step / pause-persistence intent: MEDIUM — operator preferences (Open Questions 1–4)

**Research date:** 2026-06-12
**Valid until:** 2026-07-12 (stable; re-verify if saq is bumped past 0.26.x or Phase 36's factory signature changes)
