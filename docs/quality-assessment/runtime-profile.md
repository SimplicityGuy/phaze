# Test runtime assessment

Assessment bead: **Profile fast-floor, fixture, collection, full-gate and CI costs (phaze-wj5mj)**,
within **Improve test quality and bead velocity (phaze-3l0q6)**.

The highest observed aggregate cost remains coverage-selection escalation. The present measurements
also show an uneven CI partition and material recurring guard costs. Candidate optimizations must
improve accepted-bead throughput while retaining real consumers, coverage authority and isolation.
This report proposes experiments; it changes no tests, fixtures, selection policy or CI configuration.
The full local observational attempt **failed because its temporary instrumentation contaminated six
child-pytest probes**. Those six pass on an uninstrumented targeted rerun. The failed attempt supplies
cost observations, not a green gate benchmark or a new coverage baseline. The verified CI **98.88%
line coverage** remains the current baseline; no accepted-submission improvement has been demonstrated.

## Provenance and measurement boundaries

Local source base: `4f9c035c7b51b40f4ffea655a642b5ff177def63`, measured 2026-10-11 UTC.
Only three report/index paths differ from CI source commit
`d1835b022f99397ed6be69359c57938a9ceaffd9`; production source, tests and gate recipes are identical.
The local environment is macOS arm64, Python 3.14.7, uv 0.12.1, pytest 9.1.1,
pytest-asyncio 1.4.0, coverage 7.16.2, SQLAlchemy 2.1.4, asyncpg 0.32.0 and just 1.57.0.
The harness is PostgreSQL 18.6 and Redis 7.4.11; local FFmpeg is 9.0.2. Reusing the existing
Redis harness preserves concurrent seats but does not exercise the current configured Valkey image.
CI runs Python 3.14.8 on Linux with its own service images; these environments are not comparable
controlled treatments.

Task-owned observational pytest instrumentation records collection, setup/call/teardown and fixture
setup timings outside tracked source. Durations include that instrumentation and normal host
contention. Phase durations are sums inside one process, not the wall time of parallel processes.
Fixture setup observations can overlap nested fixture requests and must not be added to phase totals.
No CPU attribution, interpreter profile, RSS experiment or fixture-scope change was performed.

The warm environment already existed after Beadhive provisioning. Initial dependency installation,
empty Docker-cache startup and cold model provisioning were not measured. The first local collection
is a first observation in that environment, not a cold-machine benchmark. Repetitions retain normal
pytest caches and a reused named harness seat, with no overlapping pytest processes on that seat.

## Local floor and collection observations

One canonical default collection selects **11068 of 11312 nodes**, deselecting the **244 browser
nodes**. Pytest reports **50.52 seconds**; subprocess wall time is **73.55593887506984 seconds**.
The additional boundary includes process startup/import and shutdown; it is not all collection cost.
The collection exemption does not take the database session lock because it opens no schema.

Three direct executions of the existing 37-module always-run floor are measured separately from a
selector-driven gate. They contain **546 passed and one skipped** node per successful repetition.
The skip is `test_coverage_settings_are_central.py:295`: coverage-combine uses subcommands, not
`--cov` flags, and the separate order assertion is exercised. Direct floor timing excludes lint,
mypy, selection, admission and seat provisioning; it cannot establish a selected-code gate saving.

| Warm floor repetition | Process wall seconds | Collection seconds | Setup sum seconds | Call sum seconds | Teardown sum seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 59.44655445893295 | 0.5336743339430541 | 1.518472668598406 | 52.141738873091526 | 0.4153559176484123 |
| 2 | 53.67507870902773 | 0.5131961670704186 | 1.3824180071242154 | 47.388350406545214 | 0.3256420558318496 |
| 3 | 51.44821891596075 | 0.5001729589421302 | 1.2461992065655068 | 45.4700887928484 | 0.31598428974393755 |

All three runs pass with an exclusive named seat. Their wall median is **53.67507870902773 seconds**;
three observations do not establish a stable tail. Fixture-name timing was enabled only for the third
floor repetition; phase timing was present for all three. The third setup observations include
`baseline` 0.5633142079459503 seconds and `async_engine` 0.2846889579668641 seconds, far below call cost.
The most expensive module totals in that repetition are streaming decode **9.193103542085737 seconds**,
operator attribution **9.046715289587155 seconds**, session exclusivity **4.423268832615577 seconds**,
and session-fixture gather guard **4.204410460195504 seconds**. They protect real safety invariants;
their cost does not justify floor deletion. Streaming decoder setup/cleanup and global repository scans
are better candidates for equivalent work reuse than arbitrary sleep reductions.

