# Roadmap: Phaze

## Milestones

- ✅ **v1.0 MVP** — Phases 1-11 (shipped 2026-03-30)
- ✅ **v2.0 Metadata Enrichment & Tracklist Integration** — Phases 12-17 (shipped 2026-04-02)
- ✅ **v3.0 Cross-Service Intelligence & File Enrichment** — Phases 18-23 (shipped 2026-04-04)
- 🚧 **v4.0 Distributed Agents** — Phases 24-29 (in planning, 2026-05-11)

## Phases

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

### v4.0 Distributed Agents (Phases 24-29) — IN PLANNING

- [x] **Phase 24: Schema Foundation & Agent Registry** — `agents` table, `agent_id` columns on FileRecord/ScanBatch, two-step Alembic migration with legacy backfill (completed 2026-05-11)
- [x] **Phase 25: Internal Agent HTTP API & Bearer Auth** — `/api/internal/agent/*` endpoints, token-hash auth middleware deriving `agent_id` from token, idempotent upserts on natural keys, rotatable tokens (completed 2026-05-12)
- [x] **Phase 26: Task Code Reorg & HTTP-Backed Agent Worker** — split `phaze.tasks.controller` (fileless) from `phaze.tasks.agent_worker` (file-bound), `PHAZE_ROLE` env-driven startup, per-agent SAQ queue (`phaze-agent-<id>`), self-contained job payloads (completed 2026-05-12)
- [x] **Phase 27: Watcher Service & User-Initiated Scan** — new `phaze-agent-watcher` compose service, watchdog with mtime settle/debounce, sentinel `LIVE` ScanBatch per agent, admin-triggered scan form (completed 2026-05-13)
- [x] **Phase 28: Distributed Execution Dispatch** — group-by-agent approval dispatch, per-operation ExecutionLog PATCH, unified SSE progress aggregating across agents, per-agent fingerprint sidecars in execution path (completed 2026-05-15)
- [x] **Phase 29: Deployment Hardening & Agents Admin** — strip `SCAN_PATH`/`MODELS_PATH` from application-server compose, self-signed HTTPS w/ internal CA, Redis `requirepass` + LAN binding, `docker-compose.agent.yml`, per-file-server model download, heartbeat + Agents admin page (completed 2026-05-17)

## Phase Details

### Phase 24: Schema Foundation & Agent Registry
**Goal**: The database can model who owns each file and which agent originated each scan, with existing v3.0 data preserved end-to-end through a controlled migration.
**Depends on**: Phase 23 (v3.0 shipped)
**Requirements**: DATA-01, DATA-02, DATA-03, DATA-04
**Success Criteria** (what must be TRUE):
  1. An `agents` table exists with `id`, `name`, `token_hash`, `scan_roots` (jsonb), `created_at`, `last_seen_at`, `revoked_at`, and an operator can insert/query agent rows via Postgres
  2. `FileRecord.agent_id` and `ScanBatch.agent_id` are non-null string columns, and the file uniqueness invariant has moved from `(original_path)` to `(agent_id, original_path)` (verified by attempting a same-path insert under a different agent and succeeding)
  3. After running the upgrade migration on a v3.0 snapshot, every pre-existing FileRecord and ScanBatch points at a seeded `legacy-application-server` agent whose `scan_roots` matches the prior `SCAN_PATH`
  4. One sentinel `LIVE` ScanBatch exists per registered agent and is reused (not duplicated) when re-applied
  5. The migration is two-step (add nullable + backfill, then enforce NOT NULL + swap unique constraint) and can be downgraded cleanly to the v3.0 schema on an unmigrated test DB
**Plans**: 5 plans
- [ ] 24-01-PLAN.md — Test infrastructure: tests/test_migrations/ package + alembic-driven fixture (Wave 0)
- [ ] 24-02-PLAN.md — Agent model + ScanStatus.LIVE + agent_id columns + composite UQ on model layer (Wave 1)
- [ ] 24-03-PLAN.md — Migration 012: agents table, legacy agent seed, FKs, partial UQ, backfill + integration tests (Wave 2)
- [ ] 24-04-PLAN.md — Migration 013: NOT NULL + composite UQ swap + safe downgrade + [BLOCKING] roundtrip smoke (Wave 3)
- [ ] 24-05-PLAN.md — Ingestion service: stamp legacy agent_id, swap conflict target to composite (Wave 3)

