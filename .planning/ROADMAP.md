# Roadmap: Phaze

## Milestones

- ✅ **v1.0 MVP** — Phases 1-11 (shipped 2026-03-30)
- ✅ **v2.0 Metadata Enrichment & Tracklist Integration** — Phases 12-17 (shipped 2026-04-02)
- ✅ **v3.0 Cross-Service Intelligence & File Enrichment** — Phases 18-23 (shipped 2026-04-04)
- ✅ **v4.0 Distributed Agents** — Phases 24-29 (shipped 2026-05-17)
- ✅ **v5.0 Cloud Burst Analysis** — Phases 47-51 (shipped 2026-06-26)
- 🚧 **v6.0 Kubernetes Burst Analysis** — Phases 52-56 (in progress)

## Phases

### v6.0 Kubernetes Burst Analysis (Phases 52-56) — IN PROGRESS

> K8s becomes a **third** analysis-routing target alongside local and the v5.0 OCI A1: long sets that can't finish locally run as ephemeral, quota-scheduled **Kueue batch Jobs** on a remote x64 cluster. Mirrors the v5.0 spine (image → legs → pipeline → routing seam → deploy); the single live-seam edit (Phase 55) is deliberately last, and Phases 52-54 are each independently unit-testable without a live cluster (respx / moto / fake kube API) so the 85% coverage gate holds at every phase boundary. **No phase needs a research-phase** — every external API surface (kr8s, aioboto3, Kueue v1beta2) was verified same-day against Context7 / official docs and each phase has a direct v5.0 precedent.

- [x] **Phase 52: Job-runner image & one-shot entrypoint** — x86 GHCR image FROM the existing essentia base; one-shot pull → windowed analyze → POST result → exit; honest exit codes; internal CA baked in (KJOB-01..05) (completed 2026-06-27)
- [x] **Phase 53: S3 object-staging leg** — control-plane presign (aioboto3) + file-server agent httpx-PUT upload + pod presigned GET; `file_id`-scoped keys; cleanup on every outcome; `cloud_job` sidecar migration (KSTAGE-01..05) (completed 2026-06-28)
- [x] **Phase 54: Kube submit/watch + reconcile cron** — suspended per-file Kueue Job (kr8s); fast submit + reconcile cron; out-of-band callback authoritative; no ledger-seed; Inadmissible-vs-Pending (KSUBMIT-01..06) (completed 2026-06-28)
- [ ] **Phase 55: Routing, state & ledger integration** — `cloud_target` selector + `stage_cloud_window` K8s branch + `enqueue_router` additions + AST guard (the one live-seam edit) (KROUTE-01..05)
- [ ] **Phase 56: Deployment, runbook, config & docs** — Kueue admin runbook + least-privilege RBAC + `_FILE` secrets + transport-agnostic endpoints + ephemeral-identity Agents-UI note + master toggle (KDEPLOY-01..05)

<details>
<summary>v1.0 MVP (Phases 1-11) -- SHIPPED 2026-03-30</summary>

- [x] Phase 1: Infrastructure & Project Setup (3/3 plans) -- completed 2026-03-27
- [x] Phase 2: File Discovery & Ingestion (3/3 plans) -- completed 2026-03-27
- [x] Phase 3: Companion Files & Deduplication (2/2 plans) -- completed 2026-03-27
- [x] Phase 4: Task Queue & Worker Infrastructure (2/2 plans) -- completed 2026-03-27
- [x] Phase 5: Audio Analysis Pipeline (2/2 plans) -- completed 2026-03-28
- [x] Phase 6: AI Proposal Generation (2/2 plans) -- completed 2026-03-28
- [x] Phase 7: Approval Workflow UI (3/3 plans) -- completed 2026-03-29
- [x] Phase 8: Safe File Execution & Audit (2/2 plans) -- completed 2026-03-29
- [x] Phase 9: Pipeline Orchestration (1/1 plan) -- completed 2026-03-30
- [x] Phase 10: CI Config & Bug Fixes (1/1 plan) -- completed 2026-03-30
- [x] Phase 11: Polish & Cleanup (3/3 plans) -- completed 2026-03-30

Full details: `.planning/milestones/v1.0-ROADMAP.md`

</details>

<details>
<summary>v2.0 Metadata Enrichment & Tracklist Integration (Phases 12-17) -- SHIPPED 2026-04-02</summary>

- [x] Phase 12: Infrastructure & Audio Tag Extraction (3/3 plans) -- completed 2026-03-31
- [x] Phase 13: AI Destination Paths (3/3 plans) -- completed 2026-03-31
- [x] Phase 14: Duplicate Resolution UI (2/2 plans) -- completed 2026-04-01
- [x] Phase 15: 1001Tracklists Integration (2/2 plans) -- completed 2026-04-01
- [x] Phase 16: Fingerprint Service & Batch Ingestion (3/3 plans) -- completed 2026-04-01
- [x] Phase 17: Live Set Matching & Tracklist Review (3/3 plans) -- completed 2026-04-02

Full details: `.planning/milestones/v2.0-ROADMAP.md`

</details>

<details>
<summary>v3.0 Cross-Service Intelligence & File Enrichment (Phases 18-23) -- SHIPPED 2026-04-04</summary>

- [x] Phase 18: Unified Search (2/2 plans) -- completed 2026-04-03
- [x] Phase 19: Discogs Cross-Service Linking (3/3 plans) -- completed 2026-04-03
- [x] Phase 20: Tag Writing (3/3 plans) -- completed 2026-04-03
- [x] Phase 21: CUE Sheet Generation (3/3 plans) -- completed 2026-04-03
- [x] Phase 22: Tracklist Integration Fixes (1/1 plan) -- completed 2026-04-04
- [x] Phase 23: v3.0 Polish & Wiring Fixes (1/1 plan) -- completed 2026-04-04

Full details: `.planning/milestones/v3.0-ROADMAP.md`

</details>

<details>
<summary>v4.0 Distributed Agents (Phases 24-29) -- SHIPPED 2026-05-17</summary>

- [x] Phase 24: Schema Foundation & Agent Registry (5/5 plans) -- completed 2026-05-11
- [x] Phase 25: Internal Agent HTTP API & Bearer Auth (8/8 plans) -- completed 2026-05-12
- [x] Phase 26: Task Code Reorg & HTTP-Backed Agent Worker (13/13 plans) -- completed 2026-05-12
- [x] Phase 27: Watcher Service & User-Initiated Scan (7/7 plans) -- completed 2026-05-14
- [x] Phase 28: Distributed Execution Dispatch (6/6 plans) -- completed 2026-05-15
- [x] Phase 29: Deployment Hardening & Agents Admin (8/8 plans) -- completed 2026-05-17

Full details: `.planning/milestones/v4.0-ROADMAP.md`

</details>

<details>
<summary>✅ v5.0 Cloud Burst Analysis (Phases 47-51) — SHIPPED 2026-06-26</summary>

Analyze long-duration audio (≥90 min) on a free OCI Ampere A1 (arm64) "compute agent" reached over Tailscale, instead of locally — clearing the long-set backlog that exceeds the local analysis timeout. Full detail archived in `milestones/v5.0-ROADMAP.md`.

- [x] **Phase 47: Official arm64 essentia agent image** — build essentia from source on a native arm64 CI runner, publish to GHCR with a parity guard (completed 2026-06-24)
- [x] **Phase 48: Compute-agent type** — register a media-less `kind="compute"` agent that drains its queue + PUTs results, surfaced on the Agents page (completed 2026-06-25)
- [x] **Phase 49: Duration routing & backfill** — route ≥90min files to an online compute agent (else "awaiting cloud"), backfill the 144 timed-out long files via the Phase 45 ledger (completed 2026-06-25)
- [x] **Phase 50: Push pipeline** — rsync-over-Tailscale "stay one ahead" push to the compute agent's scratch dir, sha256-verify, ephemeral cleanup, idempotent re-drive (completed 2026-06-26)
- [x] **Phase 51: Deployment, config & docs** — cloud-agent compose + Tailscale, all config knobs (`_FILE` secrets), OCI A1 / Tailscale-ACL runbook, master enable toggle (completed 2026-06-26)

Deployment-gated verification deferred to the live OCI A1 rollout (see STATE.md Deferred Items).

