# Faster selection through trustworthy evidence

Assessment **phaze-nlra5**, within **phaze-3l0q6**. Source assessed:
`d1835b022f99397ed6be69359c57938a9ceaffd9`; the leaf started from the assembled
documentation tip `f683964dcb0bdaa3b9ae8d532364f72c471987a4`. This is a design proposal,
not approval to change selector authority, floors, workflow, or branch policy.

Prioritize making existing selection evidence usable and maintaining it without disrupting
other worktrees. Follow with narrowly proven non-Python consumer mappings. Preserve every
existing refusal and always-run guard. Success means lower claim-to-accepted-submission time
under comparable workloads, with no lost regression detection; a higher selected-run count alone
is insufficient.

## Evidence and opportunity

The [feedback measurement](../bead-feedback-measurement.md) inspected 232 retained managed
attempts, 226 finished, identifying 114 beads. There were **178 escalations**, 28 prose gates,
four selector failures, 22 unknown records and **zero observed selected-code runs**. The
178 escalations consumed 199187.132 execution seconds. Among finished managed attempts,
141 missing-evidence escalations consumed 153564.090 seconds; 37 inference-only escalations
consumed 45623.042 seconds. These are aggregate process times, not elapsed cohort time or
avoidable cost. The population is retained attempts, not all beads or all validations.

Recent escalation-log events divide into 219 missing coverage/reaching/paired evidence and
54 inference events. There were no classified stale-map or moved-test events in that recent
cohort, so freshness is a correctness dependency and plausible preventive intervention, not
a measured explanation for all 178 escalations. Frequent reason paths include
`docs/project-structure.md` (55 occurrences), revision 078 (36), revision 085 (27), and
`src/phaze/templates/pipeline/partials/files_table_view.html` (26). One reason can name several
paths; occurrences are not independent attempts.

The [coverage baseline](coverage-baseline.md) proves 384 production files, 19781 contexts,
72133 context/file pairs and **98.88101320476379% overall lines**, from successful CI run
38100217746 at the exact source above. The baseline's strict task-local selector proof
establishes that accepted selection can work with complete paired evidence. It does not show
that the shared main index has been repaired: the shipped selector chooses the parent of
`git --git-common-dir`, not the task-local index. Resolve that ownership mismatch explicitly.

Twenty-two same-tree/command executions after the first consumed 19325.854 seconds, but only
four followed a green, consuming 2209.977 seconds. Most repeats followed red; the 64 reused
gate-use decisions out of 289 already demonstrate working reuse. Preserve fresh review authority
and do not claim the 22 repeats as savings. Accepted-submission timestamps are not joined yet;
that measurement gap must close before reporting improved bead velocity.

Runtime evidence is provisional input from the runtime assessment's retained artifacts. Three
warm direct floor runs had 547 collected, **546 passed and one skipped**, with wrapper times
59.44655445893295, 53.67507870902773 and 51.44821891596075 seconds. They are floor-only
measurements, not selected-code gates or full validation. The separate observed full local
attempt failed because the temporary observer leaked into subprocess probes; it is not a green
speed baseline and does not authorize publishing a coverage map. The exact-tree prose report
gate also waited more than 15 minutes behind two live full validations during this assessment;
this is one operational observation, not a scheduling distribution.

## Current decisions and proposed remedies

`scripts/select_impacted_tests.py` first refuses a dirty tree. It accepts the prose path only
when every raw diff entry is a regular non-executable Markdown blob in an allowed location.
An empty diff still reaches the normal path. For code it requests coverage status and impacted
tests from the shared index, accepts coverage and collectable `changed-test` facts, rejects
graph/import/name guesses, checks changed-source movement between map SHA and merge base,
and escalates the whole diff if any selected test file is missing. It strips only known coverage
phase suffixes. No filename-shaped test pairing should be promoted to execution evidence.

