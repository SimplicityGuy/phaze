# Runtime optimization through accepted-bead throughput

Assessment **Design safe fixture and shard optimizations ranked by accepted-bead throughput
(phaze-ia4ld)**, within **Improve test quality and bead velocity (phaze-3l0q6)**; inspected
2026-10-11 UTC. Recommend a small guard-efficiency pilot after lifecycle measurement and accepted
selection evidence, followed by conditional telemetry and CI experiments. No runtime change is
approved or implemented by this report. A faster test call is an input to delivery, not proof of
faster accepted beads.

## Evidence and boundaries

This proposal starts at `64232ac669dcfc4503e1909a94648bbe15f08c47`. Production source, tests,
CI and tooling remain identical to `d1835b022f99397ed6be69359c57938a9ceaffd9`; intervening changes
are assessment documents. The [runtime profile](runtime-profile.md), [feedback measurement](../bead-feedback-measurement.md),
[selection proposal](selector-proposal.md), [coverage proposal](coverage-proposal.md) and
[risk inventory](testing-risk-inventory.md) are the evidence sources, with their distinct populations.

The immutable green CI baseline is **27,482/27,793 = 98.88101320476379% lines** and diagnostic
**6,273/6,530 branches**. Preserve strictly >95% overall lines without unexplained loss from that
baseline, exclusions or denominator tricks. For unchanged code preserve the actual covered/missing
line and branch-arc sets, including destination identities; an unchanged branch ratio alone can hide
newly uncovered arcs. No new global branch percentage policy is proposed. Coordinate diagnostic
policy through the existing canonical `phaze-pms85`, and conditional coverage work through
`phaze-5f1sv`. Consumer assertions and deliberate faults remain required even at high line coverage.

Three warm direct executions of the **37-module floor** each had **547 nodes: 546 passed, one
skipped**. Process wall times were **59.44655445893295, 53.67507870902773,
51.44821891596075 seconds**; median **53.67507870902773**. Third-run call sum
**45.4700887928484** dominates setup **1.2461992065655068** and teardown
**0.31598428974393755**. These are neither selector-driven gates nor three paired before/after
samples. Retain the skip's separate coverage-combine order assertion. Streaming, database
exclusivity, gather/catalog and attribution guards stay in the floor.

The full local wrapper **1627.230832332978 seconds** was **RED**: an observational plugin inherited
by child probes produced six failures. The six clean diagnostic nodes passed separately; no second
full green baseline exists. Cost attribution includes observer overhead and concurrent load. Local
lane populations were A **5983 selected, 5970 passed, six failed, seven skipped** and B
**5085 selected/passed**; both collected **11312** and their selected union was **11068**, excluding
**244 browser nodes**. The two exclusive lanes had independent Postgres/migrations/Redis seats.
A and B pytest intervals **1329.44/1577.62 seconds** cannot establish normal green balance. Do not
subtract the six failures or add overlapping intervals to manufacture a baseline.

One independent green Linux CI run, **38100217746**, had seven coverage-step times
**74, 402, 97, 216, 203, 253, 400 seconds**; sum **1645**, mean **235**, maximum/mean
**1.7106382978723405** against the existing **1.5** balance criterion. CI job/step and local
phase timers have different boundaries and model/skip populations. The local proxy **945/700**
is not a measured local partition. Combine/enforce was **16 seconds** in a **42-second** job;
warm dependency steps were **1–2 seconds**, uploads **1–3**. These small warmed costs rank below
slow shards. Cold dependencies, empty caches and model provisioning are unmeasured.

The retained feedback corpus contains **178 escalations**, **199187.132 summed execution seconds**;
queue sum **37244.196 seconds** over **226 finished attempts**. It has no joined accepted-submission
latency and zero observed selected-code runs. A later docs check waited **1485.265036082943 seconds**
then failed **375 passed/one failed in 132.63 seconds** on category wording; a corrected exact-tree
check entered after **0.00018866709433495998 seconds** and passed **376 in 103.94 seconds**, with
**116 seconds** in its broader prose wrapper. Other assessment admissions around **0, 63 and
67 seconds** are individual observations. They neither establish queue causality nor a scheduling
speedup. Existing 64 recorded reuse decisions already show attestation reuse works.

