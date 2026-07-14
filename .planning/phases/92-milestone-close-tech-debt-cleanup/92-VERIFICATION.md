---
phase: 92-milestone-close-tech-debt-cleanup
verified: 2026-07-13T21:00:00Z
status: passed
score: 8/8 must-haves verified
overrides_applied: 0
---

# Phase 92: Milestone-Close Tech-Debt Cleanup Verification Report

**Phase Goal:** Pay down the tech debt surfaced by the 2026.7.5 milestone audit before completing
the milestone. Behavior-preserving except the PERF-02 latency win; small blast radius per item.
CLEAN-01 = parallelize `get_stage_progress` bucket-count reads via `asyncio.gather` (PERF-02
follow-up) + re-measure 200K poll latency. CLEAN-02 = fix the non-hermetic test flakes (83-01/83-03
class) so the full suite passes green under per-bucket CI isolation for EVERY bucket in
`tests/buckets.json` (D-08). CLEAN-03 = two comment-only doc fixes.

**Verified:** 2026-07-13T21:00:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `get_stage_progress` fans its independent reads out concurrently via `asyncio.gather`, each in its own `AsyncSession`, bounded by a per-poll `Semaphore(4)` | ✓ VERIFIED | `src/phaze/services/pipeline.py:499-672` — `_stats_fanout()` builds a fresh loop-bound `Semaphore(4)` per poll; `_read_in_own_session` wraps every read; a single `asyncio.gather(...)` call at :657-672 dispatches all 13 independent reads. Code-reviewed (92-REVIEW.md): "gather-order matches unpacking order 1:1... asyncio.gather cannot propagate because every task swallows Exception." |
| 2 | The fan-out is degrade-safe — a pool-timeout on session acquisition returns the safe default rather than aborting the poll | ✓ VERIFIED | `_read_in_own_session` (pipeline.py:518-543) catches the acquisition `TimeoutError` outside `fn` and returns `default`; `_safe_count`/`_safe_bucket_counts` reused verbatim (never raise). Confirmed by code review (no BLOCKER: "the one behavior that could crash the hot poll (pool saturation) is degrade-safe by construction"). |
| 3 | Before/after 200K `/pipeline/stats` poll latency is recorded, and the DENORM-01 disposition follows from the measured numbers (D-05) | ✓ VERIFIED | 92-VERIFICATION.md "PERF-02 Re-measurement" section (preserved below): DIRECT p50 1468.9→860.6 ms (−41%), endpoint p50 1737.5→1072.2 ms (−38%); DENORM-01 stays deferred/killed with reasoning recorded. |
| 4 | The returned dict shape/key order/derived `done` buckets are unchanged on a quiescent DB (no observable behavior regression from the parallelization, aside from the latency win) | ✓ VERIFIED | `tests/analyze/core/test_stage_progress.py` and `tests/shared/routers/test_pipeline.py` (part of the 1084-passed `shared` bucket, independently re-run — see below) assert the 9-key dict shape; `tests/shared/test_conftest_hermeticity.py::test_production_fanout_sees_in_test_seeded_row` independently re-asserts `analyze.done==1`/`total==1`/`metadata.done==0` against the real fan-out. |
| 5 | The 83-01/83-03 non-hermetic test-flake class is fixed at the shared conftest root (session-scoped engine + per-test `create_savepoint` outer transaction), not patched per-bucket | ✓ VERIFIED | `tests/conftest.py:246-332` — session-scoped `_db_connection`/`async_engine`, per-test `session` fixture using `AsyncSession(bind=_db_connection, join_transaction_mode="create_savepoint")`, `verify` sibling fixture, and `_route_stats_fanout` routing the production fan-out onto the same connection. Mutation-safe contract test `tests/shared/test_conftest_hermeticity.py` proves both rollback isolation and production-fan-out visibility with documented mutation recipes (not a toothless guard) — independently re-run GREEN in this verification pass. |
| 6 | The full suite passes green under per-bucket CI isolation for EVERY bucket in `tests/buckets.json` (D-08) | ✓ VERIFIED | 92-VERIFICATION.md "CLEAN-02 D-08 per-bucket gate" section (preserved below) records all 9 buckets green. **Independently re-run in this verification pass** (see Behavioral Spot-Checks) — every count matches exactly: discovery 172, fingerprint 84, analyze 571, identify 242, review 444 (combined run: 1513), agents 460, integration 248, shared 1084, plus `tests/metadata` 93 + hermeticity/traceability tests (combined run: 106). Total independently observed: 3,411 passed, 0 failed. |
| 7 | The duplicated `backends.py` KubeConfig comment appears exactly once (D-09), and the stale `agent_files.py` DISCOVERED-stamp/`files.state` comment is corrected (D-10), with zero runtime behavior change | ✓ VERIFIED | `grep -c "thread THIS backend's KubeConfig" src/phaze/services/backends.py` → 1 (confirmed directly). `agent_files.py:126-134` — comment now describes the current `set_` dict (hash/size/batch/file_type refresh only); no `files.state`/`DISCOVERED`/`data["state"]` reference found via direct grep. `git diff main...HEAD` on both files touches comment lines only (per 92-01-SUMMARY and independently confirmed no `set_=` key or logic-line changes in the diff). |
| 8 | CLEAN-01/CLEAN-02/CLEAN-03 are registered in `.planning/REQUIREMENTS.md` with no orphaned requirement IDs for Phase 92 | ✓ VERIFIED | `.planning/REQUIREMENTS.md:104-106` (checkboxes) and `:180-182` (Traceability rows, `Phase 92 | Pending`). All 3 IDs map to a plan's `requirements:` frontmatter (92-01→CLEAN-03, 92-02→CLEAN-01, 92-04/92-05→CLEAN-02); no unclaimed Phase-92 ID found in REQUIREMENTS.md. `Pending`/unchecked is the CORRECT state at verification time — per 92-05-SUMMARY and the DOCS-01 guard's documented in-flight tolerance (D-05), the checkbox/status flip to `[x]`/`Complete` happens downstream at milestone-close, not during phase verification. Traceability guard test (`tests/shared/core/test_requirements_traceability.py`) independently re-run GREEN in this pass. |

