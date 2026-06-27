# Stack Research

**Domain:** v6.0 Kubernetes Burst Analysis — offloading long-audio essentia analysis to a remote x64 Kubernetes cluster running Kueue
**Researched:** 2026-06-26
**Confidence:** HIGH (kube client, S3, Kueue CRD versions verified via PyPI + Context7 + Kueue release/docs; published same-day)

## Scope Note

This is a SUBSEQUENT-milestone stack delta. The existing validated stack (Python 3.13, uv, FastAPI, async SQLAlchemy + asyncpg, **SAQ on a PostgresQueue broker**, pydantic-settings with `_FILE` secrets, httpx, tenacity, respx, cryptography, essentia-tensorflow x86 wheel, mutagen) is unchanged. Only NEW dependencies for the Kube/Kueue offload path are researched here.

**Two new external dependencies vs. v5.0**, plus zero-new-dep image work:
1. A Python **Kubernetes API client** (control plane submits suspended Kueue Jobs, watches Workload/Job status).
2. An **S3-compatible object-storage client** (control plane stages one long file per Job; pod fetches + deletes).
3. A new **x86 one-shot Job-runner image** to GHCR — reuses existing essentia base layers, no new pip deps.

## Recommended Stack

### Core Technologies (NEW)

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| **kr8s** | 0.20.15 (latest, PyPI 2026-01-16; `requires-python >=3.9`) | Async Kubernetes client — submit suspended batch Job, watch Job conditions, read Kueue `Workload` CRD | **Async-native AND reuses the existing HTTP stack.** kr8s is built on `httpx` + `anyio` (both already direct/transitive deps of phaze) — it does NOT pull a second HTTP stack the way `kubernetes-asyncio` (aiohttp) does. Tiny, kubectl-familiar API: `await Job(...).create()` then `await job.wait(["condition=Complete","condition=Failed"], timeout=...)`. Dynamic CRD access (`kr8s.asyncio.get("workloads.kueue.x-k8s.io", ...)`) reads the Kueue Workload with no codegen/stubs. Handles kubeconfig, in-cluster service-account token, and explicit token/CA auth. Context7 benchmark score 97. |
| **aioboto3** | 15.5.0 (latest, PyPI; pins `aiobotocore[boto3]==2.25.1`) | Async S3-compatible client on the **control plane** — upload one long file, generate a presigned GET URL, delete object after analysis | Async-native (asyncio) so S3 staging composes with the async FastAPI/SAQ control plane without thread-pool gymnastics. Wraps `botocore`, the reference S3 implementation, so any S3-compatible endpoint (MinIO, Ceph RGW, Backblaze B2, Wasabi, AWS) works via `endpoint_url=`. First-class `generate_presigned_url("get_object", ...)` — the key primitive that lets the Job pod fetch with a short-lived URL and **no long-lived credentials in the cluster**. Credentials inject cleanly from `_FILE` secrets (read file → pass `aws_access_key_id` / `aws_secret_access_key` to the session). |

### Supporting Libraries / Decisions

| Item | Version | Purpose | When to Use |
|------|---------|---------|-------------|
| **httpx** (existing) | already in stack | Job pod downloads the staged file via the presigned GET URL; pod POSTs results to `/api/internal/agent/*` | **No S3 SDK in the Job image.** The pod receives a presigned GET URL (env/arg from the control plane) and does a plain `httpx` GET. It already uses httpx to call back the internal agent API (v5.0 compute-agent machinery). Keeps the Job image lean and credential-free. |
| **Kueue** (cluster-side, not a pip dep) | **v0.18.2** (released 2026-06-26) | Job queueing / quota admission controller on the remote cluster | Admin-installed on the cluster. phaze does NOT depend on a Kueue Python binding — see §"Do we need Kueue bindings?" below. Reference only: documented as cluster-admin setup (ResourceFlavor / ClusterQueue / LocalQueue). |
| pydantic-settings `_FILE` convention (existing) | already in stack | kubeconfig/SA-token, S3 credentials, bucket name, LocalQueue name, kube API endpoint, active-cloud-target selector | Reuse the established `<VAR>_FILE` secret pattern. kubeconfig and S3 creds are files mounted as Docker secrets; settings read them. No new config library. |
| tenacity (existing) | already in stack | Retry transient kube-API / S3 / network errors over the operator VPN | Reuse — the VPN (Tailscale or WireGuard) link can blip; wrap submit/watch/upload in the existing retry helpers. |

