# Stronger condition, invariant and consumer assertions

Assessment bead `phaze-lvw8l`, within `phaze-3l0q6`; inspected 2026-10-11 UTC.
Turn the six reproduced, filed defects into regressions that assert the intended consumer
outcome and fail on the recorded faulty behavior. Add bounded independent guard-operand
controls where they distinguish an actual refusal or invariant. This proposal is ready for
implementation review; it implements no test, repair, mutation experiment or new hunt.

## Evidence and acceptance contract

Inputs are the [mutation hunt](mutation-hunt.md), [analysis hunt](analysis-hunt.md),
[companion hunt](companion-hunt.md), [risk inventory](testing-risk-inventory.md),
[coverage backfills](coverage-proposal.md), [runtime proposal](runtime-optimization-proposal.md),
[runtime measurements](runtime-profile.md), [selector proposal](selector-proposal.md) and
[coverage baseline](coverage-baseline.md). Actual canonical issue bodies were read through
`bh work issue` for every defect and the condition/model/branch policy references. This leaf
starts at `b861e895abb6f564ed5e547f14a46e50b70d1cd9` after companion assessment integration.
No shared index, source, test, fixture, model environment or CI configuration changes.

The six intake reports are **5 P2 and 1 P3**; `phaze-ct6l1` is verified **P3** even though
the intake CLI assigned tracker priority 2. Their passing scratch assertions demonstrate the
bug, not correct behavior. Future regressions reverse that expectation at the named consumer.
They must be red on the named faulty source and green only when the invariant is satisfied;
an xfail, skipped environment or mocked-out physical operation cannot discharge acceptance.
Refresh exact source seams against the implementation tip before editing, without refiling
the canonical defects or silently treating unrelated repairs as the same regression.

The historical green ruler is `d1835b022f99397ed6be69359c57938a9ceaffd9`:
**27,482/27,793 = 98.88101320476379% lines**. Companion source at `c3ba3617` already included
19 commits touching 69 files beyond that measurement. This proposal does not infer coverage
of current bytes from historical counts. Retain **strictly >95% lines**, all existing branch
checks, exclusions, floors and canonical collection. Explain any loss from the historical
ruler with matched covered/missing sets, numerator/denominator and actual current source delta;
selected tests cannot publish a replacement whole-suite baseline. Assertion strength is a
separate result from executed lines and diagnostic arcs.

## Six canonical failing regressions

Suggested names below are implementation targets, not existing nodes. Keep original positive
and refusal nodes, including the inspected controls cited in the hunt reports. Budget rows
later bind each case's sample, clock and cleanup; all proposed runtimes remain unmeasured.