| Change/evidence gap | Current response | Candidate remedy and proof required | Remaining escalation |
| --- | --- | --- | --- |
| Python source with missing or stale contexts | Empty/unknown map or unsupported inference escalates | Ingest complete green contexts paired to the indexed SHA; execute the real selector against representative committed diffs and verify selected tests exercise changed behavior with deliberate faults | New untested code, shifted coordinates, absent contexts or incomplete ingestion |
| Test-only change | Accept collectable `changed-test`; reject fixtures, YAML named `tests.yml`, unrecognized tiers | Preserve that fact-based path; collect every named node against the leaf before claiming a measured run, with explicit deleted/renamed-node diagnostics | Noncollectable entries, removed node, ambiguous report or unknown artifact |
| Mixed prose and measured code | Whole-diff prose shortcut unavailable; documentation may be unknown | A narrowly defined mixed mode could separately include the complete prose floor and independently authorize every code path; raw modes and both rename ends must qualify | Any executable Markdown, nonregular mode, unsupported path or unmeasured code |
| Files-table Jinja partial | Unknown artifact escalates; Python route coverage does not prove template behavior | Explicit mapping for the actual rendered partial and its includes, justified by route rendering plus behavior-specific faults; `tests/integration/test_files_table_card.py` is a starting consumer, not authority for arbitrary table behavior | New/include/layout changes lacking demonstrated consumers; JavaScript/native-submit changes without browser coverage |
| Revision 085 | Migration text outside package coverage; filename/import inference insufficient | Existing `tests/integration/test_migrations/test_085_provider_source_observations.py` performs upgrade 084→085, seeded preservation, restart/backfill, immutable persistence and lossy-downgrade refusal; prove faults in these actual revision operations make mapped tests fail | New revision, uncovered upgrade state, changed shared Alembic environment or changed neighboring revisions |
| Other configuration, shell, build, workflow or runtime prompt artifact | Unknown or weak inference escalates | Add a mapping only after its actual process/consumer contract has been demonstrated, with representative faults | Default full gate; no broad suffix/directory exemption |

The mixed-mode idea changes current authority and needs its own reviewed implementation task.
It must not merely subtract Markdown paths from Repowise's report: run the complete prose floor,
validate every remaining artifact, and then apply all whole-report refusals. Preserve the existing
prose-only verdict and transcript distinction. Templates and migration mappings are conditional
proposals, not established coverage or preaccepted fast paths.

For files-table mapping, mutate the real partial's pager target/card placement, its selection
field name and an include dependency. A card-only test can detect frame placement but cannot
certify selection authority or native form dispatch. Those latter changes require existing real
browser consumers such as `tests/browser/test_htmx_native_submit_guard.py`, plus a demonstrated
files-table-specific flow where necessary. Missing browser tooling or a skip cannot count as a
successful mapped guard. Include edges must be explicit and checked against artifact bytes;
dynamic includes or unresolved consumers escalate.

For migration 085, inject loss of one seeded legacy field, removal of immutable-update protection,
and a broken downgrade refusal in the actual revision. Each must fail the real migration consumer
on an isolated migrations database, not a test of a mock migration runner. Also prove that a fault
in shared `alembic/env.py` cannot take the revision-only shortcut. Accepting a mapping for 085
must not authorize 078, 086, 087, 088 or a new revision automatically.

## Evidence refresh, publication and recovery

Extend `scripts/repowise_coverage_from_ci.py` and `scripts/repowise_coverage_gate.py`, not a
second ingest pipeline. The current importer validates one completed successful main push,
workflow identity, artifact/run/SHA pairing and expiry; ingests both `.coverage` contexts and
`coverage.xml` totals; verifies stored state and pairing; and restores the original checkout.
It currently temporarily switches the durable indexed checkout. Restoration after a failure
does not make that switch safe for concurrent readers of main.

