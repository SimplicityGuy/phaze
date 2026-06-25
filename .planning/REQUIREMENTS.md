# Requirements: Phaze — v5.0 Cloud Burst Analysis

**Defined:** 2026-06-24
**Core Value:** Get 200K messy music/concert files properly named, organized, deduplicated, with rich metadata — human-in-the-loop approval. v5.0 adds: long sets that can't finish locally get analyzed on free cloud compute, unattended.

## v5.0 Requirements

Each maps to exactly one roadmap phase (Traceability below).

### Official arm64 image (CLOUDIMG)

- [x] **CLOUDIMG-01**: An official arm64 essentia analysis agent image is published to GHCR, building essentia **from source** (the essentia-tensorflow wheel is x86-only) with the proven spike fixes baked in.
- [x] **CLOUDIMG-02**: The arm64 image is built and pushed by CI on a **native arm64 runner** (no QEMU) on the same release triggers as the x86 images, with matching tags.
- [x] **CLOUDIMG-03**: A CI/test guard confirms the arm64 image runs full analysis (MusiCNN + discogs-effnet) and produces results matching the x86 path within tolerance (BPM/key exact; model scores within a small epsilon).

### Compute-agent type (CLOUDAGENT)

- [ ] **CLOUDAGENT-01**: An operator can register a **compute agent** — an Agent with empty scan roots, no media, and an explicit `kind="compute"` capability marker.
- [x] **CLOUDAGENT-02**: A compute agent drains its per-agent SAQ queue and PUTs analysis results over HTTP exactly like a file-server agent, with no access to media or app ORM tables (only the SAQ Postgres broker + cache Redis + HTTP API).
- [x] **CLOUDAGENT-03**: The Agents admin page distinguishes compute agents (kind badge + liveness + queue depth) so the operator can see available cloud capacity.

### Duration routing & backfill (CLOUDROUTE)

- [ ] **CLOUDROUTE-01**: Files whose `metadata.duration` ≥ a configurable threshold (default 90 min) are routed to an available compute agent's queue instead of the local agent.
- [ ] **CLOUDROUTE-02**: When no compute agent is online, ≥threshold files are held in an "awaiting cloud" state and are **never** silently analyzed locally (where they would time out).
- [ ] **CLOUDROUTE-03**: Files below the threshold continue to analyze on the local file-server agent with unchanged behavior.
- [ ] **CLOUDROUTE-04**: The operator can backfill the existing timed-out long files (`analysis_failed`, duration ≥ threshold) to the cloud, scoped through the Phase 45 scheduling ledger so only previously-scheduled work is re-driven (no whole-backlog over-enqueue).

### Push pipeline (CLOUDPIPE)

- [ ] **CLOUDPIPE-01**: The control plane keeps at most a configurable number of cloud files staged-or-in-flight ("stay one ahead"; default 2 = one analyzing + one staged), driven by the scheduling ledger.
- [ ] **CLOUDPIPE-02**: A file-server agent pushes a cloud-routed file to the compute agent's scratch directory over the network (rsync/SSH over Tailscale) when the control plane schedules it; the file-server initiates and the compute agent only receives into scratch.
- [ ] **CLOUDPIPE-03**: The compute agent verifies file integrity (sha256, already on `FileRecord`) after transfer before analyzing; a mismatch fails the job cleanly and triggers re-push.
- [ ] **CLOUDPIPE-04**: The compute agent deletes its scratch copy after analysis completes (success or terminal failure), bounding local disk to the in-flight set.
- [ ] **CLOUDPIPE-05**: A failed or interrupted push/analysis is re-driven without orphaned scratch files or double-enqueues (idempotent, ledger-tracked).

### Deployment, config & docs (CLOUDDEPLOY)

- [ ] **CLOUDDEPLOY-01**: A cloud-agent deployment (compose) brings up the compute agent with Tailscale connectivity, no media mount, a scratch volume, and the arm64 image.
- [ ] **CLOUDDEPLOY-02**: All cloud-burst parameters — threshold, max in-flight, agent concurrency, scratch dir, push SSH target, cloud queue name, and a master enable toggle — are configurable via pydantic-settings with `_FILE`-secret support.
- [ ] **CLOUDDEPLOY-03**: A runbook documents OCI Always-Free A1 provisioning and a Tailscale ACL that scopes the A1 to exactly `lux:{5432,6379,8000}` + `nox→A1:22`, plus a least-privilege Postgres role for the queue broker.
- [ ] **CLOUDDEPLOY-04**: The entire cloud-burst feature can be disabled by a single config toggle, reverting to all-local analysis with no other change.

## Future Requirements (deferred)

- **CLOUDSCALE-01**: More than one concurrent cloud analysis / a second A1 instance (RAM-bound on the 12 GB Always-Free shape; revisit if a paid/larger shape is used).
- **CLOUDROUTE-05**: Cost/throughput-aware routing beyond a fixed duration threshold (e.g. project analyze-time, not just duration).
- **CLOUDIMG-04**: Multi-arch single-tag manifest (amd64 + arm64) instead of a separate arm64 tag.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Object storage (S3/OCI bucket) staging | Replaced by rsync-over-Tailscale to the A1's local disk; no egress, no bucket, no 20 GB cap |
| App-server ↔ file-server media transfer | v4.0 boundary preserved; only ephemeral file-server → compute-agent analysis transfer is added |
| Cloud agent scanning / owning files | Compute agents are pure extra compute — no scan roots, no FileRecord ownership |
| Paid cloud compute | Free-tier only (OCI Always-Free A1); paid burst was considered and deferred |
| Multi-tenant cloud self-service | Operator pre-provisions the A1 + token, as with all agents |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| CLOUDIMG-01..03 | Phase 47 | Pending |
| CLOUDAGENT-01..03 | Phase 48 | Pending |
| CLOUDROUTE-01..04 | Phase 49 | Pending |
| CLOUDPIPE-01..05 | Phase 50 | Pending |
| CLOUDDEPLOY-01..04 | Phase 51 | Pending |

_Phase numbers 47-51 continue from v4.0's last phase (46). Finalized by the roadmapper 2026-06-24; 18 requirements, 100% coverage, one phase each. See ROADMAP.md §"Phase Details (v5.0)"._
