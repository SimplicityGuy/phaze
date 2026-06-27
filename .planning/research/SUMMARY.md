# Project Research Summary

**Project:** phaze — v6.0 Kubernetes Burst Analysis
**Domain:** Adding a remote Kueue/Kubernetes offload path to an existing async Python music-analysis pipeline
**Researched:** 2026-06-26
**Confidence:** HIGH

## Executive Summary

v6.0 is a tightly-scoped additive milestone: Kubernetes becomes a *third routing target* alongside the existing local and v5.0 OCI A1 targets. The execution model changes from "persistent SAQ-draining compute agent" to "ephemeral, quota-scheduled Kueue batch Job submitted per long file," but the control-plane choreography is structurally identical to v5.0. The duration router, AWAITING_CLOUD hold, advisory-locked window cron, scheduling ledger, and out-of-band result callback over `/api/internal/agent/*` are all reused verbatim. The single live-seam edit is one new branch inside `stage_cloud_window` gated by a `cloud_target` config setting. Stack delta versus v5.0 is exactly two new pip dependencies on the control plane (`kr8s` for async Kubernetes API access, `aioboto3` for S3 presigning and cleanup) and zero new pip deps in the Job image, which reuses the published x86 essentia base layers with a swapped entrypoint.

Kueue is an admission/quota gate only — it carries no result payload. The authoritative result channel is unchanged: the pod POSTs analysis output to `/api/internal/agent/analysis/{file_id}` as a registered compute agent, reconciled by `file_id`, using the same idempotent upsert `put_analysis` already in production. This decoupling is architecturally load-bearing: a dropped Kubernetes watch never loses a result. The watch (via kr8s) and a periodic `reconcile_k8s_jobs` cron handle lifecycle, cleanup, and eviction detection only. Phases 52–54 are each independently unit-testable without a live cluster (respx/moto/fake kube API), keeping the 85% coverage gate reachable.

The most critical integration constraint is DIST-01 (CI-enforced): the application server has no media mount and physically cannot upload file bytes to S3. The control plane presigns S3 URLs (aioboto3) but the **file-server agent** PUT-uploads bytes via httpx to a presigned PUT URL — mirroring how v5.0's `push_file` moved bytes via rsync. The Job pod GET-downloads via a presigned GET URL using httpx already present in the image, keeping the pod and the agent both credential-free. aioboto3 lives exclusively on the control plane. The top recurring pitfall class from the research — stranding files through a missed watch, a race between TTL and reconcile, or a presigned URL expiring while the Job waits behind quota — all resolve to the same root fix: make the out-of-band callback the only authoritative terminal signal, and treat every watch/reconcile result as supplementary lifecycle bookkeeping.

## Key Findings

### Recommended Stack

