# Dashboard and polling latency

Measured on 2026-10-15 using a migrated, isolated synthetic PostgreSQL corpus and
dedicated Redis database. These results describe local database work, not production
network latency or Kubernetes API behavior.

## Changes and boundaries

- Construct owned SAQ pools with their configured bounds before opening them.
  Previously SAQ opened its default pool of four connections before resizing to
  the requested zero-to-two agent queue budget. Resizing left the initial excess
  connections until the pool's idle shrink interval. Queue ownership and normal
  connect/disconnect behavior remain unchanged.
- Reuse the healthy Summary request's stage-progress snapshot when seeding its
  client store. A failed source read retains the independent seed retry. Each new
  request reads fresh data; there is no cache across requests.
- Guard the reporting orphan predicate with a SQL CASE on its required scheduling
  intent. The existing orphan predicate remains the true branch. This prevents
  expensive retry, completion and broker predicates from running on files without
  intent. Eligibility, recovery, display classification and scoped filters retain
  their existing predicates and failure behavior.

Live broker counts, the concurrency limit of four, source-failure isolation,
SAVEPOINT fallbacks and the five-second browser polling configuration are retained.
Cached queue handles still accumulate for distinct agent identities; this change
bounds each pool rather than introducing queue eviction.

## Reproducible comparison

The baseline used product source `f4309acdd1bc887999bb29d7acd684b997716a24`;
the candidate used `7f8309f8a8d8e903c4587df09d7893f1ed8ca796`.
Both ran through the same copied profiling harness (SHA-256
`8aa9473fcb9bb2c75f7ee66fe485812c8f42889b51fef92fd4c929a643d27c66`),
Python 3.14.7 interpreter, dependencies, working directory and effective settings.
The launcher asserted each source HEAD and imported module paths, and recorded
product file hashes. No source checkout, application workers or lifecycle tasks ran.

The schema was migrated through revision 092. The retained corpus contained:

- 150,000 files; 105,000 metadata rows, including 3,000 failures;
- 67,500 analysis rows: 60,000 completed and 7,500 failed;
- 180,000 analysis windows, 60,000 set profiles and 30,000 proposals;
- 3,000 scheduling intents, 1,500 stage skips and 10,500 cloud jobs;
- 1,500 legacy tracklists and 7,500 provider objects, observations and explicit
  selections each;
- six agents, including a revoked fileserver and a never-seen compute agent;
- 30 serialized broker jobs: 20 queued and 10 active.

The registry configured one local backend and two compute backends. It exercised
database-backed states without contacting an external compute API. Synthetic job
payloads are count fixtures, not certified runnable worker inputs. Redis started
and remained empty. Both versions used SQLAlchemy pool size five with five overflow
connections and the same bounded read semaphore.

Each endpoint received two warmup waves. Single-tab results contain five measured
samples per endpoint. Three-tab results contain three simultaneous requests per
wave and three measured waves: nine samples per endpoint. The harness waited five
seconds after each completed wave; this is bounded simultaneous-tab pressure,
not a replay of a fixed-rate browser schedule. Reported wall times include full
endpoint handling and the same read-only instrumentation in both versions.
Percentiles are omitted because these sample counts are small.

## Paired endpoint results

Times are milliseconds, shown as median / maximum.

| Load | Endpoint | Baseline | Candidate | Median reduction |
| --- | --- | ---: | ---: | ---: |
| Single tab, n=5 | `GET /` | 3897.833 / 3963.868 | 1716.645 / 1746.491 | 55.96% |
| Single tab, n=5 | `GET /pipeline/stats` | 2068.068 / 2114.297 | 1545.204 / 1571.507 | 25.28% |
| Three tabs, n=9 | `GET /` | 5755.344 / 6885.299 | 2500.122 / 3367.669 | 56.56% |
| Three tabs, n=9 | `GET /pipeline/stats` | 3061.662 / 3891.222 | 2512.940 / 2920.637 | 17.92% |

All 56 measured responses returned 200 without instrumentation errors. The
single-tab Summary median met the provisional 2500 ms goal, and polling improved.
The concurrent Summary median was 2500.122 ms; these measurements do not establish
a universal latency bound.

The selected database's observed client peak fell from 98 to 36 in both load
shapes, including the observer and ownership guard. Agent queue pools were
configured for zero-to-two connections: baseline pools actually reached four;
candidate pools reached one. The controller, configured for two-to-eight, reached
five versus three. The candidate stayed below the fixture's conservative
60-connection configured budget. These are observed counts for one API process,
not a deployment-wide capacity promise.

Summary SQLAlchemy operations per request fell from 229 to 190: SELECTs from 83
to 70, and SAVEPOINT/RELEASE pairs from 73 to 60. Stage-progress reads fell from
two to one. Polling retained 174 SQLAlchemy operations, 42 live broker count
SELECTs and 42 psycopg connection-check executions. Both retained two Redis
commands per request. Polling improves through reduced predicate work rather
than omission of live reads.

Remaining work is substantial. In the single-tab candidate, the two status
aggregates had cursor-duration medians of 563.998 and 553.351 ms; the guarded
orphan counts took 463.746 and 409.822 ms. Stage-progress spans had medians of
1202.844 ms on Summary and 1188.821 ms on polling. Spans overlap and must not be
added to infer wall time. This change does not eliminate those shared queries.

## Correctness and experiment evidence

Before and after every series, exact broker key, queue, status and serialized
payload rows matched. Agent buckets, file/intent/completed-analysis counts,
zero analyses completed today and zero Redis keys also matched throughout.
All ten active jobs were already beyond the 1800-second claimed-overdue threshold
before the new baseline and remained overdue throughout the pair. Seen agents
were already beyond the 300-second stale threshold. No fixture timestamps were
refreshed. Earlier exploratory endpoint results preceded the overdue transition
and are excluded from these gains.

A rollback-only CASE experiment compared both stages across all-files, populated
agent and empty-agent scopes. All six comparisons had zero EXCEPT ALL membership
differences and exact bucket equality. Forty-four edge cases covered absent and
populated intent, retry/completion ordering, terminal states, skip/missing/nonmedia
files, broker and cloud states, mismatched functions and malformed payloads/keys.
All added rows were rolled back; the original 150,000 files, 3,000 intents and 30
broker rows remained. A preliminary aliased-ledger join preserved parity but
showed no useful work reduction and was rejected.

In three-sample EXPLAIN comparisons, metadata orphan-query median execution time
fell from 803.578 to 410.008 ms and analysis from 1205.950 to 485.072 ms. Shared
buffer hits fell from 218,383 to 8,474 and from 443,366 to 7,106 respectively.
JIT time increased rather than explaining the gain: the benefit came from avoided
predicate work.

Bounded correctness validation passed 77 tests covering queue construction,
request freshness, healthy/degraded snapshot reuse, scoped buckets, retry,
completion, terminal and missing-broker behavior. A strengthened real pool cleanup
and cluster-catalog scoping selection passed eight tests. Profiler safety and
attribution passed nine tests, including real competing advisory-lock refusal,
wrong-engine detection, URL override rejection and independent request attribution.
Those tests used a separate disposable validation database and Redis allocation;
the retained benchmark corpus was never given to pytest. All repository and
commit hooks passed. Full-suite validation remains a separate integration gate.
