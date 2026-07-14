# Phase 92: Milestone-Close Tech-Debt Cleanup - Pattern Map

**Mapped:** 2026-07-13
**Files analyzed:** 5 modified + 1 new test (+ perf harness reuse)
**Analogs found:** 6 / 6 (all in-repo; zero new packages)

> This phase is 90% MODIFY, one CREATE (a fixture-contract test), and one REUSE (the Phase-82 perf
> harness). Every pattern below is copy-from an existing in-repo file — no external template needed.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/pipeline.py` (`get_stage_progress`, `_safe_count`, `_safe_bucket_counts`) | service | request-response / read-fan-out | `src/phaze/tasks/discogs.py` (`asyncio.gather` + `Semaphore`) | role-match (both bound concurrent async work; discogs writes, this reads) |
| `tests/conftest.py` (`async_engine`, `session`, `client`) | test-infra fixture | transactional isolation | current `async_engine`/`session` fixtures (self, being rewired) + SQLAlchemy 2.0 `create_savepoint` recipe | exact (same fixtures, new mechanism) |
| `src/phaze/services/backends.py:563-566` | service (comment) | n/a (cosmetic) | n/a — delete duplicate | exact location |
| `src/phaze/routers/agent_files.py:131-135` | router (comment) | n/a (cosmetic) | n/a — reword stale comment | exact location |
| `tests/shared/test_conftest_hermeticity.py` (**NEW**) | test (infra/contract) | request-response (commit→independent read) | `tests/shared/test_conftest_dsn_coercion.py` (infra test of conftest) + `tests/review/routers/test_duplicates.py:298-347` (commit-then-independent-session-read) | role-match |
| `scripts/perf_explain.py` / `scripts/seed_perf_corpus.py` / `just perf-*` (D-05, REUSE) | dev/CI tooling | batch/bench | Phase 82 harness (self) | exact (reuse verbatim) |

**Bucket map** (`tests/buckets.json` → top-level `tests/<name>/` dir): `discovery, metadata, fingerprint,
analyze, identify, review, agents, integration, shared`. The NEW fixture-contract test belongs in the
**`shared`** bucket (`tests/shared/`), alongside the existing conftest/infra guards.

---

## Pattern Assignments

### `src/phaze/services/pipeline.py` — CLEAN-01 (service, read-fan-out)

**Analog for the concurrency shape:** `src/phaze/tasks/discogs.py:59-66` — the ONLY existing
`asyncio.gather` + `asyncio.Semaphore` pairing in `src/`.

**Semaphore-bounded gather pattern** (`discogs.py:59-66`) — copy the SHAPE (module/function-level
`Semaphore`, `async with semaphore:` inside a per-item coroutine, `await asyncio.gather(*[...])`):
```python
semaphore = asyncio.Semaphore(settings.discogs_match_concurrency)

async def _match_one(track: TracklistTrack) -> list[dict[str, Any]]:
    async with semaphore:
        return await match_track_to_discogs(client, track)

match_results = await asyncio.gather(*[_match_one(t) for t in eligible])
```
Difference for Phase 92: each task must open its **own** `AsyncSession` (discogs shares one session
because its `_match_one` does no DB I/O inside the gather — it hits an HTTP API). See D-03.

**Session factory to fan out from** — `src/phaze/database.py:45` (the app sessionmaker, module-level):
```python
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
```
Per-task acquisition idiom already used across the codebase (`discogs.py:32`, `database.py:50`):
```python
async with async_session() as s:
    ...  # one connection checked out per task
