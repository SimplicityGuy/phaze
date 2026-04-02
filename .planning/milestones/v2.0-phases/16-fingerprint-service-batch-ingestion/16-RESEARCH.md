# Phase 16: Fingerprint Service & Batch Ingestion - Research

**Researched:** 2026-04-01
**Domain:** Audio fingerprinting (audfprint + Panako), Docker service architecture, batch processing
**Confidence:** MEDIUM

## Summary

Phase 16 introduces two separate Docker containers for audio fingerprinting -- one running audfprint (Python, landmark-based) and one running Panako (Java, time-stretch/pitch-shift robust). Both expose thin FastAPI HTTP APIs and store fingerprint databases on Docker named volumes. The main app's arq worker calls both containers per file, combining scores via weighted average (60/40 audfprint/Panako).

The primary technical risks are: (1) audfprint is an older, unmaintained Python project with no pip package -- it must be cloned from GitHub and vendored into the container, and its Python 2/3 codebase needs compatibility verification with modern numpy/scipy; (2) Panako uses LMDB for storage which handles persistence well but is AGPL-licensed, requiring careful isolation; (3) parsing CLI output from both tools requires defensive string handling since neither was designed for machine-readable output.

**Primary recommendation:** Build both containers with FastAPI wrappers that invoke the fingerprint tools via subprocess, normalize output into consistent JSON responses with 0-100 confidence scores, and persist fingerprint databases on named Docker volumes. Use httpx AsyncClient in the main app's worker to call both containers.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: Two separate containers -- one for audfprint (Python), one for Panako (Java). Each has its own Dockerfile and API. Clean isolation between runtimes.
- D-02: audfprint container uses the same base image/tooling as the main app (Python 3.13 + uv). Separate Dockerfile (e.g., `Dockerfile.audfprint`).
- D-03: Panako container uses JRE slim (Eclipse Temurin) with Panako JAR. Thin FastAPI wrapper in Python calls Panako via subprocess.
- D-04: Docker volumes for fingerprint database persistence. Named volumes (like pgdata pattern) that survive restarts and rebuilds.
- D-05: Both containers on internal Docker network only. Not exposed to host. Consistent with postgres/redis pattern.
- D-06: Main app's arq worker calls both containers directly via HTTP. Abstracted cleanly so adding a 3rd or 4th fingerprint engine requires only a new adapter -- no changes to orchestration logic.
- D-07: FastAPI for both containers' HTTP APIs. Consistent API style, OpenAPI docs, same patterns as main app.
- D-08: Shared volume + file path for audio access. Fingerprint containers mount the music volume (read-only). Ingest endpoint receives a file path, not file upload.
- D-09: Three endpoints per container: POST /ingest, POST /query, GET /health. Minimal API surface.
- D-10: Query endpoint returns normalized 0-100 confidence scores. Each engine normalizes its own output.
- D-11: Weighted average for combining engines. audfprint 60% / Panako 40%.
- D-12: Single-engine matches included with penalty -- capped at 70% confidence.
- D-13: Python Protocol class defines the common interface. Each engine adapter implements it.
- D-14: Pipeline stage + manual trigger. Auto-enqueue after scan (new files), plus manual "Fingerprint All" API endpoint for backfill.
- D-15: DB counter + API endpoint for progress tracking. Track fingerprinted count with total/completed/failed.
- D-16: Failed files marked with failure reason. Skip on this pass, retry on next backfill run.
- D-17: Both engines always -- every file gets fingerprinted by both audfprint and Panako.
- D-18: File transitions to FINGERPRINTED state after both engines successfully process it.

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

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.

</user_constraints>

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| FPRINT-01 | Fingerprint service runs as a long-running Docker container with API/message interface | Two containers (audfprint + Panako) each with FastAPI HTTP API, named volumes for persistence, internal Docker network. Decisions D-01 through D-10 fully cover this. |
| FPRINT-02 | Batch job fingerprints all music files via worker pool with persistent fingerprint database | arq worker task calling both containers via httpx, progress tracking model in Postgres, state transition to FINGERPRINTED. Decisions D-14 through D-18 fully cover this. |