### Phase 25: Internal Agent HTTP API & Bearer Auth
**Goal**: The application server exposes an authenticated, idempotent HTTP surface that agents can call to record every state change, with `agent_id` derived from the bearer token and never trusted from request bodies.
**Depends on**: Phase 24
**Requirements**: DIST-04, DIST-05, AUTH-01, AUTH-04
**Success Criteria** (what must be TRUE):
  1. Every `/api/internal/agent/*` route requires a bearer token; an unauthenticated request returns 401 and an unknown/revoked token returns 403
  2. The `agent_id` used by every endpoint is resolved by hashing the bearer token and looking it up in the `agents` table; any `agent_id` field in a request body is ignored or rejected
  3. Replaying the same chunk of file upserts, the same proposal mutation, or the same execution-log PATCH with the same natural keys (`(agent_id, original_path)`, `file_id`, `proposal_id`, agent-generated log UUIDs) produces no duplicate rows and the same final state
  4. Setting `agents.revoked_at` on a row immediately causes that agent's next `/api/internal/agent/*` call to be rejected with no application-server restart required (verified by integration test)
  5. The API surface covers, at minimum, file upsert, metadata write, fingerprint write, execution-log create/patch, and heartbeat — all callable end-to-end with an HTTP client
**Plans**: 6 plans
- [x] 25-01-PLAN.md — Schema foundation: Agent.last_status JSONB + migration 014 + conftest fixtures (Wave 1)
- [x] 25-02-PLAN.md — Auth helper module (agent_auth.py) + AUTH-01/AUTH-04 tests (Wave 2)
- [x] 25-03-PLAN.md — Files router + xmax regression test + schemas + auto-enqueue (Wave 3)
- [x] 25-04-PLAN.md — Metadata + Fingerprint + Heartbeat routers + schemas + tests (Wave 3)
- [x] 25-05-PLAN.md — Execution-log router (POST + PATCH monotonic) + schemas + tests (Wave 3)
- [x] 25-06-PLAN.md — App wiring: register 5 routers in main.py + config knobs (Wave 4)
- [x] 25-07-PLAN.md — Gap closure CR-01: agent_metadata partial-PUT NULL clobber + regression test (Wave 1, gap_closure)
- [x] 25-08-PLAN.md — Gap closure CR-02: execution-log terminal-state idempotent retry + regression tests (Wave 1, gap_closure)
**UI hint**: yes

### Phase 26: Task Code Reorg & HTTP-Backed Agent Worker
**Goal**: SAQ task code is cleanly split between the application server (fileless `phaze.tasks.controller`) and agents (file-bound `phaze.tasks.agent_worker`), with role-driven startup and per-agent queues so the same Docker image runs both roles correctly. Three new internal-agent endpoints (`/whoami`, `PUT /analysis/{file_id}`, `POST /tracklists`, `PATCH /proposals/{id}/state`) close the contract gap from Phase 25 so the full file-bound task surface can run on agents.
**Depends on**: Phase 25
**Requirements**: DIST-03, TASK-01, TASK-02, TASK-03, OPS-01
**Success Criteria** (what must be TRUE):
  1. `phaze.tasks.controller` exposes only fileless tasks (`generate_proposals`, `match_tracklist_to_discogs`, `scrape_and_store_tracklist`, `search_tracklist`, `refresh_tracklists` cron) and `phaze.tasks.agent_worker` exposes only file-bound tasks (`process_file`, `extract_file_metadata`, `fingerprint_file`, `scan_live_set`, `execute_approved_batch`)
  2. Setting `PHAZE_ROLE=control` boots the application-server worker with the fileless settings module and Postgres access; setting `PHAZE_ROLE=agent` boots the agent worker with the file-bound settings module and an HTTP client to the application server, with no Postgres driver loaded
  3. Every file-bound task body uses the HTTP client (no `async_session` import reachable in agent-worker code paths) and writes results via `/api/internal/agent/*`
  4. Each agent worker pulls from a per-agent SAQ queue named `phaze-agent-<agent_id>`; the application-server enqueuer selects the queue from `FileRecord.agent_id` and a job enqueued for agent A never executes on agent B
  5. Agent task jobs carry a self-contained payload (`file_id`, `file_path`, `file_type`, model paths, agent metadata) sufficient to execute without any read-back to the application server during the job