| Case / canonical defect | Proposed regression location and failing oracle | Current faulty behavior and required repair boundary |
| --- | --- | --- |
| A1 / `phaze-zjwp4` — live tag writer after Undo, P2 | Extend the real tag retry/Undo integration seams with `test_cancelled_writer_cannot_overwrite_acknowledged_undo`. Hold A before real mutagen write, cancel its await, start retry B. If B completes while A is held, complete actual Undo and capture acknowledged tags/digest BEFORE releasing A; release A, settle every writer and assert Undo unchanged. If genuine ownership blocks B, observe its admission stage, release A, await B then actual Undo; reread final disk/audit/hash | Current admitted thread resumes after Undo and changes tags without a callback. Mutation ownership must outlive the coroutine and govern retry/Undo until the actual worker terminates; alternatively terminate and reap the owned subprocess before admitting successors |
| A2 / `phaze-ct6l1` — partial save loses original Undo anchor, P3 | Extend `tests/integration/test_tag_undo_loop_seam.py` and real tag task/callback fixtures with `test_partial_save_retains_original_undo_anchor`. Inject only the installed ID3 tail-save failure after the real ID3v2 save. Confirm mutation evidence and durable original; actual Undo restores original, and a subsequent retry/Undo chain still anchors to that original | Current FAILED filtering excludes the first possibly landed save and later anchors to already changed tags. Selection needs durable evidence distinguishing pre-mutation failure from attempted/possibly landed mutation; adding every FAILED row is incorrect |
| A3 / `phaze-83205` — reviewed metadata changes before enqueue, P2 | Extend `tests/integration/test_selected_source_consumers.py` and the actual bulk tag route with `test_bulk_later_candidate_revalidates_metadata_before_audit`. Validate genuine A/B tokens; pause before B's durable authorization, commit B's metadata change, resume. Separate-session query finds no stale B audit/job; response requires refreshed review. A retains its valid committed result | Current enqueue validates enrichment but omits reviewed file/metadata revisions. Lock/refresh and compare the actual reviewed revisions or used facts before audit creation and broker dispatch; already authorized jobs keep their original authority |
| A4 / `phaze-33c4p` — protected orphan is never revisited, P2 | Extend real orphan/startup integration tests with `test_scheduled_orphan_revisit_after_protection_expires`. Seed retired-backend UPLOADED rows, young or broker-live at boot; boot succeeds. Advance eligibility, run only registered scheduled work, and assert eventual AWAITING plus drain eligibility and one bounded transition. Never call a second manual orphan sweep as the success path | Current boot-only successful skip has no later owner. A narrow bounded revisit must recover the deferred cohort while age, live-key, current-owner and submitted/running protection remain intact |
| A5 / `phaze-jpm1y` — ACTIVE capacity waiter loses heartbeat, P2 | Extend `tests/shared/tasks/test_live_worker.py` and actual Postgres broker integration with `test_worker_waiter_survives_sweep_and_completes_once`. Actual Worker dispatch uses concurrency 2/child limit 1; first synthetic job progresses while second waits. Advance controlled SAQ time past its deadline, run actual sweep, assert one attempt/no false retry; release first and require second completion. Assert relay settlement on acquire/cancel | Current ACTIVE waiter has no job touch before permit admission. Maintain waiting-state liveness or keep capacity waiters queued; the mechanism must end on admission and must not mask a stalled admitted child |
| A6 / `phaze-g7x85` — equal interpretation loses new parent, P2 | Extend `tests/integration/test_local_source_import.py`, `test_provider_persistence.py` and viewing/backfill consumers with `test_equal_interpretations_keep_each_raw_attribution`. Capture unchanged synthetic text at 140,000 then 160,000 retained characters; import both. Fresh consumers before and after replay show current interpretations and incomplete classification, retaining the older acquisition's immutable history | Current equal normalized payload reuses a child with the first raw parent; current view is empty and classifier reports parser_old. Parent-scoped creation with correct conflict handling or a durable attribution edge must preserve all consumers and same-parent idempotency |

### What the existing observations actually prove

A1 used real task, synthetic MP3 mutation, PostgreSQL and ASGI snapshot/result/Undo callbacks,
production queue serialization, fake delivery and an Event barrier: **1 passed, 11 warnings
in 6.38 s**. It did not run SAQ's actual timeout/abort delivery or a real mount hang. The
regression must distinguish two schedules. If B completes while A remains held, complete actual
Undo, independently capture its acknowledged raw tags/digest and durable hash, THEN release A
and settle every writer. The final read must still equal that Undo acknowledgement; the old
code is red on this path. If a safe ownership repair blocks B, observe the real blocked/admission
stage with a bounded event/probe, release A within the harness budget, await B, then perform
actual eligible Undo and reread final disk/audit/hash. A timeout alone is not proof of ownership
blocking; retain the admission-stage evidence and treat an unexplained timeout as incomplete.
Do not universally postpone Undo until A terminates: that would pass the faulty code. Requiring
B to complete while A holds ownership would reject the intended serialization repair. Retain a separate actual
Worker timeout/cancellation case before claiming the full SAQ lifecycle is established.

