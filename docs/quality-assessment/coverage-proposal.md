# Focused coverage backfills for faster accepted beads

Assessment `phaze-xy08m`, epic `phaze-3l0q6`; inspected 2026-10-11 UTC.
Prioritize refused mutation and honest degraded state, then small configuration/path guards.
These are implementation-ready proposals, not implemented tests, reproduced defects, or
permission to file the implementation molecule. Verified hunt fixes belong to the later
assertion proposal and must not acquire duplicate regression tasks here.

## Evidence, targets and cost contract

Source ruler: `d1835b022f99397ed6be69359c57938a9ceaffd9`, the immutable successful CI
[baseline](coverage-baseline.md): **27,482/27,793 = 98.88101320476379% line coverage**,
384 production Python files, 311 missing lines. This task starts at `f683964d`, with only
assessment documentation added to that source ruler. The [risk inventory](testing-risk-inventory.md)
joins exact missing lines with recent changes and existing consumer tests. Line references below
refer to that immutable source, not future moving HEAD. Dynamic execution is evidence that a
line ran, not that an assertion would reject a wrong result.

Overall acceptance stays **strictly >95% lines**, with no unexplained regression from the
fresh 98.88101320476379% baseline. Numerator/denominator and changed covered/missing sets must
accompany each matched before/after report. Do not change exclusions, patch thresholds, branch
policy or the denominator to meet this proposal. Proposed module targets apply only to the
named follow-on tasks; they create no blanket per-module floor. Branch/condition evidence is
diagnostic and may expose failures that line counts cannot.

| Module under `src/phaze/` | Baseline | Follow-on line target | Minimal additional lines for >95% at unchanged denominator |
| --- | --- | --- | --- |
| `routers/duplicates.py` | 117/128 (91.41%) | >95% | 5 |
| `services/source_workspace.py` | 64/70 (91.43%) | >95% | 3 |
| `services/companion_capture.py` | 64/68 (94.12%) | >95% | 1 |
| `runtime_config_notify.py` | 56/61 (91.80%) | >95% | 2 |
| `runtime_config_catalog.py` | 23/25 (92.00%) | >95% | 1 |
| `runtime_config_backends.py` | 52/55 (94.55%) | >95% | 1 |
| `services/media_path_resolve.py` | 39/42 (92.86%) | >95% | 1 |
| `services/pipeline/analyze.py` | 94/100 (94.00%) | >95% | 2 |
| `services/backends/lane_snapshot.py` | 141/149 (94.63%) | >95% | 1 |
| `services/selected_cue.py` | 53/57 (92.98%) | >95% | 2 |
| `services/selected_discogs.py` | 40/43 (93.02%) | >95% | 1 |
| `routers/cue.py` | 155/167 (92.81%) | >95% within source-authority task | 4 |

The last column is arithmetic, not a quota for padding tests. Exercise all named meaningful
failure paths even when a single extra line meets the percentage. Duplicates' five guard/error
lines alone can meet its target; file-size formatting need not be added merely for coverage.

Runtime classes used below: **U** = pure/local fault-injection test, no service or child;
**P** = isolated real Postgres transaction/HTTP consumer; **B** = real browser/native form;
**R** = real process/broker/model consumer already retained. No new proposal has measured
seconds yet. A numeric savings estimate, promised speedup or hard seconds budget would be
invented. The implementation measurement budget is one targeted before/after sample series
at the same source/host/isolation, then the required bead and molecule gates; no full suite
per matrix cell. Record collection, setup, call, teardown, queue and total gate time separately.
Choose a numeric incremental budget only after that pilot and final decision; keep the
unmeasured addition on the review sheet until then. Use the [feedback measurement](../bead-feedback-measurement.md)
and runtime-profile report to assess accepted-bead latency, rather than optimizing only call time.

## Proposed task C1: duplicate refusal at the actual HTTP boundary

