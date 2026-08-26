# ADR-0005 — Emit `resources.limits.memory` on analyze Jobs

| | |
| --- | --- |
| **Status** | Accepted — supersedes the requests-only lock |
| **Date** | 2026-08-04 |
| **Decider** | Repository owner (operator decision) |
| **Investigation** | Spike `phaze-esut` — [`docs/spikes/phaze-esut-analysis-memory-profile.md`](../spikes/phaze-esut-analysis-memory-profile.md) |
| **Supersedes** | "Q1 RESOLVED-adopted: requests-only is locked" — the inline decision in `src/phaze/services/kube_staging.py::build_job_manifest` |
| **Implemented by** | Not yet — follow-up B in the spike's §11, to be filed via `/bh:replan` |

______________________________________________________________________

## Context

`build_job_manifest` emits `resources.requests` only, with **no `limits`**, on the analyze Job
it submits to Kueue. The docstring records this as locked:

> `resources.requests` ONLY -- NO `limits` (Kueue's quota accounting reads requests;
> Q1 RESOLVED-adopted: requests-only is locked).

In production the consuming deployment ran `phaze-analyze-*` pods at `req=8Gi lim=` on a 31 GB
k0s burst node. Between 2026-07-29 and 2026-08-04 the node recorded **55 OOM kills** and one
hard crash. Because the pods carry no limit, the kernel never cgroup-OOMs the offending pod: the
kills are `oom-kill:constraint=CONSTRAINT_NONE`, i.e. **global**, and victims are chosen across
the whole node by `oom_score_adj`. Confirmed collateral: `coredns` ×2, `metrics-server` ×2,
`local-path-provisioner` ×2. **phaze's memory behaviour killed cluster infrastructure, not just
phaze's own work.**

Spike `phaze-esut` measured the analyze pipeline end to end. The findings that bear on this
decision:

- Peak RSS is a **duration-independent 8.5–10.5 GiB floor** on the measurement host, created
  almost entirely inside the first `_run_model_sets` call, and dominated by the 34
  `TensorflowPredict*` graphs held co-resident in `_classifier_cache`. *(Superseded macOS
  figure — `phaze-esut` ran on the measurement host of its era, not the Linux deployment target,
  and the pipeline it measured predates the model-major rework, the batch-size change and the
  streaming decode. It is kept here as the historical starting point of the investigation; see
  the 2026-08-19 amendment at the end of this document for the current, Linux-measured figures.)*
- **Every file exceeds the 8Gi request**, including a 3.3-minute one (9.73 GiB measured). A
  12.1-hour file peaked at 9.03 GiB — one of the *cheapest* runs.
- The coefficient of peak on one decoded-signal copy (`sample_rate × channels × duration`) is
  **0.00 ± 0.05**. A pre-registered test of the decoded-copies hypothesis predicted 39.2 GiB and
  measured 9.03 GiB.

## The premise that failed

The lock was adopted on the reasonable premise that **`requests` approximate actual usage**, so
that Kueue's quota arithmetic (3 × 8Gi ≤ 24Gi) would also bound real consumption. The premise is
falsified: the request is below the floor for **every** input, and the residual error observed
in production reaches 3.8×.

It is worth separating two claims that the original lock ran together:

1. **"Kueue's quota accounting reads requests."** *True, and unaffected by this ADR.* It is a
   reason to keep `requests` authoritative and undistorted. It is **not** a reason to omit
   `limits` — a limit is invisible to Kueue's quota accounting and changes no scheduling
   decision.
1. **"Therefore emit requests only."** This does not follow from (1), and its cost is that a
   pod-scoped fault escalates to a node-scoped one.

## Decision

1. **`build_job_manifest` emits `resources.limits.memory`** when a new optional
   `KubeConfig.memory_limit` is set. When it is `None`, **no `limits` key is emitted at all** and
   the manifest is byte-identical to today's — the same backward-compatibility posture already
   used for `models_pvc_name` and `active_deadline_seconds`.
1. **`requests` remains the Kueue quota input and is unchanged in role.** The limit is a kernel
   bound, not a scheduling input.
1. **No CPU limit.** CPU is measurably not the constraint (the spike's §8 shows the workload is
   serialized on single-threaded decode), and a CPU limit would throttle a job that is already
   the slow path.
