# Phase 56: Deployment, runbook, config & docs - Research

**Researched:** 2026-06-28
**Domain:** Kubernetes/Kueue operator runbook (manifests + namespaced RBAC), live LocalQueue startup probe (kr8s), ephemeral-identity Agents-UI invariant, config/deployment docs
**Confidence:** HIGH (code paths read first-hand; Kueue schemas verified against official docs + Context7; the v1beta1↔v1beta2 transition is the one MEDIUM-risk item, flagged below)

## Summary

Phase 56 is an **ops-only phase**: almost all config code already shipped (Phases 53/54/55). The
single toggle `cloud_target` exists; the three per-target fail-fast validators
(`_enforce_s3_config_when_k8s`, `_enforce_kube_config_when_k8s`,
`_enforce_compute_scratch_dir_when_a1`) exist; every `_FILE`-secret field exists; transport-agnosticism
is true by construction (every kr8s call reaches an operator-provided `kube_api_url` over whatever mesh
the operator runs). So KDEPLOY-02/03/05 are **documentation**, and the only **net-new code** is the
KDEPLOY-04 pair: a startup LocalQueue-reachability probe and the Agents-UI ephemeral-identity treatment.

The runbook (KDEPLOY-01) ships copy-paste-ready Kueue manifests for objects phaze does NOT create —
**CPU-only ResourceFlavor**, **single-CQ no-preemption ClusterQueue** (CPU+memory `nominalQuota`),
**LocalQueue** — plus a **namespaced least-privilege RBAC** Role/ServiceAccount/RoleBinding and the
bearer-token **Secret**. The one verification subtlety: Kueue introduced **`v1beta2`** and **deprecated
`v1beta1`** (the version phaze's `kube_workload_api_version` currently defaults to). The runbook
manifests MUST match `kube_workload_api_version`, and the runbook must tell the operator to keep the two
in lockstep with their installed Kueue.

The LocalQueue probe is a near-exact clone of the existing `kube_staging.get_job` GET idiom
(`new_class(...)` + construct-by-name + `refresh()`), run once at `controller.py:startup`, non-fatal
(warn + surface). The one real design decision is **cross-process flag plumbing**: the probe runs in the
controller-worker process but the dashboard renders in the api process, so the flag must live in shared
storage. **Redis (`app.state.redis`) is the recommended surface** — it already backs the dashboard's
degrade-safe counter reads.

**Primary recommendation:** Treat the runbook as a faithful copy of the v5.0 `cloud-burst.md` +
`51-HOMELAB-CHANGE-PROMPT.md` shape, swapping OCI/Tailscale/PG-role content for Kueue/RBAC/Secret
content; add `kube_staging.get_local_queue()` mirroring `get_job()`; surface the unreachable flag via a
Redis key written at controller startup and read degrade-safe by a new
`get_localqueue_unreachable()` dashboard service; ship the ephemeral-lane note + rely on the
**already-structural** DEAD-suppression (the one-shot pod never heartbeats).

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

- **D-01: Copy-paste-ready YAML for every operator-owned object.** Complete, apply-ready manifests +
  `kubectl apply` steps for: CPU-only **ResourceFlavor**, single-CQ no-preemption **ClusterQueue**
  quota, **LocalQueue**, **namespaced least-privilege RBAC** (Role verbs `create/get/delete` on `jobs`
  (batch) + `get/watch/list` on `workloads.kueue.x-k8s.io`, one namespace; ServiceAccount; RoleBinding),
  and the cluster **Secret** carrying the compute-agent bearer token. Operator edits names/quota/
  namespace and applies. Mirrors v5.0 51 D-10/D-11 (phaze = authoritative spec, operator applies).
- **D-02: Cluster setup also delivered as a ready-to-paste homelab change-prompt.** Per v5.0 D-09/D-10:
  phaze ships the runbook/manifests as source-of-truth SPEC AND emits a ready-to-paste homelab repo
  change-prompt (deploy ordering via `datum@nox`/`datum@lux`, where manifests get applied, secret
  provisioning). Workspace boundary holds: **phaze = spec, homelab = live infra**. No live `kubectl`/
  cluster mutation authored in the phaze repo.
- **D-03: One new `docs/k8s-burst.md` holds the whole K8s feature.** Mirrors v5.0 `docs/cloud-burst.md`
  (51 D-13): runbook (D-01 manifests), homelab change-prompt (D-02), deploy ordering, transport-agnostic
  endpoint notes (Tailscale **or** WireGuard, KDEPLOY-03), smoke test. Keeps each target self-contained;
  do NOT fold K8s into `cloud-burst.md` (already ~23KB, A1-specific) or `deployment.md`.
- **D-04: Config knobs documented in `docs/configuration.md`; `deployment.md` carries the toggle + a
  pointer.** K8s/S3 knob table (cloud_target, kube_api_url, kube_namespace, kube_local_queue,
  kube_workload_api_version, s3_* fields, presign/lifecycle/part-size/max-attempts) lands in
  `configuration.md` (sourced from existing `Field(...)` descriptions), flagging `_FILE`-secret fields.
  `docs/deployment.md` gets the single-`cloud_target` revert section (KDEPLOY-05 names deployment.md
  literally) + a pointer to `docs/k8s-burst.md`. `docs/README.md` indexes the new doc.
- **D-05: Warn + surface on the dashboard; do NOT crash the app.** When `cloud_target=="k8s"`, the
  startup probe logs a clear WARNING and surfaces "K8s LocalQueue unreachable" on the pipeline dashboard
  (reuse the Phase 54 Inadmissible surface — `cloud_job.inadmissible` flag + `inadmissible_card.html`
  pattern, 54 D-06), but lets the control plane start. A live kube-API probe — a DIFFERENT failure class
  from the three config fail-fast validators (which stay fatal).
- **D-06: Probe runs once at the controller SAQ worker's `startup` hook** (`tasks/controller.py:startup`,
  where the Phase 54 kr8s client + reconcile cron already live). GETs the configured LocalQueue object
  (`kube_local_queue` in `kube_namespace`) via the existing kr8s client. The api lifespan is rejected
  (it doesn't submit Jobs and may not hold kube creds). Surface state via a flag the dashboard reads.
- **D-07: Minimal v6.0 treatment — no perpetual-DEAD pill + an informational note.** Ensure no k8s
  compute-agent registers as a heartbeating `Agent` row (so `classify()` (`agent_liveness.py:68`) never
  renders a perpetually-DEAD/`dead` pill for the k8s lane), and add a static informational note on the
  Agents page explaining the k8s lane is **ephemeral / Job-based**, with live liveness visible via
  in-flight Kueue workloads on the pipeline dashboard. Full synthetic "k8s burst" card is **deferred to
  v7.0 RECORD-03**.

### Claude's Discretion

- Exact RBAC verb/resource list refinement against the live kr8s submit/reconcile call graph (the Job
  create/get/delete + Workload get/watch/list set is the floor; planner verifies nothing else is
  touched).
- Smoke test as a doc checklist vs a scripted check (v5.0 left this to discretion).
- The kr8s mechanism for the LocalQueue GET and exact dashboard flag/field plumbing (mirror the Phase 54
  `inadmissible` flag + reconcile pattern rather than inventing a new surface).
- Exact wording/structure of the homelab change-prompt, within the D-02 spec content.
- Precise `deployment.md` vs `k8s-burst.md` split wording, as long as deployment.md literally documents
  the revert toggle + the cluster/bucket/secret setup is reachable from it (criterion 5).
- Whether transport-agnostic (KDEPLOY-03) gets its own doc subsection or is folded into the
  endpoint-config table — doc structure only; no code.

### Deferred Ideas (OUT OF SCOPE)

- **Full ephemeral "k8s burst" Agents card** with liveness derived from in-flight Kueue workloads —
  v7.0 RECORD-03. v6.0 ships the minimal note + DEAD-suppression only (D-07).
- **KDEPLOY-06** — ConfigMap-mounted internal CA so CA rotation doesn't require a Job-image rebuild
  (v6.0 bakes the CA into the image). Deferred.
- **KSUBMIT-07/08/09, KROUTE-05 cost/throughput routing** — future requirements, not this phase.
- phaze creating Kueue admin objects (cluster-admin/runbook only — phaze references a LocalQueue by
  name).
- Authoring live infra (OpenTofu/cluster provisioning) inside the phaze repo (workspace boundary).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| KDEPLOY-01 | Cluster-admin runbook for the Kueue objects + namespaced RBAC + bearer-token Secret phaze does NOT create | Verified Kueue v1beta1 manifest schemas (ResourceFlavor/ClusterQueue/LocalQueue) + minimal RBAC verb set + the v1beta1↔v1beta2 transition caveat below. Manifest blocks ready in §Runbook Content. |
| KDEPLOY-02 | K8s/S3 pydantic-settings + `_FILE` secrets + fail-fast validators | **Already shipped** — confirmed in `config.py` (3 validators at `:600`/`:621`/`:642`; `_FILE` fields in `SECRET_FILE_FIELDS` `:348`). This phase = documentation only. Knob table sourced from `Field(...)` descriptions. |
| KDEPLOY-03 | Transport-agnostic connectivity (Tailscale or WireGuard, no mesh code) | **True by construction** — `kube_staging._api` builds the kr8s client from operator-provided `kube_api_url`; zero mesh-specific code. Documentation only. |
| KDEPLOY-04 | Startup LocalQueue-reachability probe + ephemeral-identity Agents-UI treatment | **Net-new code.** Probe design (kr8s GET via `get_local_queue()`), cross-process flag (Redis), and the DEAD-suppression structural proof are in §Net-New Code Design. |
| KDEPLOY-05 | Single-toggle revert to all-local/A1 | Toggle (`cloud_target`) **already shipped** (55 D-02). Documentation only — the revert section lands in `deployment.md` (literal criterion). |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Kueue admin objects (ResourceFlavor/ClusterQueue/LocalQueue) | Cluster-admin (operator, out of repo) | phaze docs (authoritative spec) | phaze references a LocalQueue by name; never creates quota objects (KDEPLOY-01, REQUIREMENTS Out-of-Scope) |
| Namespaced RBAC + bearer-token Secret | Cluster-admin (operator) | phaze docs (spec) | Least-privilege grant lives in the cluster; phaze ships the exact Role/SA/RoleBinding YAML |
| Kube Job submit / reconcile / LocalQueue probe | API/Backend (control plane: SAQ controller worker) | — | Kube creds live on the control plane only (DIST-01); `kube_staging.py` is the single home of every kr8s call |
| LocalQueue-unreachable flag storage | Redis cache (cross-process bus) | — | Probe runs in controller process, dashboard renders in api process → flag MUST be in shared storage, not memory |
| LocalQueue-unreachable alert render | Frontend Server (SSR Jinja on api) | — | Reuses `inadmissible_card.html` amber-alert family on the pipeline dashboard |
| Ephemeral-lane note + DEAD-suppression | Frontend Server (SSR) + API auth invariant | — | Static note on Agents page; the one-shot pod never heartbeats, so no Agent row goes DEAD |
| Config validation (k8s/S3 required-when-k8s) | API/Backend (pydantic-settings at construction) | — | Already shipped; fail-fast at startup |

## Standard Stack

No new packages. Every capability reuses libraries already in `pyproject.toml`.

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `kr8s` | `>=0.20.15` (locked at 0.20.15) | All Kubernetes API calls (Job submit/get/delete, Workload list, **LocalQueue GET — new**) | Already the single kube client in `kube_staging.py`; the LocalQueue probe reuses `new_class(...)` + `refresh()` verbatim [VERIFIED: pyproject.toml / uv.lock] |
| `redis.asyncio` | (transitive, in use) | Cross-process LocalQueue-unreachable flag bus | `controller.startup` already builds `ctx["redis"]`; the api already exposes `app.state.redis` (degrade-safe reads) [VERIFIED: controller.py:104, routers/pipeline.py:99-108] |
| `pydantic-settings` | `>=2.13.1` | K8s/S3 config knobs + `_FILE` secrets (already shipped) | Documentation target, no code [VERIFIED: config.py:348,406,534-580] |
| Jinja2 + HTMX | (in use) | LocalQueue alert + ephemeral note | Reuse `inadmissible_card.html` + Agents page shell; zero new tokens/components (per 56-UI-SPEC) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Redis flag for LocalQueue-unreachable | New Postgres singleton/KV table | Heavier (a migration for one boolean); but matches the `_safe_count` Postgres degrade pattern exactly. **Rejected** — no existing KV table (`ls src/phaze/models/` confirms none); Redis is already the cross-process cache bus the dashboard reads. |
| Redis flag | Reuse `cloud_job.inadmissible` | Doesn't fit — at startup there may be zero `cloud_job` rows; the probe is a global boot-state signal, not per-row. |
| kr8s `refresh()`-by-name GET | kr8s `.get(name=...)` classmethod | `refresh()` on a construct-by-name object is the **exact idiom already used by `get_job`** (`kube_staging.py:197-203`) — copy it for consistency and test-seam reuse. |

**Installation:** None — no new dependencies.

## Package Legitimacy Audit

> This phase installs **zero new external packages**. The only library touched (`kr8s`) is already a
> locked dependency, was slop-checked at Phase 54 install time (`kr8s>=0.20.15`, kr8s-org,
> BSD-3-Clause, pure-Python — see the comment at `pyproject.toml:26`), and is reused unchanged.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| (none) | — | — | — | — | — | No new packages — audit N/A |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

## Runbook Content (KDEPLOY-01) — verified Kueue manifests

> All manifests verified against the official Kueue docs and Context7 `/kubernetes-sigs/kueue`.
> **The apiVersion in these manifests MUST match `kube_workload_api_version`** (config default
> `kueue.x-k8s.io/v1beta1`, `config.py:564`). See the v1beta1↔v1beta2 caveat in §State of the Art.

### CPU-only ResourceFlavor
A "CPU-only" flavor is simply a flavor with **no GPU/accelerator constraint** — essentia analysis is
CPU-bound (no GPU/Coral; `project_essentia_cpu_bound_no_accel` memory). An empty-spec flavor matches any
node; add `nodeLabels` only if the operator wants to pin the burst to a specific CPU node pool.
```yaml
# [CITED: kueue.sigs.k8s.io/docs/concepts/cluster_queue]
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: phaze-cpu          # operator edits
# spec: {}  # CPU-only = no accelerator tag; optionally pin to a node pool:
# spec:
#   nodeLabels:
#     node-pool: cpu-burst
```

### Single-CQ no-preemption ClusterQueue (CPU + memory nominalQuota)
```yaml
# [CITED: kueue.sigs.k8s.io/docs/concepts/cluster_queue]
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: phaze-cq           # operator edits
spec:
  namespaceSelector: {}    # single-CQ, cluster-wide; scope via the LocalQueue namespace
  preemption:              # NO preemption (criterion 1)
    reclaimWithinCohort: Never
    withinClusterQueue: Never
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: phaze-cpu
      resources:
      - name: "cpu"
        nominalQuota: "8"   # operator sizes
      - name: "memory"
        nominalQuota: "32Gi"
```
**Note on `pods`:** the official quick-start sometimes includes a `pods` covered resource. It is optional
for a CPU/memory-only quota. Keep the runbook to `cpu`+`memory` (matches phaze's requests-only Job
manifest, `kube_staging.build_job_manifest` — `resources.requests` cpu+memory, NO limits).

### LocalQueue (the object `kube_local_queue` names; the probe GETs it)
```yaml
# [CITED: kueue.sigs.k8s.io/docs/concepts/cluster_queue]
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: phaze-lq           # == PHAZE_KUBE_LOCAL_QUEUE
  namespace: phaze         # == PHAZE_KUBE_NAMESPACE
spec:
  clusterQueue: phaze-cq
```

### Namespaced least-privilege RBAC (ServiceAccount + Role + RoleBinding)
The verb set below is the **floor derived from phaze's actual kr8s call graph** (see §Validation
Architecture). D-01 specifies `get/watch/list` on workloads as the conservative spec; the code today
only `list`s workloads and `get`s/`create`s/`delete`s jobs, **plus a new `get` on localqueues for the
Phase 56 probe**.
```yaml
# [ASSUMED — schema is standard rbac.authorization.k8s.io/v1; verb set derived from code, see Validation Architecture]
apiVersion: v1
kind: ServiceAccount
metadata:
  name: phaze-submitter
  namespace: phaze
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: phaze-submitter
  namespace: phaze
rules:
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["create", "get", "delete"]      # submit_job / get_job / delete_job
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["workloads"]
  verbs: ["get", "watch", "list"]         # get_workload_for (.list); get/watch = conservative spec
- apiGroups: ["kueue.x-k8s.io"]
  resources: ["localqueues"]
  verbs: ["get"]                          # NEW: the Phase 56 startup reachability probe
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: phaze-submitter
  namespace: phaze
subjects:
- kind: ServiceAccount
  name: phaze-submitter
  namespace: phaze
roleRef:
  kind: Role
  name: phaze-submitter
  apiGroup: rbac.authorization.k8s.io
```
**kr8s API discovery note:** `kr8s.asyncio.api()` performs a version/discovery handshake at session
creation (`kube_staging._api`, the `_create_session` comment at `kube_staging.py:99-105`). Discovery
endpoints (`/api`, `/apis`) are normally readable by any authenticated principal; if the operator runs an
unusually locked-down cluster, the runbook should mention that API discovery must succeed. [ASSUMED —
confirm against the operator's cluster]

### Bearer-token Secret (the compute-agent callback token)
The one-shot pod authenticates its `/api/internal/agent/*` callback with `agent_token`
(`PHAZE_AGENT_TOKEN`, `construct_agent_client` in `job_runner.py:174`). This is the **same** bearer
mechanism as fileserver/compute agents — minted via `phaze agents add --kind compute` (creates an Agent
row; see §Net-New Code Design for the D-07 implications). The token rides in a cluster Secret consumed by
the Job pod env (`PHAZE_AGENT_TOKEN_FILE` per the `_FILE` convention).
```yaml
# [ASSUMED — standard core/v1 Secret; token value minted by `phaze agents add --kind compute`]
apiVersion: v1
kind: Secret
metadata:
  name: phaze-agent-token
  namespace: phaze
type: Opaque
stringData:
  PHAZE_AGENT_TOKEN: "phaze_agent_<...>"   # operator pastes the minted token
```

## Net-New Code Design (KDEPLOY-04)

### 1. LocalQueue startup reachability probe

**Where the kr8s call lives:** add `get_local_queue()` to `src/phaze/services/kube_staging.py` (the
single home of every kr8s call). Mirror `get_job()` (`kube_staging.py:197-203`) exactly:
```python
# Pattern derived from kube_staging.get_job + get_workload_for (already in-repo)
async def get_local_queue() -> Any:
    cfg = _kube_config()                 # reuses the kube_api_url/namespace/local_queue gate
    api = await _api(cfg)
    lq_cls = new_class(kind="LocalQueue", version=cfg.kube_workload_api_version, namespaced=True)
    lq = lq_cls({"metadata": {"name": cfg.kube_local_queue, "namespace": cfg.kube_namespace}}, api=api)
    await lq.refresh()                   # 404 -> kr8s.NotFoundError
    return lq
```
- Reuse `cfg.kube_workload_api_version` for the LocalQueue class version (LocalQueue is in the same
  `kueue.x-k8s.io` group as Workload — already proven by `get_workload_for`, `kube_staging.py:229`).
- `refresh()` raises `kr8s.NotFoundError` on a 404 (LocalQueue missing/misnamed → misconfig) and other
  exceptions on transient kube-API/mesh failures. **Both → "unreachable" (warn + surface), never crash.**

**Where the probe runs (D-06):** in `tasks/controller.py:startup`, gated on `cfg.cloud_target == "k8s"`,
wrapped in its OWN broad `try/except` (the boot-resilience discipline already used for the ledger
backfill and recovery at `controller.py:145-157` — a probe failure must NEVER abort controller boot).
On any failure: `logger.warning(...)` + set the cross-process flag; on success: clear it.

**Cross-process flag (the one real decision — D-06 discretion):** the probe runs in the **controller
worker process**; the dashboard renders in the **api process**. An in-memory flag will not cross that
boundary. Use **Redis** — both processes already share it:
- Controller writes a key at startup, e.g. `SET phaze:k8s:localqueue_unreachable 1` (or `DEL`/`0` on
  success), via `ctx["redis"]` (`controller.py:104`).
- The dashboard reads it through a new degrade-safe service `get_localqueue_unreachable(redis) -> bool`
  that mirrors `read_counters`/`get_inadmissible_count` (`routers/pipeline.py:99-108`,
  `services/pipeline.py:821`): any read error or missing `app.state.redis` → `False` (silent).
- Wire it into both dashboard render paths (`routers/pipeline.py:499` first-load and `:575` the 5s OOB
  re-push) exactly where `inadmissible_count` is seeded, and add the new alert section as a sibling OOB
  carrier (per 56-UI-SPEC: lives OUTSIDE `#pipeline-stats`, `hx-swap-oob` on the 5s poll, stable
  `<section id>`, empty/silent when reachable).

**Alert template:** a new partial (or a state on the cloud-state card family) reusing
`inadmissible_card.html` verbatim — amber `role="alert"`, empty when reachable. Copy is locked in
56-UI-SPEC: heading `⚠ K8s LocalQueue unreachable`, body
`K8s LocalQueue unreachable — verify PHAZE_KUBE_LOCAL_QUEUE / cluster connectivity.`

### 2. Ephemeral Agents-UI identity (D-07) — the DEAD-suppression is STRUCTURAL

This is the most important correctness finding. The DEAD pill **cannot** arise for the k8s lane, by
construction, for two independent reasons:

1. **`classify()` only returns `dead` when `last_seen_at` is set AND ≥300s old**
   (`agent_liveness.py:79-86`). An Agent row that has **never** heartbeated has `last_seen_at IS NULL`
   → `classify()` returns **`never`**, NOT `dead` (precedence at `:79`).
2. **`last_seen_at` is set ONLY by `POST /api/internal/agent/heartbeat`** (`agent_heartbeat.py:30-37`).
   `get_authenticated_agent` (the bearer dependency used by every callback, `agent_auth.py:62`) does
   **NOT** touch `last_seen_at`. The heartbeat loop (`_heartbeat_loop`) lives in the long-lived
   **SAQ agent worker** (`tasks/agent_worker.py:67`). The k8s one-shot pod runs **`job_runner.py`**
   (presign → download → analyze → callback → `sys.exit`), which has **no heartbeat loop**.

**Therefore:** the k8s lane's bearer-token Agent row (created by `phaze agents add --kind compute` to
satisfy callback auth) will classify as `never` and can **never** transition to `dead` — nothing ever
heartbeats it. The D-07 "no perpetual-DEAD pill" invariant is satisfied with **zero suppression code**.

