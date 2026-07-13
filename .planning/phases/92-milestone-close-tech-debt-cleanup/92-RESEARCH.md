# Phase 92: Milestone-Close Tech-Debt Cleanup - Research

**Researched:** 2026-07-13
**Domain:** Async SQLAlchemy concurrency (asyncio.gather over independent sessions) + hermetic async pytest fixtures (transactional rollback) + a doc-hygiene fix
**Confidence:** HIGH (both non-trivial patterns verified against SQLAlchemy 2.0 official docs via Context7; measurement harness confirmed present in-repo)

## Summary

Phase 92 has two genuinely non-trivial technical risks and one trivial one. The good news up front: **the PERF-02 measurement harness (D-05) already exists and is fully reusable** — `scripts/seed_perf_corpus.py`, `scripts/perf_explain.py`, and the `just perf-db-up / perf-seed / perf-explain` targets were all built in Phase 82. Nothing needs to be rebuilt; the planner reuses them verbatim to capture the before/after numbers. The Phase-82 baseline is recorded: `get_stage_progress()` DIRECT **p50 1290.9 ms** and `GET /pipeline/stats` full endpoint **p50 1405.3 ms**, both OVER the `< ~1s` D-07 budget. `perf_explain.py::time_stage_progress()` is the exact "before/after" instrument.

**CLEAN-01** is a concurrency correctness + pool-safety problem, not an algorithm problem. SQLAlchemy 2.0 confirms (Context7, official docs) that a single `AsyncSession`/asyncpg connection **cannot** run concurrent statements — one `AsyncSession` per `asyncio.gather` task, each checked out from the sessionmaker, is mandatory. The load-bearing risk is the connection pool: the app engine is `pool_size=5, max_overflow=5` (hard cap **10 connections per worker process**), deliberately lean after the PgBouncer session-mode exhaustion incident. A full ~9-way fan-out would consume nearly an entire worker's pool on **every 5s poll**, and multiple uvicorn workers multiply that against the homelab `max_db_conn=80` cap. **Recommendation: bound the fan-out with an `asyncio.Semaphore` (cap ~4).** The three heavy enrich-bucket reads (~360/440/340 ms) dominate the serial 1290 ms; running just those concurrently collapses the critical path to roughly the slowest single read + the cheap counts — comfortably under 1 s — while keeping peak concurrent checkouts small enough to coexist with normal request traffic and the orphan-refresher.

**CLEAN-02** has a clean modern answer that is *simpler* than the recipe CONTEXT.md's D-07 describes. In SQLAlchemy 2.0 the entire "manual `after_transaction_end` → re-issue `begin_nested()`" event-listener dance is **obsolete**: binding the session to a connection that holds an outer transaction, with **`join_transaction_mode="create_savepoint"`**, makes in-test `commit()` calls become SAVEPOINT releases that a single outer-transaction rollback at teardown discards — while remaining visible to sibling sessions bound to the same connection. This is exactly the invariant the `get_session`-never-commits pattern needs. The async wiring (session-scoped engine → per-test connection + outer transaction → `AsyncSession(bind=conn, join_transaction_mode="create_savepoint")` → rollback on teardown) is small and well-supported.

**Primary recommendation:** CLEAN-01 → `asyncio.gather` over per-task sessions from the existing `async_session` sessionmaker, bounded by `asyncio.Semaphore(4)`, each read keeping its `_safe_count`/`_safe_bucket_counts` degrade wrapper (which now rolls back only its own session). CLEAN-02 → session-scoped engine + per-test outer transaction + `AsyncSession(join_transaction_mode="create_savepoint")`; do **not** hand-roll the event listener. CLEAN-03 → delete the duplicated 2-line block and fix one stale comment. Re-run `just perf-seed`/`just perf-explain` and record both numbers in `92-VERIFICATION.md`.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Parallelized stage-progress reads | API / Backend (`services/pipeline.py`) | Database (asyncpg pool) | Pure server-side query orchestration; the pool is the shared resource the fan-out contends for |
| Poll-latency measurement | Dev/CI tooling (`scripts/*.py`, `justfile`) | Database (throwaway perf DB) | Standalone `uv run` bench against a dedicated perf DB, never prod |
| Hermetic test isolation | Test infra (`tests/conftest.py`) | Database (`phaze_test` DB) | Fixture-level construct; blast radius is the whole suite (D-08) |
| Doc hygiene | Source comments | — | Zero runtime tier involvement |

## Standard Stack

No new packages. Everything needed is already a project dependency.

### Core (in-repo, verified present)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| SQLAlchemy | >=2.0.51 (pyproject) | async ORM, `join_transaction_mode`, `async_sessionmaker` | `join_transaction_mode="create_savepoint"` is a 2.0-only feature — the exact tool for both CLEAN-01 sessions and CLEAN-02 fixture `[CITED: docs.sqlalchemy.org/en/20/orm/session_transaction.html]` |
| asyncpg | >=0.31.0 (pyproject) | PostgreSQL async driver | Existing driver; the "one operation at a time per connection" constraint is what forces per-task sessions |
| pytest-asyncio | (dev group) | async fixtures | `@pytest_asyncio.fixture` already used throughout conftest |