1. **Reject duration-derived requests, and reject duration-based admission as a memory control.**
   Peak is uncorrelated with duration; sizing on duration would be precisely the wrong variable.
   (A duration gate remains defensible on *exposure-time* and burst-throughput grounds — see the
   spike's §8/§10 — but that is a different decision, not this one.)
1. **The 8Gi request guidance is withdrawn.** Interim operator guidance on a 31 GB node:
   `memory_request: 12Gi`, `memory_limit: 16Gi`, concurrency 1 — to be re-derived from a Linux
   measurement (spike follow-up C) rather than from the macOS floor plus the observed ratchet.

> **Update 2026-08-05 — the Linux measurement ran (`phaze-7i0k`,
> [report](../spikes/phaze-7i0k-linux-memory-measurement.md)) and the interim numbers in point 5
> are superseded: `memory_request: 9Gi`, `memory_limit: 12Gi`, concurrency 2.** The Linux peak is
> 7.99 GiB synthetic and 7.92–7.96 GiB on real production audio — *lower* than the macOS floor
> this ADR reasoned from, and with no ratchet above it. 12Gi over-reserved by 50%; 16Gi sat above
> the 15.27 GiB floor of the pathological OOM population it was meant to backstop. **The decision
> itself is unaffected and, if anything, better supported:** the pathological runs were not
> reproduced under any allocator, thread, concurrency, or audio-content variant, so they are not
> reachable by tuning — which is exactly the argument for a kernel-enforced limit rather than a
> larger request. The `None` code default stays as decided in point 1.

## Consequences

**Intended.** A pod that exceeds its limit is OOMKilled by its own cgroup. The victim is
deterministic and is the offending pod. Cluster infrastructure stops being collateral. The
failure becomes observable in the Job/Workload status instead of arriving as an unexplained node
crash.

**Failure handling already exists and is unchanged.** A cgroup kill of the analysis child
surfaces as a non-zero child exit, which `job_runner` maps to `EXIT_ANALYSIS`, and which the SAQ
worker path maps to `AnalysisSubprocessError` → terminal `reason="crashed"`. Neither path blindly
re-runs a deterministically doomed file. If the *parent* is the victim, the pod fails and
`backoffLimit: 0` makes that immediately terminal.

**QoS is unchanged.** The pods are already Burstable (confirmed by the `kubepods/burstable`
cgroup path in the OOM records). Setting a memory limit without a CPU limit leaves them
Burstable.

**Accepted cost.** A limit set too low converts a would-have-succeeded job into a failure. This
is why the interim limit (16Gi) is set above the interim request (12Gi) rather than equal to it:
the goal is a **backstop against the ratchet**, not a tight bound. Tightening it is a follow-on
decision once the residency fix (spike follow-up A) has landed and been measured.

**What this ADR does not do.** It does not reduce peak usage by a single byte. It changes the
*shape of the failure*, which is strictly better, and it is deliberately the small, backward-
compatible change that ships first. The actual reduction is spike follow-up A — restructuring
`_run_model_sets` to model-major iteration so one TF graph is resident instead of 34.

> **Follow-up A landed 2026-08-05 (`phaze-15sw`).** Measured on the burst node: the Linux
> envelope maximum fell **7.986 → 2.482 GiB (−68.9%)**. This ADR's decision is unaffected — a
> limit is still the right backstop against the unexplained 2–4× population of
> `phaze-7i0k` §6d, which the restructure does not explain or reach. But the **numbers** in it
> are now sized against a peak that no longer exists: the interim 12Gi/16Gi, and the 9Gi/12Gi
> that superseded them, are both derived from an ~8 GiB working set. Re-deriving them from the
> measured 2.5 GiB is `phaze-7qfd`'s job, not this ADR's.
>
> **And the target moved again on 2026-08-06 (`phaze-0582`).** Setting `TensorflowPredict*`'s
> `batchSize` to 32 (essentia's default is 64; `discogs-effnet-bs64-1` stays at 64 because its
> Placeholder is fixed) takes the same 60-minute saturated-cap shape from **2.4445 → 1.6206 GiB
> (−33.7%)** for **+0.40%** wall, re-measured end to end on the burst node. So whoever picks up
> `phaze-7qfd` should derive from **~1.6 GiB**, not 2.5 — and should do it from a **joint**
> measurement with `phaze-5lop`'s streaming decode rather than by adding the two spikes'
> numbers. `phaze-rc1q` §6 is the standing proof that exactly this kind of arithmetic can be
> wrong by a gigabyte; the two changes point opposite ways and have never run in one process.
>
> **And every one of those numbers is qualified by a core count as of 2026-08-06
> (`phaze-rvcn`).** TensorFlow sizes both of its thread pools from the machine's core count,
> so an analyze process that inherits those defaults has a peak that is a property of the
> *node*, not of the workload: 7.986 / 2.482 / 1.6206 GiB are all figures for a
> **4-physical-core** host (Xeon E3-1271 v3, 8 logical). Measured on that node, restricting
> the process to 2 physical cores moves the unpinned peak 1.3349 -> 1.2936 GiB, and pinning
> `TF_NUM_INTEROP_THREADS=1` alone moves it -15.3% -- the thread pools, not the graph or the
> batch, are what is left coupling peak to hardware. phaze now derives all three thread
> variables from the schedulable physical core count before importing essentia
> (`services/analysis_sizing.py`), which makes the derived peak flat (1.127-1.153 GiB across
> effective core counts 1-4). **This ADR's decision is again unaffected** -- a limit remains
> the right backstop -- but whoever re-derives the numbers must state the core count they hold
> for, and should derive from the *pinned* figure, which is the only one that is a property of
> the code. See `docs/k8s-burst.md`, "Thread sizing is derived, not configured".
>
> **The joint measurement this note asked for exists as of 2026-08-06 (`phaze-5lop`), and it
> ends the chain of moving targets.** Rather than adding the two spikes' numbers, the whole
> shipped pipeline was measured end to end through the real `analyze_file` on the burst node,
> 60-minute file at saturated caps, one variable at a time:
>
> | configuration | wall | **peak** |
> | --- | ---: | ---: |
> | `main` before `phaze-5lop` (batch 32 + derived threads + hoisted extractors) | 3,205.05 s | **1.3999 GiB** |
> | **`main` with the streaming decode (shipped)** | **1,960.37 s** | **1.7383 GiB** |
>
> **`1.7383 GiB` is the number to derive from** — a whole-process `VmHWM`, not a stage figure,
> on a 4-physical-core host, with the thread pools pinned. The caution above was warranted:
> the two changes do point opposite ways, and adding them would have been wrong in both
> directions (`phaze-rc1q`'s own prototype measured **3.584 GiB** for the decode change alone,
> which would have breached a 3Gi request; the shipped implementation is under half that because
> it carries two mitigations the prototype did not). **This ADR's decision is again unaffected**
> — the unexplained 2–4× population in `phaze-7i0k` §6d is neither explained nor reached by a
> decode rewrite, so a limit is still the right backstop, and `3Gi`/`4Gi` still clear the
> measured peak by 1.73× / 2.30×.

______________________________________________________________________

## Amendment 2026-08-19 (`phaze-y6np2`) — follow-up C discharged; point 4 restated as conditional

**What was believed.** Point 5, unedited above, still reads as live guidance: `memory_request:
12Gi`, `memory_limit: 16Gi`, concurrency 1, pending "a Linux measurement (spike follow-up C)".
Decision point 4 states "Peak is uncorrelated with duration" without qualification.

**What was tested.** `phaze-u1n7j`, [report](../spikes/phaze-u1n7j-vox-fix-verification.md), is
spike follow-up C: it re-measured this ADR's `_run_model_sets` pipeline on real Linux hardware —
**`vox`, Debian 13 (trixie), kernel 6.12.100, glibc 2.41, Xeon E3-1271 v3, 4 physical / 8 logical
cores, 31.31 GiB, k0s burst node** — running the deployed job image **`job:2026.8.2`**, on the
same three real corpus files `phaze-b2qs9` used. It is also the verification of the D-09 fix
(`src/phaze/services/analysis.py:844`) that made the current sizing regime possible at all: the
exhaustive chunking this ADR's own §Context reasons about had leaked its streaming network per
chunk, making peak RSS **linear** in duration (`0.7634 + 0.3108 × n_fine_chunks` GiB, R² 0.99959,
breaching the deployed 4Gi limit at ~4 hours — measured by `phaze-b2qs9`). With D-09's fix
(disconnect the network before dropping it, then `gc.collect()`), peak RSS on the same files,
same node, same image:

| file | duration | **peak RSS (GiB)** | headroom under the deployed 4Gi limit |
| --- | ---: | ---: | ---: |
| `<set-01>` | 1:00 | 1.4985 | 62.5% |
| `<set-04>` | 4:00 | 1.6500 | 58.8% |
| `<set-07>` — longest in corpus | 12:04 | 1.6725 | **58.2%** (41.8% used) |

The spread across the 12.1× duration span is +11.6% overall and **+1.4%** between the two bands
whose coarse chunks are both full (`<set-04>` → `<set-07>`, 3.1× the fine-chunk count) — a
residual slope of **0.0013 GiB per fine chunk** against the defect's **0.3108**, i.e. **99.6% of
the slope removed**. Full detail, including the pre-registered abort-gate check and the mechanism
confirmation under glibc, is in the spike.

**Point 5's figures, tracked to their disposition:**

| pair (request / limit / concurrency) | source | status |
| --- | --- | --- |
| `12Gi` / `16Gi` / 1 *(point 5, as written above)* | this ADR, interim, from `phaze-esut`'s macOS 8.5–10.5 GiB floor plus an assumed allocator ratchet | **withdrawn** — `phaze-7i0k` refuted the ratchet (Linux measured *lower* than macOS, not higher) |
| `9Gi` / `12Gi` / 2 *(2026-08-05 update, above)* | `phaze-7i0k`, against an ~8.0 GiB window-major floor | **withdrawn** — superseded by `phaze-15sw`'s model-major rework (34 graphs → 1 resident at a time), a code shape that no longer exists |
| `3Gi` / `4Gi` *(the 2026-08-06 "number to derive from" update, above; deployed today per `docs/k8s-burst.md`)* | `phaze-5lop`'s 1.7383 GiB joint measurement | **still current, and now better supported** — see below |

**What this amendment decides.** Spike follow-up C is **discharged**. It does **not** license a
new `memory_request`/`memory_limit` value — the spike says so explicitly (§5): the headroom table
above is measured solo on an idle node, not under the concurrent admission Kueue actually
schedules, so it is not a basis for tightening (or loosening) the limit. What it establishes is
that the already-deployed `3Gi`/`4Gi` pair (`docs/k8s-burst.md`, ["Superseded
values"](../k8s-burst.md#superseded-values)) is **even better supported** than the 1.73×/2.30×
margin it was carrying against the pre-D-09 1.7383 GiB figure: the longest file in the corpus now
uses **41.8%** of the deployed 4Gi limit, against **2.57×** OVER that same limit before the D-09
fix (`phaze-b2qs9`, `<set-07>`, 10.2768 GiB). **No `memory_request`/`memory_limit` change is made
by this amendment.** Point 5's "concurrency 1" is likewise withdrawn as stale for the same reason
the request/limit figures are: concurrency is tracked independently of this ADR as Kueue's `cap`
(currently `4`, `docs/k8s-burst.md`, ["How many files can run at
once?"](../k8s-burst.md#how-many-files-can-run-at-once)), and is unaffected by anything here.

**Decision point 4 is restated as conditional.** "Peak is uncorrelated with duration" is true
again of the shipped pipeline, but **only given the D-09 invariant holds** — a chunk's streaming
network must be *disconnected*, not merely dropped, with `gc.collect()` run alongside it
(`src/phaze/services/analysis.py:844`). Break D-09 and the 0.3108 GiB-per-fine-chunk slope
returns; that is precisely the mechanism by which the original OOM shipped, on this same
"duration-independent" premise stated without its precondition. Point 4 should be read as: *peak
is uncorrelated with duration, given D-09 holds* — the precondition travels with the conclusion
from this amendment forward.

**Operator note (not applied by this amendment).** The deployed `memory_request`/`memory_limit`
are operator config and are not tracked in this repository, so no deployed value is changed here.
Given the restored headroom (41.8% of the 4Gi limit at the corpus's longest file, versus the
1.73×/2.30× margin the same pair was previously carrying), the currently-deployed `3Gi`/`4Gi`
pair remains adequately — if not generously — sized, and no *reduction* in `cap` or the memory
tier is indicated. Whether the *operator* wants to use the extra headroom to raise Kueue's
concurrency `cap` above its current `4` is a capacity-planning choice this ADR does not make;
raising it is unrelated to this amendment's memory-safety finding and would need its own
concurrency measurement (in the lineage of `phaze-3j67` / `phaze-8r6t4`), not a memory one.