</phase_requirements>

## Standard Stack

### Core (Fingerprint Containers)

| Library/Tool | Version | Purpose | Notes |
|--------------|---------|---------|-------|
| audfprint | HEAD (GitHub clone) | Landmark-based audio fingerprinting | No pip package. Clone `dpwe/audfprint` into container. MIT license. |
| Panako | latest (GitHub clone) | Time-stretch/pitch-shift robust fingerprinting | Built via Gradle `shadowJar`. AGPL license -- isolated in own container. |
| FastAPI | >=0.135.2 | HTTP API for both containers | Same version as main app for consistency |
| uvicorn | >=0.34.0 | ASGI server for both containers | Same as main app |
| httpx | >=0.28.1 | HTTP client (main app worker calls containers) | Already in project dependencies |

### audfprint Container Dependencies

| Dependency | Purpose | Notes |
|------------|---------|-------|
| numpy | Array operations | Required by audfprint |
| scipy | Signal processing | Required by audfprint |
| docopt | CLI argument parsing | Required by audfprint (used internally even when called via Python API) |
| joblib | Parallel processing | Required by audfprint |
| psutil | System monitoring | Required by audfprint |
| librosa | Audio loading | Required by audfprint for audio decoding |
| ffmpeg (system) | Audio decoding | Required by librosa/audfprint |

### Panako Container Dependencies

| Dependency | Purpose | Notes |
|------------|---------|-------|
| Eclipse Temurin JRE 21 | Java runtime | JRE slim image for minimal size. Panako needs JDK 11+ but use LTS 21. |
| ffmpeg (system) | Audio decoding | Required by Panako |
| Python 3.13-slim | FastAPI wrapper runtime | Thin wrapper only -- no heavy Python deps |
| LMDB (bundled) | Fingerprint storage | Bundled with Panako JAR via shadowJar. Stored on named volume. |

### Main App Additions

| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| httpx | >=0.28.1 | Async HTTP calls to fingerprint containers | Already a dependency |

No new pip dependencies needed in the main app. The fingerprint containers are self-contained.

## Architecture Patterns

### Recommended Project Structure

```
services/
  audfprint/
    Dockerfile.audfprint       # Python 3.13-slim + audfprint + FastAPI
    app.py                     # FastAPI app with /ingest, /query, /health
    audfprint/                 # Cloned audfprint source (vendored)
    requirements.txt           # numpy, scipy, docopt, joblib, psutil, librosa, fastapi, uvicorn
  panako/
    Dockerfile.panako          # Temurin JRE 21 + Python 3.13-slim (multi-stage)
    app.py                     # FastAPI app with /ingest, /query, /health
    panako.jar                 # Built Panako shadow JAR (or build in Dockerfile)
    requirements.txt           # fastapi, uvicorn
src/phaze/
  services/
    fingerprint.py             # Protocol + adapters (AudfprintAdapter, PanakoAdapter) + FingerprintOrchestrator
  tasks/
    fingerprint.py             # arq task: fingerprint_file(ctx, file_id)
  models/
    fingerprint.py             # FingerprintResult SQLAlchemy model (tracks per-engine results)
```

### Pattern 1: Protocol-Based Engine Abstraction (D-06, D-13)

**What:** Python Protocol class defining the fingerprint engine interface. Each engine gets an adapter that implements it. Orchestrator loops over registered adapters.
**When to use:** Always -- this is a locked decision.

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class FingerprintEngine(Protocol):
    """Common interface for fingerprint engines."""

    @property
    def name(self) -> str: ...

    @property
    def weight(self) -> float: ...

    async def ingest(self, file_path: str) -> IngestResult: ...

    async def query(self, file_path: str) -> list[QueryMatch]: ...

    async def health(self) -> bool: ...