Proposed ownership: one dispatcher-controlled refresh operation per repository builds a generation
in a dedicated refresh seat at the eligible CI SHA using the existing importer/gate functions.
Repowise keys repository records by absolute path: a disposable worktree creates a different
record. Therefore extend the existing selector with an explicit validated generation location and
repository identity contract before using this route. Do not pretend the current hardcoded shared
path already supports it, and do not silently rebind another repository's record. If installed
Repowise cannot support an immutable generation, retain safe full escalation and schedule a
coordinated shared-index refresh only when all durable-checkout readers are quiescent.

Each generation records repository object identity, source SHA, indexed tree, CI workflow/run,
artifact ID and digest, binary/XML digests, tool/environment versions, complete default test
population and skip inventory, line totals, context and mapped-file counts, and the actual
consumer-proof version. A generation is immutable after read-back validation. Publication changes
one pointer atomically under a repository writer lock; readers pin the generation for the entire
selection. Source/index/map must remain paired. The default Python coverage population excludes
the separately measured browser population; never describe it as all consumer coverage.

Trigger a refresh after a new eligible green main run when source or test bytes changed, after
test moves/deletions, after verified map incompleteness, or on a named stale/missing-evidence
escalation. Coalesce simultaneous requests for the same SHA, allow only one writer, and avoid
refreshing just because a leaf HEAD differs from map SHA. Target-aware source movement remains
the criterion. A newer map must not be used for an older base without verifying compatible source
coordinates; divergent/unsupported ancestry escalates. Do not use age alone as freshness proof.

Interrupted downloads, checksum mismatch, incomplete contexts, expired/failed/wrong-run artifacts,
count mismatch, wrong repository identity and writer failure leave the prior generation intact.
An older valid generation can be used only when the existing target-aware checks establish
applicability; otherwise full validation is the recovery. Never overwrite a good generation with
the failed instrumented local run or a partial shard. A local fallback uses the existing
`just repowise-coverage` with greenlet per-test contexts, canonical population and isolated seat,
after a normal capacity reservation. Its cost includes a full coverage run and ingestion.

Recurring cost is download + hashing + index build + both ingests + read-back + publication.
No complete current refresh duration has been measured here. The baseline's combined artifact
is **27843100 bytes**; treat that as one payload observation, not stable transfer time. CI reuse
does not add a local full suite, but retains CI cost. Measure warm/cold refresh, bytes, retained
disk and recovery time before choosing generation retention or refresh cadence. Retain at least
the active generation and last verified rollback generation until a measured retention policy is
approved. Do not mutate main, repoint a live reader, or bypass validation admission.

## Always-run floor and cost boundaries

Keep all 37 modules in `tests/fast_floor.txt`. Their recorded reasons are critical meta guards
and the application's boot path, including streaming teardown whose absence caused long-file
memory failure. A score-only optimizer previously dropped validation wiring, coverage, database
and streaming safeguards. Breadth remains the selector's responsibility; adding general
coverage modules to this floor would rebuild a static suite underneath it.

The table gives **summed setup/call/teardown report seconds per module in warm run three**,
rounded to six decimal places. It is not wall time, a removal saving, or a fixture-only budget.
All measurements include the temporary observer. Collection was 0.5001729589421302 seconds;
call total 45.4700887928484 seconds dominates setup 1.2461992065655068 and teardown
0.31598428974393755. Individual reports provide a ranking for retained-guard optimization.

