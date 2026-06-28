# Phaze

## What This Is

A music collection organizer that ingests ~200K music files (mp3, m4a, ogg, opus) and concert video streams, analyzes them for BPM/mood/style/key, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. As of v4.0, phaze runs as a **two-host distributed system**: an application server (API, UI, Postgres, Redis, fileless workers, no file mounts) and one or more file-server agents that own the music/video files locally, pull jobs from per-agent SAQ queues, and write every state change back over authenticated HTTPS. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

## Core Value

Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review. Files stay where they live; decisions stay on one server.

## Current Milestone: v6.0 Kubernetes Burst Analysis

**Goal:** Offload long-duration audio analysis to a remote **x64 Kubernetes cluster running Kueue** as a third routing target alongside local and the v5.0 OCI A1 — following the v5.0 cloud-burst pattern (duration routing, compute-agent result callback, master toggle), but with the execution unit changed from "persistent host draining a SAQ queue" to "ephemeral, quota-scheduled Kueue batch Job submitted per file."

**Target features:**
- x86 Kueue Job-runner image published to GHCR — reuses the existing x86 essentia stack (the cluster is x64, so no arm64 source build); a one-shot entrypoint: pull file from object storage → analyze → POST result → exit
- Kube-API submission seam: the control plane submits a *suspended* batch `Job` labeled `kueue.x-k8s.io/queue-name`, then watches the `Workload` for admission→completion and reconciles results by `file_id`; Kueue owns quota/admission
- Object-storage staging: the long file is uploaded to an **operator-provided S3-compatible bucket** (reuse existing), the Job downloads it, and the object is cleaned up after analysis (ephemeral). Secrets via the `_FILE` convention. **(Reverses v5.0's "no object storage" decision — see Out of Scope.)**
- Router extension: "K8s" becomes a third cloud target; a single config setting selects the **active cloud target** (local / A1 / K8s). Same long-set routing seam as v5.0 (≥ duration threshold) — long files only, conservative scope
- Result callback reuses v5.0's compute-agent machinery: the Job pod authenticates back to `/api/internal/agent/*` as a registered compute agent
- Transport-agnostic connectivity: Tailscale *or* WireGuard — phaze only consumes operator-provided reachable endpoints (kube API, S3, callback), no mesh-specific code
- Deployment + runbook + config/docs: Kueue admin objects (ResourceFlavor / ClusterQueue / LocalQueue) documented as cluster-admin setup; phaze references a configured LocalQueue name; kubeconfig/service-account token via `_FILE` secret; all behind the existing `cloud_burst_enabled` master toggle with per-target config

**Key context:** Mirrors v5.0's duration-routing + compute-agent + result-reconciliation design, but the execution unit changes from a persistent SAQ-draining host to an ephemeral Kueue Job. x64 hardware removes the arm64-image burden — the existing x86 essentia stack is reused. Two new external dependencies vs. v5.0: object storage (S3-compatible client) and a Kubernetes API client. Connectivity is intentionally transport-agnostic (Tailscale or WireGuard), unlike v5.0's Tailscale-specific pipeline.

## Current State

**v5.0 Cloud Burst Analysis shipped 2026-06-26.** Phaze can now offload long-duration audio (≥ a configurable threshold) that times out locally to a free OCI Ampere A1 (arm64) "compute agent" reached over Tailscale — duration-routed, rsync-pushed, sha256-verified, and analyzed unattended, all behind a single `cloud_burst_enabled` master toggle that defaults to all-local. 5 phases (47-51), 23 plans; all requirements (CLOUDIMG/CLOUDAGENT/CLOUDROUTE/CLOUDPIPE/CLOUDDEPLOY) validated. Live end-to-end verification is deployment-gated on the homelab OCI A1 rollout (see milestones/v5.0-MILESTONE-AUDIT.md + STATE.md Deferred Items).

**v4.0 Distributed Agents shipped 2026-05-17.** Phaze runs across two hosts: a control-plane application server and one or more file-server agents.

**Phase 53 (v6.0, complete 2026-06-28):** S3 object-staging leg — the control plane presigns S3 multipart PUT/GET URLs and orchestrates cleanup via a pure-aioboto3 `s3_staging` service (the **only** file importing the S3 SDK — DIST-01 boundary CI-enforced), while the file-server agent uploads bytes over httpx to presigned PUT URLs with no SDK or bucket credentials, and the pod fetches a just-in-time presigned GET at startup. A `cloud_job` per-`file_id` sidecar table (migration 025) tracks staging status/`upload_id`; staged objects use `file_id`-scoped keys, are deleted inline on every terminal outcome (success/failure/re-drive) with a bucket lifecycle TTL backstop, and S3 config is operator-provided via `_FILE` secrets against any S3-compatible backend. Validated requirements: KSTAGE-01..05. Code review surfaced and fixed 5 real robustness bugs (idempotent abort/complete, re-drive URL-clobber, empty-ETag, presign readiness guard); CR-01 was a verified false-positive. The producer is built but not yet wired into the live routing seam (Phase 55 owns that).

**Phase 51 (v5.0, complete 2026-06-26):** Deployment, config & docs — `docker-compose.cloud-agent.yml` (arm64, host-Tailscale, no media, named scratch volume), the `cloud_burst_enabled` master toggle gating all three cloud entry points (routing seam, staging cron, backfill), the homelab OCI A1 + Tailscale-ACL + least-privilege Postgres broker provisioning runbook, and the full config/docs surface. Validated requirements: CLOUDDEPLOY-01..04. Post-audit fixes (#161/#162): cloud-agent compose `python3 -m saq` start command, `compute_scratch_dir` fail-fast guard, scratch-dir-skew diagnostic, and the WR-03 push-timeout-coupling guard.

**Phase 30 (post-v4.0 fix, complete 2026-06-10):** Resolved systemic control-plane SAQ queue misrouting — every manually-triggered UI/API enqueue previously targeted a consumer-less unnamed `default` queue (stranded 11,428 jobs in the v4.0.6 incident). All enqueue sites (pipeline, tracklists, scan/ingestion) now route through a shared `enqueue_router.resolve_queue_for_task` helper: controller-bound tasks → named `controller` queue, per-agent tasks → `phaze-agent-<id>` via active-agent selection (0-agent surfaces a 503/empty-state). A static AST guard test prevents recurrence.

**Phase 47 (v5.0, complete 2026-06-24):** Official arm64 essentia agent image — `Dockerfile.agent-arm64` builds essentia from source (the essentia-tensorflow wheel is x86-only) on `python:3.13-slim-bookworm` against TF 2.20.0 with all four spike fixes baked in; CI builds it on a native `ubuntu-24.04-arm` runner and publishes `-arm64`-tagged images to GHCR *only* after a numeric-parity guard compares arm64 `analyze_file` output against an x86 golden. Validated requirements: CLOUDIMG-01/02/03. Unlocks the OCI Ampere A1 free-tier compute agent for Phases 48-51.

**Phase 50 (v5.0, complete 2026-06-26):** Cloud push pipeline — a file-server agent rsyncs a cloud-routed long file to the compute agent's scratch dir over SSH-over-Tailscale (`push_file` task, shell-free argv with `--` terminator + pinned host keys + 0600 secret temp files); the compute agent sha256-verifies the scratch copy against `FileRecord` before analyzing and unlinks it in a `finally` (kept on retryable failure so the SAQ retry can re-verify). New `PUSHING`/`PUSHED` states; a `stage_cloud_window` cron keeps ≤N files staged-or-in-flight ("stay one ahead", default 2) under a pg advisory lock; control-side `pushed`/`mismatch` internal-API callbacks drive the handoff (idempotent, ledger-tracked) with two D-09 dashboard count cards. Validated requirements: CLOUDPIPE-01..05. Live rsync-over-Tailscale transfer to a real compute agent is Phase 51 (deploy/provisioning).

- ~14,300 lines of Python source + ~28,000 lines of tests across 29 phases, 94+ plans total (v1.0–v4.0)
- Tech stack: FastAPI, SQLAlchemy (async), SAQ + Redis (per-agent queues), litellm, essentia-tensorflow, mutagen, rapidfuzz, httpx, watchdog, cryptography (self-signed CA), tenacity, respx, HTMX + Tailwind + Alpine.js
- Two Docker Compose stacks: `docker-compose.yml` (app-server: api with TLS via internal CA, controller worker, postgres, redis with `requirepass` + LAN bind, no file mounts) and `docker-compose.agent.yml` (file-server: agent worker, watcher, audfprint + panako sidecars)
- 14 Alembic migrations, 14 SQLAlchemy models (Agents added in v4.0), per-file-server fingerprint sidecars
- Internal API surface: `/api/internal/agent/*` with token-hash bearer auth, idempotent natural-key upserts, 403-before-state-machine cross-tenant guards, 30s heartbeat
- Admin UI: proposals, duplicates (with cross-FS fingerprint notice), tracklists, pipeline dashboard with **Trigger Scan card**, unified search, Discogs linking, tag review, CUE management, **Agents** page with liveness + queue depth
- Operator workflow: `just up` (app-server), `just up-agent` (each file-server), `just up-all` (single-host dev); full deployment walkthrough in `docs/deployment.md`

## Previous State

<details>
<summary>v3.0 shipped 2026-04-04</summary>

Single-host enrichment milestone: unified FTS search with faceted filtering, Discogs cross-service linking with fuzzy matching and bulk-link, format-aware tag writing with 4-layer cascade (tracklist > discogs > metadata > filename) and verify-after-write, CUE sheet generation with fingerprint-preferred timestamps and Discogs REM enrichment.

- 6 phases, 11 plans
- 13 Alembic migrations, 13 SQLAlchemy models
- TagWriteLog audit, DiscogsLink with confidence scoring, three-entity UNION ALL search (file/tracklist/discogs)

</details>

<details>
<summary>v2.0 shipped 2026-04-02</summary>

Metadata enrichment & tracklist integration. Audio tag extraction (mutagen), AI destination paths with collision detection, duplicate resolution UI, 1001Tracklists integration with monthly cron, dual fingerprint service (audfprint + Panako) with batch ingestion.

- 6 phases, 16 plans, 538 tests passing
- ~5,966 lines of Python added

</details>

<details>
<summary>v1.0 shipped 2026-03-30</summary>

Full pipeline operational: scan → analyze → propose → approve → execute.

- 11 phases, 24 plans, 282 tests passing
- ~7,975 lines of Python
- Tech stack: FastAPI, SQLAlchemy (async), arq, litellm, essentia-tensorflow, HTMX + Tailwind
- 4 Alembic migrations, 6 SQLAlchemy models

</details>

## Requirements

### Validated

- ✓ Containerized backend services running via Docker Compose — v1.0 Phase 1
- ✓ PostgreSQL database for all metadata and state — v1.0 Phase 1
- ✓ Alembic database migrations — v1.0 Phase 1, 10
- ✓ Recursive directory scanning for music/video/companion files — v1.0 Phase 2
- ✓ SHA256 hash computation and storage — v1.0 Phase 2
- ✓ Original filename and path recorded in PostgreSQL — v1.0 Phase 2
- ✓ File type classification (music, video, companion) — v1.0 Phase 2
- ✓ Companion files linked to media files via directory proximity — v1.0 Phase 3
- ✓ Exact duplicate detection via SHA256 hash grouping — v1.0 Phase 3
- ✓ arq + Redis task queue with bounded worker pool, retry with backoff, process pool — v1.0 Phase 4 (replaced by SAQ in v4.0)
- ✓ BPM detection for music files — v1.0 Phase 5
- ✓ Mood and style classification for music files — v1.0 Phase 5
- ✓ Analysis runs in parallel across worker pool — v1.0 Phase 4
- ✓ AI-powered filename proposals via litellm with batch prompting and structured output — v1.0 Phase 6
- ✓ Proposals stored as immutable records in PostgreSQL — v1.0 Phase 6
- ✓ Admin web UI with paginated proposal list, status filtering, bulk actions, keyboard shortcuts — v1.0 Phase 7
- ✓ Admin can approve/reject individual proposals with FileRecord state transition — v1.0 Phase 7, 11
- ✓ Safe file execution via copy-verify-delete protocol with proposed_path routing — v1.0 Phase 8, 11
- ✓ Append-only audit log for all file operations — v1.0 Phase 8
- ✓ Pipeline orchestration: scan→analyze→propose triggers via API endpoints — v1.0 Phase 9

- ✓ Audio tag extraction (ID3/Vorbis/MP4/FLAC/OPUS) feeding richer LLM context — v2.0 Phase 12
- ✓ Shared async engine pool replacing per-invocation engine creation — v2.0 Phase 12
- ✓ AI destination path proposals with collision detection and directory tree preview — v2.0 Phase 13
- ✓ Duplicate resolution UI with auto-scoring, side-by-side comparison, resolve/undo — v2.0 Phase 14
- ✓ 1001Tracklists integration with search, scrape, fuzzy match, periodic refresh — v2.0 Phase 15
- ✓ Dual fingerprint service (audfprint + Panako) with batch ingestion — v2.0 Phase 16
- ✓ Live set scanning with tracklist review, inline editing, approve/reject — v2.0 Phase 17

- ✓ Unified search across files, tracklists, and metadata with faceted filtering — v3.0 Phase 18
- ✓ Discogsography cross-service linking via HTTP API with fuzzy matching and confidence scores — v3.0 Phase 19
- ✓ Write corrected tags to destination copies with review UI, verify-after-write, and audit logging — v3.0 Phase 20
- ✓ CUE sheet generation from tracklist data with fingerprint-preferred timestamps and Discogs REM enrichment — v3.0 Phase 21

- ✓ File servers run agents that own files locally; the application server orchestrates and stores all state — v4.0 Phase 24-29
- ✓ HTTP-only boundary between agents and the application server (no shared filesystem, no shared database access) — v4.0 Phase 25-26
- ✓ Per-agent bearer token auth with `agent_id` derived from token, never from request body — v4.0 Phase 25
- ✓ Continuous file watcher service on each file server that streams new arrivals to the application server — v4.0 Phase 27
- ✓ Distributed approval execution: group approved proposals by agent and dispatch one sub-batch per file server — v4.0 Phase 28
- ✓ Self-signed HTTPS via internal CA + Redis `requirepass` + LAN bind + per-file-server fingerprint sidecars — v4.0 Phase 29
- ✓ Same Docker image for both roles via `PHAZE_ROLE={control,agent}` env; new `docker-compose.agent.yml` for file servers — v4.0 Phase 26, 29
- ✓ 30s heartbeat + Agents admin page with liveness, queue depth, last-seen — v4.0 Phase 29

- ✓ Official arm64 essentia agent image published to GHCR via native arm64 CI build with numeric-parity guard — v5.0 Phase 47
- ✓ Compute-agent type (no scan roots / no media) with duration-based, capability-aware analysis routing — v5.0 Phase 48-49
- ✓ Ledger-scoped backfill of timed-out long files to the cloud agent (no whole-backlog over-enqueue) — v5.0 Phase 49
- ✓ rsync-over-Tailscale "stay one ahead" push pipeline (control-plane orchestrated, ephemeral scratch + sha256 verify) — v5.0 Phase 50
- ✓ Cloud-agent deployment + OCI A1 / Tailscale-ACL runbook; `cloud_burst_enabled` master toggle; `_FILE`-secret-capable config — v5.0 Phase 51

### Active

_v6.0 Kubernetes Burst Analysis (detailed REQ-IDs in `REQUIREMENTS.md`):_
- x86 Kueue Job-runner image published to GHCR (one-shot: pull from object storage → analyze → POST result → exit)
- Kube-API submission seam: control plane submits a suspended Kueue `Job`, watches the `Workload`, reconciles by `file_id`
- Object-storage staging to an operator-provided S3-compatible bucket (ephemeral upload + cleanup; `_FILE` secrets)
- K8s as a third routing target; single config setting selects the active cloud target (local / A1 / K8s)
- Job-pod result callback reuses the v5.0 compute-agent `/api/internal/agent/*` surface
- Transport-agnostic connectivity (Tailscale or WireGuard) — operator-provided reachable endpoints only
- Cluster/Kueue deployment + runbook; all parameters configurable via pydantic-settings under the `cloud_burst_enabled` toggle

### Out of Scope

- Cross-file-server fingerprint matching — per-agent fingerprint DB only in v4.0; documented as v4.0 limitation, tracked as XAGENT-01, deferred to a later milestone
- Cross-file-server execution batches (moves spanning hosts) — XAGENT-02, deferred
- Delete / move / rename detection in the file watcher — v4.0 watcher only handles `created` events; tracked as WATCH-05/06, deferred
- Watcher catch-up on startup (rescan files that landed while watcher was down) — WATCH-07; manual user-initiated scan covers this in v4.0
- mTLS in addition to bearer tokens for the agent boundary — OPS-05, deferred
- Multi-tenant agent self-service registration — OPS-06; today operator pre-seeds tokens
- Agent metric scraping endpoint (Prometheus-compatible) — OPS-07, deferred
- Natural language querying across services — deferred
- Acoustic near-duplicate detection via fingerprint similarity — deferred
- Public network access — private LAN only
- Offline mode — real-time server tool, not a desktop app
- Files transferred between application server and file server — v4.0 keeps files local to file servers; transfer would defeat the boundary. **(Narrowed in v5.0: still no app↔file-server transfer, but a file-server agent may push a long file to an ephemeral *cloud compute agent* for analysis-only, then delete it — extra compute, not a data home. v6.0 keeps this: the long file is staged to ephemeral object storage for the Kueue Job, downloaded, analyzed, deleted — analysis-only, not a data home.)**
- Postgres replication / read-replica on file server — agents stay HTTP-only (Option II in v4.0 grilling was rejected)
- ~~Tailscale / mesh networking — plain private LAN chosen in v4.0 (Q10b)~~ **(Reversed in v5.0: Tailscale is the transport for the off-LAN cloud compute agent. Generalized in v6.0: connectivity is transport-agnostic — Tailscale or WireGuard — phaze consumes operator-provided reachable endpoints only.)**
- ~~No object storage — v5.0's cloud agent analyzed from local rsync'd scratch, never a bucket~~ **(Reversed in v6.0: ephemeral Kueue Job pods have no persistent local disk, so the long file is staged to an operator-provided S3-compatible bucket, downloaded by the Job, and deleted after analysis. Ephemeral staging only — not a data home.)**
- GPU / Coral TPU acceleration for the cluster nodes — essentia-tensorflow analysis is CPU-bound on this workload; cluster nodes and Kueue resource requests target CPU, not accelerators (see Key Decisions)

## Context

- v1.0–v4.0 shipped: full pipeline from scan → tag extract → analyze → propose (filename + path) → approve → execute, now distributed across application server + file-server agents
- ~200K files total, mix of music files and full concert video streams
- Concert videos are primarily recordings of live streams (YouTube streams from festivals, etc.)
- FileMetadata fully populated via mutagen tag extraction (ID3/Vorbis/MP4/FLAC/OPUS)
- Dual fingerprint service (audfprint + Panako) per file server with weighted scoring (60/40, 70% single-engine cap); no cross-file-server matching in v4.0
- 1001tracklists integration operational with monthly refresh cron (runs on app-server controller worker)
- This is a personal tool running on a private home LAN, not a multi-user SaaS

## Constraints

- **Language**: Python 3.13 exclusively
- **Package manager**: uv only
- **Deployment**: Docker Compose on private LAN; two-host topology (app-server + file-server agents)
- **Database**: PostgreSQL (app-server only; agents have zero direct DB access)
- **Scale**: Must handle ~200K files efficiently — batch processing and parallelization required
- **Naming format**: Live sets: `{Artist} - Live @ {Venue|Event} {YYYY.MM.DD}.{ext}`, Album tracks: `{Artist} - {Track #} - {Track Title}.{ext}`

**Per-agent fingerprint indices (v4.0).** Each file server's `audfprint` and `panako` sidecars index ONLY that file server's local files. Duplicate audio content landing on different file servers will NOT cross-match. Cross-file-server fingerprint matching is XAGENT-01 (deferred to a post-v4.0 milestone). The Duplicate Resolution admin UI surfaces this constraint as an inline, per-session-dismissible banner on every page load so the operator interprets fingerprint-derived results with this scope in mind.

### Deployment (v4.0 — Distributed Agents)

Phaze v4.0 production runs as **two Docker Compose files on two private-LAN hosts**:

- **Application server** (`docker-compose.yml`): `api` (uvicorn-direct TLS via internal CA), `worker` (fileless controller-role SAQ worker), `postgres`, `redis` (password-auth + LAN-bound port). **No file mounts** beyond `./certs/` — the app-server has no way to read or write music/video file content (DIST-01).
- **File servers** (`docker-compose.agent.yml`, one stack per file-server host): `worker` (agent-role SAQ worker), `watcher` (watchdog-based file event poster), `audfprint` + `panako` (local fingerprint sidecars). Holds the music/video library locally; reaches the app-server over HTTPS for every state change.

Locked invariants (Phase 29):

- All agent → app-server traffic uses **HTTPS** terminated by uvicorn against a self-signed internal CA generated in the app-server's `api` container on first start. Operators distribute the public CA cert (`phaze-ca.crt`) to each file server via scp/rsync; the CA private key (`phaze-ca.key`, mode 0600) never leaves the app-server.
- **Redis** on the app-server requires `requirepass` and is bound to the private LAN IP (or loopback in dev). Agents connect with `redis://default:<password>@<host>:6379`. In `PHAZE_AGENT_ENV=production`, `AgentSettings` rejects a passwordless `redis_url` at boot.
- **0 new pip dependencies** beyond `cryptography` (added Phase 29 for cert generation).
- `docker-compose.agent.yml` enforces `${SCAN_PATH:?SCAN_PATH required}` on all four services — compose parse fails fast on a misconfigured file-server host.
- Operator workflow: `just up` (app-server), `just up-agent` (each file-server), `just up-all` (single-host dev). Full walkthrough in `docs/deployment.md`.

Deferred to a future ops phase: mTLS for the agent boundary, agent self-registration UI, Prometheus metrics scrape endpoint, automated CA rotation. See `.planning/milestones/v4.0-REQUIREMENTS.md` §"Future Requirements → Operational Polish" (OPS-05..OPS-07).

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| PostgreSQL over SQLite | 200K files with complex metadata, relationships, and future cross-service queries need a real RDBMS | ✓ Good — handles async access, complex queries, JSON columns well |
| Organization before search | Getting files organized is the primary win; search/NLQ is a follow-on | ✓ Good — v1.0 delivers complete organization pipeline |
| Human-in-the-loop approval | No file moves without admin review — safety for a large, irreplaceable collection | ✓ Good — approval UI with undo prevents mistakes |
| Containerized services | Clean separation of concerns, reproducible deployment on home server | ✓ Good — Docker Compose with health checks works reliably |
| HTMX over React SPA | Single-user admin tool doesn't need SPA complexity | ✓ Good — zero build step, CDN delivery, full interactivity |
| arq over Celery | Async-first, simple config, Redis-native — single user doesn't need Celery complexity | — Replaced — migrated to SAQ in v4.0 prep; arq was in maintenance mode and SAQ has active development + per-agent queue affordances |
| SAQ over arq (v4.0) | Active maintenance, built-in web UI, native per-queue worker model | ✓ Good — clean fit for per-agent `phaze-agent-<id>` queues |
| essentia-tensorflow for analysis | 34 pre-trained models, BPM/key/mood/style in one library | ✓ Good — baked into Docker image, process pool execution |
| litellm for LLM abstraction | Provider flexibility without vendor lock-in | ⚠️ Revisit — supply chain incident on 1.82.7/1.82.8, pin aggressively |
| copy-verify-delete protocol | Never direct move — SHA256 verification before deleting original | ✓ Good — safety for irreplaceable collection, preserved across the v4.0 HTTP boundary via per-operation PATCH |
| State machine on FileRecord | Explicit state transitions (DISCOVERED→ANALYZED→PROPOSED→APPROVED→EXECUTED→MOVED/UNCHANGED/FAILED) | ✓ Good — enables pipeline dashboard stage counts |
| mutagen for tag read/write | Zero-dependency, supports all major tag formats | ✓ Good — reliable across ID3/Vorbis/MP4/FLAC/OPUS |
| audfprint + Panako hybrid | Complement each other: landmark-based vs tempo-robust | ✓ Good — weighted orchestrator with per-engine results |
| rapidfuzz for fuzzy matching | Fast token_set_ratio for tracklist-to-file matching | ✓ Good — weighted scoring with artist/event/date |
| Long-running fingerprint containers | HTTP API over subprocess calls for fingerprint services | ✓ Good — persistent DBs, Docker Compose integration; now per-file-server in v4.0 |
| Distributed agents (v4.0) | Files stay on file servers; application server owns API, UI, Postgres, Redis | ✓ Good — v4.0 shipped end-to-end; two-host topology operational with strict HTTP-only boundary |
| HTTP-only agent boundary (v4.0) | Agents have zero Postgres access; all writes go through `/api/internal/agent/*` | ✓ Good — `test_agent_worker_does_not_import_phaze_database` subprocess gate enforces the boundary at CI time |
| One SAQ queue per agent (v4.0) | `phaze-agent-<id>` queue per file server; enqueuer picks queue by `FileRecord.agent_id` | ✓ Good — matches SAQ's native pull model, clean per-agent maintenance |
| Per-agent bearer token auth (v4.0) | `agent_id` derived from token lookup on application server, never from request body | ✓ Good — partial-index `ix_agents_token_hash_active WHERE revoked_at IS NULL` gives O(1) lookup; revoke = instant block |
| Per-agent fingerprint DB (v4.0) | Each file server runs its own audfprint+panako sidecars indexing only its files | ⚠️ Revisit — known v4.0 limitation; XAGENT-01 deferred. Operator banner mitigates UX surprise |
| Self-signed internal CA (v4.0) | Generated in api container on first start; public cert distributed by operator via scp | ✓ Good — no DNS dependency, no public ACME, no rotation pain for single-user LAN |
| Redis `requirepass` + LAN bind (v4.0) | App-server Redis is broker + cache; password + interface bind is the minimal credible hardening on a private LAN | ✓ Good — `AgentSettings` fail-fast in production prevents passwordless misconfig |
| Group-by-agent execution dispatch (v4.0) | In-Python `defaultdict(list)` over SQL `GROUP BY` — at 1-5 agents × ≤10K proposals, type-safe path is cheaper than DB aggregation | ✓ Good — preserves write-ahead `ExecutionLog` audit over HTTP boundary via per-operation PATCH |
| Pre-uvicorn entrypoint shim (v4.0) | Cert bootstrap then `execvp uvicorn` so signals + PID-1 propagate cleanly | ✓ Good — clean Docker stop semantics, no double-process tree |
| Two-step Alembic migration (v4.0) | 012 adds + backfills, 013 enforces NOT NULL + swaps UQ — preserves v3.0 data via `legacy-application-server` seed | ✓ Good — round-trip downgrade smoke gate caught the boundary; zero data loss in production migration |
| CPU-only cluster nodes for v6.0 (no GPU / no Coral) | essentia analysis is CPU-bound: wall-clock is dominated by `MonoLoader` decode + native DSP (rhythm/onset/spectral) on long sets; the TF classifier step is a tiny slice. Coral needs int8 TFLite (essentia ships full float TF) and GPU only speeds the negligible inference. Throughput lever is horizontal CPU parallelism across files — Kueue quota delivers exactly that. | ▶ Planned — Kueue resource requests target `cpu`/`memory` only; generic x64 CPU node pool; consistent with v1.0 ProcessPoolExecutor decision |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition:**
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone:**
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-26 — after v5.0 Cloud Burst Analysis milestone*