```

### Pattern 2: Subprocess CLI Wrapper (both containers)

**What:** FastAPI endpoint receives file path, invokes CLI tool via subprocess, parses stdout, returns JSON.
**When to use:** Both audfprint and Panako containers.

```python
# audfprint container -- app.py
import asyncio
import subprocess
from fastapi import FastAPI, HTTPException

app = FastAPI()

@app.post("/ingest")
async def ingest(file_path: str) -> dict:
    """Add a file to the audfprint database."""
    result = await asyncio.to_thread(
        subprocess.run,
        ["python", "audfprint.py", "add", "--dbase", "/data/fprint/fprint.pklz", file_path],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"status": "ingested", "file_path": file_path}
```

```python
# panako container -- app.py
import asyncio
import subprocess

@app.post("/ingest")
async def ingest(file_path: str) -> dict:
    """Add a file to the Panako database."""
    result = await asyncio.to_thread(
        subprocess.run,
        ["java", "-jar", "/app/panako.jar", "store", file_path],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"status": "ingested", "file_path": file_path}
```

### Pattern 3: Async HTTP Client Adapter (main app)

**What:** Each adapter uses httpx.AsyncClient to call its container's HTTP API.
**When to use:** In `services/fingerprint.py` for the main app.

```python
class AudfprintAdapter:
    def __init__(self, base_url: str, weight: float = 0.6) -> None:
        self._base_url = base_url
        self._weight = weight
        self._client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    @property
    def name(self) -> str:
        return "audfprint"

    @property
    def weight(self) -> float:
        return self._weight

    async def ingest(self, file_path: str) -> IngestResult:
        response = await self._client.post("/ingest", json={"file_path": file_path})
        response.raise_for_status()
        return IngestResult(**response.json())
```

### Pattern 4: Weighted Score Combination (D-11, D-12)

**What:** Orchestrator collects scores from all engines, applies weights, caps single-engine matches.
**When to use:** In `FingerprintOrchestrator.query()`.

```python
async def combined_query(self, file_path: str) -> list[CombinedMatch]:
    results = {}
    for engine in self.engines:
        try:
            matches = await engine.query(file_path)
            results[engine.name] = matches
        except Exception:
            results[engine.name] = []

    # Combine by matched track, weighting each engine's score
    combined = {}
    for engine in self.engines:
        for match in results.get(engine.name, []):
            key = match.track_id
            if key not in combined:
                combined[key] = {"scores": {}, "track_id": key}
            combined[key]["scores"][engine.name] = match.confidence

    final = []
    for entry in combined.values():
        scores = entry["scores"]
        if len(scores) == len(self.engines):
            # Both engines agree -- weighted average
            score = sum(self.engines_by_name[name].weight * s for name, s in scores.items())
        else:
            # Single engine only -- cap at 70 (D-12)
            score = min(70.0, max(scores.values()))
        final.append(CombinedMatch(track_id=entry["track_id"], confidence=score))

    return sorted(final, key=lambda m: m.confidence, reverse=True)
