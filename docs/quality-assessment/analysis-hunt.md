# Analysis and worker orchestration hunt: second bounded round

Assessment bead `phaze-troov`, within `phaze-3l0q6`; inspected 2026-10-11 UTC.
This is the one analysis round explicitly released at `phaze-bexe7`. Findings are bug intake,
not authorization to implement fixes. The next operator decision is `phaze-7h65l`.

## Provenance and complete slice

Hunt label: `bughunt-2026-10-11-phaze-troov`. Production source is immutable commit
`d1835b022f99397ed6be69359c57938a9ceaffd9`; the task began at
`64232ac669dcfc4503e1909a94648bbe15f08c47`, with assessment documentation added since that
source. Each of the three independent lenses must read the same 18 files, totalling
10,141 physical lines. Supporting callers, installed SAQ and tests provide evidence without
expanding the claimed complete slice. Lead reproduction results and verifier source judgments are separately attributed below.
All adversarial verifiers use fresh-context
`gpt-6-astra`, different from the finder model.

| Complete slice | Files |
| --- | --- |
| Services | `analysis.py`, `analysis_decoder.py`, `analysis_exec.py`, `analysis_attempt.py`, `analysis_windows.py`, `analysis_sizing.py`, `analysis_timeline.py`, `cloud_staging.py` under `src/phaze/services/` |
| Child and job entry points | `src/phaze/analysis_child.py`, `src/phaze/job_runner.py` |
| Tasks | `controller.py`, `agent_worker.py`, `recovery_replay.py`, `submit_cloud_job.py`, `reap_orphaned_backend_cloud_jobs.py`, `reconcile_cloud_jobs.py`, `functions.py`, `heartbeat.py` under `src/phaze/tasks/` |

The task-owned artifact directory is `assessment-artifacts/phaze-troov` beneath the Beadhive
worktrees root. It retains `slice.json`, `plan.md`, finder and coverage JSONs, verdicts,
`round-2-confirmed.json`, append-only `journal.jsonl`, exact report bodies, bounded scratch
reproductions and raw logs. Only the lead appends the journal. These artifacts are outside
tracked source. No source, test, CI, configuration or production/archive data was modified.

## Confirmed logic findings

### Boot-only orphan recovery never revisits a temporarily protected row — P2

The orphan reaper skips an uploaded row younger than its stale bound or with a live submit
job. The controller invokes it once at startup; a successful skip does not schedule a retry.
Periodic reconciliation visits configured backends, while the drain requires an awaiting row.
When an operator removes a backend at restart, a protected orphan can become eligible later
without another scheduled owner revisiting it. Hot removal is separately vetoed for in-flight
work, so that route is excluded from this finding.

The bounded reproduction uses actual PostgreSQL, the boot retry helper, orphan reaper,
periodic reconciliation and drain eligibility query. It seeds synthetic SAQ rows rather than
running actual delivery. One cohort starts at age **60 seconds** against a **900-second** bound;
the other starts at **901 seconds** with a live submit row. After advancing eligibility and
removing synthetic broker liveness, three actual reconciliation ticks leave the rows uploaded
and excluded from the drain. A second manual orphan sweep transitions them to awaiting.
`orphan-repro-with-broker-state.log`: **2 passed, 10 warnings in 15.71 s** on the exclusive
isolated port-5433 database. The earlier **1 passed, 10 warnings in 9.73 s** run had no broker
table and is superseded by the explicit broker-state observation. No Kubernetes, S3 or real
SAQ delivery was exercised.

Existing tests cover young-row protection, live-submit protection, already-stale orphan
requeue, configured-backend exclusion, submitted-orphan preservation and startup invocation.
They do not test protection becoming false after successful startup. The missing oracle should
start young or broker-live, then make the row eligible and assert **scheduled work alone**
eventually makes it awaiting and drain-eligible. Negative controls must preserve the row while
young or live and leave submitted/running or normally owned rows unchanged.

Full-body judgment distinguishes historical `phaze-pnt12` (the stale-at-deploy recovery
cohort) and `phaze-bqpdx` (inert terminal/exhausted submission markers). Fingerprint:
`logic:src/phaze/tasks/reap_orphaned_backend_cloud_jobs.py:_requeue_orphaned_staging_rows:one-shot-deferred-recovery`.
Sanctioned untriaged intake: **`phaze-33c4p`**. Its original “Verification” text is the
fresh verifier’s source-only account; the later “Bounded synthetic observation” is the lead’s
run. That intake does not label this distinction consistently. This maintained report scopes
the two explicitly; no sanctioned existing-intake edit verb was exposed by `bh work`/`report`.

### An ACTIVE job waiting for local child capacity loses individual liveness — P2

Worker dispatch concurrency and child-pool capacity are independently accepted. A dispatched
job waits inside `_extract_and_analyze` for the local limiter before extraction or child
progress heartbeats start. The agent-level heartbeat does not touch that individual SAQ job.
A healthy long-running first file can therefore make a waiting second file appear stuck and
consume retries before its analysis begins. Permanent ledger orphaning was not established.

