<!-- generated-by: gsd-executor -->
# Homelab Change Prompt — Phaze v6.0 Kubernetes Burst (Kueue objects + RBAC + Secret)

> **Paste the section below into the homelab repo agent.** It is a ready-to-apply
> change request for the homelab deployment of Phaze. It carries the **cluster setup
> spec** for the v6.0 Kubernetes (Kueue) burst target: the operator-owned Kueue admin
> objects (ResourceFlavor / ClusterQueue / LocalQueue), the namespaced least-privilege
> RBAC (ServiceAccount / Role / RoleBinding), and the compute-agent bearer-token Secret.
> Phaze is the **source-of-truth spec**; all live infra (the applied `kubectl` manifests,
> the cluster RBAC, the Secret) lives in the **homelab** repo / cluster and is applied
> there (workspace boundary, D-02). Use placeholders only — never commit a real token,
> kubeconfig, or cluster endpoint. **No live `kubectl` is authored in the phaze repo.**

---

## Context for the homelab agent

Phaze v6.0 adds a **Kubernetes (Kueue) burst** target: long audio sets (≥ the cloud-route
threshold) are staged to an S3-compatible bucket and analyzed by an **ephemeral, one-shot
Kueue `Job`** on an **x64 Kubernetes cluster**, instead of timing out on the local
fileserver. This is the **third** `PHAZE_CLOUD_TARGET` (`local` | `a1` | `k8s`), alongside
the v5.0 OCI A1 compute agent. The control plane (`lux`):

- submits a **suspended `batch/v1` Job** labeled `kueue.x-k8s.io/queue-name` into a
  **LocalQueue** the operator owns (Kueue admits it against the ClusterQueue quota),
- watches the paired **Workload** and reconciles results by `file_id` via a `*/5` cron,
- reaches the cluster over **whatever mesh the operator runs** (Tailscale **or**
  WireGuard) — phaze consumes operator-provided reachable endpoints only, no mesh code.

**phaze does NOT create any cluster objects.** Phaze references a LocalQueue **by name**
and submits Jobs into it; Kueue admission, RBAC, and the bearer-token Secret are
**cluster-admin** responsibilities. The authoritative, copy-paste-ready manifests live in
the phaze repo at **`docs/k8s-burst.md` → Cluster-admin runbook** — apply those exact
manifests; this prompt is the deploy-ordering wrapper around them.

Phaze emits the spec; **homelab applies it.** The pieces below are exactly what the homelab
repo / cluster must author/apply:

1. **Kueue admin objects** — ResourceFlavor / ClusterQueue / LocalQueue (this prompt, §1).
2. **Namespaced RBAC** — ServiceAccount / Role / RoleBinding (this prompt, §2).
3. **Compute-agent token Secret** — minted on the control plane, applied to the cluster (§3).
4. **Control-plane env knobs** — `PHAZE_CLOUD_TARGET=k8s` + kube/S3 settings (§4).

The feature ships **off by default** (`PHAZE_CLOUD_TARGET=local`); after the cluster objects
below are applied, the operator sets the target and restarts the `lux` control plane (§5).

Apply the changes below to the homelab Phaze deployment (`datum@nox` and `datum@lux`, plus
the operator-owned Kubernetes cluster).

> **⚠ Confirm the Kueue version FIRST (load-bearing).** Phaze defaults to
> `PHAZE_KUBE_WORKLOAD_API_VERSION=kueue.x-k8s.io/v1beta1` and the runbook manifests are
> `v1beta1`. Kueue has **deprecated v1beta1** in favor of **v1beta2**. The manifest
> apiVersion, the cluster's served Kueue version, and `PHAZE_KUBE_WORKLOAD_API_VERSION`
> **must all agree.** If your cluster serves v1beta2 only, set
> `PHAZE_KUBE_WORKLOAD_API_VERSION=kueue.x-k8s.io/v1beta2` AND apply the v1beta2 manifest
> variants. See `docs/k8s-burst.md` → *apiVersion lockstep*.

---

## 1. Apply the Kueue admin objects

Apply the **CPU-only ResourceFlavor**, the **single-CQ no-preemption ClusterQueue** (CPU +
memory `nominalQuota` — essentia is CPU-bound, no GPU/Coral), and the **LocalQueue** that
phaze references by name. The exact manifests are in
**`docs/k8s-burst.md` → Cluster-admin runbook §1–§3** — apply them verbatim, editing only the
placeholder names/quota/namespace to match the cluster:

