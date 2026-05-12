# Requirements: Phaze

**Defined:** 2026-05-11
**Core Value:** Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

## v4.0 Requirements

Requirements for Distributed Agents. Each maps to roadmap phases.

### Topology & Boundary

- [ ] **DIST-01**: The application server runs the API, UI, Postgres, Redis, and a fileless SAQ worker; it has no `SCAN_PATH` or `MODELS_PATH` filesystem mounts and cannot read or write file content
- [ ] **DIST-02**: Each file server runs one or more agents (SAQ worker + watcher + audfprint + panako sidecars) that hold local files and execute all file-bearing work locally
- [x] **DIST-03**: Each agent pulls jobs from a per-agent SAQ queue named `phaze-agent-<agent_id>` on the application server's Redis; the application server enqueues file-bound jobs onto the correct queue using `FileRecord.agent_id`
- [ ] **DIST-04**: Agents have zero direct Postgres access; every state change (file discovered, analysis result, fingerprint, execution log, heartbeat) is an authenticated HTTPS call to `/api/internal/agent/*` on the application server
- [ ] **DIST-05**: Every `/api/internal/agent/*` endpoint is idempotent on retry; natural keys (`(agent_id, original_path)`, `file_id`, `proposal_id`, agent-generated log UUIDs) guarantee replay safety

### Data Model & Migration

- [ ] **DATA-01**: An `agents` table records each registered agent with `id`, `name`, `token_hash`, `scan_roots` (jsonb), `created_at`, `last_seen_at`, and `revoked_at`
- [ ] **DATA-02**: `FileRecord.agent_id` is a non-null string column referencing the agents table; the unique constraint on the file table moves from `(original_path)` to `(agent_id, original_path)`
- [ ] **DATA-03**: `ScanBatch.agent_id` is a non-null string column; one sentinel `LIVE` `ScanBatch` per agent acts as the parent batch for all watcher-originated file events
- [ ] **DATA-04**: A two-step Alembic migration adds the new columns and `agents` table, seeds a `legacy-application-server` agent row pointing at the current `SCAN_PATH`, backfills every existing `FileRecord` / `ScanBatch` to it, and only then enforces `NOT NULL` and swaps the unique constraint

### Authentication & Security

- [ ] **AUTH-01**: Each agent authenticates to the application server with a unique bearer token; the application server stores only the token hash and derives `agent_id` from the token lookup — never from a request body field
- [ ] **AUTH-02**: All agent → application-server traffic uses HTTPS terminated by a self-signed certificate issued by an application-server-local internal CA; each agent's httpx client trusts that CA file
- [ ] **AUTH-03**: Redis on the application server requires `requirepass` and is bound only to the private LAN interface (no `0.0.0.0` exposure); agents connect with `redis://default:<password>@<host>:6379`
- [ ] **AUTH-04**: Agent tokens are rotatable: revoking a token in the `agents` table immediately blocks further `/api/internal/agent/*` calls from that agent without requiring an application-server restart

### Scan & Watcher

- [ ] **SCAN-01**: The administrator can trigger a scan of a specific path on a specific agent from the admin UI; the application server enqueues `scan_directory(scan_path, batch_id)` onto the chosen agent's queue
- [ ] **SCAN-02**: As an agent walks the scan path, it streams discovered file records to the application server in chunks (e.g., 500 records per request); the application server upserts each chunk and enqueues `extract_file_metadata` per new music/video file before the scan completes
- [ ] **SCAN-03**: Each file server runs an always-on `phaze-agent-watcher` service that observes its configured roots with the `watchdog` library; new file events stream to the application server via the same scan-batch upsert endpoint, attributed to a per-agent sentinel `ScanBatch`
- [ ] **SCAN-04**: The watcher waits for a file's `mtime` to be stable for a configurable settle period (default 10s) before computing SHA-256 and posting it; partial / in-progress writes are not propagated

### Task Execution

- [ ] **TASK-01**: File-bound SAQ tasks (`process_file`, `extract_file_metadata`, `fingerprint_file`, `scan_live_set`, `execute_approved_batch`) run only on agents; their bodies use an HTTP client to the application server instead of an `async_session`
- [x] **TASK-02**: Fileless SAQ tasks (`generate_proposals`, `match_tracklist_to_discogs`, `scrape_and_store_tracklist`, `search_tracklist`, `refresh_tracklists` cron) run only on the application-server worker and continue using direct Postgres access
- [x] **TASK-03**: Agent task job payloads carry all data the agent needs (`file_id`, `file_path`, `file_type`, model path, etc.) so jobs are self-contained snapshots at enqueue time; agents never read file state from the application server during job execution
- [ ] **TASK-04**: Each file server runs its own audfprint and panako sidecars indexing only that file server's files; no cross-file-server fingerprint matching is supported in v4.0

### Distributed Execution

- [ ] **EXEC-01**: When the administrator triggers an approved-batch execution, the application server groups approved proposals by `FileRecord.agent_id` and enqueues one `execute_approved_batch` sub-job per affected agent under a shared parent `batch_id`
- [ ] **EXEC-02**: Each agent performs copy-verify-delete locally for its sub-batch and reports per-operation status to the application server via PATCH calls so the write-ahead `ExecutionLog` audit trail is preserved across the HTTP boundary
- [ ] **EXEC-03**: Agents PATCH per-file progress updates to the application server; the application server owns the `exec:{batch_id}` Redis hash and continues to serve SSE progress from a single aggregated key
- [ ] **EXEC-04**: A batch that spans multiple agents reports unified progress (`total`, `completed`, `failed`) to the UI; per-agent breakdown is available for debugging

