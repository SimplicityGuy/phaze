# Phase 16: Fingerprint Service & Batch Ingestion - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-01
**Phase:** 16-fingerprint-service-batch-ingestion
**Areas discussed:** Container architecture, API design, Hybrid scoring strategy, Batch ingestion workflow

---

## Container Architecture

### Engine Containerization

| Option | Description | Selected |
|--------|-------------|----------|
| Single container, both runtimes | One Docker image with Python + JRE. Simplest deployment. | |
| Two separate containers | audfprint (Python) + Panako (Java) each with own API. | ✓ |
| Python container + Panako subprocess | Single Python container, Panako via subprocess. | |

**User's choice:** Two separate containers
**Notes:** Clean isolation between Python and Java runtimes.

### Storage Persistence

| Option | Description | Selected |
|--------|-------------|----------|
| Docker volume | Named Docker volume. Survives restarts. | ✓ |
| Host bind mount | Mount host directory. | |
| PostgreSQL storage | Store in PostgreSQL binary columns. | |

**User's choice:** Docker volume
**Notes:** Consistent with pgdata pattern.

### Orchestration

| Option | Description | Selected |
|--------|-------------|----------|
| Main app calls both directly | arq worker makes HTTP calls to both in parallel. | ✓ |
| Gateway container | Third container wraps both APIs. | |
| Redis message queue | Pub/sub via Redis. | |

**User's choice:** Main app calls both directly
**Notes:** User emphasized: "ensure this is abstracted nicely in case we want to add a third or fourth fingerprinting service."

### Network Exposure

| Option | Description | Selected |
|--------|-------------|----------|
| Internal only | Both on internal Docker network. | ✓ |
| Exposed externally | Ports exposed to host. | |

**User's choice:** Internal only
**Notes:** Consistent with postgres/redis pattern.

### audfprint Base Image

| Option | Description | Selected |
|--------|-------------|----------|
| Same base (Python 3.13 + uv) | Reuse project's tooling. Separate Dockerfile. | ✓ |
| Standalone minimal | Minimal Python image. | |
| Multi-stage from main | Shared base image with caching. | |

**User's choice:** Same base
**Notes:** None

### Panako Base Image

| Option | Description | Selected |
|--------|-------------|----------|
| JRE slim + Panako JAR | Eclipse Temurin JRE slim with pre-built JAR. | ✓ |
| Full JDK + build from source | Build Panako in container. | |
| Pre-built Panako image | Use existing Docker image. | |

**User's choice:** JRE slim + Panako JAR
**Notes:** None

---

## API Design

### HTTP Framework

| Option | Description | Selected |
|--------|-------------|----------|
| FastAPI for both | audfprint uses FastAPI, Panako gets FastAPI wrapper. | ✓ |
| FastAPI + Javalin | Each uses native language framework. | |
| Flask for both | Lighter, no async. | |

**User's choice:** FastAPI for both
**Notes:** Consistent API style and OpenAPI docs.

### File Transfer

| Option | Description | Selected |
|--------|-------------|----------|
| Shared volume + path | Containers mount music volume. Ingest receives path. | ✓ |
| File upload in request | Multipart upload. | |
| Hybrid | Volume for batch, upload for ad-hoc. | |

**User's choice:** Shared volume + path
**Notes:** Essential for 200K file batch processing.

### Endpoint Surface

| Option | Description | Selected |
|--------|-------------|----------|
| Ingest + Query + Health | POST /ingest, POST /query, GET /health. | ✓ |
| + Status | Add GET /status with DB stats. | |
| Full CRUD | Add DELETE and GET per fingerprint. | |

**User's choice:** Ingest + Query + Health
**Notes:** Minimal API surface covering FPRINT-01 and FPRINT-02.

### Query Response Format

| Option | Description | Selected |
|--------|-------------|----------|
| Normalized 0-100 | Each engine normalizes output to 0-100. | ✓ |
| Raw engine scores | Return native engine output. | |
| Both raw + normalized | Both for debugging and display. | |

**User's choice:** Normalized 0-100
**Notes:** Consistent with tracklist matching pattern (Phase 15).

---

## Hybrid Scoring Strategy

### Score Combination

| Option | Description | Selected |
|--------|-------------|----------|
| Weighted average | audfprint 60% / Panako 40%. | ✓ |
| Max of both | Take highest score from either engine. | |
| You decide | Let Claude choose based on research. | |

**User's choice:** Weighted average
**Notes:** Emphasizes audfprint precision, gives Panako weight for tempo drift.

### Single-Engine Matches

| Option | Description | Selected |
|--------|-------------|----------|
| Include with penalty | Report match but cap at 70%. | ✓ |
| Accept at face value | No penalty for single-engine. | |
| Require both engines | Only report when both agree. | |

**User's choice:** Include with penalty
**Notes:** Both engines agreeing is stronger signal.

### Engine Abstraction

| Option | Description | Selected |
|--------|-------------|----------|
| Protocol class | Python Protocol defining ingest/query/health. | ✓ |
| ABC base class | Abstract base class with required methods. | |
| Dict-based config | Engine configs in list/dict with factory. | |

**User's choice:** Protocol class
**Notes:** Type-safe and extensible.

---

## Batch Ingestion Workflow

### Trigger

| Option | Description | Selected |
|--------|-------------|----------|
| Pipeline stage + manual | Auto after scan + manual backfill endpoint. | ✓ |
| Manual only | User triggers from UI/API. | |
| Automatic only | Auto-queued after scan. | |

**User's choice:** Pipeline stage + manual
**Notes:** Consistent with Phase 12 D-09 pattern.

### Progress Tracking

| Option | Description | Selected |
|--------|-------------|----------|
| DB counter + API endpoint | Track total/completed/failed. UI polls. | ✓ |
| arq job results | Query Redis for counts. | |
| Log-based | Progress in stdout only. | |

**User's choice:** DB counter + API endpoint
**Notes:** None

### Failure Handling

| Option | Description | Selected |
|--------|-------------|----------|
| Mark failed + retry later | Record reason, skip, retry on next run. | ✓ |
| Mark failed permanently | Record failure, no retry unless manual. | |
| Skip silently | Log error, no tracking. | |

**User's choice:** Mark failed + retry later
**Notes:** File stays in current state (not FINGERPRINTED).

### Engine Usage Per File

| Option | Description | Selected |
|--------|-------------|----------|
| Both engines always | Every file fingerprinted by both. | ✓ |
| audfprint first, Panako on failure | Panako as fallback only. | |
| Parallel both engines | Call both concurrently. | |

**User's choice:** Both engines always
**Notes:** Maximizes matching capability for Phase 17.

---

## Claude's Discretion

- Dockerfile details for both containers
- docker-compose.yml service definitions
- audfprint library integration details
- Panako subprocess wrapper
- FastAPI app structure within containers
- arq task function structure
- Progress tracking model design
- Retry backoff strategy
- Exact weight calibration

## Deferred Ideas

None — discussion stayed within phase scope.
