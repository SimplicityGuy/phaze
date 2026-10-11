# Bead feedback latency measurement

Assessment bead: **Measure bead feedback latency, escalation causes, and repeated validation cost
(phaze-3re0j)**, within **Improve test quality and bead velocity (phaze-3l0q6)**.

The largest observed validation cost is the fallback to full coverage when the selector lacks
accepted evidence. The current lifecycle already reuses many exact-tree greens. The next proposals
should address evidence completeness and queue delays before assuming check and submit always
duplicate a full suite. None of the costs below establishes an achievable saving.

## Provenance and population

Source base: `d1835b022f99397ed6be69359c57938a9ceaffd9`. Measurement window is **2026-10-04
00:00:00 UTC inclusive to 2026-10-11 01:55:00 UTC exclusive**, filtered by run `started_at`, use
`created_at`, or log event timestamp, respectively. Evidence was read on 2026-10-11 UTC.
No new benchmark suite, source/test/CI change, or production access was used for this assessment.

Read-only inputs are the shared clone's `.git/fast-gate-escalations.log`,
`.git/fast-gate-docs-runs.log`, `.git/bh/validation/runs/*/manifest.json`,
`.git/bh/validation/uses/*.json`, and available `.bh/validation/runs/*/gate.log` artifacts.
The command and event semantics were checked against the base's `justfile` and
`scripts/select_impacted_tests.py`, plus the installed Beadhive 0.24.2 record implementation and
`bh work check --help` / `bh work review --help`. The executing assessment environment is macOS,
Python 3.14.7 through `uv`; historical runs retain their own environment in their gate logs.

| Population | Count | What it can establish |
| --- | ---: | --- |
| Retained manifests inspected, all dates | 552 | Artifact inventory, with zero JSON parse errors |
| Runs starting inside window | 232 | Distinct managed execution attempts, each with its own run ID |
| Finished runs with start/end timestamps | 226 | Recorded execution latency; six unfinished records excluded |
| Runs with readable gate logs | 223 | Selector population when a verdict is printed |
| Runs identifying a bead | 204 | 114 distinct beads; 28 records do not name a bead |
| Distinct recorded source trees | 200 | Source-tree identity, rather than branch name |
| Gate-use decisions inside window | 289 | Executions and exact-evidence reuse, separate from attempts |
| Gate-use decisions recording reuse | 64 | 46 submit, five merge, 12 validation, one molecule |
| Escalation log events inside window | 273 | Fallback events, including unmanaged/manual executions |
| Docs completion log events inside window | 42 | Successfully completed prose-floor events |

Managed execution phases are 89 check, 127 submit, four validation, and 12 molecule. Commands
are 220 `just check-fast` and 12 `just check-all`. Outcomes are 167 green, 56 red and nine without
a green/red verdict. The 226 finished population includes three non-verdict records, so latency
must not be equated with time to green.

Observed selector populations are **178 escalation, 28 docs, four fail, 22 unknown, zero selected
code runs**. Escalations represent **76.72% of these 232 retained execution records**, with unknown
records preserved in the denominator. This is a retained managed-run proportion, not a fleet-wide
escalation rate or the proportion of beads affected. A prose-floor failure is not appended to the
docs-completion log, whereas escalation is appended before the full delegate runs. Consequently,
the two plain-text logs cannot provide a fair combined denominator. The selected-code path has no
equivalent durable completion log. Retention and unrecorded manual checks remain further gaps.

The all-date escalation file had 921 physical lines: 919 valid three-field records and two malformed
traceback fragments. Its last valid event was 2026-10-10 23:23:36 UTC. The earlier planning count of
920 was a different observation, not a denominator. Recent escalation events name 81 distinct
branch strings, but 114 events say `HEAD`; these cannot be treated as one bead. The 192 records
beyond the first event per branch therefore do **not** establish 192 retries. The docs log has 79
valid all-date records, 22 recent branch strings and 16 recent `HEAD` events.