A2 used real ID3v2 save with a narrow `ID3.__save_v1` error, real callbacks and actual Undo;
A3 used genuine token validation, a committed metadata revision, real enqueue and serializer.
The accepted isolated rerun for A2/A3 together was **2 passed, 11 warnings in 1.79 s**.
Their original intake timings (1.48 s and 0.96 s) used the default test database and are
superseded for seat evidence. Fake delivery meant caught callback ledger-cleanup errors did
not prove live `saq_jobs` cleanup. A3 observed the per-row validation/enqueue gap, not actual
two-candidate bulk scheduling or a disk overwrite. Proposed bulk/Worker consumers extend that
proof; no audio corruption or permanent loss follows from the original observations.

A4 used real PostgreSQL, boot helper, reaper, periodic reconciliation and drain query:
**2 passed, 10 warnings in 15.71 s**. Ages were 60 seconds then 901 against a 900-second bound;
three periodic ticks left both protected-at-boot cohorts stranded until a manual sweep.
Broker liveness was synthetic SQL state, not live delivery, S3, Kubernetes or a production restart.
The new regression must invoke the actual configured scheduler/registered callable, proving
the revisit is wired, while keeping remote providers outside this local invariant.

A5 used accepted settings 2/2/1, actual limiter, production `_extract_and_analyze`, installed
`Job.stuck` and real `PostgresQueue.sweep`: **1 passed in 0.57 s**. A manually stamped ACTIVE
row and held permit substituted for actual Worker dispatch and a progressing child; controlled
SAQ time advanced 3,601 seconds past a 3,600-second heartbeat. Actual Worker dispatch, abort
delivery, extraction and inference remain additional indispensable consumers for the full
liveness claim. Permanent ledger orphaning was not established.

A6 used actual bounded capture, authenticated storage helper, import, review context and
classification on a real isolated database: **2 passed, 10 warnings in 1.09 s**. Input was
170,000 ASCII characters, below the 262,144-byte capture bound; default import retained
131,072 characters. Both acquisitions and interpretations were incomplete. Same-140,000
control showed two interpretations/incomplete state; changed capture showed zero/parser_old.
Replay asserted the same IDs retaining a stale parent, without another consumer reread.
The new regression adds fresh-session consumer rereads after replay. The original used ORM
schema, so retain real migration 085 immutability/upgrade consumers separately; a view-helper
test cannot certify the database trigger or browser native form. Playwright was absent during
the companion hunt; its frontend pass is static-only.

## Named guard-operand matrices

Define a valid base fixture and vary one named operand while the others remain valid. For an
OR refusal guard, use the all-false case plus each individually true refusal; for an AND
eligibility predicate, use all-true plus each individually false refusal. Keep exact input
values and observed operand states in case IDs. Exercise short-circuit positions using valid
earlier operands so the target operand is reached. Coupled/undefined operands require a
separate constrained fixture and explicit rationale, not a claimed impossible independence.
This is targeted condition evidence, not a new global MC/DC or branch policy.

