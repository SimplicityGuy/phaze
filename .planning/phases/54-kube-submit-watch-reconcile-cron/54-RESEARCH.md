# Phase 54: Kube submit / watch + reconcile cron - Research

**Researched:** 2026-06-27
**Domain:** Kueue-scheduled Kubernetes batch-Job submit + control-plane reconcile (kr8s async), fake-kube testing
**Confidence:** HIGH (kr8s API verified against Context7 `/kr8s-org/kr8s` + `/websites/kr8s` and PyPI; Kueue conditions verified against FEATURES.md which was verified against Context7 `/kubernetes-sigs/kueue`)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01: Cron-only poll — NO live kube watch stream.** Reconcile cron lists/re-reads in-flight Workloads/Jobs each tick; the `/api/internal/agent/*` callback IS the real-time completion signal; the cron is the safety net for eviction / Inadmissible / failure / cleanup. No long-lived watch task.
- **D-02: The `cloud_job` rows are the durable in-flight registry the cron iterates.** The cron finds work by reading `cloud_job` rows in a submitted/in-flight state (the Job name recorded in `kueue_workload` at submit time) and reconciles each against the kube API. Never touches the agent-queue ledger.
- **D-03: Reconcile cadence = every 5 minutes** (`*/5 * * * *`), matching the existing `stage_cloud_window` cron family. Fixed cron, not a config knob.
- **D-04: phaze explicitly deletes the Job after reconcile records the terminal outcome.** A generous `ttlSecondsAfterFinished` is a backstop; deletion happens only *after* the DB reflects the result, so the status read can never lose to GC. TTL only covers the "phaze never reconciled" orphan case.
- **D-05: Reconcile deletes the staged S3 object on no-callback terminal outcomes** (eviction, lost pod, Job `Failed` before POSTing) via the existing Phase 53 `delete_staged_object(file_id)` before/at Job deletion. (Success path still deletes inline in the result callback per Phase 53 D-02.)
- **D-06: Inadmissible → pipeline-UI alert + WARNING log**, driven off a flag on the `cloud_job` row plus a WARNING log line. Healthy `Pending` (quota wait) stays silent.
- **D-07: Inadmissible holds indefinitely and does NOT consume the re-drive cap.** Reconcile alerts and leaves the file held (Job stays suspended/queued) without counting attempts or marking `ANALYSIS_FAILED`.
- **D-08: New `cloud_submit_max_attempts` ControlSettings knob, default ~3**, mirroring `push_max_attempts` (`config.py:421`), resolved via the same env/`_FILE` machinery. Attempt count on the `cloud_job` row. A re-drive = re-stage through the K8s window (fresh Job). After the cap → `ANALYSIS_FAILED` (no cross-target fallback).
- **D-09: Extend `CloudJobStatus` + add `kueue_workload` and `attempts` columns in Phase 54's own migration.** Add new string-backed members for the submit/reconcile lifecycle (submitted/running/succeeded/failed — planner finalizes exact members); only the CHECK-constraint membership list changes. `cloud_phase` left untouched for Phase 55.

### Claude's Discretion (resolved in this research — see body)
- Exact suspended-Job spec details (`backoffLimit`, Kueue-requeue neutralization, `ttlSecondsAfterFinished` value, resource requests). → §Suspended-Job Spec.
- Submit-task shape & idempotency guard ("Job already exists"), deterministic Job-name scheme, `enqueue_router` routing. → §Submit Task & Idempotency.
- kr8s client construction / kubeconfig & namespace surface on `ControlSettings` (`_FILE` secrets). → §kr8s Client & Config.
- Kube conditions → outcomes mapping. → §Status → Outcome Mapping.
- Fake-kube test harness shape. → §Fake-Kube Test Harness.
- Exact new `CloudJobStatus` member names + pipeline-UI alert element. → §Data Model Changes / recommendations.

### Deferred Ideas (OUT OF SCOPE)
- None deferred in discussion. Adjacent work owned by other phases: `stage_cloud_window` K8s branch + `cloud_target` + `enqueue_router` additions + AST guard (**Phase 55**); `cloud_phase` column (**Phase 55**); deploy / RBAC / runbook / `_FILE` wiring / master toggle (**Phase 56**).
- Out of scope per REQUIREMENTS.md: `maximumExecutionTimeSeconds` runaway guard (KSUBMIT-07), cross-target re-route on eviction (KSUBMIT-08), multi-file/elastic Jobs (KSUBMIT-09).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| KSUBMIT-01 | Submit a single **suspended** `batch/v1` Job (`kueue.x-k8s.io/queue-name=<LocalQueue>`, `restartPolicy: Never`, `parallelism: 1`, cpu/memory requests only), idempotently via a deterministic name keyed to `file_id` | §Suspended-Job Spec (full spec dict), §Submit Task & Idempotency (deterministic name + 409 guard via kr8s) |
| KSUBMIT-02 | Submit task returns in seconds and never blocks a worker; reconcile cron owns Workload status, cleanup, re-drive | §Submit Task (one fast kube POST, controller queue), §Reconcile Loop (the `*/5` cron) |
| KSUBMIT-03 | Result authoritative ONLY via out-of-band callback reconciled by `file_id`; dropped/expired watch never loses or duplicates | §Status → Outcome Mapping (callback is truth; cron is safety net, D-01), existing `agent_analysis.put_analysis` is the result write |
| KSUBMIT-04 | Reconcile distinguishes healthy `Pending` from `Inadmissible`; detects success/failure/eviction | §Status → Outcome Mapping (exact condition tuples), §Reconcile Loop |
| KSUBMIT-05 | On failure/eviction re-drive up to a bounded cap → then `ANALYSIS_FAILED`; no cross-target fallback; `backoffLimit`/Kueue requeue neutralized | §Suspended-Job Spec (backoffLimit=0), §Re-drive Cap (`cloud_submit_max_attempts`, D-08), §Kueue-requeue neutralization |
| KSUBMIT-06 | Finished Jobs cleaned up without a TTL-vs-read race; NO `process_file:<id>` ledger seed for K8s files | §TTL-vs-read ordering (D-04 explicit delete), §No-ledger-seed invariant (cloud_job row is the registry, never a SchedulingLedger row) |
</phase_requirements>

## Summary

This phase wires the control plane to a remote x64 Kueue cluster via **kr8s** (async Python kube client). The shape is deliberately narrow and reuses three existing precedents almost verbatim: the **`s3_staging.py` seam pattern** (a pure-SDK module keyed by `file_id`, no ORM imports), the **`cloud_staging.py` orchestration pattern** (DB upsert + enqueue + re-drive loop), and the **`stage_cloud_window` cron pattern** (a narrow `*/5` recovery-only cron registered in `controller.py`).

