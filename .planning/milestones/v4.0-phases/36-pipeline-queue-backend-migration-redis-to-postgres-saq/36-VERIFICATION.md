---
phase: 36-pipeline-queue-backend-migration-redis-to-postgres-saq
verified: 2026-06-12T18:00:00Z
status: passed
score: 8/8
overrides_applied: 0
deferred:
  - truth: "End-to-end enqueue→dequeue smoke on homelab Postgres broker (live two-host)"
    addressed_in: "Post-Phase 38 homelab redeploy"
    evidence: "36-HOMELAB-CHANGE-PROMPT.md is the paste-ready operator deliverable; final env/secret consolidation deferred post-Phase 38 per ROADMAP Step D note. Standing project constraint: real homelab smoke is docs-only at code-verification time."
  - truth: "saq_jobs first-boot DDL auto-creation + DB-role grants verified on homelab cluster"
    addressed_in: "Post-Phase 38 homelab redeploy"
    evidence: "36-HOMELAB-CHANGE-PROMPT.md documents the advisory-lock-guarded init_db() and required role grants. Verified by advisory-lock idempotency in SAQ source; not runnable against the homelab cluster until Phase 38 consolidation."
---

# Phase 36: Pipeline Queue Backend Migration (Redis → Postgres) — Verification Report

**Phase Goal:** Queue backend on Postgres; native priority + scheduled-park available; no regression in reboot re-enqueue, SAQ UI, or determinism.
**Verified:** 2026-06-12
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Single `build_pipeline_queue` factory returns `PostgresQueue`; all four construction sites route through it; no `Queue.from_url(redis_url)` remains | VERIFIED | `queue_factory.py:63` calls `PostgresQueue.from_url`; `controller.py`, `agent_worker.py`, `main.py`, `agent_task_router.py` all call `build_pipeline_queue(...)`. `grep -rn "Queue.from_url" src/phaze` returns only the factory's `PostgresQueue.from_url` and docstring references. |
| 2 | No code reads `queue.redis`; cache reads use `getattr(job.queue, "cache_redis", None)` | VERIFIED | `grep -rn "\.redis\b" src/phaze` returns only comments/docstrings (`agent_exec_batches.py:100`, `pipeline.py:79`, `queue_factory.py:17`, `proposal.py:240`). `deterministic_key.py:110,131` both use `getattr(job.queue, "cache_redis", None)`. `proposal.py` reads `ctx["redis"]`, not `queue.redis`. |
| 3 | `PHAZE_QUEUE_URL` in `config.py`, member of `SECRET_FILE_FIELDS`, validator strips SQLAlchemy dialect | VERIFIED | `config.py:79` — `SECRET_FILE_FIELDS = frozenset({"database_url", "redis_url", "queue_url"})`. `config.py:164-185` — `queue_url` Field with `AliasChoices("PHAZE_QUEUE_URL", "queue_url")` and `_strip_sqlalchemy_driver` before-validator normalizing `+asyncpg`/`+psycopg` to bare `postgresql://`. |
| 4 | Native per-job priority (lower int = sooner) + scheduled-park (future jobs stay parked) available and proven on real PostgresQueue | VERIFIED | `tests/integration/test_pg_queue_priority.py`: `test_lower_priority_integer_dequeues_first` enqueues {50,10,90} and asserts dequeue order 10→50→90. `test_future_scheduled_job_parks` asserts a future-`scheduled` job does not dequeue while a ready sibling does; second dequeue returns `None`. Real `PostgresQueue.from_url` used (no hook interference). |
| 5 | Deterministic-key dedup preserved: in-flight re-enqueue returns `None` (ON CONFLICT no-op); re-enqueue succeeds after COMPLETE with strictly-greater `scheduled` | VERIFIED | `tests/integration/test_pg_dedup.py`: `test_in_flight_duplicate_key_returns_none` asserts second enqueue of same key = `None`. `test_key_reenqueues_after_completion` drives job to `COMPLETE` via `finish()`, then re-enqueues with `scheduled+1` and asserts it lands. Both use real `PostgresQueue`. |
| 6 | `/saq` monitor renders over `PostgresQueue.info()` | VERIFIED | `tests/test_web/test_saq_mount.py:92-123` — `test_mount_renders_over_postgres_queue_info` constructs a real `PostgresQueue(open=False)`, patches `.info()` to canonical `QueueInfo` mapping, drives `/saq/api/queues`, asserts name + counts render, asserts `pg_queue.info.assert_awaited()`, and asserts `PostgresQueue.info` is an async coroutine function. |
| 7 | No regression: Phase 32 reboot re-enqueue intact; Phase 26 import boundary (no sqlalchemy.ext.asyncio in agent) preserved; Phase 35 deterministic key hooks carried over | VERIFIED | `reenqueue.py` unchanged (grep confirms same function body). `tests/test_task_split.py:68` confirms `sqlalchemy.ext.asyncio` forbidden AND `saq.queue.postgres` present in agent import graph. `test_queue_factory.py:41-46` asserts both `apply_project_job_defaults` and `apply_deterministic_key` registered on every factory-built queue. |
| 8 | Step D: `36-HOMELAB-CHANGE-PROMPT.md` exists and is substantive; README/deployment/configuration/.env.example updated for Postgres-broker + cache-only-Redis + `PHAZE_QUEUE_URL` | VERIFIED | `36-HOMELAB-CHANGE-PROMPT.md` exists with 7 topics (PHAZE_QUEUE_URL secret, dep swap, DDL, firewall edge, connection budget, Redis demotion, deploy ordering). `README.md:260` — "SAQ on PostgreSQL (psycopg3)"; `README.md:65` — `REDIS` = "cache only". `docs/configuration.md:57` documents `PHAZE_QUEUE_URL`. `.env.example:41` — `PHAZE_QUEUE_URL=postgresql://...`. |

