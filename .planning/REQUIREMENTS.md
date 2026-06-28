# Requirements: Phaze — v6.0 Kubernetes Burst Analysis

**Defined:** 2026-06-26
**Core Value:** Get 200K messy music/concert files properly named, organized, deduplicated, with rich metadata — human-in-the-loop approval. v6.0 adds: long sets that can't finish locally can be analyzed on a remote x64 Kubernetes cluster running Kueue, as a third routing target alongside local and the v5.0 OCI A1 — ephemeral, quota-scheduled, unattended.

## Design spine (locked at milestone definition)

- **Execution unit:** one ephemeral **Kueue batch Job per long file** (not a persistent SAQ-draining host). Kueue is an admission/quota gate only — it carries no result payload.
- **Results stay out-of-band:** the Job pod POSTs analysis output to the existing `/api/internal/agent/*` surface as a registered compute agent, reconciled by `file_id`. A dropped kube watch never loses a result.
- **DIST-01 byte path:** the control plane *presigns* S3 URLs (aioboto3) but the **file-server agent uploads** the bytes (httpx PUT) and the **pod downloads** (presigned GET). aioboto3 lives only on the control plane; agent and pod are S3-credential-free.
- **One live-seam edit:** a single `cloud_target` branch inside `stage_cloud_window`. Reuse the duration router, AWAITING_CLOUD hold, advisory-locked in-flight window, `PUSHING`/`PUSHED` states, scheduling ledger, and `cloud_burst_enabled` toggle unchanged.
- **CPU-only:** essentia analysis is CPU-bound; Kueue resource requests target `cpu`/`memory`, never GPU/Coral.
- **Transport-agnostic:** Tailscale *or* WireGuard; phaze consumes operator-provided reachable endpoints only.

## v6.0 Requirements

Each maps to exactly one roadmap phase (Traceability below).

### Job-runner image & one-shot entrypoint (KJOB)

- [x] **KJOB-01**: An x86 Kueue Job-runner image is published to GHCR, built `FROM` the existing x86 essentia base image (the cluster is x64 — no arm64 source build) with **zero new pip dependencies** and a one-shot entrypoint.
- [x] **KJOB-02**: The entrypoint runs as a one-shot: request a fresh presigned download URL from the control plane → download the file → sha256-verify against `FileRecord` → analyze → POST the result to `/api/internal/agent/*` (reconciled by `file_id`) → exit.
- [x] **KJOB-03**: Analysis in the pod uses the windowed/streaming path so a multi-hour set never OOMs under a hard pod memory limit (no whole-file `MonoLoader` decode); memory requests are sized from measured peak RSS on the longest real sets.
- [x] **KJOB-04**: The entrypoint has an honest exit-code contract — non-zero on download, integrity, analysis, or callback failure; it never reports success on a failed analysis.
- [x] **KJOB-05**: The pod trusts the control plane's internal CA (baked into the image) for the HTTPS callback; no TLS bypass (`verify=False`) anywhere.

### S3 object-staging leg (KSTAGE)

- [x] **KSTAGE-01**: The control plane presigns S3 PUT/GET URLs and deletes objects (aioboto3) and **never reads or uploads file bytes itself** — preserving the CI-enforced DIST-01 boundary (no media mount on the application server).
- [x] **KSTAGE-02**: The file-server agent uploads the file bytes to the presigned PUT URL over HTTP (httpx), then callbacks the control plane; no S3 SDK or bucket credentials live on the agent or the pod.
- [x] **KSTAGE-03**: The presigned GET URL is minted just-in-time when the pod requests it at startup (necessarily post-admission), so it never expires during a long Kueue quota wait.
- [x] **KSTAGE-04**: Each staged object uses a `file_id`-scoped key and is deleted on **every** terminal outcome (success, failure, eviction, re-drive), with a bucket lifecycle TTL as a backstop against orphaned-object leaks.
- [x] **KSTAGE-05**: S3 endpoint, bucket, addressing style, and credentials are operator-provided config via `_FILE` secrets and work against any S3-compatible backend (`endpoint_url`), not just AWS.

### Kube submission, watch & reconcile (KSUBMIT)

