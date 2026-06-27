# Feature Research

**Domain:** Kueue-scheduled Kubernetes batch-Job offload (v6.0 Kubernetes Burst Analysis)
**Researched:** 2026-06-26
**Confidence:** HIGH (Kueue concepts/lifecycle verified against Context7 `/kubernetes-sigs/kueue` + official docs at kueue.sigs.k8s.io)

> Scope note: This file covers ONLY the new Kueue/Kubernetes feature surface. The v5.0
> cloud-burst machinery (duration routing, compute-agent callback over `/api/internal/agent/*`,
> reconciliation by `file_id`, `cloud_burst_enabled` toggle) already exists and is reused as-is —
> it is treated here as a dependency, not re-researched.

---

## How Kueue Actually Works (the model phaze must rely on)

A one-paragraph mental model, then the feature classification.

Kueue does **not** manipulate Jobs directly. You submit a normal `batch/v1` Job carrying the label
`kueue.x-k8s.io/queue-name: <LocalQueue>`. A Kueue admission webhook intercepts it and, for any Job
that has that label, **keeps it suspended** (`.spec.suspend=true`) and creates a paired `Workload`
object representing the Job's resource ask. Kueue's scheduler tries to fit that Workload's quota into
a `ClusterQueue` (via the `LocalQueue` → `ClusterQueue` mapping). When quota is available it **admits**
the Workload and flips the Job's `suspend` to `false`, letting the pod start. When the pod finishes,
Kueue syncs the Job's terminal status back onto the Workload as a `Finished` condition. **Kueue carries
no result payload** — it is purely an admission/quota gate. The analysis result must travel out-of-band,
which is exactly phaze's plan (the pod POSTs to the internal API).

**Object ownership (who creates what):**

| Object | Scope | Who creates it | phaze's relationship |
|--------|-------|----------------|----------------------|
| `ResourceFlavor` | Cluster | Cluster admin (runbook) | References indirectly via ClusterQueue; never touched |
| `ClusterQueue` | Cluster | Cluster admin (runbook) | Holds the CPU/memory quota; never touched by phaze |
| `LocalQueue` | Namespace | Cluster admin (runbook) | phaze references it **by name** (config string) on every Job |
| `Job` (suspended, labeled) | Namespace | **phaze, per long file** | The one object phaze writes |
| `Workload` | Namespace | **Kueue** (auto, per Job) | The object phaze **watches** for admission/finish |