Local profile tools: macOS arm64, Python **3.14.7**, uv **0.12.1**, pytest **9.1.1**, coverage
**7.16.2**, PostgreSQL **18.6**, harness Redis **7.4.11**. CI Python **3.14.8** is separate.
The current seat recipe requests Valkey 9 but reuses the live Redis harness; it does not certify
Valkey compatibility or justify recreating containers during concurrent work. Preserve service skew
as a coverage scope limit. No production archive data is needed for these experiments.

## Ranked implementation candidates

Rank is ordinal judgment combining recurrence, acceptance-path reach, observed cost, safety and
implementation effort. Costs are attributed observations, not removable budgets. Effort estimates
are relative design judgments, not promised delivery times. File names are the concrete candidate
edit boundary; no edits to them occur in this assessment.

| Rank / package | Observed cost and acceptance-path hypothesis | Confidence / effort | Candidate files and shared contention |
| --- | --- | --- | --- |
| 0: Measurement and usable evidence | 178 retained escalations dominate summed execution. Selection authority can reduce recurring full fallback only where real evidence exists; acceptance/bounce joins are missing | High cost observation; throughput effect unproved / medium-high | Reuse selection packages A–C in `selector-proposal.md`: existing selector, coverage importer/gate and BH lifecycle records. Shared selector/justfile boundary; serialize with coverage work |
| 1: Equivalent documentation and historical guard work | Historical audit **139.71380341914482 seconds** in failed full observation; attribution floor **9.046715289587155 seconds** in warm repetition three. Diagram/label friction incurred required gate reruns | High observed repetition; cache benefit unproved / medium | `tests/shared/test_historical_evidence_audit.py`, `scripts/audit_historical_evidence.py`, `scripts/maintenance_inventory.py`, `tests/shared/test_documentation_integrity.py`, `scripts/check_documentation_integrity.py`, attribution guard. Shared maintained-document parser; batch one owner |
| 2: Admission fairness under current capacity | Long admission tail can outweigh cheap prose checks. One 1485.265036082943-second wait is diagnostic context, not fairness proof | Medium opportunity; mechanism and causal effect unproved / medium-high | Beadhive admission/lifecycle surfaces outside this repository, coordinated HQ task; no repository test shortcut or new worker |
| 3: Real telemetry producer reuse | `tests/shared/telemetry/test_analysis_instrumentation.py` **384.66611037694383 seconds**; successful real-model call **134.77467108296696 seconds**. Several compatible successful no-model calls repeat analysis | High module cost; safe shareability conditional / medium-high | That module and `tests/shared/telemetry/conftest.py`; shared SDK globals, fixture/coverage contexts and analysis contract; coordinate with analysis hunt/backfills |
| 4: Exact seven-shard rebalance | CI critical steps **402 and 400 seconds**, maximum/mean **1.7106382978723405**. Move measured work from both slow shards, preserving all consumers | High one-run imbalance; stable benefit unknown / medium | `tests/ci_shards.json`, `tests/shared/test_partition_guard.py`, workflow wiring tests, `.github/workflows/tests.yml` only if necessary; serialize with any guard move and canonical partition owner |
| 5: Registry process calls and bounded seeding | Real registry module **119.25598458270542 seconds**; connection setup A **18.886305711930618/1605**, B **22.51642852718942/1657**. Setup alone is not the heavy call bottleneck | High costs, weak attribution to removable work / medium | `tests/shared/test_redis_seat_registry.py`, `scripts/redis-seat-registry.sh`, `tests/conftest.py`, actual consumer factories. Fixture/harness shared files; separate schema/migration review |

### Package 1: preserve guard meaning, reuse immutable work

The documentation integrity module took **1.3706225792411715 seconds** in the failed observed full
run; its current-repository test took **1.2619235829915851 seconds** across phases. Its
`assert report["mermaid_blocks"] == 25` rejects a new well-formed 26th diagram. During this
assessment the count was the sole failure after earlier error assertions passed. This is evidence
of a brittle count contract and rerun friction, not proof that a real renderer ran: the current
unit consumer calls `check_documentation` with `render_mermaid=False`. Keep structural and actual
render-consumer evidence separate. Earlier proposal diagrams were converted to dependency tables,
with external `.mmd` artifacts retained. This report introduces no maintained Mermaid block.