</details>

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Infrastructure & Project Setup | v1.0 | 3/3 | Complete | 2026-03-27 |
| 2. File Discovery & Ingestion | v1.0 | 3/3 | Complete | 2026-03-27 |
| 3. Companion Files & Deduplication | v1.0 | 2/2 | Complete | 2026-03-27 |
| 4. Task Queue & Worker Infrastructure | v1.0 | 2/2 | Complete | 2026-03-27 |
| 5. Audio Analysis Pipeline | v1.0 | 2/2 | Complete | 2026-03-28 |
| 6. AI Proposal Generation | v1.0 | 2/2 | Complete | 2026-03-28 |
| 7. Approval Workflow UI | v1.0 | 3/3 | Complete | 2026-03-29 |
| 8. Safe File Execution & Audit | v1.0 | 2/2 | Complete | 2026-03-29 |
| 9. Pipeline Orchestration | v1.0 | 1/1 | Complete | 2026-03-30 |
| 10. CI Config & Bug Fixes | v1.0 | 1/1 | Complete | 2026-03-30 |
| 11. Polish & Cleanup | v1.0 | 3/3 | Complete | 2026-03-30 |
| 12. Infrastructure & Audio Tag Extraction | v2.0 | 3/3 | Complete | 2026-03-31 |
| 13. AI Destination Paths | v2.0 | 3/3 | Complete | 2026-03-31 |
| 14. Duplicate Resolution UI | v2.0 | 2/2 | Complete | 2026-04-01 |
| 15. 1001Tracklists Integration | v2.0 | 2/2 | Complete | 2026-04-01 |
| 16. Fingerprint Service & Batch Ingestion | v2.0 | 3/3 | Complete | 2026-04-01 |
| 17. Live Set Matching & Tracklist Review | v2.0 | 3/3 | Complete | 2026-04-02 |
| 18. Unified Search | v3.0 | 2/2 | Complete | 2026-04-03 |
| 19. Discogs Cross-Service Linking | v3.0 | 3/3 | Complete | 2026-04-03 |
| 20. Tag Writing | v3.0 | 3/3 | Complete | 2026-04-03 |
| 21. CUE Sheet Generation | v3.0 | 3/3 | Complete | 2026-04-03 |
| 22. Tracklist Integration Fixes | v3.0 | 1/1 | Complete | 2026-04-04 |
| 23. v3.0 Polish & Wiring Fixes | v3.0 | 1/1 | Complete | 2026-04-04 |
| 24. Schema Foundation & Agent Registry | v4.0 | 5/5 | Complete | 2026-05-11 |
| 25. Internal Agent HTTP API & Bearer Auth | v4.0 | 8/8 | Complete | 2026-05-12 |
| 26. Task Code Reorg & HTTP-Backed Agent Worker | v4.0 | 13/13 | Complete | 2026-05-12 |
| 27. Watcher Service & User-Initiated Scan | v4.0 | 7/7 | Complete | 2026-05-14 |
| 28. Distributed Execution Dispatch | v4.0 | 6/6 | Complete | 2026-05-15 |
| 29. Deployment Hardening & Agents Admin | v4.0 | 8/8 | Complete | 2026-05-17 |
| 30. Fix control-plane SAQ queue misrouting | v4.0 | 5/5 | Complete   | 2026-06-10 |
| 31. Windowed Time-Series Audio Analysis | v4.0 | 6/6 | Complete   | 2026-06-11 |
| 32. Pipeline Reboot Resilience & Re-enqueue | v4.0 | 4/4 | Complete   | 2026-06-11 |
| 33. SAQ Monitoring UI (mounted in phaze-api) | v4.0 | 4/4 | Complete   | 2026-06-11 |
| 34. Pipeline Queue-Depth Status & Double-Enqueue Guard | v4.0 | 5/5 | Complete | 2026-06-10 |
| 35. Pipeline Determinism, Idempotency & Per-Job-Type Observability | v4.0 | 5/5 | Complete | 2026-06-12 |
| 36. Pipeline Queue Backend Migration (Redis → Postgres SAQ) | v4.0 | — | Complete | 2026-06-12 |
| 37. Per-Stage Pause and Priority Control Plane | v4.0 | 4/4 | Complete | 2026-06-12 |
| 38. Pipeline DAG Pause/Priority UI and Rescan Button Removal | v4.0 | 3/3 | Complete | 2026-06-13 |
| 39. Tracklist Search DAG Node | v4.0 | 1/1 | Executed | — |
| 40. Tracklist Fingerprint-Scan DAG Node | v4.0 | 1/1 | Executed | — |
| 41. Scrape and Match DAG Triggers | v4.0 | 1/1 | Executed | — |
| 42. Recovery-Only Pipeline Automation | v4.0 | 2/2 | Executed | — |
| 43. Analyze Throughput Fix | v4.0 | 4/4 | Complete | 2026-06-17 |
| 44. Analyze Observability UI | v4.0 | 4/4 | Complete | 2026-06-18 |
| 45. Scheduling Ledger for Orphan Recovery | v4.0 | 6/6 | Complete    | 2026-06-19 |
| 46. Heartbeat Starvation Fix | v4.0 | 1/1 | Complete | 2026-06-23 |
| 47. Official arm64 essentia agent image | v5.0 | 4/4 | Complete    | 2026-06-24 |
| 48. Compute-agent type | v5.0 | 3/3 | Complete   | 2026-06-25 |
| 49. Duration routing & backfill | v5.0 | 4/4 | Complete    | 2026-06-25 |
| 50. Push pipeline | v5.0 | 8/8 | Complete    | 2026-06-26 |
| 51. Deployment, config & docs | v5.0 | 4/4 | Complete   | 2026-06-26 |
| 52. Job-runner image & one-shot entrypoint | v6.0 | 3/3 | Complete    | 2026-06-27 |
| 53. S3 object-staging leg | v6.0 | 5/5 | Complete    | 2026-06-28 |
| 54. Kube submit/watch + reconcile cron | v6.0 | 6/6 | Complete    | 2026-06-28 |
| 55. Routing, state & ledger integration | v6.0 | 5/6 | In Progress|  |
| 56. Deployment, runbook, config & docs | v6.0 | 0/? | Not started | - |

## Phase Details (v6.0 Kubernetes Burst Analysis)

> **Milestone goal:** Long sets that can't finish locally are analyzed on a remote **x64 Kubernetes cluster running Kueue** as a third routing target alongside local and the v5.0 OCI A1 — ephemeral, quota-scheduled **batch Jobs submitted per file**. The control-plane choreography (duration router, AWAITING_CLOUD hold, advisory-locked in-flight window, `PUSHING`/`PUSHED` states, scheduling ledger, `cloud_burst_enabled` toggle, out-of-band `/api/internal/agent/*` result callback reconciled by `file_id`) is reused verbatim from v5.0; the execution unit changes from a persistent SAQ-draining host to an ephemeral Kueue Job. Two new control-plane deps vs. v5.0: `kr8s` (async Kubernetes API) and `aioboto3` (S3 presign/cleanup); **zero** new pip deps in the Job image. Dependency order: image → S3 leg → kube submit/reconcile → routing seam → deploy. Each phase = its own PR (worktree branch). **No phase requires a research-phase** (see milestone note).

### Phase 52: Job-runner image & one-shot entrypoint

**Goal**: An x86 Kueue Job-runner image exists on GHCR with a one-shot entrypoint that pulls a file, analyzes it (windowed), POSTs the result, and exits with an honest exit-code contract — the execution unit everything else depends on, built and tested independently of any live cluster or bucket.
**Depends on**: Phase 51 (prior milestone shipped); first v6.0 phase — no intra-milestone dependency. Direct precedent: v5.0 Phase 47 (image build/publish).
**Requirements**: KJOB-01, KJOB-02, KJOB-03, KJOB-04, KJOB-05
**Success Criteria** (what must be TRUE):

  1. Operator can pull an x86 Job-runner image from GHCR, built `FROM` the existing x86 essentia base image with **zero new pip dependencies**, that boots and runs the one-shot entrypoint.
  2. Given a presigned download URL, the entrypoint downloads the file, sha256-verifies it against the `FileRecord`, runs analysis, POSTs the result to `/api/internal/agent/*` (reconciled by `file_id`), and exits.
  3. A multi-hour set analyzes through the windowed/streaming path without OOMing under a hard pod memory limit (no whole-file `MonoLoader` decode); memory requests are sized from measured peak RSS on the longest real sets.
  4. The entrypoint returns a non-zero exit code on any download, integrity, analysis, or callback failure and never reports success on a failed analysis.
  5. The pod's HTTPS callback trusts the control plane's internal CA baked into the image, with **no** `verify=False` TLS bypass anywhere.

**Plans**: 3 plansPlans:
**Wave 1**

- [x] 52-01-PLAN.md — Shared analysis_wire converters + request_download_url client method + presign response schema (KJOB-02)
- [x] 52-03-PLAN.md — Dockerfile.job (FROM api base, baked CA, zero new deps) + needs-gated docker-publish job + deployment guards (KJOB-01/05)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 52-02-PLAN.md — job_runner one-shot orchestrator + distinct exit-code contract + import-boundary/CA/windowed tests (KJOB-02/03/04/05)

**Research**: Skip — directly parallels v5.0 Phase 47.

### Phase 53: S3 object-staging leg