### Development / Build (NEW image, zero new pip deps)

| Tool | Purpose | Notes |
|------|---------|-------|
| Docker buildx (existing CI) | Build the x86 one-shot Job-runner image | **Reuse existing x86 essentia base layers.** The v4.0/v5.0 x86 agent image already bakes `essentia-tensorflow` + ffmpeg + `fpcalc` + the analysis code. Build the Job image `FROM` that published x86 base and swap only the entrypoint to the one-shot runner (pull presigned URL → analyze → POST result → exit). The cluster is x64, so **no arm64 source build** (unlike Phase 47's `Dockerfile.agent-arm64`). |
| GHCR publish (existing CI) | Publish `*-k8sjob` (or similar) tag | Reuse the existing GHCR publish workflow + `just` delegation. Tag-triggered. The numeric-parity guard from Phase 47 is unnecessary here — same x86 wheel as the production analysis path, already parity-validated. |

## Do We Need Kueue-Specific Python Bindings?

**No.** Submit a normal `batch/v1` `Job` and read the Kueue `Workload` as a generic custom object. Specifics:

- **Submission:** a standard `batch/v1` Job with two additions:
  - `metadata.labels["kueue.x-k8s.io/queue-name"] = "<LocalQueue name>"` (phaze references an operator-configured LocalQueue).
  - `spec.suspend: true` — Kueue requires Jobs start suspended; the controller un-suspends on admission. (Submitting un-suspended bypasses quota; Kueue webhooks re-suspend, but submit it suspended explicitly.)
- **Workload CRD apiVersion:** **`kueue.x-k8s.io/v1beta2`** is the current served + storage version as of **Kueue v0.18** (graduated from v1beta1). **`kueue.x-k8s.io/v1beta1` is now DEPRECATED but still served** (emits a deprecation warning) for backward compatibility — long-lived objects auto-convert only on a write. **Target v1beta2**, and make the apiVersion a config constant so a cluster pinned to an older Kueue (still v1beta1) is a one-line change. Confirm the cluster's served version with `kubectl get --raw /apis/kueue.x-k8s.io` at deploy time.
- **Watching status — two signals, both via kr8s, no bindings:**
  - **Completion:** watch the Job's own conditions — `await job.wait(["condition=Complete","condition=Failed"], timeout=...)`. Simplest, most reliable terminal signal.
  - **Admission / quota visibility (optional but recommended for observability):** read the `Workload`. Find it via the Job UID: `workloads.kueue.x-k8s.io` labeled `kueue.x-k8s.io/job-uid=<job.metadata.uid>`. Its `status.conditions` carry `QuotaReserved` (reason `Pending` vs `Admitted`) and terminal `Finished` (reason `JobFinished`). Surfacing "Pending — insufficient quota" vs "Admitted" makes the AWAITING_CLOUD UI honest about why a long file is still queued.
- **Result reconciliation stays unchanged:** the Job pod POSTs the analysis back to `/api/internal/agent/*` as a registered compute agent (v5.0 machinery), reconciled by `file_id`. The kube watch is a liveness/lifecycle signal, NOT the result channel — the authoritative result arrives over the existing internal HTTP API. This decoupling means a missed watch event never loses a result.

## Installation

```bash
# Control plane (app-server) — NEW deps
uv add kr8s aioboto3

# Job-runner image: NO new pip deps — reuses the existing x86 essentia agent layers.
#   FROM <existing x86 agent base image>; swap ENTRYPOINT to the one-shot runner.
#   Pod downloads via presigned URL using httpx (already present), POSTs via httpx.

# Cluster-side (operator, not a phaze dependency):
#   Install Kueue v0.18.x; create ResourceFlavor / ClusterQueue / LocalQueue (CPU+memory quota only).
```

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| **kr8s** (async, httpx-based) | **kubernetes-asyncio** 36.1.0 | If you need a 1:1 faithful port of the official client's generated API surface (every `*Api` class, full model stubs). Downside for phaze: it is **aiohttp**-based — a second async HTTP stack alongside the project's httpx, doubling TLS/connection-pool config and dependency weight. CRD access is verbose via `CustomObjectsApi`. Choose only if a team already standardizes on the official client's models. |
| **kr8s** | **official `kubernetes`** 36.0.2 | If the code path were synchronous. It is **sync-only** (urllib3/requests) — every call would need `asyncio.to_thread` wrapping to avoid blocking the FastAPI/SAQ event loop, and it has the heaviest dep tree (generated models). Not suitable for an async control plane. |
| **aioboto3** (async, control plane) | **boto3** 1.43.36 in `asyncio.to_thread` | If you want to avoid the `aiobotocore` layer and prefer the most battle-tested sync SDK. S3 staging is low-frequency (one upload per long file), so thread-pool offload is acceptable. Pick this if `aiobotocore`'s exact-pin of botocore causes a resolver conflict. |
| **aioboto3** | **minio** 7.2.20 (MinIO SDK) | If the bucket is specifically MinIO and you want the lightest dependency (urllib3-based, no botocore). It supports `presigned_get_object` / `fput_object` / `remove_object`. Downside: **sync-only** (needs thread-pool offload) and ties the mental model to MinIO even though it speaks generic S3. boto3/aioboto3's `endpoint_url=` is more portable across S3 backends (operator-provided bucket type is unknown/transport-agnostic). |
| presigned GET URL + httpx in the pod | S3 SDK inside the Job image | If the object is too large for a single presigned GET or you need multipart resumable download in the pod. For one-file-per-Job ephemeral staging, presigned URL + httpx GET is simpler and keeps the pod credential-free and SDK-free. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| **A Kueue-specific Python binding / generated client** | Kueue ships no first-party Python client; third-party generated stubs lag the CRD and add maintenance. The Workload is just a custom object. | Generic dynamic-object access via kr8s; pin the `kueue.x-k8s.io/v1beta2` apiVersion as a config constant. |
| **official `kubernetes` (sync) in the async control plane** | Sync/urllib3 blocks the event loop; heaviest generated dep tree. | kr8s (async, httpx). |
| **`kubernetes-asyncio` purely for async** | Pulls **aiohttp** — a parallel HTTP stack to the project's httpx, doubling config surface and deps for no functional gain here. | kr8s reuses the existing httpx stack. |
| **An S3 SDK inside the Job pod image** | Forces long-lived cluster credentials and bloats the image. | Control plane mints a short-lived presigned GET URL; pod fetches with httpx (already present). |
| **Mesh-/transport-specific libraries (Tailscale/WireGuard SDKs)** | Connectivity is intentionally transport-agnostic — phaze consumes operator-provided reachable endpoints (kube API, S3, callback) only. | Plain endpoint URLs in pydantic-settings; no networking code. |
| **Arm64 build tooling for the Job image** | The cluster is x64; the existing essentia x86 wheel runs directly. The Phase 47 from-source arm64 build is not needed. | Reuse the published x86 essentia agent base layers. |
| **Object storage as a data home** | Out-of-scope per PROJECT.md — staging is ephemeral analysis-only (upload → download → delete). | Lifecycle/TTL on the bucket + explicit delete after the result callback; never treat the bucket as canonical. |

## Stack Patterns by Variant

**If the operator's cluster runs Kueue < v0.18 (still v1beta1 storage):**
- Set the Workload apiVersion config constant to `kueue.x-k8s.io/v1beta1`.
- Because phaze reads the Workload dynamically (no compiled stubs), this is a single config change — kr8s adapts to the served version returned by the API.

**If the bucket is MinIO specifically and dependency minimalism dominates:**
- `minio` 7.2.20 in `asyncio.to_thread` is a lighter alternative to aioboto3 (no botocore).
- Still emit a presigned GET URL for the pod; the control-plane SDK choice doesn't change the credential-free pod design.

**If `aiobotocore`'s pinned botocore conflicts with another dep at resolve time:**
- Drop to `boto3` (sync) wrapped in `asyncio.to_thread`. S3 staging frequency (one upload per long file) makes the thread-pool cost negligible.

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| kr8s 0.20.15 | Python 3.13 | `requires-python >=3.9`; pure-async path via `kr8s.asyncio`. Deps (`httpx`, `anyio`, `cryptography`, `pyyaml`) already in phaze — only adds `cachetools`, `httpx-ws`, `python-box`, `python-jsonpath`, `packaging`. Light, no second HTTP stack. |
| kr8s 0.20.15 | Kueue v0.18 (`v1beta2`) | Reads `workloads.kueue.x-k8s.io` dynamically; no codegen. Target v1beta2; v1beta1 still served (deprecated). |
| aioboto3 15.5.0 | Python 3.13 | Pins `aiobotocore[boto3]==2.25.1` (which pins exact botocore/boto3). Self-contained; verify no other dep needs a different botocore. |
| aioboto3 / boto3 | S3-compatible endpoints | Use `endpoint_url=` for non-AWS backends (MinIO, Ceph RGW, B2, Wasabi). `generate_presigned_url` works against all. |
| Kueue v0.18.2 | batch/v1 Job | Job must be submitted with `spec.suspend: true` + `metadata.labels["kueue.x-k8s.io/queue-name"]`. Kueue un-suspends on admission. |
| New x86 Job image | existing x86 essentia base | Same wheel as the production analysis path — already numeric-parity-validated; reuse layers, swap entrypoint only. |

## Integration Points with the Existing Async Stack

- **Submitter is the async control plane** (FastAPI/SAQ on the PostgresQueue broker). A SAQ task on the `controller` queue (routed via the existing `enqueue_router.resolve_queue_for_task`) performs: stage file to S3 → submit suspended Job → watch Job/Workload → mark routed. kr8s `await job.wait(...)` is `anyio`-based and cooperates with the SAQ worker's event loop. Use a bounded `wait(..., timeout=...)`; on timeout, re-poll the Workload status on the next task run rather than holding a worker slot for hours (mirrors v5.0's "stay one ahead" pattern and the windowed-analysis timeout learnings).
- **No Redis dependency added** — broker is Postgres; kube watch state lives on `FileRecord` (AWAITING_CLOUD → submitted/admitted → result-reconciled), not in Redis.
- **Result channel unchanged** — the Job pod POSTs to `/api/internal/agent/*` (bearer token, `agent_id` from token) and reconciles by `file_id`. The kube watch is lifecycle/observability only; a dropped watch never loses a result.
- **Secrets via `_FILE`** — kubeconfig/SA-token, S3 access key/secret, bucket, LocalQueue name, kube API endpoint, and the active-cloud-target selector (local / A1 / K8s) all flow through pydantic-settings, gated by the existing `cloud_burst_enabled` master toggle.
- **Retries via tenacity** — wrap kube submit/watch and S3 upload/delete in existing retry helpers for VPN blips.
- **respx** already in the test stack covers the pod's httpx callback; kr8s and aioboto3 both expose mockable async surfaces (kr8s against a fake API server / recorded responses; aioboto3 via the `botocore` stubber or `moto`) for control-plane unit tests, keeping the 85% coverage gate reachable without a live cluster.

