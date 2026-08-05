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
  `TensorflowPredict*` graphs held co-resident in `_classifier_cache`.
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