| Matrix | Named operands and minimum base/change rows | Independent consumer oracle |
| --- | --- | --- |
| G1 recovery row shape | `blob_parses`, `key_is_string`, `function_resolves_to_key_builder`. Four rows: valid; malformed blob; non-string key; unresolvable function/key. A fifth positive row omits/non-stringifies the blob function but supplies a supported key prefix: current helper deliberately falls back. Current implementation lives in `tasks/recovery_backfill.py::_classify_saq_job_row` | Exact classification/key versus refusal; invalid row produces no redrive enqueue. Preserve supported prefix fallback and all identity/key builders' existing controls; do not turn the historical function-type check into a new current refusal policy |
| G2 reviewed authority | `file_revision_same`, `metadata_revision_same`, `selected_evidence_same`, `legacy_revision_same`, `discogs_identity_and_facts_same`. Six primary rows; split Discogs identity, timestamp and facts into three additional pairwise isolated cases where feasible | Changed operand before durable authorization yields no new audit/job and refreshed-review response; separate transaction reread. Same reviewed facts allow the expected serialized job; changing an already authorized row is a distinct lifecycle |
| G3 replay and owner | `intent_supported`, `file_identity_same`, `attempt_owner_current`, `inventory_revision_same`, `durable_commit_marker_present`. Valid execution/replay controls plus independent stale/wrong/unsupported rows; marker-present and marker-absent have different allowed transitions | No unreviewed mutation or minted replay authority; byte/digest/audit state agrees with the documented transition. Preserve existing real SIGKILL/fsync/marker consumers; marker presence alone never substitutes for matching owner/intent |
| G4 orphan eligibility | `row_exists`, `status_is_staging`, `backend_is_retired`, `broker_key_not_live`, `age_at_least_bound`. Six primary rows; ages bound−1, exactly bound, bound+1 supplement the time operand, and configured/SUBMITTED/RUNNING controls remain | Under scheduled work, ineligible rows' status/attempts remain byte-for-byte unchanged; eligible retired staging cohort becomes awaiting once and enters the actual drain query |
| G5 interpretation identity | `same_source_object`, `same_parser_generation`, `same_full_revision`, `same_normalized_payload`, `requested_parent_valid`, `current_parent_attributed`. Valid same-parent replay plus each isolated identity difference; payload equal/different and same/distinct-parent pair form a four-row attribution core | Digest collisions alone never imply equality; equal payload/distinct capture remains visible to that capture's consumers, old rows unchanged. Missing/foreign parent is refused; semantic equality must not invent a conflict |
| G6 progress and lifecycle | `waiting_for_capacity`, `admitted`, `meaningful_progress_advanced`, `cancelled_or_terminal`. Explicit state transitions rather than unconstrained Boolean combinations | Waiting job stays live without spending retries; admitted progress extends only progress supervision; admitted no-progress is terminated/reaped; terminal/cancelled tasks leave no relay touching the job |

G1 reuses **`phaze-5f1sv`**, whose historical fused guard hid the non-string-key operand
despite covered arcs. Its 12,996-combination differential sweep showed incumbent equivalence,
not a production defect. The current helper has moved and uses parsed-blob/key validation plus
supported-prefix fallback; the matrix follows current behavior rather than reproducing an old
guard verbatim. G2 is a proposed durable contract including the currently missing
metadata/file comparisons, not a claim that those operands already exist at that boundary.
G3/G6 reuse the existing execution/analysis invariant controls rather than starting another
hunt. Existing input/schema validation is part of each supported caller; helper-only malformed
rows do not prove an HTTP input can bypass its schema.

## Invariant, consumer and bounded negative controls

For each proposed test, name the one temporary altered source operand/transition it must
detect, the first wrong observable and the original positive control it must preserve. Apply
an operator only in a disposable implementation-review worktree after a baseline green test;
restore exact bytes and verify the final tree. Nothing in this assessment runs these operators.
Never publish altered guards, weaken production protection, or test against production state.