```

### Anti-Patterns to Avoid

- **File upload to fingerprint containers:** Do NOT upload audio via HTTP multipart. Mount the music volume read-only and pass file paths (D-08). Uploading 200K files would be catastrophically slow.
- **Direct library import of audfprint in main app:** audfprint has heavy numpy/scipy/librosa deps and an old codebase. Keep it isolated in its container.
- **Synchronous subprocess calls in async endpoints:** Use `asyncio.to_thread(subprocess.run, ...)` or `asyncio.create_subprocess_exec` in the FastAPI wrappers to avoid blocking the event loop.
- **Shared fingerprint database volume between containers:** Each engine has its own volume. audfprint uses `.pklz` (pickle), Panako uses LMDB. Completely different formats.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Audio fingerprint extraction | Custom spectral peak detection | audfprint (landmarks) + Panako (spectral peaks with time-freq robustness) | Decades of research behind these algorithms. Reimplementing would take months. |
| Time-stretch/pitch-shift matching | Custom DTW or correlation | Panako | Its core differentiator. Handles 93-107% speed variation out of the box. |
| Fingerprint database storage | Custom hash table on disk | audfprint's `.pklz` + Panako's LMDB | Both tools manage their own storage format. Don't interfere. |
| CLI output parsing | Regex-heavy custom parsers | Structured wrappers with error handling | But DO write careful parsers -- just don't skip error handling. |

## Common Pitfalls

### Pitfall 1: audfprint Python 3.13 Compatibility

**What goes wrong:** audfprint was written for Python 2/3 compatibility with no recent maintenance. Modern numpy (2.x) and scipy may break it.
**Why it happens:** The codebase uses patterns like `sys.version_info[0] >= 3` checks and imports deprecated numpy APIs.
**How to avoid:** The container isolates this risk. However, D-02 says "same base image/tooling as main app (Python 3.13 + uv)". If audfprint fails on Python 3.13, fallback to Python 3.11 in the audfprint container. Test this early -- build the Dockerfile first and verify `audfprint.py new --dbase test.pklz test.mp3` works.
**Warning signs:** ImportError on numpy deprecated types (`np.float` removed in numpy 2.0), `time.clock` removal (Python 3.8+, but already handled in audfprint source).

### Pitfall 2: Subprocess Blocking the Event Loop

**What goes wrong:** `subprocess.run()` blocks the async event loop, causing the FastAPI health check to time out while a long fingerprint operation runs.
**Why it happens:** Fingerprinting a single file can take 5-30 seconds. A synchronous subprocess call blocks all concurrent requests.
**How to avoid:** Use `asyncio.to_thread(subprocess.run, ...)` or `asyncio.create_subprocess_exec()` for all subprocess calls in the FastAPI wrappers.
**Warning signs:** Health check failures, HTTP timeouts on concurrent requests.

### Pitfall 3: audfprint Database Lock Contention

**What goes wrong:** audfprint's `.pklz` database is a serialized Python pickle. It cannot handle concurrent writes from multiple processes.
**Why it happens:** Pickle serialization is inherently single-writer. If two ingest requests hit simultaneously, one will corrupt the database.
**How to avoid:** Serialize all write operations through a single FastAPI instance. Use an asyncio.Lock or queue to ensure only one ingest/add operation happens at a time. Reads (match) can be concurrent since they don't modify the database.
**Warning signs:** Corrupted `.pklz` file, "pickle load failed" errors.

### Pitfall 4: Panako LMDB Size Limits

**What goes wrong:** LMDB has a default `map_size` that may be too small for 200K files. The database silently stops accepting writes when full.
**Why it happens:** Panako's default LMDB configuration may assume smaller datasets.
**How to avoid:** Check Panako's configuration options for LMDB map size. Set a generous size (e.g., 10GB) via Panako's config system (`panako config` shows options). Monitor database stats via `panako stats`.
**Warning signs:** Ingest silently fails, `panako stats` shows stagnant fingerprint count.

### Pitfall 5: File Path Mismatch Between Containers

**What goes wrong:** The main app stores paths as `/data/music/...` but the fingerprint container mounts the volume at a different path.
**Why it happens:** Docker volume mount points can differ between containers.
**How to avoid:** Mount the music volume at the same path (`/data/music:ro`) in all containers. The file paths stored in Postgres will then be valid in every container.
**Warning signs:** "File not found" errors in fingerprint containers despite files existing in the main app.

### Pitfall 6: State Transition Race Condition

**What goes wrong:** File transitions to FINGERPRINTED after only one engine processes it, or the state gets set twice.
**Why it happens:** Two separate HTTP calls (one per engine) complete at different times. If the task function checks "is fingerprinted by both?" incorrectly, it may transition too early.
**How to avoid:** Store per-engine results in a `FingerprintResult` table. After both engines return, a single atomic state transition happens. Use a DB query: `SELECT COUNT(*) FROM fingerprint_results WHERE file_id = X AND status = 'success'` and transition only when count equals number of engines.
**Warning signs:** Files in FINGERPRINTED state missing one engine's results.

## Code Examples

### audfprint Container Dockerfile

```dockerfile
# Dockerfile.audfprint
FROM python:3.13-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