Three new pieces: (1) a **kube seam module** (`kube_staging.py`) wrapping kr8s — submit a suspended Job, list in-flight Jobs, resolve + read the paired Kueue Workload, delete a Job; (2) a **fast `submit_cloud_job` SAQ task** on the controller queue that does one kube POST and returns; (3) a **`reconcile_cloud_jobs` cron** (`*/5`) that iterates `cloud_job` rows in a submitted/running state, maps Job + Workload conditions to outcomes, owns cleanup (explicit delete after recording the result, per D-04), re-drives failures under a bounded cap, and surfaces Inadmissible. The `/api/internal/agent/*` callback (already built) remains the only authoritative result channel; the cron is purely the safety net.

The single hardest correctness property is the **TTL-vs-read ordering**: resolved by D-04 (phaze deletes the Job *after* the DB reflects the result; TTL is only the orphan backstop). The single hardest test property is doing all of this **without a live cluster**: resolved by a two-layer test strategy — monkeypatch the kube seam with canned status objects for the reconcile/submit business logic, plus respx-stubbed kube REST verbs for the seam module itself (kr8s talks httpx, and the project already standardizes on respx).

**Primary recommendation:** Add `kr8s>=0.20.15` as a control-plane dependency. Build a pure `kube_staging.py` seam (mirror `s3_staging.py`) + a `submit_cloud_job` controller task + a `reconcile_cloud_jobs` `*/5` cron (mirror `stage_cloud_window`). Submit a suspended `batch/v1` Job named `phaze-analyze-<file_id>` with `backoffLimit: 0`, `ttlSecondsAfterFinished: 900`, the `kueue.x-k8s.io/queue-name` label on the Job, requests-only resources; guard re-submit with `exists()` + 409 `ServerError` catch. Reconcile reads Job `status.succeeded`/`Failed` condition and the Workload's `QuotaReserved`/`Admitted`/`Evicted` conditions; delete the Job (and call `delete_staged_object` on no-callback terminal) only after the outcome is committed. Test with monkeypatched seam fakes + respx.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Submit suspended Job (kube POST) | Control plane (app server) | — | Only the control plane holds kube creds; the agent/pod are credential-free (DIST-01). |
| Reconcile Workload/Job status | Control plane (cron) | — | Cron + DB sidecar are control-plane state; the cluster carries no result payload. |
| Result delivery (analysis output) | Pod → control plane HTTP | — | KSUBMIT-03: out-of-band `/api/internal/agent/*` is the only authoritative channel (already built). |
| In-flight registry / re-drive bookkeeping | Control plane DB (`cloud_job`) | — | D-02: the `cloud_job` row, not a kube watch and not the recovery ledger, is the source of in-flight truth. |
| S3 object cleanup on no-callback terminal | Control plane (reconcile) | Bucket lifecycle TTL (backstop) | D-05: reuse Phase 53 `delete_staged_object`; lifecycle TTL is the last-resort net. |
| Quota admission / scheduling | Kueue (cluster) | — | Kueue is an admission gate only; phaze submits + observes, never manages quota objects. |
| Pod-level retry | **Neutralized** (backoffLimit=0) | Control-plane re-drive | KSUBMIT-05: the control plane solely owns retry; infra retry is disabled. |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| kr8s | `>=0.20.15` | Async Kubernetes client (submit/list/get/delete Jobs + Kueue Workloads) | `[VERIFIED: PyPI]` Pure-Python, httpx-based async client (`kr8s.asyncio`); kubectl-familiar object model; first-class CRD support via `new_class` (needed for the Kueue `Workload` CRD). `requires_python >=3.9` — compatible with phaze's 3.14. httpx transport means it tests cleanly with the project's existing **respx**. Repo: github.com/kr8s-org/kr8s. |

### Supporting (already in the project — reuse, no new install)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| respx | `>=0.23.1` | Mock kr8s's httpx kube calls | `[VERIFIED: pyproject.toml]` Already a dev dep; used across `test_job_runner`, `test_s3_upload`, `test_agent_client_*`. The seam-module tests stub the kube REST surface with it. |
| aioboto3 | `>=15.5.0` | S3 delete on no-callback terminal (D-05) | `[VERIFIED: pyproject.toml]` Reconcile calls the existing `s3_staging.delete_staged_object(file_id)` — no new S3 code. |
| SQLAlchemy / asyncpg | `2.0.x` | `cloud_job` reads/writes + Phase 54 migration | `[VERIFIED: codebase]` Standard project ORM; the migration mirrors the Phase 53 `cloud_job` migration. |
| pydantic-settings | (project) | `cloud_submit_max_attempts` + kube config fields via `_FILE` | `[VERIFIED: config.py]` Mirror `push_max_attempts` + the `SECRET_FILE_FIELDS` machinery. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| kr8s | official `kubernetes` / `kubernetes_asyncio` client | `[ASSUMED]` The official async client is heavier, codegen-based, more verbose for CRDs, and historically awkward to mock. kr8s's httpx core + `new_class` CRD support + the project's respx standardization make it the better fit. **kr8s is the assumed choice — confirm with the operator before locking** (Phase 56 RBAC will be written against whatever client is chosen). |
| respx seam-stubbing | live `kind`/`minikube` integration test | A live cluster contradicts the phase mandate ("testable against a fake kube API, no live cluster"). Keep live-cluster validation for Phase 56 deploy. |

**Installation:**
```bash
uv add kr8s            # >=0.20.15, control-plane dependency
```
Place under the control-plane dependency surface only (the agent/pod must stay kube-credential-free, mirroring how aioboto3 is control-plane-only). Keep deps alphabetically sorted in `pyproject.toml` per CLAUDE.md. Respect the repo's 7-day supply-chain cooldown (`uv` `exclude-newer`); kr8s 0.20.15 is well-aged so the floor resolves cleanly.

**Version verification:** `kr8s` latest = **0.20.15**, `requires_python >=3.9` `[VERIFIED: PyPI 2026-06-27 via pip index + pypi.org/pypi/kr8s/json]`.

## Package Legitimacy Audit

> slopcheck was **not available** at research time (pip install failed in sandbox). Per protocol, `kr8s` is tagged `[ASSUMED]` and the planner should gate the install behind a `checkpoint:human-verify` task. Independent corroboration below substantially de-risks it.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| kr8s | PyPI | First release 0.1.0 in 2023; 90+ releases through 0.20.15 | High (widely used; dask/kr8s ecosystem) | github.com/kr8s-org/kr8s (active, documented at docs.kr8s.org) | unavailable | Approved — install behind checkpoint:human-verify |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

Corroboration (compensating for unavailable slopcheck): mature version history (0.1.0→0.20.15), real and active source repo with published docs, `requires_python` metadata present, project URLs (Changelog/Docs/Issues/Repository) all resolve. No postinstall scripts (pure Python wheel).

## kr8s Client & Config

### Client construction (async)
kr8s's async API factory is `kr8s.asyncio.api(...)`. In async mode it is awaited:

