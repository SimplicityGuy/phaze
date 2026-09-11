# Parallel full-coverage gate spike

- **Bead:** `phaze-b9ylj.1`
- **Date:** 2026-09-11
- **Baseline tree:** `8b3b9ec8`
- **Scope:** measurement and decision only; no product, test, or build changes

## Question

Can phaze's local non-browser full-coverage gate run in parallel while preserving its exact test
selection, source execution, true branch coverage, failure semantics, and state isolation?

The acceptance threshold is strict: a design is a **GO** only if it completes three consecutive
green runs, leaks no resources, and improves median wall time by at least 40% over the serial gate.
The browser suite remains in its existing separate gate; both the serial baseline and candidates
deselected the same 189 browser tests.

## Method

All measurements used the same checkout and dependency environment. Every concurrent pytest
process received the three exact exports printed by a separate `just test-db-for <seat>` call:

- `TEST_DATABASE_URL`, for a distinct PostgreSQL application database;
- `MIGRATIONS_TEST_DATABASE_URL`, for a distinct migrations database; and
- `PHAZE_REDIS_URL`, for a distinct Redis logical database.

No run set `PHAZE_TEST_DB_ALLOW_SHARED`, and no run used `test-db-down`. Each lane also received a
unique coverage data file, JUnit file, pytest cache, temporary directory, Python bytecode cache,
and Beadhive test-report directory. Coverage was combined only after every lane succeeded.

The serial collection order was the canonical order. Candidate manifests were built as lists of
`--deselect` arguments, so every worker collected the full tree and retained a subsequence of the
canonical order. The two-worker assignment grouped the seven existing CI buckets from
`tests/ci_shards.json` as follows:

| Lane | Existing buckets | Selected nodes |
| --- | --- | ---: |
| A | `shared-registry`, `ingest-pipeline`, `review-agents`, `shared-root` | 4,865 |
| B | `integration-shared-services`, `shared-telemetry`, `analyze-rest` | 3,791 |
| **Union** | all seven buckets | **8,656** |

Lane A contains four skipped tests, so its green execution summary is 4,861 passed and 4 skipped.
The shard balance deliberately leaves the 3.26 GB real-model telemetry test in lane B and keeps
the Redis registry test in lane A.

For each candidate, the following were compared with the serial baseline:

1. collected pytest node IDs, duplicates, omissions, additions, and per-lane relative order;
2. measured `src/phaze` files and executed `(file, line)` pairs;
3. coverage.py's executed true-branch destinations, not merely raw trace arcs;
4. measured coverage contexts and the source lines attributed to any differing context;
5. pytest status, child-process status, coverage-combine status, wall time, maximum resident set,
   host memory, and seat registry state; and
6. cleanup after success, a pytest failure, a coverage-combine failure, and an interrupt.

## Evidence

### Serial baseline

| Result | Value |
| --- | ---: |
| Selected nodes | 8,656 |
| Outcome | 8,652 passed, 4 skipped, 189 browser tests deselected |
| Pytest wall | 1,249.95 s |
| External wall | **1,259.60 s** |
| Maximum resident set | 5,450,055,680 B |
| Measured `src/phaze` files | 295 |
| Executed source lines | 28,156 |
| Executed true branch arcs | 4,181 |

### Collection identity

Actual `pytest --collect-only` runs against both two-worker manifests produced 4,865 and 3,791
nodes. Their union contained exactly the baseline's 8,656 nodes, with zero duplicates, zero
missing nodes, and zero additions. Each lane's sequence exactly equalled the serial sequence
filtered to that lane.

A tempting path-list prototype also had an 8,656-node union, but it reordered modules. Three
agent-startup tests then failed because their process-global import preconditions changed. That
run is excluded from the green series. Node-set equality alone is insufficient; preserving the
baseline's relative order is a required invariant.

### Three consecutive two-worker runs