Propose replacing the fixed total with a meaningful collection contract: deterministic inventory
must equal the exact independently enumerated current maintained-source locations and bodies,
including zero or added/removed blocks. Add malformed/unclosed-fence, excluded historical/generated
scope and missing-source negative controls; prove a valid added block succeeds and invalid content
fails. Do not merely change 25 to 26, delete the assertion without replacement, or infer a rendered
SVG from parsing. Preserve pinned renderer version **11.12.0**, complete SVG, nonzero exit and
lingering-process-group failure/cleanup tests. The existing renderer mechanism already has per-block
fresh output directories; proposing that as new work would be a no-op.

Historical tests already cache `_original_source_identifiers`; this is not a new cache opportunity.
They still materialize isolated archives and verify mutable mutation snapshots. Profile immutable
revision/tree reads and byte parsing separately from extraction, live enumeration and audit calls.
A first pilot may memoize immutable historical byte maps by repository object identity, resolved
commit/tree, helper/parser version and configuration, then create fresh mutable snapshot copies for
each fault case. A cache cannot retain `REPO_ROOT`, live paths or mutable audit verdicts. Always
re-enumerate current tracked/untracked nonignored paths and compare current bytes: new files,
removed baseline files, numeric drift and identifier leaks must still fail. Cache maps are immutable
or copied before consumers mutate them. Bound retained bytes and clear task-owned temporary material
on failure/interrupt; interrupted writes cannot publish a half entry.

For maintained-document parse reuse, key immutable text parsing by content digest plus parser/helper
version and all mode/config inputs; source path remains part of link resolution. Recompute live path
inventory, local target existence/anchors, index ownership, generator and migration relationships.
A future render-result cache additionally needs renderer/package/browser version, helper/config,
input body bytes, resource/font dependencies and a complete validated artifact. Unknown dependency
freshness is a miss. Retain an actual real-render smoke/failure lane even on hits; fresh cleanup
oracles still execute. Never reuse a success for bad syntax, a changed renderer, missing output,
new source block, poisoned cache, cancelled child or live external-URL status. External probes are
separate, time-sensitive observations. Renderer cost was not measured in the 1.37-second module;
parse reuse starts as the simpler hypothesis, with render caching deferred until its own baseline.

Attribution guard wording also caused reruns. Prefer precise terminology and diagnostics reporting
actual line, matched phrase and grammatical claim location; preserve real uncited/hybrid claims,
wrapped prose, comments/docstrings and Q&A negative controls. A narrow parser distinction between
an identified test-category label and a historical decision claim may be piloted only against the
existing full scanner fixture corpus plus genuine uncited examples. No global meta-discussion
allowlist, heading exemption, relaxed date/bead requirement or new confirmed-bug claim is warranted.

### Package 2: capacity-neutral admission pilot

Extend existing BH request/admission records with authoritative evidence class, arrival, admission,
release, cancellation and acceptance identity; determine class from the validated diff/command,
not a caller label. Simulate/replay recorded two-slot workloads before a live policy pilot. Compare
FIFO with an aging bounded short-class policy: no extra validation slot, no self-declared cheap
code run, no full-worker starvation, no grant after cancellation, no leaked permit on process death.
Record every class's median/p95/max wait and acceptance latency, not only prose wins. Include cold
provisioning and normal guards in cost accounting. If read-only replay cannot establish comparable
populations, defer policy changes. The other observed 0/63/67-second admissions do not supply a
counterfactual for the 1485-second request.

### Package 3: compatible real analysis capture

First isolate repeated successful no-model analysis from the independent real-model consumer and
failure-path test. A shared immutable synthetic PCM fixture is safe only if input bytes remain
unchanged; it does not imply a shared live SDK provider is safe. Candidate design: one explicit
real producer captures an immutable result and exported observations for compatible read-only
assertions, under a fresh actual SDK sink. Each consuming existing node receives independent
snapshot copies; it must be labeled a captured-observation consumer, not a fresh inference run.
Keep producer failures attributable and refresh per-test context mapping rather than pretending all
consumer nodes independently execute production lines. Retain a separate fresh-sink rerun to prove
emission after provider replacement, plus unchanged missing-model/failing-run and broken-exporter
consumers. If the existing independent assertions need live provider/identity state, reject sharing
those cases and optimize only input preparation. Never serve a stale snapshot as newly emitted data.