**Score:** 8/8 truths verified

---

### Deferred Items

Items not yet met but explicitly addressed in later operational steps. These are homelab deployment actions, not code gaps.

| # | Item | Addressed In | Evidence |
|---|------|-------------|----------|
| 1 | End-to-end enqueue→dequeue smoke on live homelab Postgres broker | Post-Phase 38 homelab redeploy | `36-HOMELAB-CHANGE-PROMPT.md` is the paste-ready deliverable; 36-04-SUMMARY states "Final env/secret consolidation deferred to after Phase 38 per ROADMAP Step D note." |
| 2 | `saq_jobs` first-boot DDL auto-creation + DB-role grants on homelab cluster | Post-Phase 38 homelab redeploy | Prompt covers advisory-lock-guarded `init_db()` and required role grants (CREATE in own schema + LISTEN/NOTIFY). Not runnable against the homelab cluster until Phase 38 consolidation. |

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/tasks/_shared/queue_factory.py` | Single PostgresQueue construction seam | VERIFIED | 77 lines; `build_pipeline_queue` returns `PostgresQueue`, registers both hooks, attaches `cache_redis`. No connection opened at construction — `q.pool.closed is True` proven by `test_queue_factory.py:75`. |
| `src/phaze/config.py` | `PHAZE_QUEUE_URL` field + `_strip_sqlalchemy_driver` + `SECRET_FILE_FIELDS` | VERIFIED | `queue_url` at line 164; validator at 170-185; `SECRET_FILE_FIELDS` frozenset at line 79. |
| `src/phaze/tasks/controller.py` | Routes through factory (construction site 1) | VERIFIED | `queue = build_pipeline_queue("controller", get_settings().queue_url, cache_redis_url=get_settings().redis_url, min_size=2, max_size=8)` at module level. Shutdown closes `queue_cache_redis` (WR-01 fix). |
| `src/phaze/tasks/agent_worker.py` | Routes through factory (construction site 2) | VERIFIED | `queue = build_pipeline_queue(_queue_name, get_settings().queue_url, cache_redis_url=get_settings().redis_url, min_size=1, max_size=4)` at module level. Shutdown closes `queue_cache_redis` (WR-01 fix). |
| `src/phaze/main.py` | Routes through factory (construction site 3); opens pool; closes `cache_redis` on shutdown | VERIFIED | `_app.state.controller_queue = build_pipeline_queue(...)` + `await _app.state.controller_queue.connect()` + per-agent `await q.connect()` before `/saq` mount. `getattr(..., "cache_redis", None)` closed before `disconnect()`. |
| `src/phaze/services/agent_task_router.py` | Routes through factory (construction site 4); `enqueue_for_agent` opens pool; `close()` shuts `cache_redis` | VERIFIED | `build_pipeline_queue(f"phaze-agent-{agent_id}", self._queue_url, ...)` in `_queue_for`. `await queue.connect()` before enqueue. WR-01 fix: `getattr(queue, "cache_redis", None)` closed in `close()`. |
| `tests/test_queue_factory.py` | 4 construction-time contract tests | VERIFIED | Tests: type is `PostgresQueue`, both hooks registered, `cache_redis` is `aioredis.Redis`, pool is `closed is True` at construction. |
| `tests/integration/test_pg_queue_priority.py` | REQ-36-2 priority + scheduled-park on real PG | VERIFIED | Two tests: lower-int dequeues first; future-`scheduled` job stays parked. Real `PostgresQueue.from_url` + `connect()`. Connectivity-probe skip guard. |
| `tests/integration/test_pg_dedup.py` | REQ-36-3 ON CONFLICT dedup + reenqueue after completion | VERIFIED | Two tests: in-flight key → `None`; terminal-status key + `scheduled+1` → lands again. Real `PostgresQueue`. |
| `tests/test_web/test_saq_mount.py` | REQ-36-4 `/saq` renders over `PostgresQueue.info()` | VERIFIED | `test_mount_renders_over_postgres_queue_info` constructs real `PostgresQueue(open=False)`, patches `.info()`, asserts render + `assert_awaited()` + `iscoroutinefunction`. |
| `tests/test_task_split.py` | Agent import boundary: no `sqlalchemy.ext.asyncio`, `saq.queue.postgres` present | VERIFIED | `PHAZE_QUEUE_URL` set in subprocess env; forbidden = `sqlalchemy.ext.asyncio`; positive assertion `saq.queue.postgres in sys.modules`. |
| `.planning/.../36-HOMELAB-CHANGE-PROMPT.md` | Paste-ready operator runbook (Step D deliverable) | VERIFIED | File exists; 7 topics: PHAZE_QUEUE_URL secret, dep swap, DDL, firewall edge, connection budget, Redis demotion, deploy ordering. No real credentials inlined. |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `controller.py` | `build_pipeline_queue` | `from phaze.tasks._shared.queue_factory import build_pipeline_queue` | WIRED | Module-level queue construction confirmed in diff. |
| `agent_worker.py` | `build_pipeline_queue` | `from phaze.tasks._shared.queue_factory import build_pipeline_queue` | WIRED | Module-level queue construction confirmed in diff. |
| `main.py` | `build_pipeline_queue` | `from phaze.tasks._shared.queue_factory import build_pipeline_queue` | WIRED | Lifespan construction + `await connect()` confirmed in diff. |
| `agent_task_router.py` | `build_pipeline_queue` | `from phaze.tasks._shared.queue_factory import build_pipeline_queue` | WIRED | `_queue_for` + `enqueue_for_agent` pool-open confirmed in diff. |
| `deterministic_key.py` hooks | `cache_redis` on queue | `getattr(job.queue, "cache_redis", None)` | WIRED | Lines 110 and 131 confirmed. |
| `proposal.py` | Redis cache | `ctx["redis"]` (not `queue.redis`) | WIRED | Confirmed in diff; docstring updated (IN-01 resolved). |
| All 4 shutdown paths | `cache_redis.aclose()` | `getattr(queue, "cache_redis", None)` | WIRED | WR-01 fix confirmed in controller.py, agent_worker.py, main.py, agent_task_router.close(). |
| `pyproject.toml` | `saq[postgres]>=0.26.4` | dependency extra swap | WIRED | grep confirms `"saq[postgres]>=0.26.4"` present; `saq[redis]` absent. |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `tests/integration/test_pg_queue_priority.py` | docstring | `queue.redis` appears in docstring referencing old pattern | INFO | Comment only — no code reads `queue.redis`. Acceptable. |
| `src/phaze/tasks/agent_worker.py` | `ctx["redis"]` setup (IN-03) | `ctx["redis"]` created but no current agent task reads it (counter hooks use `queue.cache_redis`) | INFO | Harmless; lazy `from_url`, documented as reserved for future agent cache use. Not a blocker. |

No TBD/FIXME/XXX markers found in phase-modified files. No stubs. No empty implementations.

---

### Gaps Summary

No gaps found. All eight observable truths are VERIFIED by direct code inspection. The single pre-existing code-review warning (WR-01: `cache_redis` handle lifecycle leak) was fixed in commit `520e431` across all four shutdown paths before this verification.

The two homelab operational smoke tests listed in the VALIDATION.md are classified as **deferred** (post-Phase 38) per the standing project constraint documented in the ROADMAP Step D note and the project memory. They are not code gaps — the change prompt and all code are ready; the homelab redeploy is gated behind Phase 38 env/secret consolidation.

---

### Suite Status (as reported by orchestrator, not re-run)

**`just integration-test` at HEAD (`520e431`):** 1721 passed, 0 failed — including the 4 new `tests/integration/` tests (real Postgres broker, `just test-db` ephemeral harness) and the updated `test_web/test_saq_mount.py` + `test_task_split.py` regression surfaces.

---

_Verified: 2026-06-12T18:00:00Z_
_Verifier: Claude (gsd-verifier)_