## Latency and components

Durations are seconds. Median is the ordinary sample median; p95 uses nearest rank
`sorted_values[ceil(0.95 * n) - 1]`. These heterogeneous historical distributions describe the
observed workload and contention, not a controlled before/after experiment.

| Finished population | n | Execution median | Execution p95 | Summed execution seconds |
| --- | ---: | ---: | ---: | ---: |
| All managed attempts | 226 | 891.579 | 1967.155 | 220914.448 |
| Escalated selector | 178 | 929.090 | 2302.659 | 199187.132 |
| Docs selector | 28 | 97.506 | 488.605 | 3970.203 |
| Selector fail | 4 | 13.630 | 18.209 | 48.758 |
| Unknown selector | 16 | 957.907 | 4127.629 | 17708.354 |
| Check phase | 84 | 857.889 | 2302.659 | 83553.955 |
| Submit phase | 127 | 919.764 | 1814.577 | 122371.141 |

The admission queue is a separate component: its recorded median is below 0.001 seconds, p95
**1131.344 seconds**, sum **37244.196 seconds**, maximum **3578.555 seconds** across 226 finished
records. Beadhive acquires the admission permit before creating the run manifest, so this queue
time is outside the `finished_at - started_at` execution interval. Adding each record's queue and
execution gives an all-attempt median **907.387 seconds**, p95 **3181.376 seconds**, sum
**258158.644 seconds**. This still omits work before admission, such as some provisioning and
initialization, and any review wait.

For docs attempts, queue plus execution has median **109.225 seconds**, p95 **2257.512 seconds**
and sum **11644.160 seconds**. The high tail warrants investigating admission scheduling; it does
not authorize more simultaneous full suites, bypasses, or reduced test floors.

Every one of the 28 docs logs has one pytest summary: its median is **39.010 seconds**, p95
**272.750 seconds**. The per-attempt difference between recorded execution and pytest summary has
median **56.705 seconds**, p95 **109.285 seconds**, sum **2088.563 seconds**. This residual includes
lint, mypy, interpreter startup/collection outside pytest's reported interval, seat work and
wrappers; it cannot be attributed to an individual component without further timestamps.
Twenty-five docs logs print the broader `test-fast` elapsed timer: median **79 seconds**, p95
**309 seconds**. This timer and pytest's timer have different boundaries; neither is the complete
bead feedback interval.

Escalated runs often have two concurrent lane pytest summaries. Summing their times double-counts
overlap, so no full-suite component sum or fixture-time saving is inferred from those summaries.
Provisioning, selector, lint, mypy and local pre-commit lack consistent separate start/end records
in this historical cohort. Accepted-submission latency, review/bounce wait and CI queue latency are
**unavailable as joined end-to-end measurements**. Mutable issue start/update fields alone cannot
reconstruct each attempt or acceptance event.

## Escalation causes and repeated work

| Root-cause class | Recent log events | Managed finished attempts with this reason | Execution median | Execution p95 | Summed execution seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| No coverage, reaching test or accepted paired test | 219 | 141 | 908.145 | 1966.557 | 153564.090 |
| Graph inference offered without accepted coverage | 54 | 37 | 1004.479 | 2341.115 | 45623.042 |

No stale-line-map or missing-test reason appears in this recent classified cohort. Those failure
classes remain possible and have appeared historically. Classification matches the literal reason
families, including `are inference, not coverage`; it does not reinterpret inference as coverage.

Missing-map reasons frequently include migration revisions, templates and mixed documentation.
For example, `docs/project-structure.md` appears in 55 missing-map events;
`alembic/versions/078_companion_content_features.py` in 36;
`alembic/versions/085_provider_source_observations.py` in 27; and
`src/phaze/templates/pipeline/partials/files_table_view.html` in 26. These are path-occurrence
counts, not independent attempt counts: one reason may name multiple paths.