**Priority P1; dependencies:** risk inventory and final implementation decision; coordinate with
any hunt-derived dedup fix before editing. Extend `tests/review/routers/test_duplicates.py`
using its durable plan/group factories and real database-backed client. Runtime **P**.

- Source `routers/duplicates.py:187`: submit an otherwise valid reviewed plan with a
  PostgreSQL-invalid group hash through the actual route. Assert no 500, safe no-op response,
  and unchanged durable plan/group/audit state. A service-only test does not execute this guard.
- Source `:195`: inject a zero-result `commit_review_plans` outcome at the route seam after
  seeding a real plan. Assert safe no-op response with no resolve-success/undo marker.
  This is a **substituted service outcome**, not evidence of a real CAS race; retain
  `tests/integration/test_dedup_resolve_undo_shadow.py` for the real replay/CAS boundary.
- Source `:306–308`: submit a bulk request containing an invalid or stale durable plan,
  through the actual commit route. Assert expected error fragment, rollback, no partial
  resolution/audit insertion, and that a subsequent valid request still commits.
  Existing service guards cover empty/repeated/partial/foreign/replayed plans; existing
  router bulk-OOB tests cover successful and empty removals, not these missing catch lines.

**Failure oracle and negative control:** snapshot durable rows before/refusal and query them
with a separate real session after the response; compare exact state, not just response status.
Delete the invalid-plan catch/rollback in a temporary mutation and demonstrate a red error
or poisoned-next-request assertion. Removing the response refusal must make the success-marker
assertion fail. Revert mutations before commit. Filesystem absence claims require a real
filesystem consumer fixture; HTTP/database checks alone must not be described as disk proof.

**Acceptance:** new tests reach all five named lines, refuse invalid authority without partial
state, retain dedup guard/replay integration cases, meet the module target and report incremental
P cost. No formatter-only padding and no fixture scope change in this task.

## Proposed task C2: companion dispatch eligibility and workspace transaction recovery

**Priority P1; dependencies:** final decision and companion-hunt disposition; keep this test task
separate from any actual capture/persistence fix. Extend
`tests/integration/test_companion_capture_broker_seam.py` (eligibility),
`tests/discovery/routers/test_agent_companion_capture.py` (HTTP refusal), and
`tests/integration/test_selected_source_consumers.py` (workspace). Runtime **P**, with existing
**R** broker/authenticated-agent subprocess test retained.

- `services/companion_capture.py:44`: change owning agent before calling dispatch with the
  previously expected owner. Assert `ValueError` and no queue enqueue/read/report side effect.
- `:60`: independently test a 129-character request key and a NUL-bearing key; accept a
  128-character control. Assert refusal and **no enqueue**, not no queue connection: current
  code connects before this check. A queue recorder is a labeled substitute, not real broker proof.
- `:128`: independent missing and ambiguous inventory cases; `:133`: matching-fingerprint
  junk, contrasted with old-fingerprint junk. Assert refusal of current junk only and no enqueue.
  Existing worker byte/character caps, wrong-worker and symlink/subprocess cases concern reading;
  they do not execute these controller-side eligibility guards.
- `services/source_workspace.py:77–79,143–145`: exercise both real query consumers using an
  actual PostgreSQL statement error inside their nested transaction. One targeted execute wrapper
  may issue deliberately invalid SQL on the same session and then delegate normally; label the
  fault injector explicitly. Assert an empty bounded page (`has_next=False`, clamped page/size),
  warning, and successful subsequent SQL in the outer transaction. Render the real CUE/source
  workspace consumer and verify no stale selected-action authority appears. Existing tests prove
  current/stale/missing inventory and successful pages; they never reach these exception tails.

**Negative controls:** remove each eligibility operand individually and require the no-enqueue
assertion to fail; change fingerprint equality to inequality and require the old-junk control to
fail. Remove `begin_nested` and require a real subsequent SQL query to expose the aborted
transaction. Changing the empty degraded page to stale cached authority must fail the rendered
consumer assertion. Do not claim these proposed mutations have been run in this assessment.