## Sources

- PyPI JSON API — verified latest versions 2026-06-26: `kr8s` 0.20.15, `aioboto3` 15.5.0, `boto3` 1.43.36, `minio` 7.2.20, `kubernetes` 36.0.2, `kubernetes-asyncio` 36.1.0 (HIGH)
- PyPI `requires-dist` — kr8s deps (httpx/anyio/cryptography/pyyaml already in stack); aioboto3 pins `aiobotocore[boto3]==2.25.1` (HIGH)
- Context7 `/kr8s-org/kr8s` — async Job create + `wait(["condition=Complete","condition=Failed"])`, dynamic custom-object get (benchmark 97) (HIGH)
- Context7 `/websites/kueue_sigs_k8s_io` — Workload status conditions (QuotaReserved/Admitted/Finished), `queue-name` label, suspend semantics, job-uid→workload lookup; examples show `apiVersion: kueue.x-k8s.io/v1beta2` (HIGH)
- GitHub `kubernetes-sigs/kueue` releases — latest **v0.18.2**, published 2026-06-26 (HIGH)
- WebSearch (kueue.sigs.k8s.io docs + Red Hat build of Kueue) — v1beta2 is current served+storage version as of v0.18; v1beta1 deprecated but still served, auto-converts on write (MEDIUM-HIGH, multiple sources agree):
  - https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta1/
  - https://github.com/kubernetes-sigs/kueue/releases/tag/v0.18.0
  - https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/ai_workloads/red-hat-build-of-kueue

---
*Stack research for: v6.0 Kubernetes Burst Analysis (Kube/Kueue offload delta)*
*Researched: 2026-06-26*