Both classes are **safe fallback decisions** under current selector authority. A missing Python
coverage row can reflect stale evidence or absent meaningful tests; a template or migration may
need a separate real-consumer mapping that Python coverage cannot establish. A mixed prose/code
diff cannot take the prose-only path just because one path is documentation. The data does not
partition legitimate full validation from fixable evidence drift, and all 199187.132 seconds of
escalated execution cannot be called avoidable.

Grouping finished executions by exact `(tree, command)` yields **22 executions after the first**,
totalling **19325.854 seconds**, or **8.75%** of summed recorded execution time. Only **four** follow
an earlier green in the same group, totalling **2209.977 seconds**, or **1.00%** of summed execution
time; their phases are three validation and one molecule. Most repeated trees follow a red, where
an environment repair, flaky rerun or intentional diagnostic may justify re-execution. Even the
four after-green executions are not automatically waste: review defaults to a fresh check, and
authority/environment differences must be checked before reuse. The 64 reuse decisions are
**22.15% of 289 recorded gate-use decisions**, showing existing savings machinery is active.
They are not 64 measured avoided durations.

## Representative changes and current CI

The cohort includes Python, test-only, template/UI, migration, mixed docs/code and prose-only
changes, as evidenced by changed-test paths and reason paths in the retained gate logs. It supplies
no observed selected-code run to benchmark against a representative small Python or test-only
diff. A follow-on paired experiment must retain those strata, rather than selecting only easy
prose changes. Historical selected-run examples in [Gates and isolation](gates-and-isolation.md)
are dated reference measurements and cannot fill that current missing population.