- `metadata.name` of the LocalQueue **must equal** `PHAZE_KUBE_LOCAL_QUEUE` (default
  `phaze-lq`), and its `metadata.namespace` **must equal** `PHAZE_KUBE_NAMESPACE` (default
  `phaze`).
- The ClusterQueue has **no preemption** (`reclaimWithinCohort: Never`, `withinClusterQueue:
  Never`) and covers `cpu` + `memory` only (no `pods`, no `limits` — matches phaze's
  requests-only Job manifest). Size `nominalQuota` for the cluster's CPU node pool.

```bash
# On a cluster-admin context (NOT in the phaze repo):
kubectl create namespace phaze     # == PHAZE_KUBE_NAMESPACE
kubectl apply -f resourceflavor.yaml   # docs/k8s-burst.md §1
kubectl apply -f clusterqueue.yaml     # docs/k8s-burst.md §2
kubectl apply -f localqueue.yaml       # docs/k8s-burst.md §3
```

---

## 2. Apply the namespaced least-privilege RBAC

The control plane authenticates to the kube API as the `phaze-submitter` ServiceAccount. The
Role grants **exactly** the verb floor derived from phaze's kr8s call graph — **namespaced,
no cluster-wide grants**:

- `batch/jobs`: `create`, `get`, `delete` (submit / get / delete the analysis Job)
- `kueue.x-k8s.io/workloads`: `get`, `watch`, `list` (read admission state)
- `kueue.x-k8s.io/localqueues`: `get` (the startup reachability probe — **load-bearing**;
  omitting it 403s the probe and falsely reports "LocalQueue unreachable" forever)

Apply the ServiceAccount + Role + RoleBinding from
**`docs/k8s-burst.md` → Cluster-admin runbook §4** verbatim:

```bash
kubectl apply -f rbac.yaml          # docs/k8s-burst.md §4 (SA + Role + RoleBinding)
```

The Role is `kind: Role` (not `ClusterRole`) and the RoleBinding binds it in the single
`phaze` namespace — there are **no cluster-wide grants**.

---

## 3. Provision the compute-agent token Secret

The one-shot pod authenticates its `/api/internal/agent/*` result callback with a
compute-agent bearer token — the **same** mechanism as the v5.0 fileserver/compute agents.
Mint it on the **`datum@lux` control plane** (this creates an `Agent` row so the callback
authenticates), paste it into the Secret `stringData`, and apply the Secret to the cluster.
The pod consumes it via `PHAZE_AGENT_TOKEN_FILE` (the `_FILE` convention — never a plain env
var or a log line).

```bash
# On datum@lux (control plane): mint the token.
phaze agents add --kind compute
# Paste the printed token into secret.yaml stringData.PHAZE_AGENT_TOKEN, then on a
# cluster-admin context:
kubectl apply -f secret.yaml        # docs/k8s-burst.md §5 (core/v1 Opaque Secret)
```

**Never commit the token.** It is a `SecretStr` on the phaze side and rides a cluster Secret
on the kube side — use the `*_FILE` convention end to end; placeholders only in this prompt.

---

## 4. Set the control-plane k8s / S3 env knobs (`datum@lux`)

On the `lux` control plane, set `PHAZE_CLOUD_TARGET=k8s` plus the kube client + S3 staging
knobs. These are **control-plane-only** (kube creds live on the control plane, DIST-01);
secrets honor the `*_FILE` convention and **fail fast at startup** if the `cloud_target=k8s`
requirements are unset. Do **not** duplicate the per-knob table here — the canonical reference
is [`docs/configuration.md` → Kube submit/reconcile settings] and [→ S3 object-staging
settings]:

```bash
# In the datum@lux .env (Kubernetes / Kueue target) — placeholders only:
PHAZE_CLOUD_TARGET=k8s
PHAZE_KUBE_API_URL=https://kube-api.example:6443
PHAZE_KUBE_NAMESPACE=phaze                          # == the LocalQueue namespace (§1)
PHAZE_KUBE_LOCAL_QUEUE=phaze-lq                     # == the LocalQueue name (§1)
PHAZE_KUBE_WORKLOAD_API_VERSION=kueue.x-k8s.io/v1beta1   # MUST match the cluster's served Kueue version
PHAZE_KUBE_KUBECONFIG_FILE=/run/secrets/phaze_kube_kubeconfig   # or PHAZE_KUBE_SA_TOKEN_FILE
PHAZE_S3_BUCKET=phaze-staging
PHAZE_S3_ENDPOINT_URL=https://s3.example            # any S3-compatible endpoint
PHAZE_S3_ACCESS_KEY_ID_FILE=/run/secrets/phaze_s3_key
PHAZE_S3_SECRET_ACCESS_KEY_FILE=/run/secrets/phaze_s3_secret
```