The three repetitions are direct floors, **not three complete fast gates**. A current representative
selected-code gate cohort was unavailable: the feedback assessment observed zero selected-code runs,
and importing evidence into one task-local index does not repair every worktree's accepted mapping.
The coordinator bounded additional measurement to one final docs validation rather than repeat the
costly historical prose guards three times. Prior baseline and feedback docs validations are separate
trees, not matched repetitions. The remaining paired experiment belongs to **phaze-nlra5**: accepted
selected/full gates on representative code, test-only, migration, template and mixed-prose strata,
three warm repeats where feasible, with request-to-acceptance linkage and unchanged fallback authority.

## Failed full local observation and narrow diagnosis

One `just check` at immutable report checkpoint `ab9b5b85326a6ea4e083a8d6235065cb1e144d48`
used the existing two-lane supervisor, canonical manifests and greenlet dynamic contexts.
Its whole subprocess wrapper took **1627.230832332978 seconds** (27 minutes), exit **1**;
the supervisor printed **1607.27 seconds**, status **1**. This exceeded the initial 12–20 minute
estimate. Concurrent risk-document validation, static analysis and later read-only hunting were
present. Neither observational overhead nor contention was independently quantified.

| Lane | Selected | Passed | Failed | Skipped | Deselected | Pytest seconds | Observational plugin interval seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 5983 | 5970 | 6 | 7 | 5329 | 1329.44 | 1334.6593535830034 |
| B | 5085 | 5085 | 0 | 0 | 6227 | 1577.62 | 1582.961981875007 |

Each lane collected all **11312** canonical nodes; their selected union is **11068**. Both headers
named exclusive, independently derived Postgres databases on port 5433 and separate Redis indices.
The supervisor released both lane seats. Summing either lane interval would double-count overlap.
Observed lane B is longer; these failed-attempt intervals suggest reasonably similar local lane
costs, but cannot certify normal green balance or a before/after speedup. Removing the six failures
could change the A interval. The local proxy based on independent CI steps is therefore insufficient.

| Lane | Collection seconds | Setup sum seconds | Call sum seconds | Teardown sum seconds |
| --- | ---: | ---: | ---: | ---: |
| A | 58.14287154201884 | 86.68934968009125 | 1110.8047789297998 | 58.32842084381264 |
| B | 57.98633416602388 | 136.6188612425467 | 1271.355859893025 | 98.08534881856758 |

The supervisor's separate warmed canonical collection observed **10.847497707931325 seconds**.
Each lane uses its own fresh bytecode/cache/temp prefixes. These timings, the earlier 50.52-second
collection and process boundaries describe different cache and load states; they do not prove an
optimization. Calls dominate setup/teardown in both lanes.

The instrument was a task-owned pytest plugin loaded through `PYTEST_PLUGINS`. Synthetic child-pytest
tests inherit that variable but intentionally replace `PYTHONPATH`; five probes then failed with
`ModuleNotFoundError: runtime_probe`, and the sixth could not ingest the report its child failed to
produce. This was an assessment instrumentation mistake. The original failed logs and JUnit are
retained. With no plugin environment, the exact six nodes pass together: **6 passed, one warning,
10.30 seconds**, with an exclusive named seat. This diagnosis does not convert the original full
attempt to green. No second full run was made, and the supervisor correctly omitted coverage combine,
line-floor enforcement and publication after the failed lane. Those full-run measurements are absent.

The seven skips are: two NFD/NFC filesystem probes on normalization-insensitive macOS; one Linux-only
inotify probe; one coverage-combine flag-order parameter whose subcommand order is checked separately;
and three real-corpus attribution-recovery fixtures whose historical local transcripts had been
pruned. Complete skipped-node identities/reasons remain in both retained JUnit and `full-summary.json`.
The **244 browser nodes** are excluded by the canonical default selection and are not skips.