**No installation step. No `## Package Legitimacy Audit` needed** — Phase 92 installs zero external packages.

## Runtime State Inventory

> Phase 92 is behavior-preserving (code + test-infra + comments). Not a rename/migration. This inventory confirms there is no hidden runtime state to migrate.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — CLEAN-01 changes *how* reads are issued, not what is written; CLEAN-02 changes test isolation only; CLEAN-03 is comments. Verified: `get_stage_progress` is read-only. | None |
| Live service config | None — no config keys, no external service touched. Verified by scope. | None |
| OS-registered state | None. | None |
| Secrets/env vars | The perf bench reads DSNs (`perf_db_dsn`) and the test suite reads `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` — all pre-existing, unchanged. | None (but see port footgun in Landmines) |
| Build artifacts | None. | None |

## Architecture Patterns

### CLEAN-01 — asyncio.gather over independent AsyncSessions (bounded)

**The hard rule (verified):** "Because it is stateful, a separate `AsyncSession` must be used for each individual task when working with concurrent asyncio operations like `asyncio.gather()`." `[CITED: docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html]`. SQLAlchemy 2.0 proactively raises `IllegalStateChangeError` / "another operation is in progress" if you share one session across gather tasks `[CITED: docs.sqlalchemy.org/en/20/errors.html]`. This is exactly the "another operation is in progress" failure D-03 anticipates.

**The SQLAlchemy example (`examples/asyncio/gather_orm_statements.html`) explicitly warns** that fanning ORM statements across many connections "loses all transactional safety and is also not necessarily any more performant" and adds CPU-bound merge cost `[CITED: docs.sqlalchemy.org/en/20/_modules/examples/asyncio/gather_orm_statements.html]`. That warning is about the *ORM-result-merge* variant (`merge_frozen_result` back into one session). **Phase 92 does NOT need that variant** — `get_stage_progress` returns plain scalar counts / small dicts, not ORM entities to merge. Each read returns an `int` or a `dict[str,int]`, so there is no merge cost and no ORM identity-map concern. The gather is over count queries, which is the benign case.