The bounded reproduction accepts actual settings/runtime configuration with dispatch concurrency
**2**, child capacity **1**, stall setting **1800 seconds**, and job heartbeat **3600 seconds**.
It holds the actual limiter, invokes production `_extract_and_analyze`, manually stamps the
broker row ACTIVE, and advances the installed SAQ clock by **3601 seconds**. Actual `Job.stuck`
and `PostgresQueue.sweep` identify the untouched waiter and retry its stored row to QUEUED with
`error=swept`. `waiter-repro-rerun.log`: **1 passed in 0.57 s**, exclusive isolated port-5433
PostgreSQL. Actual Worker dispatch and abort delivery were not exercised; a held permit
substitutes for a progressing child. No extraction or classifier inference ran. The initial
scratch run failed on an unsupported test-helper keyword (**1 failed in 1.45 s**) before the
observation; the corrected run is the accepted evidence.

Existing `test_process_file_respects_ctx_analysis_semaphore` only waits **0.05 seconds**;
extraction/progress heartbeat tests start after admission. Lane concurrency and live limiter
resize tests establish the reachable configuration but do not cross a waiting job's heartbeat
boundary. A meaningful regression should use actual Worker dispatch with two jobs and one child,
keep the first synthetic child progressing past the waiter's deadline, run actual sweep, and
assert the waiter retains one attempt and completes after release. Control time and cadence
instead of sleeping an hour. Disabling admission-wait liveness should make the negative control
fail; separately prove a genuinely stalled admitted child is still killed and cancellation
settles any waiter relay. This must preserve progress-only child supervision.

Body judgment distinguishes `phaze-grx3` (broker overclaim before dispatch), `phaze-mvq8z.21`
(live deadline skew) and `phaze-2cqx` (scratch preservation after cancellation). Fingerprint:
`logic:src/phaze/tasks/functions.py:_extract_and_analyze:untouched-capacity-waiter`.
Sanctioned untriaged intake: **`phaze-jpm1y`**.

## Real-consumer limits and unrun ground

Pure reductions, window/count checks and fake predictions do not establish real graph output
shape, finiteness, numerical equivalence or exhaustive model compatibility. Canonical
`phaze-28883` owns the unrun **34-graph** coarse TensorFlow sweep; the prior real local
**12-head** run is not evidence for all 34 graphs. This round acquired no large models and ran
no archive samples. A proposed graph validation environment must pin real artifacts and runtime,
use synthetic bounded audio at short/tail/exact-window boundaries, check actual outputs and
counts, and demonstrate chunk-bounded PCM and peak RSS. This is a coverage gap, not a newly
filed model defect.

No remote cloud lifecycle, production restart, real multi-hour inference or whole-process
memory profile was run. Read-only structural graph orientation used the existing source
snapshot; current source and consumers were authoritative. No shared index was refreshed.
The rest of the codebase and companion/browser/persistence hunt remain unrun in this round.

## Concrete existing regression nodes

- Orphan protection: `tests/analyze/tasks/test_reap_orphaned_backend_cloud_jobs.py::test_never_reaps_an_orphaned_row_younger_than_its_bound`, `::test_skips_an_orphaned_uploaded_row_whose_submit_job_is_live`, and `::test_requeues_a_stale_uploaded_row_on_a_retired_backend`.
- Boot wiring: `tests/analyze/recovery_cloud/orphan_replay/test_controller_startup.py::test_startup_calls_reap_orphaned_backend_cloud_jobs_once`.
- Capacity waiting: `tests/shared/tasks/test_functions.py::test_process_file_respects_ctx_analysis_semaphore`; same module extraction/progress heartbeat nodes cover admitted work.
- Independent settings and resizing: `tests/shared/tasks/test_live_worker.py::test_lane_concurrency_is_the_lane_knob_clamped_by_worker_max_jobs` and `tests/agents/tasks/test_agent_worker_resizable_pool.py::test_a_live_pool_size_reload_resizes_both_the_limiter_and_the_telemetry_pool`.

These existing nodes were inspected, not rerun as individual tests. Proposed oracles and negative
controls above address the confirmed failure combinations; the scratch observations do not
replace those future regressions. Fresh-context verifiers performed source/consumer refutation;
the reproduction measurements were executed by the lead.

## Results, dedup and comparative metrics

| Lens | Complete files | Candidates | Confirmed | Mechanical / judged duplicates | Filed novel | Severity | Errored / aborted agents |
| --- | ---: | ---: | ---: | --- | ---: | --- | --- |
| Logic / numerical correctness | 18 | 2 | 2 | 0 / 0 | 2 | 2 P2 | 0 / 0 |
| Resource / error handling | 18 | 0 | 0 | 0 / 0 | 0 | — | 0 / 0 |
| Concurrency / state transitions | 18 | 0 | 0 | 0 / 0 | 0 | — | 0 / 0 |
| Round 2 total | 18 per lens | 2 | 2 | 0 / 0 | 2 | 2 P2 | 0 / 0 |