Prove poisoning one consumer cannot affect another, stale prior-run spans do not satisfy a fresh
run, `Once`/provider globals restore after exception/cancellation, SDK shutdown completes and leaked
threads/exporters are absent. Corrupt window totals, tier labels, identity placement or RSS emission
and require their original assertion nodes to fail. Retain actual `analyze_file`, per-chunk counts,
labels, peak RSS, coarse/fine windows and strict undeclared-attribute enforcement. The successful
200-second real-model test exercises **12 selected heads**, not the complete **34-graph** sweep or
numerical reference equivalence; `phaze-28883` remains canonical. Record manifest and actual weight
verification separately; absence of models or new skips is a rejection, not a speedup.

### Packages 4 and 5: distribution and setup after call costs

For shards, recompute current same-run file weights and trial moving complete compatible modules
from both shared-root and integration/shared-services onto lighter shards. Keep **seven** shards,
serial DB buckets and exact disjoint canonical node union; no xdist. Verify collection/order/import
assumptions for proposed placement, byte-equivalent coverage combination and contexts, and every
required resource/tool per shard. Local **two-worker** manifest collection stays canonical: each lane
still collects the full population before exact deselection. Reducing its three collection passes
without proving import-order equivalence is NO-GO. A static weight simulation may propose placements
but cannot prove a green critical-path reduction. Existing balance target stays <=1.5 times the
current measured mean; recalculate after any node/cost changes instead of dividing by old 235.

Registry fixtures already reuse module-scoped throwaway containers and reset each case. Do not
propose increasing scope as completed savings or replace atomic allocation, reclaim and real client
behavior with mocks. Measure real `docker exec`/script probe boundaries, then consider batching
only read observations that do not alter interleaving barriers or freshness. Keep transient ping,
exhaustion, released-seat, missing-container, atomic allocation and live-client failure oracles.
Interrupted scripts must not free another seat; test owned-container cleanup without touching the
shared harness. A fixture itself may start Valkey while the shared harness remains Redis; keep
that version-specific population explicit.

Default schema creation is already session-scoped, and connection setup is small relative to calls.
Measure later factory flush/refresh work separately; fixture setup timing excludes returned factory
calls. Pilot batched independent row seeding only when identity/FK/default/flush visibility and
commit/rollback behavior remain exact. Keep a real subsequent query after injected error or
cancellation and verify no state escapes rollback. Migration fixtures' **17.42778779088985 seconds
for 13 migrated-engine setups** and **15.00738495879341 for 13 prior-revision setups** do not justify
broadening scope: revision tests intentionally leave incompatible schema states. Preserve
function-scoped loops/engines, NullPool, per-test migration reset and session leak guards unless a
separate measured restoration design proves every revision-specific failure/interrupt case.

## Matched pilot contract and implementation batches

Every candidate inherits this contract. Record immutable baseline/candidate SHA, exact ordered node
IDs and manifests, collected/selected/deselected/skip reasons, host/service/model/tool versions,
context mode and command. Existing-node population stays identical in before/after comparisons;
new negative controls are reported separately and then included in final gates. If a producer-sharing
change alters node semantics, report that explicitly and require the coverage-map consumer proof,
not just an equal count. Unsupported strata and environments remain visible. Ordinary sys.monitoring
and context-bearing greenlet/CTracer runs are distinct measurements; disabling contexts is NO-GO.

Use the exact exports from `just test-db-for` for every process: independent Postgres, migrations
DB and Redis; port 5433, exclusive header, one pytest per database. Preserve canonical order, 37-floor
modules, two local workers and seven CI shards. No harness teardown, bypass, skip inflation or
concurrent pytest on one seat. Child instrumentation must be observational and scoped so synthetic
child processes receive their intended environment; verify observer absence before timed samples.

Run **three paired warm before/after repetitions** sequentially per targeted existing population
where feasible, alternating baseline/candidate order and preserving caches/version state. Record
cold preparation separately; snapshot outputs and setup/call/teardown/collection plus process wall.
Use direct-target samples to reject ineffective designs cheaply. Then run three paired complete fast
gates on the representative committed change strata specified in the selection proposal, including
admission, lint, mypy, hooks and any fresh branch evidence. Do not rerun a full suite per matrix cell.
One normal required full molecule/context gate and the required CI comparisons validate adopted
changes; exact-tree replay and fresh-review authority remain separate. A failed/interrupt sample
stays red/incomplete and is not silently dropped from the latency report.