This split is load-bearing for the roadmap: phaze creates exactly one kind of object (the Job) and
reads exactly one kind (the Workload + the Job's own status). The three Kueue admin objects are
documented cluster-admin/runbook setup, never created or mutated by phaze.

---

## Feature Landscape

### Table Stakes (Must exist for the offload to function at all)

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Submit a **suspended** `batch/v1` Job with `kueue.x-k8s.io/queue-name: <LocalQueue>` label | This label is the only thing that makes Kueue manage the Job; submitting with `suspend: true` guarantees no pod starts before Kueue gates it | MEDIUM | Kueue's webhook would suspend it anyway, but submitting suspended avoids a race where the pod briefly starts. KEP-973 example shows `suspend: true` + queue-name label together. Pod template: `restartPolicy: Never`. |
| Locate the **Workload** Kueue created for the Job | Admission/quota state lives on the Workload, not the Job | MEDIUM | Workload is owned by the Job (owner ref) and discoverable by Job UID / label selector. Name pattern `<job-name>-<hash>`. Don't hard-code the name; resolve via owner reference or a `kueue.x-k8s.io/job-uid`-style selector. |
| Detect **admission** programmatically | Need to know the pod actually started (vs. still queued) | LOW | Admitted Workload has BOTH `type: QuotaReserved` and `type: Admitted` set to `True`. Before that, `QuotaReserved=False, reason=Pending` ("workload didn't fit"). |
| Detect **success** | Drives "this file is analyzed" | LOW | Source of truth is the Job's own terminal state: `status.succeeded >= 1` / Job `Complete` condition. Kueue mirrors this into the Workload `Finished` condition (reason `JobFinished`). Watch either; the Job status is the most direct. |
| Detect **failure** | Drives retry/fallback | MEDIUM | Job `Failed` condition / `status.failed` past `backoffLimit`. Kueue also surfaces a terminal `Finished`. Distinguish *analysis failed* (Job Failed) from *evicted/deactivated* (see below) — they need different responses. |
| Out-of-band **result retrieval** via pod → internal API | Kueue returns no payload; confirmed it is admission-only | LOW (reuses v5.0) | Pod POSTs the analysis result to `/api/internal/agent/*` as a registered compute agent, reconciled by `file_id`. This is the actual result channel; the Workload/Job watch only tells phaze *when* and *whether* it ran. |
| Set `ttlSecondsAfterFinished` on the Job | Ephemeral Jobs/Workloads must self-clean or they pile up in the cluster | LOW | Kubernetes-native TTL-after-finished controller deletes the Job (and its Workload) after completion. **Ordering hazard:** TTL must be long enough that phaze reads the terminal status before GC, OR phaze deletes the Job itself after reconciling. |
| Set `backoffLimit` low (e.g. 0–1) + `restartPolicy: Never` | One long, expensive analysis per file — don't let the pod silently retry many times | LOW | Control-plane owns retry/fallback policy (re-route to A1 or local), not the Job's backoff loop. |
| **Tolerate stalled admission** (quota exhausted) as a normal state, not a failure | Conservative long-files workload will frequently sit pending behind quota | MEDIUM | Workload stays `QuotaReserved=False, reason=Pending` indefinitely until quota frees. phaze must not time this out as a failure prematurely; it is "queued," not "broken." |
| Idempotent submission + idempotent callback | Watches drop events; jobs may be resubmitted; callbacks may double-fire | MEDIUM | Reuses v5.0 reconcile-by-`file_id` + ledger pattern. Guard against "Job already exists" and duplicate result POSTs. |
| Control-plane **orphan/timeout reconcile** | Watch streams disconnect; events get missed | MEDIUM | Need a periodic reconcile that re-reads Workload/Job status for in-flight files, not pure event-driven watch. Pairs with the TTL ordering hazard above. |

### Differentiators (Worth doing, raise robustness of the conservative path)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| `maximumExecutionTimeSeconds` on the Workload as a server-side runaway guard | Long-set analysis is exactly where v4/v5 hit multi-hour timeouts; a cluster-side ceiling auto-deactivates a stuck Workload | LOW | Exceeding it in `Admitted` state auto-deactivates the Workload (sets `.spec.active=false`), surfacing an `Evicted` condition. Mirrors phaze's existing bounded-timeout philosophy from the v4.0.10 windowed-analysis incident. |
| Detect **eviction / deactivation** and re-route to fallback target | Turns a cluster hiccup into "fall back to A1/local" instead of a stuck file | MEDIUM | `Evicted` condition with reason `WorkloadInactive`/`Deactivated` (preemption, `maximumExecutionTimeSeconds`, or PodsReady backoff exhaustion). With a conservative no-preemption single-CQ setup this is rare, but cheap to detect and route. |
| Surface **admission state** (queued-behind-quota vs running vs finished) on the pipeline dashboard | Operator can see "5 long files waiting on cluster quota" instead of an opaque "in progress" | MEDIUM | Maps Workload conditions (`QuotaReserved` pending → "queued", `Admitted` → "running", `Finished` → done) onto the existing D-09 status cards. |
| Watch via the **Workload** (not just the Job) for admission visibility | The Job alone can't tell you "queued vs admitted"; only the Workload exposes quota state | LOW | Lets phaze distinguish "Kueue hasn't admitted yet" from "running slowly," informing whether to wait or fall back. |

### Anti-Features (Do NOT build / explicitly avoid)

| Feature | Why It's Tempting | Why Problematic | Alternative |
|---------|-------------------|-----------------|-------------|
| Create/manage `ResourceFlavor` / `ClusterQueue` / `LocalQueue` from phaze | "Self-contained deploy" | These are cluster-admin, cluster-scoped objects; managing quota from an app needs elevated RBAC and couples phaze to cluster policy. PROJECT explicitly scopes them as runbook setup | phaze references a **configured LocalQueue name**; admin creates RF/CQ/LQ per runbook |
| Configure preemption / priority classes / cohorts / fair sharing | "Make long files jump the queue" | Adds cluster-policy surface phaze shouldn't own; conservative scope is a single CQ with plain FIFO quota | Leave preemption off in the runbook; if a file can't get quota, it waits or falls back |
| Partial admission / elastic jobs (`job-min-parallelism`, `ElasticJobsViaWorkloadSlices`) | "Use whatever capacity is free" | Each Job analyzes **one** file → `parallelism: 1`; partial admission is meaningless at parallelism 1 | Single-pod Job; horizontal scale comes from submitting *more Jobs*, governed by quota |
| Rely on Kueue/Job **requeue + backoff** as the retry mechanism | "Kueue already retries" | Kueue's requeue (PodsReadyTimeout backoff, `requeueState.count` → deactivation) is for admission/pods-ready, not for *analysis failed*; conflates infra retry with app retry | Control-plane owns retry/fallback by `file_id` (re-route to A1/local), as v5.0 already does |
| Pull results from **pod logs** or kube API exec | "Result is right there in the pod" | Brittle, requires log-scraping RBAC, pod GC races the read | Pod POSTs structured result to `/api/internal/agent/*` (already the design) |
| Treat a long-lived **watch** as the only completion signal | "Watches are real-time" | Watch connections drop; missed events strand files | Watch as the fast path + periodic reconcile/poll of in-flight Workloads as the safety net |
| MultiKueue / multi-cluster dispatch | "Burst across many clusters" | Large new surface (MultiKueue controllers, secrets per cluster); far beyond one conservative x64 cluster | Single cluster, single LocalQueue; revisit only if a second cluster ever appears |
| GPU/accelerator resource requests in the Job | "Faster analysis" | Already decided out of scope — essentia is CPU-bound (PROJECT Key Decisions) | Job requests `cpu`/`memory` only |

---

## Feature Dependencies

```
[Submit suspended labeled Job]
    └──requires──> [LocalQueue exists]  (admin/runbook)
                       └──requires──> [ClusterQueue + ResourceFlavor]  (admin/runbook)

[Watch Workload for admission/finish]
    └──requires──> [Job submitted]  (Kueue auto-creates the Workload)

[Detect success/failure]
    └──requires──> [Watch Workload + read Job status]

[Result reconciled into Postgres]
    └──requires──> [Out-of-band pod POST to /api/internal/agent/*]   (reuses v5.0)
    └──correlates-by──> [file_id]                                    (reuses v5.0)

[Job cleanup via ttlSecondsAfterFinished]
    └──conflicts──> [Control-plane reads terminal status]
        (TTL must outlast the read, OR phaze deletes the Job after reconciling)

[Re-route on eviction/timeout]
    └──enhances──> [Detect eviction/deactivation]
    └──reuses──> [v5.0 duration-routing fallback to A1/local]
```

### Dependency Notes

- **Submit Job requires LocalQueue (and behind it CQ + ResourceFlavor):** A Job whose `queue-name`
  points at a nonexistent LocalQueue/ClusterQueue gets `QuotaReserved=False, reason=Inadmissible`
  ("ClusterQueue doesn't exist") and never runs. phaze must validate/surface a misconfigured queue
  name early rather than letting files hang.
- **Cleanup conflicts with status read:** `ttlSecondsAfterFinished` deletes the Job *and* its Workload.
  If TTL is shorter than phaze's reconcile interval, phaze can miss the terminal status. Either set a
  generous TTL (minutes, comfortably > reconcile period) or have phaze delete the Job explicitly after
  it has recorded the outcome. This is the single most important ordering decision in the watch loop.
- **Eviction detection enhances fallback:** Reusing v5.0's routing seam, an evicted/deactivated/timed-out
  Workload should mark the file for re-routing (A1 or local) rather than being stranded.

---

## MVP Definition

### Launch With (v6.0 core — the "happy path + don't-strand-files" set)

- [ ] Submit suspended `batch/v1` Job with `queue-name` label, `restartPolicy: Never`, low `backoffLimit`, `ttlSecondsAfterFinished` — the write seam
- [ ] Resolve and watch the paired Workload (by owner ref / UID), not a guessed name
- [ ] Detect admission (`QuotaReserved=True` + `Admitted=True`), success (Job `Complete` / Workload `Finished` reason `JobFinished`), and failure (Job `Failed`)
- [ ] Out-of-band result POST from pod → `/api/internal/agent/*`, reconciled by `file_id` (reuse v5.0)
- [ ] Tolerate indefinite pending-on-quota without timing it out as a failure
- [ ] Control-plane orphan/timeout reconcile loop (watch fast-path + periodic re-read) with TTL-vs-read ordering handled
- [ ] Idempotent submission + idempotent callback (no double-analysis, no double-record)
- [ ] LocalQueue name + kubeconfig/SA token via `_FILE` config, behind `cloud_burst_enabled`

### Add After Validation (v6.x)

- [ ] `maximumExecutionTimeSeconds` runaway guard — add once typical long-set wall-clock is measured on the cluster
- [ ] Eviction/deactivation detection → automatic re-route to A1/local — add once the happy path is proven
- [ ] Pipeline dashboard admission-state cards (queued-behind-quota vs running vs finished)

### Future Consideration (defer / likely never for this tool)

- [ ] Priority classes for urgent files — only if quota contention becomes real
- [ ] MultiKueue / second cluster — only if a second cluster ever exists
- [ ] Elastic/partial admission — only if a Job ever batches >1 file (not planned)

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Submit suspended labeled Job | HIGH | MEDIUM | P1 |
| Resolve + watch Workload | HIGH | MEDIUM | P1 |
| Detect admission/success/failure from conditions | HIGH | LOW | P1 |
| Out-of-band result POST (reuse v5.0) | HIGH | LOW | P1 |
| Tolerate pending-on-quota | HIGH | MEDIUM | P1 |
| Orphan/timeout reconcile + TTL ordering | HIGH | MEDIUM | P1 |
| Idempotent submit + callback | HIGH | MEDIUM | P1 |
| `ttlSecondsAfterFinished` cleanup | MEDIUM | LOW | P1 |
| `maximumExecutionTimeSeconds` guard | MEDIUM | LOW | P2 |
| Eviction → re-route fallback | MEDIUM | MEDIUM | P2 |
| Admission-state dashboard cards | MEDIUM | MEDIUM | P2 |
| Preemption / priority / partial admission / MultiKueue | LOW | HIGH | P3 (anti) |

## Kueue Behavior Reference (named precisely, for requirements)

| Signal | Where | Meaning phaze acts on |
|--------|-------|-----------------------|
| `QuotaReserved` = `False`, reason `Pending` | Workload `.status.conditions` | Queued — waiting for quota; **not** a failure |
| `QuotaReserved` = `False`, reason `Inadmissible` | Workload | Misconfig (e.g. ClusterQueue doesn't exist) — surface to operator |
| `QuotaReserved` = `True` | Workload | Quota reserved in a ClusterQueue |
| `Admitted` = `True` | Workload | Job unsuspended, pod can run |
| `Finished`, reason `JobFinished` | Workload | Terminal; cross-check Job status for succeeded vs failed |
| Job `Complete` / `status.succeeded >= 1` | Job | Analysis succeeded (most direct success signal) |
| Job `Failed` / `status.failed` past `backoffLimit` | Job | Analysis failed → retry/fallback |
| `Evicted`, reason `WorkloadInactive` (a.k.a. Deactivated) | Workload | Preempted, `maximumExecutionTimeSeconds` exceeded, or PodsReady backoff exhausted → re-route |
| `.status.requeueState.count` / `.requeueAt` | Workload | PodsReadyTimeout backoff bookkeeping; reaching `backoffLimitCount` → `.spec.active=false` (deactivated) |
| `.spec.suspend` on Job | Job | `true` until admitted; flips to `false` on admission |

## Sources

- Kueue — Running Jobs task (queue-name label, suspend handling, LocalQueue/ClusterQueue/ResourceFlavor, partial admission, JobFinished in Workload conditions): https://kueue.sigs.k8s.io/docs/tasks/run/jobs/ — HIGH
- Kueue — Workload concept (Job↔Workload sync, `maximumExecutionTimeSeconds`, `requeueState`, `.spec.active` deactivation): https://kueue.sigs.k8s.io/docs/concepts/workload/ — HIGH
- Context7 `/kubernetes-sigs/kueue` — Troubleshooting Jobs ("admitted Workload has `QuotaReserved` and `Admitted` True"; preemption shows `Evicted`), troubleshooting_provreq (`QuotaReserved` reasons `Pending`/`Inadmissible`), KEP-973 (suspended Job + queue-name + priority label example), KEP-369 job-interface reconcile (Finished → SetWorkloadCondition), KEP-349/KEP-1282 (PodsReadyTimeout requeue → deactivation → `Evicted`/`WorkloadInactive`) — HIGH
- phaze `.planning/PROJECT.md` v6.0 milestone + Key Decisions (CPU-only nodes, object-storage staging, LocalQueue referenced not created, reuse of v5.0 compute-agent callback) — HIGH

---
*Feature research for: Kueue-scheduled Kubernetes batch-Job offload*
*Researched: 2026-06-26*
