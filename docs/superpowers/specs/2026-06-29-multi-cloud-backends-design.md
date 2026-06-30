# Multi-Cloud Backends — Design (proposed v8.0)

**Status:** Approved shape, deferred start. This is the **next** milestone (v8.0, phases 63+).
v7.0 (UI Redesign) remains the active milestone; this doc is captured now and promoted via
`/gsd:new-milestone` once v7.0 ships. Specific implementation details are intentionally left
to plan-time.

**Date:** 2026-06-29
**Author:** brainstormed with Robert
**Supersedes:** the single `cloud_target` selector introduced in v6.0 (Phase 55).

---

## 1. Motivation

Today phaze can route a long, locally-timing-out audio file to exactly **one** remote
execution target, chosen by a single 3-value selector:

```python
# src/phaze/config.py:406
cloud_target: Literal["local", "a1", "k8s"]
```

The duration router (`_route_discovered_by_duration`, `routers/pipeline.py:257`) holds long
files in `AWAITING_CLOUD`, and a single drain loop (`stage_cloud_window`,
`tasks/release_awaiting_cloud.py:110-193`) dispatches them via a hardcoded `if/elif` on
`cloud_target`. Only one target is ever active, and the in-flight window is a single global
counter (`cloud_max_in_flight`, default 2).

We want to run analysis on **local + Kueue (1+ clusters) + cloud-compute (1+ providers)
simultaneously**, draining the long-file backlog across every enabled backend, preferring
free/owned capacity and spilling to paid only under load.

## 2. Reframed model

Three **categories** of execution target, each able to run 1+ instances concurrently:

| Category | What it is | Multiplicity | Today |
|----------|------------|--------------|-------|
| **local** | homelab fileserver/worker | 1 | exists |
| **kueue** | generic batch-on-Kubernetes; location-agnostic (homelab/OKE/GKE/EKS — phaze doesn't care where the cluster lives) | **1+ clusters** | 1 cluster, flat `kube_*` config |
| **cloud-compute** | OCI / AWS / GCP / … VMs running the `kind=compute` agent | **1+ providers** | 1 (OCI A1), but only one selectable at a time |

**Key realization from the codebase map:** the cloud-compute path is *already*
provider-agnostic. Today's `a1` target has **zero** OCI-specific code — "A1/arm64" is purely a
Docker image + deploy choice. A `kind=compute` agent registers, drains a per-agent SAQ queue,
receives files via rsync-over-Tailscale, analyzes, and PUTs results. An AWS or GCP box running
the same agent image is already just another `kind=compute` agent. The gaps are **multiplicity**
(only one compute agent is ever selected — `select_active_agent(kind='compute')`) and
**identity/tier** (no notion of cost rank or per-backend cap).

Kueue is treated as its own category, **separate** from the cloud abstraction: a cluster+Kueue
is generic and it does not matter where it runs.

## 3. Locked decisions

1. **Next milestone, not now.** v7.0 stays active; this is v8.0.
2. **Generalize the existing three categories** into a pluggable, simultaneously-orchestrated
   model. **No new concrete providers** this milestone (the seam makes them trivial follow-ons).
3. **Static routing, no provisioning.** Operators deploy backends themselves; phaze routes to
   whatever is online. No cloud SDKs, no instance lifecycle, no teardown.
4. **Cost-tiering = operator-assigned priority ranks + per-backend caps** — not an automated
   dollar-cost model. Local is *free but ranked last* (slow for long files), which proves rank
   ≠ pure dollar cost. The operator encodes "free-and-fast first, paid next, slow-local last."
5. **Unified config registry (Option A).** One declarative `backends:` list on `ControlSettings`
   is the single source of truth for what exists, each entry's kind, rank, and cap.
6. **Local is the last-resort tier** — lowest rank + small cap, reached only when all
   higher-ranked backends are full or offline.
7. **Shared S3 staging** across all Kueue clusters (one bucket; control plane stays the sole S3
   importer, preserving the DIST-01 no-media boundary). Per-cluster kube config; shared bytes.

## 4. Architecture

### 4.1 Backend config registry (replaces `cloud_target`)

A new `backends:` list on `ControlSettings` is the source of truth. Illustrative shape (exact
schema decided at plan-time):

```yaml
backends:
  - id: a1-oci          # OCI A1 — free, fast
    kind: compute
    rank: 10
    cap: 1
    agent_ref: oci-a1-arm64        # binds to a live kind=compute agent row
  - id: kueue-homelab   # your cluster — free
    kind: kueue
    rank: 10
    cap: 4
    kube: { kubeconfig, namespace, localqueue, image, cpu, mem }
  - id: kueue-gke       # paid cluster
    kind: kueue
    rank: 20
    cap: 8
    kube: { ... }
  - id: aws-burst       # paid VM
    kind: compute
    rank: 30
    cap: 2
    agent_ref: aws-c7g
  - id: local           # last resort
    kind: local
    rank: 99
    cap: 1
```

- `kind=compute` entries are *available* only when their referenced `kind=compute` agent is
  heart-beating.
- `kind=kueue` entries are *available* only when their cluster LocalQueue probe passes.
- `kind=local` is always available.
- **Back-compat shim:** `cloud_target=a1` (or `k8s`) synthesizes a one-entry `backends` list so
  existing single-target deploys keep working through the transition. (Decision: ship the shim.)

### 4.2 The `Backend` protocol (the seam that removes the `if/elif`)

One internal protocol the three kinds implement, consolidating the duplicated `cloud_target`
switch currently spread across `stage_cloud_window`, three config validators, and the staging
modules:

```python
class Backend(Protocol):
    id: str
    rank: int
    cap: int

    async def is_available(self, ...) -> bool      # agent heartbeat / cluster probe / always (local)
    async def in_flight_count(self, ...) -> int     # from the cloud_job registry, scoped to this backend
    async def dispatch(self, file, ...) -> None      # compute → rsync push; kueue → S3 + kr8s submit; local → process_file
    async def reconcile(self, ...) -> None           # kueue → cron read; compute → existing /pushed + callback path
```

Implementations: `LocalBackend`, `ComputeAgentBackend`, `KueueBackend`. The existing staging
modules (`services/s3_staging.py`, `services/kube_staging.py`, `tasks/push.py`) become the
*bodies* of `dispatch`/`reconcile` rather than branches. **Internal protocol only** — no
external/third-party plugin loading.

### 4.3 Scheduler — tiered drain (replaces `stage_cloud_window`'s fork)

Per tick, under the existing `pg_advisory_xact_lock`:

1. Enumerate enabled backends from the registry; filter to `is_available()`.
2. For each `AWAITING_CLOUD` file, pick the available backend with the **lowest rank** whose
   `in_flight_count() < cap`; call `backend.dispatch(file)`.
3. The global `cloud_max_in_flight` becomes **per-backend `cap`**.

Local (rank 99) is naturally reached only when every cloud/kueue backend is full or offline.
**Optional staleness guard** (deferred decision): only release to local once a file has waited
beyond a threshold, so a momentary backlog blip doesn't dump long files onto slow local. Default
position: keep it simple (rank 99 + cap 1, no staleness logic) unless plan-time says otherwise.

### 4.4 In-flight registry — per-backend

The `cloud_job` sidecar (`models/cloud_job.py`) gains a `backend_id` column so in-flight counts
are per-backend and reconcile knows which backend owns each file. Today the registry is
Kueue-only; generalize it to also record compute-agent pushes, so spillover and recovery are
uniform across all backends. (Additive migration.)

### 4.5 Failure / spillover

A backend going offline, or a job failing mid-flight, returns the file to `AWAITING_CLOUD`; the
next drain tick re-dispatches it to the next eligible backend (not necessarily the one that
failed). Existing `reconcile_cloud_jobs` + the recovery ledger handle the mechanics; both become
`backend_id`-aware.

## 5. What stays untouched (keeps scope tight)

- **Result return** — `put_analysis` keyed by `file_id` (`routers/agent_analysis.py:126`) is
  already backend-agnostic; all backends reconcile through it. No change.
- **Duration gating** — `_route_discovered_by_duration` still decides cloud-eligibility (long →
  `AWAITING_CLOUD`, short → local `process_file`). No change.
- **Agent HTTP surface** (`/api/internal/agent/*`), **shared S3 staging leg**, and **windowed
  analysis** all stay as-is.

## 6. Non-goals (YAGNI)

- No instance provisioning / teardown.
- No automated dollar-cost model or spend-tracking API (ranks are operator-set).
- No new providers' SDKs this milestone.
- No external/third-party plugin loading — the `Backend` protocol is internal.
- No per-cluster S3 buckets.

## 7. Deferred to plan-time

Per agreement ("specific details we sort out when we get to implementing"):

- Exact `backends:` schema and validation (per-entry fail-fast validators, replacing the three
  current per-target validators).
- Whether the staleness guard on local is worth building.
- Concurrency-cap accounting edges (counting `PUSHING/PUSHED` vs `cloud_job` rows uniformly).
- Migration sequencing for `cloud_target` → `backends` (shim lifetime, deprecation).
- Admin/UI surfacing of per-backend lanes (note: v7.0 Phase 58 already introduces local/A1/k8s
  Analyze lane cards — this milestone generalizes those to N lanes; coordinate then).
- Per-backend reconcile cron cadence for compute vs kueue.

## 8. Rough phase shape (indicative — finalized by `/gsd:new-milestone` + `/gsd:plan-phase`)

1. **Backend registry & config model** — `backends:` list, validation, `cloud_target` back-compat
   shim. No behavior change yet (registry read but single dispatch path retained).
2. **`Backend` protocol + three implementations** — refactor `stage_cloud_window`'s `if/elif`
   and the staging modules behind the protocol; `cloud_job.backend_id`; behavior-preserving.
3. **Tiered scheduler** — rank/cap drain loop with per-backend windows; multiple backends live
   simultaneously; spillover/failure re-dispatch.
4. **Multi-Kueue** — N clusters from config sharing one S3 bucket; per-cluster probe/reconcile.
5. **Deployment, config & docs** — `_FILE` secrets per backend, runbook, master revert toggle,
   admin surfacing of N lanes.

(Dependency-strict order; 1 → 2 are behavior-preserving refactors that de-risk 3.)