| Cohesive batch | Prerequisite and exact pilot scope | GO / NO-GO and cost boundary |
| --- | --- | --- |
| R0 lifecycle/selection prerequisites | Selection packages A–C; representative Python, test-only, template, migration, mixed and docs strata | Join stable bead/attempt/tree to claim, first submit, acceptance and every bounce. Missing events stay unknown; first successful submit is only a proxy. No throughput claim until joined acceptance cohort exists |
| R1 guard semantics + immutable audit parse | All existing nodes in documentation-integrity, historical-audit and attribution modules, plus separate new fault controls; same live inventory | GO only with equivalent original failure detection, fresh mutable snapshots, bounded cache cost and lower matched gate cost. Render mode has its own population. Batch shared helper changes and one required gate |
| R2 BH admission fairness | Same replay arrivals/classes/two-slot capacity; live matched cohorts only after policy authorization | GO only when accepted-bead median/tail improve without starving any class or losing permits/safety. More slots or missing acceptance events is NO-GO; measure queue and execution separately |
| R3 telemetry capture | All eight current instrumentation nodes in canonical order, fresh-provider control and existing broken-exporter consumers; real models retained | GO only with semantic consumer proof, no contamination/resource leak and unchanged coverage sets after legitimate context refresh. If fresh emission independence is required, share PCM only or reject |
| R4 shard balance | Exact current canonical default nodes and seven manifest paths; separate browser population unchanged | GO only after green matched CI runs show <=1.5 max/mean and lower critical wall without missing contexts, higher skip count or setup/tool regression. Full local order unchanged |
| R5 factory/registry pilot | Existing registry nodes; exact consumers of chosen seeding factory; migration nodes remain unchanged unless separate restoration proof | GO only with measured later factory/probe cost, durable state/atomicity and exception/cancellation cleanup controls. Reusing already-shared containers/schema or speculative loop widening is NO-GO |

Dependencies: R0 precedes accepted-velocity claims; R1 can pilot independently but adoption depends
on R0's outcome linkage. R3 precedes final R4 weights if approved because it changes workload cost.
R5 is lower priority and independent of R1/R3 unless shared fixtures overlap. Coordinate R3 with
analysis hunt/assertion proposals, and R5 with coverage backfills using the same factories. Keep
selector/justfile, fixture/conftest and shard/workflow owners serialized; independent test-only
consumer additions may run in separate seats. Avoid an omnibus source optimization that prevents
attribution. Each approved batch has its own bead gate and review, followed by required molecule
`just check-all`, branch evidence and CI boundaries. Required checks are never replaced by ad-hoc
microbenchmarks; exact-tree attestation avoids redundant reruns only within existing authority.

Numeric speedup promises are deferred until pilot baselines. Report paired medians and differences;
three samples supply a screen, not a stable p95. Tail evidence requires the declared workload window
with all attempts, failures, admission, bounce and review waits. Compare accepted-bead cohort
median/p95 and accepted count per declared interval across matched change strata. Retain attempts
with missing acceptance as censored/unknown, not zero-latency successes. GO requires unchanged
safety/failure detection, >95% line coverage and no unexplained line/arc loss, plus an observed
acceptance-path improvement exceeding the pilot's observed variability. If only call time falls,
report a component improvement and keep throughput verdict pending. CI evidence remains a secondary
critical-path target, with before/after runner, population and resource setup held comparable.

## Rejected shortcuts and validation

Do not delete slow floor guards, shorten real timeout contracts by arbitrary sleeps, mock away
integration/model consumers, increase workers, weaken attribution citations, bypass admission,
reuse mutable schemas/SDK globals, publish failed coverage or tune thresholds/exclusions to green.
Do not optimize already session-scoped schema or already module-scoped containers as new work.
Dependency caches and combine/upload tuning are deferred until cold evidence changes their rank.
No lower accepted-bead latency or production bug is established by this proposal.

Task-owned evidence is retained at `<scratch>/assessment-artifacts/phaze-ia4ld/`: original bead
brief, exact seat exports, targeted extraction from the retained profile, task-local Repowise
structural context and final validation/handoff. No full suite or new runtime measurement is run
for this proposal. Required all-file pre-commit hooks and exact-tree `just check-fast` validate
this report through the normal lifecycle; their result is recorded at submission. The next gate
is the final assessment decision and separately approved implementation molecule.