**Acceptance:** all four capture guard lines and both workspace error tails measured; module
line targets met; independent transaction and real broker tests retained. Matrix setup may reuse
factories, but cases retain independent state and cannot share a database with concurrent pytest.

## Proposed task C3: configuration failure guards without long sleeps

**Priority P2; dependencies:** final decision; independent of source-authority changes.
Extend `tests/shared/config/test_runtime_config_notify.py`,
`tests/shared/config/test_runtime_config_backends.py`, and add
`tests/shared/config/test_runtime_config_catalog.py`. Runtime **U** for new defensive cases;
existing notify/poll override tests remain **P** with real LISTEN/NOTIFY.

- `runtime_config_notify.py:99`: native `postgresql://` DSN passes unchanged; keep the
  driver-prefixed normalization controls. Assert exact string, not a substring.
- `:182–184,188`: event-controlled reload substitute first raises `RuntimeError`, then
  permits a second iteration; assert warning and continuing task. Cancel while blocked inside
  reload, assert propagated cancellation and await handle stop/closed connection. Use events
  and bounded waits, not a 30-second sleep. Existing committed-NOTIFY/missed-NOTIFY tests
  exercise real propagation; they do not execute these defensive poll-handler faults.
- `runtime_config_backends.py:91–93`: controlled `Path.read_bytes` permission/I/O error
  distinguishes real `OSError` from FileNotFoundError. Assert `None`, warning and no contents
  or secret value in captured log. Drive reloader with last-good settings and unavailable file;
  assert it does not silently replace those settings, and a later readable edit can apply.
  Existing invalid-TOML, in-flight-removal and identical-content tests are different paths.
- `runtime_config_catalog.py:70`: erase both explicit help and model-field description for
  one restart-only key in a restored monkeypatch context; assert named `AssertionError`.
  Assert the normal returned catalog has names/descriptions but no resolved secret values.
  The `:76` import-time mismatch guard is a separate isolated-process candidate: if added,
  load the real module with controlled mismatched reloadable-key inputs and require failure.
  Avoid shared interpreter reload that mutates imported singleton/config state. The >95% target
  does not require an expensive subprocess solely to execute this one line.

**Negative controls:** swallowing cancellation must fail the await/closed-resource oracle;
  exiting on ordinary poll fault must fail the second-iteration event. Applying an unreadable
  registry as empty must fail last-good identity checks. Removing the catalog assertion must
  fail `raises`. Existing config precedence, real NOTIFY and live backend removal controls are
  indispensable and remain enabled; mocks cannot discharge their consumer guarantees.

**Acceptance:** each named non-import defensive path executes with discriminating assertions,
all three module targets met, no wall-clock poll delay added, no changed configuration semantics.

## Proposed task C4: pipeline and lane degradation distinguishes unknown from empty

**Priority P1; dependencies:** final decision; coordinate runtime-profile recommendations but do
not fold fixture/shard redesign into these assertions. Extend
`tests/shared/services/pipeline/test_analyze.py` and
`tests/shared/services/test_lane_snapshot.py`; use
`tests/shared/routers/pipeline/test_analysis.py` for the real template response. Runtime **U/P**.

- `services/pipeline/analyze.py:342–344,384–386`: fault the working-set and filtered-page
  query independently inside a real PostgreSQL nested transaction. Assert empty rows,
  `has_next=False`, normalized page/size and preserved validated status for filtered pages;
  subsequent SQL succeeds. Existing successful pagination/status tests execute neither catch.
- `services/backends/lane_snapshot.py:379`: force routing-snapshot degradation, assert neutral
  hold reason, not an invented capacity/offline cause; pair with an observed empty registry.
- `:434,438–439`: fail settings/backend resolution and require **None**, not zero live count.
- `:476,480–482`: fail routing resolution/probe/in-flight count and require **None** plus
  rollback attempt; also fail rollback and require the bounded degraded response/log.
  Existing poisoned-probe/sibling-lane/fanout/rollback tests protect backend lane snapshots;
  they do not execute these distinct routing and aggregate live-count exception tails.