| Invariant / bounded operator | Expected failure and preserved controls |
| --- | --- |
| A1 remove mutation ownership/drain or restore direct cancellable offload | After settling all writers, acknowledged Undo's tags/digest fail. Controls: serial write/Undo succeeds, terminal snapshot refusal still prevents new offload, cancellation before mutation leaves original unchanged |
| A2 restore FAILED-only exclusion despite landed-save evidence | Actual Undo fails to restore original after injected tail save. Controls: pre-save/snapshot failure remains excluded; non-mutating FAILED retry shadow does not displace the real original; successful retry chain remains anchored |
| A3 omit one durable revision comparison or read stale ORM state | B's stale audit/enqueue causes the no-side-effect oracle to fail. Controls: unchanged token passes; stale initial POST still refuses; selected/legacy/Discogs guards retain independent refusals; valid A is not rolled back by later B refusal |
| A4 remove scheduler registration/revisit, or bypass age/live gate | Registration removal fails eventual scheduled recovery; bypass fails unchanged protected-row control. Controls: configured and submitted/running rows remain owned/surfaced, repeated eligible ticks do not double-increment attempts |
| A5 remove waiting relay/admission protection, or let relay survive admission | Real sweep retries the healthy waiter, or no-progress child remains falsely live. Controls: cancelled waiter settles; admitted progress touches remain; capacity increase/shrink maintains supported ownership; terminal job gets no later touches |
| A6 restore global equal-payload reuse without attribution, or relax current-parent filter | Latest raw's fresh consumer view/classifier fails, or a different parent's interpretation falsely appears. Controls: same-parent replay idempotent, full payload collision test, old history immutable, parser-old/foreign-parent refusal and incomplete status preserved |
| Exhaustive chunk-bounded analysis: drop one tail/natural window or count only successful coarse windows as total | Independent expected window inventory/count assertion fails, including rejected/failed window accounting. Controls: short/exact/tail fixtures, fine/coarse tiers, decode errors and real progress/teardown remain; do not reintroduce caps/stride |
| Consumer fidelity: remove migration immutable-update protection, rename actual form field/target, or corrupt telemetry tier/count | Actual upgraded DB rejects immutable update; actual browser submits pinned intent and displays result; real SDK export rejects incorrect totals/undeclared fields. Each needs its own consumer environment; mock SQL, markup strings or fake spans cannot count as that proof |

A surviving altered source variant means **no demonstrated detection for that operator**.
First verify it reached the changed operand and caused a distinguishable supported outcome.
If equivalent/unreachable under the actual caller contract, retain that evidence and narrow
the claim. If reachable and wrong, strengthen the consumer oracle or leave the case unresolved;
never count line execution or another killed variant as its success. An operator that breaks
import/setup is a harness failure, not detection of the intended semantic fault. Expected
refusal must prove unchanged durable/disk state where relevant, rather than only a raised
exception, HTTP status or call-count assertion. Do not collapse cases just because they reach
the same lines: remove redundancy only when the preserved assertion and failure distinction
are demonstrably the same.

## Case budgets, environment classes and cleanup

These are **proposed deterministic sample/clock/resource caps**, not measured runtime or speed
promises. First implementation pilot may reject or revise a cap with a recorded reason;
exceeding it is an incomplete/failing harness observation, never a green skip. Limit baseline
and each altered variant to one targeted invocation initially; after detection is established,
use three paired warm baseline/candidate targeted samples sequentially. Do not run full gates
per matrix cell. Required bead/molecule/CI gates remain the final authority.