| Run | Lane A external wall | Lane A max RSS | Lane B external wall | Lane B max RSS | End-to-end wall | Result |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 697.55 s | 1,436,073,984 B | 618.22 s | 5,523,619,840 B | 703.5 s | green |
| 2 | 692.01 s | 1,577,172,992 B | 616.95 s | 5,087,150,080 B | 693 s | green |
| 3 | 707.40 s | 1,362,608,128 B | 628.72 s | 5,338,431,488 B | 708 s | green |

The candidate median is **703.5 s**, versus **1,259.60 s** serial: a **44.15% improvement**. The
end-to-end measurement includes coverage combination. Run 3 sampled host
active+wired+compressed memory for the whole overlap: it rose from 19,565,608,960 B to a peak of
26,409,222,144 B on a 34,359,738,368 B host, leaving about 7.41 GiB by that conservative proxy.
The largest sum of per-lane maximum resident sets across the three runs was 6,959,693,824 B; those
per-process maxima need not have occurred simultaneously.

All three runs produced the same coverage result:

| Coverage set | Serial | Two workers | Missing | Extra |
| --- | ---: | ---: | ---: | ---: |
| Measured source files | 295 | 295 | 0 | 0 |
| Executed source lines | 28,156 | 28,156 | 0 | 0 |
| Executed true branch arcs | 4,181 | 4,181 | 0 | 0 |

The raw coverage trace had one fewer self-loop and four additional self-loops in the candidate:
`metadata_extraction.py:82→82` was absent, while `jobs.py:90→90`, `push.py:263→263`, and
`s3_upload.py:131→131` and `:201→201` were present. Coverage.py does not classify these scheduling
self-loops as branch destinations. The executed true-branch set is exactly equal, so this is
trace scheduling noise rather than branch-coverage loss.

The serial data contained 15,697 measured contexts and the candidate contained 15,694. The three
names absent from the candidate were:

- `test_an_unset_variable_writes_nothing_anywhere` in `test_bh_test_report_drop_zone.py`;
- `test_no_module_queries_a_cluster_wide_view_without_scoping_it` in
  `test_cluster_wide_catalog_scoping.py`; and
- `test_no_test_gathers_http_requests_over_the_shared_session_fixture_without_citing_adr_0015`
  in `test_gather_over_session_fixture_guard.py`.

All three nodes were collected and passed in the candidate. A context-filtered query of the
serial coverage data found **zero `src/phaze` lines** attributed to each context. They are
source-scanning guards; incidental asynchronous work was attributed to their names only in the
serial schedule. Exact source-line and true-branch sets therefore provide the fidelity guarantee
that raw context-name equality cannot.

### One-worker, three-worker, and xdist findings

One worker is the serial baseline.

The order-preserving three-worker trial partitioned the exact 8,656-node union into 1,894, 4,791,
and 1,971 nodes. It ended in 503.24 s, but is **not a valid candidate**:

| Lane | Outcome | External wall | Max RSS |
| --- | --- | ---: | ---: |
| A | 1,894 passed | 402.42 s | 899,137,536 B |
| B | 4,788 passed, 2 skipped, **1 failed** | 503.24 s | 1,281,802,240 B |
| C | 1,969 passed, 2 skipped | 493.86 s | 5,231,017,984 B |

`test_agent_startup_invokes_ensure_models_present_after_whoami` failed because
`agent_worker` was first imported under a different earlier test's environment than it is in the
serial suite. Preserving relative order within each lane cannot preserve process-global
preconditioning supplied by a node assigned to another lane. Fixing that test isolation is beyond
this docs-only spike; until it is fixed, three workers are unsafe.

pytest-xdist is also **NO-GO for execution with the current fixtures**. Workers inherit one
`TEST_DATABASE_URL`, one `MIGRATIONS_TEST_DATABASE_URL`, and one `PHAZE_REDIS_URL`. The database
session lock correctly refuses the second worker; bypassing it would reintroduce concurrent schema
create/drop and Redis key-sweep corruption. xdist becomes feasible only if a plugin provisions and
exports a complete independent seat before each worker's session begins. It was not executed
because doing so against one seat would violate the repository's database-safety rule.

### Failure propagation and cleanup

The three-worker test failure propagated a nonzero runner status, skipped coverage combination,
and released all three seats. A separate forced combine failure also propagated status 1 and
released both seats. Successful two-worker runs released both seats after coverage combination.

