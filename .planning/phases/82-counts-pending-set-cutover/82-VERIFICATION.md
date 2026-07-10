# Phase 82 — VERIFICATION

Recorded manual/measured verifications for the Counts & Pending-Set Cutover phase. This file holds the
**PERF-02** measurement (the deliverable that licenses the DENORM-01 decision) and the **D-02** deploy-target
gate that must be re-checked at homelab rollout.

---

## PERF-02 — `/pipeline/stats` poll latency at 200K (the DENORM-01 licensing measurement)

**Requirement:** PERF-02 (D-06, D-07). Prove the derived read queries stay fast at scale — or surface that
they do not — on a corpus that carries the migration-032 partial indexes the anti-joins ride, and make the
DENORM-01 go/no-go call against the `< ~1s` budget (D-07).

### Measurement provenance (Pitfall 5 / D-06 — READ THIS FIRST)

The numbers below were taken on a **LOCAL, dedicated, synthetic corpus at migration HEAD (036)**, NOT against
live lux. This is mandatory: prod is at Alembic ~031 and **lacks** the 032 partial indexes, so a live EXPLAIN
would show pessimistic Seq Scans and misjudge the plan (D-06). The measurement is only valid where the
indexes exist.

- **DB:** dedicated ephemeral `postgres:18-alpine` container (`just perf-db-up`, host port 5545, DB
  `phaze_perf82`) — a SEPARATE container from the shared `phaze-test-db` so a concurrent `just test-db`
  recreate cannot wipe the corpus mid-measurement (observed: the shared container was recreated by a sibling
  session and dropped an earlier perf DB — hence the dedicated container).
- **Migration head:** `036` (confirmed `alembic current` → `036 (head)`; ≥036 satisfies D-02's index +
  backfill precondition).
- **Host:** Apple-Silicon dev host under colima. Absolute milliseconds are host-relative; the transferable
  finding is the RELATIVE story (which query shape dominates, and that it is sequential).
- **Harness:** `scripts/seed_perf_corpus.py` (seed) + `scripts/perf_explain.py` (EXPLAIN + timing), driven by
  `just perf-seed` / `just perf-explain`. Warm-up iteration excluded; 20 timed iterations; single connection.

### Seed parameters (D-06 selectivity profile, N=200,000)

`just perf-seed 200000` → measured row counts on the seeded DB:

| Table / predicate | Rows | Share of N |
|---|---:|---:|
| `files` total | 200,000 | 100% |
| `files` music/video (in `MUSIC_VIDEO_TYPES`) | 194,000 | 97% |
| `files` non-media (`txt`, exercises the file_type scope) | 6,000 | 3% |
| `metadata` rows | 140,000 | 70% |
| `metadata.failed_at` set (metadata FAILED bucket) | 4,000 | 2% |
| `fingerprint_results` status=`success` | 110,000 | 55% |
| `fingerprint_results` status=`failed` (failure-only) | 4,000 | 2% |
| `analysis.analysis_completed_at` set (DONE) | 80,000 | 40% |
| `analysis.failed_at` set (terminal, XOR-disjoint from DONE) | 10,000 | 5% |
| `cloud_job` rows (cycling active statuses) | 2,000 | 1% |
| `cloud_job` status=`awaiting` | 286 | ~0.14% |
| `dedup_resolution` markers | 4,000 | 2% |
| `scheduling_ledger` in-flight rows (process_file / extract_file_metadata / fingerprint_file) | 5,000 | 2.5% |
| `saq_jobs` provisioned (idle, for the endpoint queue-activity reads) | table present | — |

`FileRecord.state` was stamped to the furthest reached stage (dual-write realism). Every insert used
parameterized `unnest`-array bulk INSERTs (T-82-07 — no f-string SQL); `--reseed` is hard-gated to a
perf-named DB.

### Headline timings (authoritative — direct, warm, 20 iterations)

| Measurement | p50 | p95 | min | max | vs `< ~1s` budget (D-07) |
|---|---:|---:|---:|---:|:--|
| `get_stage_progress()` DIRECT (the DENORM-relevant DB core) | **1290.9 ms** | 1419.2 ms | 1130.7 ms | 1549.8 ms | **OVER** |
| `GET /pipeline/stats` full endpoint (ASGI, real route) | **1405.3 ms** | 1489.1 ms | 1322.1 ms | 1533.7 ms | **OVER** |

