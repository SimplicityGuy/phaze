# Files workspace latency

The Files list keeps its status badges derived from authoritative rows. It does not persist a
status cache or count the whole archive to paginate. The query now first materializes the exact
page of IDs, then derives display facts for those IDs within the **same SQL statement and snapshot**.

The membership query retains the existing filters, sort direction, unique ID tiebreaker, offset
and one extra sentinel row. Stage filters and stage sorting still require stage predicates over
the matching corpus. The outer query repeats the selected order because materializing a CTE does
not guarantee its scan order. Agent/root facts and the existing bulk profile loader remain bounded
by the page plus sentinel. Transactions, SAVEPOINT degradation and operator controls are unchanged.

## Baseline and target

The baseline product revision was `f4309acdd1bc887999bb29d7acd684b997716a24`. Each of the
32 combinations below had **five measured samples after one excluded warmup**, for 160 samples.
All requests returned HTTP 200 without degraded reads; direct IDs, buckets and sentinel values
stayed stable, and every rendered slice contained exactly the corresponding direct-query IDs.
These are local synthetic measurements, not production population percentiles.

Times are **median / maximum milliseconds**. Page size was the actual application default, 50.
“Rail” means the Files workspace fragment; “Table” means its narrow table fragment.

| Case | Direct list | Full document | Rail | Table |
| --- | ---: | ---: | ---: | ---: |
| Default | 448.390 / 450.123 | 2214.998 / 2310.509 | 536.894 / 540.679 | 504.812 / 516.347 |
| Page 2 | 1837.456 / 1900.346 | 3603.974 / 3635.900 | 1976.597 / 2070.740 | 1913.211 / 1994.545 |
| Page 50 | 1922.454 / 2009.187 | 3622.749 / 3715.875 | 1975.263 / 2006.460 | 1947.879 / 1964.845 |
| Type descending | 468.869 / 482.094 | 2205.245 / 2225.317 | 499.142 / 516.219 | 504.186 / 529.767 |
| Metadata failed | 2397.200 / 2439.467 | 4047.339 / 4079.580 | 2422.211 / 2437.327 | 2351.616 / 2388.274 |
| Any stage failed | 3853.192 / 3968.929 | 5643.868 / 5841.146 | 3970.320 / 4170.077 | 3911.358 / 3967.268 |
| Analyze ascending | 2048.736 / 2093.756 | 3815.450 / 4017.102 | 2100.205 / 2136.437 | 2095.800 / 2167.801 |
| Media scope | 480.645 / 547.183 | 2313.123 / 2332.978 | 516.462 / 551.913 | 508.879 / 512.005 |

The Files-local target was a warm median at or below **250 ms** for ordinary navigation,
including subsequent pages. The direct reads meet it; applying the same budget to the rendered
fragments still misses it: ordinary rail/table medians are **258.138–286.342 ms**. The full page
also pays for shared shell data. No subsequent sidebar additions or independent dashboard
optimizations are included in either side of this comparison.

### Paired candidate result

The measured candidate was `0e29f01f378e59a7a798286fc83f6edb34353d9c`, using the **identical
exploration harness**, unchanged database, configuration, page size, one warmup and five samples
per case. Every candidate sample matches the baseline IDs, derived buckets and sentinel; all
rendered row IDs also match exactly. All 32 median latencies improve; query counts are unchanged.

| Case | Direct list | Full document | Rail | Table |
| --- | ---: | ---: | ---: | ---: |
| Default | 229.036 / 232.431 | 1976.092 / 1998.993 | 286.342 / 298.423 | 265.432 / 269.291 |
| Page 2 | 222.850 / 234.866 | 1950.243 / 1975.494 | 258.138 / 265.033 | 267.143 / 271.880 |
| Page 50 | 236.527 / 243.830 | 1946.298 / 2011.956 | 263.549 / 270.702 | 276.023 / 284.066 |
| Type descending | 221.737 / 228.806 | 1945.000 / 1971.098 | 267.791 / 280.055 | 262.495 / 273.389 |
| Metadata failed | 1640.339 / 1664.840 | 3358.235 / 3368.176 | 1692.484 / 1704.284 | 1718.807 / 1759.263 |
| Any stage failed | 3170.642 / 3248.038 | 4911.572 / 4939.873 | 3250.636 / 3390.746 | 3295.020 / 3326.630 |
| Analyze ascending | 1405.403 / 1422.198 | 3145.272 / 3181.665 | 1529.345 / 1596.842 | 1481.236 / 1496.567 |
| Media scope | 225.272 / 302.179 | 1963.906 / 1978.271 | 278.221 / 280.136 | 276.315 / 281.307 |