| Preserved floor module | Observed phase seconds |
| --- | ---: |
| `tests/analyze/services/pipeline/test_analysis_streaming_decode.py` | 9.193104 |
| `tests/shared/core/test_health.py` | 0.307849 |
| `tests/shared/core/test_main_lifespan.py` | 0.479690 |
| `tests/shared/core/test_route_reachability.py` | 2.961149 |
| `tests/shared/test_adr_numbering.py` | 0.001430 |
| `tests/shared/test_bh_test_report_drop_zone.py` | 1.052363 |
| `tests/shared/test_branch_coverage_check.py` | 0.383079 |
| `tests/shared/test_change_gate.py` | 0.726198 |
| `tests/shared/test_ci_workflow_wiring.py` | 0.530668 |
| `tests/shared/test_cluster_wide_catalog_scoping.py` | 3.353953 |
| `tests/shared/test_conftest_dsn_coercion.py` | 0.001989 |
| `tests/shared/test_conftest_hermeticity.py` | 0.350666 |
| `tests/shared/test_containment.py` | 0.160872 |
| `tests/shared/test_coverage_floor.py` | 0.014474 |
| `tests/shared/test_coverage_gate.py` | 0.002696 |
| `tests/shared/test_coverage_settings_are_central.py` | 0.540340 |
| `tests/shared/test_db_guard.py` | 0.012339 |
| `tests/shared/test_fast_gate.py` | 1.894395 |
| `tests/shared/test_gather_over_session_fixture_guard.py` | 4.204410 |
| `tests/shared/test_hadolint_version_pin.py` | 0.016141 |
| `tests/shared/test_health_baseline.py` | 0.014460 |
| `tests/shared/test_justfile_default.py` | 0.023837 |
| `tests/shared/test_no_exclude_newer_cooldown.py` | 0.002720 |
| `tests/shared/test_no_filestate_guard.py` | 0.805846 |
| `tests/shared/test_no_hardcoded_broker_dsn_fallback.py` | 0.108860 |
| `tests/shared/test_operator_attribution_citations.py` | 9.046715 |
| `tests/shared/test_partition_guard.py` | 0.031168 |
| `tests/shared/test_redis_worktree_isolation.py` | 0.183075 |
| `tests/shared/test_repowise_coverage_from_ci.py` | 0.539734 |
| `tests/shared/test_repowise_coverage_gate.py` | 0.034551 |
| `tests/shared/test_seat_name_normalization.py` | 0.740890 |
| `tests/shared/test_settle_discipline.py` | 3.136756 |
| `tests/shared/test_shared_harness_teardown_guard.py` | 0.004104 |
| `tests/shared/test_stamp_coverage_scope.py` | 0.391760 |
| `tests/shared/test_test_db_session_exclusivity.py` | 4.423269 |
| `tests/shared/test_validate_seat_derivation.py` | 0.666470 |
| `tests/shared/test_validation_gate_recipes.py` | 0.690252 |

Streaming and citation checks, session exclusivity, gather/catalog scoping, route reachability
and settle discipline warrant profiling inside their existing modules. Cache only immutable parse
inputs within an appropriate lifetime; preserve real subprocess/session adversarial controls.
No floor pruning, blanket fixture-scope widening, replacing DB behavior with mocks, or fixed sleeps
in place of barriers. Detailed optimization belongs to **phaze-ia4ld**. Queue-aware evidence-class
scheduling is a separate hypothesis for that proposal: preserve two full-worker capacity, isolation
and fairness; never add a third full worker or allow a code change to self-label as prose.

## Concrete follow-on boundaries and rollout

These are proposed work packages for the final implementation plan, not newly filed or dispatched
beads. The first three establish measurement and safe evidence ownership; consumer adoption waits
for their verification.