The baseline worker supplied authenticated metadata and logs for successful
[CI run 38100217746](https://github.com/SimplicityGuy/phaze/actions/runs/38100217746), at the exact
source base above. This is **one run**, so no CI median or tail is claimed. The first job started
00:58:12 UTC; aggregate results completed 01:06:56 UTC: **524 seconds** to that boundary. Final
parity publication completed 01:14:33 UTC: **981 seconds** from first job start. Event-to-run queue
time and accepted-submission alignment are unavailable.

| CI component | Observed seconds | Boundary |
| --- | ---: | --- |
| Code-quality job | 56 | Job start to completion; pre-commit step 26 |
| Slowest coverage shard job, integration-shared-services | 450 | Job start to completion; coverage step 400 |
| Shared-root coverage shard job | 444 | Job start to completion; coverage step 402 |
| Browser contract job | 368 | Job start to completion; browser step 322 |
| Combine coverage job | 42 | Job start to completion; combine/enforce step 16 |

Coverage-step wall times across the seven shards are **74, 402, 97, 216, 203, 253, 400 seconds**
in shard-list order (shared-registry, shared-root, shared-telemetry, review-agents, analyze-rest,
ingest-pipeline, integration-shared-services). The two slow shards are close in this run;
rebalancing just one would leave the other on the critical path. The 400-second coverage step's
pytest summary is 387.08 seconds, and the 402-second step's summary is 388.96 seconds: runner
step and pytest timers are distinct. Deployment publication adds later work and must not be
folded into a test-suite timing claim.

## Next three interventions to evaluate

1. **Repair and maintain accepted selection evidence.** Feed the current baseline and these
   reason/path frequencies into **Design faster, justified selection (phaze-nlra5)**. Separate stale
   Python contexts from genuinely untested code and real template/migration consumer gaps.
   Compare safe selected gates against the same full gate on representative strata, retaining
   every required floor and escalation. Cost boundary: selector plus selected tests and
   downstream full-coverage fallback, excluding implementation time. Current evidence ranks this
   first by observed aggregate cost, not by an invented savings projection.
2. **Profile admission and the prose/always-run floor together.** Feed queue tails and residual
   timings into **Profile test and gate costs (phaze-wj5mj)** and **Design fixture and shard
   optimizations (phaze-ia4ld)**. Add temporary task-owned timing of seat, lint, mypy, selector,
   collection, setup/call/teardown and admission; compare concurrent workloads under the existing
   two-worker bound. Cost boundary: gate request through completion, with queue explicit.
   Investigate scheduling fairness before raising concurrency, and preserve each worktree's
   Postgres, migration database and Redis isolation.
3. **Close lifecycle measurement gaps and audit remaining deliberate reruns.** Record/join gate
   use, tree, authority, environment, claim, submission, acceptance and bounce events in the
   existing lifecycle evidence surfaces. Audit the four after-green repeats, unknown selector
   records and unrecorded selected/manual runs; retain fresh review semantics unless separately
   approved. Cost boundary: work start to accepted submission, with review and CI waits explicit.
   The current 64 reuse decisions make unconditional removal of check/submit repetition a weaker
   candidate than the first two. This is a proposal, not new gate or instrumentation policy.

## Reproduce and maintain the measurement

Use a fixed UTC window and immutable source base; preserve task-owned snapshots before retention
prunes logs. Read the JSON records and raw log files directly without mutating the ledger or
calling review with `--run`. Query issue state with `bh work issue <id> --json` when checking bead
identity; use `bh work show` and a read-only `bh work review` packet to inspect specific lifecycle
boundaries. Do not derive identity from a detached `HEAD` label.

Run analysis with **`uv run --no-sync python`** and the standard library. Resolve the shared git
directory with `git rev-parse --git-common-dir`. In a worktree its `.git` is a file, not the shared
artifact directory. A reproducible aggregation must do the following:

1. Parse `runs/*/manifest.json` once each; filter on `started_at`; retain unfinished and
   non-verdict records in population counts but exclude missing duration pairs from timing.
   Parse `uses/*.json` separately on `created_at`; reuse is a decision, not a new execution.
2. Join gate logs by `run_id`, respecting manifest `artifacts.gate_log` when it exists or the
   shared `.bh/validation/runs/<run-id>/gate.log` path. Recognize `selector: run/docs/escalate/fail`;
   use `FAST-GATE-ESCALATION` only as a fallback. Preserve unreadable/absent verdicts as unknown.
3. Calculate `datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)`;
   calculate admission queue separately, then a per-attempt sum. Use the median and nearest-rank
   p95 formula above. Never sum concurrent pytest lane times as wall time.
4. Order runs by start time before grouping `(tree, command)`. Report both executions beyond
   the first and repeats following a prior green, without calling either automatically avoidable.
   Preserve phase and outcomes. Report unknown identity and command/authority limitations.
5. Split escalation/docs plain logs on the first two tabs; quarantine malformed fragments.
   Filter their UTC timestamp field. Match missing-map reasons containing `no coverage` / `no
   accepted` / `no test reaching`; match weaker evidence containing `are inference, not coverage`;
   classify `map predates` separately as stale; keep all other reasons visible. Count path
   occurrences separately from attempt counts. Avoid joins based only on branch and approximate
   timestamps.
6. For CI, obtain authenticated exact-SHA run metadata through the approved repository workflow;
   preserve job and step boundaries independently. Label samples by runner and environment.
   Never turn one run into a median, or compare local timers to CI pytest summaries as though
   they have equal boundaries.

Task-local analysis snapshots are attached to the assessment workspace under
`assessment-artifacts/phaze-3re0j` (summary, per-run table and reproducible standard-library
analysis). They include local paths for analysis and are deliberately untracked. The tables and
recipe here are the durable scrubbed evidence. Future instrumentation should extend existing
records rather than create a competing selection or coverage pipeline. A future cohort must
capture a total event denominator, stable bead/attempt/tree identity, per-stage timestamps,
reuse/fresh-review authority, and acceptance/bounce events before claiming improved bead velocity.

## Validation

This report changes documentation only. Validation is the repository's existing prose gate and
pre-commit hooks, recorded on this bead at submission. It does not establish a new coverage
baseline or claim that the current full local suite was run.
