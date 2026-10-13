# Optional pilot disposition manifest

Task **phaze-x45ht (PD)** records the complete optional-branch disposition for implementation epic
**phaze-75qpj**, before final assembled verification **phaze-47gba (G0)** and adoption review
**phaze-8d5bs (A0)**. The approved design is the
[implementation plan](../quality-assessment/implementation-plan.md) (assessment PR #716, source
`75e586ef9d1d412dfcf8ecb34818ecdda2576e70`). This manifest was written on main
`0f7a81afc92807bbcf2138ca04c85b8a30d4de05`, which contains every commit cited below.

Every optional pilot named by the plan has exactly one row here: **ADOPT** (its bytes are on main and
enter G0), **EXCLUDE** (no new authority; existing production and test authority retained) or
**DEFERRED** (not authorized in this round). No branch is omitted. Omission would let a branch bypass
the union gate without a recorded decision.

**This manifest waives nothing.** G0 still runs the actual assembled canonical full union over every
adopted byte, every canonical repair and every original real-failure oracle. An exclusion here does
not shrink that population. A0 decides from G0's observed outcome; this manifest does not anticipate
that decision.

## Observation cohort and outcome authority

**Operator decision, 2026-10-11.** Question as put: *"Before the velocity pilots run, which
observation cohort should they use? Repairs and coverage backfills can proceed without this
choice."* Answer as given (selected option label, verbatim): *"Next 20 completed beads, grouped
by change type"*. The other offered option was "Seven days, grouped by Python/tests, docs, and
template/migration changes". The reply arrived at 19:03:57 UTC. The cohort cutoff is
**2026-10-11T19:04:16Z**. Durable record: the recovered-decision comment on bead `phaze-x45ht` and
its follow-up comment, which records the offered options. Before those comments were written, no
bead comment or ADR section held it. The source exchange is a session transcript whose question
call lists the offered options, so the answer is confirmed to be an exact option label. The
decision covers cohort membership only.

The cohort is complete in membership: **20** beads, every one an accepted-review join. Its outcome
authority is not complete. The [M0 readback](../bead-feedback-readback.md) (`phaze-k04p7`) finds
**0** authoritative original work-start events and therefore **0** joined velocity observations.
The current claim event is not the original claim. Missing starts are censored, not reconstructed
from validation timestamps.

**No accepted-bead velocity claim, no paired gain claim and no stable-p95 claim is supported by this
cohort, for any pilot in this manifest.** That includes R4, whose adoption rests on matched CI
runtime and never on velocity.

## Disposition table

Commits are the landed bytes on main: the pilot's own commit(s), then its `--no-ff` merge.

