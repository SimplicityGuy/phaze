# Current coverage evidence for test quality and bead velocity

Assessment bead: `phaze-it2fy`, within `phaze-3l0q6`. Measured source:
`d1835b022f99397ed6be69359c57938a9ceaffd9`. Evidence inspected 2026-10-11 UTC.
This report describes an authentic existing green CI measurement, not a new local full-suite run.
It changes no source, tests, workflow, coverage baseline, or gate floor.

## Provenance and scope

[Successful CI run 38100217746](https://github.com/SimplicityGuy/phaze/actions/runs/38100217746)
measured this exact immutable assigned base. GitHub metadata identifies branch `2026.10.5`,
with all seven coverage jobs and the combine job successful. The combined artifact is
`coverage-combined`, artifact ID `11687447243`, downloaded size 27,843,100 bytes, unexpired at inspection.
The base differs from the earlier planning snapshot; results below apply only to the named base,
not to the September evidence or any future integration HEAD.

CI used CPython 3.14.8 on Linux x64, uv 0.13.0, pytest 9.1.1, coverage.py 7.16.2,
PostgreSQL 18.6 (`postgres:18-alpine`) and Valkey 9.1.2 (`valkey/valkey:9-alpine`).
The local artifact reader used CPython 3.14.7, uv 0.12.1, pytest 9.1.1 and coverage.py 7.16.2.
Local harness containers remain Redis 7 and Postgres 18; they were not used for the CI measurement.
Do not conflate their cache-server versions.

Each CI matrix job owns separate GitHub service containers. The existing command is
`just test-bucket <name> "<paths>"`; its tracked recipe sets `PHAZE_COVERAGE_GREENLET=greenlet`
and uses pytest with `--cov --cov-context=test --cov-fail-under=0 --cov-report=`,
JUnit output, and `-q`. The seven partitions are `tests/ci_shards.json` at the measured commit.
`just coverage-combine` combines the binaries and enforces existing coverage floors.
No alternate collection, sharding, coverage pipeline or relaxed floor was introduced.

CI's `-q` hides the pytest database header and skip reasons. Therefore an exclusive-lock header
cannot be attested from this retained transcript. The per-job container provisioning and DSNs
are retained as isolation evidence. CI uses localhost:5432 inside its disposable job, under the
existing CI guard exception; this is not a recommendation to use developer port 5432 locally.
No shared local container was stopped or replaced.

## Results

| Coverage metric | Covered / measured | Exact percentage |
|---|---:|---:|
| Overall lines | 27,482 / 27,793 | 98.88101320476379% |
| Diagnostic branches | 6,273 / 6,530 | 96.06431852986218% |
| Combined coverage.py metric | 33,755 / 34,323 | 98.34513300119454% |

There are 311 missing lines, 257 missing branches, 243 partial branches and 917 excluded lines.
CI explicitly reported the 95% overall line floor and 90% per-module line floor passing.
No new global branch floor is imposed. Existing configured combined and branch-regression guards remain intact.

| Non-browser shard | Passed | Skipped | pytest duration |
|---|---:|---:|---:|
| shared-registry | 57 | 0 | 61.00 s |
| shared-root | 1,368 | 6 | 388.96 s |
| shared-telemetry | 227 | 1 | 82.65 s |
| review-agents | 2,194 | 2 | 201.27 s |
| analyze-rest | 1,279 | 0 | 189.77 s |
| ingest-pipeline | 2,351 | 1 | 240.60 s |
| integration-shared-services | 3,573 | 5 | 387.08 s |

Total: **11,049 passed, 15 skipped, zero reported failures or errors**.
These durations include context tracing and are CI shard pytest times, not local feedback time,
job wall time, or a measured context-tracing overhead comparison. Their sum is 1,551.33 s;
388.96 s is the largest shard pytest time. Job and step timestamps are in the retained run metadata.

A separate real-browser job reported **242 passed, 2 skipped in 311.22 s**. It is not part of
this coverage binary or numerator. Local default `--collect-only` at the same source commit
reported **11,068 of 11,312 collected, 244 browser tests deselected, 38.99 s**.
That local population is four larger than the 11,064 CI default outcomes. Collection depends on
platform/tool availability; the exact cause of those four differences is not established here.
Do not silently equate populations or claim that collection-only executed tests.
CI summaries report no deselection count; the 244 figure is the local collection result.

## Binary, XML and consumer verification

The original combined `.coverage` and XML were preserved. JSON was generated from that binary,
against the immutable worktree source, using existing coverage.py. Both reports agree exactly on
line and branch denominators and covered counts. All **384 tracked `src/phaze/**/*.py` files**
appear in the binary and generated report: zero absent production Python files. Coverage omits
`tests/*`, measures `source=["phaze"]`, and has `relative_files=true` and `branch=true`.
The 917 excluded lines are distinct from omitted test files and missing executable lines.

The binary contains **19,781 contexts including the empty import/global context**,
**11,051 distinct test node IDs across phase contexts**, and **8,575 distinct run-phase IDs**.
Tests that execute no production lines need not have run-phase rows. Phase contexts are not
independent test cases and should not be used as a suite-size denominator.

Repowise was initialized in this bead's worktree with fast structural mode, no editor setup,
no hook installation, no agent/CLAUDE generation, no prose/provider and no seed. Only its ignored
local index was populated. Strict binary and XML ingestion produced:

- 72,133 context-to-source file pairs; 19,780 phase-context records, 322 mapped source files.
- 384 of 384 XML paths matched; zero unmatched or ambiguous paths; `mapping_partial=false`.
- Index and both ingests at the exact source SHA above.
- Existing `scripts/repowise_coverage_gate.py` passed with run-start freshness checks.

The difference between 384 file-total entries and 322 context-mapped files is meaningful:
global/import execution and files without named test execution can have totals but no useful
per-test selection. It is not missing coverage measurement.
Repowise's aggregated branch percentage is 95.93%; coverage.py's authentic branch totals are
96.06431852986218%. Use coverage.py counts for branch acceptance, not that differently aggregated
downstream value. Repowise's line counts match exactly.

The real `repowise impacted-tests` consumer read the new map. The dependency-only HEAD change
correctly requests a full run. An actual summary change produced 628 coverage-backed phase records,
but the existing selector classifier correctly escalated its three unmeasured templates.
An execution-source change produced 141 coverage-backed records and 1,748 inferred records; the
existing classifier correctly escalated 1,747 inferred candidates across two files. Details are in
`impacted-execution.json` and `selector-verification.txt` in the artifact directory.
No inferred import/call/filename matches were promoted into coverage evidence.

**Shared selector restoration remains deferred.** `scripts/select_impacted_tests.py` deliberately
resolves the durable checkout from `git --git-common-dir`, regardless of a worktree's local index.
Thus this task-local map proves ingestion and consumer behavior but does not speed other beads'
current gates. The CI refresh driver temporarily changes the durable checkout and rejects
non-main CI branches; invoking it during concurrent work would violate this assessment's isolation.
A coordinated idle/shared-index refresh with reviewed exact-SHA provenance is needed separately.
No fake scope stamp or baseline regeneration was used to bypass that boundary.

## Where deeper coverage work starts

Compared with the preserved September 22 JSON, **85 current production files are newly measured**.
Their previous absence meant unknown coverage, not zero. They include runtime configuration,
companion capture/import, deployments, junk review, selected cue/Discogs sources and source workspaces.
The complete inventory is `added-source-files.json` in the artifact directory.

| Current low-line module | Exact line percentage | Missing lines |
|---|---:|---:|
| routers/duplicates.py | 91.40625% | 11 |
| services/source_workspace.py | 91.42857142857143% | 6 |
| routers/agent_cue.py | 91.66666666666667% | 2 |
| runtime_config_notify.py | 91.80327868852459% | 5 |
| runtime_config_catalog.py | 92.0% | 2 |
| routers/cue.py | 92.81437125748504% | 12 |
| services/selected_cue.py | 92.98245614035088% | 4 |
| services/selected_discogs.py | 93.02325581395348% | 3 |
| services/pipeline/analyze.py | 94.0% | 6 |
| services/companion_capture.py | 94.11764705882354% | 4 |

These are testing candidates, not a claim that low coverage alone determines risk.
The downstream risk inventory should combine this data with changed-frequency, destructive seams,
assertion strength and execution concurrency. High overall coverage does not prove real coarse
TensorFlow model coverage: CI logs say no classifier models were configured by default.
Binary totals cannot establish browser JavaScript behavior, meaningful condition combinations,
production image compatibility, mutation detection, or the reasons for the 15 skipped tests.
Those remain explicit assessment work, including existing `phaze-28883` coarse-model coverage.

## Retained artifacts and reproducibility

Task-owned artifacts are outside tracked source at
`<scratch>/assessment-artifacts/phaze-it2fy/` (the concrete task-owned path is recorded in the bead handoff).
They include `run.json`, `ci.log`, `ci-summary.txt`, `ci-combined/.coverage`,
`ci-combined/coverage.xml`, generated `coverage.json`, local `collection.log`,
`added-source-files.json`, strict-ingest logs, `coverage-status.json`, `index-status.json`,
`ingest-gate.txt`, impacts JSON and `seat.exports`.
Raw CI logs remain untracked; no live archive data was accessed.

Original artifact SHA-256 values:

- `.coverage`: `c74f260952d03ee4ecb3933d352ca1ad1eb4df18d40e348aa9c181cee6bd2455`
- `coverage.xml`: `c451227fd11a56916da25135bbe2c51e9f32ad966c704e94c309402d0cf8e45a`
- Generated JSON: `53a1f5659324c7f80590de327939ab62b7f5152fafdd3177598904412aee10ac`

Artifacts can be downloaded again from the named CI run while retention lasts. The local evidence
must remain available until downstream assessment workers finish; the ignored index is task-local,
so raw artifacts and this committed report are the durable review inputs.
