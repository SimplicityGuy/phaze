---
phase: 36
status: clean
depth: standard
reviewed: 2026-06-12
files_reviewed: 22
critical: 0
warning: 1
info: 3
resolution: WR-01 + IN-01 + IN-02 fixed in commit 520e431; full suite green (1721 passed)
---

# Phase 36 Code Review — Pipeline Queue Backend Migration (Redis → Postgres SAQ)

**Depth:** standard · **Reviewed:** 2026-06-12

## Summary

The migration is mechanically sound. All four queue-construction sites use the single `build_pipeline_queue` factory, `queue.redis` reads are fully removed (only docstring/comment matches remain), the `_strip_sqlalchemy_driver` validator normalizes both `+asyncpg` and `+psycopg` dialect forms, dedup semantics carry over via the ON CONFLICT contract (verified by integration tests), and the `generate_proposals` AttributeError (RESEARCH Pitfall 1) is fixed via `ctx["redis"]`.

One genuine resource-lifecycle warning and three info items found. No Critical findings.

## Warnings

### WR-01: `cache_redis` handle attached to every queue is never closed on shutdown — RESOLVED

Files: `src/phaze/main.py` (API lifespan), `src/phaze/services/agent_task_router.py` (`close()`), `src/phaze/tasks/controller.py` (shutdown hook), `src/phaze/tasks/agent_worker.py` (shutdown hook).

`build_pipeline_queue` attaches `q.cache_redis = aioredis.Redis.from_url(...)` to every queue (`queue_factory.py:70`). `PostgresQueue.disconnect()` closes only the psycopg3 pool, not the dynamically-attached `cache_redis` attribute. All shutdown paths left `cache_redis` unclosed → up to N+1 leaked Redis pools per graceful shutdown (and `ResourceWarning` noise if any counter hook fired). Fix: close `getattr(queue, "cache_redis", None)` in each shutdown path before/after `disconnect()`.

## Info

- **IN-01 (RESOLVED):** `src/phaze/services/proposal.py:239` docstring still cited `queue.redis` — the exact broken pattern this phase removed. Updated to reference `ctx["redis"]`.
- **IN-02:** `queue_factory.py` defined `logger`/`import structlog` with zero log calls. Added a debug construction log (pool sizing is the operational risk per RESEARCH Pitfall 4).
- **IN-03:** `agent_worker.py` creates `ctx["redis"]` that no current agent task reads (counter hooks use `queue.cache_redis`). Harmless (lazy `from_url`); documented as reserved.

## Test Quality

Integration tests are well-structured: connectivity-probe `pytest.skip` for optional broker, parameterized cleanup DELETE, scheduled-park test avoids flaky sleep via a ready sibling. `DedupFakeQueue` documents its caller-`key=`-only dedup; hook-driven dedup is covered by the real-Postgres integration tests (REQ-36-3).