The endpoint is ~115 ms above `get_stage_progress` alone — the remaining per-node counts (awaiting / straggler /
failed / pushing / pushed / inadmissible / cloud-phase / lanes), the queue-activity reads, and the template
render. **`get_stage_progress` dominates**, and within it the three enrich four-bucket reads dominate (below).

> **Both numbers exceed the `< ~1s` D-07 budget at 200K.** This is recorded as measured — not smoothed.

### Per-query EXPLAIN (ANALYZE, BUFFERS) @ 200,000 files

Each shape was rebuilt from the REAL clause builders (`eligible_clause` / `dedup_resolved_clause` /
`stage_status_case`) and compiled to literal-bound SQL, so the plan is the ACTUAL query the app issues.

| Query shape | Exec time (ANALYZE, w/ per-node timing) | Rows out | 032 partial index used? |
|---|---:|---:|:--|
| `get_metadata_pending_files` | 103.97 ms | 53,550 | No — non-selective done anti-join → Parallel Hash Anti Join |
| `get_fingerprint_pending_files` | 202.64 ms | 79,333 | No — non-selective done anti-join → Hash Anti Join |
| `get_discovered_files_with_duration` (analyze pending + LEFT JOIN + cloud-exclusion) | 239.30 ms | 97,465 | **Yes — `ix_analysis_failed`** (Bitmap Index Scan on the ~failed(ANALYZE) branch) |
| `four_bucket[metadata]` (GROUP BY `stage_status_case`) | 726.34 ms (TIMING OFF ≈ 363–397 ms) | 4 | **Yes — `ix_metadata_failed`** (Bitmap Index Scan, failed branch); Seq Scan on files for the CASE |
| `four_bucket[fingerprint]` | 433.54 ms (TIMING OFF ≈ 441 ms) | 4 | No — success predicate (110K/114K) non-selective → Seq Scans on `fingerprint_results` |
| `four_bucket[analyze]` | 401.01 ms (TIMING OFF ≈ 339 ms) | 4 | **Yes — `ix_analysis_failed`** (Bitmap Index Scan, failed branch); Seq Scan on `analysis` for DONE |

Verbatim evidence — a 032 partial index IS chosen where selective (the failure-marker anti-joins):

```
-- get_discovered_files_with_duration (Execution Time: 239.302 ms)
Parallel Hash Right Anti Join  (actual time=119.062..135.034 rows=51566.50 loops=2)
  ->  Parallel Bitmap Heap Scan on analysis analysis_1  (actual time=0.587..1.759 rows=5000.00 loops=2)
        ->  Bitmap Index Scan on ix_analysis_failed  (actual time=0.432..0.432 rows=10000.00 loops=1)

-- four_bucket[analyze] (Execution Time: 401.013 ms)
GroupAggregate  (actual time=380.874..399.189 rows=4.00 loops=1)
  ->  Sort  (actual time=368.277..383.035 rows=194000.00 loops=1)   -- 194K-row sort dominates
        Sort Method: external merge  Disk: 3024kB
        ...  ->  Bitmap Index Scan on ix_analysis_failed  (actual time=0.417..0.417 rows=10000.00 loops=1)
```

### Index-scan finding (honest reading of the must_have)

- **`ix_analysis_failed`** — CONFIRMED used (Bitmap Index Scan) in TWO shapes (`get_discovered_files_with_duration`
  and `four_bucket[analyze]`), on the selective `~failed(ANALYZE)` branch (10K/90K analysis rows).
- **`ix_metadata_failed`** — CONFIRMED used (Bitmap Index Scan) in `four_bucket[metadata]`, on the failed branch
  (4K metadata rows).
- **`ix_fprint_success`**, **`ix_analysis_completed`** — NOT chosen. Their predicates are the *positive*
  done-clauses covering 40–57% of rows at this corpus; a partial index over that many rows is non-selective,
  so Postgres correctly prefers a Seq/Hash scan. This is optimal planning, **not** a regression.
- **`ix_cloud_job_awaiting`** — NOT chosen. `cloud_job` is a 2K-row table (Seq Scan cost 61), and the analyze
  cutover's exclusion tests the broader `_ACTIVE_CLOUD_STATUSES` set, not the `awaiting`-only predicate the
  index covers.

