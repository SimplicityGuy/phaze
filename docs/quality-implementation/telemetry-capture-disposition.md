# Telemetry capture: retain independent real producers

Task **phaze-axfxb (R3)** records the bounded preflight disposition for implementation epic
**phaze-75qpj**, approved assessment PR #716, source
`75e586ef9d1d412dfcf8ecb34818ecdda2576e70`.

**Disposition: exclude producer-output sharing and PCM-fixture changes from this adoption.**
Keep the existing real analysis calls, function-scoped SDK providers, test nodes, assertions,
coverage contexts, exporter failures and cleanup. No runtime or accepted-bead velocity saving is
claimed. This is the optional task's explicit exclusion path, not a completed sharing experiment.

## Readiness and source scope

This leaf was provisioned and claimed at implementation parent
`4910c157ddb4134bf755a46f2861c1f9023c1418`. Beadhive warned that refreshing the parent from
main conflicts and provisioned from the stale base. The recurring orphan recovery and waiting-job
heartbeat repairs are merged on main, but their commits `63b27206` and `3d5ebbad` are **not
ancestors of this leaf**. Assignment therefore did not deliver the task's intended worker baseline.
No older worker implementation was edited and no upstream merge was performed in the container.
The dispatcher must reconcile that baseline through the approved lifecycle before final verification.

The operator chose the next **20 completed beads**, grouped by actual change type, as the
observation cohort. A complete accepted-bead comparison is not available here. M0 documents
unknown historical work-start boundaries; SB excludes unsupported immutable evidence generations;
SC's reviewed disposition is not integrated into this parent. Those facts do not authorize an
adoption or a speed claim. This report supplies a source-only exclusion without executing the denied
pilot on an older worker baseline. It does not certify SC or reconcile its integration blocker.

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