**Plans**: 13 plans
- [x] 26-01-PLAN.md — Deps (tenacity + respx + mypy overrides) + settings split (Base/Control/Agent + get_settings) + enum extensions (ProposalStatus.EXECUTED/FAILED, FileState.MOVED/UNCHANGED) (Wave 1)
- [x] 26-02-PLAN.md — PhazeAgentClient + 4-class error hierarchy + tenacity retry funnel + respx contract tests (Wave 2)
- [x] 26-03-PLAN.md — 5 new schema modules (agent_identity, agent_analysis, agent_tracklists, agent_proposals, agent_tasks) (Wave 2)
- [x] 26-04-PLAN.md — AgentTaskRouter + Redis integration tests (Wave 3)
- [x] 26-05-PLAN.md — GET /api/internal/agent/whoami router + 4 contract tests (Wave 3)
- [x] 26-06-PLAN.md — PUT /api/internal/agent/analysis/{file_id} router (idempotent upsert) + 8 contract tests (Wave 3)
- [x] 26-07-PLAN.md — POST /api/internal/agent/tracklists router (Redis idempotency cache) + integration tests (Wave 3)
- [x] 26-08-PLAN.md — PATCH /api/internal/agent/proposals/{id}/state router (state-machine joint update) + 11 contract tests incl. W1 cross-tenant guard (Wave 3)
- [x] 26-09-PLAN.md — phaze.tasks.controller SAQ settings module (fileless tasks only) (Wave 4)
- [x] 26-10-PLAN.md — phaze.tasks.agent_worker SAQ settings module + tests/test_task_split.py subprocess import-boundary test (D-25) (Wave 5)
- [x] 26-11-PLAN.md — Rewrite 5 file-bound task bodies (process_file, extract_file_metadata, fingerprint_file, scan_live_set, execute_approved_batch) to use ctx['api_client'] (Wave 4) -- COMPLETE 2026-05-12; D-03 import boundary verified; ExecutionStatus moved to phaze.enums; scan_live_set artist/title resolution removed (known v3.0 UI regression for future Phase 27/28 controller-side enrichment)
- [x] 26-12-PLAN.md — main.py wiring (4 new include_router + app.state.task_router + app.state.redis) + agent_files.py refactor to AgentTaskRouter (Wave 5)
- [x] 26-13-PLAN.md — Delete worker.py + session.py + docker-compose.yml controller.settings + doc sweep (legacy hostname-leaked name retired in favour of `controller`) (Wave 6)

### Phase 27: Watcher Service & User-Initiated Scan
**Goal**: Each file server continuously streams new file arrivals to the application server, and the administrator can also trigger an explicit scan of any path on any agent from the admin UI.
**Depends on**: Phase 26
**Requirements**: DIST-02, SCAN-01, SCAN-02, SCAN-03, SCAN-04
**Success Criteria** (what must be TRUE):
  1. A new `phaze-agent-watcher` service is defined and starts alongside `worker`, `audfprint`, and `panako` on the file-server compose; it stays running and observes the agent's configured roots via the `watchdog` library
  2. Dropping a new file into a watched root results in a new `FileRecord` appearing on the application server under that agent's sentinel `LIVE` ScanBatch, with `(agent_id, original_path)` as the natural key
  3. A file whose `mtime` is still changing is **not** posted; only after the configured settle period (default 10s) of stable `mtime` does the watcher compute SHA-256 and stream the record (verified by writing a file slowly and observing no early upsert)
  4. From the admin UI, an administrator can choose `(agent, scan_path)` and trigger a scan; this enqueues `scan_directory(scan_path, batch_id)` onto the chosen agent's queue and the agent streams discovered files back in chunks (e.g., 500 records per request), with `extract_file_metadata` enqueued per new music/video file before the scan completes
  5. The same upsert endpoint serves both bulk scans and per-file watcher events, and a re-walked path produces no duplicate FileRecord rows