| Package | Scope and deliverable | Acceptance / negative control | Cost to measure |
| --- | --- | --- | --- |
| A: Joined selector/lifecycle evidence | Extend existing gate records with verdict/reason, stable bead/attempt/tree, evidence generation, stage timings, accepted-submission/bounce join | Retain unknown/manual/reuse/fresh populations; detached HEAD resolves by bead identity; missing events stay missing; record schema failures visibly | Recording overhead and retained bytes; claim→accepted submission, queue and gate separately |
| B: Coordinated generation refresh | Existing importer/gate functions, explicit selector evidence location, writer/pointer ownership, durable provenance | Real Repowise round trip; wrong repo/SHA, failed/expired CI, missing halves/contexts/counts, interrupted writer and concurrent reader/writer refuse or retain prior valid generation; main checkout unchanged | Download/build/ingest/read-back wall, warm/cold, failed-recovery wall and disk |
| C: Selected/full shadow equivalence | Representative Python, test-only, mixed prose/code, template, migration, unknown/config changes | All changed artifacts have named authority; injected regressions fail selected consumers; unsupported strata escalate; compare full outcome with canonical order/populations and isolated seats | Selector + floor + impacted tests + any branch-evidence rerun, admission, lint/typecheck/hooks; full comparator separately |
| D: Mixed prose/code authority | Existing selector's strict raw classifier and complete prose floor plus accepted code evidence | Executable Markdown, runtime prompt, symlink/type/rename mode, unknown code and missing node cannot disappear; actual docs corruption fails prose guard | Added prose floor versus observed full fallback for matched diffs |
| E: Bounded template/migration mappings | Explicit byte/version/include consumer inventory, begin with files-table partial and revision 085 | Real rendered/seeded-upgrade faults above; consumers missing/moved/skipped or dependencies unresolved escalate; no inferred template coverage | Consumer/browser/migration gate wall, setup, mapping review and invalidation frequency |
| F: Branch diagnostics through canonical phaze-pms85 | Print covered/total/uncovered baseline/current and inherited versus newly missing arcs | Keep current branch rule unless that canonical task records an explicit decision; counts cannot hide a genuinely new uncovered branch | Diagnostic/recovery time, not claimed selector saving |

| Dependency | Why it precedes adoption |
| --- | --- |
| A and B → C | Joined outcomes and paired generations support trustworthy shadow comparisons |
| C → D and E | Proven shadow equivalence precedes each new mixed/consumer authority |
| A, D, E and F → matched rollout decision | Lifecycle evidence, independently accepted authority classes and branch diagnostics inform adoption |

Start in shadow mode: the existing verdict remains authoritative while proposed evidence and
consumer selections are recorded. For each supported stratum use at least three matched green
diffs and corresponding deliberately faulty variants, preserving changed-test facts, floors and
canonical order. Run selected and full comparators sequentially or on separate isolated seats,
under normal admission. This is a designed sample, not a statistical guarantee of all future
false negatives. Publish selected node IDs, exact source/evidence tree, collection/outcome/skip
inventories and mutation-control results. A full run finding a regression missed by proposed
selection blocks that authority expansion; no successful easy strata can average it away.

Then adopt verified Python evidence first; enable each mixed/consumer class independently after
fresh review. Reclassify unsupported diffs to full validation immediately on broken provenance,
missing mapping, collection mismatch or a shadow disagreement. Retain last verified generation
and the original selector route for recovery. Measure selector decisions as well as accepted
submission median/p95 on matched scope/environment cohorts; disclose unknown joins and any shift
in skip, branch or test populations. Keep >95% overall lines and explain any fall from the actual
98.88101320476379% baseline using complete green full evidence. Selected coverage is never a
new overall coverage baseline.

Canonical **phaze-pms85** describes the 44/46→42/44 branch-ratio case with two missing branches
unchanged. Diagnostics can reduce investigation friction; changing to count/both-signals is a
separate recorded policy decision with counterexamples for newly added uncovered branches.
The user's line-only acceptance target does not silently remove existing per-bead branch gates.

## Validation and limits

Only this maintained report and the documentation index change. Evidence comes from the merged
assessment reports, shipped selector/importer and real consumer tests, canonical phaze-pms85,
and retained runtime floor JSON at `<scratch>/assessment-artifacts/phaze-wj5mj/`.
Per-module times were reproduced by summing reports grouped by node-ID file, including all phases;
quantities above remain exact apart from the explicitly rounded module table. No shared index
refresh, selector change, consumer experiment, implementation filing, or additional full suite
was performed by this bead. Repository pre-commit and the existing prose gate are required before
handoff; the eventual submission records exact-tree validation separately from this design.
