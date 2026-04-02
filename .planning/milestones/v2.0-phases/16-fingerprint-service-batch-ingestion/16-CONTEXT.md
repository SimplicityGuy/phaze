# Phase 16: Fingerprint Service & Batch Ingestion - Context

**Gathered:** 2026-04-01
**Status:** Ready for planning

<domain>
## Phase Boundary

Two dedicated fingerprint service containers (audfprint + Panako) running as long-lived Docker services with HTTP APIs, a clean abstraction layer for extensibility, and batch ingestion of all music files through the worker pool with progress tracking. Covers FPRINT-01 and FPRINT-02.

</domain>

<decisions>
## Implementation Decisions

### Container Architecture
- **D-01:** Two separate containers — one for audfprint (Python), one for Panako (Java). Each has its own Dockerfile and API. Clean isolation between runtimes.
- **D-02:** audfprint container uses the same base image/tooling as the main app (Python 3.13 + uv). Separate Dockerfile (e.g., `Dockerfile.audfprint`).
- **D-03:** Panako container uses JRE slim (Eclipse Temurin) with Panako JAR. Thin FastAPI wrapper in Python calls Panako via subprocess.
- **D-04:** Docker volumes for fingerprint database persistence. Named volumes (like pgdata pattern) that survive restarts and rebuilds.
- **D-05:** Both containers on internal Docker network only. Not exposed to host. Consistent with postgres/redis pattern.
- **D-06:** Main app's arq worker calls both containers directly via HTTP. **Abstracted cleanly** so adding a 3rd or 4th fingerprint engine requires only a new adapter — no changes to orchestration logic.

### API Design
- **D-07:** FastAPI for both containers' HTTP APIs. Consistent API style, OpenAPI docs, same patterns as main app.
- **D-08:** Shared volume + file path for audio access. Fingerprint containers mount the music volume (read-only). Ingest endpoint receives a file path, not file upload. Essential for 200K file batch processing.
- **D-09:** Three endpoints per container: `POST /ingest` (fingerprint a file), `POST /query` (find matches), `GET /health`. Minimal API surface covering FPRINT-01.
- **D-10:** Query endpoint returns normalized 0-100 confidence scores. Each engine normalizes its own output. Consistent with tracklist matching pattern (Phase 15 D-13).

### Hybrid Scoring Strategy
- **D-11:** Weighted average for combining engines. audfprint 60% / Panako 40% — emphasizes audfprint's precision for studio tracks while giving Panako weight for tempo-shifted live recordings.
- **D-12:** Single-engine matches included with penalty — capped at 70% confidence. Both engines agreeing is a stronger signal than either alone.
- **D-13:** Python Protocol class defines the common interface (ingest, query, health methods). Each engine adapter implements it. New engines just add a new adapter. Type-safe and extensible per user requirement.

### Batch Ingestion Workflow
- **D-14:** Pipeline stage + manual trigger. Auto-enqueue after scan (new files), plus manual "Fingerprint All" API endpoint for backfill. Consistent with tag extraction pattern (Phase 12 D-09).
- **D-15:** DB counter + API endpoint for progress tracking. Track fingerprinted count with total/completed/failed. UI can poll for progress.
- **D-16:** Failed files marked with failure reason. Skip on this pass, retry on next backfill run. File stays in current state (not transitioned to FINGERPRINTED).
- **D-17:** Both engines always — every file gets fingerprinted by both audfprint and Panako. Maximizes matching capability for Phase 17 (live set scanning). Two HTTP calls per file.
- **D-18:** File transitions to FINGERPRINTED state after both engines successfully process it. FileState.FINGERPRINTED already exists in the enum (Phase 12 D-03).

### Claude's Discretion
- Dockerfile details for both containers (base image tags, dependency installation, entry points)
- docker-compose.yml service definitions (ports, healthchecks, volume mount paths, depends_on)
- audfprint library integration details (landmark extraction, database format)
- Panako subprocess wrapper (JAR invocation, output parsing)
- FastAPI app structure within each container
- arq task function structure for batch fingerprinting
- Progress tracking table/model design
- Retry backoff strategy for failed fingerprinting
- Exact weight calibration for hybrid scoring (60/40 is starting point, tunable)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — FPRINT-01, FPRINT-02 acceptance criteria

### Project Context
- `.planning/PROJECT.md` — Specifies audfprint + Panako hybrid, long-running container with API/message interface, weighted scoring
- `CLAUDE.md` — Python 3.13 exclusively, uv package manager, Docker Compose deployment

### Existing Code (MUST READ)
- `docker-compose.yml` — Current 4-service stack (api, worker, postgres, redis). Add audfprint + panako services.
- `Dockerfile` — Current main app Dockerfile. Reference for audfprint container's base.
- `src/phaze/models/file.py` — FileRecord with FileState enum (FINGERPRINTED already defined)
- `src/phaze/tasks/worker.py` — WorkerSettings, task registration, cron jobs
- `src/phaze/tasks/functions.py` — Existing task patterns (process_file)
- `src/phaze/tasks/metadata_extraction.py` — Tag extraction task pattern (reference for fingerprint task)
- `src/phaze/services/metadata.py` — Service pattern for metadata operations

### Prior Phase Context
- `.planning/phases/12-infrastructure-audio-tag-extraction/12-CONTEXT.md` — D-03: FINGERPRINTED state, D-09: auto + manual triggers, D-04: queue all files for backfill
- `.planning/phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` — Worker patterns (max_jobs, retry with backoff)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- Docker Compose stack with named volumes (pgdata), health checks, internal networking
- arq worker infrastructure with retry/backoff, process pool patterns
- FileState.FINGERPRINTED already in the enum — ready to use
- Metadata extraction task as template for fingerprint task structure

### Established Patterns
- Docker services with health checks and depends_on conditions
- Read-only volume mounts for music files (SCAN_PATH)
- arq task functions with session management and error handling
- FastAPI for HTTP APIs

### Integration Points
- New docker-compose services: audfprint, panako
- New Dockerfiles: Dockerfile.audfprint, Dockerfile.panako
- New service: `services/fingerprint.py` (Protocol + adapters + orchestration)
- New task: `tasks/fingerprint.py` (batch ingestion task)
- Worker registration in `tasks/worker.py`
- State transition to FINGERPRINTED in FileRecord

</code_context>

<specifics>
## Specific Ideas

- User explicitly wants clean abstraction for engine extensibility — adding a 3rd/4th fingerprint engine should only require a new adapter, no orchestration changes
- Panako container has a thin FastAPI wrapper around the Java JAR (subprocess calls) — keeps the API interface uniform with audfprint
- 60/40 weight split is a starting point — user acknowledged this is tunable
- Single-engine match cap at 70% ensures both-engine agreement is incentivized

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 16-fingerprint-service-batch-ingestion*
*Context gathered: 2026-04-01*