- [ ] **KSUBMIT-01**: When a long file is staged for K8s, the control plane submits a single **suspended** `batch/v1` Job (labeled `kueue.x-k8s.io/queue-name=<LocalQueue>`, `restartPolicy: Never`, `parallelism: 1`, `cpu`/`memory` requests only) via the kube API; submission is idempotent (deterministic name keyed to `file_id`).
- [ ] **KSUBMIT-02**: The submit task returns within seconds and never blocks a worker waiting for analysis; a periodic reconcile cron owns Workload status, cleanup, and re-drive.
- [ ] **KSUBMIT-03**: The result is authoritative **only** via the out-of-band `/api/internal/agent/*` callback reconciled by `file_id`; a dropped or expired kube watch never loses or duplicates a result.
- [ ] **KSUBMIT-04**: The reconcile loop distinguishes healthy `Pending` (queued behind quota — wait indefinitely) from `Inadmissible` (misconfigured LocalQueue — surface to the operator) and detects success / failure / eviction.
- [ ] **KSUBMIT-05**: On Job failure or eviction the file is re-driven through the K8s staging window up to a bounded max-attempts cap, then marked `ANALYSIS_FAILED` — **no cross-target fallback** in v6.0. Job `backoffLimit`/Kueue requeue are neutralized so the control plane solely owns retry.
- [ ] **KSUBMIT-06**: Finished Jobs are cleaned up without a TTL-vs-read race (TTL longer than the reconcile interval, or phaze deletes the Job after recording the outcome); **no `process_file:<id>` ledger row is seeded for K8s files**, so `recover_orphaned_work` never wrongly re-enqueues them onto an agent queue (the CLOUDROUTE-02 hazard).

### Routing, state & ledger integration (KROUTE)

- [ ] **KROUTE-01**: A single `cloud_target` config selector (`Literal["local", "a1", "k8s"]`) chooses the active analysis target — `k8s` routes ≥threshold long files through the Kueue path, `a1` keeps the v5.0 OCI A1 path, `local` disables cloud burst — all under the existing `cloud_burst_enabled` master toggle.
- [ ] **KROUTE-02**: K8s offload reuses the existing duration router + AWAITING_CLOUD hold + advisory-locked `stage_cloud_window` in-flight window (`cloud_max_in_flight`) as a single new branch; long files only (conservative scope), never a whole-backlog sweep.
- [ ] **KROUTE-03**: K8s in-flight files reuse the `PUSHING`/`PUSHED` states (no new `FileRecord` state); Kueue admission phase lives in a `cloud_phase` column on a new `cloud_job` sidecar table (Alembic migration), leaving the FileRecord state machine unchanged.
- [ ] **KROUTE-04**: A static AST guard test asserts every K8s enqueue site routes through `enqueue_router` (no consumer-less default-queue enqueue, no whole-backlog enqueue), preventing recurrence of the v4.0.6 / v5.0 over-enqueue incidents.
- [ ] **KROUTE-05**: ≥threshold backfill of timed-out long files (`analysis_failed`, duration ≥ threshold) can be driven to the K8s target, ledger-scoped exactly like v5.0 (only previously-scheduled work re-driven).

### Deployment, runbook, config & docs (KDEPLOY)

- [ ] **KDEPLOY-01**: A cluster-admin runbook documents the Kueue objects phaze does **not** create (ResourceFlavor / ClusterQueue / LocalQueue, CPU-only flavor, single-CQ no-preemption quota), the least-privilege RBAC Role/ServiceAccount for phaze (create/get/delete Jobs, get/watch/list Workloads in one namespace), and the cluster Secret carrying the compute-agent bearer token.
- [ ] **KDEPLOY-02**: All K8s/S3 parameters — `cloud_target`, kube API endpoint + kubeconfig/SA-token, LocalQueue name, Workload apiVersion, S3 endpoint/bucket/credentials, presign expiry, max-attempts, in-flight timeout — are pydantic-settings with `_FILE`-secret support and a model validator that fail-fasts when `cloud_target="k8s"` but required K8s/S3 config is missing.
- [ ] **KDEPLOY-03**: Connectivity is transport-agnostic — phaze consumes operator-provided reachable endpoints (kube API, S3, callback) over either Tailscale or WireGuard, with no mesh-specific code or assumptions.
- [ ] **KDEPLOY-04**: At startup (when `cloud_target="k8s"`) phaze validates the configured LocalQueue is reachable and surfaces a clear error otherwise; the cluster compute-agent identity is shown as an ephemeral (Job-based) identity in the Agents UI rather than a perpetually-DEAD heartbeating agent.
- [ ] **KDEPLOY-05**: The entire K8s offload reverts to all-local (or A1) via the single `cloud_target` / `cloud_burst_enabled` toggle with no other change; `docs/deployment.md` documents the full cluster + bucket + secret setup.

## Future Requirements (deferred)