```python
# Source: Context7 /websites/kr8s — kr8s.api(url, kubeconfig, serviceaccount, namespace, context)
import kr8s.asyncio

api = await kr8s.asyncio.api(
    url=settings.kube_api_url,           # e.g. https://<tailscale-or-wireguard-endpoint>:6443
    kubeconfig=settings.kube_kubeconfig, # OR a kubeconfig path (mutually-exclusive style)
    namespace=settings.kube_namespace,   # the single namespace phaze is RBAC-scoped to
)
```
`[CITED: docs.kr8s.org — kr8s.api signature]` The control plane runs OUTSIDE the cluster (home server, reaching the API over Tailscale/WireGuard), so it authenticates via a kubeconfig file or `url`+token, NOT in-cluster `serviceaccount`. Build the kube creds via the `_FILE` secret convention (a kubeconfig mounted as a file, or an SA bearer token).

### Config fields to add to `ControlSettings` (mirror `cloud_max_in_flight` / `push_max_attempts`)
| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `cloud_submit_max_attempts` | `int` (gt=0, lt=20) | `3` | D-08; mirrors `push_max_attempts` exactly (`config.py:421`). |
| `kube_api_url` | `str \| None` | `None` | Kube API endpoint (operator-provided reachable endpoint; transport-agnostic per KDEPLOY-03). |
| `kube_namespace` | `str \| None` | `None` | The single namespace phaze submits/lists in. |
| `kube_local_queue` | `str \| None` | `None` | The `kueue.x-k8s.io/queue-name` value stamped on every Job. |
| `kube_job_image` | `str \| None` | `None` | The Phase 52 Job-runner image ref baked into the Job spec. |
| `kube_kubeconfig` / `kube_sa_token` | `SecretStr \| None` | `None` | Add to `SECRET_FILE_FIELDS` so `<VAR>_FILE` siblings auto-resolve (`config.py:345`). |
| `kube_workload_api_version` | `str` | `"kueue.x-k8s.io/v1beta1"` | Workload CRD apiVersion (KDEPLOY-02 lists it as config). |

**Resource requests** (`kube_job_cpu_request`, `kube_job_memory_request`) reuse Phase 52's measured peak RSS. `[ASSUMED]` — confirm the exact measured values against Phase 52's KJOB-03 output before locking.

**Validator scope note:** the model-validator that fail-fasts on missing kube config belongs to `cloud_target="k8s"` (KDEPLOY-02), which is **Phase 55**. In Phase 54 add the fields as **optional** (default `None`); do NOT couple them to `cloud_burst_enabled` yet (that would break the existing Phase 53 cloud-on-but-no-kube deploys). Add only the field surface here.

## Suspended-Job Spec (KSUBMIT-01, KSUBMIT-05)

The one object phaze writes. Build it as a plain dict and hand it to kr8s `Job`:

```python
# Suspended batch/v1 Job — phaze creates exactly this; Kueue auto-creates the paired Workload.
job_manifest = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": f"phaze-analyze-{file_id}",          # deterministic, DNS-1123 (≤63 chars: 14+36=50)
        "namespace": settings.kube_namespace,
        "labels": {
            "kueue.x-k8s.io/queue-name": settings.kube_local_queue,  # ON THE JOB (not the pod template)
            "app.kubernetes.io/managed-by": "phaze",
            "phaze.dev/file-id": str(file_id),        # for list-by-label reconcile fallback
        },
    },
    "spec": {
        "suspend": True,                               # KSUBMIT-01: never starts a pod before Kueue gates it
        "parallelism": 1,
        "completions": 1,
        "backoffLimit": 0,                             # KSUBMIT-05: any pod failure → immediately terminal Failed
        "ttlSecondsAfterFinished": 900,                # D-04 backstop only (3× the 5-min reconcile)
        "template": {
            "spec": {
                "restartPolicy": "Never",              # KSUBMIT-01
                "containers": [{
                    "name": "analyze",
                    "image": settings.kube_job_image,
                    "resources": {
                        "requests": {                  # KSUBMIT-01: requests only — Kueue counts requests for quota
                            "cpu": settings.kube_job_cpu_request,
                            "memory": settings.kube_job_memory_request,
                        },
                    },
                }],
            },
        },
    },
}
```