**Recommended shape** (Claude's-discretion structuring per CONTEXT):

```python
# Source pattern: docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html (one AsyncSession per task)
import asyncio
from phaze.database import async_session  # the existing app sessionmaker

_STATS_FANOUT = asyncio.Semaphore(4)  # D-03 pool-headroom cap (see pool math below)

async def _read_in_own_session(fn):
    """Run one degrade-safe read in its OWN AsyncSession (own pool connection)."""
    async with _STATS_FANOUT:
        async with async_session() as s:
            return await fn(s)   # fn already wraps _safe_count/_safe_bucket_counts internally

async def get_stage_progress(session: AsyncSession) -> dict[str, dict[str, int | None]]:
    # Kick off every independent read concurrently, each with its own session.
    (music_video_total, tracklist_total, discovery_done, convergence_total,
     metadata_b, fingerprint_b, analyze_b, scan_search_done, scrape_done,
     match_done, proposals_done, execute_done, execute_total) = await asyncio.gather(
        _read_in_own_session(lambda s: _safe_count(s, _MV_TOTAL_STMT, node="music_video_total")),
        _read_in_own_session(lambda s: _safe_count(s, _TRACKLIST_TOTAL_STMT, node="tracklist_total")),
        _read_in_own_session(lambda s: _safe_bucket_counts(s, Stage.METADATA)),
        # ... etc, one task per current read ...
    )
    # Assemble the SAME dict, in the SAME key order, from the gathered values.
    return { "discovery": {"done": discovery_done, "total": discovery_done}, ... }
```

**Key structural points:**
- The incoming `session` parameter is kept for signature compatibility but is **no longer the source of the reads** — each read gets its own from `async_session()`. (Confirm no caller relies on the passed session being the transaction the reads run in — it doesn't; callers just want the dict.)
- `_safe_count` / `_safe_bucket_counts` are reused **verbatim** (D-04). They already `try/except → log → rollback → safe default` and **never raise**. Because each now operates on its *own* session, the "aborted transaction poisons the next stage's COUNT" hazard the current shared-session rollback guards against **disappears** — a per-session failure is fully isolated. Keep the rollback (it rolls back *its own* session) but its cross-node poisoning role is now moot.
- Because the wrappers never raise, plain `asyncio.gather(...)` (default `return_exceptions=False`) is safe. **Defensive belt-and-suspenders:** wrap the session *acquisition* itself so a pool-timeout during checkout also degrades to the safe default rather than propagating — otherwise a `pool_timeout` `TimeoutError` raised *outside* `_safe_count` would abort the whole gather. Either wrap acquisition in try/except returning the safe default, or use `return_exceptions=True` and post-map exceptions to defaults. **Recommend the former** (keeps the "never raises into the 5s poll" contract intact end-to-end).

**Anti-pattern to avoid:** the `merge_frozen_result` / out-of-band-session-merge pattern from the SQLAlchemy example. Not needed here (counts, not entities) and it carries the CPU/transactional-safety warnings.

### CLEAN-02 — Hermetic fixture via join_transaction_mode="create_savepoint"

**The modern 2.0 mechanism (verified, and it supersedes the CONTEXT D-07 event-listener recipe):**

> "In SQLAlchemy 2.0 this is achieved by setting `join_transaction_mode` to `'create_savepoint'`, which allows the Session to use SAVEPOINTs for internal transaction management without affecting the external transaction's state. This setup enables ORM code to call commit normally while the test teardown handles the final rollback of all interactions." `[CITED: docs.sqlalchemy.org/en/20/orm/session_transaction.html — "Joining a Session into an External Transaction"]`

The official 2.0 what's-new note is explicit that this **replaces** the old "manual event handlers or savepoint management" recipe `[CITED: docs.sqlalchemy.org/en/20/changelog/whatsnew_20.html — New transaction join modes for Session]`. **Do not hand-roll the `after_transaction_end` listener.** The D-07 language ("`after_transaction_end` → re-`begin_nested()`") describes the pre-2.0 recipe; `create_savepoint` mode *is* that recipe, built in.

**Why this satisfies the `get_session`-never-commits constraint (D-07 CRITICAL):** In `create_savepoint` mode the Session's root is always a SAVEPOINT nested inside the outer (test-owned) transaction. When a mutating router calls `await session.commit()`, that releases the current SAVEPOINT and immediately opens a new one — the rows become visible **within the outer transaction** to any sibling session bound to **the same connection**, but nothing is durably committed. At teardown, one `await outer_trans.rollback()` discards everything. This is precisely the "in-test commits visible to siblings yet rolled back at teardown" property the memory note (`project_get_session_never_commits`) demands.

**CRITICAL wiring corollary the planner must enforce:** the visibility-to-siblings property only holds if **every** session in a test (the test's `session`, the app's `get_session` override, and any independent read session) is bound to the **one** shared connection that owns the outer transaction. A session opened on a *different* pool connection would be in a *different* transaction and — because the outer transaction is uncommitted — would **not** see the in-test rows (read-committed isolation). The current conftest already funnels everything through one `session` object (the `client`/`authenticated_client` fixtures override `get_session` with `lambda: session`), so the existing "single shared session" design maps cleanly onto "single shared connection." Keep that funnel.

**Recommended async fixture shape** (Claude's-discretion wiring per CONTEXT):

```python
# Source pattern: docs.sqlalchemy.org/en/20/orm/session_transaction.html (async adaptation)
@pytest_asyncio.fixture(scope="session")
async def async_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)      # schema created ONCE
    # seed the stable FK-parent fileserver ONCE, committed for real (outside per-test txns)
    async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session_factory() as s:
        s.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
        await s.commit()
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

@pytest_asyncio.fixture
async def _db_connection(async_engine):
    async with async_engine.connect() as conn:
        yield conn

@pytest_asyncio.fixture
async def session(_db_connection) -> AsyncGenerator[AsyncSession]:
    outer = await _db_connection.begin()                    # per-test outer transaction
    s = AsyncSession(bind=_db_connection, join_transaction_mode="create_savepoint")
    try:
        yield s
    finally:
        await s.close()
        await outer.rollback()                              # discards ALL in-test commits
```

`AsyncSession` accepts `join_transaction_mode` and a connection `bind` — it forwards to the sync `Session`. No event listener needed. Event listeners, *if* ever wanted for the savepoint-restart in a fallback path, must be attached to the **sync** proxy objects (`engine.sync_engine`, `session.sync_session` / `session.sync_session_maker`) because SQLAlchemy fires events on the sync layer — but with `create_savepoint` this is unnecessary.

### Recommended file touch map

```
src/phaze/services/pipeline.py     # CLEAN-01: get_stage_progress fan-out + Semaphore + acquisition-guard
tests/conftest.py                  # CLEAN-02: async_engine → session-scope; new connection/session fixtures
src/phaze/services/backends.py     # CLEAN-03 (D-09): delete lines 565-566 (dup of 563-564)
src/phaze/routers/agent_files.py   # CLEAN-03 (D-10): fix stale DISCOVERED-stamp comment (~line 132-135)
92-VERIFICATION.md                 # D-05: record before/after p50/p95
```

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Concurrent reads on one session | A shared-session gather with manual locking | One `AsyncSession` per gather task from `async_session()` | SQLAlchemy explicitly forbids concurrent ops on one session; it raises `IllegalStateChangeError` `[CITED: errors.html]` |
| Rollback-after-commit test isolation | Manual `after_transaction_end` event listener re-issuing `begin_nested()` | `AsyncSession(join_transaction_mode="create_savepoint")` | The 2.0 built-in *is* that recipe; hand-rolling it duplicates library code and risks subtle savepoint-restart bugs `[CITED: session_transaction.html]` |
| Merging ORM results across sessions | `merge_frozen_result` fan-out | N/A — reads return scalars/dicts, no merge needed | The merge pattern carries the docs' own CPU + transactional-safety warnings; irrelevant to count queries |
| 200K perf harness | A fresh seeder/bench | `just perf-db-up` + `just perf-seed N=200000` + `just perf-explain` (Phase 82) | Fully built, parameterized, injection-safe, perf-DB-name-guarded; `time_stage_progress()` is the exact instrument |

**Key insight:** the two hard parts of this phase are both *already solved by SQLAlchemy 2.0 features* (per-task sessions; `create_savepoint` join mode) and the measurement is *already solved by Phase 82's harness*. The engineering risk is in the pool-headroom sizing and the fixture's shared-connection funnel, not in inventing mechanisms.

## Pool Headroom Analysis (D-03 — the load-bearing decision)

**Verified pool config** (`src/phaze/config.py:307-331`, applied in `src/phaze/database.py:35-43`):
- `db_pool_size = 5`
- `db_max_overflow = 5` → **hard ceiling of 10 concurrent connections per engine (per uvicorn worker process)**
- `db_pool_timeout = 10s` (fail-fast on saturation), `pool_pre_ping=True`, `pool_recycle=1800`
- Homelab pooler: `max_db_conn ≈ 80`, app role LIMIT raised to ~85 (post-incident, `project_pgbouncer_pool_exhaustion`).

**The math for a single `/pipeline/stats` poll:**
- `get_stage_progress` currently issues ~13 sequential reads (music_video_total, tracklist_total, discovery, convergence, 3 enrich buckets, scan_search, scrape, match, proposals, execute, execute_total).
- `/pipeline/stats` *also* calls `get_queue_activity`, `get_stage_controls`, `get_global_reconciliation`, `get_cached_stage_orphan_counts` (O(1), no DB), etc. Those are additional DB reads on the request's own session.

**Full fan-out (~13 concurrent sessions) is UNSAFE:** it would try to check out 13 connections from a per-worker pool capped at 10 → immediate `pool_timeout` waits on every poll, every 5s, and with 2-4 uvicorn workers a burst of 26-52 connections against the 80 pooler cap while normal request traffic also needs connections. This directly re-creates the exhaustion class the lean pool was set to avoid.

**Recommended: `asyncio.Semaphore(4)`.** Rationale:
- The serial cost is dominated by the **three enrich bucket reads**: `four_bucket[metadata]` ≈ 363-397 ms, `four_bucket[fingerprint]` ≈ 441 ms, `four_bucket[analyze]` ≈ 339 ms (Phase 82 EXPLAIN, `82-VERIFICATION.md:236-238`). Running those three concurrently (Semaphore ≥ 3 admits all three) collapses ~1150 ms of serial bucket time into ~440 ms wall-clock. The remaining `_safe_count` reads are cheap (single-digit to ~200 ms) and drain quickly through the semaphore.
- Cap 4 → at most 4 extra concurrent checkouts per poll, leaving ≥6 of the 10-slot pool for the request's own session + other concurrent requests. Across 2-4 workers that's a 8-16 connection burst against the 80 cap — safe headroom.
- Projected result: critical path ≈ `max(heavy trio) + a couple cheap reads` ≈ **~450-650 ms**, comfortably under the `< 1 s` D-07 budget — *provided the measurement confirms it* (D-05 is the arbiter, not this projection).

**If the measurement still exceeds budget** with the Semaphore cap: raising `db_pool_size` is the risky lever (PgBouncer session-mode pinning); the correct escalation is DENORM-01 (which becomes a live v2 candidate per D-05), not pool inflation. Record the number either way.

## D-05 Measurement Harness — REUSE, do not rebuild

**Everything exists** (Phase 82, confirmed present):
- `scripts/seed_perf_corpus.py` — parameterized `unnest`-array bulk seeder, deterministic `uuid5`, `ON CONFLICT DO NOTHING` (idempotent), `--reseed` hard-gated to a DB whose name contains `perf`. Seeds the D-06 mid-pipeline selectivity profile at N=200000.
- `scripts/perf_explain.py` — three instruments: `run_explains()` (EXPLAIN ANALYZE BUFFERS the hot query shapes), **`time_stage_progress()`** (times `get_stage_progress(session)` directly, p50/p95 — *this is the before/after DENORM-relevant number*), and `time_endpoint()` (times the real `GET /pipeline/stats` via ASGITransport, p50/p95).
- `justfile` targets: `perf-db-up` (dedicated `postgres:18-alpine` on its own port, never wiped by `test-db` recreates), `perf-seed N='200000'` (alembic upgrade head + seed), `perf-explain ITER='20'`.

**Measurement procedure for D-05:**
1. `just perf-db-up && just perf-seed N=200000`.
2. **BEFORE:** on the current serial code, `just perf-explain ITER=20` → record `get_stage_progress() DIRECT` and `GET /pipeline/stats` p50/p95. (Phase-82 baseline to reproduce: **1290.9 / 1405.3 ms p50**.)
3. Implement CLEAN-01.
4. **AFTER:** `just perf-explain ITER=20` again → record the new p50/p95.
5. Write both into `92-VERIFICATION.md` with the `< 1 s` verdict. Per D-05/SC1, a lightweight overlap-only proof does NOT satisfy the bar — the 200K endpoint/direct numbers are required.

**What exactly is measured:** `/pipeline/stats` poll latency (full ASGI endpoint) and `get_stage_progress()` direct core, p50/p95, over 20 iterations after a warm-up, at 200K music/video files at migration HEAD (≥036 so the 032 partial indexes exist). `time_endpoint()` provisions the SAQ `saq_jobs` tables first so the queue-activity reads take the real path, not the degrade path.

**One harness caveat for the planner:** `time_stage_progress()` currently reuses **one** session across all iterations. That is fine for measuring serial code. After CLEAN-01, `get_stage_progress` opens its own sessions internally from `async_session` (the app engine), so `perf_explain.py` must point the app engine (or the sessionmaker `get_stage_progress` imports) at the perf DSN — verify the bench still routes the internal `async_session()` to the perf DB, not the default `settings.database_url`. This may require a small harness tweak (e.g. set `PHAZE_DATABASE_URL` to the perf DSN for the bench run, since `phaze.database.async_session` binds to the module-level `settings.database_url`). Flag: the current `time_endpoint` overrides `get_session` via `dependency_overrides`, but the *new* internal `async_session()` calls bypass that override. **This is a real integration point** — the planner must ensure the parallelized reads hit the perf DB during measurement (simplest: run the bench with `PHAZE_DATABASE_URL=<perf dsn>` exported so the module-level engine binds to it).

## Common Pitfalls

### Pitfall 1: Snapshot skew changes "byte-identical" semantics under live writes
**What goes wrong:** Today all ~13 reads share one session = one transaction = one consistent MVCC snapshot. Splitting into N independent sessions = N transactions = N snapshots taken microseconds apart. On a **quiescent** DB (all tests, and the perf bench) every value is identical → tests stay byte-identical. Under **live concurrent writes**, two nodes could reflect slightly different points in time (e.g. `metadata.done` vs `analyze.done` off by a row).
**Why it happens:** independent sessions can't share a snapshot without a shared connection/transaction — which would re-serialize the reads and defeat the parallelization.
**How to avoid:** Accept it — a 5s dashboard poll already churns; sub-second cross-node skew is invisible. But **state it explicitly** in the plan/verification: "returned dict is byte-identical on a quiescent DB; under concurrent writes nodes may reflect snapshots microseconds apart — acceptable for a 5s poll." Do NOT claim strict byte-identity under load. The integration-point requirement (CONTEXT: "keep dict shape and derived done buckets byte-identical") is satisfied for shape + quiescent values; the tests prove it.

### Pitfall 2: A pool-timeout escaping the degrade wrappers aborts the whole gather
**What goes wrong:** `_safe_count`/`_safe_bucket_counts` catch query errors, but session *acquisition* (`async with async_session()`) can raise `TimeoutError` after `pool_timeout=10s` if the pool is saturated — that raise happens *outside* the wrapper and, with default `gather`, cancels/propagates and 500s the poll.
**How to avoid:** wrap acquisition in the same degrade discipline (return the node's safe default on acquisition failure), OR use `return_exceptions=True` + map exceptions to defaults. Keep the "never raises into the 5s poll" contract end-to-end. The Semaphore cap also makes saturation far less likely.

### Pitfall 3: Fixture only isolates when every session shares the outer connection
**What goes wrong:** If a test (or an app dependency) opens a session on a *different* pool connection than the one holding the per-test outer transaction, it won't see in-test committed rows (uncommitted outer txn, read-committed isolation) — tests that seed-then-read-via-client break.
**How to avoid:** ensure `client`/`authenticated_client`'s `get_session` override continues to yield the **same** `session` object (already the case at `conftest.py:234,273`). All sibling reads must funnel through that one connection. Audit any test that constructs its own engine/session — those must be excluded from the transactional fixture (they manage their own DB): the `tests/test_migrations/**` and `tests/integration/**` suites already build their own engines against `MIGRATIONS_TEST_DATABASE_URL` / a real `PostgresQueue` and do NOT consume `async_engine`/`session` — confirm that stays true (they're auto-marked `integration` by `pytest_collection_modifyitems`).

### Pitfall 4: begin_nested() inside production code under create_savepoint mode
**What goes wrong:** several pipeline functions (`_agent_stage_buckets`, `get_stage_busy_counts`, `get_live_job_keys`, `get_search_busy_count`, `get_scan_busy_count`, `_compute_stage_orphan_counts`, `get_agent_recent_scans`) use `async with session.begin_nested()`. Under `create_savepoint` mode the session's root is already a SAVEPOINT, so these create a *nested* SAVEPOINT (savepoint-within-savepoint).
**Why it's fine:** PostgreSQL supports nested SAVEPOINTs; SQLAlchemy handles them. But verify the affected functions' tests stay green under the new fixture — a nested savepoint rollback must not surprise the outer create_savepoint root. (These functions are NOT part of `get_stage_progress`'s fan-out, but they ARE exercised by the same test suite the CLEAN-02 fixture rewires — hence the D-08 full-suite gate.)

### Pitfall 5: Test-DB port footgun (5432 vs 5433)
**What goes wrong:** `TEST_DATABASE_URL` defaults to `localhost:5432/phaze_test` (`conftest.py:43`) but `just test-db` provisions the ephemeral integration Postgres on **5433** and `MIGRATIONS_TEST_DATABASE_URL` defaults to 5432 too (`reference_migrations_test_db_port`). Running a bucket in isolation without exporting both URLs to the running DB's port fails with connection errors that *look like* the colima flake CLEAN-02 is trying to eliminate.
**How to avoid:** when running `just test-bucket <bucket>` for the D-08 gate, export the correct DB URLs/ports; document the exact env in the plan so a reviewer reproducing the green gate doesn't misread a port error as a hermeticity regression.

### Pitfall 6: Colima VM pressure can still surface as flakes — but for a different reason now
**What goes wrong:** the whole point of CLEAN-02 is that create_savepoint isolation removes the *committed-seed-row-survives-teardown* race (`pk_agents` collision). But full-suite runs under colima also flake on raw DB connection errors from VM pressure (`reference_local_fullsuite_colima_flake`) — a different class.
**How to avoid:** the D-08 acceptance gate is per-bucket isolation (`just test-bucket`), which is the project standard and sidesteps whole-suite VM pressure. Verify each bucket green in isolation; don't conflate a VM-pressure connection flake with a hermeticity failure.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual `after_transaction_end` event listener re-issuing `begin_nested()` for rollback-after-commit test isolation | `Session(bind=conn, join_transaction_mode="create_savepoint")` | SQLAlchemy 2.0 | The CONTEXT D-07 recipe is now a one-liner config; no listener to hand-roll |
| Sharing one Session across `asyncio.gather` tasks | One `AsyncSession` per task (enforced by `IllegalStateChangeError`) | SQLAlchemy 2.0 proactive detection | Concurrent reads MUST fan out sessions; can't cheat with one session |

**Deprecated/outdated:**
- The pre-2.0 event-listener savepoint-restart recipe: still works, but superseded — do not introduce it.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `asyncio.Semaphore(4)` is the right cap | Pool Headroom | If measurement still over budget → escalate to DENORM-01 (a documented D-05 outcome), or re-tune the cap; not a correctness risk |
| A2 | Parallelizing the 3 heavy bucket reads gets under 1 s | Summary / Pool Headroom | If not, DENORM-01 becomes the live v2 candidate — exactly what D-05 is designed to decide. Projection only; the harness is the arbiter |
| A3 | `time_stage_progress()`/`time_endpoint()` route the new internal `async_session()` to the perf DB via `PHAZE_DATABASE_URL` | D-05 Harness | If the bench measures the wrong DB, the number is meaningless — planner MUST verify the bench binds the module-level engine to the perf DSN (concrete integration point flagged) |
| A4 | No migration/integration test consumes the `session`/`async_engine` fixtures | Pitfall 3 | If one does, the session-scoped/create_savepoint change could break it — planner must grep-verify before landing |

## Open Questions

1. **Does the perf bench correctly route `get_stage_progress`'s new internal sessions to the perf DB?**
   - What we know: current `perf_explain.py` uses `dependency_overrides[get_session]` + a local engine; the parallelized code will call `phaze.database.async_session` directly, which binds to `settings.database_url`.
   - What's unclear: whether exporting `PHAZE_DATABASE_URL=<perf dsn>` for the bench run is sufficient (module-level `engine` is built at import time from `settings`).
   - Recommendation: run the bench as `PHAZE_DATABASE_URL=<perf dsn> just perf-explain`; if `settings` is already imported before the env is set, the harness may need a tiny tweak to construct the engine against the perf DSN. Plan a Wave-0 check.

2. **Should the incoming `session` param of `get_stage_progress` be removed or kept?**
   - What we know: callers pass their request session; the reads no longer use it.
   - Recommendation: keep the parameter for signature stability (least blast radius), document it as unused-by-design, OR remove it and update the ~2 callers. Claude's discretion — keeping it is the lower-risk default for a behavior-preserving phase.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Docker (colima) | perf-db-up, test-db | ✓ (project standard) | — | — |
| PostgreSQL 18-alpine | perf bench | ✓ via `just perf-db-up` | 18 | — |
| Local Postgres on 5432/5433 | test suite / migration tests | ✓ (dev standard) | — | — |
| `uv` | all commands | ✓ (project constraint) | — | — |

No missing dependencies. All tooling is pre-existing.

## Validation Architecture

> nyquist_validation is enabled (`.planning/config.json → workflow.nyquist_validation: true`).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (dev group) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) + `tests/buckets.json` for CI partitioning |
| Quick run command | `uv run pytest tests/shared/core/test_stage_progress.py -x` (per-node) |
| Full suite command | `just test-bucket <bucket>` for each of the 9 buckets (D-08 gate) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CLEAN-01 | `get_stage_progress` returns the same dict shape + values (quiescent) after parallelization | integration | `uv run pytest tests/integration/test_stage_progress_buckets.py -q` | ✅ (Phase 82) |
| CLEAN-01 | Per-read degrade still isolates a failing node to its safe default | unit/integration | `uv run pytest tests/analyze/core/test_stage_progress.py -q` | ✅ |
| CLEAN-01 | Poll latency measured before/after at 200K | manual-bench | `PHAZE_DATABASE_URL=<perf> just perf-explain ITER=20` → `92-VERIFICATION.md` | ✅ harness (Phase 82) |
| CLEAN-01 | Concurrency doesn't raise "another operation is in progress" | integration | full `/pipeline/stats` route test under the new fan-out | ✅ (`tests/shared/routers/test_pipeline.py`) |
| CLEAN-02 | Committed seed rows never survive into the next test (no `pk_agents` collision) | infra | `just test-bucket agents` + `just test-bucket analyze` in isolation, repeated | ⚠️ new hermeticity assertion may be added |
| CLEAN-02 | In-test `commit()` visible to sibling read, rolled back at teardown | infra | a fixture-level test asserting commit-then-independent-read then next-test-clean | ❌ Wave 0 — add a dedicated fixture-contract test |
| CLEAN-02 | Full ~1750-test suite green under per-bucket isolation | acceptance | every bucket via `just test-bucket <name>` | existing suite |
| CLEAN-03 | Comments corrected; anti-drift guard still passes; zero runtime change | static | `uv run pytest tests/shared/test_partition_guard.py` + existing anti-drift guards | ✅ |

### Sampling Rate
- **Per task commit:** the touched node's targeted test (`test_stage_progress.py` / the fixture-contract test).
- **Per wave merge:** the affected buckets via `just test-bucket` (at minimum `analyze`, `agents`, `shared`, `integration`).
- **Phase gate:** **all 9 buckets green in isolation** (D-08) + both perf numbers recorded in `92-VERIFICATION.md` (D-05), before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] A dedicated **fixture-contract test** proving the `create_savepoint` behavior: (a) commit inside a test is visible to a sibling read on the same connection, (b) the next test sees a clean DB (no surviving row). This is the mutation-safe proof that CLEAN-02 actually isolates — without it, a green suite proves nothing (the flake is intermittent). Cross-reference `feedback_mutation_test_guard_tests`: break the fixture (revert to function-scoped create_all) and watch the seed-collision return.
- [ ] Verify the perf harness routes the parallelized internal sessions to the perf DB (Open Question 1) — a Wave-0 smoke run of `just perf-explain` against a small N confirming non-degrade timings.
- [ ] Grep-verify no `tests/test_migrations/**` or `tests/integration/**` test consumes `session`/`async_engine` in a way incompatible with session-scope (Pitfall 3 / A4).

## Security Domain

> `security_enforcement` is not explicitly disabled; included for completeness. This phase adds **no** new attack surface: no new endpoints, no new inputs, no new packages, no new secrets.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | no | No new inputs; `get_stage_progress` takes no user input; perf seeder uses parameterized `unnest` arrays (no interpolated values) already |
| V6 Cryptography | no | None touched |
| V2/V3/V4 Auth/Session/Access | no | No auth/route changes |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via perf seeder | Tampering | Already mitigated: `unnest`-array bound params + perf-DB-name `--reseed` guard (Phase 82, `seed_perf_corpus.py`) — no change needed |
| Pool exhaustion → DoS of `/health` and requests | Denial of Service | **This is the live risk of CLEAN-01** — the Semaphore cap + acquisition-degrade are the mitigation; the whole Pool Headroom section addresses it |

## Sources

### Primary (HIGH confidence)
- Context7 `/websites/sqlalchemy_en_20` — "Using AsyncSession with Concurrent Tasks" / "AsyncSession is not safe for use in concurrent tasks" (`orm/extensions/asyncio.html`); `IllegalStateChangeError` concurrency detection (`errors.html`); "Joining a Session into an External Transaction" + `join_transaction_mode="create_savepoint"` (`orm/session_transaction.html`); "New transaction join modes" (`changelog/whatsnew_20.html`); `examples/asyncio/gather_orm_statements.html` (merge-pattern warning).
- In-repo Phase 82 artifacts: `scripts/seed_perf_corpus.py`, `scripts/perf_explain.py`, `justfile` perf targets, `.planning/phases/82-counts-pending-set-cutover/82-VERIFICATION.md` (baseline p50/p95 + per-query EXPLAIN).
- In-repo source: `src/phaze/services/pipeline.py` (get_stage_progress + degrade helpers), `src/phaze/database.py` (engine/pool + `async_session`), `src/phaze/config.py:307-331` (pool defaults), `tests/conftest.py` (current fixtures), `.planning/phases/83-cloud-routing-sidecar-cutover/deferred-items.md` (83-01/83-03 root cause).

### Secondary (MEDIUM confidence)
- Project MEMORY: `project_pgbouncer_pool_exhaustion` (pool caps), `project_get_session_never_commits` (commit-then-independent-read invariant), `reference_migrations_test_db_port` (5432/5433 footgun), `reference_ci_bucket_isolation` / `reference_local_fullsuite_colima_flake` (flake class), `feedback_mutation_test_guard_tests` (guard-test discipline).

## Metadata

**Confidence breakdown:**
- CLEAN-01 concurrency mechanism: HIGH — SQLAlchemy 2.0 docs are explicit and version-current.
- CLEAN-01 pool sizing (Semaphore 4): MEDIUM — sound math from Phase-82 per-query numbers + verified pool caps, but the exact cap is validated by the D-05 measurement, not asserted.
- CLEAN-02 fixture mechanism: HIGH — `join_transaction_mode="create_savepoint"` is the documented, purpose-built 2.0 feature for exactly this.
- D-05 harness reuse: HIGH — the scripts and just targets exist and are read; one integration caveat flagged (Open Question 1).
- CLEAN-03: HIGH — both locations confirmed by direct file read.

**Research date:** 2026-07-13
**Valid until:** ~2026-08-13 (stable; SQLAlchemy 2.0 async semantics are settled)

## RESEARCH COMPLETE

**Phase:** 92 - Milestone-Close Tech-Debt Cleanup
**Confidence:** HIGH

### Key Findings
- The PERF-02 measurement harness (D-05) **already exists** — `scripts/seed_perf_corpus.py` + `scripts/perf_explain.py` + `just perf-db-up/perf-seed/perf-explain`. Reuse, don't rebuild. Phase-82 baseline: `get_stage_progress` p50 **1290.9 ms**, endpoint p50 **1405.3 ms** (both over the `<1s` budget). `time_stage_progress()` is the before/after instrument.
- CLEAN-01: SQLAlchemy 2.0 mandates **one `AsyncSession` per gather task**; full ~13-way fan-out is **unsafe** against `pool_size=5/max_overflow=5` (10-conn/worker ceiling, lean post-PgBouncer-incident). Recommend `asyncio.Semaphore(4)` — parallelizing the three heavy enrich-bucket reads (~360/440/340 ms) collapses the critical path under 1 s while capping pool pressure.
- CLEAN-02: The modern answer is **`AsyncSession(join_transaction_mode="create_savepoint")`**, which *replaces* the manual `after_transaction_end` event-listener recipe D-07 describes. It satisfies the `get_session`-never-commits invariant *iff* every session in a test shares the one outer-transaction connection (the existing `client` override already funnels this).
- Two real integration landmines flagged: (1) the perf bench must route the new internal `async_session()` calls to the perf DB (`PHAZE_DATABASE_URL` export); (2) "byte-identical" holds on a quiescent DB (tests) but becomes microsecond snapshot-skew under live writes — acceptable for a 5s poll, but must be stated, not claimed as strict identity.
- CLEAN-03 locations confirmed: `backends.py:565-566` (delete dup of 563-564), `agent_files.py` stale DISCOVERED comment (~132-135).

### File Created
`.planning/phases/92-milestone-close-tech-debt-cleanup/92-RESEARCH.md`

### Confidence Assessment
| Area | Level | Reason |
|------|-------|--------|
| Standard Stack | HIGH | Zero new packages; existing deps verified |
| Concurrency mechanism (CLEAN-01) | HIGH | SQLAlchemy 2.0 docs explicit + version-current |
| Pool sizing | MEDIUM | Sound math; final cap arbitrated by D-05 measurement |
| Fixture mechanism (CLEAN-02) | HIGH | `create_savepoint` is the purpose-built 2.0 feature |
| Pitfalls | HIGH | Grounded in verified source + project memory |

### Open Questions
1. Does `perf_explain.py` route the parallelized internal sessions to the perf DB? (Wave-0 smoke check — export `PHAZE_DATABASE_URL`.)
2. Keep vs remove the now-unused `session` param of `get_stage_progress`? (Recommend keep for minimal blast radius.)

### Ready for Planning
Research complete. Planner can now create PLAN.md files. Note the Wave-0 gaps (fixture-contract test; perf-DB routing check; migration-test fixture-usage grep) and the D-08 full-suite / D-05 measurement gates.