| Case | Bounded sample and clock | Proposed containment/cleanup cap; indispensable class |
| --- | --- | --- |
| A1 | One synthetic MP3; A/B/Undo schedule plus pre-mutation cancellation and serial controls. Event waits at most 5 s each; no mount outage or 600-second sleep | Run the whole physical-thread schedule in an owned subprocess, 60 s wall cap, terminate then kill/reap within 5 s each if stuck. Release barriers in finally, settle executor and callbacks before success. Real disk + PG indispensable; actual Worker timeout case separate |
| A2 | One synthetic MP3 per landed-tail-error, pre-save-error and successful-chain case; one injected tail fault per case | 30 s owned process cap, 5 s terminate and 5 s kill/reap on failure; restore mutagen patch, close file and settle callback/Undo before deletion. Real ID3 save/PG indispensable; no replacement save mock |
| A3 | Two synthetic records; one B change after validation; unchanged and independently stale-revision controls. At most one held barrier per transition | Async waits 5 s each; release barriers and await tasks within 10 s, rollback/close all sessions and inspect durable rows independently. Real PG + actual bulk route required; recorder delivery labelled substituted, live serializer/Worker consumer separate |
| A4 | Two protected cohorts (young/live), one configured and submitted/running controls. Ages 60→901 at bound 900; extra bound−1/exact/bound+1 rows. At most three scheduler ticks plus idempotency tick | Controlled clocks, no 900-second sleep. Each await 5 s; settle scheduler within 10 s, remove task-owned broker rows, rollback/close sessions. Real PG and actual registered work required; remote cloud environment optional |
| A5 | Two Worker jobs, concurrency 2/child limit 1; controlled time advances 3,601 s beyond heartbeat 3,600. Permit release, cancellation and admitted-stall variants separately | At most three observed relay/sweep cycles per variant, 5 s event waits and 10 s Worker/queue shutdown. Owned worker subprocess capped 60 s with 5 s terminate/kill/reap steps. Real PG/Worker indispensable; no inference needed to prove waiting admission |
| A6 | Two captures of one 170,000-character synthetic text, budgets 140,000/160,000; one import and one replay each. Same-140,000 control; fresh consumer queries after replay | Real capture file remains under tmp fixture; each await 5 s, sessions/transactions settled within 10 s. Real PG persistence and current consumers indispensable. Migrated-schema immutability and browser interaction are separately retained |
| G1–G6 operand rows | Enumerated rows above, one valid base plus independent changed operands; no cartesian whole-repo fuzzing. Each source operator targets one named operand | Pure/local cases get one 5 s killable-process cap if synchronous code can loop; async cases follow their owning A-row budget. No blind retry of a failed schedule; retain seed, inputs, stage and teardown outcome |
| Window/reduction unit boundary | At most six deterministic PCM/input cases: empty/short/exact/tail/two-window/failed-window, both tiers where supported; exact independent window/count formulas and recorded reduction inputs | Pure numerical helper cases need no graph download; 5 s process cap for an intentionally broken synchronous loop. Actual decoder/child/RSS controls stay in real process class with existing contracts; costs remain unmeasured |
| Migration/browser/telemetry | One seeded 084→085 upgrade/history/update-refusal case; one actual pinned native-form flow plus stale/refused control; retain all eight actual telemetry nodes | Separate isolated migrations DB; browser and SDK resources closed via their existing verified fixtures. Retain existing fixture deadlines rather than invent shorter integration deadlines. Record unavailable tooling as unrun, not success |

Async timeout cannot terminate a blocked synchronous executor thread. A1/A2 require process
ownership, barrier release and actual termination/reaping evidence; returning after coroutine
cancellation or deleting its tmp file while a writer remains live is invalid cleanup. Synthetic
clock advances must drive the actual production scheduler/SAQ clock seam and permit the relay
to run at explicit barriers; advancing only `Job.stuck` time while starving its relay tests the
harness rather than waiting-state protection. Keep meaningful-progress stall supervision for
admitted analysis independent of broker liveness.

Fast deterministic **unit/boundary** work covers G1 row shapes, pure comparisons/reductions,
serialized key/payload expectations and bounded refusal collaborators. Indispensable **real
integration** work covers physical tags/digests, PG locking/revision visibility, actual scheduled
work, Worker dispatch/sweep/cancellation, upgraded-schema immutability and browser/native forms.
Optional environment checks cover remote-provider restart/storage behavior and model/image
compatibility. Optional here means separate approval/environment delivery; a task claiming that
consumer cannot replace the check with a mock or convert its absence into acceptance.

Every PG run uses exact `just test-db-for` exports for test DB, migrations DB and Redis;
port 5433, test-name guard, exclusive session header, one pytest process per DB, no shared
bypass or container teardown. Current docs-gate seat is independently allocated. Concurrent
catalog queries are scoped to `current_database()` or the waiting backend. Preserve function
loop/session isolation, real subsequent-query recovery checks, two full-gate slots and seven
CI shards. Fixture/event clocks bound a schedule, not proof of production elapsed behavior.

## Numerical and real model claims