**Plans**: 7 plans
- [x] 27-01-PLAN.md — Foundation: watchdog dep, AgentSettings watcher knobs, _shared/agent_bootstrap refactor, test scaffolding + extended import-boundary tests (Wave 0)
- [x] 27-02-PLAN.md — Schemas: FileUpsertChunk.batch_id, ScanBatchPatch/Response, ScanDirectoryPayload, TriggerScanForm (Wave 1)
- [x] 27-03-PLAN.md — Endpoints: PATCH /api/internal/agent/scan-batches + batch_id resolution in POST /files + patch_scan_batch client method + main.py wiring + contract tests (Wave 2)
- [x] 27-04-PLAN.md — Agent task: scan_directory(scan_path, batch_id) with chunking, per-chunk PATCH, terminal PATCH; registered in agent_worker.settings.functions (Wave 3)
- [x] 27-05-PLAN.md — Watcher package: phaze.agent_watcher (Debouncer, WatcherEventHandler, Poster, __main__); 16+ unit tests covering thread bridge, stuck-file cap, OSError vanish, LIVE-sentinel resolution (Wave 3)
- [x] 27-06-PLAN.md — Admin UI: routers/pipeline_scans.py (POST + GET progress + GET agent-roots HTMX swap), 6 partial templates, dashboard.html extension + 10 contract tests (Wave 3)
- [x] 27-07-PLAN.md — Deployment + docs: docker-compose watcher service, .env.example knobs, per-service README, STATE.md accumulation (Wave 5)
**UI hint**: yes

### Phase 28: Distributed Execution Dispatch
**Goal**: Approving a batch that spans multiple file servers results in each agent doing its own local copy-verify-delete, while the application server preserves the write-ahead audit trail and presents unified progress to the operator.
**Depends on**: Phase 27
**Requirements**: EXEC-01, EXEC-02, EXEC-03, EXEC-04, TASK-04
**Success Criteria** (what must be TRUE):
  1. Triggering execution on an approved batch groups proposals by `FileRecord.agent_id` and enqueues one `execute_approved_batch` sub-job per affected agent under a shared parent `batch_id`; the dispatch decision is visible in logs and via an admin endpoint
  2. Each agent performs copy-verify-delete locally for its assigned proposals and PATCHes per-operation status (started, copied, verified, deleted, failed) to the application server, so the `ExecutionLog` write-ahead trail survives the HTTP boundary with no rows lost on retry
  3. The application server owns the `exec:{batch_id}` Redis hash and serves SSE progress from a single aggregated key; the admin UI shows unified `total / completed / failed` counts that match the sum across all participating agents
  4. The execution UI exposes a per-agent breakdown (which agent handled which sub-batch, with its own counts) for debugging without requiring database access
  5. Each file server's audfprint and panako sidecars index only that file server's files; fingerprint queries during execution-adjacent flows resolve against the local sidecar and the limitation (no cross-file-server fingerprint matching) is documented in the admin UI / docs
**Plans**: 6 plans
- [x] 28-01-PLAN.md — Wave 0: test scaffolding + new dirs + audfprint/panako allow-list validator + sub_batch_index schema field
- [x] 28-02-PLAN.md — Wave 1: ExecBatchProgressPayload + agent_exec_batches router + main.py wiring + PhazeAgentClient.post_exec_batch_progress (contract tests)
- [x] 28-03-PLAN.md — Wave 1: execution_dispatch service (group-by-agent + revoked filter + chunking) + grouping unit tests
- [x] 28-04-PLAN.md — Wave 2: start_execution rewrite + SSE generator extension + agents_table.html + progress.html rewrite + revoked banner
- [x] 28-05-PLAN.md — Wave 2: tasks/execution.py — per-proposal terminal progress POST + SAQ-meta UUID lift (closes L6/L22) + _classify_failure_step + <step>: <reason> error_message
- [x] 28-06-PLAN.md — Wave 3: cross_fs_fingerprint_notice.html partial + duplicates/list.html inclusion + PROJECT.md Constraints paragraph + STATE.md accumulation
**UI hint**: yes

