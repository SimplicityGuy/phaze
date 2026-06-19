<!-- GSD:GENERATED -->
# Phase 45: Scheduling Ledger for Orphan Recovery - Research

**Researched:** 2026-06-18
**Domain:** SAQ job lifecycle hooks, control-vs-agent DB boundary, Postgres durable ledger, Alembic migration
**Confidence:** HIGH (all findings grounded in actual repo + installed `saq==0.26.4` source; cited file:line)

## Summary

The ledger is implementable with the existing single-chokepoint architecture, but the locked
"clear at `increment_completed` (after_process)" site has a hard structural limit: **the four
AGENT stages run their `after_process` on the Postgres-free agent worker, which cannot write a
control-side Postgres ledger.** All enqueues, by contrast, already originate control-side
(verified: no agent task calls `queue.enqueue`), so the ledger WRITE is naturally
control-side-only. The CLEAR splits cleanly: controller-stage jobs clear at the controller's
`after_process` hook (it has `ctx["async_session"]`); agent-stage jobs clear in the control-side
HTTP callback handlers the agent already calls on BOTH success and terminal failure
(`agent_analysis.py` has `PUT /{file_id}` AND `POST /{file_id}/failed`).

SAQ's terminal-failure hook question resolves favorably: `Worker.process()` calls
`_after_process` in a `finally` block (`worker.py:434`) after EVERY outcome — `COMPLETE`,
`FAILED`, `ABORTED`, and even a retry. The in-memory `job.status` distinguishes them: `finish()`
sets `job.status = <terminal>` (`queue/base.py:296`), `retry()` sets `job.status = Status.QUEUED`
(`queue/base.py:276`). So a single `after_process` hook clears on `job.status in
TERMINAL_STATUSES` and correctly leaves a retrying job alone — **clear-on-success and
clear-on-terminal-failure are ONE hook, not two** (locked decision #1), for the stages that run
their `after_process` on the controller.

**Primary recommendation:** Add a Postgres `scheduling_ledger` table (PK = deterministic
`job.key`, plus task name, routing hint, full re-enqueue payload JSONB, timestamps). WRITE it
from a new control-side capability folded into the existing `before_enqueue` chokepoint
(`apply_deterministic_key`), gated on a DB handle attached to the queue (symmetric with
`cache_redis`) so the agent's never-fired hook degrades to no-op. CLEAR controller stages at the
controller `after_process`; CLEAR agent stages in the existing agent-callback endpoints (success
+ failed). Rewrite `recover_orphaned_work` to drive off `ledger − live saq_jobs keys`, replaying
the stored payload through the SAME keyed producers. Backfill via a one-time idempotent startup
reconcile, not a data migration.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
1. **Terminal `failed` clears the ledger (NO poison re-queue).** A job that exhausts its retry
   budget and goes `failed` has its ledger entry cleared — recovery never re-queues `failed`
   jobs. Requires hooking SAQ's terminal-failure path, not just the success completion hook.
2. **Ledger tracks ALL keyed job types** — every job through the `before_enqueue`
   deterministic-key chokepoint (all 8 stages: process_file, extract_file_metadata,
   fingerprint_file, scan_live_set, generate_proposals, search_tracklist,
   scrape_and_store_tracklist, match_tracklist_to_discogs).
3. **Backfill from live `saq_jobs` at deploy.** On first startup after the migration, seed the
   ledger from current `queued`/`active` `saq_jobs` rows. One-time, idempotent (keyed by
   deterministic key).

### Claude's Discretion
- Exact ledger schema columns, clear-mechanism wiring, backfill-as-migration-vs-startup,
  recovery query shape — recommended below.

### Deferred Ideas (OUT OF SCOPE)
- Bug A (Anthropic key → litellm) — shipped in PR #145.
- Bug B (nox panako/audfprint host alias) — homelab deploy fix, separate.
- Cloud-burst analysis — roadmap backlog.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| L-01 | Durable ledger written at the single `before_enqueue` chokepoint | §1 chokepoint, §5 schema/migration |
| L-02 | Ledger cleared on completion AND terminal failure | §2 SAQ hook mechanism (one hook), §7 agent-vs-control split |
| L-03 | Recovery re-queues `ledger − live saq_jobs keys` via existing keyed producers | §3 live-keys query, §4 recovery rewrite |
| L-04 | Backfill from live `saq_jobs` on first startup, idempotent | §5 backfill recommendation |
| L-05 | Control-only boundary preserved; agent worker stays Postgres-free | §7 pitfall resolution |
| L-06 | New reversible Alembic migration (async template); 85% coverage | §5 migration, §6 tests |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Ledger WRITE (on enqueue) | Control (API + controller worker) | — | All enqueues originate control-side; verified no agent task calls `queue.enqueue` |
| Ledger CLEAR (controller stages) | Controller worker `after_process` | — | Controller worker has `ctx["async_session"]` |
| Ledger CLEAR (agent stages) | Control API (agent-callback endpoints) | — | Agent worker is Postgres-free; agent reports success+failure over HTTP |
| Ledger table + reads | Control-side Postgres | — | Durable outside `saq_jobs`; control-only module boundary |
| Recovery producer | Controller worker | — | Needs Postgres + `task_router`; `reenqueue.py` is control-only |

## Standard Stack

No new external packages. Everything is in-repo:

| Component | Location | Role in Phase 45 |
|-----------|----------|------------------|
| `PostgresQueue` (broker) | `saq==0.26.4` | Owns `saq_jobs`; ledger lives OUTSIDE it |
| `before_enqueue` chokepoint | `tasks/_shared/deterministic_key.py:86` `apply_deterministic_key` | Add ledger upsert here |
| `after_process` hook | `tasks/_shared/deterministic_key.py:118` `increment_completed` | Add controller-stage ledger clear here |
| Hook registration | `tasks/_shared/queue_factory.py:67-74` (`register_before_enqueue`) + `tasks/controller.py:171` / `tasks/agent_worker.py:213` (`after_process` kwarg) | Unchanged seam |
| Recovery producer | `tasks/reenqueue.py:187` `recover_orphaned_work` | Rewrite to drive off ledger |
| Keyed producers | `services/analysis_enqueue.py:43` `enqueue_process_file`; controller `queue.enqueue(...)` | Reuse verbatim |
| Migration template | `alembic/versions/020_add_pipeline_stage_control.py` | Mirror for the new table |

**Package Legitimacy Audit:** Not applicable — no external packages installed this phase.

---

## §1. The `before_enqueue` chokepoint (the WRITE site)

**File:** `src/phaze/tasks/_shared/deterministic_key.py`

- `apply_deterministic_key(job)` (`:86`) is the `before_enqueue` hook. For any function in
  `_KEY_BUILDERS` (`:74-83`, exactly the 8 stages), it sets `job.key =
  f"{job.function}:{builder(job.kwargs or {})}"` (`:103`) UNCONDITIONALLY, then best-effort INCRs
  the enqueued counter via `getattr(job.queue, "cache_redis", None)` (`:110-115`). A function
  absent from `_KEY_BUILDERS` returns early (`:100-101`) and keeps SAQ's random key. `[VERIFIED: codebase]`
- `increment_completed(ctx)` (`:118`) is the `after_process` hook. It reads `ctx["job"]`, returns
  unless `job.status == Status.COMPLETE` (`:125-126`) and `job.function in _KEY_BUILDERS`
  (`:128-129`), then INCRs the completed counter via `cache_redis` (`:131-133`). `[VERIFIED: codebase]`

**Where the ledger upsert slots in:** immediately after `job.key` is finalized at `:103`, before
the counter INCR. `job.key`, `job.function`, and `job.kwargs` are all final at that point. The
upsert payload = `(key=job.key, function=job.function, kwargs=job.kwargs)`. This is the SINGLE
universal write — every keyed enqueue passes through here.

**Hook registration is universal across BOTH roles:** `build_pipeline_queue` (`queue_factory.py:66-74`)
registers `apply_deterministic_key` as `before_enqueue` on EVERY queue it builds, and it is the
only construction seam. It is used by: the controller queue (`tasks/controller.py:163`), the agent
worker queue (`tasks/agent_worker.py:206`), and `AgentTaskRouter._queue_for`
(`services/agent_task_router.py`, via `build_pipeline_queue`). So the hook is on the controller
queue AND every per-agent queue. `[VERIFIED: codebase]`

**BUT the hook only *fires* where `enqueue()` is actually called.** Verified: no agent-side task
function (`tasks/functions.py`, `tasks/metadata_extraction.py`, `tasks/fingerprint.py`,
`tasks/scan.py`) calls `queue.enqueue` — grep returned nothing. Every producer lives control-side
(`routers/pipeline.py`, `routers/scan.py`, `routers/tracklists.py`, `services/enqueue_router.py`,
`services/agent_task_router.py`, `services/analysis_enqueue.py`). **Conclusion: the ledger WRITE
always executes in the control process** (the API process for manual triggers via
`AgentTaskRouter`; the controller worker for recovery/startup). The agent worker's registered
`before_enqueue` hook is dead code in practice — it never enqueues. This is the key that makes a
Postgres ledger write feasible without violating the agent boundary. `[VERIFIED: codebase]`

### How to give the WRITE hook DB access

`before_enqueue` only receives `job`; the DB handle must hang off the queue object, exactly as
`cache_redis` does (`queue_factory.py:79`, read via `getattr(job.queue, "cache_redis", None)`).
**Recommendation:** attach a `ledger_sessionmaker` (an `async_sessionmaker`) to the control-side
queues and read it in the hook via `getattr(job.queue, "ledger_sessionmaker", None)`. When absent
(agent queue, or a test fake) the hook degrades to a logged no-op — identical discipline to the
counter INCR's `try/except` (`:113-115`). Attach it:
- on the controller queue + `ctx["queue"]` in `tasks/controller.py:startup` (it already builds
  `task_engine` at `:66`),
- on each `AgentTaskRouter` per-agent queue (the router is constructed in the API lifespan and in
  `controller.startup:105` — it can take a sessionmaker and set it on each `_queue_for` queue).

This keeps the write Postgres-side and control-only while leaving `deterministic_key.py` in
`_shared` (it stays import-clean — it only does `getattr`, never imports `phaze.database`).

---

## §2. SAQ terminal-failure hook (CRITICAL — locked decision #1)

**Installed version:** `saq==0.26.4` (`.venv/.../saq`). `[VERIFIED: installed package]`

**Mechanism — `after_process` DOES fire on terminal failure.** `Worker.process()`
(`saq/worker.py:341`) wraps the whole job lifecycle and runs `_after_process(context)` in a
`finally` block at `worker.py:434-437`. The terminal outcomes set in the body:

| Path | `worker.py` line | `job.finish(...)` / `retry` | Resulting in-memory `job.status` |
|------|------------------|------------------------------|----------------------------------|
| Success | `:377` | `finish(Status.COMPLETE)` | `COMPLETE` |
| Aborted | `:385` | `finish(Status.ABORTED)` | `ABORTED` |
| Exception, retries left | `:419` | `retry(error)` | `QUEUED` (re-enqueued, NOT terminal) |
| Exception, retries exhausted | `:421` | `finish(Status.FAILED)` | `FAILED` |
| Timeout | raises → `:418-421` | retry or `FAILED` | `QUEUED` or `FAILED` |

`finish()` sets `job.status = status` (`queue/base.py:296`); `retry()` sets `job.status =
Status.QUEUED` (`queue/base.py:276`). `after_process` runs AFTER both, in `finally`. So inside
the hook, `job.status` is the authoritative terminal/non-terminal signal. `[VERIFIED: installed package]`

`saq.job` defines `TERMINAL_STATUSES = {Status.COMPLETE, Status.FAILED, Status.ABORTED}`
(`job.py:41`). `retryable` is `self.retries > self.attempts` (`job.py:261`). `[VERIFIED: installed package]`

**Therefore: ONE hook, not two.** There is no separate `on_failure`/`on_abort` worker hook in
SAQ 0.26.4 — `after_process` is the universal post-job callback. The existing `increment_completed`
already lives there. Extend it (or add a sibling `after_process` callable — `Worker.__init__`
accepts a list via `ensure_coroutine_function_many`, `worker.py:116`) to clear the ledger when
`job.status in TERMINAL_STATUSES`, and crucially do NOT clear when `job.status == Status.QUEUED`
(a retry is still scheduled — its ledger entry must survive). This single predicate satisfies both
clear-on-success and clear-on-terminal-failure.

> **Hard constraint:** this clear path only reaches Postgres on the CONTROLLER worker (it has
> `ctx["async_session"]`). On the AGENT worker `after_process` runs Postgres-free, so the
> agent-stage clears CANNOT happen here — see §7.

---

## §3. The `saq_jobs` broker schema — querying "live keys"

`saq_jobs` is SAQ-owned (created by `PostgresQueue.init_db()`, NOT Alembic — never reference it in
a migration; see `020`'s CRITICAL banner). Relevant columns used in-repo: `key` (the deterministic
`<function>:<natural_id>`), `status`, `queue`, plus a serialized `job` BYTEA blob. `[VERIFIED: codebase]`

**Existing patterns (count, not key-set):**
- `count_inflight_jobs` (`services/pipeline.py:734`) runs `_INFLIGHT_COUNT_SQL` (`:731`):
  `SELECT COUNT(*) FROM saq_jobs WHERE status IN ('queued', 'active')`, inside a SAVEPOINT
  (`begin_nested()`), degrade-to-0 on error. `[VERIFIED: codebase]`
- `_STAGE_BUSY_SQL` (`:322`): `SELECT split_part(key, ':', 1) AS fn, COUNT(*) ... GROUP BY fn` —
  the established "bucket by key prefix" idiom. `[VERIFIED: codebase]`

**What Phase 45 needs (the actual KEY SET):** a sibling helper in `services/pipeline.py`:

```python
_LIVE_KEYS_SQL = text("SELECT key FROM saq_jobs WHERE status IN ('queued', 'active')")

async def get_live_job_keys(session: AsyncSession) -> set[str]:
    """Return the set of saq_jobs keys currently queued/active. Degrade-safe (SAVEPOINT)."""
```

Mirror the SAVEPOINT + degrade discipline of `count_inflight_jobs` (`:750-756`). Parked/paused
jobs keep `status='queued'` (they ARE live — same note as `:739-740`), so they are correctly
excluded from the orphan set.

**`saq_jobs` terminal rows are NOT durable** — do not use them as the clear signal. Default
`Job.ttl = 600` (10 min, `saq/job.py:124`); `_finish` sets `expire_at = now + ttl`
(`queue/postgres.py:837-839`) and `sweep()` (`:357`) `DELETE`s expired rows. A `COMPLETE`/`FAILED`
row vanishes ~10 min after the job ends, so "ledger key with no `saq_jobs` row" cannot
distinguish "done-and-swept" from "lost". This is precisely why the ledger needs its OWN durable
clear (§2/§7), and why recovery's exclusion set is "live keys" (queued/active), not "terminal
keys". `[VERIFIED: installed package]`

---

## §4. The recovery producer rewrite

**File:** `src/phaze/tasks/reenqueue.py` (control-only; module banner `:1-38`).

**Today:** `recover_orphaned_work` (`:187`) gates on `count_inflight_jobs == 0` (`:220-225`),
then reconciles all 8 stages from the **complement-of-done** pending-set queries
(`_reconcile_controller_stages:123`, `_reconcile_agent_stages:162`). This is the bug: those
queries (`get_files_by_state(DISCOVERED)`, `get_metadata_pending_files`, `get_untracked_files`,
etc.) include never-scheduled work. `force=True` makes that sweep unconditional. `[VERIFIED: codebase]`

**Rewrite to drive off the ledger:**

```
orphaned_rows = [row for row in ledger if row.key not in live_saq_jobs_keys]
```

Then replay each `orphaned_row` through the SAME keyed producer it was originally enqueued by,
using the **stored payload** (NOT a re-derived pending set). The ledger row already carries
`function` + `kwargs` (the original `model_dump(mode="json")` payload), so recovery reconstructs
the enqueue exactly:

| Ledger `function` | Replay path (reuse existing producer) | Routing |
|-------------------|----------------------------------------|---------|
| `process_file` | `enqueue_process_file(agent_queue, ...)` OR `agent_queue.enqueue("process_file", key=..., **kwargs)` | agent queue via `select_active_agent` + `task_router.queue_for` |
| `extract_file_metadata` / `fingerprint_file` / `scan_live_set` | `agent_queue.enqueue(function, **kwargs)` | agent queue |
| `generate_proposals` / `search_tracklist` / `scrape_and_store_tracklist` / `match_tracklist_to_discogs` | `ctx["queue"].enqueue(function, **kwargs)` | controller queue |

Because the stored `kwargs` is the full original payload, the `extra="forbid"` agent schemas
(`ProcessFilePayload` etc.) still validate and nothing dead-letters — exactly what the current
`_reconcile_agent_payloads:106` achieves, but now sourced from the ledger instead of rebuilt from
`FileRecord`. The `before_enqueue` hook re-stamps the identical `job.key`, so the deterministic-key
dedup still collapses any still-live item to a `None`/skip (Phase-32 idempotency, preserved).

**What a ledger row must store to re-enqueue correctly (answers the §4 question directly):**
1. `key` (PK) — the deterministic dedup key.
2. `function` — the task name to enqueue.
3. `kwargs` (JSONB) — the COMPLETE payload (`job.kwargs`), so agent stages get the full
   `ProcessFilePayload`/`ExtractMetadataPayload`/etc., not just the natural id.
4. `routing` hint — `"agent"` vs `"controller"`. Derivable from `function`
   (`enqueue_router.AGENT_TASKS` vs `CONTROLLER_TASKS`, `:44-69`), so this column is optional but
   recommended for an explicit, testable replay (avoids re-importing the routing sets into the
   recovery loop).

**Routing recovery detail:** agent replays need a live agent. Keep the existing
`select_active_agent` + `NoActiveAgentError` skip-with-warning behavior (`:231-239`): if no agent
is online (cold boot), skip agent-routed ledger rows (leave them in the ledger for the next
recovery) and reconcile controller-routed rows. The `process_file` analyze payload already carries
`agent_id`/`models_path` in its stored `kwargs`, but the LIVE active agent should own the replay —
re-stamp `agent_id` from `select_active_agent` if the original agent is now revoked (discuss flag).

**`force` semantics flip (per CONTEXT):** `force=True` no longer means "sweep the domain backlog";
it means "reconcile the ledger now" (bypass only the `count_inflight_jobs==0` no-op gate). The
manual "Recover" button and the startup hook both still call this one function — no drift
(`controller.py:118-122` startup call unchanged in shape). The complement-of-done helpers
(`get_metadata_pending_files` et al.) are NO LONGER read by recovery; they remain in use by the
manual DAG triggers (`routers/pipeline.py`) and stay untouched there. `[VERIFIED: codebase]`

---

## §5. Models + migration

**Models live in** `src/phaze/models/`, all on `Base` + optional `TimestampMixin`
(`models/base.py:18-28`). Register the new model in `models/__init__.py` (the import is what makes
Alembic autogenerate + metadata-create see it). Closest template: `PipelineStageControl`
(`models/pipeline_stage_control.py`) — a standalone non-`saq_jobs` app table with a string PK and
`TimestampMixin`. `[VERIFIED: codebase]`

### Recommended `scheduling_ledger` schema

```python
class SchedulingLedger(TimestampMixin, Base):
    __tablename__ = "scheduling_ledger"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)   # "<function>:<natural_id>"
    function: Mapped[str] = mapped_column(String(64), nullable=False) # task name
    routing: Mapped[str] = mapped_column(String(16), nullable=False)  # "agent" | "controller"
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)      # full job.kwargs to replay
    enqueued_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    # created_at / updated_at from TimestampMixin
```

- PK on `key` makes the WRITE an idempotent `INSERT ... ON CONFLICT (key) DO UPDATE` (Postgres
  `pg_insert(...).on_conflict_do_update`, the established idiom — see `agent_analysis.py:154-163`).
  A re-enqueue of a still-scheduled key just refreshes `payload`/`enqueued_at`.
- The CLEAR is `DELETE FROM scheduling_ledger WHERE key = :key` (or a batch `key IN (...)`).
- No FK to `files`/`tracklists`: a ledger row must survive even if its target row is mid-flight;
  the natural id is inside `payload`. Add a plain index on `function` for per-stage diagnostics
  (optional).
- `String(255)` PK is safe: keys are `<function>:<uuid>` (≤ ~64 chars) or
  `generate_proposals:<sha256hex>` (`deterministic_key.py:57-67`, 64 hex chars) → ~80 max.

### Migration

New revision **`022`**, `down_revision = "021"` (021 is the current head —
`alembic/versions/021_add_analysis_coverage_columns.py:36-37`). `[VERIFIED: codebase]`

Mirror `020`'s structure exactly (`op.create_table` with explicit
`created_at`/`updated_at` server_default columns + `PrimaryKeyConstraint`; `downgrade()` drops the
table). **Do NOT reference `saq_jobs`** in the migration (020's CRITICAL banner, `:14-17`). The
migration is purely additive and reversible — no data migration.

### Backfill (locked decision #3) — recommend STARTUP RECONCILE, not data migration

**Recommendation: a one-time idempotent startup reconcile, NOT an Alembic data step.** Rationale:
1. An Alembic `upgrade()` data step that reads `saq_jobs` directly couples the migration to
   SAQ-owned schema (forbidden by the 020 banner) and to a live broker.
2. The backfill must read each `saq_jobs` row's serialized `job` blob to recover `function` +
   `kwargs` (the blob is JSON — `pipeline.py:782-802` already deserializes `saq_jobs.job` blobs
   for the straggler detector; reuse that pattern). That's runtime logic, not DDL.
3. It must be idempotent and re-runnable (keyed by deterministic `key` → `ON CONFLICT DO
   NOTHING`), which a startup reconcile naturally is; an Alembic step runs once and can't re-cover
   a window.

Implement as a control-side function (e.g. `backfill_ledger_from_saq_jobs(session)` in
`reenqueue.py` or `services/pipeline.py`) invoked once in `controller.startup` BEFORE
`recover_orphaned_work` (`controller.py:118`). It selects `saq_jobs` rows with
`status IN ('queued','active')`, deserializes each blob, and `INSERT ... ON CONFLICT (key) DO
NOTHING` into the ledger. Safe to run every boot (idempotent); after the transition cohort drains
it becomes a cheap no-op. Gate it so it never aborts boot (same try/except as `:118-122`).

---

## §6. Tests

| Target | Existing pattern to copy | Notes |
|--------|--------------------------|-------|
| `before_enqueue` ledger write | `tests/test_tasks/test_queue_defaults.py`, `test_recovery.py` fakes (`tests/_queue_fakes.py` `DedupFakeQueue`) | Assert a ledger upsert fires; assert agent queue (no `ledger_sessionmaker`) degrades to no-op |
| `after_process` terminal clear | `test_recovery.py` ctx shape; construct `ctx["job"]` with each `Status` | Assert clear on COMPLETE/FAILED/ABORTED, NO clear on QUEUED (retry) |
| Recovery off the ledger | `tests/test_tasks/test_recovery.py` (full harness: `_make_ctx`, `_patch_inflight`, `DedupFakeTaskRouter`, `seed_active_agent`) | Replace pending-set seeding with ledger-row seeding; assert `ledger − live keys` replay + dedup-skip |
| Live-keys query | `test_recovery.py::test_count_inflight_jobs_reads_real_saq_jobs` (`:274`) — `@pytest.mark.integration`, builds a real `PostgresQueue`, probes Postgres, skips if unavailable | Clone for `get_live_job_keys`: enqueue a real keyed job, assert its key appears in the set |
| Migration up/down | `tests/test_migrations/test_020.py` + `conftest.py` `migrated_engine` (upgrade head → assert table/columns; reversible) | Needs `phaze_migrations_test` DB on the ephemeral Postgres |
| Backfill reconcile | new — seed `saq_jobs` blobs (reuse straggler blob shape), assert ledger seeded + idempotent on re-run | Integration (real `saq_jobs`) |

**Integration DB:** `just test-db` spins ephemeral Postgres on **5433** + Redis on **6380**
(`justfile:5-11,68-149`); tests read `TEST_DATABASE_URL` /
`MIGRATIONS_TEST_DATABASE_URL` (`justfile:148-149`, `test_migrations/conftest.py:35-38`). Unit
tests use the metadata-driven `async_engine`/`session` fixtures from `tests/conftest.py`.
**85% coverage gate** (CLAUDE.md) — the WRITE hook branch (handle present vs absent), the
terminal-status matrix, and the recovery replay all need explicit cases. `[VERIFIED: codebase]`

---

## §7. Pitfall (THE biggest design risk): agent-vs-control ledger write/clear location

**The tension, stated plainly:** CONTEXT locks the clear site as
`increment_completed`/`after_process` in `_shared/deterministic_key.py`. That hook runs on BOTH
roles, but the agent worker is **deliberately Postgres-free** (`agent_worker.py:1-7` banner;
import-boundary test `tests/test_task_split.py`). The four agent stages
(`process_file`, `extract_file_metadata`, `fingerprint_file`, `scan_live_set`) execute their
`after_process` on the AGENT worker, which has **no `ctx["async_session"]`** and cannot import
`phaze.database`. **An agent-side `after_process` therefore cannot clear a control-side Postgres
ledger row.** `[VERIFIED: codebase]`

The WRITE side has NO such problem: §1 verified all enqueues are control-side (no agent task calls
`queue.enqueue`), so the WRITE always runs in the control process. The conflict is purely on the
CLEAR side, and only for the four agent stages.

### Recommended resolution (durability-first, honors all 3 locked decisions)

**Split the clear by tier, both control-side:**

1. **Controller stages** (`generate_proposals`, `search_tracklist`, `scrape_and_store_tracklist`,
   `match_tracklist_to_discogs`): cleared at the controller `after_process`
   (`increment_completed`, `controller.py:171`) on `job.status in TERMINAL_STATUSES`. Literally the
   locked site. The controller worker has `ctx["async_session"]` (`controller.py:72`). ✅

2. **Agent stages**: cleared in the existing **control-side HTTP callback handlers** the agent
   already invokes on BOTH success and terminal failure. Verified for analyze:
   `routers/agent_analysis.py` has `PUT /{file_id}` → `FileState.ANALYZED` (`:188`, success) AND
   `POST /{file_id}/failed` → `FileState.ANALYSIS_FAILED` (`:213`, terminal failure). The sibling
   endpoints exist for the other agent stages (`agent_metadata.py`, `agent_fingerprint.py`,
   `agent_scan_batches.py` / `agent_tracklists.py`). Add `DELETE FROM scheduling_ledger WHERE key
   = '<function>:<file_id>'` in the SAME control-side transaction that records the result/failure.
   The natural id (`file_id`/`tracklist_id`) is in the callback path/payload, and the function name
   is fixed per endpoint, so the key reconstructs exactly. This clears on success AND on terminal
   failure (locked decision #1) and is fully Postgres-durable. ✅

**Why this is correct, not a workaround:** the agent's terminal outcome only becomes
control-visible via these HTTP callbacks — that IS the agent→control terminal signal. The
after_process hook on the agent is the wrong layer (no DB). Clearing in the callback handler is the
*earliest control-side moment* the terminal outcome is known.

**Residual gap (flag for discuss):** a worker crash / OOM / SAQ-timeout-SIGKILL that ends an agent
job WITHOUT any HTTP callback leaves its ledger row uncleared. That row will be re-enqueued by the
next recovery — which is *arguably correct* (the work genuinely didn't finish and wasn't
deliberately failed). Locked decision #1 targets jobs that *exhaust retries and report `failed`*
(the auth/connect failures), which DO emit the `/failed` callback. So the recommended design
satisfies decision #1; the crash-without-callback case degrades to "recoverable", not "poison
re-flood". Confirm this interpretation in discuss.

### Considered alternative (documented, not recommended)

A uniform `after_process` clear that pushes `job.key` into a Redis set via `cache_redis` from BOTH
roles (mirroring how `increment_completed` already INCRs a Redis counter from both roles,
`deterministic_key.py:131-133`), drained control-side into Postgres `DELETE`s by the recovery pass
/ a light cron. **Pro:** one uniform hook, matches the counter pattern, no callback-handler edits.
**Con:** the clear signal then depends on Redis durability — undercutting the CONTEXT's core
durability rationale ("ledger lives outside `saq_jobs` so it survives a broker truncate"); a Redis
flush loses un-drained clears and a `failed` job could be re-queued once (mild decision-#1
violation, self-correcting). Recommend only if wiring 4 callback handlers is deemed too invasive.

### Other pitfalls

- **Don't let `deterministic_key.py` import `phaze.database`.** It is `_shared` (loaded by the
  agent). Access the ledger sessionmaker via `getattr(job.queue, "ledger_sessionmaker", None)`
  only — no module-level DB import. The hook must degrade silently when the handle is absent
  (agent queue, test fakes), exactly like the `cache_redis` block (`:109-115`). `[VERIFIED: codebase]`
- **Never touch `saq_jobs` in the migration** (020 banner, `:14-17`). The ledger is a separate
  app table.
- **`before_enqueue` runs pre-dedup** (`deterministic_key.py:24-28`): a duplicate-key enqueue that
  SAQ later no-ops STILL fires the hook. With `ON CONFLICT (key) DO UPDATE/NOTHING` this is
  harmless (idempotent upsert) — do NOT add pre-dedup detection.
- **`reenqueue.py` must stay control-only** (`:1-9` banner; `test_task_split.py`). All ledger
  reads/writes for recovery stay there or in `services/pipeline.py` (also control-side). Never
  import it from `_shared` or `agent_worker`.

---

## Runtime State Inventory (rename/migration check)

This is an additive-schema phase, not a rename, but it introduces durable runtime state:

| Category | Items | Action |
|----------|-------|--------|
| Stored data | New `scheduling_ledger` Postgres table | Alembic `022` create (reversible) |
| Live service config | None | — |
| OS-registered state | None | — |
| Secrets/env vars | None new | — |
| Build artifacts | None | — |
| **Transition cohort** | Live `saq_jobs` queued/active rows at deploy (incl. any residual ~44.5k cohort if present) | One-time idempotent startup backfill (§5) seeds the ledger so in-flight work stays recoverable — no blind window |

## State of the Art

| Old (Phase 42) | New (Phase 45) | Impact |
|----------------|----------------|--------|
| Recovery reconciles complement-of-done pending sets | Recovery replays `ledger − live keys` | Never-scheduled `DISCOVERED` files no longer swept in |
| `force=True` = sweep domain backlog | `force=True` = reconcile ledger now | Manual "Recover" can't detonate the queue |
| No record a stage was scheduled | Durable ledger row per keyed enqueue | "Scheduled and lost" becomes distinguishable from "never scheduled" |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The non-analyze agent stages (metadata/fingerprint/scan) each have a control-side success AND terminal-failure callback endpoint to host the ledger clear (confirmed only for analyze: `agent_analysis.py` PUT + `/failed`) | §7 | If a stage lacks a `/failed` callback, its terminal-failure clear must fall back to the Redis-drain alternative or a new endpoint — verify each `agent_*` router before planning |
| A2 | `String(255)` PK comfortably fits every deterministic key | §5 | Underflow only if a future builder produces a longer natural id — current max ~80 chars |
| A3 | Attaching `ledger_sessionmaker` to control-side queues is acceptable (symmetric with `cache_redis`) | §1 | If rejected, the WRITE must move to an explicit call in each producer (more touch points, drift risk) |
| A4 | Re-stamping `agent_id` from the live `select_active_agent` on replay (when the original agent is revoked) is desired | §4 | If the original agent must be honored, replay needs revoked-agent handling |

## Open Questions

1. **Per-stage clear coverage (A1).** Confirm `agent_metadata` / `agent_fingerprint` /
   `agent_scan_batches` expose a terminal-FAILURE callback (not just success) so decision #1 holds
   for all four agent stages. Recommendation: audit each `agent_*` router during planning; if a
   `/failed` path is missing, either add one or accept "crash→recoverable" for that stage.
2. **Crash-without-callback semantics.** Confirm the §7 residual-gap interpretation (re-queue a
   no-callback crashed job) is acceptable under locked decision #1.
3. **Backfill `payload` fidelity.** Deserializing `saq_jobs.job` blobs recovers `kwargs`, but
   confirm the blob carries the full original payload (it should — `pipeline.py:782-802` reads
   these blobs today). Verify against a real broker in the integration test.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (dev) | ledger table, recovery | ✓ (compose) | 16+ | — |
| `saq` | hook semantics | ✓ | 0.26.4 | — |
| Ephemeral test Postgres :5433 | integration + migration tests | ✓ (`just test-db`) | 16 | unit tests use metadata-create `async_engine` |
| Ephemeral test Redis :6380 | counter-hook tests | ✓ (`just test-db`) | 7 | — |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config | `pyproject.toml` (`[tool.pytest.ini_options]`), markers incl. `integration` |
| Quick run | `uv run pytest tests/test_tasks/test_recovery.py -x` |
| Full suite | `just test` (ephemeral DB/Redis) → `uv run pytest --cov` |

### Phase Requirements → Test Map
| Req | Behavior | Type | Command | Exists? |
|-----|----------|------|---------|---------|
| L-01 | WRITE upserts ledger at before_enqueue | unit | `pytest tests/test_tasks/test_deterministic_key.py -x` | ❌ Wave 0 (new file) |
| L-02 | clear on COMPLETE/FAILED/ABORTED, not QUEUED | unit | `pytest tests/test_tasks/test_deterministic_key.py -x` | ❌ Wave 0 |
| L-02 | agent stages cleared in callback handlers | unit | `pytest tests/test_routers/test_agent_analysis.py -x` | extend existing |
| L-03 | recovery replays `ledger − live keys` | unit | `pytest tests/test_tasks/test_recovery.py -x` | extend existing |
| L-03 | `get_live_job_keys` reads real saq_jobs | integration | `pytest tests/test_tasks/test_recovery.py -m integration` | ❌ Wave 0 |
| L-04 | backfill idempotent | integration | `pytest -m integration -k backfill` | ❌ Wave 0 |
| L-06 | migration 022 up/down | migration | `pytest tests/test_migrations/test_022.py -x` | ❌ Wave 0 |

### Wave 0 Gaps
- [ ] `tests/test_tasks/test_deterministic_key.py` — WRITE + terminal-clear matrix (L-01/L-02)
- [ ] `tests/test_migrations/test_022.py` — create/reverse (L-06)
- [ ] `get_live_job_keys` + backfill integration cases (L-03/L-04)
- [ ] extend `test_recovery.py` to seed ledger rows instead of pending sets

## Sources

### Primary (HIGH — installed source / codebase, cited file:line)
- `saq==0.26.4` `worker.py:341-438` (process + finally `_after_process`), `:116` (after_process list)
- `saq` `queue/base.py:276` (retry→QUEUED), `:296` (finish→status); `job.py:41` (TERMINAL_STATUSES), `:124` (ttl=600), `:261` (retryable)
- `saq` `queue/postgres.py:357` (sweep), `:825-845` (`_finish` expire_at/DELETE)
- `tasks/_shared/deterministic_key.py:74-139`; `queue_factory.py:66-83`
- `tasks/reenqueue.py:123-243`; `tasks/controller.py:118-194`; `tasks/agent_worker.py:1-7,206-232`
- `services/pipeline.py:322,731-756` (live/inflight SQL); `services/analysis_enqueue.py:43-101`; `services/enqueue_router.py:44-152`
- `models/base.py:18-28`; `models/pipeline_stage_control.py`; `alembic/versions/020,021`
- `routers/agent_analysis.py:93-222` (PUT + `/failed`); `tests/test_tasks/test_recovery.py`; `tests/test_migrations/conftest.py`; `justfile:5-149`

### Secondary / Tertiary
- None — every claim verified against installed source or repo this session.

## Metadata

**Confidence breakdown:**
- SAQ hook mechanism (§2): HIGH — read installed `saq==0.26.4` worker/queue/job source directly.
- WRITE-is-control-side (§1): HIGH — grep-verified no agent task enqueues.
- Agent-vs-control clear split (§7): HIGH on the structural constraint; MEDIUM on per-stage
  callback coverage (A1 — analyze confirmed, other three assumed-by-pattern, must verify).
- Schema/migration (§5): HIGH — mirrors verified `020`/`021` pattern.

**Research date:** 2026-06-18
**Valid until:** ~2026-07-18 (stable internal codebase; re-verify if `saq` is upgraded past 0.26.4)
