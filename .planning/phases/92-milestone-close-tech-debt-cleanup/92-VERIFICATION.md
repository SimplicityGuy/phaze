# Phase 92 Verification

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