### Deployment & Operations

- [x] **OPS-01**: Both the application-server role and the agent role run from the same Docker image; `PHAZE_ROLE={control,agent}` (or equivalent env) selects which SAQ settings module is loaded and which startup resources are instantiated
- [ ] **OPS-02**: A new `docker-compose.agent.yml` brings up only `worker`, `watcher`, `audfprint`, and `panako` on a file server, configured via env to point at the application server's Redis URL, API URL, and bearer token
- [ ] **OPS-03**: Each file server runs `just download-models` once at setup to populate its own local `/models` volume; the application server no longer downloads or mounts models
- [ ] **OPS-04**: Each agent posts a heartbeat to `/api/internal/agent/heartbeat` every 30 seconds; the application server updates `agents.last_seen_at` and exposes an "Agents" admin page listing each agent's status, queue depth, last seen, and revoked state

## Future Requirements

Deferred to a later milestone. Tracked but not in current roadmap.

### Cross-Agent Capabilities

- **XAGENT-01**: Cross-file-server fingerprint matching (agent-side orchestrator fans out queries to other agents' sidecars)
- **XAGENT-02**: Cross-file-server execution batches (moves that span hosts, e.g., relocating a file from one file server to another)

### Watcher Enhancements

- **WATCH-05**: Delete event handling (`FileState.MISSING` or equivalent state transition)
- **WATCH-06**: Move / rename detection within a watched tree (treat as state update, not new-file insertion)
- **WATCH-07**: Watcher catch-up on startup (scan tree against last-seen timestamps to recover files that arrived while watcher was down)

### Operational Polish

- **OPS-05**: mTLS in addition to bearer tokens for the agent boundary
- **OPS-06**: Multi-tenant agent registration self-service (today: admin pre-seeds tokens)
- **OPS-07**: Agent metric scraping endpoint (Prometheus-compatible)

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Files transferred between application server and file server | The whole point of v4.0 is keeping files local to file servers; transfer would defeat the boundary |
| Public-internet agent connectivity | Private LAN only; would require real certs, DNS, and a hardened threat model |
| Postgres replication / read-replica on file server | Option II in the grill was rejected — agents stay HTTP-only |
| Tailscale / mesh networking | User chose plain private LAN (Q10b); revisit only if topology demands roaming |
| Background "rebalance" task that redistributes files across agents | Single-file-server in v4.0; multi-host rebalancing belongs in a future milestone |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| DIST-01 | Phase 29 — Deployment Hardening & Agents Admin | Pending |
| DIST-02 | Phase 27 — Watcher Service & User-Initiated Scan | Pending |
| DIST-03 | Phase 26 — Task Code Reorg & HTTP-Backed Agent Worker | Complete |
| DIST-04 | Phase 25 — Internal Agent HTTP API & Bearer Auth | Pending |
| DIST-05 | Phase 25 — Internal Agent HTTP API & Bearer Auth | Pending |
| DATA-01 | Phase 24 — Schema Foundation & Agent Registry | Pending |
| DATA-02 | Phase 24 — Schema Foundation & Agent Registry | Pending |
| DATA-03 | Phase 24 — Schema Foundation & Agent Registry | Pending |
| DATA-04 | Phase 24 — Schema Foundation & Agent Registry | Pending |
| AUTH-01 | Phase 25 — Internal Agent HTTP API & Bearer Auth | Pending |
| AUTH-02 | Phase 29 — Deployment Hardening & Agents Admin | Pending |
| AUTH-03 | Phase 29 — Deployment Hardening & Agents Admin | Pending |
| AUTH-04 | Phase 25 — Internal Agent HTTP API & Bearer Auth | Pending |
| SCAN-01 | Phase 27 — Watcher Service & User-Initiated Scan | Pending |
| SCAN-02 | Phase 27 — Watcher Service & User-Initiated Scan | Pending |
| SCAN-03 | Phase 27 — Watcher Service & User-Initiated Scan | Pending |
| SCAN-04 | Phase 27 — Watcher Service & User-Initiated Scan | Pending |
| TASK-01 | Phase 26 — Task Code Reorg & HTTP-Backed Agent Worker | Pending |
| TASK-02 | Phase 26 — Task Code Reorg & HTTP-Backed Agent Worker | Complete |
| TASK-03 | Phase 26 — Task Code Reorg & HTTP-Backed Agent Worker | Complete |
| TASK-04 | Phase 28 — Distributed Execution Dispatch | Pending |
| EXEC-01 | Phase 28 — Distributed Execution Dispatch | Pending |
| EXEC-02 | Phase 28 — Distributed Execution Dispatch | Pending |
| EXEC-03 | Phase 28 — Distributed Execution Dispatch | Pending |
| EXEC-04 | Phase 28 — Distributed Execution Dispatch | Pending |
| OPS-01 | Phase 26 — Task Code Reorg & HTTP-Backed Agent Worker | Complete |
| OPS-02 | Phase 29 — Deployment Hardening & Agents Admin | Pending |
| OPS-03 | Phase 29 — Deployment Hardening & Agents Admin | Pending |
| OPS-04 | Phase 29 — Deployment Hardening & Agents Admin | Pending |

**Coverage:** 26 / 26 v4.0 requirements mapped ✓

---
*Last updated: 2026-05-11 — milestone v4.0 roadmap mapped (Phases 24-29)*