**Score:** 8/8 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/services/pipeline.py` | Parallelized `get_stage_progress` fan-out | ✓ VERIFIED | Contains `asyncio.gather` over `_read_in_own_session`, bounded `_stats_fanout()` seam; wired (called by `/pipeline/stats` router and covered by 3 test files). |
| `.planning/phases/92-milestone-close-tech-debt-cleanup/92-VERIFICATION.md` | Recorded before/after 200K perf numbers + D-08 gate | ✓ VERIFIED | Both sections present and preserved (see below); this file is the same file being extended, not discarded. |
| `src/phaze/services/backends.py` | De-duplicated KubeConfig comment | ✓ VERIFIED | `grep -c` confirms exactly 1 occurrence. |
| `src/phaze/routers/agent_files.py` | Corrected ON-CONFLICT comment | ✓ VERIFIED | Direct read confirms comment matches current `set_` dict; no stale `files.state` reference. |
| `tests/conftest.py` | Session-scoped engine + per-test `create_savepoint` session + `verify` fixture + fan-out routing | ✓ VERIFIED | All four elements present at the cited line ranges; exercised by the mutation-safe contract test. |
| `tests/shared/test_conftest_hermeticity.py` | Mutation-safe hermeticity contract test | ✓ VERIFIED | 107-line file; 3 tests, documented mutation recipes; independently re-run GREEN. |
| `.planning/REQUIREMENTS.md` | CLEAN-01/02/03 checkboxes + Traceability rows | ✓ VERIFIED | Present at lines 104-106 and 180-182; no orphaned Phase-92 IDs. |
| 13 migrated verify-site test files (92-04) | Verify reads via shared per-test connection fixture | ✓ VERIFIED | `tests/review/routers/test_duplicates.py` and siblings pass under per-bucket isolation (review 444 passed, independently re-run). |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `get_stage_progress` reads | `phaze.database.async_session` | per-task session acquisition (`_read_in_own_session`) | ✓ WIRED | Confirmed by grep + code review; production callers (`/pipeline/stats` router) unchanged signature. |
| `tests/conftest.py` `_route_stats_fanout` | `phaze.database.async_session` | `monkeypatch.setattr` to a `_db_connection`-bound `create_savepoint` sessionmaker + `Semaphore(1)` | ✓ WIRED | `tests/conftest.py:260-287`; directly exercised and proven by `test_production_fanout_sees_in_test_seeded_row` (independently re-run GREEN). |
| `tests/conftest.py` `session`/`client` override | `get_session` | single-connection funnel | ✓ WIRED | Session-scoped `_db_connection` + per-test `session`/`verify` both bind to it; commit-visibility proven by the two `_probe_agent_*` tests. |

### Data-Flow Trace (Level 4)

Not applicable in the strict sense (no UI-rendered dynamic data component in this phase), but the
production-fan-out data path was traced end-to-end and independently confirmed non-hollow:
`test_production_fanout_sees_in_test_seeded_row` seeds a real `AnalysisResult` row, calls the REAL
(unmocked) `get_stage_progress`, and asserts `analyze.done == 1` / `total == 1` / `metadata.done == 0`
— proving the routed fan-out reads real per-test data, not a static/degraded default. Independently
re-run GREEN as part of the `shared` bucket (1084 passed).

### Behavioral Spot-Checks

All 9 `tests/buckets.json` buckets were independently re-run against the live test DB
(`localhost:5433`) in this verification pass (not merely re-read from SUMMARY claims):

| Bucket | Command | Result | Status |
|--------|---------|--------|--------|
| shared conftest hermeticity + traceability guard + metadata | `pytest tests/shared/test_conftest_hermeticity.py tests/shared/core/test_requirements_traceability.py tests/metadata` | 106 passed | ✓ PASS |
| agents | `pytest tests/agents` | 460 passed | ✓ PASS (matches claimed 460 exactly) |
| integration | `pytest tests/integration` | 248 passed | ✓ PASS (matches claimed 248 exactly) |
| shared | `pytest tests/shared` | 1084 passed | ✓ PASS (matches claimed 1084 exactly) |
| discovery + fingerprint + analyze + identify + review | `pytest tests/discovery tests/fingerprint tests/analyze tests/identify tests/review` | 1513 passed | ✓ PASS (sum of claimed 172+84+571+242+444=1513, exact match) |
| ruff check (touched src files) | `uv run ruff check src/phaze/services/pipeline.py src/phaze/services/backends.py src/phaze/routers/agent_files.py tests/conftest.py` | All checks passed | ✓ PASS |
| mypy (touched src files) | `uv run mypy src/phaze/services/pipeline.py src/phaze/services/backends.py src/phaze/routers/agent_files.py` | Success: no issues found in 3 source files | ✓ PASS |
| anti-pattern scan (TBD/FIXME/XXX/TODO/HACK/PLACEHOLDER) on touched files | `grep -n -E "TBD\|FIXME\|XXX\|TODO\|HACK\|PLACEHOLDER"` on pipeline.py, backends.py, agent_files.py, conftest.py, test_conftest_hermeticity.py | none found | ✓ PASS |

**Independently observed total: 3,411 tests passed, 0 failed, across all 9 buckets** — this is a live
re-run in this verification session, not a re-statement of SUMMARY.md's claim. Every per-bucket count
matches the SUMMARY/prior-VERIFICATION claim exactly.

### Probe Execution

No `scripts/*/tests/probe-*.sh` probes are declared by this phase's PLAN/SUMMARY files, and none exist
under `scripts/`. Step 7c: SKIPPED (no declared or conventional probes for this phase).

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|--------------|-------------|--------------|--------|----------|
| CLEAN-01 | 92-02 | Parallelize `get_stage_progress` via `asyncio.gather`, re-measure 200K poll latency | ✓ SATISFIED | Code + perf numbers verified above. |
| CLEAN-02 | 92-03, 92-04, 92-05 | Fix non-hermetic test flakes; full suite green under per-bucket CI isolation (D-08) | ✓ SATISFIED | All 9 buckets independently re-run green; mutation-safe contract test passes. |
| CLEAN-03 | 92-01 | Two comment-only doc fixes (D-09, D-10) | ✓ SATISFIED | Both comments confirmed corrected; zero logic-line diff. |

No orphaned requirements: `.planning/REQUIREMENTS.md` Phase-92 rows (CLEAN-01/02/03) all map to a plan
that claims them in its `requirements:` frontmatter; no additional Phase-92 ID appears in
REQUIREMENTS.md without a claiming plan.

### Anti-Patterns Found

None blocking. Code review (92-REVIEW.md, independently read and cross-checked) found 0
Critical/Blocker, 4 Warning, 2 Info — all advisory:

- **WR-01** (pool-checkout headroom, `Semaphore(4)` raises peak poll checkout to ~5 against the lean
  10-conn pool) — degrade-safe by construction, accepted design trade-off per REVIEW.
- **WR-02** (independent per-read snapshots can transiently break `done <= total` under concurrent
  writes) — documented, self-correcting for a 5s single-user poll, accepted per REVIEW.
- **WR-03** (`verify` fixture correctness relies on call-site parameter order rather than an explicit
  `session` dependency) — a real but low-risk latent footgun for future test authors; does not affect
  current suite hermeticity (proven GREEN + mutation-tested). Not a phase-goal blocker.
- **WR-04** (dead `async_engine` parameter retained in several test helpers after the routing rewrite)
  — cosmetic, test-only, does not affect goal achievement.
- **IN-01, IN-02** — informational, no action required for phase-goal achievement.

None of these are BLOCKER-class per the code reviewer, and none contradict any of the 8 observable
truths verified above. WR-03 and WR-04 are legitimate follow-up polish items but do not block the
phase goal (test suite is green, hermetic, and mutation-tested regardless of parameter-order fragility
that has not yet manifested).

### Human Verification Required

None. All truths are verified programmatically (live test execution against a running Postgres/Redis
test stack + direct code/diff inspection). No UI, visual, or subjective-judgment truths exist in this
phase's scope.

### Gaps Summary

No gaps. All 8 must-have truths (roadmap goal decomposed: CLEAN-01 parallelization + perf verdict,
CLEAN-02 hermeticity fix + D-08 gate, CLEAN-03 comment fixes, requirements bookkeeping) are VERIFIED
against the live codebase and a live re-run of the full 9-bucket suite (3,411 tests, 0 failures,
independently reproduced in this verification session — not merely re-stated from SUMMARY.md). The two
prior-recorded sections (PERF-02 re-measurement, CLEAN-02 D-08 per-bucket gate) are preserved unchanged
below and corroborated by this session's independent re-run.

---

## PERF-02 Re-measurement (D-05)

CLEAN-01 parallelizes `get_stage_progress`'s independent reads via `asyncio.gather` over
bounded per-task `AsyncSession`s. This section records the before/after `/pipeline/stats`
poll latency at 200K scale, which decides DENORM-01's disposition (D-05 / SC1).

### Measurement environment (reproducible)

| Item | Value |
|------|-------|
| Perf DB | dedicated `postgres:18-alpine`, container `phaze-perf-db`, **host port 5545**, DB `phaze_perf82` (via `just perf-db-up`) |
| Perf DB DSN (asyncpg raw) | `postgresql://phaze:phaze@localhost:5545/phaze_perf82` |
| Perf DB DSN (SQLAlchemy) | `postgresql+asyncpg://phaze:phaze@localhost:5545/phaze_perf82` |
| Redis (SAQ cache handle) | `redis://localhost:6380/0` (running `phaze-test-redis`) |
| Corpus | N=200000 music/video files at migration **HEAD (039)** — D-06 mid-pipeline profile (metadata=140000, fingerprint=114000, analysis=90000, cloud_job=2000, dedup=4000, ledger=5000) |
| Iterations | ITER=20 (after one excluded warm-up), p50/p95 reported |
| Instrument | `scripts/perf_explain.py` (Phase 82) — `time_stage_progress()` (DIRECT) + `time_endpoint()` (full ASGI route) |

**Reproduction commands** (note the 5545 perf-DB port — NOT the 5432/5433 test-DB ports, RESEARCH Pitfall 5):

```bash
# 1. Bring up the dedicated perf DB (host port 5545).
just perf-db-up

# 2. Migrate + seed. The 200K seeder writes files.state (dropped by migration 039) and
#    files owned by the legacy sentinel (deleted by migration 038), so a one-shot
#    `alembic upgrade head` cannot be used on a fresh DB (see landmine below). Stage it:
PERF_SA="postgresql+asyncpg://phaze:phaze@localhost:5545/phaze_perf82"
PERF_RAW="postgresql://phaze:phaze@localhost:5545/phaze_perf82"
PHAZE_DATABASE_URL="$PERF_SA" uv run alembic upgrade 037            # schema + legacy sentinel (revoked) present, files.state exists
uv run python scripts/seed_perf_corpus.py --n 200000 --dsn "$PERF_RAW" --reseed
docker exec phaze-perf-db psql -U phaze -d phaze_perf82 -c \
  "INSERT INTO agents (id,name,kind,scan_roots) VALUES ('perf-fileserver','perf-fileserver','fileserver','[]'::jsonb) ON CONFLICT DO NOTHING;"
PHAZE_DATABASE_URL="$PERF_SA" uv run alembic upgrade 038            # reattribute legacy files -> perf-fileserver, delete sentinel
PHAZE_DATABASE_URL="$PERF_SA" uv run alembic -x force=1 upgrade head  # 039 drops files.state (force past mid-flight guard on the throwaway perf DB)

# 3. Bench. PHAZE_DATABASE_URL routes BOTH the harness's local engine (--dsn) AND the
#    module-level phaze.database.async_session (the AFTER fan-out seam) to the perf DB.
PHAZE_DATABASE_URL="$PERF_SA" PHAZE_REDIS_URL="redis://localhost:6380/0" \
  uv run python scripts/perf_explain.py --dsn "$PERF_RAW" --iterations 20
```

### Migration-038/039 staging landmine (recorded for the reviewer)

A fresh perf DB cannot reach HEAD with a single `alembic upgrade head`:

- **Migration 038** (`retire_legacy_sentinel`) hard-aborts with `No non-revoked fileserver
  agent exists; cannot reattribute` — the seeder's files are owned by the legacy sentinel
  (seeded *revoked* by migration 012), and 038 requires a *non-revoked* fileserver
  reattribution target (MEMORY `project_legacy_sentinel_retirement`, CR-02). Fix: seed at
  037, insert a non-revoked `perf-fileserver`, then run 038 (it reattributes the 200K
  legacy-owned files to `perf-fileserver` and deletes the sentinel).
- **Migration 039** (`drop_files_state_column`) aborts with `857 mid-flight row(s)` because
  the seeded corpus carries active `cloud_job` rows — its guard refuses to drop `files.state`
  while bytes are notionally in transit. On the *throwaway perf DB* this is safe to force:
  `alembic -x force=1 upgrade head`.

Because the whole `upgrade head` runs in one transaction, the 039 abort rolls back 038 too —
hence the explicit `alembic upgrade 038` step before the forced 039.

### Routing confirmation (Open Question 1 — RESOLVED, no harness tweak needed)

After CLEAN-01, `get_stage_progress` opens its own sessions from the module-level
`phaze.database.async_session`, which binds to `settings.database_url` **at import**.
Exporting `PHAZE_DATABASE_URL=<perf SA dsn>` **before** the process starts binds that engine
to the perf DB. Verified directly:

```
engine.url = postgresql+asyncpg://phaze:***@localhost:5545/phaze_perf82
files via module-level async_session = 200000
```

So the AFTER fan-out reads hit the perf DB, not the app default. **No edit to
`scripts/perf_explain.py` was required** — the documented env export is sufficient.

### Before (serial)

Current serial `get_stage_progress` (13 sequential awaits on one session), N=200000, ITER=20:

| Instrument | p50 | p95 | min | max |
|------------|-----|-----|-----|-----|
| `get_stage_progress()` DIRECT | **1468.9 ms** | 1528.9 ms | 1311.3 ms | 1530.6 ms |
| `GET /pipeline/stats` (full ASGI) | **1737.5 ms** | 1799.0 ms | 1685.3 ms | 1821.5 ms |

Reproduces the Phase-82 baseline in the same regime (Phase-82: DIRECT ~1290.9 ms, endpoint
~1405.3 ms p50; this run is on a busier host). Both are well over the `< ~1 s` D-07/SC1
budget — the serial critical path is dominated by the three enrich `GROUP BY
stage_status_case` bucket reads. (The endpoint number degrades the Redis pipeline-counter
read to `{}` because the ASGI test app skips the lifespan — a pre-existing harness property
that is identical before and after, so it does not bias the delta.)

### After (parallelized)

CLEAN-01 fan-out (`asyncio.gather` over per-task sessions, `Semaphore(4)`), SAME N=200000 perf DB,
ITER=20. `PHAZE_DATABASE_URL=<perf sa dsn>` confirmed the internal `async_session()` fan-out hit the
perf DB (routing proof above):

| Instrument | Before p50 | After p50 | Before p95 | After p95 | Δ p50 |
|------------|-----------|-----------|-----------|-----------|-------|
| `get_stage_progress()` DIRECT | 1468.9 ms | **860.6 ms** | 1528.9 ms | 913.9 ms | −41.4% |
| `GET /pipeline/stats` (full ASGI) | 1737.5 ms | **1072.2 ms** | 1799.0 ms | 1120.9 ms | −38.3% |

(After DIRECT: min 794.2 / max 925.9 ms, n=20. After endpoint: min 1014.4 / max 1202.2 ms, n=20.)

### Verdict (SC1: `< ~1 s`)

- **`get_stage_progress()` DIRECT p50 = 860.6 ms — UNDER the `< ~1 s` budget.** This is the
  DENORM-relevant number: the pure DB cost a denormalized stage-bitmap (DENORM-01) would replace.
  Parallelizing the three heavy enrich `GROUP BY stage_status_case` reads (the serial-cost dominators)
  collapsed the critical path from ~1.29–1.47 s to ~0.86 s exactly as RESEARCH's Pool-Headroom
  projection predicted (~450–650 ms + host overhead).
- **`GET /pipeline/stats` full-endpoint p50 = 1072.2 ms — marginally ABOVE `~1 s`** on this host.
  Host caveat: the BEFORE run reproduced the Phase-82 baseline ~14–24% HIGH (DIRECT 1468.9 vs 1290.9;
  endpoint 1737.5 vs 1405.3 ms p50), i.e. this bench host runs ~1/5 slower than the Phase-82
  reference. Normalized to that reference the endpoint p50 is ≈ 860 ms — under budget. The residual
  ~72 ms over 1 s is NOT in the stage-progress reads (those are the 860 ms DIRECT core) but in the
  other endpoint components (`get_queue_activity`, `get_stage_controls`, `get_global_reconciliation`,
  template render, ASGI) — which DENORM-01 does not touch.

### DENORM-01 disposition (D-05)

**DENORM-01 stays DEFERRED / killed.** Its target — the `get_stage_progress` stage-count DB reads —
is now UNDER the `< 1 s` budget at 200K (860.6 ms DIRECT), so denormalizing a per-stage bitmap column
would buy nothing against SC1. The full-endpoint p50 sits ~7% over `1 s` on a ~20%-slow bench host
and normalizes to under budget on the Phase-82 reference host; the excess, when present, lives OUTSIDE
the reads DENORM-01 would replace. Tie-break rule (D-05, honored): if a quiescent reference-host
re-measure ever shows `/pipeline/stats` p50 durably over `1 s`, the escalation is (a) profile the
non-stage-progress endpoint components, then (b) DENORM-01 as a live v2 candidate — NEVER
`db_pool_size` inflation (PgBouncer session-mode pinning risk, post-exhaustion-incident lean pool).

### Snapshot-skew caveat (RESEARCH Pitfall 1)

The returned dict is byte-identical on a QUIESCENT DB (all tests + this quiescent perf bench assert
identical shape/values). Under concurrent writes the independent per-read sessions may reflect MVCC
snapshots microseconds apart — acceptable for a 5 s dashboard poll. This is NOT a claim of strict
byte-identity under live writes; the T-92-02-SKEW disposition (accept) documents it.

---

## CLEAN-02 D-08 per-bucket gate

The CLEAN-02 acceptance gate (D-08): the whole suite must pass GREEN under the project's per-bucket
CI isolation standard — every bucket in `tests/buckets.json` run cold, in its own process. This proves
the 92-03 `create_savepoint` rewrite + 92-04 verify-site migration + production fan-out routing +
CLEAN-01 parallelization + CLEAN-03 comment edits all coexist hermetically, with no cross-test state
leak, `pk_agents` collision, or zero/stale fan-out read.

### Environment (reproducible)

| Item | Value |
|------|-------|
| Test DB (SQLAlchemy) | `postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test` (`TEST_DATABASE_URL`) |
| Migrations test DB | `postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test` (`MIGRATIONS_TEST_DATABASE_URL`) |
| Redis (SAQ cache/broker handle) | `redis://localhost:6380/0` (`PHAZE_REDIS_URL`) |
| Containers | `phaze-test-db` (host port 5433), `phaze-test-redis` (host port 6380) |
| Runner | `just test-bucket <name>` (== `pytest tests/<name>`), each bucket cold in its own process |

Note the **5433** test-DB port (not the 5432 default, not the 5545 perf-DB port) — RESEARCH Pitfall 5.
No `--lf`/watch-mode/`-p no:logging` flags were used (each bucket ran clean from cold).

### Per-bucket results

| Bucket | Result | Verdict |
|--------|--------|---------|
| discovery | 172 passed | ✅ green |
| metadata | 93 passed | ✅ green |
| fingerprint | 84 passed | ✅ green |
| analyze | 571 passed | ✅ green |
| identify | 242 passed | ✅ green |
| review | 444 passed | ✅ green |
| agents | 460 passed | ✅ green |
| integration | 248 passed | ✅ green |
| shared | 1084 passed | ✅ green |

**All 9 buckets exit 0. D-08 gate SATISFIED.**

**Independently re-confirmed by the verifier** in this verification pass (2026-07-13, live re-run
against `phaze-test-db`/`phaze-test-redis`, not a re-statement of the SUMMARY claim): every count above
matches exactly, combined into 5 verification runs totaling 3,411 passed / 0 failed:
`agents`=460, `integration`=248, `shared`=1084, `discovery+fingerprint+analyze+identify+review`=1513
(=172+84+571+242+444), `metadata`+hermeticity-contract-test+traceability-guard=106.

### Failure classes exercised (and fixed) to reach green

The gate did NOT pass on the first pass — the 92-03 session-scoped-engine conversion exposed four
latent hermeticity defects (backlogged as DI-92-04-01 / DI-92-04-02, close-out owned by this plan):

1. **`tests/agents` — 5 failures (leaked committed rows).** `tests/agents/cli/test_agents_add.py`'s
   `test_main_*` cells commit agent rows via a real `create_async_engine` CLI path; under the
   session-scoped engine (no per-test `drop_all`) they survived into `test_agent_bootstrap.py` and made
   `ensure_dev_agent` see a non-empty table. Fixed with a self-cleaning `_cleanup_committed_agents`
   fixture that deletes every agent except the `test-fileserver` FK parent in teardown.
2. **`tests/integration` — 3 `test_drain_double_dispatch` failures (invisible seed).** The cells seeded
   via the single-connection `create_savepoint` `session` but `stage_cloud_window` reads through its own
   pool connections → zero candidates. Migrated to the `committed_db` fixture (committed seed, visible
   cross-connection), mirroring the 92-04 concurrency-cell move.
3. **`tests/integration` — `test_lifespan_orphan_task` (unreachable engine).** The module-level `engine`
   binds to the docker `postgres:5432` default at import (`socket.gaierror` at the lifespan `SELECT 1`);
   `TEST_DATABASE_URL` never steered it. Fixed by rebinding `phaze.main.engine`/`async_session` to the
   reachable test DB in the test.
4. **`tests/integration` — 74 `test_stage_status_equivalence` errors + an ordering cascade (blind FK
   seed).** The session-scoped `async_engine` seed and several `db_session` fixtures blind-INSERT
   `test-fileserver`, which collides once `committed_db` re-seeds a committed parent before the seeder
   runs. Made every such seed idempotent (get-or-insert), matching the guard three sibling fixtures
   already carried — the suite is now order-independent.

No residual `pk_agents` collision, no surviving-seed-row, and no zero/stale fan-out read remains in any
bucket. No colima VM-pressure flake was encountered (per-bucket isolation sidesteps whole-suite VM
pressure, RESEARCH Pitfall 6).

---

_Verified: 2026-07-13T21:00:00Z_
_Verifier: Claude (gsd-verifier)_

---

## Post-Audit Debt Paydown (2026-07-14)

After the milestone audit (`2026.7.5-MILESTONE-AUDIT.md`) flagged remaining code-level tech debt, the operator directed a full paydown on the phase-92 branch. 10 atomic commits, TDD (RED→GREEN) on every correctness fix, full 9-bucket D-08 re-gate GREEN afterward (**3,409 tests, 0 failed**):

| Group | Items | Result | Commits |
|-------|-------|--------|---------|
| A (P92 review) | perf seeder drops dropped `files.state` write; WR-04 dead `async_engine` params; WR-03 `verify` depends on `session` | fixed | 4057bbf0, 7b2c95f7, 5288edad |
| B (P81) | WR-01 metadata failure upsert guarded against clobbering a DONE row (+test); WR-02 = **test blind spot** (D-11 respected, NOT reopened; twin-divergence test doubles as D-11 mutation guard) | fixed | a14e8b50, 9fe72f8e |
| C (P85) | WR-01 review-builder starvation → keyset paging (read) + migration-free `NO_OP` `TagWriteLog` eviction (write); WR-02 single-tally count; WR-03 SQL-bounded cue eligible half; WR-04 documented | fixed | 995f80fc, 1b3dc323 |
| D (P83-06) | root cause was **STRANDING** (worse than the note's "mis-route") + the note's suggested fix was insufficient; operator-approved **Option A reverses locked D-09** — backfill produces a clean drainable held file, `stage_cloud_window` is single owner | fixed | 89f291b4, fa9df92d, 032fc711 |

Full D-08 re-gate: discovery 172 · metadata 94 · fingerprint 84 · analyze 570 · identify 242 · review 448 · agents 460 · integration 252 · shared 1087. The D-09 reversal is recorded in STATE.md Decisions.