# Clone audfprint
RUN git clone --depth 1 https://github.com/dpwe/audfprint.git /app/audfprint

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install Python deps
COPY services/audfprint/pyproject.toml services/audfprint/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy FastAPI wrapper
COPY services/audfprint/app.py ./

ENV UV_NO_SYNC=1

RUN useradd -m -r audfprint
USER audfprint

EXPOSE 8001
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
```

### Panako Container Dockerfile (Multi-Stage)

```dockerfile
# Dockerfile.panako
# Stage 1: Build Panako JAR
FROM eclipse-temurin:21-jdk-jammy AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/JorenSix/Panako.git /build/panako
WORKDIR /build/panako
RUN ./gradlew shadowJar

# Stage 2: Runtime
FROM python:3.13-slim

WORKDIR /app

# System deps: JRE + ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install JRE (minimal)
COPY --from=eclipse-temurin:21-jre-jammy /opt/java/openjdk /opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:$PATH"

# Copy built Panako JAR
COPY --from=builder /build/panako/build/libs/Panako-*-all.jar /app/panako.jar

# Install uv + Python deps for FastAPI wrapper
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY services/panako/pyproject.toml services/panako/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY services/panako/app.py ./

ENV UV_NO_SYNC=1
# Panako needs writable home for LMDB
ENV HOME=/data/fprint

RUN useradd -m -r panako
USER panako

EXPOSE 8002
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
```

### docker-compose.yml Additions

```yaml
  audfprint:
    build:
      context: .
      dockerfile: services/audfprint/Dockerfile.audfprint
    volumes:
      - "${SCAN_PATH:-/data/music}:/data/music:ro"
      - audfprint_data:/data/fprint
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 10s
      timeout: 5s
      retries: 3

  panako:
    build:
      context: .
      dockerfile: services/panako/Dockerfile.panako
    volumes:
      - "${SCAN_PATH:-/data/music}:/data/music:ro"
      - panako_data:/data/fprint
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/health"]
      interval: 10s
      timeout: 5s
      retries: 3

volumes:
  pgdata:
  audfprint_data:
  panako_data:
```

### FingerprintResult Model

```python
class FingerprintResult(TimestampMixin, Base):
    """Per-engine fingerprint result for a file."""

    __tablename__ = "fingerprint_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    engine: Mapped[str] = mapped_column(String(30), nullable=False)  # "audfprint" or "panako"
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # "success" or "failed"
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_fprint_file_engine", "file_id", "engine", unique=True),
    )