```

**Degrade wrappers to REUSE VERBATIM (D-04)** — `pipeline.py:319-336` (`_safe_count`) and
`pipeline.py:339-376` (`_safe_bucket_counts`). Both already `try / except → log warning →
guarded rollback → safe default` and **never raise**. Keep them unchanged; call each inside its own
session. Their cross-node "aborted-txn poisons the next COUNT" role becomes moot once each read owns
its session (RESEARCH §CLEAN-01) but the rollback stays (now rolls back only its own session).

**The dict to preserve byte-for-byte** — `pipeline.py:558-592`. The return is a 9-key dict
(`discovery, metadata, fingerprint, analyze, scan_search, scrape, match, proposals, execute`); the
three enrich nodes spread `_safe_bucket_counts(...)` + `total`; the rest are `{"done": int, "total":
int|None}`. After parallelization, assemble the SAME dict in the SAME key order from gathered values.
Keys, order, and the derived `done` buckets must be identical (integration test
`tests/integration/test_stage_progress_buckets.py` is the guard).

**Pool-safety belt (RESEARCH Pitfall 2 — do NOT skip):** wrap the *session acquisition* itself in the
degrade discipline. `async with async_session()` can raise `TimeoutError` after `db_pool_timeout=10s`
(`database.py:40`) OUTSIDE `_safe_count`, which would abort the whole `gather`. Wrap acquisition to
return the node's safe default, OR use `gather(..., return_exceptions=True)` and map exceptions to
defaults. Keep the "never raises into the 5s poll" contract end-to-end.

**Pool math the planner MUST honor (D-03):** `database.py:38-40` sets `pool_size=5, max_overflow=5`
→ hard ceiling **10 connections per worker**, deliberately lean post-PgBouncer-incident (see the
load-bearing comment at `database.py:24-34`). Full ~13-way fan-out is UNSAFE. RESEARCH recommends
`asyncio.Semaphore(4)`.

---

### `tests/conftest.py` — CLEAN-02 (test-infra fixture, transactional isolation)

**Current fixtures being rewired** (read in full):
- `async_engine` — `conftest.py:198-219`: function-scoped, `create_all`/`drop_all` per test, commits a
  `test-fileserver` seed row. This per-test create/commit/drop is the flake root (D-06).
- `session` — `conftest.py:222-227`: builds a fresh `async_sessionmaker(async_engine)` per test.
- `client` / `authenticated_client` — `conftest.py:230-236` / `260-280`: override `get_session` with
  `lambda: session` — the single-session funnel RESEARCH requires stays intact.
- `make_file` and sibling factories (`conftest.py:415-445`, `448+`): call `await session.commit()` —
  these in-test commits are exactly what `create_savepoint` must intercept-yet-rollback.

**Target mechanism (RESEARCH supersedes the D-07 wording):** session-scoped engine (schema created
ONCE) + per-test outer transaction on ONE connection + `AsyncSession(bind=conn,
join_transaction_mode="create_savepoint")` + `await outer.rollback()` on teardown. Do **NOT** hand-roll
the `after_transaction_end` / `begin_nested()` event listener — `create_savepoint` IS that recipe,
built into SQLAlchemy 2.0. Recommended fixture shape is in `92-RESEARCH.md:107-142`.

**Import conventions to match** (`conftest.py:10-13`):
```python
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
```

**Seed-once corollary:** the `test-fileserver` FK-parent row (currently `conftest.py:213-215`) must be
committed ONCE in the session-scoped engine setup (durably, OUTSIDE per-test txns) so `make_file`'s
`agent_id="test-fileserver"` FK target survives all tests. `make_file` at `conftest.py:431` hardcodes
that id.

> ### 🔴 LOAD-BEARING LANDMINE — the independent-verify-session tests (planner MUST resolve)
> **21 call sites across 13 non-integration files** (grep-verified 2026-07-13; excludes conftest.py's own fixture def + tests/integration) build an INDEPENDENT session with
> `async_sessionmaker(async_engine)` to prove a router/task actually COMMITTED (reading committed rows
> from a *different* connection). Canonical example — `tests/review/routers/test_duplicates.py:314-317`:
> ```python
> verify_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
> async with verify_factory() as verify:
>     markers = (await verify.execute(select(DedupResolution.file_id))).scalars().all()
>     assert list(markers) == [dup.id], "resolve did not COMMIT the dedup marker"
> ```
> Under `create_savepoint`, an in-test `commit()` is only a SAVEPOINT release inside the
> **uncommitted** outer transaction. A `verify_factory` session on a *different* pool connection will
> NOT see those rows (read-committed) → **these tests break**. This directly collides with RESEARCH
> Pitfall 3 ("every session must share the one outer-transaction connection").
>
> **Files that use this pattern** (planner must migrate each — bind verify sessions to the shared
> per-test connection, or provide a `verify`-fixture bound to `_db_connection`):
> `tests/review/routers/test_duplicates.py` (5), `tests/analyze/tasks/test_reconcile_cloud_jobs.py` (3),
> `tests/agents/routers/test_agent_push.py` (2), `tests/analyze/test_force_skip_writer.py` (2),
> `tests/agents/routers/test_agent_s3.py`, `tests/analyze/test_retry_affordances.py`,
> `tests/analyze/tasks/test_submit_cloud_job.py`, `tests/analyze/tasks/test_recovery.py`,
> `tests/analyze/tasks/test_release_awaiting_cloud.py`, `tests/analyze/core/test_dispatch_snapshot.py`,
> `tests/analyze/core/test_staging_cron.py`, `tests/analyze/core/test_reenqueue.py`,
> `tests/discovery/tasks/test_scan_reaper.py`, `tests/integration/test_drain_double_dispatch.py`.
> Note: `tests/integration/**` builds its OWN engine (`tests/integration/conftest.py:41,97`) against
> a real broker DB and does NOT consume the `async_engine`/`session` fixtures — leave those untouched
> (they're auto-marked `integration` at `conftest.py:145-164` and manage their own DB). Grep-verify
> per RESEARCH A4 before landing.
>
> **SEPARATE, BROADER failure class (NOT a verify-session site):** `tests/analyze/core/test_stage_progress.py` and
> `tests/shared/routers/test_pipeline.py` seed via `session.commit()` then read counts from **`get_stage_progress`**,
> whose PRODUCTION fan-out (92-02) opens its OWN `phaze.database.async_session` on a DIFFERENT pool connection — so it
> reads ZERO/STALE under create_savepoint. This is fixed by **plan 92-03 Task 2** (route the fan-out through the per-test
> connection), NOT by rebinding a verify session. Do NOT migrate those two files in 92-04.

**Integration/migration exclusion is already structural** — `conftest.py:145-164`
(`pytest_collection_modifyitems`) auto-marks anything consuming `DB_FIXTURES`
(`conftest.py:45`) or under `tests/integration/` / `test_migrations/`. Adding a new
`_db_connection`/`verify` fixture: decide whether it joins `DB_FIXTURES` for auto-marking.

---

### `tests/shared/test_conftest_hermeticity.py` — CLEAN-02 fixture-contract test (**NEW**)

**Placement analog:** `tests/shared/test_conftest_dsn_coercion.py` — an existing infra test that
imports from `tests.conftest` and asserts on the fixture layer itself. Same bucket (`shared`), same
"test the test-infra" role. Its header docstring style + `from tests.conftest import ...` import
(`test_conftest_dsn_coercion.py:1-13`) is the template.

**Behavioral analog:** `tests/review/routers/test_duplicates.py:298-347` — the commit-then-read-from-
an-independent-session assertion shape (see the LANDMINE box). The new test asserts the OTHER half:
that an in-test commit IS visible to a sibling read **on the shared connection**, AND that the next
test sees a clean DB (no surviving row).

**What the test must prove (RESEARCH Wave-0 Gap + `feedback_mutation_test_guard_tests`):**
1. commit inside a test is visible to a sibling read bound to the SAME connection;
2. the next test sees a clean DB (no `test-agent-01` / `legacy-application-server` survivor →
   no `pk_agents` collision);
3. **mutation-safety:** reverting the fixture to function-scoped `create_all`/`drop_all` must make the
   seed-collision return (a green fixture proves nothing on its own — break it, watch it fail, restore).

---

### `src/phaze/services/backends.py:563-566` — CLEAN-03 / D-09 (cosmetic)

Lines 563-564 and 565-566 are **byte-identical** (confirmed):
```python
# MKUE-01/D-04: thread THIS backend's KubeConfig so every get_job/get_workload_for/
# delete_job inside reconcile targets the file's own cluster.
```
Delete ONE copy (keep lines 563-564, drop 565-566). Zero runtime change. No analog needed.

---

### `src/phaze/routers/agent_files.py:131-135` — CLEAN-03 / D-10 (cosmetic)

Stale DISCOVERED-stamp comment inside the `on_conflict_do_update` block (`agent_files.py:131-135`).
Reword only; the code (`ON CONFLICT` never overwrites `state`) is correct — the comment references the
Phase-90-removed `files.state` semantics. Zero runtime change. No analog needed.

> Anti-drift note: RESEARCH §Validation flags `tests/shared/test_partition_guard.py` and existing
> anti-drift guards must still pass after the comment edits — verify no guard greps these exact lines.

---

## Shared Patterns

### Per-source degrade discipline (never-raise into the 5s poll)
**Source:** `src/phaze/services/pipeline.py:319-336` (`_safe_count`), `:339-376` (`_safe_bucket_counts`),
also mirrored by `get_stage_controls` (`:602-632`).
**Apply to:** every parallelized read in CLEAN-01, AND the new session-acquisition guard.
```python
try:
    return int((await session.execute(stmt)).scalar() or 0)
except Exception:
    logger.warning("stage_progress_degraded", node=node, exc_info=True)
    try:
        await session.rollback()
    except Exception:
        logger.warning("stage_progress_rollback_failed", node=node, exc_info=True)
    return 0
```

### Bounded async fan-out
**Source:** `src/phaze/tasks/discogs.py:59-66` (Semaphore + gather); `src/phaze/database.py:45,50`
(sessionmaker + `async with async_session()`).
**Apply to:** `get_stage_progress` CLEAN-01 fan-out.

### Commit-then-independent-session read (the invariant CLEAN-02 must preserve)
**Source:** `tests/review/routers/test_duplicates.py:298-347`; MEMORY `project_get_session_never_commits`
(`get_session` at `database.py:48-51` NEVER commits; mutating routers commit themselves;
`client` override funnels `get_session → lambda: session` at `conftest.py:234`).
**Apply to:** the `create_savepoint` fixture wiring — commits stay visible to same-connection siblings,
roll back at teardown.

### Pool-cap constraint (load-bearing)
**Source:** `src/phaze/database.py:24-43` (the incident comment + `pool_size=5/max_overflow=5`);
MEMORY `project_pgbouncer_pool_exhaustion`.
**Apply to:** the D-03 Semaphore sizing decision.

### D-05 perf harness (REUSE, do not rebuild)
**Source:** `scripts/perf_explain.py` (`time_stage_progress()` / `time_endpoint()`),
`scripts/seed_perf_corpus.py`, `just perf-db-up / perf-seed / perf-explain` (all Phase 82).
**Apply to:** before/after 200K measurement → `92-VERIFICATION.md`.
**Integration caveat (RESEARCH Open Q1 / A3):** after CLEAN-01, `get_stage_progress` opens its own
sessions from the module-level `phaze.database.async_session` (bound to `settings.database_url` at
import), bypassing the bench's `dependency_overrides[get_session]`. Run the bench with
`PHAZE_DATABASE_URL=<perf dsn>` so the internal fan-out sessions hit the perf DB — Wave-0 smoke check.

---

## No Analog Found

None. Every file has an in-repo analog. The only genuinely NEW mechanism —
`join_transaction_mode="create_savepoint"` — has no prior codebase usage (grep confirms zero
`join_transaction_mode` / `AsyncSession(bind=` in `tests/`), so the planner sources it from
`92-RESEARCH.md:107-142` (SQLAlchemy 2.0 docs, cited), not from an existing file.

| Concern | Role | Data Flow | Reason |
|---------|------|-----------|--------|
| `create_savepoint` fixture wiring | test-infra | transactional | No prior use in repo; follow RESEARCH recipe (cited SQLAlchemy 2.0 docs) |

---

## Metadata

**Analog search scope:** `src/phaze/{services,tasks,routers,database.py}`, `tests/{shared,review,agents,
analyze,discovery,integration}/`, `scripts/`, `tests/buckets.json`.
**Files scanned:** ~20 read/grepped; 21 independent-verify-session call sites (13 non-integration files) enumerated.
**Pattern extraction date:** 2026-07-13