### Phase 29: Deployment Hardening & Agents Admin
**Goal**: A real two-host deployment runs end-to-end with the application server holding no file mounts, HTTPS + Redis hardening in place, and an admin can see at a glance which agents are alive and healthy.
**Depends on**: Phase 28
**Requirements**: DIST-01, AUTH-02, AUTH-03, OPS-02, OPS-03, OPS-04
**Success Criteria** (what must be TRUE):
  1. The application-server `docker-compose.yml` declares no `SCAN_PATH` or `MODELS_PATH` mount; starting the stack and attempting to read a music file from inside the `api` or `controller` container fails (verified manually) and the application server has no way to read or write file content
  2. A new `docker-compose.agent.yml` brings up exactly `worker`, `watcher`, `audfprint`, and `panako` on a file server, configured via env (`PHAZE_API_URL`, `PHAZE_REDIS_URL`, `PHAZE_AGENT_TOKEN`, `PHAZE_AGENT_ID`) to reach the application server; running it on a second host registers the agent and begins watching
  3. All agent → application-server traffic uses HTTPS terminated by a self-signed certificate from an application-server-local internal CA; each agent's `httpx` client trusts the CA file and rejects untrusted certs (verified by swapping the CA and observing connection failure)
  4. Redis on the application server requires `requirepass` and is bound only to the private LAN interface; an attempt to connect from outside the LAN or without the password fails, and agents connect with `redis://default:<password>@<host>:6379`
  5. Running `just download-models` on a fresh file server populates that host's local `/models` volume; the application-server image neither downloads nor mounts models
  6. Each agent posts a heartbeat to `/api/internal/agent/heartbeat` every 30 seconds; the Agents admin page lists every registered agent with name, status (alive/stale/revoked), queue depth, and last-seen timestamp, and refreshes without requiring a manual page reload
**Plans**: 8 plans
- [x] 29-01-PLAN.md — TLS termination + cert bootstrap (cryptography dep, cert_bootstrap, entrypoint, agent_ca_file/api_tls_sans/verify=) + D-04 wrong-CA integration test (Wave 1)
- [x] 29-02-PLAN.md — AgentSettings agent_env field + production-mode redis_url password validator (Wave 1)
- [x] 29-03-PLAN.md — Root docker-compose.yml strip mounts + delete watcher/agent-worker/audfprint/panako + redis hardening + .env.example + filesystem-isolation YAML-parse tests (Wave 1)
- [x] 29-04-PLAN.md — docker-compose.agent.yml (4 services: worker, watcher, audfprint, panako) + .env.example.agent + agent-compose YAML-parse tests + docker-publish.yml tag verification (Wave 2)
- [x] 29-05-PLAN.md — Models auto-download: phaze.scripts.download_models Python helper + phaze.tasks._shared.model_bootstrap + agent_worker/watcher startup wiring + bash shim rewrite (Wave 2)
- [x] 29-06-PLAN.md — Heartbeat caller: phaze.tasks.heartbeat.heartbeat_tick + SAQ CronJob registration in agent_worker.settings (trailing-seconds cron form per Critical Discovery #2) (Wave 3)
- [x] 29-07-PLAN.md — Agents admin page: constants + services.agent_liveness + utils.humanize + routers.admin_agents + 3 Jinja templates + base.html nav link + main.py registration (Wave 3)
- [x] 29-08-PLAN.md — Justfile recipes (up-agent, up-all) + docs/deployment.md + PROJECT.md Deployment subsection + scripts/update-project.sh touch + blocking human-verify checkpoint (Wave 4)
**UI hint**: yes

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
| 24. Schema Foundation & Agent Registry | v4.0 | 0/5 | Not started | - |
| 25. Internal Agent HTTP API & Bearer Auth | v4.0 | 8/8 | Complete    | 2026-05-12 |
| 26. Task Code Reorg & HTTP-Backed Agent Worker | v4.0 | 13/13 | Complete   | 2026-05-12 |
| 27. Watcher Service & User-Initiated Scan | v4.0 | 7/7 | Complete    | 2026-05-14 |
| 28. Distributed Execution Dispatch | v4.0 | 6/6 | Complete   | 2026-05-15 |
| 29. Deployment Hardening & Agents Admin | v4.0 | 8/8 | Complete    | 2026-05-17 |