**Goal**: The long file moves from the file-server agent into ephemeral S3-compatible object storage and back down to the Job pod via presigned URLs — the control plane presigns only (preserving DIST-01), the agent uploads the bytes, and objects are cleaned up on every terminal outcome.
**Depends on**: Phase 52 (the Job pod's httpx-GET download interface defines what URL this leg must produce). Testable end-to-end without a live cluster (moto/stubber + respx).
**Requirements**: KSTAGE-01, KSTAGE-02, KSTAGE-03, KSTAGE-04, KSTAGE-05
**Success Criteria** (what must be TRUE):

  1. The control plane presigns S3 PUT/GET URLs and deletes objects (aioboto3) but **never reads or uploads file bytes itself**, preserving the CI-enforced DIST-01 no-media-mount boundary on the application server.
  2. A file-server agent uploads the file bytes to a presigned PUT URL over httpx and callbacks the control plane; **no** S3 SDK or bucket credentials live on the agent or the pod.
  3. The presigned GET URL is minted just-in-time when the pod requests it at startup (post-admission), so it never expires during a long Kueue quota wait.
  4. Each staged object uses a `file_id`-scoped key and is deleted on **every** terminal outcome (success, failure, eviction, re-drive), with a bucket lifecycle TTL as a backstop against orphaned-object leaks.
  5. S3 endpoint, bucket, addressing style, and credentials are operator-provided via `_FILE` secrets and work against any S3-compatible backend (`endpoint_url`), not just AWS.

**Plans**: 5 plans (4 waves), includes the `cloud_job` sidecar Alembic migration
**Wave 1**

- [x] 53-01-PLAN.md — Foundation: S3 deps + ControlSettings config + cloud_job model/migration (KSTAGE-04, KSTAGE-05)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 53-02-PLAN.md — s3_staging aioboto3 service + presign-download server route (KSTAGE-01, KSTAGE-03, KSTAGE-04)
- [x] 53-03-PLAN.md — Agent upload leg: agent_s3 schemas + httpx upload_file_s3 task + enqueue-seam registration (KSTAGE-02)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 53-04-PLAN.md — Control-side callbacks + cloud_staging producer/re-drive (KSTAGE-01, KSTAGE-04)
- [x] 53-05-PLAN.md — Inline staged-object delete on the result callback, D-02 (KSTAGE-04)

**Research**: Skip — aioboto3 well-documented; moto patterns established.

### Phase 54: Kube submit / watch + reconcile cron

**Goal**: The control plane submits a suspended per-file Kueue Job, a fast submit task never blocks a worker, and a periodic reconcile cron owns Workload lifecycle, cleanup, and re-drive — with the out-of-band `/api/internal/agent/*` callback as the **only** authoritative result channel. The highest-risk phase; testable against a fake kube API.
**Depends on**: Phase 53 (the submit task bakes the presigned GET URL this leg produces into the Job spec). Phase 52 (the Job image is the execution unit).
**Requirements**: KSUBMIT-01, KSUBMIT-02, KSUBMIT-03, KSUBMIT-04, KSUBMIT-05, KSUBMIT-06
**Success Criteria** (what must be TRUE):

  1. When a long file is staged for K8s, the control plane submits a single **suspended** `batch/v1` Job (labeled `kueue.x-k8s.io/queue-name`, `restartPolicy: Never`, `parallelism: 1`, cpu/memory requests only) idempotently via a deterministic name keyed to `file_id`.
  2. The submit task returns within seconds without blocking a worker waiting for analysis; a periodic reconcile cron owns Workload status, cleanup, and re-drive.
  3. The analysis result is authoritative **only** via the out-of-band `/api/internal/agent/*` callback reconciled by `file_id` — a dropped or expired kube watch never loses or duplicates a result.
  4. The reconcile loop distinguishes healthy `Pending` (queued behind quota — waits indefinitely) from `Inadmissible` (misconfigured LocalQueue — surfaced to the operator) and detects success / failure / eviction.
  5. On Job failure or eviction the file is re-driven up to a bounded max-attempts cap then marked `ANALYSIS_FAILED` (no cross-target fallback); finished Jobs are cleaned up with no TTL-vs-read race, and **no `process_file:<id>` ledger row is seeded** for K8s files (so `recover_orphaned_work` never wrongly re-enqueues them onto an agent queue).

**Plans**: 6 plans in 4 waves
**Wave 1**

- [x] 54-01-PLAN.md — kr8s dependency (legitimacy-gated) + kube config surface on ControlSettings (D-08) [Wave 1]
- [x] 54-02-PLAN.md — cloud_job model extension + migration 026 (CloudJobStatus + kueue_workload/attempts/inadmissible, D-09) [Wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 54-03-PLAN.md — pure kr8s seam (suspended-Job manifest + submit/list/get/delete) + fake-kube test substrate (KSUBMIT-01/05/06) [Wave 2]
- [x] 54-04-PLAN.md — Inadmissible pipeline-UI alert off cloud_job.inadmissible (D-06, KSUBMIT-04) [Wave 2]

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 54-05-PLAN.md — fast submit_cloud_job task + enqueue_router/controller registration, no ledger seed (KSUBMIT-01/02/06) [Wave 3]

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 54-06-PLAN.md — reconcile cron (status mapping, delete-after-record, S3 cleanup, bounded re-drive, Inadmissible) + */5 registration (KSUBMIT-02/03/04/05/06) [Wave 4]

**Research**: Skip — kr8s/Kueue patterns verified same-day against Context7 + kueue.sigs.k8s.io.

### Phase 55: Routing, state & ledger integration (the live seam)

**Goal**: K8s becomes the third cloud target selected by a single config setting, wired into the existing duration router / `stage_cloud_window` / scheduling ledger as one new branch — the only phase that touches the live v5.0 seam, kept last among code phases to minimize the partially-integrated window.
**Depends on**: Phase 53 and Phase 54 (both legs must exist before the branch is wired to them).
**Requirements**: KROUTE-01, KROUTE-02, KROUTE-03, KROUTE-04, KROUTE-05
**Success Criteria** (what must be TRUE):

  1. A single `cloud_target` selector (`Literal["local", "a1", "k8s"]`) under the existing `cloud_burst_enabled` toggle chooses the active analysis target — `k8s` routes ≥threshold long files through the Kueue path, `a1` keeps the v5.0 OCI A1 path, `local` disables cloud burst.
  2. K8s offload reuses the existing duration router, AWAITING_CLOUD hold, and advisory-locked `stage_cloud_window` in-flight window (`cloud_max_in_flight`) as a single new branch — long files only (conservative scope), never a whole-backlog sweep.
  3. K8s in-flight files reuse the `PUSHING`/`PUSHED` states (no new `FileRecord` state); the Kueue admission phase lives in a `cloud_phase` column on the `cloud_job` sidecar table, leaving the FileRecord state machine unchanged.
  4. A static AST guard test asserts every K8s enqueue site routes through `enqueue_router` (no consumer-less default-queue enqueue, no whole-backlog enqueue), preventing recurrence of the v4.0.6 / v5.0 over-enqueue incidents.
  5. Operator can backfill ≥threshold timed-out long files (`analysis_failed`, duration ≥ threshold) to the K8s target, ledger-scoped exactly like v5.0 so only previously-scheduled work is re-driven.

**Plans**: 6 plans in 3 waves
**Wave 1**

- [x] 55-01-PLAN.md — `cloud_target` config rename (D-02 hard-replace `cloud_burst_enabled`, per-target validators, call-site + test re-keys) [wave 1]
- [x] 55-02-PLAN.md — `cloud_phase` schema: migration 027 + `CloudPhase` enum/column + submit seed + reconcile co-writes (D-04) [wave 1]
- [x] 55-06-PLAN.md — operator config/docs migration to `PHAZE_CLOUD_TARGET` (.env.example, control-plane compose, configuration/cloud-burst/deployment docs) [wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 55-03-PLAN.md — the live-seam k8s branch: `stage_cloud_window` fork + no-commit S3 core + `report_uploaded` PUSHED-flip/`submit_cloud_job` enqueue (D-01, L1/L2) [wave 2]
- [x] 55-04-PLAN.md — ledger-scoped backfill fork (no `process_file` seed for k8s) + KROUTE-04 AST guard (D-03, L3/L4) [wave 2]

**Wave 3** *(blocked on Wave 2 completion)*

- [ ] 55-05-PLAN.md — KROUTE-06 admission-state cards (`get_cloud_phase_counts` + `admission_state_card.html` + OOB wiring) [wave 3]

**Research**: Skip — extends the existing `enqueue_router` + `stage_cloud_window` patterns.

### Phase 56: Deployment, runbook, config & docs

**Goal**: The K8s offload is operable and fully operator-controlled — a cluster-admin runbook for the Kueue/RBAC/Secret objects phaze does *not* create, transport-agnostic endpoint config, fail-fast config validation, an ephemeral-identity Agents-UI note, and a single toggle back to all-local. Ops-only phase analogous to v5.0 Phase 51.
**Depends on**: Phase 55 (the runbook verifies the config Phase 55 introduces). All code paths exist; this phase makes them operable.
**Requirements**: KDEPLOY-01, KDEPLOY-02, KDEPLOY-03, KDEPLOY-04, KDEPLOY-05
**Success Criteria** (what must be TRUE):

  1. A cluster-admin runbook documents the Kueue objects phaze does **not** create (ResourceFlavor / ClusterQueue / LocalQueue, CPU-only flavor, single-CQ no-preemption quota), the least-privilege namespaced RBAC Role/ServiceAccount (create/get/delete Jobs, get/watch/list Workloads in one namespace), and the cluster Secret carrying the compute-agent bearer token.
  2. All K8s/S3 parameters are pydantic-settings with `_FILE`-secret support, and a model validator fail-fasts when `cloud_target="k8s"` but required K8s/S3 config is missing.
  3. phaze consumes operator-provided reachable endpoints (kube API, S3, callback) over either Tailscale or WireGuard, with **no** mesh-specific code or assumptions.
  4. At startup (when `cloud_target="k8s"`) phaze validates the configured LocalQueue is reachable and surfaces a clear error otherwise; the cluster compute-agent shows as an ephemeral (Job-based) identity in the Agents UI rather than a perpetually-DEAD heartbeating agent.
  5. Operator can revert the entire K8s offload to all-local (or A1) via the single `cloud_target` / `cloud_burst_enabled` toggle with no other change; `docs/deployment.md` documents the full cluster + bucket + secret setup.

**Plans**: TBD
**UI hint**: yes
**Research**: Skip — ops runbook; no research needed.

### Phase 30: Fix systemic control-plane SAQ queue misrouting — every manually-triggered enqueue targets the consumer-less default queue

**Goal:** Every control-plane (UI/API) enqueue lands on a queue an actual worker consumes. Route the misrouted sites (pipeline.py, tracklists.py, scan.py/ingestion.py) through a shared helper: controller-bound tasks → `controller` queue, per-agent tasks → `AgentTaskRouter` with active-agent selection. The `default` queue ends with no producers. Regression tests assert correct queue targeting. See CONTEXT.md.
**Requirements**: QR-01 (every control-plane enqueue targets a consumed queue; default queue has no producers), QR-02 (per-agent routing uses active-agent selection; 0-agent surfaces a clear error), QR-03 (regression + guard tests assert queue targeting and prevent recurrence)
**Depends on:** Phase 29
**Plans:** 5/5 plans complete

Plans:

- [x] 30-01-PLAN.md — Routing foundation: named controller queue in lifespan, remove default queue, enqueue-routing helper + active-agent selection
- [x] 30-02-PLAN.md — Fix pipeline.py (process_file / generate_proposals / extract_file_metadata / fingerprint_file — 8 handlers) + tests
- [x] 30-03-PLAN.md — Fix tracklists.py (scrape/search/match → controller; scan_live_set → per-agent) + scan-status poll re-targeting + tests
- [x] 30-04-PLAN.md — Fix legacy /api/v1/scan → ingestion extract_file_metadata per-agent routing + tests
- [x] 30-05-PLAN.md — Cross-cutting guard test (no default-queue producers) + routing docs + full-suite verification

### Phase 31: Windowed Time-Series Audio Analysis

**Goal:** Rewrite `analyze_file` to stream-decode each file once and analyze it per-window — fixing the `RhythmExtractor2013` `OnsetDetectionGlobal` buffer-overflow crash and the latent whole-file OOM on multi-hour sets — producing a two-tier time-series: fine tier (BPM + key) every 30s, coarse tier (mood/style/danceability) every 3min, fixed-duration and configurable. Persist windows in a new queryable `analysis_window` child table with partial indexes; keep representative aggregates (median BPM, modal key, dominant mood/style) on the existing `analysis` row so proposals/search/sort are unaffected. Extend `AnalysisWritePayload` with a `windows` list and make `put_analysis` replace a file's windows idempotently. Add a compact review-UI row with a BPM sparkline that HTMX-expands to a multi-lane timeline (SVG/CSS, no charting lib). First plan task is a spike validating the streaming single-pass decode on a real 2-hour file.
**Design spec:** docs/superpowers/specs/2026-06-10-windowed-analysis-design.md
**Requirements**: ANL-01 (BPM/key/mood/style detection) extended to time-series; new cross-archive queryability of time-varying characteristics.
**Depends on:** Phase 30
**Rollout:** Ships as v4.0.10 → GHCR publish → homelab redeploy → re-run "Run analysis" (no rescan; Redis already purged of doomed/stale jobs).
**Plans:** 6/6 plans complete

Plans:

- [x] 31-01-PLAN.md — Spike & decode-strategy lock (EasyLoader-primary vs decode+Resample-hybrid) on a real ≥2h file [Wave 1]
- [x] 31-02-PLAN.md — `AnalysisWindow` model + additive migration 018 (table + composite/partial/label indexes, CASCADE FK) [Wave 1]
- [x] 31-03-PLAN.md — Wire schema `AnalysisWindowPayload` + idempotent `put_analysis` child-row replace [Wave 2]
- [x] 31-04-PLAN.md — Rewrite `analyze_file` to per-window decode + aggregate reductions + window-config AgentSettings [Wave 2]
- [x] 31-05-PLAN.md — `process_file` windows payload build (import-boundary preserved) + job timeout/retries tuning [Wave 3]
- [x] 31-06-PLAN.md — Review-UI BPM sparkline + HTMX-expandable multi-lane SVG/CSS timeline fragment [Wave 2]

### Phase 32: Pipeline Reboot Resilience & Re-enqueue

**Goal:** Make the analysis pipeline self-healing across full host reboots and container restarts for a large corpus (11,428 files, long per-file jobs). Postgres `FileState` is the durable source of truth; Redis stays a disposable/ephemeral broker (no AOF). On agent-worker startup and/or via a periodic cron, re-enqueue `FileState.DISCOVERED` files that have no active job, so a reboot resumes the remaining work automatically instead of requiring a manual "Run analysis" re-trigger. Resilience is idempotent and per-file (NOT intra-file) — re-running an interrupted file is safe because `put_analysis` replaces a file's window rows (Phase 31, plan 31-03). Note: the bounded-generous `worker_job_timeout` (~4h, not 0) + `retries=1` that lets SAQ reclaim a dead/restarted worker's in-flight job ships in Phase 31 plan 31-05 — this phase is the reboot/queue-loss recovery layer on top of that.
**Decisions:** Reboot recovery = startup/cron re-enqueue from Postgres (chosen over Redis AOF persistence), 2026-06-10. Re-enqueue runs in the CONTROLLER worker (direct Postgres + routing), not the agent worker; deterministic SAQ key `process_file:<file_id>` in a shared FastAPI-free helper used by BOTH the dashboard and the reboot path; analysis stage only.
**Depends on:** Phase 31
**Rollout:** Follows v4.0.10; ships as a subsequent v4.0.x → GHCR publish → homelab redeploy.
**Plans:** 6 plans (4 complete; 2 gap-closure for L-02 — wave 1)

Plans:

- [x] 32-00-PLAN.md — Wave 0 harness: dedup-aware `DedupFakeQueue`/`DedupFakeTaskRouter` so the SAQ no-op-on-duplicate-key behavior is unit-testable without Redis [Wave 0]
- [x] 32-01-PLAN.md — Shared FastAPI-free `enqueue_process_file` + `process_file_job_key` helper; refactor dashboard `_enqueue_analysis_jobs` to emit the deterministic key [Wave 1]
- [x] 32-02-PLAN.md — Controller `reenqueue_discovered(ctx)` task: query DISCOVERED, route to active agent, shared-helper enqueue with dedup no-op, zero-agent graceful skip [Wave 2]
- [x] 32-03-PLAN.md — Controller wiring: stash/close `ctx['task_router']`, call re-enqueue once on startup, register `CronJob(*/5)` [Wave 3]

### Phase 33: SAQ Monitoring UI (mounted in phaze-api)

**Goal:** Expose SAQ's built-in monitoring web UI by mounting it into the existing `phaze-api` FastAPI ASGI app at the `/saq` subpath — NOT the standalone `saq --web` server, NOT a new bound port, NO app-layer auth. `phaze-api` is deployed behind a reverse proxy that already terminates TLS and enforces internal-realm auth, so the dashboard is intentionally unauthenticated at the app layer.
**Approach / tasks:**

1. Anchor: app factory `create_app()` in `src/phaze/main.py:115` (`app = FastAPI(...)`, entrypoint `phaze.entrypoint` → uvicorn :8000). The lifespan (`main.py:49`) already creates the SAQ queue + task_router + redis on startup and holds them in `app.state` — **reuse those same `saq.Queue` instance(s)** (same Redis connection from `REDIS_URL`/`REDIS_URL_FILE`); do NOT open a second connection pool.
2. Identify every queue worth monitoring: the named **controller** queue (`phaze.tasks.controller.settings`) plus the per-agent / distributed-agent queues (`AgentTaskRouter`). Mount the dashboard over all of them.
3. Mount via `from saq.web.starlette import saq_web` → `app.mount("/saq", saq_web("/saq", queues=[control_queue, ...]))`. **Confirm the import path for the installed SAQ version** (`saq[redis]>=0.26.4`) — `saq.web.starlette` vs `saq.web` — before committing.
4. SAQ is already a direct dependency (workers use it); no new dependency. (If the web extra is needed at runtime, add `saq[web]` — verify against the installed version.)
5. Verify the mount does NOT break TLS startup, the `/health` healthcheck, or any existing router; and that `/saq` loads the dashboard listing the queue(s).
6. PR description must note the UI is intentionally unauthenticated at the app layer because it is only reachable behind the reverse proxy's internal-realm auth.

**Constraints:** No standalone web server, no new bound port, no auth middleware — the only change is mounting `saq_web` into the existing FastAPI app.
**Depends on:** Phase 31 (controller queue + lifespan queue wiring already in place from Phase 30/31)
**Plans:** 6 plans (4 complete; 2 gap-closure for L-02 — wave 1)
Plans:

- [x] 33-00-PLAN.md — Wave 0 harness: add FakeQueue.info() so saq_web renders without Redis
- [x] 33-01-PLAN.md — Wave 1: build_saq_app(/saq) mount helper + enable_saq_ui flag + unit tests
- [x] 33-02-PLAN.md — Wave 2: mount /saq in the lifespan (controller + per-agent queues) + integration tests
- [x] 33-03-PLAN.md — Wave 3: "Queue Monitor" link from the pipeline dashboard to /saq (operator request) + render test

### Phase 34: Pipeline Queue-Depth Status & Double-Enqueue Guard

**Goal:** Surface live SAQ queue depth on the pipeline dashboard so an in-flight analysis run is visible after a page refresh and the trigger buttons cannot double-enqueue. The DB cannot distinguish "nothing queued" from "everything queued" — files stay `DISCOVERED` until a worker finishes them, so after refresh the dashboard looks identical whether or not "Run Analysis" was clicked (the reported bug: 11,428 `process_file` jobs were live on `phaze-agent-nox` with 0 analyzed, yet the button stayed clickable). Fix by reading authoritative queue depth via `Queue.count("queued"/"active")` (cheap Redis `ZCARD`/`LLEN`) on the already-wired `app.state.controller_queue` and the per-agent `app.state.task_router` queues. New service `get_queue_activity(app_state, session)` returns `agent_queued`/`agent_active`/`controller_queued`/`controller_active` summed across all non-revoked agents (scheduled cron jobs excluded by `count`). Surface the counts through the existing 5s `/pipeline/stats` poll. Add a persistent OOB-swapped "Processing" card (`partials/processing_card.html`) above the stats bar showing a progress bar of `analyzed / (analyzed + agent_busy)` — `done` derived from the DB `analyzed` count (survives worker restarts) — plus "N queued · M active"; the card renders empty when idle. **Coarse** button disable via the Alpine `$store.pipeline`: Analyze / Fingerprint / Extract-Metadata disabled when `agent_busy > 0`; Generate Proposals disabled when `controller_busy > 0` (single-worker queue is processed serially, so coarse is honest — accepted trade-off that Fingerprint/Metadata are also blocked during an analysis run). Note: the dashboard currently renders only the Analyze + Proposals buttons; this phase ALSO adds the missing Fingerprint + Extract-Metadata buttons (wired to the already-existing `/pipeline/fingerprint` + `/pipeline/extract-metadata` HTMX endpoints) so all four actions are surfaced and gated (operator decision 2026-06-10).
**Design spec:** Approved inline (brainstorming session 2026-06-10); coarse disable + DB-derived progress denominator chosen by operator.
**Requirements**: Operability/observability of the pipeline-actions dashboard; prevents accidental duplicate-enqueue of the full corpus (~11,428 files).
**Depends on:** Phase 30 (enqueue_router + controller/agent queue wiring on `app.state`)
**Rollout:** Ships as a subsequent v4.0.x → GHCR publish → homelab redeploy.
**Status:** Complete (verified 2026-06-10 — VERIFICATION.md status: passed, 5/5 must-haves; full suite green, phase-module coverage 90.52%).
**Plans:** 5/5 plans executed

- [x] 34-00-PLAN.md — Wave 0: add seedable async `count` to `FakeQueue`/`FakeTaskRouter` test doubles
- [x] 34-01-PLAN.md — Wave 1: `get_queue_activity(app_state, session)` service with split failure isolation
- [x] 34-02-PLAN.md — Wave 2: wire counts + guarded percent into dashboard()/stats contexts + OOB store-write nodes
- [x] 34-03-PLAN.md — Wave 3: persistent `processing_card.html` (progress bar + queued/active, OOB-swapped)
- [x] 34-04-PLAN.md — Wave 3: four trigger buttons + coarse agentBusy/controllerBusy disable + store defaults

### Phase 35: Pipeline Determinism, Idempotency & Per-Job-Type Observability

**Goal:** Make every pipeline job schedule-safe (no duplicate queued items), idempotent (no duplicate rows), give the operator manual control over metadata extraction, and surface per-job-type progress on the dashboard. Generalizes the Phase 32 deterministic-key fix (which covered only `process_file`) to the whole pipeline. Surfaced by the 2026-06-11 queue-doubling incident: random-uuid `process_file` jobs from the pre-Phase-32 "Run Analysis" path could not dedup against the new deterministic-key re-enqueue, doubling the live queue to ~22,830 jobs over 11,428 files.

**Scope (5 work items):**

1. **Deterministic SAQ keys for ALL job types**, enforced CENTRALLY in the enqueue layer (`enqueue_router` / `agent_task_router` / a SAQ `before_enqueue` hook) so every task is keyed by construction as `<task>:<natural_id>` and no call site can drift. Today only `process_file` (`analysis_enqueue.py:64`) is keyed; `extract_file_metadata` (3 sites), `fingerprint_file`, `generate_proposals`, `scan_live_set`, `search_tracklist`, `scrape_and_store_tracklist`, `match_tracklist_to_discogs` all use random uuid keys.
2. **Audit + ensure ALL task DB writes upsert** (`ON CONFLICT DO UPDATE`) so re-runs never duplicate rows. Already idempotent (D-26): `agent_analysis`, `agent_metadata`, `agent_fingerprint`, `agent_files`, `agent_tracklists`. Verify/fill gaps: `generate_proposals` (proposals), `execute_approved_batch` (execution_log), `tag_write_log`.
3. **Remove auto metadata-extraction from discovery/scan** (`agent_files.py:130-161` D-20/21/22 + `ingestion.py:183-191` D-09 auto-enqueue `extract_file_metadata` per discovered music/video file). Make `extract_file_metadata` MANUAL-only — operator triggers it from the dashboard.
4. **Add a "Metadata" stage card** to the pipeline dashboard (`stage_cards.html`), counting files with extracted metadata, placed between Discovered and Fingerprinted.
5. **Per-job-type progress bars** on the dashboard (replace the single aggregate `processing_card.html`), backed by MAINTAINED per-function counters (SAQ hooks / Redis counter set), not live scans. **UI direction: render as a DAG view** — chosen design is sketch 001 Variant B ("Graph canvas": node-edge DAG on an SVG canvas, each node = a stage with live count + per-stage progress bar + trigger button gated by upstream deps + agent-busy). Items 3-4 (Metadata stage) and the per-job-type counters (item 5) feed the DAG nodes. Build note: draw edges from node anchor points (not hand-placed coordinates as in the throwaway sketch). Sketch: `.planning/sketches/001-pipeline-dag-view/`.
6. **Stage ordering & parallelization model** — formalize the stage DAG and which stages run concurrently, driven by the data-dependency research in `35-STAGE-DEPENDENCIES.md`. Findings: Discovery → {`extract_file_metadata` ∥ `fingerprint_file` ∥ `process_file` ∥ tracklist-branch} all parallel (each reads only the file on disk); `generate_proposals` joins on analysis **+** metadata only (NOT fingerprint/tracklist); tracklist sub-chain (`search`/`scan_live_set` → `scrape` → `discogs`) is sequential; `execute_approved_batch` is terminal (gated by proposals + approval). Use this to drive the orchestration fan-out and the per-job-type progress UI tiers.

**Locked decisions (operator, 2026-06-11):** (A) centralized enqueue-layer key enforcement (not per-call-site); (B) maintained per-function counters for progress data (not live scan, not SAQ-stats-only). Reverses the Phase 34 D-09/D-20/21/22 auto-extract behavior for metadata.
**Research artifact:** `35-STAGE-DEPENDENCIES.md` (stage DAG + evidence, written 2026-06-11).
**Requirements**: Schedulability without duplicate queue items; idempotent re-runs; operator-controlled metadata extraction; per-job-type pipeline observability.
**Depends on:** Phase 30 (enqueue_router seam), Phase 32 (deterministic-key pattern + `analysis_enqueue.py`), Phase 34 (dashboard processing card + stats poll).
**Rollout:** Ships as a subsequent v4.0.x → GHCR publish → homelab redeploy.
**Status:** Complete (verified 2026-06-12 — VERIFICATION.md status: passed, 6/6 must-haves; code review 2 blockers + 3 warnings fixed; UAT verified in-browser incl. a chip-overlap fix; full suite 1721 green).
**Plans:** 5/5 plans complete
Plans:
**Wave 1**

- [x] 35-01-PLAN.md — Centralized deterministic SAQ keys (before_enqueue hook + _KEY_BUILDERS) + maintained per-function counters (enqueued/after_process) + remove auto metadata-extraction (D-06) + drift-guard test [Wave 1]
- [x] 35-02-PLAN.md — Proposals idempotency: migration 019 (dedupe → partial unique index uq_proposals_file_id_pending) + store_proposals on_conflict_do_update (D-04) + execution_log/tag_write_log audit [Wave 1]
- [x] 35-03-PLAN.md — get_stage_progress reconcile query: per-stage output-table COUNT(DISTINCT), the D-03 DB-truth source for the parallel DAG nodes (RESEARCH Q5) [Wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 35-04-PLAN.md — Dashboard data plumbing: extend $store.pipeline + dashboard()/pipeline_stats_partial() contexts + stats_bar.html OOB per-node seeds [Wave 2]

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 35-05-PLAN.md — DAG canvas UI (sketch 001 Variant B): 9-node SVG graph with honest topology + gated triggers + <ol> fallback; removes stage_cards.html + processing_card.html (D-01) [Wave 3]

### Phase 36: Pipeline Queue Backend Migration (Redis to Postgres SAQ)

**Goal:** Migrate the SAQ task queue from the Redis backend to the Postgres backend so native per-job `priority` and `scheduled`-based job control become available (both are Postgres-only in SAQ; confirmed `saq/queue/postgres.py` dequeues `WHERE now>=scheduled AND priority BETWEEN .. ORDER BY priority, scheduled`). This is the enabling substrate for Phases 37–38.

**Scope:**

1. Swap dependency `saq[redis]` → `saq[postgres]` (pulls `psycopg`/`psycopg_pool` v3). SAQ runs its own psycopg3 async pool, **separate** from the SQLAlchemy/asyncpg engine; SAQ auto-manages its `saq_jobs` table.
2. New setting `PHAZE_QUEUE_URL` (Postgres DSN, defaults from the existing Postgres config). `controller.py` + `agent_worker.py` build `PostgresQueue.from_url(...)` instead of `Queue.from_url(redis_url, ...)`.
3. Redis container stays for cache/rate-limiting only — no longer the queue broker.
4. Carry over both before-enqueue hooks unchanged (`queue_defaults`, `deterministic_key`) — they are queue-level and backend-agnostic.

**Regression checks (highest-risk part):** Phase 32 reboot re-enqueue resilience, Phase 33 SAQ `/saq` monitoring UI (backend-agnostic `saq_web`, verify against Postgres), Phase 35 determinism/idempotency (deterministic-key dedup on Postgres). Smoke test enqueue→dequeue on Postgres.

**Deliverable (Step D — homelab):** Produce a ready-to-paste change prompt for the **homelab repo** agent: add `PHAZE_QUEUE_URL` env on control + agent services, image dep swap (`saq[redis]`→`saq[postgres]`), `saq_jobs` table first-boot/DB-perms note, Redis-no-longer-broker, redeploy ordering via `datum@nox` / `datum@lux`. (Final consolidation after Phase 38 if UI/control changes add env.)

**Requirements**: Queue backend on Postgres; native priority + scheduled-park available; no regression in reboot re-enqueue, SAQ UI, or determinism.
**Depends on:** Phase 35
**Rollout:** Ships as a v4.0.x → GHCR publish → homelab redeploy (paired with the Step D homelab change).
**Plans:** 6 plans (4 complete; 2 gap-closure for L-02 — wave 1)
**Status:** Complete (verified 2026-06-13 — VERIFICATION.md status: passed, 8/8 must-haves; full suite green 1721 passed; code review WR-01/IN-01/IN-02 resolved).
Plans:
**Wave 1**

- [x] 36-01-PLAN.md — Foundation: saq[postgres] dep swap, PHAZE_QUEUE_URL setting, build_pipeline_queue factory

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 36-02-PLAN.md — Core swap: all 4 construction sites → PostgresQueue via factory + cache-Redis decoupling (proposals, counters, pipeline)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 36-03-PLAN.md — Regression: real-PG priority/scheduled + dedup integration tests, /saq monitor + agent import-boundary
- [x] 36-04-PLAN.md — Step D homelab change-prompt + README/deployment/configuration/.env docs

### Phase 37: Per-Stage Pause and Priority Control Plane (table, API, worker hooks)

**Goal:** Add backend controls to pause and reprioritize the three agent pipeline stages — `metadata` (`extract_file_metadata`), `analyze` (`process_file`), `fingerprint` (`fingerprint_file`) — operating on the Postgres-backed `saq_jobs` table via plain UPDATEs.

**Scope:**

1. **`pipeline_stage_control` table** (Alembic migration): `stage` PK (metadata/analyze/fingerprint), `paused` bool, `priority` int (default 50, range 0–100, **lower = higher priority = sooner**, maps directly to SAQ `priority` — no inversion), `updated_at`.
2. **Enqueue hook** stamps every new job with its stage's current `priority`; if the stage is paused, also sets `scheduled = SENTINEL` (far-future) so the job parks on enqueue.
3. **Priority endpoint** `POST /pipeline/stages/{stage}/priority` (delta): update the control row, then `UPDATE saq_jobs SET priority=:n WHERE status='queued' AND <function=stage>` — reorders the already-queued backlog live.
4. **Pause endpoint** `POST /pipeline/stages/{stage}/pause`: set `paused=true`, `UPDATE saq_jobs SET scheduled=SENTINEL WHERE status='queued' AND <function=stage>`. Active jobs finish (drain semantics).
5. **Resume**: `paused=false`, `UPDATE saq_jobs SET scheduled=0 WHERE status='queued' AND <function=stage> AND scheduled=SENTINEL` — sentinel-guarded so genuine retry backoffs are never clobbered.

**Requirements**: Drain-style pause + live backlog reprioritization per agent stage; retry backoffs preserved; no double-pickup.
**Depends on:** Phase 36 (Postgres queue backend)
**Plans:** 6 plans (4 complete; 2 gap-closure for L-02 — wave 1)
**Status:** Complete (verified 2026-06-13 — VERIFICATION.md status: human_needed, 21/21 code must-haves verified; full suite green 1739 passed; code review WR-01/WR-02/IN-01 resolved; 2 homelab deployment-confidence UAT items deferred to 37-HUMAN-UAT.md).

Plans:
**Wave 1**

- [x] 37-01-PLAN.md — Schema foundation: PipelineStageControl model + migration 020 (seed 3 rows + CHECK 0-100) + STAGE_TO_FUNCTION/SENTINEL constants [Wave 1]

**Wave 2** *(blocked on Wave 1)*

- [x] 37-02-PLAN.md — apply_stage_control before_enqueue hook (TTL-cached job.queue.pool read) + raw saq_jobs UPDATE service helpers + build_pipeline_queue wiring + import-boundary test [Wave 2]

**Wave 3** *(blocked on Wave 2)*

- [x] 37-03-PLAN.md — Real-PG integration tests: drain-pause (REQ-37-1 + Pitfall-1 count), live reorder (REQ-37-2), sentinel-guarded resume (REQ-37-3), no-double-pickup concurrency (REQ-37-4) [Wave 3]

**Wave 4** *(blocked on Wave 3)*

- [x] 37-04-PLAN.md — FastAPI control endpoints (priority delta/pause/resume) + StagePriorityDelta schema + main.py registration + endpoint tests + README [Wave 4]

### Phase 38: Pipeline DAG Pause/Priority UI and Rescan Button Removal

**Goal:** Surface the Phase 37 controls on the pipeline DAG and remove the confusing duplicate scan affordance.

**Scope:**

1. **Remove the "Rescan Files" anchor** on the Discovery node (`dag_canvas.html` ~L202) — it just scrolled to the same `POST /pipeline/scans` form as "Start Scan"; confusing duplicate.
2. **Per-stage controls** on each of the 3 agent nodes: a **Pause/Resume** toggle and a **priority stepper** showing the raw number, with buttons labeled by intent — **"▲ Higher priority"** decrements the number, **"▼ Lower priority"** increments — plus a "lower runs first" hint. HTMX-posted to the Phase 37 endpoints.
3. **Extend `/pipeline/stats`** poll to return each stage's `{paused, priority}` so controls reflect live state across the 5s refresh.
4. Existing `agentBusy`-based trigger-button disabling stays as-is (out of scope; separate concern).

**Requirements**: Operator can pause/resume and raise/lower priority per agent stage from the DAG; Rescan button gone; live state reflected.
**Depends on:** Phase 37
**Rollout:** Final homelab Step D consolidation here if any new env/UI config emerges.
**Plans:** 3/3 plans complete
**Status:** Complete (verified 2026-06-13 — VERIFICATION.md status: human_needed, 4/4 REQs verified at source; full suite green 1750 passed; code review CR-01 blocker [priority endpoint form-encode] + WR-01 resolved; 5 browser/deployment-visual UAT items deferred to 38-HUMAN-UAT.md).
Plans:
**Wave 1**

- [x] 38-01-PLAN.md — Remove the dead "Rescan Files" anchor from the Discovery node (+ negative guard test) [Wave 1]
- [x] 38-03-PLAN.md — Degrade-safe get_stage_controls + _build_dag_context 6 int keys + base.html store seeds + OOB/store/degrade tests + README [Wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 38-02-PLAN.md — stage_controls macro (pause/resume + priority steppers) on the 3 agent nodes + NODE_LAYOUT recompute + <ol> a11y + guard-test updates [Wave 2]

> **Theme (Phases 39-42): "The DAG is the single manual control surface; automation only in recovery."**
> Today the tracklist sub-chain (Scan/Search, Scrape, Match) is display-only on the DAG — its triggers live on the Tracklists/Proposals pages — and a steady-state cron (`reenqueue_discovered`) effectively auto-runs Analyze. These phases make every stage manually triggerable from the DAG, each gated on its real prerequisite, and confine all automatic enqueueing to a restart/queue-loss recovery pass.

### Phase 39: Tracklist Search DAG Node — bulk manual search_tracklist trigger (button + endpoint + per-stage busy gating), gated on Metadata done

**Goal:** Make the DAG the control surface for name-based tracklist discovery. Split the display-only "Scan / Search" head into a triggerable **Search** node with a bulk pipeline-level endpoint that enqueues `search_tracklist` over eligible files (artist from extracted Metadata tags or parseable filename). Add the DAG trigger button + per-stage busy gating (same pattern as Phase 38 agent stages), **disabled until Metadata has produced tags**. Manual only — no auto-trigger.
**Requirements**: bulk search endpoint routes via `enqueue_router` (controller queue, not default); button gated on `metadataDone > 0`; per-stage busy count + "busy" gating reusing the Phase-38/quick-t7k pattern; regression tests for gating + routing.
**Depends on:** Phase 38 (DAG controls/gating pattern)
**Plans:** 1 plan
**Status:** Complete (shipped — PR #129).

Plans:

- [x] 39-01-PLAN.md — Bulk search_tracklist trigger endpoint + Search DAG node (metadataDone/searchBusy gate) + tests

### Phase 40: Tracklist Fingerprint-Scan DAG Node — bulk manual scan_live_set trigger (button + endpoint + gating), gated on discovered files + online agent; runs independently of Search

**Goal:** Add a second, independent tracklist-discovery node: a **Fingerprint Scan** node whose bulk endpoint enqueues `scan_live_set` (agent-side audio-fingerprint identification) over discovered files. Add the DAG trigger button + busy gating, **disabled unless there are discovered files AND an online file-server agent** (surface a clear "no active agent" state). Runs independently of Phase 39 — both produce tracklists, no fallback between them.
**Requirements**: bulk scan endpoint routes per-agent via `AgentTaskRouter` active-agent selection; 0-agent surfaces a visible disabled/empty state; button gated on `discovered > 0` + agent online; regression tests.
**Depends on:** Phase 38 (DAG pattern); independent of Phase 39
**Plans:** 0 plans
**Status:** Complete (shipped — PR #130).

Plans:

- [x] Shipped via PR #130 (planned inline; no separate plan file)

### Phase 41: Scrape and Match DAG Triggers — bulk scrape-pending (scrape_and_store_tracklist) and match-pending (match_tracklist_to_discogs) buttons, gated on tracklist existence

**Goal:** Give the **Scrape** and **Match** nodes real manual triggers. Scrape button bulk-enqueues `scrape_and_store_tracklist` for every tracklist missing a scraped version; Match button bulk-enqueues `match_tracklist_to_discogs` for every tracklist not yet linked to Discogs. Each is "bulk over pending" (skips already-done rows) and **disabled until ≥1 tracklist exists**.
**Requirements**: two bulk endpoints route to the controller queue via `enqueue_router`; gates on `scrapeTotal`/`matchTotal` derived from tracklist count; both skip already-complete rows (deterministic-key dedup); regression tests for pending-set selection + gating.
**Depends on:** Phases 39 and 40 (need tracklists to exist before scrape/match are meaningful)
**Plans:** 1 plan
**Status:** Complete (shipped — PR #131).

Plans:

- [x] 41-01-PLAN.md — bulk Scrape + Match controller-routed triggers, busy/pending service reads, node gating (Needs tracklist / All scraped|matched / Scraping…|Matching…), and regression tests

### Phase 42: Recovery-Only Pipeline Automation — gate reenqueue_discovered + generalize so the only automatic enqueue is a restart/queue-loss recovery pass restoring all in-flight stages; no steady-state auto-advance

**Goal:** Enforce the core principle across the pipeline: the ONLY automatic enqueue is a restart/queue-loss **recovery pass** that restores ALL in-flight stages (metadata, analyze, fingerprint, proposals, tracklist) to their prior queue state — never a steady-state auto-advance. Replace the unconditional every-5-min `reenqueue_discovered` cron (which effectively auto-runs Analyze) with restart/queue-loss detection that reconciles each stage's expected-vs-actual in-flight set once per recovery event.
**Requirements**: recovery trigger fires on detected restart/queue-loss (not a fixed interval); reconciliation covers every stage, not just DISCOVERED→analyze; idempotent via deterministic keys (no double-enqueue, ref Phase 32 incident); steady-state produces zero automatic enqueues; tests prove no auto-advance when queues are healthy.
**Depends on:** Phase 32 (reboot re-enqueue resilience — this generalizes and constrains it)
**Plans:** 2 plans
**Status:** Complete (shipped — PR #132).

Plans:

- [x] 42-01-PLAN.md — Backend recovery engine: recover_orphaned_work producer + queue-loss detector + shared all-stages pending-set helpers (anti-drift) + unit/integration tests
- [x] 42-02-PLAN.md — Wiring + surface: remove the */5 auto-advance cron, gate startup recovery, add the /pipeline/recover endpoint + global DAG Recover button + docs

### Phase 43: Analyze Throughput Fix — bound per-file analysis cost, kill-on-timeout, and surface analysis state

**Goal:** Make the Analyze stage actually drain. Long DJ/concert essentia analysis legitimately exceeds the 4h timeout (root-caused 2026-06-17: 72 timeouts vs 60 completions over ~57h; cost is O(file duration)). Bound per-file cost so a 3h set costs ≈ a 20-min track, kill runaway essentia children deterministically, stop wasteful retries, and make analysis outcomes (done / sampled / failed) visible in the file state machine. Backend-only — redeployable to the homelab immediately. Full root cause + decisions: `.planning/debug/analyze-4h-timeouts.md`.
**Requirements**:

- Cap + **even stride** windowing — caps **60 fine / 30 coarse** per file (config-exposed); when a file exceeds the cap, stride evenly across the whole file (constant cost, full-file coverage). Emit coverage (`windows_analyzed`/`windows_total`, `sampled` flag).
- **Kill-on-timeout** — replace the bare `ProcessPoolExecutor` (whose child is not killed on cancel, leaking compute + starving the 4-of-8 pool) with `pebble.ProcessPool` (or equiv) + an inner per-task timeout that SIGKILLs/recycles the child, below the SAQ job timeout.
- **State-machine fix** — set `FileState.ANALYZED` on successful analysis PUT; add `ANALYSIS_FAILED` on terminal failure; persist sampled/coverage (Alembic migration). Fixes the latent "re-enqueue all 11,428" bug (every file currently stuck `discovered`). Worker is Postgres-free → terminal-failure/coverage marking goes via a new control API endpoint.
- **Retry policy** — `retries=1` for transient errors, but treat `TimeoutError` as **terminal** (no wasteful re-run); lower the SAQ `process_file` timeout from 14400s to ~2h (inner timeout does the real killing).
- Regression tests for stride/cap, kill-on-timeout, state transitions, and timeout-terminal retry behavior.

**Depends on:** none (independent of 39–42; builds on the Phase 31 windowed-analysis design)
**Plans:** 6 plans (4 complete; 2 gap-closure for L-02 — wave 1)

Plans:

- [x] 43-01-PLAN.md — Kill-on-timeout pebble pool + inner-timeout/cap config knobs (Wave 1)
- [x] 43-02-PLAN.md — Cap + even-stride bounding (60/30) + coverage emit in analyze_file (Wave 1)
- [x] 43-03-PLAN.md — State machine (ANALYZED/ANALYSIS_FAILED) + coverage columns (migration 021) + worker-callable failure endpoint (Wave 2)
- [x] 43-04-PLAN.md — Enqueue policy (timeout 7200/retries 2) + timeout-terminal classification + coverage forwarding (Wave 3)

### Phase 44: Analyze Observability UI — straggler/failed count, sampled badge, deepen-analysis re-trigger

**Goal:** Surface the analysis outcomes Phase 43 starts recording. Add a dashboard count/list of failed/straggler files, a "sampled — more data available" badge on files that were strided, and a "deepen analysis" re-trigger that re-enqueues a sampled file with a higher/unbounded window budget. Lands after Phase 43 so the backend truth exists first.
**Requirements**: dashboard straggler/`ANALYSIS_FAILED` count + list; sampled badge driven by the coverage fields; "deepen analysis" action enqueues `process_file` with an elevated cap (via a payload flag); regression tests for the new reads + re-trigger.
**Depends on:** Phase 43 (consumes its state/coverage fields + control API)
**Plans:** 6 plans (4 complete; 2 gap-closure for L-02 — wave 1)
Plans:
**Wave 1**

- [x] 44-01-PLAN.md — ProcessFilePayload fine/coarse cap fields + enqueue_process_file pass-through + worker process_file threading (deepen backend lever)
- [x] 44-02-PLAN.md — degrade-safe straggler (saq_jobs) + ANALYSIS_FAILED (files.state) dashboard service reads + straggler_threshold_sec knob

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 44-03-PLAN.md — POST /pipeline/files/{file_id}/deepen re-trigger (per-agent routing, full payload, deterministic-key dedup)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 44-04-PLAN.md — dashboard straggler/failed card + sampled badge partial + deepen button + router context wiring

### Phase 45: Scheduling Ledger for Orphan Recovery — recover only previously-scheduled-and-lost work, not the entire domain backlog

**Goal:** Add a durable scheduling ledger that records "this `<task>:<natural_id>` was enqueued" at the single `before_enqueue` chokepoint and clears it on completion AND terminal failure, so recovery re-queues exactly `ledger − live saq_jobs keys − completed` through the existing keyed producers — never the complement-of-done domain backlog that detonated the queue (~11.4k never-scheduled files) in the 2026-06-18 incident.
**Requirements**: L-01 durable ledger written at the single before_enqueue chokepoint; L-02 ledger cleared on completion AND terminal failure (controller stages via after_process, agent stages via the existing control-side callback handlers); L-03 recovery re-queues `ledger − live keys − completed` via existing keyed producers; L-04 idempotent startup backfill from live saq_jobs; L-05 control-only boundary preserved (agent worker stays Postgres-free); L-06 reversible Alembic migration 022 + 85% coverage.
**Depends on:** Phase 42
**Plans:** 6/6 plans complete

Plans:

**Wave 1**

- [x] 45-01-PLAN.md — SchedulingLedger model + migration 022 + ledger service (upsert/clear/read + routing) + get_live_job_keys + WRITE hook + controller-stage CLEAR hook + queue ledger_sessionmaker wiring

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 45-02-PLAN.md — agent-stage ledger clears in the existing control-side callback handlers (analyze success+/failed, metadata/fingerprint/scan success/terminal) — Option-B-refined headline decision
- [x] 45-03-PLAN.md — rewrite recover_orphaned_work to replay `ledger − live − domain-completed` via existing keyed producers (incident regression: never-scheduled files left alone)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 45-04-PLAN.md — idempotent startup backfill_ledger_from_saq_jobs (deserialize queued/active blobs, DO NOTHING, keyed-only) + startup wiring before recovery

**Gap closure (wave 1)** *(close L-02 sub-gaps CR-01 + CR-02 from 45-VERIFICATION.md; parallel — disjoint files)*

- [x] 45-05-PLAN.md — CR-01: guard the scan_live_set no-match report_scan_terminal call (re-raise on retryable, swallow+log on terminal) so a controller hiccup no longer leaks scan_live_set:<file_id>
- [x] 45-06-PLAN.md — CR-02: add POST /{file_id}/failed terminal-failure callbacks for extract_file_metadata + fingerprint_file (control-side ledger clear) + agent-worker terminal-attempt acks + recovery regression test

### Phase 46: Heartbeat Starvation Fix — decouple agent liveness heartbeat from the SAQ worker concurrency pool so a worker saturated with long process_file jobs still reports liveness and is not marked DEAD

**Goal:** A file-server agent worker saturated with multi-hour `process_file` analysis jobs still reports liveness and stays `alive` — the heartbeat runs as an asyncio background task in the worker startup hook (cancelled on shutdown), decoupled from the SAQ `worker_max_jobs` dispatch pool that the old `CronJob` heartbeat competed for and was starved by.
**Requirements**: Heartbeat fires on a fixed ~30s cadence independent of dispatch-pool saturation (proven by test); CronJob removed; all existing defensive behavior preserved; ≥85% coverage; docs + orphaned `cron:heartbeat_tick` row cleanup documented.
**Depends on:** Phase 45
**Plans:** 1/1 plans complete
Plans:

- [x] 46-01-PLAN.md — Background-task heartbeat: send_heartbeat/_heartbeat_loop refactor + interval constant, startup launch/shutdown cancel + CronJob removal, starvation-independence + defensive-branch tests, docs + orphaned-cron-row cleanup

## Phase Details (v5.0 Cloud Burst Analysis)

> **Milestone goal:** Long sets that can't finish locally get analyzed on free cloud compute (OCI Always-Free A1, arm64), unattended. Dependency order: image → compute-agent type → routing+backfill → push pipeline → deployment+docs. Each phase = its own PR (worktree branch). arm64 essentia is proven this session (`spike/arm64-essentia-analysis`: BPM bit-identical, mood/style labels exact, window-for-window).

### Phase 47: Official arm64 essentia agent image

**Goal**: An official arm64 essentia analysis agent image exists on GHCR — essentia built **from source** (the wheel is x86-only) with the proven spike fixes — published by CI on a native arm64 runner, and proven to match the x86 analysis path.
**Depends on**: Phase 46 (prior milestone shipped); first v5.0 phase — no intra-milestone dependency.
**Requirements**: CLOUDIMG-01, CLOUDIMG-02, CLOUDIMG-03
**Success Criteria** (what must be TRUE):

  1. Operator can pull an arm64-tagged phaze agent image from GHCR that boots and imports essentia successfully on arm64 hardware.
  2. CI builds and pushes the arm64 image on a **native arm64 runner** (no QEMU) on the same release triggers as the x86 image, so a matching arm64 tag appears on every release.
  3. A CI parity guard runs full analysis (MusiCNN + discogs-effnet) on the arm64 image and confirms results match the x86 path within tolerance (BPM/key exact, model scores within a small epsilon); the build fails if parity breaks.**Plans**: 4 plans

**Wave 1**

- [x] 47-01-PLAN.md — arm64 agent Dockerfile (3.13 + essentia-from-source + 4 spike fixes; scoped requires-python reconciliation)
- [x] 47-03-PLAN.md — parity toolkit: bpm/key-exact + epsilon comparator, shared dump CLI, deterministic reference clip

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 47-02-PLAN.md — CI native-arm64 build + push (-arm64 tags, import-smoke), hadolint matrix, just recipes, tag test

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 47-04-PLAN.md — CI parity guard (x86 golden + build-blocking arm64 compare; fix #4 real-audio proof) + docs

### Phase 48: Compute-agent type

**Goal**: phaze recognizes a "compute agent" — a media-less, scan-rootless `kind="compute"` Agent that pulls analysis jobs and PUTs results exactly like a file-server agent, visible as available cloud capacity on the Agents admin page.
**Depends on**: Phase 47 (a working compute agent runs the arm64 image).
**Requirements**: CLOUDAGENT-01, CLOUDAGENT-02, CLOUDAGENT-03
**Success Criteria** (what must be TRUE):

  1. Operator can register a compute agent with empty scan roots and an explicit `kind="compute"` marker, and it appears on the Agents admin page.
  2. The Agents admin page distinguishes the compute agent (kind badge + liveness + queue depth) so the operator can see available cloud capacity at a glance.
  3. The compute agent drains its per-agent SAQ queue and PUTs analysis results over HTTP, with no access to media or app ORM tables (only the SAQ Postgres broker + cache Redis + HTTP API — import-boundary test passes).

**Plans**: 3 plans (2 waves)
**UI hint**: yes

Plans:

**Wave 1**

- [x] 48-01-PLAN.md — Schema foundation: Agent.kind column + ck_agents_kind_enum CHECK + migration 024 (backfill 'fileserver')

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 48-02-PLAN.md — Registration: `agents add --kind` flag (relax scan-roots for compute) + AgentSettings.kind (relax empty-scan-roots startup gate)
- [x] 48-03-PLAN.md — Visibility + boundary: kind badge partial + Kind column on the Agents admin page (per UI-SPEC) + reaffirm compute-agent ORM import boundary

### Phase 49: Duration routing & backfill

**Goal**: Analysis jobs route by duration — long files (≥ configurable threshold, default 90 min) go to an online compute agent, short files stay local with unchanged behavior, and the existing timed-out long files can be backfilled to the cloud without re-detonating the queue.
**Depends on**: Phase 48 (a compute agent + its queue must exist to route to); Phase 45 (scheduling ledger, already shipped).
**Requirements**: CLOUDROUTE-01, CLOUDROUTE-02, CLOUDROUTE-03, CLOUDROUTE-04
**Success Criteria** (what must be TRUE):

  1. A file whose `metadata.duration` ≥ the threshold is enqueued to an available compute agent's queue instead of the local agent.
  2. A file below the threshold continues to analyze on the local file-server agent with unchanged behavior.
  3. When no compute agent is online, a ≥threshold file is held in an "awaiting cloud" state and is **never** silently analyzed locally (where it would time out); the operator can see it waiting.
  4. Operator can backfill the existing 144 `analysis_failed` long files to the cloud, scoped through the Phase 45 scheduling ledger so only previously-scheduled work is re-driven (no whole-backlog over-enqueue).

**Plans**: 4 plans (3 waves)
**Wave 1**

- [x] 49-01-PLAN.md — Routing foundation: cloud_route_threshold_sec config, FileState.AWAITING_CLOUD, kind-filtered select_active_agent, duration/awaiting/backfill service helpers (Wave 1)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 49-02-PLAN.md — Per-file duration router fork + split-count response + "Awaiting cloud" count card (Wave 2)
- [x] 49-04-PLAN.md — State-driven release_awaiting_cloud cron + controller registration + D-04 pending regression (Wave 2)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 49-03-PLAN.md — Backfill endpoint + "Backfill to cloud" button + ledger-scoped re-drive (Wave 3)

### Phase 50: Push pipeline

**Goal**: A cloud-routed long file physically reaches the compute agent's local disk, is integrity-verified, analyzed, and cleaned up — the control plane keeping the pipeline "one ahead" with no orphaned scratch files and no double-enqueues.
**Depends on**: Phase 49 (routing must place files on the cloud queue first).
**Requirements**: CLOUDPIPE-01, CLOUDPIPE-02, CLOUDPIPE-03, CLOUDPIPE-04, CLOUDPIPE-05
**Success Criteria** (what must be TRUE):

  1. When the control plane schedules a cloud file, a file-server agent pushes it to the compute agent's scratch directory over rsync/SSH-over-Tailscale (the file-server initiates; the compute agent only receives into scratch).
  2. The compute agent verifies sha256 against the `FileRecord` after transfer before analyzing; a mismatch fails the job cleanly and triggers a re-push.
  3. The compute agent deletes its scratch copy after analysis completes (success or terminal failure), bounding local disk to the in-flight set.
  4. The control plane keeps at most the configured number of cloud files staged-or-in-flight ("stay one ahead", default 2 = one analyzing + one staged), driven by the scheduling ledger.
  5. A failed or interrupted push/analysis is re-driven with no orphaned scratch files and no double-enqueue (idempotent, ledger-tracked).

**Plans**: 8 plans
Plans:
**Wave 1**

- [x] 50-00-PLAN.md — Nyquist test stubs (push pipeline / staging cron / routing seam)
- [x] 50-01-PLAN.md — Contracts: PUSHING/PUSHED states, payload fields, push schemas, config knobs + _FILE secrets

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 50-02-PLAN.md — Totality guards (key/counter/router) + recovery classification of PUSHING/PUSHED
- [x] 50-03-PLAN.md — push_file rsync-over-SSH task + compute-only scratch janitor + agent-client callbacks
- [x] 50-04-PLAN.md — process_file scratch read + off-loop sha256 verify + finally cleanup; producer kwargs

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 50-05-PLAN.md — Internal-API push callbacks (pushed → enqueue process_file; mismatch → capped re-drive)
- [x] 50-06-PLAN.md — Routing seam → AWAITING_CLOUD hold + stage_cloud_window ≤N bounded cron

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 50-07-PLAN.md — Dashboard "Staged (pushing)" + "Analyzing (cloud)" count cards

### Phase 51: Deployment, config & docs

**Goal**: The compute agent is deployable and fully operator-controlled — a Tailscale-connected compose stack, every cloud-burst parameter configurable, an OCI A1 + Tailscale-ACL provisioning runbook, and a single master toggle that reverts to all-local analysis.
**Depends on**: Phase 50 (deploys the full working push pipeline).
**Requirements**: CLOUDDEPLOY-01, CLOUDDEPLOY-02, CLOUDDEPLOY-03, CLOUDDEPLOY-04
**Success Criteria** (what must be TRUE):

  1. Operator can bring up the compute agent from a cloud-agent compose file with Tailscale connectivity, no media mount, a scratch volume, and the arm64 image.
  2. Every cloud-burst parameter — threshold, max in-flight, agent concurrency, scratch dir, push SSH target, cloud queue name, and the master enable toggle — is configurable via pydantic-settings with `_FILE`-secret support.
  3. Operator can follow a runbook to provision an OCI Always-Free A1 and a Tailscale ACL scoping the A1 to exactly `lux:{5432,6379,8000}` + `nox→A1:22`, plus a least-privilege Postgres role for the queue broker.
  4. Operator can disable the entire cloud-burst feature with a single config toggle, reverting to all-local analysis with no other change.

**Plans**: 4 plans (2 waves)
Plans:
**Wave 1**

- [x] 51-01-PLAN.md — Master toggle: cloud_burst_enabled field + 3 gate sites (routing seam, staging cron, backfill) + unit tests (CLOUDDEPLOY-04, CLOUDDEPLOY-02)
- [x] 51-02-PLAN.md — docker-compose.cloud-agent.yml (worker-only, arm64, named scratch, host Tailscale) + invariant test (CLOUDDEPLOY-01)
- [x] 51-04-PLAN.md — Homelab change prompt: OCI A1 OpenTofu spec + Tailscale ACL JSON + least-privilege broker role SQL (CLOUDDEPLOY-03)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 51-03-PLAN.md — Docs: configuration.md knob table + new cloud-burst.md runbook + deployment.md pointer + README index (CLOUDDEPLOY-02/03/04 docs)

## Backlog (unscheduled — no phase number yet)

- **Distributed cloud analysis (burst the backlog).** _[SCHEDULED as v5.0 Cloud Burst Analysis, Phases 47-51 — narrowed to rsync-over-Tailscale to a free arm64 OCI A1 (essentia built from source), no object storage. See Phase Details (v5.0).]_ Offload long-file analysis to cloud x86 workers via the existing agent model: stage file to object storage → cloud worker pulls (presigned GET) → analyzes → PUTs result; **reconcile by `file_id`** (already end-to-end), sha256 for download integrity. Only new pieces: optional `source_url`+`sha256` on `ProcessFilePayload` + a "stager". essentia is **x86-only** (no aarch64 wheel; source build infeasible). Best near-free path = **GCP $300/90-day trial, x86 e2 spot, GCS same-region** (≈$0 out of pocket); min-cost paid = OCI E5 preemptible (~$100, free egress). **Gate: only pursue if nox throughput is still insufficient after the Phase 43 redeploy + re-measure** — bounding may make this moot. Full design: memory `reference-essentia-arm64-cloud-burst` + `project-analyze-4h-timeout-incident`.