### Spec decisions (Claude's Discretion, resolved)
- **`backoffLimit: 0`** (not 1). With `restartPolicy: Never` + `backoffLimit: 0`, the **first** pod failure sets Job `status.failed = 1` and the `Failed` condition immediately — no silent in-Job retries. This is the Job-spec half of "control plane solely owns retry" (KSUBMIT-05). `[CITED: FEATURES.md anti-pattern "Rely on Kueue/Job requeue + backoff as the retry mechanism"]`
- **Kueue-requeue neutralization (the honest picture):** there is **no Job-spec field** that disables Kueue's requeue. Kueue requeue/deactivation is governed by *cluster-side* config (`waitForPodsReady`, `podsReadyTimeout`, `backoffLimitCount`) which is **Phase 56's runbook** (single-CQ, no-preemption). Within Phase 54 the neutralization is behavioral: (1) `backoffLimit: 0` kills pod-level retry, and (2) reconcile treats any `Evicted`/`WorkloadInactive` as a **terminal** signal that the control plane handles via its own bounded re-drive — phaze never relies on Kueue to re-admit a failed Workload. Document this split clearly so the planner doesn't search for a non-existent spec flag. `[CITED: FEATURES.md §Kueue Behavior Reference — requeueState → .spec.active=false deactivation]`
- **`ttlSecondsAfterFinished: 900`** (15 min = 3× the `*/5` reconcile). Because D-04 has phaze delete the Job explicitly *after* recording the outcome, the TTL only ever fires in the "phaze never reconciled at all" orphan case; 900s gives a comfortable margin over one missed reconcile tick. Recommend a module constant (`JOB_TTL_SECONDS = 900`), not a config knob — consistent with the fixed `*/5` cron (D-03). `[CITED: FEATURES.md §Dependency Notes — "single most important ordering decision"]`
- **Resources: requests only** per KSUBMIT-01 (Kueue's quota accounting reads requests). **Open tension:** KJOB-03 (Phase 52) calls for a *hard pod memory limit* for OOM safety on multi-hour sets. KSUBMIT-01 says "requests only". These are not strictly contradictory (a limit can coexist with a request). Recommend honoring CONTEXT D's "requests only" as the locked default and flagging the KJOB-03 memory-limit as a planner decision (see Open Questions). `[ASSUMED]`
- **Deterministic name `phaze-analyze-<file_id>`** gives idempotency for free: the same `file_id` always maps to the same Job name → a duplicate submit hits 409. A legitimate re-drive only re-submits *after* reconcile deleted the prior terminal Job (D-04/D-05), so the name is free — no `409` on legitimate re-drive, and no attempt suffix needed. `[CITED: FEATURES.md "Idempotent submission … guard against Job already exists"]`

## Submit Task & Idempotency (KSUBMIT-01, KSUBMIT-02)

A fast controller-queue SAQ task `submit_cloud_job` (built + unit-tested here; **wired into `stage_cloud_window` in Phase 55**, never auto-wired in this phase). One kube POST, returns in seconds (KSUBMIT-02). It mirrors `cloud_staging.stage_file_to_s3`'s upsert-then-act shape.

```python
# Submit + idempotency guard. kr8s ServerError carries the httpx response (status_code) and
# the k8s Status object (reason == "AlreadyExists").
# Source: Context7 /kr8s-org/kr8s object methods (create/exists/delete); kr8s/_exceptions.py ServerError(status, response)
import kr8s
from kr8s.asyncio.objects import Job

async def submit_job(file_id, api) -> str:
    job = Job(job_manifest, api=api)          # construct from dict (namespace taken from manifest)
    try:
        await job.create()                     # POST /apis/batch/v1/namespaces/{ns}/jobs
    except kr8s.ServerError as exc:
        # Idempotent: a prior identical submit already created this Job name.
        if getattr(exc.response, "status_code", None) == 409:
            await job.refresh()                # load the existing object's uid/status
        else:
            raise
    await job.refresh()
    return job.metadata.uid                    # store as the Workload-discovery key
```
- **Routing:** add `submit_cloud_job` to `controller.py` `settings["functions"]` AND to `CONTROLLER_TASKS` in `enqueue_router.py` (it's control-plane work — kube creds live there). Enqueue with the deterministic key `submit_cloud_job:<file_id>` (matches the `s3_upload:<file_id>` idiom). `[VERIFIED: enqueue_router.py CONTROLLER_TASKS]`
- **`cloud_job` write:** on successful submit, set the row `status = SUBMITTED`, `kueue_workload = <job-name>`, and (on a re-drive) increment `attempts`. Mirror the `cloud_staging` `pg_insert(...).on_conflict_do_update` upsert against the unique `file_id` FK so a re-stage is idempotent. `[VERIFIED: cloud_staging.py:81-102]`
- **No-ledger-seed invariant (KSUBMIT-06):** the submit path must write ONLY the `cloud_job` row — it must NOT create a `process_file:<id>` SchedulingLedger row. The `cloud_job` row is the in-flight registry the cron iterates (D-02); a ledger row would let `recover_orphaned_work` re-enqueue the file onto a local agent queue (the CLOUDROUTE-02 hazard). `[VERIFIED: reenqueue.py _get_awaiting_cloud_ids docstring + CONTEXT D-02]`

## Status → Outcome Mapping (KSUBMIT-04)

The reconcile loop reads **two objects per in-flight file**: the **Job** (success/failure, most direct) and the paired **Kueue Workload** (admission state: Pending vs Inadmissible vs Evicted). The Workload apiVersion is `kueue.x-k8s.io/v1beta1`; define it as a CRD class.

```python
# Source: Context7 /kr8s-org/kr8s new_class for CRDs; FEATURES.md §Kueue Behavior Reference
from kr8s.asyncio.objects import new_class

Workload = new_class(kind="Workload", version="kueue.x-k8s.io/v1beta1", namespaced=True)

# Resolve the Workload Kueue created for this Job. DO NOT hard-code its name (it is
# <job-kind>-<job-name>-<hash>). Kueue stamps the Job UID on the Workload as a label.
async def get_workload_for(job_uid, namespace, api):
    wls = [w async for w in kr8s.asyncio.get(
        "workloads", namespace=namespace,
        label_selector={"kueue.x-k8s.io/job-uid": job_uid}, api=api,
    )]
    return wls[0] if wls else None
```
`[ASSUMED]` the exact label key `kueue.x-k8s.io/job-uid` — FEATURES.md says "discoverable by Job UID / label selector" and Context7 KEP-973 notes the owner-ref/uid linkage; **verify the precise label key against the live Kueue version in Phase 56**, and fall back to the owner-reference or the `phaze.dev/file-id` label phaze stamps on its own Job→Workload chain. The phaze-stamped `phaze.dev/file-id` label on the Job is NOT auto-propagated to the Workload by Kueue, so prefer the Kueue job-uid label or owner-ref.

### The exact tuples a reconcile loop matches
`[CITED: FEATURES.md §Kueue Behavior Reference — verified against Context7 /kubernetes-sigs/kueue]`

| Outcome | Object | Field / condition tuple `(type, status, reason)` | phaze action |
|---------|--------|--------------------------------------------------|--------------|
| **Succeeded** | Job | `status.succeeded >= 1` (primary) **or** condition `("Complete", "True", —)` | Record success path; delete Job (D-04). Result already landed via callback. |
| **Failed** | Job | condition `("Failed", "True", —)` **or** `status.failed >= 1` (with `backoffLimit:0`) | No-callback terminal → re-drive under cap (D-08), or `ANALYSIS_FAILED` at cap. Delete S3 object (D-05) + Job. |
| **Queued (healthy)** | Workload | `("QuotaReserved", "False", "Pending")` | **Silent.** Waits indefinitely (D-07 "Pending waits forever"). No cap consumption, no alert. |
| **Inadmissible (misconfig)** | Workload | `("QuotaReserved", "False", "Inadmissible")` | **Loud.** Set `inadmissible` flag on `cloud_job` → pipeline-UI alert (D-06) + WARNING log. Hold; NO cap consumption (D-07). |
| **Quota reserved** | Workload | `("QuotaReserved", "True", —)` | In-flight; keep `status = SUBMITTED/RUNNING`. |
| **Admitted / running** | Workload | `("Admitted", "True", —)` (and Job `.spec.suspend == false`) | Mark `status = RUNNING`; keep waiting for callback/terminal. |
| **Evicted / deactivated** | Workload | `("Evicted", "True", "WorkloadInactive")` (a.k.a. Deactivated / Preempted) | No-callback terminal → re-drive under cap (D-08). Delete S3 object (D-05) + Job. |
| **Finished (cross-check)** | Workload | `("Finished", "True", "JobFinished")` | Terminal marker; cross-check the **Job** status for succeeded-vs-failed (Job is the source of truth). |

**Truth precedence (KSUBMIT-03):** the analysis *result* is authoritative ONLY from the `/api/internal/agent/*` callback reconciled by `file_id` (already built — `agent_analysis.put_analysis` / `report_analysis_failed`). The reconcile cron NEVER writes an analysis result; it only reads kube state to drive **cleanup, re-drive, and alerting**. A Job that shows `Complete` but whose callback already recorded the result is just a cleanup trigger (delete the Job). A Job that shows `Failed`/`Evicted` with NO callback having landed is the no-callback terminal path that triggers re-drive + S3 delete. This separation is what makes "a dropped/expired watch never loses or duplicates a result" true.

## Reconcile Loop (D-01, D-02, D-04, D-05)

`reconcile_cloud_jobs` — a `*/5` cron registered in `controller.py` `cron_jobs` (cron-only; **NOT** added to `CONTROLLER_TASKS`, exactly like `reap_stalled_scans`). It is narrow — in-flight K8s reconcile ONLY — and must carry the same "DO NOT re-add a general auto-advance cron" guard comment as the existing crons. `[VERIFIED: controller.py:215-235]`

Per-tick algorithm:
1. **Find in-flight work from the DB sidecar (D-02):** `SELECT cloud_job WHERE status IN (SUBMITTED, RUNNING)`. The `cloud_job` row — not a watch, not the recovery ledger — is the iteration source.
2. For each row: build the api client, `get` the Job by `kueue_workload` name; resolve its Workload by job-uid.
3. **Map conditions → outcome** (table above).
4. **On a terminal outcome, the ordering is load-bearing (D-04):**
   1. record the outcome in the DB (advance `cloud_job.status`; on no-callback terminal, drive the FileRecord re-drive/`ANALYSIS_FAILED` decision) and **commit**;
   2. on a no-callback terminal (Failed/Evicted/lost), call `s3_staging.delete_staged_object(file_id)` (D-05, idempotent);
   3. **then** `await job.delete()`.
   The status read can never lose to GC because phaze reads + records *before* it deletes; the TTL only covers a Job phaze never reached.
5. **On Inadmissible (D-06/D-07):** set the `inadmissible` flag, WARNING-log, leave the Job in place, do NOT touch `attempts`.
6. **On healthy Pending / QuotaReserved / Admitted:** update `status` (SUBMITTED↔RUNNING) and move on — no action.

```python
# Cron registration (controller.py settings) — mirror stage_cloud_window; narrow, recovery-only.
CronJob(reconcile_cloud_jobs, cron="*/5 * * * *"),  # type: ignore[type-var]
```

### Delete semantics
`await job.delete(propagation_policy="Background")` removes the Job; Kueue garbage-collects the owned Workload. `[CITED: docs.kr8s.org — delete(propagation_policy, grace_period, force)]` Make delete idempotent (swallow `NotFoundError`) so a re-run after a partial tick is safe — mirror the `s3_staging` "missing object is the desired end state" idempotency idiom. `[VERIFIED: s3_staging.py:40-46,194-210]`

## Re-drive Cap (KSUBMIT-05, D-08)

Mirror the Phase 50 `push_max_attempts` re-drive loop exactly. On a no-callback terminal (Failed/Evicted):
- if `cloud_job.attempts < cloud_submit_max_attempts`: increment `attempts`, delete the prior Job + S3 object, then re-stage through the K8s window (a fresh `submit_cloud_job` — same deterministic name, now free). A "re-drive" is a full re-stage, identical to how `cloud_staging.redrive_upload` re-runs `stage_file_to_s3`. `[VERIFIED: cloud_staging.py:129-143]`
- if `attempts >= cap`: mark the FileRecord `ANALYSIS_FAILED` (no cross-target fallback — KSUBMIT-05), delete the Job + S3 object, terminate the `cloud_job` lifecycle.
- **Inadmissible never enters this path** (D-07): it is an operator-config fault, not a transient analysis failure; re-submitting hits the same broken queue. Hold + alert only.

## Data Model Changes (D-09)

Phase 54's own Alembic migration (one migration, scoped to this phase), extending the Phase 53 `cloud_job` table:

| Change | Detail |
|--------|--------|
| Add columns | `kueue_workload: str \| None` (the Kueue/Job name — reserved by 53-CONTEXT D-03), `attempts: int` (default `0`, server_default `'0'`), `inadmissible: bool` (default `False`, drives the D-06 alert). |
| Extend `CloudJobStatus` | Add members for the submit/reconcile lifecycle: **`SUBMITTED = "submitted"`**, **`RUNNING = "running"`**, **`SUCCEEDED = "succeeded"`** (keep existing `UPLOADING`/`UPLOADED`/`FAILED`). `FAILED` is reused for the terminal failure. `[ASSUMED member names — planner finalizes]` |
| Update CHECK constraint | Drop + recreate `ck_cloud_job_status_enum` / `status_enum` with the new membership list `('uploading','uploaded','submitted','running','succeeded','failed')`. The `status` column is `String(16)` — all new members fit (`succeeded`=9). No Postgres enum-type migration (string-backed by design). `[VERIFIED: cloud_job.py:28-62]` |
| Leave untouched | `cloud_phase` — Phase 55's column (D-09). |

**Migration test:** add to `tests/test_migrations/` mirroring the existing migration tests; assert upgrade/downgrade and the new CHECK membership (the project has `test_migration_019_dedupe.py` precedent). `[VERIFIED: tests/ listing]`

## Pipeline-UI Alert (D-06)

The Inadmissible alert surfaces on the existing Phase 34 queue-depth / Phase 44 analyze-observability pipeline DAG dashboard (`src/phaze/routers/pipeline.py` + templates), driven off the `cloud_job.inadmissible` boolean. Recommend a single warning banner/chip ("K8s Jobs not admitting — check LocalQueue config") shown when any `cloud_job` row has `inadmissible = true`, plus the WARNING log line in reconcile. Keep healthy `Pending` invisible (no chip). `[ASSUMED exact element — planner picks the chip/banner to match the existing dashboard vocabulary]`

## Fake-Kube Test Harness (Claude's Discretion, resolved)

**Recommendation: a two-layer strategy** — it gives full coverage without a live cluster and matches how the project already tests the S3 leg (moto server for the `s3_staging` seam; fakes for `cloud_staging` orchestration).

### Layer 1 — Reconcile / submit business logic: monkeypatch the kube seam
Put all kr8s calls behind a thin `kube_staging.py` module (mirror `s3_staging.py`: pure, keyed by `file_id`, NO ORM imports) exposing `submit_job`, `list_inflight_jobs`, `get_job`, `get_workload_for`, `delete_job`. Test `reconcile_cloud_jobs` and `submit_cloud_job` by monkeypatching those functions to return **canned status objects**, simulating each transition. This is where the high-value state-machine coverage lives and it runs with zero HTTP.

```python
# Simulate a Kueue Workload condition set with a plain object (kr8s exposes .status as a dict).
from types import SimpleNamespace

def fake_workload(*conditions):  # conditions: list of (type, status, reason)
    return SimpleNamespace(status={"conditions": [
        {"type": t, "status": s, "reason": r} for (t, s, r) in conditions
    ]})

PENDING      = fake_workload(("QuotaReserved", "False", "Pending"))
INADMISSIBLE = fake_workload(("QuotaReserved", "False", "Inadmissible"))
ADMITTED     = fake_workload(("QuotaReserved", "True", ""), ("Admitted", "True", ""))
EVICTED      = fake_workload(("Evicted", "True", "WorkloadInactive"))

def fake_job(succeeded=0, failed=0):
    return SimpleNamespace(status={"succeeded": succeeded, "failed": failed},
                           metadata=SimpleNamespace(uid="uid-1", name="phaze-analyze-..."))
```
Drive the transition sequence Pending→Admitted→Running→Succeeded (and the eviction / Inadmissible / Failed branches) by having the monkeypatched `get_job`/`get_workload_for` return the next canned object per call.

### Layer 2 — The seam module itself: respx-stub the kube REST surface
kr8s talks to the API server over **httpx**, and the project already standardizes on respx (`test_job_runner`, `test_s3_upload`, `test_agent_client_*`). Stub the verbs the seam touches:
- `POST /apis/batch/v1/namespaces/{ns}/jobs` → `201` (create) and `409` with a k8s `Status{reason:"AlreadyExists"}` body (idempotency path)
- `GET /apis/batch/v1/namespaces/{ns}/jobs/{name}` → Job status JSON
- `GET /apis/kueue.x-k8s.io/v1beta1/namespaces/{ns}/workloads?labelSelector=...` → Workload list
- `DELETE /apis/batch/v1/namespaces/{ns}/jobs/{name}` → `200`; also a `404` case to prove delete idempotency

**respx friction to plan for:** kr8s performs API **discovery** on first use (`GET /api`, `/apis`, `/apis/{group}/{version}`). The respx fixture must stub those discovery endpoints too (a small canned discovery doc), or construct the `Api` with explicit settings to minimize discovery. Build this as one shared `kube_respx` conftest fixture so every seam test reuses it. `[VERIFIED: kr8s/_exceptions.py — ServerError(status, response: httpx.Response); kr8s uses httpx]`

This keeps the heavy/fiddly HTTP-fidelity tests small and focused on the seam, while the state-machine logic (the actual risk) gets exhaustive, fast, HTTP-free coverage.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Talk to the kube API | Raw httpx + hand-built REST paths + auth | kr8s `kr8s.asyncio` | Discovery, auth (kubeconfig/SA/token), CRD handling, retries, watch — all solved; matches the FEATURES.md design. |
| Read Kueue Workload CRD | A bespoke CRD client | kr8s `new_class(kind="Workload", version="kueue.x-k8s.io/v1beta1")` | First-class CRD object model with `.get`/`.list`/`.status`. |
| In-flight tracking | A new table or a kube watch cache | The existing `cloud_job` sidecar (D-02) | Survives restarts; never touches the agent-queue ledger (KSUBMIT-06). |
| S3 cleanup on eviction | New delete logic | `s3_staging.delete_staged_object(file_id)` (Phase 53 D-02) | Idempotent, already swallows already-absent errors (D-05). |
| Re-drive / attempts loop | New retry machinery | Mirror `cloud_staging.redrive_upload` + `push_max_attempts` shape (D-08) | Battle-tested bounded-retry precedent. |
| Job retry | `backoffLimit > 0` / Kueue requeue | Control-plane re-drive (KSUBMIT-05) | FEATURES.md anti-pattern: infra retry conflates with app retry. |

**Key insight:** every moving part of this phase has a direct in-repo precedent (`s3_staging` seam, `cloud_staging` orchestration, `stage_cloud_window` cron, `push_max_attempts` re-drive). The only genuinely new thing is the kr8s seam — keep it as thin and pure as `s3_staging.py`.

## Common Pitfalls

### Pitfall 1: TTL-vs-read race
**What goes wrong:** `ttlSecondsAfterFinished` GCs the finished Job (and its Workload) before reconcile reads the terminal status → the file looks stuck/unknown forever.
**Why it happens:** TTL shorter than (or comparable to) the reconcile interval.
**How to avoid:** D-04 — phaze deletes the Job *after* committing the outcome; TTL (900s) is only the never-reconciled backstop. Order in code: record+commit → S3 delete (no-callback path) → Job delete.
**Warning signs:** a test where the Job 404s on the reconcile read; a `cloud_job` stuck in RUNNING with no terminal.

### Pitfall 2: Seeding a `process_file` ledger row for a K8s file (the CLOUDROUTE-02 hazard)
**What goes wrong:** `recover_orphaned_work` later replays the ledger row and re-enqueues the long file onto a *local agent* queue — analyzing it on the wrong target.
**Why it happens:** copying the local/agent enqueue idiom (which writes a SchedulingLedger row) into the kube submit path.
**How to avoid:** KSUBMIT-06 — the submit path writes ONLY the `cloud_job` row. No `SchedulingLedger`. The cron reconciles off `cloud_job` (D-02).
**Warning signs:** a `scheduling_ledger` row with `function="process_file"` for a file that also has a `cloud_job`.

### Pitfall 3: Treating healthy `Pending` as a failure
**What goes wrong:** a file queued behind quota for hours gets timed-out / re-driven / failed prematurely, burning the re-drive cap.
**Why it happens:** conflating `QuotaReserved=False, reason=Pending` (normal) with a fault.
**How to avoid:** match the exact `(type,status,reason)` tuples (table above). `Pending` is silent and waits forever (D-07); only `Inadmissible` is loud; only `Failed`/`Evicted` consume the cap.

### Pitfall 4: Hard-coding the Workload name
**What goes wrong:** the Workload is `<job-kind>-<job-name>-<hash>`; a guessed name 404s and admission state is never read.
**How to avoid:** resolve via the Kueue job-uid label / owner-reference, not a constructed name (FEATURES.md table-stakes note).

### Pitfall 5: respx tests failing on kr8s API discovery
**What goes wrong:** the first kr8s call triggers `GET /api` / `/apis` discovery that isn't stubbed → tests fail with confusing connection errors.
**How to avoid:** stub discovery endpoints in a shared fixture, or push the HTTP-level tests to the thin seam only and cover logic via monkeypatched fakes (Layer 1).

## Runtime State Inventory

> This is a greenfield additive phase (new task + new cron + new columns), not a rename/refactor. Inventory included for completeness.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `cloud_job` table gains 3 columns + 3 new status enum members | Alembic migration (D-09) — code + schema change. |
| Live service config | Kueue cluster objects (RF/CQ/LQ) are operator-owned (Phase 56 runbook), NOT created by phaze | None in Phase 54 — phaze references LocalQueue by name only. |
| OS-registered state | New `*/5` `reconcile_cloud_jobs` cron in the controller worker (SAQ, in-process) | Registered in `controller.py` `cron_jobs`; survives restart via Postgres broker. |
| Secrets/env vars | New kube config fields (`kube_*`) + `cloud_submit_max_attempts`; kube creds via `_FILE` | Add fields (optional in Phase 54); add kube creds to `SECRET_FILE_FIELDS`. Full wiring/RBAC is Phase 56. |
| Build artifacts | New control-plane dependency `kr8s` | `uv add kr8s` → `uv.lock` updates; control-plane image rebuild. |

## Code Examples

### Listing in-flight Jobs by label (reconcile discovery fallback)
```python
# Source: Context7 /websites/kr8s — async list by label_selector
import kr8s.asyncio
async for job in kr8s.asyncio.get(
    "jobs", namespace=settings.kube_namespace,
    label_selector={"app.kubernetes.io/managed-by": "phaze"}, api=api,
):
    print(job.name, job.status.get("succeeded"), job.status.get("failed"))
```
(Primary discovery is the `cloud_job` sidecar per D-02; this label-list is a cross-check / orphan-Job sweep.)

### Object lifecycle methods (async)
```python
# Source: Context7 /kr8s-org/kr8s — Object Methods (Async)
await job.refresh()             # GET — reload .status/.metadata
exists = await job.exists()     # belt-and-suspenders idempotency before create
await job.delete(propagation_policy="Background")  # DELETE — Kueue GCs the owned Workload
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Long-lived kube **watch** as the completion signal | Cron-only reconcile + out-of-band callback (D-01) | This phase | No reconnect/expired-stream handling; sub-minute responsiveness buys nothing for hour-scale work. |
| Infra-owned retry (`backoffLimit`/Kueue requeue) | Control-plane bounded re-drive (KSUBMIT-05) | v6.0 | App owns the retry budget; infra retry neutralized. |
| TTL-only Job cleanup | Explicit delete-after-record + TTL backstop (D-04) | This phase | Status read can never lose to GC. |

**Deprecated/outdated:** none specific to this phase. kr8s 0.20.x is current; pin `>=0.20.15`.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | kr8s is the right client (vs official `kubernetes_asyncio`) | Standard Stack | LOW — both work; kr8s is lighter + tests cleaner. Confirm before Phase 56 RBAC is written against it. |
| A2 | The Workload-discovery label is `kueue.x-k8s.io/job-uid` | Status Mapping | MEDIUM — if the key differs, reconcile can't find the Workload (admission state unreadable). Verify against the live Kueue version; owner-ref fallback exists. |
| A3 | `ttlSecondsAfterFinished: 900` is comfortably > one reconcile tick | Suspended-Job Spec | LOW — D-04 makes explicit delete primary; TTL only orphan-backstop. |
| A4 | Resources are "requests only" (KSUBMIT-01) despite KJOB-03's hard memory limit | Suspended-Job Spec | MEDIUM — no OOM ceiling on a multi-hour pod could OOM-kill; see Open Question 1. |
| A5 | New `CloudJobStatus` members = SUBMITTED/RUNNING/SUCCEEDED (+ existing FAILED) | Data Model | LOW — planner finalizes names; only the CHECK list changes. |
| A6 | kr8s `ServerError.response.status_code == 409` is the AlreadyExists signal | Submit Task | LOW — verified `ServerError(status, response: httpx.Response)` in `kr8s/_exceptions.py`; `.status.reason == "AlreadyExists"` is the alternative check. |
| A7 | Phase 52's measured peak-RSS values are available for `kube_job_memory_request` | kr8s Config | MEDIUM — if not measured, request sizing is a guess; confirm against KJOB-03 output. |

## Open Questions

1. **KSUBMIT-01 "requests only" vs KJOB-03 "hard pod memory limit".**
   - What we know: Kueue counts *requests* for quota; KSUBMIT-01 says requests only; KJOB-03 (Phase 52) wants a hard memory limit so a multi-hour set never OOMs.
   - What's unclear: whether the Job spec should also set `resources.limits.memory`.
   - Recommendation: honor CONTEXT "requests only" as the locked default; raise the KJOB-03 limit as an explicit planner decision (a memory limit can coexist with the request without violating Kueue accounting). Default to no limit unless Phase 52's measured peak RSS argues otherwise.

2. **Exact Workload→Job linkage label.** Resolve `kueue.x-k8s.io/job-uid` (A2) against the deployed Kueue version, or implement an owner-reference walk as the robust fallback. Low effort to support both.

3. **kr8s async object construction ergonomics.** Context7 shows both `Job(dict, api=api)` (sync construct, await methods) and an `await Job(...)` form in one example. Recommend the `Job(dict, api=api)` + `await job.create()` form (consistent across the docs); verify the exact constructor signature against 0.20.15 at implementation time (trivial to confirm in a one-line repl/test).

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| kr8s (PyPI) | submit/reconcile seam | ✗ (not yet a dep) | 0.20.15 available | `uv add kr8s` |
| Live Kueue cluster | runtime (prod) | ✗ (intentional) | — | Fake-kube tests (respx + monkeypatched seam) — phase mandate |
| respx | seam HTTP tests | ✓ | >=0.23.1 | — |
| aioboto3 / moto | S3 delete reuse + tests | ✓ | >=15.5.0 / >=5.1.0 | — |
| Postgres / asyncpg | `cloud_job` + migration | ✓ | project | — |

**Missing dependencies with no fallback:** none (live cluster is intentionally out of scope for this phase).
**Missing dependencies with fallback:** `kr8s` — install via `uv add kr8s` (behind the package-legitimacy checkpoint).

## Validation Architecture

> nyquist_validation assumed enabled (no explicit `false` found). VALIDATION.md should be generated from this section.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`asyncio_mode = "auto"`) `[VERIFIED: pyproject.toml:120-125]` |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`; `testpaths = ["tests"]` |
| Quick run command | `uv run pytest tests/test_tasks/test_reconcile_cloud_jobs.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (85% min per CLAUDE.md) |

### Critical State Transitions & Edge Cases — MUST have coverage
| Req | Behavior under test | Test type | Automated command (target paths) | Exists? |
|-----|---------------------|-----------|----------------------------------|---------|
| KSUBMIT-01 | Submit builds a suspended Job: `suspend:true`, `parallelism:1`, `backoffLimit:0`, `restartPolicy:Never`, queue-name label on Job, requests-only, `ttlSecondsAfterFinished` set | unit | `uv run pytest tests/test_tasks/test_submit_cloud_job.py -x` | ❌ Wave 0 |
| KSUBMIT-01 | **Idempotent re-submit:** second submit with same `file_id` hits 409 → no error, no duplicate `cloud_job` row | unit | `tests/test_tasks/test_submit_cloud_job.py::test_resubmit_409_is_idempotent` | ❌ Wave 0 |
| KSUBMIT-02 | Submit task returns without awaiting analysis (one kube POST; routes via `enqueue_router` to controller queue) | unit | `tests/test_tasks/test_submit_cloud_job.py::test_fast_return_controller_queue` | ❌ Wave 0 |
| KSUBMIT-03 | Reconcile NEVER writes an analysis result; callback is the only result writer (reconcile on a `Complete` Job with prior callback only cleans up) | unit | `tests/test_tasks/test_reconcile_cloud_jobs.py::test_reconcile_never_writes_result` | ❌ Wave 0 |
| KSUBMIT-04 | `Pending` → silent hold (no alert, no cap) | unit | `...::test_pending_is_silent` | ❌ Wave 0 |
| KSUBMIT-04 | `Inadmissible` → `inadmissible` flag set + WARNING log, **no** cap consumption (D-07) | unit | `...::test_inadmissible_alerts_without_cap` | ❌ Wave 0 |
| KSUBMIT-04 | Transition sequence Pending→Admitted→Running→Succeeded reconciles correctly each tick | unit | `...::test_admission_to_success_sequence` | ❌ Wave 0 |
| KSUBMIT-04/05 | `Evicted (WorkloadInactive)` → no-callback terminal → re-drive | unit | `...::test_eviction_triggers_redrive` | ❌ Wave 0 |
| KSUBMIT-05 | Failure re-driven up to `cloud_submit_max_attempts`, then `ANALYSIS_FAILED` (no cross-target fallback) | unit | `...::test_max_attempts_cap_then_analysis_failed` | ❌ Wave 0 |
| KSUBMIT-05 | Inadmissible hold does NOT increment `attempts` even across many ticks | unit | `...::test_inadmissible_never_consumes_cap` | ❌ Wave 0 |
| KSUBMIT-06 | **TTL-vs-read ordering:** outcome recorded + committed BEFORE `job.delete()`; S3 delete before Job delete on no-callback terminal | unit | `...::test_delete_after_record_ordering` | ❌ Wave 0 |
| KSUBMIT-05/D-05 | No-callback terminal calls `delete_staged_object(file_id)`; success path does NOT (callback already deleted) | unit | `...::test_s3_delete_only_on_no_callback_terminal` | ❌ Wave 0 |
| KSUBMIT-06 | **No-ledger-seed invariant:** submit writes a `cloud_job` row and NO `SchedulingLedger` `process_file` row | unit | `tests/test_tasks/test_submit_cloud_job.py::test_no_process_file_ledger_seed` | ❌ Wave 0 |
| KSUBMIT-06 | `delete_job` idempotent: a 404 on delete is swallowed | unit | `tests/test_services/test_kube_staging.py::test_delete_idempotent_404` | ❌ Wave 0 |
| KSUBMIT-01 | Seam: create POST returns 201; 409 raises `ServerError` with `response.status_code==409` | unit (respx) | `tests/test_services/test_kube_staging.py::test_create_and_conflict` | ❌ Wave 0 |
| D-09 | Migration upgrade/downgrade + CHECK membership includes new status members | unit | `uv run pytest tests/test_migrations/ -k cloud_job` | ❌ Wave 0 |
| KSUBMIT-06 | AST/import-boundary: `kube_staging.py` has NO ORM imports (mirror `s3_staging` purity) | unit | `tests/test_*_split.py` style import-boundary test | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_tasks/test_reconcile_cloud_jobs.py tests/test_tasks/test_submit_cloud_job.py tests/test_services/test_kube_staging.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** full suite green + ≥85% coverage + `pre-commit run --all-files` (ruff/mypy/bandit) before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_services/test_kube_staging.py` — seam (respx): create/409, get, list-by-label, delete/404. Needs a shared `kube_respx` fixture stubbing kr8s discovery endpoints.
- [ ] `tests/test_tasks/test_submit_cloud_job.py` — submit spec, idempotency, fast-return, no-ledger-seed.
- [ ] `tests/test_tasks/test_reconcile_cloud_jobs.py` — the full condition→outcome state machine (the highest-value coverage), via monkeypatched seam fakes.
- [ ] `tests/test_migrations/` entry for the Phase 54 `cloud_job` migration.
- [ ] `conftest` fixtures: `kube_respx` (discovery + verbs) and canned Workload/Job factories (`fake_workload`/`fake_job`).
- [ ] Dependency install: `uv add kr8s` (behind package-legitimacy checkpoint).

## Project Constraints (from CLAUDE.md)

- Python **3.14 exclusively**; `uv` only — prefix every command with `uv run` (never bare `pip`/`pytest`/`mypy`). `uv add kr8s` for the new dep.
- Ruff (line length 150, `target-version py313`) + mypy strict (excludes `tests/`); type hints on all functions; double quotes.
- `pyproject.toml` section + dependency ordering: alphabetical deps; `[build-system]→[project]→[project.scripts]→[tool.*]→[dependency-groups]`.
- **85% coverage min**, Codecov with service flags; patch target 80%.
- Pre-commit hooks (frozen SHAs) must pass — including the local `uv run mypy .` hook, bandit (`-x tests -s B608`), shellcheck, yamllint. Never `--no-verify`.
- **PR per phase** on a worktree branch (current branch `gsd/phase-54-...`); no direct main commits; commit frequently during execution.
- Keep service READMEs + docs updated alongside code; keep `scripts/update-project.sh` current if adding a service.
- Supply-chain cooldown: 7-day `exclude-newer` window — kr8s 0.20.15 is well-aged, resolves cleanly.

## Sources

### Primary (HIGH confidence)
- Context7 `/kr8s-org/kr8s` — async object methods (create/exists/refresh/delete), `new_class` CRD definition, async list by label_selector, dynamic CRD get.
- Context7 `/websites/kr8s` — `kr8s.api(url, kubeconfig, serviceaccount, namespace, context)` signature, `async_get` signature, object `delete(propagation_policy, grace_period, force)`.
- PyPI `kr8s` JSON (pypi.org/pypi/kr8s/json) — version 0.20.15, `requires_python >=3.9`, project URLs, release history `[VERIFIED 2026-06-27]`.
- `kr8s/_exceptions.py` (GitHub main) — `ServerError(status, response: httpx.Response)`; exception set `NotFoundError`/`APITimeoutError`/`ServerError` `[VERIFIED via raw.githubusercontent.com]`.
- `.planning/research/FEATURES.md` — Kueue status-signals table, TTL-vs-read ordering hazard, anti-patterns (verified against Context7 `/kubernetes-sigs/kueue`).
- Codebase: `cloud_job.py`, `cloud_staging.py`, `s3_staging.py`, `enqueue_router.py`, `controller.py`, `config.py`, `reenqueue.py`, `agent_analysis.py`, `pyproject.toml`, `tests/` listing `[VERIFIED: Read/grep]`.

### Secondary (MEDIUM confidence)
- Workload-discovery label `kueue.x-k8s.io/job-uid` — inferred from FEATURES.md "discoverable by Job UID / label selector" + Context7 KEP-973 notes; verify against live Kueue (A2).

### Tertiary (LOW confidence)
- kr8s async constructor `await Job(...)` vs `Job(dict)` ergonomics — one Context7 example shows `await Job(...)`; recommend the `Job(dict, api=api)` form and confirm at implementation (Open Question 3).

## Metadata

**Confidence breakdown:**
- Standard stack (kr8s): HIGH — version + API verified via Context7 + PyPI + source.
- Kueue condition mapping: HIGH — FEATURES.md table verified against Context7 `/kubernetes-sigs/kueue`; one MEDIUM item (Workload-link label, A2).
- Suspended-Job spec: HIGH — fields are standard batch/v1 + Kueue label; one MEDIUM tension (requests-vs-limit, A4).
- Test harness: HIGH — two-layer strategy matches existing repo patterns (moto-for-seam + fakes-for-logic); respx already in use.
- Pitfalls / architecture: HIGH — every piece has an in-repo precedent.

**Research date:** 2026-06-27
**Valid until:** 2026-07-27 (kr8s/Kueue are stable; re-verify the Workload-link label and kr8s constructor form at implementation).