All three independent passes completed the full slice; no lens is unfinished. Zero-candidate
passes have no confirm-rate denominator. Excluded historical or cross-lens leads were not
promoted into candidates and are not counted as post-verification duplicates.

| Round | Candidates → confirmed → filed | Confirm rate | Novelty rate | Filed severity | P0/P1 share |
| --- | --- | --- | --- | --- | --- |
| [Mutation / replay round 1](mutation-hunt.md) | 3 → 3 → 3 | 100% | 100% | 2 P2, 1 P3 | 0% |
| Analysis / orchestration round 2 | 2 → 2 → 2 | 100% | 100% | 2 P2 | 0% |
| Expedition through round 2 | 5 → 5 → 5 | 100% | 100% | 4 P2, 1 P3 | 0% |

Actual spend and cost per filed bug are **unavailable**. Reproduction runtimes are not model
spend estimates. The two novel edge-case findings show useful yield; two zero-additional-finding
lenses and absent real models do not establish convergence or safety outside the exercised paths.

Dedup loaded **15 open bug bodies**, **3,916 all-status issues**, and **3 historical footer
fingerprints**. Metadata-only fingerprint lookup returned none; footer extraction recovered
the real history. Full relevant issue bodies informed judgment, including the canonical model
coverage issue. Sanctioned `bh report` filed both verified defects as untriaged P2 intake;
requested hunt labels remain in the bodies rather than applied tracker labels. Post-filing
`bh work intake` returned an empty near-duplicate set. Exact body readbacks matched both drafts.
The refreshed open-bug snapshot is **18**, with exactly **2** contributed by this hunt and one
unrelated concurrent intake; that net increase is not all hunt yield. Durable readback/export
artifacts are retained for later dedup; no implementation dispatch occurred.

## Excluded leads, failures and unconfirmed seams

The resource pass excluded the known multipart compensation root (`phaze-bbwx`, `phaze-cws5`)
and sibling subprocess-pump settlement root (`phaze-702y`); current teardown kills/reaps the
child, and no distinct reachable consequence was established. It excluded the two already
confirmed logic findings as cross-lens repeats. A quiet EasyLoader fallback or native inference
call crossing the progress watchdog was not demonstrated with a permitted real input. Injected
delay would prove injected behavior only.

The concurrency pass refuted regeneration overwriting normal cloud-owned work using the actual
caller predicate, and found current redrive commit/advisory-lock ownership already guards the
historical `phaze-j2tm` mechanism. Historical `phaze-oj7x` owns the repaired upload retry mechanism.
Fresh child processes refute a shared native classifier-cache race in the current consumers.

**Unconfirmed source seam:** an external Kubernetes submit side effect can precede its database
stamp. No supported real-consumer observation established a live submit delayed past the
pending-confirmation bound; historical `phaze-32wz` was read as its adjacent owner. This is not
a filed bug or confirmation. If scoped separately, a controlled adapter/provider fault experiment
should establish whether an accepted external job retains retry ownership across delayed stamping
and reconciliation. A fabricated direct helper target or arbitrary injected delay alone would
not establish production reachability.

Error accounting separates completed agents from failed physical attempts. This round has
**zero errored/aborted finder or verifier agents**, **one failed verifier helper** (incorrect
RuntimeConfig reconstruction field, superseded by the lead's actual configuration observation),
and **one failed scratch pytest attempt** (unsupported helper keyword, corrected before accepted
observation). Resource tooling recovered a uv-cache permission failure and reread truncated output.
Concurrency tooling recovered **two failed shell invocations involving three guessed missing
paths**, with all source truncation gaps reread. Lead tooling recovered sandbox claim/cache/rebase
restrictions, used the permitted escalated lifecycle/Git paths and task-owned uv cache, and corrected
initial partial tracker export before the pass. Missing optional instruction-file lookups did not
replace the injected AGENTS guidance. These events are journaled or retained in coverage artifacts;
none is counted as a zero-finding agent result. Round 1 separately recorded **three lead
interruptions** and **one failed verifier attempt** that was replaced by a completed verification.
Those prior failures remain part of the comparison, not this round's error denominator.

## Operator boundary

Stop at **`phaze-7h65l` — Review analysis hunt yield and approve or stop the companion hunt round**.
No companion round or remediation starts automatically. Recommend that any next hunt switch to
explicitly bounded unrun companion/browser/persistence surfaces rather than repeat this analysis
slice; the operator should weigh the two novel P2 findings against unavailable spend and remote/model
limits. Stopping to specify the five confirmed regressions is also supported by the evidence.
A new round requires its own explicit scope/budget decision. Exhaustive natural windows,
chunk-bounded PCM and peak RSS, and progress-only analysis watchdog behavior remain required.
