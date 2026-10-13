# Telemetry capture: retain independent real producers

Task **phaze-axfxb (R3)** records the bounded preflight disposition for implementation epic
**phaze-75qpj**, approved assessment PR #716, source
`75e586ef9d1d412dfcf8ecb34818ecdda2576e70`.

**Disposition: exclude producer-output sharing and PCM-fixture changes from this adoption.**
Keep the existing real analysis calls, function-scoped SDK providers, test nodes, assertions,
coverage contexts, exporter failures and cleanup. No runtime or accepted-bead velocity saving is
claimed. This is the optional task's explicit exclusion path, not a completed sharing experiment.

## Readiness and source scope

The original preflight leaf was provisioned and claimed at implementation parent
`4910c157ddb4134bf755a46f2861c1f9023c1418`. Beadhive warned that refreshing the parent from
main conflicts and provisioned from the stale base. The recurring orphan recovery and waiting-job
heartbeat repairs were merged on main, but their commits `63b27206` and `3d5ebbad` were **not
ancestors of that original preflight source**. Assignment therefore did not deliver the task's
intended worker baseline at that time. No older worker implementation was edited and no upstream
merge was performed during that preflight. Its baseline required reconciliation through the approved
lifecycle before final verification.

**Operator decision, 2026-10-11.** Question as put: *"Before the velocity pilots run, which
observation cohort should they use? Repairs and coverage backfills can proceed without this
choice."* Answer as given (selected option label, verbatim): *"Next 20 completed beads, grouped
by change type"*. The reply arrived at 19:03:57 UTC, before the pilots (cohort cutoff
**2026-10-11T19:04:16Z**). Durable record: the recovered-decision comment on bead `phaze-x45ht`
and its follow-up comment, which records the offered options; the
[PD manifest](pilot-disposition-manifest.md#observation-cohort-and-outcome-authority) gives the
recovery. Implementation molecule `phaze-75qpj` does not hold the decision: its description says
only that the cohort still required operator selection. The decision covers cohort membership
only. A complete accepted-bead comparison is not available here. M0 documents
unknown historical work-start boundaries; SB excludes unsupported immutable evidence generations;
SC's reviewed disposition was not integrated into the original preflight parent. Those preflight
facts did not authorize an adoption or a speed claim. This report supplied a source-only exclusion
without executing the denied pilot on that older worker baseline; the preflight did not certify SC
or reconcile its integration blocker.

The supported resume and README-conflict rebase subsequently reconciled this leaf onto integration
parent `48bd645d47cc29d1661cb5fb8301b2bab2729f68`, as recorded by source
`c9eb55e3cc9243fa3e97ba5fb07833b76dd1493b` before this status correction. Ancestry checks on that
source confirm worker repairs `63b27206` and `3d5ebbad` and SC disposition commit `abf429b9` are
integrated; the [SC disposition](selector-equivalence-disposition.md) is present. This parent also
retains the main refresh `f5ae72d0` and its main parent `75d9e1bf`. Latest-main source
`4997133239ed6c8596178254bcdbe8ca8e62b635` is **not an ancestor** of this reconciled leaf: later
main changes are not claimed here. The original preflight observations above remain historical
evidence, and the explicit sharing exclusion remains unchanged. Integration establishes no pilot
execution, adoption, runtime benefit, cohort outcome, real-model proof or final G0 coverage result.
A fresh independent review and authoritative exact-source provider verdict remain required for this
corrected source; final G0 verification of the assembled mandatory full population remains pending.

## Existing consumer contract inspected

The module [test_analysis_instrumentation.py](../../tests/shared/telemetry/test_analysis_instrumentation.py)
contains **8 existing nodes**. Its synthetic input is **200 seconds at 8000 Hz**, with fine windows
of **30 seconds** and coarse windows of **60 seconds**. The first six nodes each call the real
`analyze_file` with unavailable model weights, exercising real decoding, fine analysis and coarse
failure isolation. The seventh has a deliberately invalid WAV. The eighth independently exercises
real selected models when its existing environment is available.

| Existing node | Live assertion retained |
| --- | --- |
| `test_the_coarse_tier_is_broken_down_per_chunk` | Real spans, both tier labels, decode and derive observations and chunk totals |
| `test_fine_windows_are_counted_and_measured` | SDK counts agree with the independently returned fine-window result |
| `test_the_run_and_the_audio_it_consumed_are_recorded` | Exactly one run and the real input-duration observation |
| `test_peak_rss_is_recorded_at_every_chunk_boundary` | Nonempty positive RSS observations at multiple chunk boundaries |
| `test_no_analysis_metric_carries_an_identifier` | Real emitted metric labels exclude identifiers and input paths |
| `test_the_file_identity_IS_on_the_span` | This run's file identity and chunk-index placement |
| `test_a_failing_run_is_still_measured` | Actual analysis exception still emits an error outcome |
| `test_analysis_with_real_models` | Actual selected model features, labels, inference/build/release/sweep observations |

The [SDK fixture](../../tests/shared/telemetry/conftest.py) proves its binding with disposable real
providers before installing each node's own real `InMemoryMetricReader` and span exporter. It
explicitly binds Phaze instruments and tracing, then shuts down both providers and resets state in
`finally`. Its comments document prior provider/proxy caching failures and cumulative-reader
contamination. Replaying another node's captured spans into these consumers would cease to prove
fresh emission into their providers. Snapshot copies alone do not restore that proof or establish
correct independent coverage contexts.

Immutable input bytes could theoretically be shared while every consumer still runs analysis into
its fresh sink. That narrower possibility is also excluded from this adoption: no paired input
preparation measurements, poisoning controls or cleanup/context equivalence experiment was run.
The current input helper already shares the implementation of synthetic WAV generation; that is
not shared PCM storage or measured cost removal. No production resource caps, window population,
fixture deadlines, skipped environments or failure cases are changed.

## Evidence limits and downstream action

The assessment measured this module at **384.66611037694383 seconds** and its real-model call at
**134.77467108296696 seconds**. Those are retained historical values, not measurements of this
leaf or savings available from WAV preparation. The accepted model scope remains **12 selected
heads**, not the deferred **34-graph** proof. No weights were downloaded, no real-model run was
performed for this preflight and no new skip was introduced.

Fresh-provider replacement, stale-span poisoning, consumer-copy poisoning, exception/cancellation
restoration, exporter/thread leakage and context-map equivalence for a proposed shared producer
remain **unrun**. No negative-control result or three-sample timing experiment is reported because
there is no proposed executable sharing implementation to validate. Existing broken/slow exporter
and bounded-shutdown tests remain unchanged. Documentation checks and hooks, independent review
and the exact-tree authoritative gate are separate lifecycle evidence; this report does not
predeclare those outcomes.

PD must record R3 as **excluded, with no runtime or velocity claim**. R4 may perform its own
bounded static preflight using the unchanged telemetry workload and current same-run weights;
this exclusion is not permission to drop these consumers or bypass R4's measurement prerequisites.
G0 still verifies the actual assembled canonical population, repairs, contexts and coverage floors.
A0 must retain the missing matched runtime/cohort evidence and decide from observed outcomes.