The v6.0 stack delta versus v5.0 is deliberately minimal. Two new control-plane dependencies: `kr8s` 0.20.15 (async, built on httpx + anyio, both already in the phaze dep tree — no second HTTP stack unlike `kubernetes-asyncio`'s aiohttp) and `aioboto3` 15.5.0 (async S3, wraps botocore, works against any S3-compatible endpoint via `endpoint_url=`). Kueue has no Python binding worth adding — a normal `batch/v1` Job with the `kueue.x-k8s.io/queue-name` label is all phaze submits; the Workload CRD is read dynamically via kr8s generic object access with `kueue.x-k8s.io/v1beta2` as a config constant (v1beta1 still served but deprecated as of Kueue v0.18). All existing helpers — httpx, tenacity, pydantic-settings `_FILE`, respx — are reused without change.

**Core technologies (new):**
- `kr8s` 0.20.15: async Kubernetes client — submit suspended Job, find Workload, read status, delete Job; reuses existing httpx/anyio dep tree
- `aioboto3` 15.5.0: async S3 client on the control plane — presign PUT/GET, delete object; `endpoint_url=` for non-AWS backends; credentials via `_FILE` secrets
- `httpx` (existing): byte transfer in the file-server agent (PUT to presigned URL) and in the Job pod (GET from presigned URL) — no S3 SDK on either
- `Kueue` v0.18.2 (cluster-side, not a pip dep): admission/quota controller; phaze references a configured LocalQueue name only

### Expected Features

v6.0 scope covers the minimum happy path that safely routes long files to the cluster and prevents stranding, plus a clearly deferred observability tier.

**Must have (table stakes — the "happy path + don't-strand-files" set):**
- Submit a `spec.suspend: true` `batch/v1` Job with `kueue.x-k8s.io/queue-name` label and `restartPolicy: Never` + low `backoffLimit`
- Resolve and watch the auto-created Workload via Job UID (not a guessed name) to detect admission, success, and failure
- Tolerate indefinite `QuotaReserved=False, reason=Pending` as a normal queued state — never time it out as failure
- Distinguish `Inadmissible` (misconfigured LocalQueue — surface immediately) from `Pending` (healthy quota wait)
- Out-of-band result POST from pod to `/api/internal/agent/*`, reconciled by `file_id` (reuses v5.0 machinery entirely)
- Periodic orphan/timeout reconcile loop (watch as fast path + cron as safety net) with TTL-vs-read ordering handled
- Idempotent submission (deterministic Job name keyed to `file_id`) and idempotent callback (`put_analysis` ON CONFLICT)
- `ttlSecondsAfterFinished` set comfortably longer than the reconcile interval (or delete-on-reconcile with long TTL as backstop)
- LocalQueue name, kubeconfig/SA-token, S3 credentials, bucket, and `cloud_target` selector via `_FILE` config under `cloud_burst_enabled`
- S3 object cleanup on ALL terminal outcomes (success, failure, eviction, re-route) — not just success — plus a bucket lifecycle TTL backstop

**Should have (v6.x — add after happy path proven):**
- `maximumExecutionTimeSeconds` on the Workload as a cluster-side runaway guard (measured once typical wall-clock is known)
- Eviction/deactivation detection → automatic re-route to A1/local rather than terminal-fail
- Pipeline dashboard admission-state cards (queued-behind-quota / admitted / running / finished) driven by `cloud_phase`

**Defer (likely never for this tool):**
- Priority classes, preemption, fair-sharing, cohorts — operator manages quota; phaze doesn't own cluster policy
- MultiKueue / multi-cluster dispatch — only one cluster planned
- Elastic/partial-admission jobs — each Job analyzes exactly one file at `parallelism: 1`
- phaze creating ResourceFlavor / ClusterQueue / LocalQueue — cluster-admin setup only
- GPU/Coral accelerator resource requests — essentia analysis is CPU-bound; cluster targets `cpu`/`memory`

### Architecture Approach

v6.0 is a third branch at exactly one existing seam. `stage_cloud_window` (the sole, advisory-locked staging entry for any cloud pipeline) gains a `cloud_target` branch: when `cloud_target == "k8s"`, it enqueues `upload_file_s3` on the file-server agent queue instead of `push_file`. The window math (`COUNT(PUSHING+PUSHED)`, `cloud_max_in_flight`, FIFO SKIP LOCKED) is reused verbatim. `PUSHING`/`PUSHED` are reused as generic in-flight states — no new FileRecord state is added. Kueue admission phase (Pending / Admitted / Finished) goes in a non-state nullable `cloud_phase` field the reconcile cron writes. The duration router, `put_analysis` callback, scheduling ledger, and `recover_orphaned_work` are all unchanged.

DIST-01 governs the byte-transfer design: the control plane (aioboto3) presigns a PUT URL, passes it to the file-server agent, the agent httpx-PUTs the bytes, callbacks `report_s3_uploaded`, the control plane presigns a GET URL, and `submit_k8s_job` bakes that URL into the Job pod env. The pod httpx-GETs the file, analyzes it, and httpx-POSTs the result back. The compute-agent bearer token is injected via a cluster Secret (not inline env) so it never appears in manifests.

**Major new components:**
1. `services/object_staging.py` — aioboto3 wrapper: presign PUT/GET, delete object; mockable via moto/stubber
2. `services/k8s_client.py` — kr8s wrapper: build suspended labeled Job, create, find Workload by job-uid, read status, delete; Workload apiVersion as config constant
3. `tasks/upload_file_s3.py` — agent task: httpx PUT bytes to presigned PUT URL; POST `uploaded` callback; registered in `AGENT_TASKS`; stays Postgres-free (import-boundary enforced)
4. `routers/agent_s3.py` — control callbacks for S3 leg: `report_s3_uploaded` / `report_s3_mismatch` (analogue of `agent_push.py`); presign GET; enqueue `submit_k8s_job`
5. `tasks/submit_k8s_job.py` — controller task: presign GET → build + submit suspended Job → record `cloud_job_name/uid` + `cloud_object_key` → `PUSHING → PUSHED`; returns in seconds, never waits for analysis
6. `tasks/reconcile_k8s_jobs.py` — controller cron (`*/2`): poll K8s-in-flight files; cleanup, re-route, eviction/stuck handling, `cloud_phase` updates; idempotent sweep
7. `Dockerfile.k8sjob` + `phaze/cli/k8s_runner.py` — x86 one-shot image FROM existing x86 essentia base; entrypoint: httpx GET → essentia one-shot (windowed) → httpx PUT `/analysis/{file_id}` → exit; zero new pip deps
8. Alembic migration — sidecar `cloud_job` table (preferred over widening `files`): `cloud_job_name/uid`, `cloud_object_key`, `cloud_phase`, `cloud_submitted_at`; no FileState enum change
9. Config additions — `cloud_target` Literal, kube endpoint + kubeconfig/SA-token `_FILE`, LocalQueue name, Workload apiVersion, S3 endpoint/bucket/credentials `_FILE`, `k8s_inflight_timeout_sec`; extend `SECRET_FILE_FIELDS`

**Modified (low risk, additive):** `stage_cloud_window` (one branch), `enqueue_router` frozensets (add new task names), `tasks/controller.py` (register new tasks + cron), `tasks/agent_worker.py` (register `upload_file_s3`), `config.py` (new settings + model validator)

**Unchanged (proof the seam is right):** `_route_discovered_by_duration`, `put_analysis`, `scheduling_ledger`, `recover_orphaned_work`, `cloud_burst_enabled` toggle, window math, advisory lock

### Critical Pitfalls

1. **Treating the kube watch as the result channel** — a dropped watch over the operator VPN strands files forever. The pod callback to `/api/internal/agent/*` reconciled by `file_id` is the ONLY authoritative result channel. The watch is lifecycle/observability only. Pair it with a periodic reconcile cron as the mandatory safety net from day one (Phase 54).

2. **Holding a SAQ worker slot on `await job.wait()`** — a Job can sit queued behind Kueue quota for hours before it starts, then run for hours more. Blocking a controller worker the entire time recreates the v4.0.10 starvation class. The fix is a fast `submit_k8s_job` task (returns in seconds) plus a periodic `reconcile_k8s_jobs` cron; state lives on `FileRecord`, not in a held coroutine (Phase 54).

3. **Presigned GET URL expiring before the suspended Job is admitted** — a 1-hour URL is baked into a Job that sits behind quota for 3 hours; the pod runs and gets a 403. Use long expiry from long-lived bucket credentials (not STS/role temp creds which silently cap at 1–12h regardless of requested expiry), or mint the GET URL just-in-time on Workload `Admitted` (Phase 53 + 54).

4. **OOM on long files in a pod with a hard cgroup memory limit** — `MonoLoader` decodes the entire audio file into RAM; a multi-hour Coachella set blows past 2Gi. The known fix is the v4.0.10 windowed/streaming analysis path. Bake windowed analysis into the one-shot entrypoint from the start; size memory requests from measured peak RSS on the longest real sets, not from a short-track example (Phase 52).

5. **Seeding a `process_file:<id>` ledger row for a K8s file** — there is no live SAQ `process_file` job for a K8s file, so `recover_orphaned_work` would wrongly re-enqueue it onto an agent queue (the CLOUDROUTE-02 violation). K8s in-flight files sit in `PUSHED`, which the backfill predicates already exclude. Do not seed the ledger row; let `reconcile_k8s_jobs` own K8s re-drive (Phase 54 + 55).

6. **Adding `SUBMITTED_K8S` as a new FileState** — forks the window count, dashboard cards, staging candidate query, and push-done set in `recover_orphaned_work`. Reuse `PUSHING`/`PUSHED` as generic in-flight states; carry Kueue admission phase in the non-state `cloud_phase` field.

7. **Whole-backlog over-enqueue into the cluster** — phaze has hit this twice (v4.0.6: 11,428 stranded jobs; v5.0: 44,500 force-swept jobs). K8s is a third target of the same duration-router + ledger seam, not a new "analyze everything" path. The `cloud_max_in_flight` window cap prevents flooding quota. Same AST guard as post-v4.0.6 covers the K8s enqueue site (Phase 55).

## Implications for Roadmap

Based on research, suggested phase structure continuing from v5.0's Phase 51:

### Phase 52: Job-Runner Image + One-Shot Entrypoint

**Rationale:** The Job image is the execution unit; everything else depends on it existing. Building it first, independently of any live cluster or bucket, lets Phases 53–54 assume a tested artifact. Parallels Phase 47 (arm64 image) in v5.0 ordering.
**Delivers:** `Dockerfile.k8sjob` published to GHCR; `phaze/cli/k8s_runner.py` one-shot entrypoint (httpx GET presigned URL → windowed essentia analysis → httpx PUT `/api/internal/agent/analysis/{file_id}` → exit); internal CA baked into the image; honest exit-code contract (non-zero on download-403, analysis failure, or callback failure)
**Addresses:** OOM on long files (windowed analysis from day one), pod→API TLS trust (CA in image), exit-code semantics (Pitfalls 4, 12, 13)
**Research flag:** Standard patterns; Phase 47 is the direct precedent. Skip research-phase.

### Phase 53: S3 Staging Leg

**Rationale:** Object staging is the byte-transfer seam that replaces v5.0's rsync. Testable entirely without a live cluster (moto/stubber + respx). Phase 54's Job submission needs the presigned GET URL this leg produces.
**Delivers:** `services/object_staging.py` (aioboto3: presign PUT/GET, delete_object); `tasks/upload_file_s3.py` (agent: httpx PUT bytes, POST `uploaded`); `routers/agent_s3.py` (control callbacks); `file_id`-scoped object key scheme; cleanup on all terminal outcomes; S3 config (`_FILE` secrets, `endpoint_url`, addressing style); Alembic migration (sidecar `cloud_job` table)
**Addresses:** DIST-01 byte-transfer constraint, S3 orphan cleanup, object-key collision, multipart for large sets, SigV4/endpoint_url misconfig (Pitfalls 8, 9, 18)
**Open question to resolve in requirements:** Presigned URL minting timing — submit-time long-expiry vs. just-in-time on Workload `Admitted=True`
**Research flag:** aioboto3 well-documented. Skip research-phase.

### Phase 54: Kube Submit / Watch + Reconcile Cron

**Rationale:** The Kubernetes API leg is the core of v6.0. With Job image (52) and S3 GET URL (53) available, the submit path is complete. Highest-risk design decisions land here — watch-vs-reconcile, TTL-vs-read, ledger non-seeding, idempotent submission. Testable against a fake kube API; no live cluster needed.
**Delivers:** `services/k8s_client.py` (kr8s: build/create/find/read/delete); `tasks/submit_k8s_job.py` (fast submit); `tasks/reconcile_k8s_jobs.py` (periodic cron: poll in-flight, cleanup, re-route, eviction detection, `cloud_phase` updates); Job spec with bearer token via cluster Secret; TTL/reconcile ordering invariant; `backoffLimit: 0` + `restartPolicy: Never`; Workload apiVersion as config constant
**Addresses:** Watch-as-result-channel, worker-slot starvation, TTL-vs-read race, ledger non-seeding (CLOUDROUTE-02), idempotent submission, Job-backoff-vs-control-plane-retry, Inadmissible-vs-Pending distinction, bearer token injection (Pitfalls 1, 2, 3, 5, 7, 10)
**Open question to resolve in requirements:** Job-Failed/evicted fallback policy — re-route to A1/local or mark ANALYSIS_FAILED?
**Research flag:** kr8s/Kueue patterns verified against Context7. Skip research-phase.

### Phase 55: Routing + Ledger Integration (the live seam)

**Rationale:** The only phase that touches the live v5.0 seam. Both legs (S3 in 53, kube in 54) must exist before the branch is wired. Kept last among code phases to minimize the partially-integrated window.
**Delivers:** `cloud_target` config setting; `stage_cloud_window` K8s branch (enqueue `upload_file_s3` instead of `push_file`); GATE 1 semantics for K8s; `enqueue_router` frozenset additions; controller + agent-worker task registrations; model validator for K8s config requirements; AST guard test covering K8s enqueue site
**Addresses:** Active-target toggle misrouting, whole-backlog over-enqueue (ledger scoping preserved), transport-specific leakage (Pitfalls 15, 16, 17)
**Open question to resolve in requirements:** `cloud_target` shape — two-way `Literal["a1", "k8s"]` or three-way `Literal["local", "a1", "k8s"]`?
**Research flag:** Standard patterns (extends existing enqueue_router). Skip research-phase.

### Phase 56: Deploy + Runbook + Docs

**Rationale:** Ops-only phase analogous to Phase 51. All code paths exist; this phase makes them operable. Kueue admin objects are documented as admin setup — phaze never creates them.
**Delivers:** Kueue admin runbook (RF/CQ/LQ creation, RBAC Role, cluster Secret for compute-agent bearer token, bucket lifecycle TTL, apiVersion verification at deploy, LocalQueue existence validation at startup); `_FILE` secret wiring; `cloud_burst_enabled` gate confirmed on all three K8s entry points; transport-agnostic endpoint config (Tailscale OR WireGuard); `docs/deployment.md` additions; perpetual-DEAD cluster compute-agent liveness UI note
**Addresses:** RBAC scoping (least-privilege namespaced Role), startup LocalQueue validation, bucket lifecycle TTL, CA cert distribution (confirmed from Phase 52 bake), token rotation scope (Pitfalls 11, 17)
**Open question to resolve in requirements:** Internal CA delivery to the Job pod — baked into image (Phase 52 planned this) vs. ConfigMap-mounted for rotation without image rebuild?
**Research flag:** Ops runbook. Skip research-phase.

### Phase Ordering Rationale

- **52 before 53:** The Job image defines the httpx GET interface the S3 leg must produce a URL for
- **53 before 54:** The kube submit task needs a presigned GET URL to bake into the Job spec; S3 staging produces it
- **54 before 55:** The live seam edit must have both legs available to branch to
- **55 before 56:** Runbook verifies config that Phase 55 introduces
- **52–54 are each independently unit-testable without a live cluster** — 85% coverage gate reachable at every phase boundary
- Mirrors v5.0's own ordering (47 image → 48/49 agent + routing → 50 pipeline → 51 deploy); live-seam edit (55) is as late as possible

### Research Flags

All five phases have standard patterns or direct v5.0 precedents. No phases in this milestone require a research-phase.

- **Phase 52:** Skip — directly parallels Phase 47 (arm64 image)
- **Phase 53:** Skip — aioboto3 well-documented; moto patterns established
- **Phase 54:** Skip — kr8s/Kueue patterns verified same-day against Context7 + kueue.sigs.k8s.io
- **Phase 55:** Skip — extends existing `enqueue_router` and `stage_cloud_window` patterns
- **Phase 56:** Skip — ops runbook; no research needed

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | kr8s 0.20.15 and aioboto3 15.5.0 verified on PyPI 2026-06-26; Kueue v0.18.2 released same day; dep overlap with existing stack confirmed; v1beta2 apiVersion verified as current served+storage version |
| Features | HIGH | Kueue Job→Workload lifecycle and all condition signals verified against Context7 `/kubernetes-sigs/kueue` and kueue.sigs.k8s.io; phaze-specific scope grounded in PROJECT.md Key Decisions |
| Architecture | HIGH | Every integration seam read directly from phaze source; DIST-01 constraint confirmed in `docker-compose.yml`; v5.0 spine traced through real modules |
| Pitfalls | HIGH | Critical pitfalls grounded in lived phaze incidents (v4.0.6, v4.0.10, v5.0); S3 presigned-URL STS expiry verified against AWS docs; Kueue lifecycle edge cases verified against Context7 |

**Overall confidence:** HIGH

### Gaps to Address

Decision questions (not research gaps — options and tradeoffs are understood) to resolve in requirements:

- **DIST-01 confirmation as a REQ-ID:** Codify "file-server agent uploads via presigned PUT; control plane presigns only" as an explicit requirement so it cannot be reversed during implementation
- **`cloud_target` shape:** Two-way `Literal["a1", "k8s"]` (with `cloud_burst_enabled=False` as "local") vs. three-way `Literal["local", "a1", "k8s"]`; decide in requirements, Phase 55 implements
- **Job-Failed/evicted fallback policy:** Re-route to A1/local vs. mark ANALYSIS_FAILED; eviction-to-fallback is P2/v6.x per FEATURES.md, but Job-Failed policy must be decided for Phase 54
- **Presigned URL minting timing:** Submit-time with long-expiry from long-lived creds vs. just-in-time on Workload `Admitted=True`; decide in requirements, Phases 53–54 implement
- **Internal CA distribution to Job pod:** Baked into image at build time (Phase 52 already planned this; requires image rebuild on CA rotation) vs. ConfigMap-mounted at runtime (survives rotation without rebuild)
- **Perpetual-DEAD cluster compute-agent liveness UI:** The cluster compute agent never heartbeats (ephemeral Jobs); the existing Agents page will show it as perpetually DEAD. Suppress liveness for a `kind="compute"` cluster identity, or add a `cluster_identity` flag. Low priority but needs a Phase 56 runbook note

## Sources

### Primary (HIGH confidence)

- Context7 `/kr8s-org/kr8s` — async Job create, `wait(["condition=Complete","condition=Failed"])`, dynamic custom-object get
- Context7 `/kubernetes-sigs/kueue` — Workload status conditions (QuotaReserved/Admitted/Evicted/Finished), `queue-name` label, suspend semantics, job-uid→workload lookup, KEP-973 examples, PodsReadyTimeout requeue/deactivation
- PyPI JSON API (2026-06-26) — kr8s 0.20.15, aioboto3 15.5.0, aiobotocore 2.25.1 verified
- GitHub `kubernetes-sigs/kueue` releases — v0.18.2 released 2026-06-26; v1beta2 as served+storage version
- kueue.sigs.k8s.io/docs — Running Jobs task, Workload concept, `maximumExecutionTimeSeconds`, `requeueState`
- phaze source (read directly) — `services/enqueue_router.py`, `services/scheduling_ledger.py`, `tasks/release_awaiting_cloud.py`, `tasks/push.py`, `tasks/reenqueue.py`, `routers/agent_analysis.py`, `routers/agent_push.py`, `models/file.py`, `config.py`
- phaze `.planning/PROJECT.md` v6.0 milestone — DIST-01, CPU-only Key Decision, Out-of-Scope reversals
- AWS S3 presigned-URL expiration docs — SigV4 max 7 days; STS/role temp creds cap at credential lifetime (1–12h) regardless of requested expiry

### Secondary (MEDIUM-HIGH confidence)

- Red Hat build of Kueue docs — v1beta2 is current; v1beta1 deprecated but still served
- phaze project memory — v4.0.6 default-queue 11,428-job over-enqueue; v5.0 force=True 44,500-job sweep; v4.0.10 OOM on long sets; v4.0 self-signed internal CA invariants

---
*Research completed: 2026-06-26*
*Ready for roadmap: yes*