- **KSUBMIT-07**: `maximumExecutionTimeSeconds` runaway guard on the Workload (v6.x — set once typical cluster wall-clock is measured).
- **KSUBMIT-08**: Eviction/deactivation → automatic **cross-target** re-route (A1/local) instead of bounded same-target re-drive (P2 per FEATURES research).
- **KROUTE-06**: Pipeline dashboard admission-state cards (queued-behind-quota / admitted / running / finished) driven by `cloud_phase`.
- **KJOB-06**: Multi-arch single-tag manifest for the Job image (currently x86-only; the cluster is x64).
- **KSUBMIT-09**: More than one concurrent file per Job / multiple LocalQueues / elastic parallelism (each Job is `parallelism: 1` in v6.0).
- **KDEPLOY-06**: ConfigMap-mounted internal CA so CA rotation doesn't require a Job-image rebuild (v6.0 bakes the CA into the image).

## Out of Scope

- **phaze creating Kueue admin objects** (ResourceFlavor / ClusterQueue / LocalQueue) — cluster-admin/runbook setup only; phaze references a LocalQueue by name.
- **GPU / Coral TPU accelerator requests** — essentia analysis is CPU-bound (decode + DSP dominate; TF inference is a tiny slice); throughput comes from horizontal CPU parallelism, which Kueue quota provides. (See PROJECT.md Key Decisions.)
- **Kueue priority classes / preemption / fair-sharing / cohorts / MultiKueue / multi-cluster dispatch** — the operator owns cluster quota policy; phaze submits and observes only.
- **Object storage as a data home** — staging is ephemeral and analysis-only; objects are deleted after every terminal outcome. Not a replacement for the file-server as the file's home.
- **Replacing the v5.0 OCI A1 path** — K8s is an *additional* third target selected by `cloud_target`, not a replacement; both cloud paths coexist.
- **Application-server ↔ file-server file transfer** — DIST-01 is unchanged; the agent uploads to S3, never the app server, and the app server still has no media mount.
- **Analyzing the whole backlog in parallel on the cluster** — v6.0 routes only ≥threshold long files through the same ledger-scoped seam; bulk/full-backlog parallelism is explicitly out of scope (conservative workload choice).

## Traceability

Each v6.0 requirement maps to exactly one phase. **Coverage: 26/26 — no orphans, no duplicates.**

| Requirement | Phase | Status |
|-------------|-------|--------|
| KJOB-01 | Phase 52 — Job-runner image & one-shot entrypoint | Complete |
| KJOB-02 | Phase 52 — Job-runner image & one-shot entrypoint | Complete |
| KJOB-03 | Phase 52 — Job-runner image & one-shot entrypoint | Complete |
| KJOB-04 | Phase 52 — Job-runner image & one-shot entrypoint | Complete |
| KJOB-05 | Phase 52 — Job-runner image & one-shot entrypoint | Complete |
| KSTAGE-01 | Phase 53 — S3 object-staging leg | Complete |
| KSTAGE-02 | Phase 53 — S3 object-staging leg | Complete |
| KSTAGE-03 | Phase 53 — S3 object-staging leg | Complete |
| KSTAGE-04 | Phase 53 — S3 object-staging leg | Complete |
| KSTAGE-05 | Phase 53 — S3 object-staging leg | Complete |
| KSUBMIT-01 | Phase 54 — Kube submit/watch + reconcile cron | Pending |
| KSUBMIT-02 | Phase 54 — Kube submit/watch + reconcile cron | Pending |
| KSUBMIT-03 | Phase 54 — Kube submit/watch + reconcile cron | Pending |
| KSUBMIT-04 | Phase 54 — Kube submit/watch + reconcile cron | Pending |
| KSUBMIT-05 | Phase 54 — Kube submit/watch + reconcile cron | Pending |
| KSUBMIT-06 | Phase 54 — Kube submit/watch + reconcile cron | Pending |
| KROUTE-01 | Phase 55 — Routing, state & ledger integration | Pending |
| KROUTE-02 | Phase 55 — Routing, state & ledger integration | Pending |
| KROUTE-03 | Phase 55 — Routing, state & ledger integration | Pending |
| KROUTE-04 | Phase 55 — Routing, state & ledger integration | Pending |
| KROUTE-05 | Phase 55 — Routing, state & ledger integration | Pending |
| KDEPLOY-01 | Phase 56 — Deployment, runbook, config & docs | Pending |
| KDEPLOY-02 | Phase 56 — Deployment, runbook, config & docs | Pending |
| KDEPLOY-03 | Phase 56 — Deployment, runbook, config & docs | Pending |
| KDEPLOY-04 | Phase 56 — Deployment, runbook, config & docs | Pending |
| KDEPLOY-05 | Phase 56 — Deployment, runbook, config & docs | Pending |