---

## 5. Deploy ordering

Apply in this order (cluster objects **before** flipping the control plane to `k8s` — the
LocalQueue must exist before the startup probe runs and before any Job submits). SSH targets
`datum@nox` / `datum@lux`. Placeholders only — never inline real secrets.

1. **Confirm the cluster's served Kueue version** and keep the manifest apiVersion +
   `PHAZE_KUBE_WORKLOAD_API_VERSION` in lockstep with it (v1beta1 by default).
2. **Cluster (operator):** apply the Kueue admin objects — ResourceFlavor → ClusterQueue →
   LocalQueue (§1).
3. **Cluster (operator):** apply the namespaced RBAC — ServiceAccount + Role + RoleBinding
   (§2).
4. **`datum@lux` control plane:** mint the compute-agent token
   (`phaze agents add --kind compute`); paste it into the Secret and apply it to the cluster
   (§3).
5. **`datum@lux` control plane:** set `PHAZE_CLOUD_TARGET=k8s` + the kube/S3 knobs (§4), then
   **restart** the controller worker + api (`cloud_target` is a startup-read — setting the env
   on a running controller does nothing until it restarts).
6. **Smoke test:** trigger analysis on a long set; confirm it stages to S3, a
   `phaze-analyze-<file_id>` Job is submitted and admitted by Kueue, the pod analyzes it, and
   the result reconciles by `file_id`. Confirm the pipeline dashboard shows **no** "K8s
   LocalQueue unreachable" alert.

**Closing notes.**

- **Off-by-default ships the cloud feature dormant.** A fresh v6.0 deploy is **all-local**
  until the cluster objects are applied and `PHAZE_CLOUD_TARGET=k8s` is set; long files route
  to the local queue (and may time out cleanly as `ANALYSIS_FAILED`) until then.
- **Flipping `PHAZE_CLOUD_TARGET` requires a control-plane restart** (startup-read of the
  settings singleton). Setting the env without restarting changes nothing.
- **The `localqueues: get` RBAC verb is load-bearing** — without it the startup probe 403s and
  the dashboard falsely reports the LocalQueue unreachable forever. The phaze runbook test
  (`tests/test_deployment/test_k8s_runbook.py`) asserts the verb floor so it cannot be dropped.
- **`datum@nox` (file server)** needs no change for the k8s target — the file server stages
  bytes to S3 over HTTP (no SDK, no bucket credentials, DIST-01), exactly as for any
  control-plane-orchestrated staging. The `datum@nox` work was the v5.0 A1 rsync path.

---

## Done-when checklist

- [ ] Cluster's served Kueue version confirmed; manifest apiVersion + `PHAZE_KUBE_WORKLOAD_API_VERSION` in lockstep (v1beta1 default, or v1beta2 variants if the cluster is v1beta2-only)
- [ ] Kueue admin objects applied from `docs/k8s-burst.md` §1–§3: CPU-only ResourceFlavor, no-preemption ClusterQueue (cpu+memory quota), LocalQueue named `PHAZE_KUBE_LOCAL_QUEUE` in `PHAZE_KUBE_NAMESPACE`
- [ ] Namespaced RBAC applied from `docs/k8s-burst.md` §4: ServiceAccount + Role (jobs create/get/delete; workloads get/watch/list; localqueues get) + RoleBinding — **no cluster-wide grants**
- [ ] Compute-agent token minted on `datum@lux` (`phaze agents add --kind compute`), pasted into the Secret, and applied to the cluster (`docs/k8s-burst.md` §5)
- [ ] `datum@lux` env set: `PHAZE_CLOUD_TARGET=k8s` + kube/S3 knobs (`*_FILE` secrets), control plane restarted
- [ ] Deploy order followed: confirm Kueue version → Kueue objects → RBAC → Secret → `PHAZE_CLOUD_TARGET=k8s` + restart → smoke test
- [ ] Smoke test green: long set stages to S3, Job admitted, pod analyzes, result reconciles by `file_id`, **no** "K8s LocalQueue unreachable" alert on the pipeline dashboard

---

*Cluster setup spec for Phase 56 (v6.0 Kubernetes Burst: Kueue admin objects + namespaced
least-privilege RBAC + compute-agent token Secret). Phaze is source-of-truth (`docs/k8s-burst.md`);
the homelab repo / cluster applies the `kubectl` manifests, the RBAC, and the Secret.*