```

### arq Task Function

```python
async def fingerprint_file(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Fingerprint a single file through all engines."""
    try:
        async with ctx["async_session"]() as session:
            result = await session.execute(
                select(FileRecord).where(FileRecord.id == uuid.UUID(file_id))
            )
            file_record = result.scalar_one_or_none()
            if file_record is None:
                return {"file_id": file_id, "status": "not_found"}

            orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]
            results = await orchestrator.ingest_all(file_record.current_path)

            # Store per-engine results
            for engine_name, engine_result in results.items():
                existing = await session.execute(
                    select(FingerprintResult).where(
                        FingerprintResult.file_id == file_record.id,
                        FingerprintResult.engine == engine_name,
                    )
                )
                fprint = existing.scalar_one_or_none()
                if fprint is None:
                    fprint = FingerprintResult(
                        file_id=file_record.id, engine=engine_name
                    )
                    session.add(fprint)
                fprint.status = engine_result.status
                fprint.error_message = engine_result.error

            # Check if ALL engines succeeded -> transition to FINGERPRINTED (D-18)
            all_success = all(r.status == "success" for r in results.values())
            if all_success:
                file_record.state = FileState.FINGERPRINTED

            await session.commit()
            return {
                "file_id": file_id,
                "status": "fingerprinted" if all_success else "partial",
            }

    except Exception as exc:
        raise Retry(defer=ctx["job_try"] * 5) from exc
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Chromaprint/AcoustID | Multiple engines (audfprint + Panako) | Project decision | Chromaprint is for exact-match identification. audfprint handles landmark matching. Panako handles pitch/tempo-shifted content. Complementary, not replacements. |
| Single fingerprint engine | Hybrid multi-engine scoring | Project decision | Higher confidence matches when both engines agree. Penalty for single-engine matches forces cross-validation. |

## Open Questions

1. **audfprint Python 3.13 Compatibility**
   - What we know: audfprint uses numpy/scipy/librosa, has Python 2/3 compat code. No recent releases.
   - What's unclear: Whether it works on Python 3.13 with numpy 2.x. The `np.float` deprecation and other numpy 2.0 breaking changes may require patches.
   - Recommendation: D-02 says use Python 3.13. Try it first. If it fails, either (a) pin numpy<2.0 in the container, (b) patch the audfprint source, or (c) use Python 3.11 in the container (deviating from D-02 slightly for practical reasons). Test this in Wave 0.

2. **audfprint Database Size at 200K Files**
   - What we know: `.pklz` is a serialized pickle with hash tables. Configurable hashbits (default 20), bucketsize (default 100), maxtime (default 16384).
   - What's unclear: How large the database gets at 200K files and whether pickle serialization performance degrades.
   - Recommendation: Start with defaults. If the database exceeds ~1GB or load times become problematic, increase hashbits or split into multiple databases.

3. **Panako LMDB Configuration for 200K Files**
   - What we know: Panako uses LMDB with default configuration. `panako config` can show/set options.
   - What's unclear: Default LMDB map size and whether it's sufficient for 200K files.
   - Recommendation: Check `panako config` output after build. Set map size to at least 10GB if configurable.

4. **Panako CLI Output Parsing**
   - What we know: Query output is semicolon-separated with fields: index, total, query path, query start, query end, match path, match ID, match start, match end, score, time factor, freq factor, match percentage.
   - What's unclear: Exact field positions and edge cases (no match, multiple matches, errors).
   - Recommendation: Build the parser with defensive handling. Test with known audio pairs. Log raw output for debugging.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Docker Compose | Container orchestration | Assumed (project constraint) | 2.x | -- |
| PostgreSQL | Main app database | Running in Docker | 18-alpine | -- |
| Redis | Task queue broker | Running in Docker | 8-alpine | -- |
| ffmpeg | Audio decoding (both containers) | Installed in Docker images | -- | -- |
| Java JRE 21 | Panako runtime | Installed in Panako container | -- | -- |

**Missing dependencies with no fallback:** None -- all dependencies are containerized.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest --cov=phaze --cov-report=term-missing` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| FPRINT-01a | FingerprintEngine Protocol definition and adapters | unit | `uv run pytest tests/test_services/test_fingerprint.py -x` | Wave 0 |
| FPRINT-01b | Audfprint container /ingest, /query, /health endpoints | integration (container) | Manual -- requires running container | Wave 0 |
| FPRINT-01c | Panako container /ingest, /query, /health endpoints | integration (container) | Manual -- requires running container | Wave 0 |
| FPRINT-01d | FingerprintOrchestrator weighted scoring | unit | `uv run pytest tests/test_services/test_fingerprint.py::test_weighted_scoring -x` | Wave 0 |
| FPRINT-01e | Single-engine match cap at 70% | unit | `uv run pytest tests/test_services/test_fingerprint.py::test_single_engine_cap -x` | Wave 0 |
| FPRINT-02a | fingerprint_file arq task function | unit | `uv run pytest tests/test_tasks/test_fingerprint.py -x` | Wave 0 |
| FPRINT-02b | FingerprintResult model and per-engine storage | unit | `uv run pytest tests/test_models/test_fingerprint.py -x` | Wave 0 |
| FPRINT-02c | State transition to FINGERPRINTED after both engines | unit | `uv run pytest tests/test_tasks/test_fingerprint.py::test_state_transition -x` | Wave 0 |
| FPRINT-02d | Trigger endpoint enqueues fingerprint jobs | unit | `uv run pytest tests/test_routers/test_pipeline_fingerprint.py -x` | Wave 0 |
| FPRINT-02e | Progress tracking (total/completed/failed counts) | unit | `uv run pytest tests/test_services/test_fingerprint.py::test_progress -x` | Wave 0 |
| FPRINT-02f | Failed files marked with error, not transitioned | unit | `uv run pytest tests/test_tasks/test_fingerprint.py::test_partial_failure -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest --cov=phaze --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_fingerprint.py` -- covers FPRINT-01a, 01d, 01e, 02e
- [ ] `tests/test_tasks/test_fingerprint.py` -- covers FPRINT-02a, 02c, 02f
- [ ] `tests/test_models/test_fingerprint.py` -- covers FPRINT-02b
- [ ] `tests/test_routers/test_pipeline_fingerprint.py` -- covers FPRINT-02d
- [ ] Alembic migration for `fingerprint_results` table

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively** -- main app and audfprint container target 3.13 (D-02). Panako container uses 3.13-slim for the FastAPI wrapper.
- **uv only** -- never bare pip/python/pytest/mypy. All containers that use Python should use uv.
- **Pre-commit hooks must pass** before commits.
- **85% minimum code coverage** -- new service/task/model code needs tests.
- **Docker Compose deployment** -- new services added to `docker-compose.yml`.
- **Every feature gets its own git worktree + PR** -- phase 16 work goes on its own branch.
- **Type hints on all functions** -- includes container FastAPI apps.
- **150-char line length, double quotes, ruff formatting.**
- **Justfile must be updated** with new service-related commands.
- **README per service** -- each container directory needs a README.

## Sources

### Primary (HIGH confidence)
- [dpwe/audfprint GitHub](https://github.com/dpwe/audfprint) -- repository structure, CLI usage, requirements.txt, database format (.pklz)
- [JorenSix/Panako GitHub](https://github.com/JorenSix/Panako) -- CLI usage, LMDB storage, build instructions, query output format
- Existing codebase: `docker-compose.yml`, `Dockerfile`, `src/phaze/tasks/metadata_extraction.py`, `src/phaze/services/pipeline.py`, `src/phaze/routers/pipeline.py` -- established patterns for Docker services, arq tasks, pipeline triggers

### Secondary (MEDIUM confidence)
- [Panako README.textile](https://github.com/JorenSix/Panako/blob/master/README.textile) -- detailed CLI output format examples, configuration options
- [audfprint match module](https://github.com/dpwe/audfprint/blob/master/audfprint_match.py) -- scoring/ranking system, match output structure

### Tertiary (LOW confidence)
- audfprint Python 3.13 compatibility -- NOT verified, based on code inspection only. Needs validation.
- Panako LMDB map size defaults -- NOT verified, inferred from LMDB general behavior. Needs validation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all tools verified, Docker isolation mitigates compatibility risks
- Architecture: HIGH -- patterns follow established codebase conventions (pipeline.py, metadata_extraction.py)
- Pitfalls: MEDIUM -- audfprint compatibility and database scaling are theoretical risks, not verified
- Container integration: MEDIUM -- subprocess wrapper pattern is straightforward but output parsing needs real testing

**Research date:** 2026-04-01
**Valid until:** 2026-05-01 (stable tools, low churn)