Local real-model behavior differs materially from CI. `tests/conftest.py` automatically discovers a
complete pinned operator model set when available. The real-model telemetry test **passed** here;
its call took **134.77467108296696 seconds**. It exercises the current selected model sweep on synthetic
200-second audio and verifies every expected model feature group and inference labels. It is not an
exhaustive proof for every audio/model combination. CI's no-model path cannot substitute for this
successful-inference evidence, and local model cost cannot be compared directly with CI no-model cost.
Specifically, `test_analysis_with_real_models` exercises **11 one-variant feature groups plus genre:
12 selected classifier heads**, not all 34 compatibility graphs. Its oracle requires every coarse
window analyzed, exactly the selected feature variants, inference attribute-set count and classifier
families, graph build/release/sweep metrics and the model-sweep span. It does not compare numerical
outputs against a cross-model reference. Existing **phaze-28883** therefore remains relevant for
the complete 34-graph compatibility/numerical sweep; a blanket claim of no local real-model test
would be incorrect.

The versioned public download manifest at the measured source contains **68 entries, 3264667851
bytes**. Canonical sorted compact JSON SHA-256 is
`3adf29196cffe7b2c5715c60840aef5746149869b3dea15977871143ab6e2fa4`.
Auto-discovery checks file presence and pinned sizes, not content hashes. No separate weight-content
hash verification was performed; this manifest digest is not a weight digest. Fixture version is the
immutable measured source above. All model/audio inputs here are public weights and synthetic audio;
no private archive was accessed.

## Current CI partition and critical path