| Evidence class | What a proposed assertion may establish | What remains outside its claim |
| --- | --- | --- |
| Incumbent differential equivalence | Same documented output on a frozen bounded corpus, matching errors and ordering; the G1 historical sweep and recorded reductions are examples | Both implementations can share the same wrong algorithm; equal values do not establish independent correctness or real models |
| Independent correctness | Exact hand-derived window counts/boundaries; independent simple reductions with known units; explicitly toleranced numerical fixtures only after the reference and feasible precision are chosen | No ungrounded float tolerance, BPM/key absolute guarantee, whole-archive accuracy or 34-model inference claim |
| Actual decoder/model/consumer | Real decoding/BPM/key through the installed pipeline; separately pinned real graph artifacts and output shape/finiteness/counts; real browser/migration/SDK outcome | Pure predictions or empty models do not discharge graph sweep; static markup does not discharge a native submit; ORM tables do not discharge a migration |

Canonical **`phaze-28883`** owns the missing real **34-graph** coarse sweep. Existing real
fine-tier equivalence uses an empty model directory for coarse inference; recorded coarse
reductions remain explicitly labelled. The measured real telemetry population has **8 nodes**
and its successful real-model consumer used **12 selected heads**, not all 34 graphs. Preserve
that result and the fresh SDK/exporter/no-contamination controls while proposing additional
proof separately. Pin real model bytes/manifest/runtime, verify artifact availability, use
bounded synthetic short/exact/tail audio, report every graph output/count and chunk/RSS
consumer. Select independent reference/tolerances only after feasibility is established;
no numerical promise or model acquisition is made here. Reuse **`phaze-5jo4c`** for broader
packaged-image, migration, telemetry/dashboard and browser seams, without duplicate filing.

## Delivery and review boundaries

Implement A1/A2 with their separately approved mutation/recovery repairs, A3 with durable
authorization repair, A4/A5 with recovery/liveness owners and A6 with attribution repair.
Coordinate shared tag/callback fixtures and shared worker fixtures serially; independent
consumer additions can use separate worktrees/seats. Do not file a generic test task that
duplicates a canonical defect's regression acceptance. G1 condition visibility belongs with
`phaze-5f1sv`; adopting global condition coverage or rewriting fused guards is its policy
decision. **`phaze-pms85`** remains diagnostic branch-policy work: print covered/total/missing
and inherited/new arcs; keep existing branch checks until separately decided. Its 44/46→42/44
example retained two missing branches and does not justify dropping a real new uncovered arc.

The **open `phaze-4ywkm` companion-file dedup/delete policy** remains unresolved. A6 preserves
existing observation idempotency and immutable history without deciding that file policy.
The companion local-directory candidate stays source-only, unrun, unfiled and deferred; it
has no proposed operational experiment or regression claim here. Static frontend review and
three interrupted lead turns do not establish convergence. The three bounded hunts remain
complete within their declared source-read lenses; their tooling failures and successful
observations retain separate accounting in the maintained reports.

Review each implemented row with canonical ID, exact source before/after, actual failing
assertion, altered variant reached/killed/survived, all preserved controls, sample manifest,
service/model/tool versions, collected/skipped nodes, resource cleanup and measured setup/call/
teardown/queue/gate time. A proposed test green on faulty source blocks acceptance until its
oracle is corrected or its unsupported claim is explicitly deferred. Required hooks and exact
tree gate validate this documentation only; no proposal cell, mutation, full baseline or hunt
was run by this bead. Task-owned actual issue readbacks and gate evidence are retained outside
tracked source in `assessment-artifacts/phaze-lvw8l` beneath the Beadhive worktrees root.

Optimize **accepted-bead velocity** with the selector/lifecycle joins and paired measurements
already proposed. Historical scratch seconds guide feasibility but are not incremental test
costs, stable tail estimates or speedup promises. Preserve same-row failure detection and all
true branch gates. Stop hunts and complete the authorized quality plan; implementation filing,
kickoff and any later assessment round require their own approval.