**Negative controls:** substitute `0` for unknown aggregate, `[]` for degraded routing, or a
causal capacity reason for neutral copy: require the real template/response distinction to fail.
Remove savepoint recovery and require the real subsequent SQL oracle to fail. Existing status
and no-overlap tests remain the positive controls; avoid testing only a private return value.

**Acceptance:** both module targets met, all named fault classes represented, status/unknown
semantics proved at service and rendered consumer; include P versus U cost separately.

## Proposed task C5: path fallback failure respects the containment consumer

**Priority P2; dependencies:** final decision; extend `tests/shared/test_media_path_resolve.py`.
Runtime **U**; preserve existing real disk NFC/NFD and
`tests/integration/test_execute_path_nfc_seam.py` consumers.

`services/media_path_resolve.py:53–54`: monkeypatch only directory iteration for a real
synthetic parent to raise `PermissionError`/`OSError`; assert original path returned and no
invented sibling chosen. The existing unreadable-parent-named test uses a nonexistent directory,
so it does not establish a directory-iteration error. `:95`: force the ancestor walk to reach a
self-parent/root with no existing ancestor, without changing global filesystem state; assert
bounded termination and original value. This second case is defensive U evidence, not a real
mount outage experiment.

**Failure oracle/negative control:** deleting the OSError handler raises; deleting the self-parent
termination fails a bounded collaborator-call sentinel: the controlled parent accessor raises
a test-only assertion after a finite number of accesses (for example, 8). The correct resolver
returns before the sentinel; the faulty loop reaches it and fails synchronously. An async
timeout cannot interrupt an in-process synchronous loop and is not an adequate guard. Do not
execute an unbounded mutation or add a killable subprocess without reclassifying its cost. Preserve byte-exact preference
and real symlink/ancestor-swap containment tests in `tests/discovery/services/test_companion_capture.py`.
The resolver supplies a path, not access authority; do not add a symlink prohibition here and call
it coverage work. Any containment change is separately reviewed product/security behavior.

**Acceptance:** >95% lines, controlled fault recognized, no filesystem mutation outside tmp
fixtures and no new production path policy. Evaluate whether root-edge coverage warrants its
incremental harness complexity before implementation approval.

## Proposed task C6: selected CUE/Discogs authority reaches actual forms and writes

**Priority P1; dependencies:** companion-hunt disposition and final decision; reconcile with
any separately active companion repairs before filing.
Extend `tests/integration/test_selected_cue_dispatch.py`,
`tests/integration/test_selected_discogs_consumers.py`, and the selected-source route tests.
Preserve `tests/browser/test_companion_viewing.py` native-form consumers. Runtime **P**, with
focused **B** additions only if current actual form coverage does not discriminate the fault.

- `services/selected_cue.py:45,54–55,63`: absent/revoked agent, missing/invalid/non-string
  capability receipt timestamp, and no selected recording source. Assert false/refusal,
  unchanged dispatch/write state and no legacy fallback. Existing owning-meta heartbeat and
  stale binding cases remain; named missing lines prove these particular tails were unexecuted.
- `services/selected_discogs.py:46`: selected source with empty/non-current tracklist evidence;
  `:55`: changed projection during lookup while other pins stay stable; `:73`: link absent,
  from foreign observation, or with an unmapped position. Assert exact refusal and unchanged
  link statuses/selection after real HTTP accept; retain concurrent-accept and real tag/CUE
  consumers. Existing changed-selection-during-HTTP test does not execute the tracks/mapping
  comparison failure in `:55` on this baseline.