Ordinary direct medians fall to 221.737–236.527 ms: default improves **48.92%**, page 2
**87.87%**, page 50 **87.70%**, type descending **52.71%**, and media scope **53.13%**.
The media-scope maximum was 302.179 ms, so the median target is not a worst-case guarantee.
Stage-dependent reads remain materially slower: metadata failed 1640.339 ms, any stage failed
3170.642 ms and analyze sorting 1405.403 ms, despite median improvements of 31.57%, 17.71%
and 31.40%, respectively. The required corpus-wide work remains.

Full-document default improves 10.79% to 1976.092 ms; page 2 improves 45.89% to 1950.243 ms.
Its shared seed remains essentially unchanged: default 1692.066 ms versus baseline 1691.038 ms.
This change therefore does **not** deliver subsecond full-page navigation.

All eight candidate plans materialize exactly 51 IDs once. Page-2 plan execution falls from
1926.684 to 224.080 ms, and its JIT work falls from 1564.545 to 167.797 ms. JIT remains enabled.
The default plan still spends 169.761 ms compiling (baseline 165.177 ms) and compiles 601 functions
instead of 571; the improvement is not removal of all compilation. The largest measured pool
acquisition was 0.937833 ms versus 0.118084 ms in the baseline, still below 1 ms. There was no
request-latency or EXPLAIN execution-time regression in the measured cases.

### Attribution

The page-2 EXPLAIN ANALYZE took 1926.684 ms, including **1564.545 ms of JIT work** for 571 functions.
Page 50 took 2006.427 ms, including 1612.017 ms of JIT work. Even though projection was below a
LIMIT, the original plan prepared full-table hashed subplans for metadata, analysis, proposals,
tracklists and other badge/history inputs. An offset also changed the JIT optimization decision.

For the default full document, median spans were 473.797 ms for Files, 1443.664 ms for shared stage
progress, 1691.038 ms for the entire shell seed, and 47.912 ms for template rendering. The direct
list emitted two SELECTs plus SAVEPOINT/release; the full document emitted 31 SELECTs plus 60
SAVEPOINT/release statements. Concurrent SQL durations overlap and cannot be summed as wall time.
SQLAlchemy pool acquisition never exceeded 0.118084 ms in the measured matrix.

The scoped server observer mostly saw active backends with no reported wait event; smaller samples
showed I/O and parallel-query coordination. It sampled warmups and EXPLAINs as well as requests,
so these counts are not per-request latency percentages or proof about production contention.
Files navigation made no SAQ count calls. A separate real-handle probe verified 26 count queries
over 13 queues: three non-revoked agents, each with the three current lanes and legacy base queue,
plus the controller. A revoked agent was excluded. No workers ran and the broker was idle.

## Comparable corpus and configuration

The dedicated synthetic database was migrated through Alembic 092 with **134 indexes** and ANALYZEd
after insertion. It contains:

| Relation or state | Exact count |
| --- | ---: |
| Files | 200000 |
| MP3 / text files | 194000 / 6000 |
| Missing files | 2000 |
| Metadata done / failed | 136000 / 4000 |
| Analysis done / failed | 80000 / 10000 |
| Coarse analysis windows | 320000 |
| Proposals: pending / approved / rejected / executed / failed | 8000 each |
| Scheduling ledger / analyze skips | 2000 / 2000 |
| Set profiles | 80000 |
| Cloud jobs | 2000 |
| Legacy tracklists | 20000 |
| Provider objects / observations / explicit selections | 10000 each |
| Non-revoked fileservers / compute agents / revoked fileservers | 2 / 1 / 1 |