Authenticated successful [CI run 38100217746](https://github.com/SimplicityGuy/phaze/actions/runs/38100217746)
is one exact-source observation. Timing comes from job and step metadata retained by the coverage
baseline bead. Coverage-step wall times are distinct from pytest-reported durations.

| Shard | Coverage step seconds | Entire job seconds |
| --- | ---: | ---: |
| shared-registry | 74 | 122 |
| shared-root | 402 | 444 |
| shared-telemetry | 97 | 146 |
| review-agents | 216 | 255 |
| analyze-rest | 203 | 252 |
| ingest-pipeline | 253 | 296 |
| integration-shared-services | 400 | 450 |

The seven coverage steps total **1645 seconds**, mean **235 seconds**. The slowest is
**1.7106382978723405 times the mean**, above the existing **1.5 times mean** balance target in
`.github/workflows/tests.yml`. The 402-second and 400-second steps both matter: improving one alone
leaves the other on the critical path. These are observed regression candidates, not an estimated
achievable speedup; the historical September partition measurement used a different suite/environment.

Dependency installation is 1–2 seconds per shard in this warmed CI run; shard upload is 1–3 seconds.
The remaining per-shard wrapper interval is 39–50 seconds and includes runner/service setup and
cleanup; it is not proven removable. Combine coverage takes **42 seconds** as a job, including
**16 seconds** combine/enforce, one second artifact download, three seconds artifact upload and four
seconds Codecov upload. Browser contract takes **368 seconds** as a job and **322 seconds** for its
test step; it remains separate from the default line-coverage population.

The first job starts 00:58:12 UTC; aggregate results complete 01:06:56 UTC, **524 seconds** later.
Final parity publication completes 01:14:33 UTC, **981 seconds** after first job start. Publication
is not a test runtime. Event-to-start queue and end-to-end acceptance linkage remain unmeasured.

The existing local grouping assigns shared-registry, ingest-pipeline, review-agents and shared-root
to lane A; integration-shared-services, shared-telemetry and analyze-rest to lane B. Summing the
independent CI coverage-step durations gives **945 versus 700 seconds**, ratio **1.35**. This is
only a partition-cost proxy. Those steps each include their own collection/setup and ran on different
runners; their sum does not measure either local lane or justify reordering modules.

## Ranked cost sources and experiment priority

The following ordering combines aggregate retained gate cost with local cost attribution; populations
are explicitly separate. It is an experiment priority, not summed fleet cost or projected savings.

| Priority | Measured cost source and boundary | Safe experiment and counterexample to preserve |
| --- | --- | --- |
| 1 | Retained escalation attempts: 199187.132 execution seconds over 178 finished attempts; admission separately totals 37244.196 seconds over 226 attempts | Repair accepted contexts/consumer mappings and measure request-to-accepted-submission on matched change strata. Preserve fallback for genuinely unmapped templates/migrations; inference is not coverage. Investigate scheduling fairness without raising the two-worker bound. |
| 2 | Failed local attempt: telemetry analysis instrumentation 384.66611037694383 seconds, including the successful real-model call above | Explore reuse of one real synthetic analysis result within compatible assertions, with fresh telemetry sinks and contamination controls. Preserve per-chunk counters, labels, peak RSS, failure isolation and actual real-model inference; mocked analysis cannot discharge these contracts. |
| 3 | Failed local attempt: historical evidence audit 139.71380341914482 seconds; Redis seat registry 119.25598458270542 seconds | Profile immutable revision/archive reads and registry container/setup boundaries separately. Cache only immutable inputs with versioned provenance, preserve fresh mutable snapshots and numeric/population/identifier checks; container/process-failure and atomic allocation consumers must remain real. |
| 4 | CI seven-step mean 235 seconds, critical steps 402 and 400 seconds; maximum/mean 1.7106382978723405 exceeds 1.5 | Trial a rebalanced exact non-overlapping seven-shard manifest using current file durations; validate all nodes/context totals. Moving only one slow shard leaves the other critical. Preserve canonical local collection and shared-state assumptions. |
| 5 | Failed local attempt: settlement guard 48.45822283311281 seconds; historical attribution recovery guard 41.668399754329585 seconds; first local floor operator attribution 9.278199961059727 seconds | Profile global scans and immutable evidence reuse while retaining real enforcement. Removing guards, stale cached live paths, missing newly added files and relying on pruned transcripts are counterexamples. |
| 6 | Default connection fixture: 18.886305711930618 seconds over 1605 setups in A and 22.51642852718942 over 1657 in B. `migrated_engine`: 17.42778779088985 seconds over 13 setups; previous-revision engine: 15.00738495879341 over 13 | Rank these below the observed heavy calls. Trial batched seeding only where commit/refresh visibility and rollback invariants remain correct. Measure factory calls separately: fixture-setup timing excludes the work performed later by returned factory functions. |

The two largest local module observations are not fixture-scope evidence. No cost above is proven
avoidable. A matched before/after must count failures, skips, bounce/review delays and accepted beads,
while overall line coverage remains above 95%; runtime alone cannot prove improved bead velocity.

## Fixture, order and context constraints

The session-scoped default schema optimization is already implemented: `tests/conftest.py` creates
schema once and rolls each test's outer transaction back. Reintroducing per-test create/drop work
would undo a completed optimization. NullPool and function-scoped async loops prevent connection
reuse across incompatible loops; broadening engine/loop scope without proof risks leaked tasks,
savepoints and cross-test contamination. A failed session close must still roll back its outer
transaction. Any optimization must preserve failure-path cleanup, not only the green path.

`tests/integration/test_migrations/conftest.py` deliberately resets public schema, upgrades to head
and disposes/resets after each `migrated_engine` use. Thirteen source consumers are a profiling
candidate, not evidence that scope can safely grow: upgrade/downgrade and failed migrations leave
different schema states. Proposed immutable schema reuse must prove exact schema restoration under
exceptions, cancellation and every revision-specific test, with independently isolated migrations DBs.

The two local lanes deliberately collect the full canonical suite before applying exact deselection
manifests. The supervisor also collects to establish the partition. Removing repeated collection
must preserve import side effects, canonical module order, exact node union and ordering of selected
nodes. An argument file that collects only lane-owned modules is a known unsafe counterexample.

Mapping runs require greenlet/CTracer and per-test contexts. Ordinary sys.monitoring and
context-bearing runs have distinct authority: disabling contexts to improve timing thins evidence
and cannot count as an optimization. Any context-overhead comparison needs the same ordered population,
runner and services; the historical +55% CPU observation is not a current runtime saving estimate.

Candidate designs flow to **Design safe fixture and shard optimizations ranked by accepted-bead
throughput (phaze-ia4ld)**. Ranking and matched experiments must include gate queues, acceptance,
review bounces and full-suite escalation, not just a faster isolated test command. Preserve the
two-worker bound, seven-shard scope and exclusive per-process Postgres/migrations/Redis seats.

## Reproduction and evidence retention

Task-owned scripts, JSON phase/fixture observations, wrapper timers, service versions and full logs
are retained under `<scratch>/assessment-artifacts/phaze-wj5mj/`; concrete paths are recorded in the
bead handoff. The scripts use `uv run --no-sync python` and fixed subprocess argument lists.
Raw logs remain untracked. Run the canonical `--collect-only` population, then each floor sequentially
on the exact three exports printed by `just test-db-for`; use the existing `just check` two-lane
supervisor for a full measurement. Read each exclusive database header, pytest summary, line floor
and process exit independently. Never sum overlapping lane times as wall time or label skipped models
as exercised. A report-only gate validates this document, not another full coverage baseline.