**Residual UX question (planner decides — within D-07's "minimal treatment"):** a `never`-classified row
WILL appear in the Agents table for the k8s token (it is a real Agent row, since callback auth requires
one). Options, in order of preference:
- (a) **Accept the `never` pill + add the static note** explaining the k8s lane is ephemeral/Job-based.
  Simplest; matches "minimal v6.0 treatment"; the note tells the operator why a `never` agent exists and
  where to look (pipeline dashboard workloads). **Recommended.**
- (b) Exclude the k8s-lane Agent row from the table by filtering on a marker (e.g. a naming convention or
  a `kind`-based filter). Risk: `kind=compute` is **also** the v5.0 A1 agent, which DOES heartbeat and
  SHOULD show — so a `kind=compute` filter would wrongly hide the A1. Needs a finer marker; more code.
  Defer the finer-grained card to v7.0 RECORD-03.

The static note (copy locked in 56-UI-SPEC) goes on `admin/agents.html` (`agents.html:11`, after the
intro `<p>`): neutral panel, `The Kubernetes burst lane runs as ephemeral, per-file Jobs — it does not
register as a heartbeating agent here. Its live activity is visible as in-flight Kueue workloads on the
pipeline dashboard.`

**Planner verification (assertable invariant):** `classify(agent, now) != "dead"` for any `now` when
`agent.last_seen_at is None`; and the k8s pod path (`job_runner.py`) never imports/calls the heartbeat
loop. (See §Validation Architecture.)

## Architecture Patterns

### Data flow — LocalQueue probe + alert (cross-process)
```
[controller worker process]                         [api process]
 controller.startup (cloud_target==k8s)              GET /pipeline (first load) ─┐
   └─ try: kube_staging.get_local_queue()                                        │
        ├─ ok   -> redis DEL phaze:k8s:localqueue_unreachable                    │
        └─ fail -> logger.warning + redis SET ...=1 ──► [Redis cache] ◄──────────┤
                                                          ▲                       │
 (broad try/except — never aborts boot)                   │  get_localqueue_unreachable(app.state.redis)
                                                          │  (degrade-safe: error/missing -> False)
 GET /pipeline/stats (5s poll, OOB re-push) ──────────────┘
   └─ render localqueue alert section (empty if reachable; amber if unreachable)
```

### Data flow — k8s callback auth (why an Agent row exists but never goes DEAD)
```
one-shot pod (job_runner.py)  --PHAZE_AGENT_TOKEN-->  POST /api/internal/agent/analysis/{file_id}
                                                        └─ get_authenticated_agent (SELECT by token_hash)
                                                           └─ does NOT touch last_seen_at
   (no _heartbeat_loop in job_runner)  ──X──>  POST /heartbeat   (never called)
   => Agent.last_seen_at stays NULL  =>  classify() == "never"  (never "dead")
```

### Doc structure (mirror the v5.0 precedent)
```
docs/
├── k8s-burst.md        # NEW: runbook (manifests) + homelab change-prompt + deploy order + transport notes + smoke test
├── configuration.md    # ADD: K8s/S3 knob table already partly present (§Kube submit/reconcile settings :105) — extend per D-04
├── deployment.md       # ADD: single-cloud_target revert section + pointer to k8s-burst.md (§Cloud-burst compute agent :44)
├── cloud-burst.md      # PRECEDENT ONLY — do NOT add k8s here (already has a transitional k8s section :297-389; D-03 wants k8s-burst.md to own it)
└── README.md           # ADD: k8s-burst.md index row under "Operations"
.planning/phases/56-.../56-HOMELAB-CHANGE-PROMPT.md   # NEW: mirror 51-HOMELAB-CHANGE-PROMPT.md shape
```
**Reusable precedent files:** `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md`
(headings: Context for the homelab agent → numbered provision/apply steps → Deploy ordering → Done-when
checklist) and `36-HOMELAB-CHANGE-PROMPT.md`.

### Anti-Patterns to Avoid
- **In-process flag for the LocalQueue alert.** The probe and the dashboard are different OS processes —
  an in-memory boolean is invisible to the dashboard. Use Redis (or Postgres).
- **Folding the k8s runbook into `cloud-burst.md`.** D-03 wants `docs/k8s-burst.md` to own it; the
  transitional k8s section already in `cloud-burst.md:297-389` should be left as a pointer (planner
  decides whether to trim it).
- **Adding new config validators.** The three per-target validators already exist and stay fatal; the
  LocalQueue probe is a SEPARATE, non-fatal runtime class. Do not "promote" the probe to a validator.
- **A `kind=compute` filter to hide the k8s agent row.** It would also hide the v5.0 A1 (also
  `kind=compute`, and it SHOULD show). Prefer accepting the `never` pill + the note.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| GET a Kueue custom resource | A raw httpx call to the kube API | `kube_staging` + `kr8s` `new_class` + `refresh()` | The auth/session/version-discovery + token-rebuild is already solved in `_api` (`kube_staging.py:87-106`) |
| Cross-process boot flag | A new Postgres table/migration | Redis key via `ctx["redis"]` / `app.state.redis` | Both processes already share Redis; the dashboard already reads it degrade-safe |
| Degrade-safe dashboard read | A bespoke try/except in the router | Mirror `get_inadmissible_count` / `read_counters` | Project-wide "hot poll never 500s" pattern (T-54-10) |
| DEAD-pill suppression for the k8s lane | New liveness branch / synthetic card | Nothing — it's structural (no heartbeat → `never`, never `dead`) | The pod runs `job_runner`, not the SAQ worker's `_heartbeat_loop` |
| Kueue manifest authoring | Inventing quota/RBAC shapes | The verified v1beta1 manifests in §Runbook Content | Official Kueue docs + Context7 confirmed |

**Key insight:** This phase's net-new code is ~one kr8s GET + one Redis flag + one Jinja partial + one
static note. The temptation is to over-build (a Postgres table, a synthetic Agents card, new validators);
every one of those is either already solved or explicitly deferred to v7.0.

## Common Pitfalls

### Pitfall 1: apiVersion drift between the runbook, the code, and the cluster
**What goes wrong:** The runbook ships `v1beta2` manifests while `kube_workload_api_version` defaults to
`kueue.x-k8s.io/v1beta1` (or vice-versa), or the operator's installed Kueue only serves one version.
**Why it happens:** Kueue introduced `v1beta2` and **deprecated `v1beta1`** (still served, deprecation
warning on write). The phaze code is pinned to `v1beta1` via the config default (`config.py:565`).
**How to avoid:** The runbook must state ONE rule loudly: **the manifest apiVersion, the cluster's served
Kueue version, and `PHAZE_KUBE_WORKLOAD_API_VERSION` must all agree.** Ship the manifests at the same
version as the config default (`v1beta1`) and add a "if your Kueue is v1beta2-only, set
`PHAZE_KUBE_WORKLOAD_API_VERSION=kueue.x-k8s.io/v1beta2` AND use the v1beta2 manifest variants" note.
**Warning signs:** `submit_job` 404s on the Workload group; the reconcile `get_workload_for` always
returns `None`; the LocalQueue probe 404s a LocalQueue that exists under a different version.

### Pitfall 2: RBAC verb set that doesn't match the live call graph
**What goes wrong:** The Role grants `watch` on workloads (per the conservative D-01 spec) but the code
actually `list`s them (no watch stream — confirmed `kube_staging.get_workload_for:231,235`), and FORGETS
the new `get localqueues` verb → the Phase 56 probe 403s and falsely reports "unreachable" forever.
**Why it happens:** The verb list is written from the spec, not the code; the new LocalQueue `get` is
easy to miss.
**How to avoid:** Derive the floor from the call graph (§Validation Architecture) — `jobs:
create/get/delete`, `workloads: list` (+ `get/watch` as conservative spec), and the **new** `localqueues:
get`. Add a test asserting the manifest verb set ⊇ the code's call set.
**Warning signs:** Probe reports unreachable on a healthy cluster; `submit_cloud_job` succeeds but
reconcile can't read admission state.

### Pitfall 3: Treating the LocalQueue probe like the config validators (crashing boot)
**What goes wrong:** The probe raises on a transient kube/mesh blip at boot and takes down
Postgres/Redis/UI/local-analysis with it.
**Why it happens:** Copy-pasting the fail-fast validator mindset onto a live runtime probe.
**How to avoid:** D-05 is explicit — warn + surface, non-fatal. Wrap in a broad `try/except` exactly like
the ledger-backfill/recovery blocks (`controller.py:145-157`). The flag read on the dashboard is
degrade-safe (error → silent).
**Warning signs:** Controller worker crash-loops when the cluster is briefly unreachable.

### Pitfall 4: The healthy-`never` k8s agent row read as a fault
**What goes wrong:** A reviewer sees the k8s token's Agent row sitting at `never` and "fixes" it by
heartbeating it or hiding all compute agents — breaking the A1 display or building the deferred v7.0 card.
**Why it happens:** `never` looks like an error if you don't know the lane is ephemeral.
**How to avoid:** The static note (D-07) explains it. Do NOT add heartbeating to the pod and do NOT
filter `kind=compute` wholesale (hides the A1).
**Warning signs:** A PR that touches `_heartbeat_loop` or adds a compute-agent filter to `_load_agents`.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Kueue `kueue.x-k8s.io/v1beta1` as the only stored version | `v1beta2` is the new storage version; `v1beta1` **deprecated** (still served, conversion-on-write) | Kueue ~v0.13/v0.14 era (mid-2026; v0.15 in flight) | Runbook + `kube_workload_api_version` must agree with the cluster's served version. phaze still defaults to v1beta1 — document the v1beta2 upgrade path. [VERIFIED: kueue.sigs.k8s.io v1beta1 + v1beta2 reference pages both live] |
| `LocalQueueStatus.Flavors`, `PriorityClassSource/Name` | dropped/renamed in v1beta2 (`PriorityClassRef`, `LocalQueueFlavorStatus` removed) | v1beta2 | Not used by phaze (phaze reads Workload conditions + LocalQueue existence only), so low blast radius — but note it in the upgrade caveat. [CITED: WebSearch Kueue v1beta2 migration] |

**Deprecated/outdated:**
- The `v1beta1` ClusterQueue snippet returned by Context7 with `nominalCapacity`/`minSize`/singular
  `flavor:` is **malformed/stale** — the canonical v1beta1 schema is `resourceGroups[].coveredResources`
  + `flavors[].resources[].nominalQuota` (verified from the official cluster_queue concept page and used
  in §Runbook Content). Do not copy the Context7 quick-start ClusterQueue verbatim.

## Validation Architecture

> `workflow.nyquist_validation` is not disabled in `.planning/config.json` → section included.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio + pytest-cov (uv-managed) [VERIFIED: CLAUDE.md, justfile] |
| Config file | `pyproject.toml` ([tool.pytest...]/[tool.coverage]) |
| Quick run command | `uv run pytest tests/test_<module>.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (≥85% required) |
| Existing kube seams | `tests/kube_fakes.py` (fake_job/fake_workload + canned condition sets), `tests/test_deployment/` (compose/job-image invariants) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Automated Command | File Exists? |
|-----|----------|-----------|-------------------|-------------|
| KDEPLOY-01 | Runbook manifests are valid YAML | unit | `uv run pytest tests/test_deployment/test_k8s_runbook.py -x` | ❌ Wave 0 |
| KDEPLOY-01 | RBAC verb set ⊇ the code's actual kr8s call set (jobs create/get/delete; workloads list; localqueues get) | unit | `uv run pytest tests/test_deployment/test_k8s_runbook.py::test_rbac_covers_call_graph -x` | ❌ Wave 0 |
| KDEPLOY-04 | `get_local_queue()` success → no flag; 404 (NotFoundError) → unreachable; transient → unreachable; **never raises out of startup** | unit | `uv run pytest tests/test_kube_staging.py::test_get_local_queue_* -x` (mock kr8s GET via kube_fakes) | ❌ Wave 0 (add fake_local_queue helper) |
| KDEPLOY-04 | Probe runs only when `cloud_target=="k8s"`; broad try/except never aborts boot | unit | `uv run pytest tests/test_controller_startup.py::test_localqueue_probe_* -x` | ❌ Wave 0 |
| KDEPLOY-04 | Dashboard read is degrade-safe (missing/error redis → silent) + alert renders empty when reachable | unit | `uv run pytest tests/test_pipeline_dashboard.py::test_localqueue_alert_* -x` | ❌ Wave 0 |
| KDEPLOY-04 | **Invariant:** `classify(agent, now) != "dead"` when `last_seen_at is None` (always `never`) | unit | `uv run pytest tests/test_agent_liveness.py -k never -x` | ✅ likely exists (extend) |
| KDEPLOY-04 | `job_runner` never imports/calls the heartbeat loop (ephemeral pod doesn't heartbeat) | unit | `uv run pytest tests/test_task_split.py -k heartbeat -x` (import-boundary style) | ✅ test_task_split.py exists (extend) |
| KDEPLOY-02/03/05 | Docs-only — assert presence/links | smoke | `uv run pytest tests/test_docs_links.py -x` if such a test exists, else doc-checklist | manual/doc |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_kube_staging.py tests/test_agent_liveness.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (≥85%)
- **Phase gate:** full suite green + `pre-commit run --all-files` before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_deployment/test_k8s_runbook.py` — parse the runbook manifests as YAML; assert the RBAC
      verb set covers the kr8s call graph (KDEPLOY-01)
- [ ] `tests/kube_fakes.py` — add a `fake_local_queue(...)` helper + a 404/transient seam for
      `get_local_queue` (mirror fake_job)
- [ ] `tests/test_kube_staging.py` — `get_local_queue` success/not-found/transient cases
- [ ] `tests/test_controller_startup.py` (or extend existing) — probe gating + boot-resilience
- [ ] `tests/test_pipeline_dashboard.py` — alert OOB carrier + degrade-safe flag read
- [ ] Extend `tests/test_agent_liveness.py` + `tests/test_task_split.py` for the D-07 invariants

**Pure-docs (not meaningfully samplable beyond YAML-validity + link checks):** the homelab change-prompt,
the deploy-ordering prose, the transport-agnostic endpoint notes, the configuration.md knob table.

## Security Domain

> `security_enforcement` not disabled → included.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | Bearer-token (`get_authenticated_agent`, sha256 token_hash, partial index) — unchanged; the cluster Secret carries the token via `_FILE` |
| V4 Access Control | yes | **Namespaced least-privilege RBAC** (the runbook's core deliverable) — no cluster-wide grants; verb set = code floor |
| V5 Input Validation | yes | Job name is `phaze-analyze-<uuid>` (DNS-1123 safe, no operator free-text → kube object names, `kube_staging.job_name:61`); alert/note strings are static through Jinja autoescape (T-54-11) |
| V6 Cryptography | yes | Token is uniform-random, sha256-hashed (no KDF needed, `agent_auth.py`); kube creds are `SecretStr`, never logged (T-54-07) |
| V7 Error Handling/Logging | yes | Probe WARNING names the env var, never the token/DSN; degrade-safe dashboard read never 500s |

### Known Threat Patterns for {control-plane ↔ kube API over mesh}
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Over-broad kube RBAC (cluster-admin token leak) | Elevation of Privilege | Namespaced Role, exact verb floor, single namespace (KDEPLOY-01) |
| Bearer token in logs | Information Disclosure | `SecretStr` + `_FILE` mounts; never logged (T-54-01/07); probe logs the env var name only |
| Operator free-text → kube object name injection | Tampering | Deterministic UUID-based names only (no operator string enters object names) |
| SSRF via `s3_endpoint_url` | Tampering/SSRF | Already validated http(s)+netloc at construction (`config.py:582`) |
| Mesh blip → boot crash (availability) | Denial of Service | Non-fatal probe (D-05), broad try/except, degrade-safe read |

## Environment Availability

> The phaze repo itself needs no new tooling. The runbook's *targets* are operator-owned (out of repo).

| Dependency | Required By | Available (in repo) | Version | Fallback |
|------------|------------|---------------------|---------|----------|
| `kr8s` | LocalQueue probe | ✓ | 0.20.15 | — |
| `redis.asyncio` | cross-process flag | ✓ (transitive) | in use | Postgres singleton (heavier) |
| Live Kueue cluster | runtime k8s burst | ✗ (operator-owned) | operator's | smoke-test is a doc checklist (discretion); unit tests use kube_fakes |
| `kubeconform`/`kubectl --dry-run` | runbook YAML validation (optional) | ✗ | — | YAML-parse test in pytest (no cluster needed) |

**Missing dependencies with no fallback:** none (the live cluster is the operator's; all phaze-side code
is testable against `kube_fakes`).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The runbook should ship `v1beta1` manifests (matching the config default) with a documented `v1beta2` upgrade path | Runbook Content / Pitfall 1 | If the operator's Kueue is v1beta2-only, v1beta1 manifests may warn/fail; mitigated by the explicit "keep all three in sync" rule |
| A2 | The k8s pod authenticates the callback with `agent_token` minted via `phaze agents add --kind compute` (creating an Agent row) | Net-New Code Design (D-07) | If a different auth scheme is intended, the "Agent row exists → `never` pill" analysis changes (but DEAD-suppression still holds — no heartbeat) |
| A3 | Redis is the right cross-process bus for the unreachable flag | Net-New Code Design | If the team prefers a Postgres singleton, add a migration; functionally equivalent, both degrade-safe |
| A4 | The code's true workload verb is `list` only (no `watch` stream); `get/watch` are conservative spec | Runbook RBAC / Pitfall 2 | If a future watch is added, the Role already covers it (spec is broader than code) |
| A5 | RBAC/Secret YAML schemas (rbac.authorization.k8s.io/v1, core/v1) are standard and stable | Runbook Content | Very low — these are GA Kubernetes APIs |
| A6 | API discovery endpoints are readable by the SA (kr8s session handshake) | Runbook RBAC note | On a hardened cluster, discovery may need an explicit grant; flagged for operator confirmation |

## Open Questions

1. **Which Kueue version is the homelab cluster actually running (v1beta1 vs v1beta2-only)?**
   - What we know: phaze code defaults to `v1beta1`; Kueue has deprecated v1beta1 in favor of v1beta2.
   - What's unclear: the operator's installed Kueue minor version.
   - Recommendation: ship v1beta1 manifests + the v1beta2 upgrade note (Pitfall 1); the homelab
     change-prompt should ask the operator to confirm/pin the Kueue version.
2. **`never` pill for the k8s token row — accept it or filter it?**
   - What we know: a `kind=compute` filter would wrongly hide the v5.0 A1 agent.
   - Recommendation: accept the `never` pill + the static note (option (a)); defer the finer card to v7.0
     RECORD-03 (explicitly out of scope per D-07).
3. **Trim the transitional k8s section already in `cloud-burst.md:297-389`?**
   - Recommendation (discretion): leave a short pointer in `cloud-burst.md` to the new `k8s-burst.md`;
     planner decides exact wording (D-03/D-04 discretion).

## Sources

### Primary (HIGH confidence)
- In-repo code (read first-hand): `src/phaze/config.py` (validators + `_FILE` fields + kube/s3 knobs),
  `src/phaze/services/kube_staging.py` (the kr8s call graph + `_api`/`get_job`/`get_workload_for`),
  `src/phaze/tasks/controller.py` (startup hook + boot-resilience pattern),
  `src/phaze/tasks/reconcile_cloud_jobs.py` (Inadmissible flag/alert),
  `src/phaze/services/agent_liveness.py` (`classify` 5-state), `src/phaze/routers/agent_auth.py` +
  `agent_heartbeat.py` (auth vs heartbeat — the DEAD-suppression proof), `src/phaze/job_runner.py`
  (one-shot pod, no heartbeat loop), `src/phaze/routers/pipeline.py` + `services/pipeline.py`
  (degrade-safe dashboard reads + `app.state.redis`),
  `src/phaze/templates/pipeline/partials/inadmissible_card.html`, `src/phaze/templates/admin/agents.html`.
- `/kubernetes-sigs/kueue` (Context7) — quick-start ResourceFlavor/ClusterQueue/LocalQueue + Job +
  RBAC RoleBinding examples.
- https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/ — canonical v1beta1 ClusterQueue (resourceGroups
  + nominalQuota + `preemption: Never`), ResourceFlavor, LocalQueue [CITED]
- https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta1/ + .../kueue.v1beta2/ — both API versions live
- v5.0 precedents: `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-CONTEXT.md` +
  `51-HOMELAB-CHANGE-PROMPT.md`; `docs/cloud-burst.md`, `docs/configuration.md`, `docs/deployment.md`.

### Secondary (MEDIUM confidence)
- WebSearch (verified against the official Kueue docs above): Kueue v1beta2 release/deprecation of
  v1beta1; minimal external-client RBAC (jobs create/get/delete; workloads get/list/watch).

### Tertiary (LOW confidence)
- The exact RBAC/Secret YAML field details are standard GA Kubernetes APIs (ASSUMED stable, A5).

## Metadata

**Confidence breakdown:**
- Runbook manifests (Kueue v1beta1) — HIGH (verified against official cluster_queue docs + Context7);
  the v1beta1↔v1beta2 version-matching is the one MEDIUM caveat (A1, Pitfall 1).
- Net-new code design (probe + flag + D-07 invariant) — HIGH (every integration point read first-hand;
  DEAD-suppression proven structurally from `classify`/`agent_heartbeat`/`job_runner`).
- RBAC verb floor — HIGH (derived directly from the kr8s call graph in `kube_staging.py`).
- Docs structure — HIGH (v5.0 precedent files read directly).

**Research date:** 2026-06-28
**Valid until:** 2026-07-28 (30 days) — EXCEPT the Kueue version caveat, which should be re-checked
against the operator's installed Kueue release at deploy time (Kueue is fast-moving; v0.15 in flight).