- `routers/cue.py:114–115,143,146,157–159`: preview artifact rejection, missing recording,
  unapplied recording and enqueue exception. Assert rendered recoverable errors and zero
  artifact write; dispatch exception is a substituted queue outcome, not live outage proof.
  Legacy cases belong in `tests/review/routers/test_cue.py` and
  `tests/review/capabilities/cue/test_degrade.py`: `:303` refuses legacy generation when an
  explicit selected source exists; assert refreshed selected-source guidance and no enqueue.
  `:407` refuses missing media/unapproved/unversioned tracklists; `:409` refuses unapplied
  media; `:412` refuses a version with no timestamp. Assert no eligible CUE text or active
  generate control in the actual rendered error card, not merely a private `None`.
  `:569` handles a stale-version request on the legacy row/non-card target; assert the
  appropriate error surface and no write. Existing happy workspace/versioned-preview cases
  do not execute these exact tails in the baseline. Preserve their positive controls and do
  not promise whole legacy compatibility from selected-source-only tests.

**Negative controls:** accept a foreign observation/position by removing one identity operand;
force legacy fallback after explicit selected-source refusal; emit success after enqueue failure.
Each must fail a durable-state/write or actual form-response assertion. Refresh evidence against
then-current source before adding mutations: active companion repairs may already cover cases.

**Acceptance:** >95% lines for selected CUE, selected Discogs and CUE router, measured refusal
at actual consumer, no changed immutable binding/selection policy, no duplicate hunt regression.
If a legacy tail needs product clarification, record that blocker rather than pad percentages.

## Delivery dependencies, scope outside Python, and review decision

| Proposed task | Prerequisites | Feeds final verification |
| --- | --- | --- |
| C1 Duplicate refusal (P) | Final implementation decision | Matched coverage, latency and molecule gate |
| C2 Capture/workspace (P; retain R) | Final implementation decision and companion-hunt disposition | Matched coverage, latency and molecule gate |
| C3 Config fault guards (U; retain P) | Final implementation decision | Matched coverage, latency and molecule gate |
| C4 Pipeline/lane honesty (U/P) | Final implementation decision | Matched coverage, latency and molecule gate |
| C5 Path fallback (U; retain real disk) | Final implementation decision | Matched coverage, latency and molecule gate |
| C6 Selected source authority (P/B) | Final implementation decision and companion-hunt disposition | Matched coverage, latency and molecule gate |

C1/C4 are first correctness-oriented backfills; C3/C5 can proceed independently with small local
cost. C2/C6 wait for the bounded companion hunt and dedup of concurrent source repairs. This
table represents proposed task boundaries, not newly filed beads or approved dispatch.
Every task carries the per-case oracle, negative control, target and cost contract above.
Final design may combine only genuinely shared fixture/setup work after runtime measurement;
keep unrelated failure semantics reviewable and avoid a giant all-backfills change.

Scripts, shell harness, SQL/Alembic, Jinja/JavaScript, CSS, images, browser actions and model
artifacts are outside the Python line denominator. Reuse canonical `phaze-5jo4c` for packaged,
real migration/browser/telemetry consumer seams, `phaze-28883` for the 34-graph coarse-model
compatibility/numerical gap, and `phaze-5f1sv` for broader fused-condition policy. Local real
model availability cannot certify model-absent CI; the runtime profile's 12 selected-head
consumer evidence is a separate, explicitly bounded population. Do not substitute static
source assertions for a browser action, Prometheus evaluation or real migration upgrade.
No new broad browser/model matrix is justified by these module percentages alone.

For each implementation candidate: report original tests retained, new tests and exact failure
oracle, proposed mutants with observed killed/survived result, immutable matched line sets,
known skips and consumer limits, and measured incremental targeted/gate latency. A survived
mutant requires stronger assertions or an explicit unsupported-case disposition; it is not
permission to weaken the acceptance. Preserve collection order, separate per-seat PostgreSQL,
migration and Redis exports, one pytest process per database, two local full-suite workers and
seven CI shards until separately measured optimization is approved. No fake speedup from skipped
integration/model/browser consumers. This assessment adds documentation only and ran no proposed
backfill, mutation experiment or new coverage measurement.