| Pilot | Bead | Disposition | Landed on main | Evidence |
| --- | --- | --- | --- | --- |
| SC: selected/full shadow equivalence | `phaze-tmf6u` | **EXCLUDE** | `abf429b9`, merge `8c5fdcfe` | [selector-equivalence disposition](selector-equivalence-disposition.md) |
| SD: mixed prose/code authority | `phaze-al4ct` | **EXCLUDE** | `9f40eae7`, merge `48bd645d` | [mixed-validation disposition](mixed-validation-disposition.md) |
| SE: files-table / revision-085 mappings (nonbrowser) | `phaze-mbtzg` | **EXCLUDE** | `9541f7d8`, merge `7317eadd` | [consumer-mapping disposition](consumer-mapping-disposition.md) |
| C5: path fallback error and root termination controls | `phaze-b6csj` | **ADOPT** (test controls only) | `631f52d4`, merge `c582a8fb` | [accepted increment](accepted-increment.md); section [C5](#c5-adopted-controls-not-an-optimization) |
| R1: immutable parse reuse in guards | `phaze-2jvjd` | **EXCLUDE** | `28275b12`, merge `d0ffb20a` | [immutable-parse disposition](immutable-parse-disposition.md) |
| R3: shared telemetry capture (producer outputs and PCM) | `phaze-axfxb` | **EXCLUDE** | `945918dc`, `c9eb55e3`, `7e4b322e`, merge `fd310ee5` | [telemetry-capture disposition](telemetry-capture-disposition.md) |
| R4: exact seven-shard rebalance | `phaze-l5uve` | **ADOPT** | `2e410625`, merge `413e1e1b` (PR #730) | Section [R4](#r4-adopted-on-one-matched-ci-run) |
| SB: coverage-generation ownership | `phaze-a393q` | **EXCLUDE** generation publication; safe fallback retained | `0e2a14d4`, `e0d26be6`, merge `6daee337` | [coverage-generation disposition](coverage-generation-disposition.md) |

The linked reports carry each pilot's reasoning, and this manifest does not restate it. Each
exclusion follows the plan's optional-completion path: a reviewed exclusion is valid without running
a pilot that was denied, unsupported or infeasible. It keeps current authority and supports no
performance or coverage claim. Common to all six exclusions: M0 outcome authority is incomplete
(the previous section). The SC/SD/SE selection pilots also lack the immutable generation that SB
found unavailable.

- **SB installed capability.** Installed Repowise **0.55.0** completed an isolated synthetic
  binary/XML round trip with an explicitly configured database URL. It has no verified immutable
  generation, reader pinning, atomic local publication or interruption rollback. `coverage status
  --generation` and `--immutable` exit 2. The **safe fallback is the disposition**:
  `scripts/select_impacted_tests.py` keeps its strict refusals and full escalation. No shared
  refresh or main-checkout switch beneath readers is scheduled. Interrupted-writer and
  concurrent-reader controls remain unrun, and so do eligible-CI costs.
- **SE browser portion.** SE's exclusion covers the nonbrowser mapping. The browser portion is
  **SE_B** (Deferred, below), not part of this row.
- **R3 and R4 ordering.** R4 used the plan's "after R3 rejection" path: its weights were taken with
  the telemetry workload unchanged.

### C5: adopted controls, not an optimization

C5 is a **GO small pilot** in the plan. Its adopted bytes are **two tests** in commit `631f52d4`.
One drives a real synthetic parent iterator that raises `OSError` midstream and requires the
original path back. The other is a finite collaborator sentinel of at most 8 root probes, which
catches missing root termination. Resolver byte preference and the genuine NFC and real-disk
consumers stay unchanged. These tests enter G0's union as ordinary nodes. **No runtime, coverage
percentage or velocity gain is claimed.** C5 has no separate disposition report. Its scope and
provenance are recorded in the [accepted increment](accepted-increment.md).

## R4: adopted on one matched CI run

**Operator decision, 2026-10-12.** Question as put: *"R4 (phaze-l5uve) has a verified, correct
shard-rebalance candidate (predicted coverage/test-step max/mean 2.33 -> 1.37). The plan says 'M0/SC
gate adoption', and every sibling pilot (R1, R3, SB, SC, SD, SE) recorded EXCLUDE because M0
outcome authority is incomplete. Should R4 be adopted, i.e. changed live CI shard authority, gated
only by matched green CI?"* Answer as given (selected option label, verbatim): *"Adopt, gate on
matched CI"*. The answer arrived at 2026-10-13T01:30Z UTC, which is 2026-10-12 local. Durable
record: the operator-decision comment on bead `phaze-l5uve`. The selected option's description,
which is the assistant's framing of the choice rather than the operator's words, carried the
adoption conditions verbatim: *"<=1.5 max/mean incl. setup/browser/combine, lower critical wall, no
missing contexts/skips"*. The decision does **not** extend to any velocity claim or to any other
pilot.

**Matched measurement.** Both runs are the `CI` workflow, `pull_request` event, conclusion
`success`. The baseline head is `a35be942` (tree `0ce62e2b`, the R4 parent tree). The candidate is
the exact PR #730 head `413e1e1b` (tree `46f548bf`). The figures below were re-derived for this
manifest from each run's job/step API timings and its retained JUnit, canonical-collection and
combined-coverage artifacts.

| Quantity | Baseline run 38317442319 | R4 run 38335835547 |
| --- | ---: | ---: |
| Seven "Run shard tests with coverage" steps (s) | 601 / 354 / 287 / 224 / 196 / 82 / 65 | 297 / 293 / 289 / 275 / 260 / 236 / 190 |
| Step max/mean | **2.326** | **1.130** |
| Seven whole shard job durations, started to completed, incl. per-job setup (s) | 647 / 394 / 336 / 276 / 238 / 162 / 124 | 349 / 331 / 331 / 325 / 299 / 285 / 276 |
| Whole-job max/mean (inclusive) | **2.080** | **1.112** |
| Longest test-workflow part, Setup start to last of {shards+combine, browser} (s) | **712** (shards+combine) | **469** (browser job) |
| Executed JUnit nodes (unique, zero duplicates) | 11313 | 11320 = 11313 + exactly 7 new |
| Skips / failures | 15 / 0 | 15 / 0 |
| Per-module order violations against baseline | n/a | 0 |
| Combined coverage lines (covered/valid) | 28051 / 28332 | 28051 / 28332 |
| Combined coverage branches (covered/valid) | 6505 / 6768 | **6504** / 6768 |
| Distinct coverage contexts, union of the seven shard `.coverage` context tables | 20245 | 20252 = 20245 + 8 new − 1 absent |

The 7 new nodes are R4's own manifest controls in `tests/shared/scripts/test_parallel_test_manifests.py`:
one positive receiver/lane check and six negative controls through the real validator. No baseline
node is missing. The ratio condition (at most 1.5) is met both on the test step alone (1.130) and on
whole shard jobs including setup (1.112). The longest part, which includes the browser job and the
coverage combine, fell from 712 s to 469 s. The critical path is now the browser job, which R4 did
not touch.

**Contexts: one baseline context is absent at the R4 head, observed, cause unproven.** The 8 new
contexts are the 7 new R4 manifest controls' `|setup` contexts and
`tests/integration/test_live_retune_e2e.py::test_retune_through_sighup|teardown`. The one absent
context is `tests/integration/test_live_retune_e2e.py::test_retune_through_an_admin_api_override|teardown`.
Its 54 arcs, in `telemetry/http`, `telemetry/instruments`, `telemetry/catalogue`,
`telemetry/tracing` and `routers/agent_heartbeat`, all reappear under the new
`test_retune_through_sighup|teardown` context. That module ran in the `integration-shared-services`
shard in both runs; R4 did not move it, though it did move other modules out of that shard. So
this looks like teardown-time attribution shifting between two neighbouring tests, and no source
arc was lost from coverage. That is an observation from the two runs' artifacts; the cause is not
proven.

**Limits.** This is **one** matched pair of runs, not a stable p95 and not a distribution. It
measures CI shard balance. It does not measure local gate time, review latency or accepted-bead
velocity. The local two-lane split is unchanged and was not re-measured here.

**One combined branch arc changed, and the cause is not established.** Branch
`src/phaze/routers/pipeline/backfill.py:114 -> 121` was covered at baseline and not at the R4 head.
That arc is the `if skipped:` false edge: live keys exist and no candidate is skipped. Source is
byte-identical between the two heads. At baseline the arc's coverage contexts are
`tests/shared/routers/pipeline/test_backfill.py` nodes in the `integration-shared-services` shard.
R4 moved that directory to `analyze-rest`. The **inferred**, unverified mechanism: a predecessor in
the old shard left live job rows that made the live-key set non-empty, so the arc was covered by
cross-test state, not by a test asserting that behavior. Line totals are identical. The plan sets no
repo-wide branch floor, and the per-bead branch check does not apply because R4 changed no
`src/phaze` file. **This is not a reason to undo R4, and it is not resolved by this manifest.** A
deliberate test of the "live keys present, none matching" edge, plus a check of whatever state was
leaking between tests, is the real fix. That needs its own bead. G0 should treat the arc as a known
pre-existing gap, not as an R4 regression or a loss to explain away.

## Deferred branches

None of these is authorized in this round. Each needs separate authorization and a formal replan
before it can carry bytes into a union gate. A deferral asserts no outcome.

| Branch | Plan row | Status in this round |
| --- | --- | --- |
| R2: capacity-neutral Beadhive admission fairness | DEFER live policy | **DEFERRED**: external Beadhive/HQ policy; no replay authorized |
| R5: later factory/registry bounded seeding | DEFER until measured | **DEFERRED**: no factory cost pilot run |
| COND: global condition policy (`phaze-5f1sv`, open) | DEFER global policy | **DEFERRED** |
| ENV-M: 34-graph coarse model proof (`phaze-28883`, open) | DEFER environment approval | **DEFERRED**: R3 keeps the existing **12-head** real-model test |
| ENV-B: browser/artifact fidelity (`phaze-5jo4c`, open) | DEFER environment approval | **DEFERRED** |
| ENV-R: remote lifecycle validation | DEFER | **DEFERRED** |
| SE_B: browser portion of SE | conditional on ENV-B | **DEFERRED**: SE's nonbrowser exclusion above is unaffected |
| C6_B: browser flow portion of C6 | conditional on ENV-B | **DEFERRED**: C6 (`phaze-q8j9r`, `ce4badef`, merge `6ea02240`) landed HTTP/U+P scope only |
| SF policy: branch-ratio policy change (`phaze-pms85`, open) | DEFER policy | **DEFERRED**: SF diagnostics (`phaze-ihvi5`) landed with existing branch policy preserved |

## Populations and denominators carried into G0

Original nodes are preserved. The latest matched CI population is 11320 executed nodes with 15
skips (above), and every baseline node is still present. The plan-era figure (**11187 passed, 7
skips, 27912/28245 lines** at `75e586ef`) and the run figures above come from different trees and
different recipes. Neither is a delta of the other. Denominator explanations stay in the documents
that own them: the [accepted increment](accepted-increment.md) and the
[coverage baseline](../quality-assessment/coverage-baseline.md). This manifest does not restate or
reconcile them. G0 must report its own assembled population, ordered disjoint node union, contexts,
denominator and strict >95% overall line result. It must keep existing branch rules and all six
canonical real-failure oracles. Any byte adopted after this manifest requires a renewed final union
gate.