So the must_have's literal expectation ("Index/Index-Only Scan, not Seq Scan on all five partial indexes across
the three pending queries + the four-bucket query") is **partially met and recorded honestly**: 3 of the 5
named partial indexes are confirmed scanned on their selective failure-marker branches; the other two plus
`ix_cloud_job_awaiting` are correctly NOT chosen because their predicates are non-selective (or the target
table is trivially small) at this corpus, where a Seq/Hash scan is the optimal plan. Critically, **no query
fell back to a pathological nested-loop** — every plan is a hash/bitmap/parallel plan.

### DENORM-01 go / no-go decision

**Decision: NO-GO — DENORM-01 (the denormalized stage-bitmap column) stays DEFERRED for now. Flagged for
operator review.**

Reasoning, licensed by the measured numbers:

1. **The number is over budget** (~1.4 s endpoint p50 vs the `< ~1s` D-07 budget), so this is NOT a clean
   "within budget, YAGNI" defer. The naive gate trigger ("over budget → pull DENORM-01 forward") IS met and
   is recorded as such.
2. **But the overage has a cheaper remedy than the denormalized column.** `get_stage_progress` runs the three
   enrich four-bucket reads **SEQUENTIALLY** (~340–440 ms each ⇒ ~1.15 s combined); that sequential chain is
   the dominant cost. The cheapest lever is to run the three `_safe_bucket_counts` reads **CONCURRENTLY**
   (`asyncio.gather` on independent sessions), projected to collapse ~1.15 s → the slowest single query
   (~0.44 s) and bring the endpoint under budget — a small, correctness-neutral change that avoids DENORM-01's
   dual-write correctness/drift burden entirely. (This is a projection, not a measurement.)
3. **`work_mem` / HashAggregate tuning does NOT help** (verified read-only): even at `work_mem=256MB` the
   planner keeps Sort + GroupAggregate for the four-bucket query — the dominant cost is the per-row correlated
   `EXISTS` SubPlan probes over the full 194K corpus (inherent to the derived CASE), not the sort spill. So the
   only remaining levers are (a) parallelize the three reads, or (b) DENORM-01.

**Recommended sequence:** parallelize the three enrich four-bucket reads (a Phase-83+ follow-up) and
re-measure; pull **DENORM-01** forward ONLY if concurrent reads + targeted query tuning still miss the `~1s`
budget at 200K. Until then the 5 s degrade-safe poll (never 500s; each read rides the `_safe_count` /
`_safe_bucket_counts` degrade discipline) absorbs the current ~1.4 s comfortably.

### Reproduce

```bash
just perf-db-up                        # dedicated PG on :5545 (isolated from the shared test-db)
just perf-seed 200000                  # migrate to HEAD (>=036) + seed the D-06 corpus
just perf-explain 20                   # EXPLAIN ANALYZE + get_stage_progress + /pipeline/stats p50/p95
just perf-db-down                      # tear the dedicated container down
```

---

## D-02 — analyze pending-set flip deploy-target gate (DEPLOYMENT-GATED — re-run at homelab rollout)

**Status: NOT satisfiable from the executor; must be re-run READ-ONLY against lux at homelab rollout.**

The analyze pending-set flip (`get_discovered_files_with_duration` cutover, Plan 82-02) is trusted in prod
ONLY once the deploy target is BOTH:

1. at **Alembic ≥036** (so the 032 partial indexes exist AND Phase 80's `036` has backfilled
   `analysis.analysis_completed_at` for the analyzed corpus, READ-03/D-13), AND
2. `COUNT(files WHERE state = 'analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL) = 0`
   (no analyzed file would be re-admitted to the analyze pending set by the derived reader).

Current known state (project memory, NOT re-probed here): **prod is at ~031** — it lacks 032 and the 036
backfill, so this gate is NOT yet green in prod. Per D-06 / Pitfall 5 the executor deliberately does NOT probe
live lux for this plan (a ~031 prod's EXPLAIN would be invalid anyway, and the read is a rollout-time check per
the 82-VALIDATION Manual-Only table). At homelab rollout, after deploying ≥036, re-run read-only against lux:

```sql
-- BEGIN TRANSACTION READ ONLY;  (read-only probe recipe: ssh datum@lux.lan, direct :5432, DB phaze)
SELECT count(*) AS analyzed_null_completed
FROM files
WHERE state = 'analyzed' AND analysis_completed_at IS NULL AND failed_at IS NULL;   -- MUST be 0
SELECT version_num FROM alembic_version;                                             -- MUST be >= 036
```

Both conditions must hold before the analyze pending-set cutover is trusted live.