An interrupt probe exposed one necessary implementation detail. Sending an interrupt to a Bash
parent and then terminating only its direct subshells left descendant `uv` and pytest processes
alive. `test-db-release` correctly refused while those descendants held PostgreSQL clients. A
corrected probe launched each lane in its own process group, forwarded termination to both groups,
waited for them, and then invoked ordinary seat release. It returned 130 within 3.57 s of the
interrupt, automatically released both seats, and left no pytest process. The production runner
must preserve that ordering and must never use forced seat release.

### Shared-surface audit

| Surface | Finding and required control |
| --- | --- |
| Application PostgreSQL | Session fixtures create and drop schema. Each lane needs its own `TEST_DATABASE_URL`. |
| Migrations PostgreSQL | Migration tests mutate their database. Each lane needs its own `MIGRATIONS_TEST_DATABASE_URL`. |
| Redis | Review fixtures sweep global key prefixes. Each lane needs its own `PHAZE_REDIS_URL`. |
| Shared harness ports | Host ports 5433 and 6380 may be shared because isolation is at the PostgreSQL database and Redis logical-DB layers; the harness containers must never be torn down by the runner. |
| Docker-spawning tests | The registry test uses unique container identity and no published host port. One-shot integration recipes use unique names and dynamic ports. Keep registry tests in one lane and propagate cleanup failures. |
| Fixed application ports | No selected test requires a shared fixed application listener. Tests that open listeners use pytest-provided or dynamically selected ports. |
| Git writes | Tests that exercise Git use temporary repositories or read the checkout. No selected node was found writing shared tracked state. Give every lane a unique temporary directory anyway. |
| Coverage and JUnit | Binary coverage data, JUnit XML, pytest caches, and Beadhive reports collide by default. Every lane needs a unique path; combine data only after all lanes succeed, then emit reports once. |
| Model memory | The real-model telemetry test owns the heavy 3.26 GB model load and dominates one lane's RSS. Keep it in a single lane and cap local concurrency at two on the measured 32 GiB class of host. |
| Collection/import state | Node partitioning changes which test first imports a module. Preserve serial relative order and retain a full candidate run as the guard; the failed three-worker trial proves this cannot be inferred from collection identity alone. |
| Success/failure/interrupt release | Register cleanup before spawning; retain child handles; terminate process groups; wait; then retry canonical seat release. Never use shared bypass or forced release. |

## Verdict

**GO for a bounded, order-preserving two-worker manual-shard runner.** It passed three consecutive
full candidate runs, preserved the exact selected-node union and exact source-line and true-branch
sets, released resources on success and failure, and improved median wall time by 44.15%.

**NO-GO for three workers and current xdist execution.** The three-worker design changes a real
test outcome through process-global import state, and xdist cannot supply the required per-worker
PostgreSQL and Redis isolation today.

The GO is conditional on implementing the process-group interrupt behavior proven necessary by
the fault probe. The prototype's direct-child signal handling is insufficient and must not ship.

## Recommendation

Replan `phaze-b9ylj` into a small implementation molecule for a two-worker local full-coverage
runner with these acceptance requirements:

1. derive lane membership from the checked-in CI bucket definition while preserving canonical
   serial collection order;
2. fail closed before execution unless the lane union exactly equals the serial node set, with no
   duplicates or additions;
3. provision each lane through `just test-db-for`, copy all three emitted exports, and use unique
   coverage, JUnit, cache, temporary, bytecode-cache, and Beadhive-report paths;
4. combine and report coverage only after both lanes pass, propagate either child failure and any
   combine/report failure, and retain the current coverage floors;
5. launch each lane in a distinct process group, forward interrupts and termination, wait for all
   descendants, and retry non-forced seat release for both lanes even if the first release fails;
6. add contract tests for collection identity, exit propagation, coverage combination, and cleanup
   on success, failure, combine failure, and interrupt; and
7. cap the design at two workers. Treat three workers or xdist as a new spike requiring repaired
   test isolation and per-worker resource provisioning.
