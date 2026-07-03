# Roadmap: Phaze

## Milestones

- ✅ **v1.0 MVP** — Phases 1-11 (shipped 2026-03-30)
- ✅ **v2.0 Metadata Enrichment & Tracklist Integration** — Phases 12-17 (shipped 2026-04-02)
- ✅ **v3.0 Cross-Service Intelligence & File Enrichment** — Phases 18-23 (shipped 2026-04-04)
- ✅ **v4.0 Distributed Agents** — Phases 24-29 (shipped 2026-05-17)
- ✅ **v5.0 Cloud Burst Analysis** — Phases 47-51 (shipped 2026-06-26)
- ✅ **v6.0 Kubernetes Burst Analysis** — Phases 52-56 (shipped 2026-06-29)
- ✅ **v7.0 UI Redesign (DAG-Centric Hybrid Console)** — Phases 57-62 (shipped 2026-07-02)
- ✅ **2026.7.0 Engineering Improvements** — Phases 63-66 (shipped 2026-07-03)
- 🚧 **2026.7.1 Multi-Cloud Backends** — Phases 67-71 (in progress)

## Phases

### 🚧 2026.7.1 Multi-Cloud Backends (Phases 67-71) — IN PROGRESS

**Milestone goal:** Generalize the single `cloud_target` selector (`local`/`a1`/`k8s`) into a declarative, cost-tiered `backends:` config registry that drains long, locally-timing-out audio files across **local + Kueue (1+ clusters) + cloud-compute (1+ providers) simultaneously** — ranked by operator-assigned `rank`, bounded by per-backend `cap`, preferring free/owned capacity and spilling to paid only under load. Static routing, **no provisioning**: phaze routes to whatever backends the operator has deployed and are online. **Zero new dependencies** — a pure application-code refactor over the existing v6.0 cloud-burst system (supersedes the v6.0 Phase 55 `cloud_target` selector). Design spine locked in `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` (on `main`, PR #182). REG-05 + revised MKUE-02/04 supersede the design's original "one shared bucket" decision per operator direction (2026-07-03). Version is provisional CalVer (`2026.7.1`), finalized at release. Phase numbering **continues** from 2026.7.0 (Phase 66 was the last integer), starting at **Phase 67** — not reset to 1.

**Execution discipline:** Dependency-strict order — Phases 67–68 are **behavior-preserving refactors** (67 config-only; 68 acceptance-gated by a byte-identical characterization test) that de-risk the first behavior-changing phase (69, the tiered scheduler), so any behavior diff surfaced from 69 on is attributable to the new multiplicity logic, not an accidental refactor regression. **Each phase ships as its own PR on a worktree branch — never a direct commit to `main`.** Requirements map 1:1 to categories: REG→67 · BACK→68 · SCHED→69 · MKUE→70 · BEUI→71 (all 21 mapped, 0 orphans). Phases **69** and **70** carry **research flags** (see `.planning/research/SUMMARY.md`): 69 for the drain↔reconcile lock-ordering + attempt-budget/cooldown split; 70 for the `cloud_job` one-row-per-file-vs-per-(file,backend) schema choice, `ComputeAgentBackend` `agent_ref`→`Agent.id` resolution, and live multi-cluster kr8s auth + multi-bucket staging. Full requirements in `.planning/REQUIREMENTS.md`; research in `.planning/research/{SUMMARY,FEATURES,PITFALLS}.md`.

- [ ] **Phase 67: Backend Registry & Config Model** — declarative `backends.toml` registry (id/kind/rank/cap) as the sole config surface + per-kind discriminated-union validators + the S3 staging-bucket registry (public/shared vs cluster-specific) + **removal** of `cloud_target` and the flat `s3_*`/`kube_*`/`compute_*` fields with **no back-compat shim** (call sites rewired to registry-derived reads); config-model-only, live all-local behavior preserved via a zero-config implicit-local default (REG-01..05)
- [ ] **Phase 68: Backend Protocol + 3 Implementations** — one `Backend` protocol (`is_available`/`in_flight_count`/`dispatch`/`reconcile`) with Local/ComputeAgent/Kueue bodies re-homing existing logic + `cloud_job.backend_id` additive migration + uniform per-backend in-flight accounting; behavior-preserving, acceptance-gated by a byte-identical characterization test (BACK-01..04)
- [ ] **Phase 69: Tiered Drain Scheduler** — rank-first eligible dispatch per file, per-backend `cap`, spill-when-full, offline→next-eligible re-dispatch with black-hole guard, stateless equal-rank tie-break, single-recovery-owner per kind; the first behavior-changing phase (SCHED-01..05) — **research flag**
- [ ] **Phase 70: Multi-Kueue (N Clusters)** — N concurrently-dispatched Kueue clusters, each staging to its REG-05-assigned bucket set (DIST-01 preserved), per-cluster probe + `backend_id`-scoped reconcile with per-backend failure isolation, per-(backend,bucket) cleanup (MKUE-01..04) — **research flag**
- [ ] **Phase 71: Deployment, Config, Docs & N-Lane UI** — N registry-derived per-backend lanes (available/offline, in-flight/cap, rank) read-only on the existing `/pipeline/stats` poll + master revert-to-all-local toggle + operator runbook/config docs incl. the `cloud_target`→`backends` migration (BEUI-01..03)

<details>
<summary>✅ 2026.7.0 Engineering Improvements (Phases 63-66) — SHIPPED 2026-07-03</summary>

Cleanup / engineering-debt paydown: faster parallel CI, code-change-gated builds, CalVer release versioning, a per-module coverage uplift, a docs-drift guard, and small UI/dead-code cleanup. **No product-behavior change, no backend behavior change.** Phase numbering continues from v7.0 (Phase 62 was the last integer; 57.1 was a decimal insert). This milestone *adopts* CalVer — the last `vN.M`-numbered planning cycle, its release the first CalVer tag (`2026.7.0`). Shipped as PRs #193/#194/#197/#198/#199. Full detail archived in `milestones/2026.7.0-ROADMAP.md`; requirements in `milestones/2026.7.0-REQUIREMENTS.md`.

- [x] **Phase 63: Parallel CI & Code-Change Gating** — partition the ~1,750-test suite into workflow-step buckets, fan out across parallel jobs, combine per-shard coverage into one Codecov upload, and skip heavy jobs on doc-only changes (skip-with-success) (CI-01..04) (completed 2026-07-02)
- [x] **Phase 64: Per-Module Coverage Uplift & Gate Raise** — raise the worst-offender modules to a per-module coverage floor with behavior-asserting tests and lift the enforced gate above today's 90.38%, wired into CI (COV-01, COV-02) (completed 2026-07-03)
- [x] **Phase 65: CalVer Adoption** — replace `vN.M` with `YYYY.MM.REVISION` (first tag `2026.7.0`) across the release procedure, version badges, image tags, and the milestone↔version mapping, historical record intact (VER-01..04) (completed 2026-07-03)
- [x] **Phase 66: Docs-Drift Gate & Dead-Code Sweep** — a CI gate cross-checking REQUIREMENTS.md traceability against passed phases + re-link the `/saq` monitor in the shell + delete vestigial dead code (DOCS-01, CLEAN-01, CLEAN-02) (completed 2026-07-03); guard-robustness follow-up hardened in PR #199

</details>

<details>
<summary>✅ v6.0 Kubernetes Burst Analysis (Phases 52-56) — SHIPPED 2026-06-29</summary>

K8s became a **third** analysis-routing target alongside local and the v5.0 OCI A1: long sets that can't finish locally run as ephemeral, quota-scheduled **Kueue batch Jobs** on a remote x64 cluster — the v5.0 control-plane choreography with the execution unit changed to a one-shot per-file Job. Full detail archived in `milestones/v6.0-ROADMAP.md`; audit in `milestones/v6.0-MILESTONE-AUDIT.md`.

- [x] **Phase 52: Job-runner image & one-shot entrypoint** — x86 GHCR image FROM the existing essentia base (zero new pip deps); one-shot presign-download → sha256-verify → windowed analyze → POST result → exit; honest exit codes; runtime-mounted internal CA (KJOB-01..05) (completed 2026-06-27)
- [x] **Phase 53: S3 object-staging leg** — control-plane presign (aioboto3, sole S3 importer) + file-server agent httpx-PUT upload + pod presigned GET; `file_id`-scoped keys; cleanup on every outcome; `cloud_job` sidecar migration (KSTAGE-01..05) (completed 2026-06-28)
- [x] **Phase 54: Kube submit/watch + reconcile cron** — suspended per-file Kueue Job (kr8s); fast submit + `*/5` reconcile cron; out-of-band callback authoritative; no ledger-seed; Inadmissible-vs-Pending (KSUBMIT-01..06) (completed 2026-06-28)
- [x] **Phase 55: Routing, state & ledger integration** — `cloud_target` selector (replaces `cloud_burst_enabled` bool) + `stage_cloud_window` K8s branch + `enqueue_router` additions + AST guard (the one live-seam edit) (KROUTE-01..06) (completed 2026-06-28)
- [x] **Phase 56: Deployment, runbook, config & docs** — Kueue admin runbook + least-privilege RBAC + `_FILE` secrets + transport-agnostic endpoints + LocalQueue startup probe + ephemeral-identity Agents-UI note + master toggle (KDEPLOY-01..06) (completed 2026-06-29)

**Post-audit fix (quick 260628-wzq):** JOB-ENV-CONTRACT — `build_job_manifest` now injects `PHAZE_JOB_FILE_ID` + `envFrom` (the pod's runtime env), closing the manifest→pod seam every admitted pod needs.

**Deferred (deployment-gated):** live K8s + real-S3 E2E (UAT 53/54/55) — re-run FIRST after the live rollout.

</details>

<details>
<summary>✅ v7.0 UI Redesign — DAG-Centric Hybrid Console (Phases 57-62) — SHIPPED 2026-07-02</summary>

Replaced the MVP tab-sprawl UI with a **DAG-centric hybrid console**: the pipeline DAG is the home + navigation spine (three-column shell — rail nav · stage workspace · per-file record slide-in), local/A1/k8s are first-class Analyze lanes, and every human approval unifies behind one before→after diff/approve gate. IA/template rewrite over existing routers/services — **no backend behavior change** (one scoped exception: Phase 57.1's incremental analyze-progress signal). Full detail archived in `milestones/v7.0-ROADMAP.md`; audit in `milestones/v7.0-MILESTONE-AUDIT.md`.

- [x] **Phase 57: Shell & DAG rail** — three-column shell, `GET /` (Analyze default) + `/s/<stage>` HTMX nav, ⌘K + header status strip, brand/theme preserved, 8 legacy routes redirect into the shell, seeded dead-template guard (SHELL-01..05) (completed 2026-06-30)
- [x] **Phase 57.1: Incremental window persistence & live analyze progress signal** — mid-flight `fine_windows_analyzed` counter (idempotent under Phase 32 re-enqueue) + `analysis_completed_at` discriminator gating partial rows out of proposals; scoped backend exception (PROG-01..03) (completed 2026-06-30)
- [x] **Phase 58: Enrich + Analyze workspaces** — Discover/Metadata/Fingerprint/Analyze views; 3 Analyze lane cards (local/A1/k8s) with Kueue quota-wait/Inadmissible; single `/pipeline/stats` poll fanout (WORK-01..05) (completed 2026-06-30)
- [x] **Phase 59: Identify workspaces** — Track-ID surfacing existing audfprint+Panako + rapidfuzz signals; Tracklist Search→Scrape→Match 3-step (IDENT-01..02) (completed 2026-07-01)
- [x] **Phase 60: Review & Apply** — unified before→after diff + Approve/Edit/Skip + server-predicate bulk-approve; Dedupe keeper-select; Cue preview; audit + reversible (REVIEW-01..05) (completed 2026-07-01)
- [x] **Phase 61: Full record + ⌘K + Agents** — per-file record slide-in, ⌘K palette (`@alpinejs/focus`), Agents page w/ ephemeral k8s identity, first-run empty state (RECORD-01..04) (completed 2026-07-02)
- [x] **Phase 62: Polish & cutover** — a11y baseline, narrow-width icon rail, docs/README refresh, CUT-02 dead-code cutover (20 legacy templates deleted, empty guard allowlist) (CUT-01..04) (completed 2026-07-02)

**Milestone audit:** PASSED — 28/28 requirements, 7/7 phases verified, 0 broken flows (`milestones/v7.0-MILESTONE-AUDIT.md`).
**Deferred (deployment-gated):** 57.1 UAT tests 8-9 (real multi-hour kill-9 on local/A1; live Kueue k8s progress) — confirm at the next homelab/cluster rollout.

</details>

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
| 55. Routing, state & ledger integration | v6.0 | 6/6 | Complete    | 2026-06-28 |
| 56. Deployment, runbook, config & docs | v6.0 | 7/7 | Complete    | 2026-06-29 |
| 57. Shell & DAG rail | v7.0 | 4/4 | Complete    | 2026-06-30 |
| 57.1. Incremental window persistence & live analyze progress signal | v7.0 | 4/4 | Complete    | 2026-06-30 |
| 58. Enrich + Analyze workspaces | v7.0 | 4/4 | Complete    | 2026-06-30 |
| 59. Identify workspaces | v7.0 | 3/3 | Complete    | 2026-07-01 |
| 60. Review & Apply | v7.0 | 4/4 | Complete   | 2026-07-01 |
| 61. Full record + ⌘K + Agents | v7.0 | 5/5 | Complete    | 2026-07-02 |
| 62. Polish & cutover | v7.0 | 4/4 | Complete    | 2026-07-02 |
| 63. Parallel CI & Code-Change Gating | 2026.7.0 | 4/4 | Complete    | 2026-07-02 |
| 64. Per-Module Coverage Uplift & Gate Raise | 2026.7.0 | 4/4 | Complete    | 2026-07-03 |
| 65. CalVer Adoption | 2026.7.0 | 2/2 | Complete   | 2026-07-03 |
| 66. Docs-Drift Gate & Dead-Code Sweep | 2026.7.0 | 3/3 | Complete    | 2026-07-03 |
| 67. Backend Registry & Config Model | 2026.7.1 | 0/TBD | Not started | - |
| 68. Backend Protocol + 3 Implementations | 2026.7.1 | 0/TBD | Not started | - |
| 69. Tiered Drain Scheduler | 2026.7.1 | 0/TBD | Not started | - |
| 70. Multi-Kueue (N Clusters) | 2026.7.1 | 0/TBD | Not started | - |
| 71. Deployment, Config, Docs & N-Lane UI | 2026.7.1 | 0/TBD | Not started | - |

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

### Phase 57: Shell & DAG rail

**Goal**: Visiting `/` renders the three-column "Hybrid Console" shell with the DAG rail as the navigation spine and **Analyze selected by default**; clicking a rail stage swaps the center workspace via HTMX with no full-page reload; the legacy tab-bar is gone, brand/theme are preserved, and every old per-tab route resolves into the shell. This is the **load-bearing foundation** — it locks the cross-cutting contracts (swap target, OOB fanout, `$store` survival, history, focus/ARIA, theme) that Phases 58-62 all depend on.
**Depends on**: Nothing (first v7.0 phase; sits on the unchanged v6.0 backend)
**Requirements**: SHELL-01, SHELL-02, SHELL-03, SHELL-04, SHELL-05
**Success Criteria** (what must be TRUE):

  1. Visiting `/` renders the three-column shell (DAG rail · `#stage-workspace` · per-file pane) with the Analyze rail node marked `aria-current="page"` — no redirect to `/pipeline`, no landing on a secondary tab.
  2. Clicking any rail stage swaps **only** `#stage-workspace` via HTMX (fragment response, never `extends base.html`) with `hx-push-url`; the header, rail, and pane survive the swap and `$store.pipeline` state persists across it.
  3. The legacy top tab-bar is removed; global search is a ⌘K header affordance and compute/agent status shows in a header status strip — both fed by the single `/pipeline/stats` 5s poll fanned out via `hx-swap-oob` behind the `oob_counts` gate (no per-region poll loops).
  4. The existing auto/dark/light theme toggle and the Jura/blue/wave-logo brand survive verbatim from `base.html`'s `<head>` (no FOUC, `dark:` utilities work, vendored Tailwind, recomputed SRI).
  5. Each of the 8 legacy routes (`/pipeline`, `/proposals`, `/tracklists`, `/tags`, `/cue`, `/duplicates`, `/search`, `/preview`) resolves in ≤1 hop to a 200 with the matching rail node pre-selected (a redirect-loop test asserts this), and a **seeded dead-template AST guard test is green** (watched green through cutover).

**Notes**: Risk phase — do not under-scope. Lock the single stable `#stage-workspace` swap-target id, the fragment-only stage-response convention, the OOB id registry + `oob_counts` gate, `$store.pipeline` consumption (not redefinition), the `htmx:historyRestore` re-init handler, and the focus-to-heading + skip-link (→ `#stage-workspace`) baseline. Stack: bump htmx→2.0.10 / Alpine→3.15.12 / Tailwind→4.3.2 and recompute every `integrity=` SRI hash (a stale hash silently blocks the script); stay on htmx 2.0.x (4.0 is beta). SHELL-05 is hybrid: canonical-URL routes render-in-shell, true renames (`/pipeline`→`/`, `/search`→⌘K) use `RedirectResponse` on the trailing-slash canonical form (FastAPI `redirect_slashes=True`). No phase research needed — all patterns are in-repo.**Plans**: 4 plans (4 waves)
**Wave 1**

- [x] 57-01-PLAN.md — Stack bumps (htmx 2.0.10 / Alpine 3.15.12 / Tailwind 4.3.2 + recomputed SRI) + seeded dead-template AST guard (wave 1)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 57-02-PLAN.md — Shell router (`GET /` + `GET /s/{stage}`) + structural three-column shell + Analyze default + theme/brand preservation + `/pipeline`→`/` (wave 2)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 57-03-PLAN.md — DAG rail nav spine + header status strip + ⌘K skeleton modal, wired into the shell (wave 3)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 57-04-PLAN.md — Conditional legacy-route redirects (7 routers) + ≤1-hop redirect-resolution test (wave 4)

**UI hint**: yes

### Phase 57.1: Incremental window persistence & live analyze progress signal (INSERTED)

**Goal:** Bump a progress **count** (`analysis.fine_windows_analyzed`/`fine_windows_total`) **incrementally as each window completes** during `analyze_file`, instead of only atomically at completion — exposing a **read-only, per-file mid-flight progress signal** the Phase 58 Analyze workspace can display for in-flight files. **Counter-only (57.1-CONTEXT D-01):** the `analysis_window` **detail** rows continue to land atomically at completion via `put_analysis`; they are NOT written incrementally — so the mid-flight write is a lightweight counter on a partial `analysis` row. Must remain **idempotent and safe under Phase 32 reboot re-enqueue**: a file killed mid-analysis leaves only a partial `analysis` row whose counter a re-run overwrites cleanly (reusing `put_analysis`'s file_id-keyed replace). A partial in-progress row must NOT be treated as a completed analysis by proposals/search/sort — gated on a new `analysis_completed_at` completion discriminator (the KEY RISK). **Deliberate, scoped exception to the v7.0 "no backend behavior change" milestone rule** (approved 2026-06-29): this is the one analysis-pipeline change v7.0 makes, isolated here so the Phase 58 UI stays presentation-only. NO new queue/task/routing semantics; representative aggregates (median BPM, modal key, dominant mood/style), the `analysis_window` rows, and the final `ANALYZED` flip are unchanged. First plan task is a spike confirming incremental counter persistence + crash-mid-run idempotency on a real long file.
**Requirements**: PROG-01, PROG-02, PROG-03
**Depends on:** Phase 57 (builds on Phase 31 windowed analysis + Phase 32 reboot resilience, both shipped)
**Plans:** 4/4 plans complete
Plans:
**Wave 1**

- [x] 57.1-01-PLAN.md — SPIKE: resolve the pebble/k8s transport fork + prove crash-mid-run idempotency on a real long file (PROG-01/02)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 57.1-02-PLAN.md — Completion discriminator (analysis_completed_at, migration 028) + tighten the proposal convergence gate so a partial row never leaks (KEY RISK / PROG-03)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 57.1-03-PLAN.md — Counter-only progress endpoint + AnalysisProgressPayload + agent_client.post_analysis_progress (fine-only; PROG-01/03)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 57.1-04-PLAN.md — Thread progress_cb through analyze_file + wire the pebble Queue-drainer and k8s to_thread bridges + throttle knob (PROG-01/02)

### Phase 58: Enrich + Analyze workspaces

**Goal**: The shell's first real content — Discover, Metadata, Fingerprint, and Analyze stage workspaces over their **existing** endpoints, with the Analyze workspace presenting the three execution lanes (local / A1 / k8s) as first-class live-capacity cards. All live updates ride the one `/pipeline/stats` 5s poll established in Phase 57.
**Depends on**: Phase 57 (every workspace swaps into the shell and consumes its `$store.pipeline` + OOB fanout); Phase 57.1 (WORK-04's in-flight windowed-progress reads the read-only mid-flight signal PROG-03 delivers)
**Requirements**: WORK-01, WORK-02, WORK-03, WORK-04, WORK-05
**Success Criteria** (what must be TRUE):

  1. Selecting Discover shows recent scans plus the count of discovered-but-not-yet-enriched files, with a scan trigger.
  2. Selecting Metadata or Fingerprint shows that stage's file queue with its existing manual trigger (metadata stays manual per the Phase 35 decision), backed by the existing endpoints.
  3. The Analyze workspace shows three execution-lane cards — local / A1 / k8s — each with live capacity, and the k8s lane surfaces Kueue **quota-wait vs. Inadmissible** state.
  4. Each in-flight Analyze file shows which lane (local/A1/k8s) it is running on and its windowed progress.
  5. Stage workspaces refresh live via the existing stats-poll (no manual reload) — verifiably **one** request per 5s in the network tab, with a `visibilitychange` guard that sheds polling when the tab is backgrounded.

**Notes**: WORK-05 is a discipline, not a feature — reuse the `stats_bar.html` OOB-seed contract for rail counts + header status strip and add **no second poll loop**. Data sources all exist (`pipeline.py`, `pipeline_scans.py`, `pipeline_stages.py`); the local/A1/k8s lane-card partials exist from v6.0. No phase research needed. **Planning finding (2026-06-30):** the v7.0 shell has NO live `/pipeline/stats` poll element today (only the legacy `dashboard.html` does) — Plan 01 wires the single persistent poll + `visibilitychange` shed into shell chrome.
**Plans**: 4 plans (sequential — shell.py STAGE_PARTIALS + the test file are shared chokepoints; one stage swapped per wave so the app stays usable at every commit)
**UI hint**: yes
Plans:
**Wave 1**

- [x] 58-01-PLAN.md — Live-poll foundation: persistent `#pipeline-stats` poll + `visibilitychange` shed in shell chrome + Phase-58 test scaffold + D-02 UI-SPEC reconciliation note (WORK-05) [Wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 58-02-PLAN.md — Shared scaffold/file-table/poll-seed-target partials + Discover workspace (recent scans + not-yet-enriched derived seed + SCAN/RECOVER) (WORK-01, WORK-05) [Wave 2]

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 58-03-PLAN.md — Metadata + Fingerprint workspaces: queue tables + EXTRACT ALL / FINGERPRINT ALL wired verbatim to existing endpoints (D-01/D-02) (WORK-02, WORK-05) [Wave 3]

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 58-04-PLAN.md — Analyze workspace: 3 always-render lane cards (local/A1/k8s) + reused cloud cards + all-in-stage file table with per-file lane badge + windowed progress (D-03/D-04/D-05/D-06) (WORK-03, WORK-04, WORK-05) [Wave 4]

### Phase 59: Identify workspaces

**Goal**: The Identify stages — a Track-ID workspace surfacing each file's **existing** identity signals, and a Tracklist workspace presenting the Search→Scrape→Match sub-chain inline as a visible 3-step. Presentation-only over existing data; no new identity backend.
**Depends on**: Phase 58 (reuses the workspace pattern — header + counts + action + table — established there)
**Requirements**: IDENT-01, IDENT-02
**Success Criteria** (what must be TRUE):

  1. The Track-ID workspace shows each file's existing identity signals — audfprint + Panako fingerprint match/score and rapidfuzz tracklist-match confidence — surfaced as match state and confidence.
  2. The Tracklist workspace presents the Search→Scrape→Match sub-chain inline as a visible 3-step with per-set match progress, triggerable from one surface.

**Notes**: **IDENT-01 re-scoped 2026-06-29** — the prototype's "AcoustID→MusicBrainz" label is dropped: `grep -ri 'acoustid|musicbrainz' src/phaze` is empty, so that backend does not exist and building it would violate the no-backend-change boundary. This phase ships the existing fingerprint + tracklist signals **only**; AcoustID/MusicBrainz is deferred to IDENT-03 (future milestone). Option 1 (re-label) is chosen → no phase research needed. (Verify what `models/fingerprint.py` persists at plan time so Track-ID surfaces the real stored fields.)
**Plans**: 3 plans (3 waves — sequential; both workspaces touch shell.py + the shared test file)Plans:
**Wave 1**

- [x] 59-01-PLAN.md — Wave-0 test scaffold + the two read-only row-assembly helpers (get_trackid_stage_files / get_tracklist_set_rows)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 59-02-PLAN.md — Track-ID workspace (combined per-file identity table) + shell wiring

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 59-03-PLAN.md — Tracklist workspace (3 step cards + per-set coverage table) + shell wiring

**UI hint**: yes

### Phase 60: Review & Apply

**Goal**: The highest-stakes interaction unified behind one gate — Rename/Path, Tag-write, and Move-files each as a before→after diff with per-file Approve/Edit/Skip and a **server-evaluated** bulk "approve all high-confidence"; Dedupe keeper-select; Cue preview/approve; every applied change audited and reversible. All over the existing approve/undo/execution endpoints — no backend behavior change.
**Depends on**: Phase 58 (the file-row → pane plumbing used by Review is established there)
**Requirements**: REVIEW-01, REVIEW-02, REVIEW-03, REVIEW-04, REVIEW-05
**Success Criteria** (what must be TRUE):

  1. Rename/Path, Tag-write, and Move-files each present pending changes as a before→after diff with per-file Approve / Edit / Skip (one Jinja diff partial over the three existing data sources).
  2. Each of those queues offers a bulk "approve all high-confidence" action that sends a **server-evaluated predicate** (action + fixed threshold) — the server re-queries pending rows above threshold at submit time, never a client-built `selectedRows` id-list.
  3. Dedupe presents duplicate groups with keeper-selection (others archived) and a bulk auto-keep-highest-quality action.
  4. Cue-sheet generation is reviewable with a preview and approve, gated on a matched tracklist.
  5. Every applied change (rename, tag-write, move, dedupe) writes an `ExecutionLog` audit row and is reversible (assert one audit row per apply).

**Notes**: Most correctness-sensitive phase (irreplaceable archive). REVIEW-02 fixes the live-polling stale-bulk-approval hazard — pick a fixed server-side confidence threshold at plan time (REVIEW-06 defers configurable thresholds; check the `tracklists.py` `reject-low` endpoint as a reference value). The 5s diff-list poll must OOB-update counts **only** — never re-render the operator's in-progress selection subtree. No phase research needed.
**Plans**: 5 plans in 2 waves

- [x] 61-01-PLAN.md — Foundation: @alpinejs/focus dep + SRI gate (shell.html+base.html) + Wave-0 tests/fixtures
- [x] 61-02-PLAN.md — Full-record slide-in (RECORD-01): GET /record/{file_id} fragment, persistent x-trap host, composed body, row/⌘K open
- [x] 61-03-PLAN.md — ⌘K command palette (RECORD-02): distinct_artists() + grouped results + roving arrow-nav + x-trap
- [x] 61-04-PLAN.md — Agents page (RECORD-03): heartbeating section + ephemeral compute lanes (classify_compute_lanes, never DEAD)
- [x] 61-05-PLAN.md — First-run empty state (RECORD-04): agent-roots guide + DISCOVERY scan (POST /pipeline/scans)

**UI hint**: yes

### Phase 61: Full record + ⌘K + Agents

**Goal**: Additive depth over the now-live shell — a full per-file record slide-in, the ⌘K command palette over the existing search service, an Agents page that models the k8s burst lane as an ephemeral Job-based identity, and a first-run empty state. Composes existing partials; introduces exactly one new CDN dep.
**Depends on**: Phase 60 (the record slide-in links into workspace fragments — lane badges, pending-approval rows — that Phases 58-60 must have built first)
**Requirements**: RECORD-01, RECORD-02, RECORD-03, RECORD-04
**Success Criteria** (what must be TRUE):

  1. Opening a file (from a row or ⌘K) shows a full per-file record: identity, metadata diff, windowed multi-lane analysis timeline, this file's pending approvals (inline-approvable), and history.
  2. ⌘K opens a command palette searching files / tracklists / artists and offering quick commands (scan, jump to a stage or review queue) — funneled through the existing search service and `enqueue_router` guards.
  3. The Agents page shows local and A1 as heartbeating agents and the k8s burst lane as an ephemeral, Job-based identity (liveness derived from in-flight Kueue workloads) — never a perpetually-DEAD agent (carries v6.0 KDEPLOY-04 intent into the new UI).
  4. When no files exist, a first-run empty state guides the operator to point phaze at a directory and shows live scan progress.

**Notes**: Introduce `@alpinejs/focus@3.15.12` here (the one new dep) — load the plugin `<script defer>` before Alpine core, version exactly matching Alpine core; use `x-trap.inert.noscroll` for the ⌘K palette and the slide-in panel focus-trap. The pane/record must ride the existing single poll — add no new loop. Verify the ⌘K "artists" facet maps to existing search fields at plan time (no backend change either way). No phase research needed.
**Plans**: TBD
**UI hint**: yes

### Phase 62: Polish & cutover

**Goal**: Close the milestone — baseline accessibility at parity-or-better, removal of the dead legacy templates/routers now that every stage is superseded, updated docs/README, and a narrow-width rail-collapse. **CUT-02 is necessarily last**: dead-code removal is only safe after every legacy route is render-in-shell-or-redirected and every page is replaced.
**Depends on**: Phase 61 (every workspace, the record, ⌘K, and Agents must all be live before legacy wrappers can be deleted without breaking SHELL-05 redirects)
**Requirements**: CUT-01, CUT-02, CUT-03, CUT-04
**Success Criteria** (what must be TRUE):

  1. The redesigned UI meets baseline accessibility — keyboard navigation for the rail and ⌘K, visible focus states, a skip link, and ARIA on the DAG — at parity with or better than today (full a11y audit sign-off).
  2. Dead templates, routers, and partials from the old tabbed UI are removed once superseded — the Phase 57 dead-template AST guard goes **green** after removing the legacy page wrappers; surviving `partials/` (now the shell's fragments) are kept.
  3. User-facing docs and the per-service README are updated to describe the new information architecture.
  4. The shell degrades reasonably at narrow widths — the rail collapses to icons — for the single-user desktop tool.

**Notes**: CUT-02 deletes the legacy page wrappers (`proposals/list.html`, `tags/list.html`, `duplicates/list.html`, `cue/list.html`, `tracklists/list.html`, `search/page.html`, `preview/tree.html`, `pipeline/dashboard.html`, and the `base.html` nav block) via three-way grep + the dead-template test; **keep all `partials/`** — they became the shell's fragments. No phase research needed.
**Plans**: 4 plans (3 in wave 1 · CUT-02 cutover last in wave 2)
Plans:
**Wave 1**

- [x] 62-01-PLAN.md — CUT-01 accessibility: close the ⌘K combobox accessible-name gap, remove the dead detail-pane aside, and lock the a11y baseline with a filesystem structural guard [Wave 1]
- [x] 62-02-PLAN.md — CUT-04 narrow-width rail: max-lg icon-only collapse + 15 per-stage inline-SVG glyphs (sr-only labels, title tooltips) + collapse guard [Wave 1]
- [x] 62-03-PLAN.md — CUT-03 docs: refresh README + docs/architecture.md + docs/project-structure.md + quick-start nav for the DAG-centric IA + docs-currency guard [Wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 62-04-PLAN.md — CUT-02 dead-code cutover (LAST): delete 8 wrapper templates + orphaned partials, strip base.html tab-bar nav, drain the dead-template allowlist to empty [Wave 2]

**UI hint**: yes

## Phase Details (2026.7.0 Engineering Improvements)

### Phase 63: Parallel CI & Code-Change Gating

**Goal**: CI runs materially faster by executing the ~1,750-test suite as parallel, independently-selectable workflow-step buckets with correct combined coverage — and skips the heavy build/test/security jobs on documentation-only changes while keeping every required status check satisfiable. All CI-workflow work, one PR.
**Depends on**: Nothing (first phase of this milestone; restructures the existing CI on top of v7.0)
**Requirements**: CI-01, CI-02, CI-03, CI-04
**Success Criteria** (what must be TRUE):

  1. The pytest suite is partitioned into independently-runnable workflow-step buckets — discovery, metadata, fingerprint, analyze, identify/tracklist, review/apply, agents/distributed, plus a generic/shared bucket (schema, config, helpers, routing) — each selectable in isolation without running the whole suite. (CI-01)
  2. CI fans the buckets out across parallel jobs (job matrix and/or `pytest-xdist`) rather than one serial run, measurably cutting wall-clock CI time. (CI-02)
  3. Per-shard `.coverage` files are combined into a single coverage report and one Codecov upload, preserving the enforced coverage gate with no per-shard loss and no double-counting. (CI-03)
  4. A docs-, `.planning/`-, or markdown-only PR skips the heavy build/test/security jobs while the required status checks still report **success** (skip-with-success, not skip-absent — a doc-only PR stays mergeable under branch protection). (CI-04)

**Notes**: CI-03's combine step must be trustworthy before Phase 64 raises the enforced gate. Resolve at planning: marker vs directory vs xdist sharding vs job matrix; where real-Postgres integration tests bucket; and the code-change-detection mechanism (changed-files gate job over bare `paths-ignore`) that avoids the "required check never runs → PR can't merge" trap. CI workflows must delegate to `just` recipes per project convention.
**Plans**: 4 plans (3 waves)
Plans:
**Wave 1**

- [x] 63-01-PLAN.md — Foundation: pytest-xdist (legitimacy-gated) + coverage relative_files + just test-bucket/coverage-combine + tests/buckets.json [Wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 63-02-PLAN.md — Directory reorg into 9 buckets (collision-safe layer sub-nesting + migrations-import fix) + partition guard, full suite green at baseline [Wave 2]

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 63-03-PLAN.md — tests.yml bucket matrix (fromJSON, per-leg services + coverage shards) + combine job (single coverage.xml + single Codecov upload) [Wave 3]
- [x] 63-04-PLAN.md — ci.yml classifier broadened (.planning/**/LICENSE/docs/.txt) as a tested delegated script + change-gate regression tests; required-check contract untouched [Wave 3]

### Phase 64: Per-Module Coverage Uplift & Gate Raise

**Goal**: Raise the under-covered tail of source modules behind a per-module coverage floor with tests that assert real observable behavior (not coverage-padding), then lift the enforced coverage gate above today's baseline and wire it into CI so future regressions fail the build.
**Depends on**: Phase 63 (the combined-across-shards coverage plumbing must be correct and trustworthy before a higher gate is enforced on it)
**Requirements**: COV-01, COV-02
**Success Criteria** (what must be TRUE):

  1. The prioritized worst-offender and v7.0-touched modules — `services/agent_liveness.py`, `routers/shell.py`, `services/pipeline.py`, `routers/tracklists.py`, `routers/pipeline.py`, `main.py`, plus the 71–78% tail — each meet a per-module coverage floor, with the added tests asserting observable behavior. (COV-01)
  2. The enforced coverage gate is raised above the current 90.38% project baseline (exact project and/or per-module target set at plan time). (COV-02)
  3. The raised gate is wired into CI so a future coverage regression below the floor/gate fails the build. (COV-02)

**Notes**: Behavior-first tests only — no assertions written solely to touch lines. Interacts with CI-03: the combined coverage number the gate enforces must already be correct (hence the Phase 63 dependency). No product/backend behavior change. **Re-baselining (planning, 2026-07-02):** the Success-Criteria worst-offender percentages are a no-DB measurement artifact — against the authoritative COMBINED coverage (2566 tests, DB up) overall is 96.89% and the ONLY sub-floor module is `services/review.py` at 83.16%. The named offenders (shell/pipeline/tracklists/main) are all ≥90% combined (main.py 100%). The phase's engineering value is the floor-enforcement machinery + a defensible raised gate, not mass test-writing; SC #1's module list is honored by the floor clearing (D-06), not by per-module test waves.
**Plans**: 4 plans in 3 waves
Plans:
**Wave 1**

- [x] 64-01-PLAN.md — Per-module floor machinery: `scripts/coverage_floor.py` (stdlib-only, D-01/D-02/D-03/D-04) + unit test [Wave 1]
- [x] 64-02-PLAN.md — `services/review.py` uplift ≥85% via behavior-asserting degrade/formatter tests (+ agent_liveness margin) [Wave 1]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 64-03-PLAN.md — Raise the global gate >90.38 (D-05) + wire the floor into `just coverage-combine` + gate-consistency guard test [Wave 2]

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 64-04-PLAN.md — Verify the combine job is a merge-blocking required check (fail-closed CI gate) [Wave 3]

### Phase 65: CalVer Adoption

**Goal**: Move release versioning from milestone-aligned `vN.M` to calendar-based `YYYY.MM.REVISION` (no leading-zero month; first tag `2026.7.0`) across the release procedure, version badges, published image tags, and the milestone↔version mapping — without breaking the historical `vN.M` record. This is the milestone that *adopts* CalVer, so its own release is the first CalVer tag.
**Depends on**: Nothing (independent of the CI/coverage line — parallel-friendly isolation)
**Requirements**: VER-01, VER-02, VER-03, VER-04
**Success Criteria** (what must be TRUE):

  1. Release versioning uses CalVer `YYYY.MM.REVISION` with no leading-zero month (first release `2026.7.0`) and a REVISION convention that supports multiple same-month patch releases. (VER-01)
  2. The release procedure (pyproject `version` + `uv.lock` bump → annotated tag push → GHCR publish) and the README version/badge line reflect the CalVer scheme. (VER-02)
  3. Published Docker image tags and any compose/deploy references use the CalVer version. (VER-03)
  4. The milestone↔version mapping in ROADMAP.md and MILESTONES.md reads milestones as named and releases as dated, without breaking the historical `vN.M` record. (VER-04)

**Notes**: Retroactively re-tagging historical `vN.M` releases as CalVer is explicitly out of scope — CalVer applies going forward. Keep README badges on one line; do not re-add removed badges. Preserve the annotated-tag-PUSH-triggers-GHCR-publish invariant (see memory `project-release-procedure`).**Plans**: 2 plans
**Wave 1**

- [x] 65-01-PLAN.md — RED test gate: retarget the CI glob guard to CalVer + add MILESTONES-mapping & CalVer-scheme structural guards

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 65-02-PLAN.md — GREEN: swap ci.yml to the CalVer-only tag glob, bump pyproject `2026.7.0` + uv.lock, add the MILESTONES mapping table, rewrite forward-looking CalVer docs

### Phase 66: Docs-Drift Gate & Dead-Code Sweep

**Goal**: Close the small remaining engineering-debt items in one PR — a CI gate that keeps REQUIREMENTS.md traceability honest against passed phases, a discreet re-link to the still-mounted `/saq` monitor in the shell, and removal of the vestigial dead code (plus the dead-template guard's own blind spot) surfaced during the v7.0 cutover.
**Depends on**: Phase 63 (the traceability CI gate slots cleanly into the restructured CI; CLEAN-01/02 are otherwise independent)
**Requirements**: DOCS-01, CLEAN-01, CLEAN-02
**Success Criteria** (what must be TRUE):

  1. A CI gate cross-checks REQUIREMENTS.md traceability against passed phases and **fails** when the table is stale — a passed phase's requirements left unmarked, or a requirement marked without a passed phase. (DOCS-01)
  2. A discreet in-UI link to the still-mounted `/saq` SAQ monitor is reachable from the shell (natural home: the Agents/Compute page) without typing the raw URL. (CLEAN-01)
  3. Vestigial dead code (unused templates, routers, and assignments surfaced during the v7.0 cutover) is identified and removed. (CLEAN-02)
  4. The dead-template guard's blind spot for its own unused entry-root literals (per the v7.0 retrospective) is closed. (CLEAN-02)

**Notes**: CLEAN-01/02 are presentation- and dead-code-only — no backend behavior change. DOCS-01 closes the manual REQUIREMENTS/ROADMAP sync gap called out across the retrospectives.
**Plans**: 3 plans
**UI hint**: yes

**Wave 1** *(parallel — no file overlap)*

- [x] 66-01-PLAN.md — DOCS-01 traceability drift guard (5 drift classes, active-vs-archived degradation, in-flight tolerance) + D-14 dead-template entry-literal check + `just docs-drift` wired into the always-run code-quality job
- [x] 66-02-PLAN.md — CLEAN-01 discreet flag-gated `/saq` footer link on the Agents page (enable_saq_ui context + template + render test)

**Wave 2** *(blocked on Wave 1 — shares justfile + benefits from a green tree per the D-12 guardrail)*

- [x] 66-03-PLAN.md — CLEAN-02 vulture dead-code sweep: legitimacy checkpoint + `vulture>=2.16` dev dep + hand-audited whitelist + `just vulture` recipe + manual-verify confirmed-dead deletions

## Phase Details (2026.7.1 Multi-Cloud Backends)

### Phase 67: Backend Registry & Config Model
**Goal**: Operator can declare the full set of execution backends (and their S3 staging buckets) in `backends.toml` as the single source of truth, and `cloud_target` + the flat `s3_*`/`kube_*`/`compute_*` fields are **removed with no back-compat shim** (their ~10 call sites rewired to registry-derived reads). The only deploy that ever ran (`cloud_target=local`, all-local) keeps working **unchanged with zero config edits** via a zero-config implicit all-local registry — not a shim. Config-model-only: no dispatch/scheduler/protocol change this phase. *(Scope revised 2026-07-03 per operator decision D-11..D-14; see Phase 67 CONTEXT.md. The 67↔68 boundary and Phase 68's byte-identical characterization premise are revisited at Phase 68 plan-time.)*
**Depends on**: Nothing new (builds on the shipped v6.0 config surface; first phase of this milestone)
**Requirements**: REG-01, REG-02, REG-03, REG-04, REG-05
**Success Criteria** (what must be TRUE):
  1. Operator can declare a `backends:` list (each entry `id` / `kind` ∈ {local, compute, kueue} / integer `rank` / integer `cap`); the app boots with it as the resolved registry and logs the effective registry (`id`/`kind`/`rank`/`cap` only — never secret material) at startup.
  2. A misconfigured backend entry fails fast **at startup** with the offending entry `id` in the message (a kueue entry missing its kube config, or a compute entry missing its bound-agent reference), not silently at dispatch time.
  3. With no `backends.toml` present (and no config pointer), the registry resolves to an implicit single `kind=local` backend — the current live all-local deploy keeps running unchanged with zero config edits (never a silently-empty / wedged-backlog registry). `cloud_target` and the flat `s3_*`/`kube_*`/`compute_*` fields no longer exist, and there is no back-compat shim (no live deploy ever exercised the `a1`/`k8s` paths).
  4. Operator can declare an S3 staging-bucket registry (each bucket shared/public or cluster-specific, credentials via inline `*_file` paths) and assign each Kueue backend its bucket set; a `cluster-specific` bucket is referenceable by at most one Kueue backend, and a Kueue backend that resolves to an empty bucket set fails fast. The flat single global S3 config is removed with the other flat fields (no shim).
  5. Per-backend secrets (kube tokens/kubeconfigs, S3 credentials, agent tokens) resolve via the existing `<VAR>_FILE` convention scoped per entry.
**Plans**: TBD (decomposed at `/gsd:plan-phase 67`)
**PR**: own worktree branch — never a direct commit to `main`. Behavior-preserving (config model only).

### Phase 68: Backend Protocol + 3 Implementations
**Goal**: The hardcoded `if/elif cloud_target` switch is replaced by one internal `Backend` protocol with three implementations and one uniform per-backend in-flight count — provably without changing single-backend dispatch behavior. This is where the accounting substrate changes, so it must be proven byte-identical before multiplicity is switched on.
**Depends on**: Phase 67 (needs the `backends:` registry to bind implementations to)
**Requirements**: BACK-01, BACK-02, BACK-03, BACK-04
**Success Criteria** (what must be TRUE):
  1. Every former `cloud_target` `if/elif` call site dispatches through the `Backend` protocol (`is_available` / `in_flight_count` / `dispatch` / `reconcile`), with `LocalBackend` / `ComputeAgentBackend` / `KueueBackend` bodies re-homing the existing staging / push / submit logic rather than rewriting it.
  2. `cloud_job` carries a `backend_id` column (additive migration + backfill of existing rows to the current single backend) and compute-agent pushes are now recorded in `cloud_job`, so `in_flight_count(backend)` returns one uniform per-backend count across all kinds from a single authoritative substrate (no path counts both `FileState{PUSHING,PUSHED}` and a `cloud_job` row for the same file).
  3. A characterization test proves single-backend dispatch decisions are byte-identical pre/post refactor — including the compute-requires-a-live-agent (GATE 1) vs. Kueue-deliberately-skips-that-gate asymmetry (a Kueue `is_available()` never depends on a compute agent — Landmine L2 preserved).
  4. Accounting is internally consistent: `sum(in_flight_count(b) for b in backends)` equals the count of in-flight `FileState` rows, with the `dispatch()` `FileState` flip + `cloud_job` upsert committed in one transaction (no in-flight-without-registry-row limbo).
**Plans**: TBD (decomposed at `/gsd:plan-phase 68`)
**PR**: own worktree branch — never a direct commit to `main`. Behavior-preserving; the characterization test (criterion 3) is the phase's acceptance gate.

### Phase 69: Tiered Drain Scheduler
**Goal**: Long files drain across every eligible backend simultaneously, cheapest-rank-first with per-backend caps, spilling to the next rank when the preferred one is full or offline. The first behavior-changing phase — the moment more than one backend can run at once.
**Depends on**: Phase 68 (a per-backend `cap` is unenforceable without Phase 68's uniform per-backend `in_flight_count()`)
**Requirements**: SCHED-01, SCHED-02, SCHED-03, SCHED-04, SCHED-05
**Success Criteria** (what must be TRUE):
  1. Each `AWAITING_CLOUD` file is dispatched to the *available* backend with the lowest `rank` whose `in_flight_count() < cap`, with eligibility evaluated **per candidate file** so a full top-rank backend spills to the next rank rather than blocking the tick; local (rank 99, small cap) is reached only when every higher-ranked backend is full or offline.
  2. The former global `cloud_max_in_flight` window is now a per-backend `cap`, enforced by count-and-claim in one transaction under the existing `pg_advisory_xact_lock` — overlapping drain/reconcile ticks never overshoot a backend's cap (reconcile shares the lock discipline).
  3. A backend going offline or a job failing mid-flight returns the file to `AWAITING_CLOUD`; the next tick re-dispatches it to the next eligible backend chosen against *current* availability, and a black-hole / cooldown guard prevents a persistently-down backend from repeatedly reclaiming and re-failing its own files (bounded total attempts → `ANALYSIS_FAILED`, never an infinite A↔B thrash loop).
  4. Two or more equal-`rank` backends are tie-broken deterministically and statelessly (lowest current utilization `in_flight/cap`, then stable `id`) — no weighted or proportional fair-share.
  5. Exactly one recovery owner exists per backend kind: `reconcile_cloud_jobs` and the recovery ledger are `backend_id`-aware and the AST over-enqueue guard now covers compute-backed cloud files, so no cloud-owned file gains a second recovery path (no replay of the 44.5k-job over-enqueue incident class).
**Plans**: TBD (decomposed at `/gsd:plan-phase 69`)
**Research**: needed — the drain↔reconcile lock-ordering change and the global-dispatch-budget vs. per-backend-cooldown split are novel correctness mechanisms with no existing phaze precedent; settle exact lock scope + attempt counters at plan-time (`/gsd:plan-phase --research-phase 69`).
**PR**: own worktree branch — never a direct commit to `main`. First behavior-changing phase.

### Phase 70: Multi-Kueue (N Clusters)
**Goal**: The registry's multiplicity extends to N real Kueue clusters dispatched concurrently, each staging to its assigned bucket set, with one cluster's failure isolated from the rest — proving multiplicity on real infrastructure without introducing a new provider type.
**Depends on**: Phase 69 (the tiered scheduler) and Phase 68 (the `KueueBackend` protocol body)
**Requirements**: MKUE-01, MKUE-02, MKUE-03, MKUE-04
**Success Criteria** (what must be TRUE):
  1. Operator can declare N Kueue-cluster backends, each with its own kube config (per-cluster kubeconfig/context), and the one control plane dispatches to them concurrently.
  2. Each cluster stages long files to a bucket drawn from its REG-05-assigned set (shared/public or cluster-specific; deterministic per-file selection when a set holds several buckets); the control plane stays the **sole** S3 importer/presigner for every bucket (DIST-01 preserved) and pods/agents stay credential-free, receiving only presigned, `file_id`-scoped, TTL-bounded URLs (objects never world-readable despite an Internet-reachable endpoint).
  3. Each cluster has its own LocalQueue reachability probe and a `backend_id`-scoped reconcile, and one cluster's probe/dispatch failure is isolated (per-backend try/except; `is_available()` returns bool, never raises) so it cannot poison the whole drain tick — healthy clusters and local still receive work.
  4. Cross-cluster/cross-bucket staged-object cleanup is scoped to the (backend, bucket) that staged the object, so a spillover re-dispatch never deletes an object another cluster or bucket is still using; the per-bucket lifecycle TTL remains the backstop.
**Plans**: TBD (decomposed at `/gsd:plan-phase 70`)
**Research**: needed — unresolved plan-time schema/resolution questions: (a) `cloud_job` one-row-per-file (mutate `backend_id` in place) vs. one-row-per-(file,backend) for attempt-scoping; (b) `ComputeAgentBackend.is_available()`/dispatch resolving its specific agent via `agent_ref`→`Agent.id` (replacing the "most-recently-seen" heuristic); plus a live-cluster verify of kr8s auth per distinct kubeconfig/context and cross-cluster stale-Job cleanup ordering (`/gsd:plan-phase --research-phase 70`).
**PR**: own worktree branch — never a direct commit to `main`.

### Phase 71: Deployment, Config, Docs & N-Lane UI
**Goal**: Operators can see all N backend lanes, revert everything to local for incident response, and follow a runbook for the `backends:` schema and the `cloud_target`→`backends` migration. Presentation/ops close-out over the now-proven scheduler and multi-Kueue — the per-lane data is already computed by phases 69–70.
**Depends on**: Phase 70 (and Phase 69 for the per-backend `{available, in_flight, cap, rank}` state the UI reads)
**Requirements**: BEUI-01, BEUI-02, BEUI-03
**Success Criteria** (what must be TRUE):
  1. The admin UI renders N per-backend lanes derived from the registry — each showing available/offline, in-flight/cap, and rank (the Kueue quota-wait-vs-Inadmissible distinction preserved and attributed per lane by `id`) — read-only and riding the existing `/pipeline/stats` 5s poll (no second poll loop), generalizing v7.0 Phase 58's fixed 3 cards to N dynamic lanes.
  2. A master toggle reverts all routing to local for incident response (the `backends`-era equivalent of today's `cloud_target=local` no-op gate).
  3. The operator runbook and configuration docs cover the `backends:` schema, per-backend `_FILE` secrets, and the `cloud_target`→`backends` migration and deprecation path.
**Plans**: TBD (decomposed at `/gsd:plan-phase 71`)
**UI hint**: yes
**PR**: own worktree branch — never a direct commit to `main`. Presentation/ops only.

## Backlog (unscheduled — no phase number yet)

- **Distributed cloud analysis (burst the backlog).** _[SCHEDULED as v5.0 Cloud Burst Analysis, Phases 47-51 — narrowed to rsync-over-Tailscale to a free arm64 OCI A1 (essentia built from source), no object storage. See Phase Details (v5.0).]_ Offload long-file analysis to cloud x86 workers via the existing agent model: stage file to object storage → cloud worker pulls (presigned GET) → analyzes → PUTs result; **reconcile by `file_id`** (already end-to-end), sha256 for download integrity. Only new pieces: optional `source_url`+`sha256` on `ProcessFilePayload` + a "stager". essentia is **x86-only** (no aarch64 wheel; source build infeasible). Best near-free path = **GCP $300/90-day trial, x86 e2 spot, GCS same-region** (≈$0 out of pocket); min-cost paid = OCI E5 preemptible (~$100, free egress). **Gate: only pursue if nox throughput is still insufficient after the Phase 43 redeploy + re-measure** — bounding may make this moot. Full design: memory `reference-essentia-arm64-cloud-burst` + `project-analyze-4h-timeout-incident`.
- **Partition the test suite for parallel CI.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 63 (CI-01/02/03). See Phase Details (2026.7.0).]_ Split the ~1750-test pytest suite into independently-runnable buckets so CI fans them out across parallel jobs instead of one serial run. Partition by **pipeline workflow-step** (discovery, metadata, fingerprint, analyze, identify/tracklist, review/apply, agents/distributed) plus a **generic/shared** bucket (schema, config, helpers, routing). Open questions to resolve at planning: marker-based selection (`@pytest.mark.<step>`) vs directory layout vs `pytest-xdist` sharding vs a CI job matrix; how to keep coverage aggregation correct across shards (combine `.coverage` files → single Codecov upload) and preserve the 85% gate; real-Postgres integration tests likely need their own bucket. Goal: cut wall-clock CI time without losing the single coverage report.
- **Adopt CalVer ([calver.org](https://calver.org/)) for release versioning.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 65 (VER-01..04). See Phase Details (2026.7.0).]_ Replace the current milestone-aligned `vN.M` scheme (now at v7.0) with a calendar-based version. Decide the exact scheme at planning (e.g. `YYYY.MM.MICRO` or `YY.MM.MICRO`) and how it coexists with the milestone narrative (milestones become named, versions become dated). Update: the release procedure (pyproject `version` + `uv.lock` bump → annotated tag PUSH → GHCR publish — see memory `project-release-procedure`), README/version badges (one-line badge style), the milestone↔version mapping in ROADMAP/MILESTONES, and any image tags / compose references. Note the prior cadence shipped many `v4.0.x` patch releases — pick a MICRO convention that supports same-month patches.
- **CI builds only when code changes.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 63 (CI-04). See Phase Details (2026.7.0).]_ Stop running the full build/test/security CI on docs- and planning-only changes (e.g. `.planning/**`, `*.md`) so commits like these backlog/requirements edits don't trigger the whole pipeline. Decide the mechanism at planning: workflow `paths`/`paths-ignore` filters vs a changed-files detection job that gates downstream jobs (the latter avoids the "required check never runs → PR can't merge" branch-protection trap that bare `paths-ignore` causes). Must keep the required status checks satisfiable on doc-only PRs (skip-with-success, not skip-absent). Pairs with the "partition test suite for parallel CI" item.
- **Re-add an in-UI link to the `/saq` SAQ monitor.** _[SCHEDULED as 2026.7.0 Engineering Improvements, Phase 66 (CLEAN-01). See Phase Details (2026.7.0).]_ _[Surfaced by the v7.0 milestone audit (`v7.0-MILESTONE-AUDIT.md`) — target the next cleanup / "engineering basics" milestone.]_ The SAQ task-queue dashboard is still mounted at `/saq` (`main.py`) and reachable by direct URL, but the v7.0 cutover (Phase 62/CUT-02) deleted the only in-UI link when it removed `dashboard.html`. Nothing is broken — the monitor works, it's just unlinked. Add a discreet link back into the shell; the natural home is the Agents / Compute page (RECORD-03 already surfaces agent state) rather than the DAG rail. Presentation-only; no backend change.
- **Harden the docs-drift guard for the between-milestones state.** _[Surfaced at the 2026.7.0 milestone close, 2026-07-03.]_ The Phase-66 traceability guard (`tests/shared/core/test_requirements_traceability.py`) reads `.planning/REQUIREMENTS.md` with no existence check in its 4 active-milestone tests, so the standard milestone-close `git rm REQUIREMENTS.md` would raise `FileNotFoundError` and fail the required code-quality check. For the 2026.7.0 close we kept REQUIREMENTS.md in place (guard verified green, all 13 reqs `[x]`→passed phases) instead of deleting it. Follow-up: make the active-milestone tests `pytest.skip` (or fail-clean) when REQUIREMENTS.md is absent, add a regression test for the archived/no-active-milestone state, then the close can `git rm` the file again. Small, self-contained; a natural quick task or a Phase-66-style guard-robustness follow-up.