The generator uses indices 1 through 200000, deterministic UUIDs from the MD5 of a table-specific
prefix plus the index, and synthetic paths grouped into four roots. Dispositions use `index % 100`:
metadata below 70 (68–69 failed), analysis below 40 done and 40–44 failed, proposals below 20
cycling five statuses, ledger at 80, skip at 81, missing at 96, non-media at 97–99, legacy tracklists
below 10, and selected provider observations at 10–14. Each completed analysis has four coarse
windows and one profile. Cloud jobs at 50 cycle seven statuses: awaiting, uploading, uploaded,
submitted and running have 286 rows each; succeeded and failed have 285 each.

Configuration used one synthetic local backend (cap 2, rank 99), SQLAlchemy pool size 5 plus 5
overflow, and a 10-second pool timeout. PostgreSQL used JIT on with `jit_above_cost=100000`, 4 MiB
work memory, 128 MiB shared buffers, 4 GiB effective cache size, random page cost 4 and 300 maximum
connections. The retained shared test cache container ran Redis 7; this does not validate Valkey 9.

The cache was warmed by preparation and the explicit warmups; there is no cold-disk claim. Four
windows per completed file is sparse compared with long concert sets. There was no concurrent
worker workload, upload traffic or heavy validation. Preserve the same fixture and configuration
for the candidate comparison, and use a separate validation seat for pytest: session teardown can
drop the application schema.

## Repeat the reads

Allocate a dedicated local seat with `just provision-test-db-seat <name>` and copy its printed exports.
Migrate and prepare a synthetic corpus with the distributions above. Keep that database separate
from all pytest and migration-test sessions. Set an explicit synthetic backend registry when
reproducing the exact local-only configuration.

```bash
uv run python -m scripts.perf_files \
  --dsn "$TEST_DATABASE_URL" \
  --redis-url "$PHAZE_REDIS_URL" \
  --iterations 5 \
  --output files-profile.json
```

This repeat probe installs real controller, agent-router and cache handles without starting the
application lifespan or any workers. It rejects production/shared targets, takes the same exclusive
database lock as pytest, and invalidates reads that escape its selected SQLAlchemy engine, even
when a service catches the error. An observer also rejects the Files service's explicit degradation signal, including Python
statement-building failures that never reach SQLAlchemy; genuine empty and past-end reads remain
valid measurements. A persistent failure flag also rejects a later caught HTTP 200 fallback.
The exploration harness additionally recorded exact plans,
per-reader spans, pool acquisition, sampled server waits and cross-shape row equivalence. Those
extra instruments mean new repeat-probe timings are a fresh series, rather than additional samples
of the recorded baseline. Use the same probe version for both sides of any new comparison.

The original exploration harness was identified by SHA-256
`da782fe60220af21f98a6b90bc079d5113d7154522d441cc147ea6b22897e7b7`; its accepted complete
profile hash was `3351add175e2ee3b985346987c8c0887decf3dfddd6908b6a0e58f094b839b2a`.
The accepted candidate profile hash was
`0eb151c66aa60851df8c50f88cb1ab17c9e349c9b352a357bcfaafc3d6aac3d0`.
The independently checked paired-comparison artifact hash was
`ff8563af33908be78a8925f07e81f6c16b0b243c20f2ac0a7dcd97a4295dfee8`.
An earlier attempt used 25 rows for direct reads and the route default of 50; its row-equivalence
guard rejected the attempt. No timing claim uses that mismatched run.

Correctness is checked independently across every allowed sort key in both directions, ties,
page boundaries, past-end pages, filtered/orphan/media views, derived statuses, roots and glyphs.
The query-shape guard requires one ID-only materialized membership CTE and one badge SELECT plus
the existing bulk profile SELECT, whose bound includes only the page and its sentinel.
