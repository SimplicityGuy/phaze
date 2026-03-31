# Architecture Patterns

**Domain:** Music file management -- v2.0 integration of tracklists, audio fingerprinting, and metadata enrichment
**Researched:** 2026-03-30
**Focus:** How new v2.0 features integrate with existing FastAPI/arq/Postgres architecture

## Existing Architecture (v1.0 Baseline)

Before defining new components, here is what already exists and must not be disrupted:

```
Containers:  api (FastAPI), worker (arq), postgres (PG18), redis (Redis 8)
Models:      FileRecord, FileMetadata, AnalysisResult, RenameProposal, ExecutionLog, FileCompanion, ScanBatch
State machine: DISCOVERED -> METADATA_EXTRACTED -> FINGERPRINTED -> ANALYZED -> PROPOSAL_GENERATED -> APPROVED/REJECTED -> EXECUTED/FAILED
Task functions: process_file, generate_proposals, execute_approved_batch
Volumes:     /data/music (ro), /data/output (rw), /models (ro)
```

Key architectural properties that v2.0 must preserve:
- Single Python codebase, two runtime modes (API + worker)
- arq tasks are thin wrappers calling services
- All CPU-bound work runs in ProcessPoolExecutor via `run_in_process_pool`
- Per-task session creation (`get_task_session()`) -- no shared engine in workers
- Fan-out/fan-in batch processing pattern
- State machine transitions guard idempotent re-runs

## v2.0 System Diagram

```
+-----------------------------------------------------------------------------+
|                      Admin Web UI (HTMX + Jinja2)                           |
|  +--------------+  +--------------+  +---------------+  +--------------+    |
|  | Approval UI  |  | Dedup Review |  | Tracklist     |  |  Dashboard   |    |
|  |  (existing)  |  |   (NEW)      |  | Browser (NEW) |  |  (existing)  |    |
|  +------+-------+  +------+-------+  +-------+-------+  +------+-------+    |
+---------+-----------------+-------------------+-----------------+-----------+
|                      API Layer (FastAPI)                                     |
|  +----------+  +----------+  +----------+  +----------+  +------------+     |
|  | Existing |  | Metadata |  |Tracklist |  |Fingerprint|  |  Dedup     |     |
|  | Routers  |  | Router   |  | Router   |  | Router    |  |  Router    |     |
|  |          |  |  (NEW)   |  |  (NEW)   |  |  (NEW)    |  |  (NEW)     |     |
|  +----+-----+  +----+-----+  +----+-----+  +----+------+  +----+------+     |
+-------+--------------+-------------+---------------+--------------+--------+
|                    Service Layer                                            |
|  +------------+  +------------+  +------------+  +------------------+       |
|  | Metadata   |  | Tracklist  |  |Fingerprint |  | Dedup Resolution |       |
|  | Extract    |  | Scraper    |  | Client     |  | Service          |       |
|  | (NEW)      |  | (NEW)      |  | (NEW)      |  | (NEW)            |       |
|  +-----+------+  +-----+------+  +-----+------+  +-----+------------+       |
+--------+--------------+---------------+--------------+-------------------+
|                    Task Queue (arq + Redis)                                |
|  +--------------------------------------------------------------------+    |
|  |  NEW tasks: extract_metadata | scrape_tracklist | submit_fingerprint|    |
|  |             | refresh_tracklists | resolve_duplicates               |    |
|  |  arq cron:  periodic_tracklist_refresh (monthly + jitter)          |    |
|  +--------------------------------------------------------------------+    |
+------------------------------------------------------------------------+
|                    Data Layer                                              |
|  +--------------+  +--------------+  +--------------+                     |
|  | PostgreSQL   |  |    Redis     |  |  Filesystem   |                     |
|  | + NEW tables |  | (task queue) |  | (music files) |                     |
|  +--------------+  +--------------+  +--------------+                     |
+-------------+----------------------------------+---------------------------+
              |                                  |
     +--------+--------+              +----------+-----------+
     | 1001tracklists   |              | Fingerprint Service  |
     | (HTTP scraping)  |              | (NEW container)      |
     +-----------------+              | audfprint + Panako   |
                                      | FastAPI thin wrapper  |
                                      | LMDB + fprint volumes |
                                      +----------------------+
```

## New Container: Fingerprint Service

The fingerprint service is the only new container in v2.0. It wraps audfprint (Python, landmark-based) and Panako (Java, tempo-robust) behind a unified FastAPI API.

### Why a Separate Container

1. **Panako requires JDK 17** -- cannot coexist in the Python 3.13 image
2. **Long-running databases** -- both audfprint and Panako (LMDB) maintain persistent index files that must survive container restarts
3. **LMDB single-writer constraint** -- Panako's LMDB does not allow concurrent writes; a dedicated service serializes access
4. **Resource isolation** -- fingerprinting is CPU-intensive and should not starve the API or arq workers
5. **Independent scaling** -- fingerprint ingestion is a one-time bulk operation; once complete, the service switches to query-only mode

### Fingerprint Service Architecture

```
+-------------------------------------------------+
|          fingerprint-service container           |
|                                                  |
|  +--------------------------------------------+ |
|  |  FastAPI thin wrapper (:8001)              | |
|  |  POST /ingest   -- add file to both DBs    | |
|  |  POST /query    -- match audio against DBs  | |
|  |  POST /compare  -- compare two files        | |
|  |  GET  /stats    -- DB sizes and health      | |
|  |  GET  /health   -- readiness probe          | |
|  +----------+----------------+-----------------+ |
|             |                |                   |
|  +----------v----+  +-------v----------------+  |
|  |  audfprint    |  |  Panako (subprocess)   |  |
|  |  (Python lib) |  |  JDK 17 + panako.jar   |  |
|  |  hash DB      |  |  LMDB at /data/panako  |  |
|  +---------------+  +------------------------+  |
|                                                  |
|  Volumes:                                        |
|    /data/audfprint  -- fingerprint database      |
|    /data/panako     -- LMDB database directory   |
|    /data/music      -- shared audio volume (ro)  |
+-------------------------------------------------+
```

### Fingerprint Service API Contract

```python
# POST /ingest
# Body: {"file_path": "/data/music/path/to/file.mp3", "file_id": "uuid"}
# Response: {"status": "ok", "audfprint_landmarks": 1423, "panako_fingerprints": 892}

# POST /query
# Body: {"file_path": "/data/music/path/to/liveset.mp3", "max_results": 20, "min_confidence": 0.3}
# Response: {"matches": [
#   {"file_id": "uuid", "file_path": "...", "engine": "audfprint", "score": 0.87,
#    "offset_seconds": 142.5, "duration_seconds": 245.0},
#   {"file_id": "uuid", "file_path": "...", "engine": "panako", "score": 0.72,
#    "offset_seconds": 143.1, "duration_seconds": 244.8, "tempo_ratio": 1.03}
# ]}

# POST /compare
# Body: {"file_a": "/data/music/a.mp3", "file_b": "/data/music/b.mp3"}
# Response: {"audfprint_match": true, "panako_match": true, "combined_score": 0.92}
```

### Hybrid Scoring

The service combines results from both engines because they have complementary strengths:

| Property | audfprint | Panako |
|----------|-----------|--------|
| Language | Python (importable) | Java (subprocess) |
| Algorithm | Landmark-based spectral peaks | Constant-Q spectral with event points |
| Speed change tolerance | Up to ~5% | Up to ~10% |
| Best for | Exact/near-exact matches | DJ sets with tempo adjustment |
| Database format | Compressed hash table (single file) | LMDB (memory-mapped, directory) |
| Python 3.13 | Needs verification -- depends on numpy/scipy (both support 3.13) | N/A (Java, subprocess call) |

Combined scoring formula (in the fingerprint service):

```python
def combined_score(audfprint_score: float | None, panako_score: float | None) -> float:
    """Weighted combination. Panako weighted higher for tempo-shifted content."""
    if audfprint_score is not None and panako_score is not None:
        return 0.4 * audfprint_score + 0.6 * panako_score
    return audfprint_score or panako_score or 0.0
```

Panako gets higher weight because the primary use case (identifying tracks within DJ live sets) inherently involves tempo adjustment.

### Fingerprint Service Dockerfile

```dockerfile
FROM python:3.13-slim AS base

# Install JDK for Panako, ffmpeg for both
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install audfprint and FastAPI wrapper
COPY fingerprint-service/requirements.txt .
RUN pip install -r requirements.txt

# Install Panako jar
COPY fingerprint-service/panako/ /opt/panako/

# Copy service code
COPY fingerprint-service/src/ /app/
WORKDIR /app

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
```

### Docker Compose Addition

```yaml
  fingerprint:
    build:
      context: .
      dockerfile: fingerprint-service/Dockerfile
    ports:
      - "${FINGERPRINT_PORT:-8001}:8001"
    env_file: .env
    volumes:
      - "${SCAN_PATH:-/data/music}:/data/music:ro"
      - fingerprint_audfprint:/data/audfprint
      - fingerprint_panako:/data/panako
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  pgdata:
  fingerprint_audfprint:
  fingerprint_panako:
```

The fingerprint container does NOT depend on postgres or redis. It is stateless from the perspective of the main app -- it just stores/queries its own fingerprint databases.

## New and Modified SQLAlchemy Models

### New Model: Tracklist

Stores scraped tracklist data from 1001tracklists.

```python
class Tracklist(TimestampMixin, Base):
    """A tracklist scraped from 1001tracklists.com."""

    __tablename__ = "tracklists"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    source_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # 1001tl internal ID
    title: Mapped[str] = mapped_column(Text, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str | None] = mapped_column(Text, nullable=True)  # e.g., "Coachella 2024"
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    track_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # full scraped payload
    last_scraped_at: Mapped[datetime] = mapped_column(server_default=func.now())
    next_refresh_after: Mapped[datetime | None] = mapped_column(nullable=True)  # for periodic refresh

    __table_args__ = (
        Index("ix_tracklists_source_id", "source_id"),
        Index("ix_tracklists_artist", "artist"),
        Index("ix_tracklists_next_refresh", "next_refresh_after"),
    )
```

### New Model: TracklistEntry

Individual tracks within a tracklist.

```python
class TracklistEntry(TimestampMixin, Base):
    """A single track entry within a tracklist."""

    __tablename__ = "tracklist_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tracklist_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklists.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # order in tracklist
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "HH:MM:SS" from 1001tl
    source_track_id: Mapped[str | None] = mapped_column(String(50), nullable=True)  # 1001tl track ID

    __table_args__ = (
        Index("ix_tracklist_entries_tracklist_id", "tracklist_id"),
        Index("ix_tracklist_entries_artist_title", "artist", "title"),
    )
```

### New Model: FingerprintMatch

Records fingerprint matches between files (e.g., a live set and its constituent tracks).

```python
class FingerprintMatch(TimestampMixin, Base):
    """A fingerprint match between two files or between a file and a tracklist entry."""

    __tablename__ = "fingerprint_matches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)  # the live set
    matched_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)  # matched track (if in library)
    tracklist_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklist_entries.id"), nullable=True)
    engine: Mapped[str] = mapped_column(String(20), nullable=False)  # "audfprint", "panako", "combined"
    score: Mapped[float] = mapped_column(Float, nullable=False)
    offset_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)  # where in the source file
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    tempo_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)  # 1.0 = same speed
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/confirmed/rejected

    __table_args__ = (
        Index("ix_fingerprint_matches_source", "source_file_id"),
        Index("ix_fingerprint_matches_matched", "matched_file_id"),
    )
```

### New Model: TracklistFileLink

Links a tracklist to a file (e.g., "this live set recording corresponds to this 1001tl tracklist").

```python
class TracklistFileLink(TimestampMixin, Base):
    """Links a FileRecord (live set) to a Tracklist from 1001tracklists."""

    __tablename__ = "tracklist_file_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    tracklist_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklists.id"), nullable=False)
    match_method: Mapped[str] = mapped_column(String(30), nullable=False)  # "fuzzy_metadata", "fingerprint", "manual"
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed/confirmed/rejected

    __table_args__ = (
        UniqueConstraint("file_id", "tracklist_id", name="uq_tracklist_file_links_pair"),
    )
```

### Modified Model: FileMetadata

The existing FileMetadata model is already scaffolded and sufficient. The mutagen extraction service populates it. Two columns should be added via migration:

```python
# Add to FileMetadata
duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
track_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

### Modified Model: AnalysisResult

The existing `fingerprint` column (Text) is sufficient for storing a reference. The `features` JSONB can store fingerprint metadata. No schema change required.

### Model Summary

| Model | Status | Table | Purpose |
|-------|--------|-------|---------|
| FileRecord | EXISTING | `files` | No changes needed |
| FileMetadata | MODIFY | `metadata` | Add `duration_seconds`, `track_number` |
| AnalysisResult | EXISTING | `analysis` | No changes needed |
| RenameProposal | EXISTING | `proposals` | No changes needed |
| ExecutionLog | EXISTING | `execution_log` | No changes needed |
| FileCompanion | EXISTING | `file_companions` | No changes needed |
| ScanBatch | EXISTING | `scan_batches` | No changes needed |
| Tracklist | **NEW** | `tracklists` | 1001tracklists scraped data |
| TracklistEntry | **NEW** | `tracklist_entries` | Individual tracks in a tracklist |
| FingerprintMatch | **NEW** | `fingerprint_matches` | Fingerprint match results |
| TracklistFileLink | **NEW** | `tracklist_file_links` | Links files to tracklists |

## Data Flow: New Features

### Flow 1: Audio Tag Extraction (mutagen)

```
FileRecord (state=DISCOVERED)
  |
  v  arq task: extract_metadata
  |  Service: MetadataExtractService.extract(file_path)
  |  Uses: mutagen to read ID3/Vorbis/MP4 tags
  |  Writes: FileMetadata row (artist, title, album, year, genre, duration, track_number, raw_tags)
  |  Transitions: DISCOVERED -> METADATA_EXTRACTED
  |
  v  existing arq task: process_file (essentia analysis)
     Transitions: METADATA_EXTRACTED -> ANALYZED
```

This inserts a new step at the front of the pipeline. The state machine already has `METADATA_EXTRACTED` defined in `FileState`. The existing `process_file` task needs modification to check for `METADATA_EXTRACTED` state instead of `DISCOVERED`.

### Flow 2: Audio Fingerprint Ingestion

```
FileRecord (state=METADATA_EXTRACTED or ANALYZED)
  |
  v  arq task: submit_fingerprint
  |  Service: FingerprintClient.ingest(file_path, file_id)
  |  HTTP call: POST fingerprint-service:8001/ingest
  |  The fingerprint service stores in audfprint + Panako LMDB
  |  No data stored in Postgres (fingerprint DBs are in the service)
  |  Transitions: -> FINGERPRINTED (or just records success in features JSONB)
  |
  v  Note: Fingerprinting can run in parallel with analysis.
     Both METADATA_EXTRACTED -> {FINGERPRINTED, ANALYZED} paths feed into proposals.
```

**Important design decision:** Fingerprint ingestion does NOT need to block the pipeline. A file can proceed to ANALYZED and even PROPOSAL_GENERATED without being fingerprinted. Fingerprinting is additive enrichment, not a prerequisite for naming. The `FINGERPRINTED` state in the state machine is available but optional -- track fingerprint status in `AnalysisResult.features` JSONB instead, to avoid blocking the critical path.

### Flow 3: 1001Tracklists Scraping

```
User/System triggers tracklist search
  |
  v  arq task: scrape_tracklist
  |  Service: TracklistScraper.search(artist, event, date)
  |  HTTP: POST to 1001tracklists.com search endpoint (with fake-useragent, rate limiting)
  |  Parses: BeautifulSoup HTML parsing
  |  Writes: Tracklist + TracklistEntry rows
  |  Sets: next_refresh_after = now + 30 days + random(0-7 days)
  |
  v  Service: TracklistMatcher.match_to_files(tracklist_id)
  |  Fuzzy matches tracklist entries against FileMetadata (artist + title)
  |  Writes: TracklistFileLink rows (match_method="fuzzy_metadata")
  |
  v  Optional: Fingerprint-based matching
     For live set files linked to a tracklist, run fingerprint queries
     against known tracks to confirm/discover track boundaries
```

### Flow 4: Periodic Tracklist Refresh

```
arq cron job: periodic_tracklist_refresh (runs daily)
  |
  v  Query: SELECT * FROM tracklists WHERE next_refresh_after < now() LIMIT 50
  |
  v  For each stale tracklist:
  |    Re-scrape from 1001tracklists.com
  |    Update TracklistEntry rows (upsert by position)
  |    Set next_refresh_after = now + 30 days + random(0-7 days)
  |
  v  Rate limit: Max 50 scrapes per cron run, 2-5 second delay between requests
     Jitter prevents thundering herd on refresh dates
```

The randomized jitter is critical: if 10,000 tracklists are scraped in the first week, without jitter they would all try to refresh on the same day 30 days later.

### Flow 5: Fingerprint-Based Track Identification in Live Sets

```
User selects a live set file in admin UI
  |
  v  API: POST /fingerprint/query
  |  Service: FingerprintClient.query(file_path)
  |  HTTP: POST fingerprint-service:8001/query
  |  Returns: list of matched tracks with time offsets
  |
  v  Service: FingerprintMatchService.store_matches(source_file_id, matches)
  |  Writes: FingerprintMatch rows
  |  Cross-references with TracklistEntry if a linked tracklist exists
  |
  v  UI: Shows proposed tracklist for the live set
     User can confirm/reject individual track identifications
```

### Flow 6: Duplicate Resolution

```
Admin UI: Dedup Review page
  |
  v  API: GET /dedup/groups
  |  Service: DedupService.get_duplicate_groups()
  |  Query: GROUP BY sha256_hash HAVING COUNT(*) > 1
  |  Also: FingerprintMatch-based acoustic duplicates (different files, same audio)
  |
  v  UI: Shows duplicate groups with metadata comparison
  |  User picks "keep" file for each group
  |
  v  API: POST /dedup/resolve
  |  Service: DedupService.resolve(keep_file_id, remove_file_ids)
  |  Marks removed files as REJECTED
  |  Does NOT delete files -- just marks state. Deletion happens in execution phase.
```

## New arq Task Functions

```python
# In tasks/metadata.py
async def extract_metadata(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Extract audio tags with mutagen and populate FileMetadata."""

# In tasks/fingerprint.py
async def submit_fingerprint(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Submit file to fingerprint service for ingestion."""

async def query_fingerprint(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Query fingerprint service to find matches for a file."""

# In tasks/tracklist.py
async def scrape_tracklist(ctx: dict[str, Any], search_params: dict[str, str]) -> dict[str, Any]:
    """Scrape a tracklist from 1001tracklists.com."""

async def refresh_tracklists(ctx: dict[str, Any]) -> dict[str, Any]:
    """Refresh stale tracklists (cron job)."""

async def match_tracklist_to_files(ctx: dict[str, Any], tracklist_id: str) -> dict[str, Any]:
    """Fuzzy-match tracklist entries to files in the library."""
```

Worker settings update:

```python
class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        # Existing
        process_file, generate_proposals, execute_approved_batch,
        # New v2.0
        extract_metadata, submit_fingerprint, query_fingerprint,
        scrape_tracklist, refresh_tracklists, match_tracklist_to_files,
    ]
    # Add cron job for periodic refresh
    cron_jobs: ClassVar[list[Any]] = [
        cron(refresh_tracklists, hour=3, minute=0),  # Run at 3 AM daily
    ]
```

## New Service Classes

| Service | Responsibility | Dependencies |
|---------|---------------|-------------|
| `MetadataExtractService` | Read audio tags with mutagen, normalize, write FileMetadata | mutagen, AsyncSession |
| `TracklistScraper` | HTTP scraping of 1001tracklists.com, rate limiting, parsing | httpx, beautifulsoup4, fake-useragent |
| `TracklistMatcher` | Fuzzy matching of tracklist entries to FileMetadata | AsyncSession, rapidfuzz (for fuzzy string matching) |
| `FingerprintClient` | HTTP client for the fingerprint service | httpx |
| `FingerprintMatchService` | Store/query fingerprint match results | AsyncSession |
| `DedupResolutionService` | Resolve duplicate groups (SHA256 + acoustic) | AsyncSession |

## New Router Endpoints

| Router | Endpoints | Purpose |
|--------|-----------|---------|
| `routers/metadata.py` | `POST /metadata/extract/{file_id}`, `POST /metadata/extract-batch` | Trigger metadata extraction |
| `routers/tracklist.py` | `GET /tracklists`, `POST /tracklists/search`, `GET /tracklists/{id}`, `POST /tracklists/{id}/refresh`, `POST /tracklists/{id}/link/{file_id}` | Tracklist CRUD and linking |
| `routers/fingerprint.py` | `POST /fingerprint/ingest/{file_id}`, `POST /fingerprint/query/{file_id}`, `GET /fingerprint/stats` | Fingerprint operations (proxied to service) |
| `routers/dedup.py` | `GET /dedup/groups`, `POST /dedup/resolve`, `GET /dedup/groups/{hash}` | Duplicate resolution workflow |

## Component Boundaries

### What Stays in the Main Codebase (phaze)

- All PostgreSQL models and migrations
- All business logic (services)
- All API endpoints and UI templates
- All arq task definitions
- HTTP client for fingerprint service (NOT the fingerprint logic itself)

### What Lives in the Fingerprint Service

- audfprint library integration (Python, imported directly)
- Panako integration (Java, subprocess calls to panako.jar)
- Fingerprint database files (audfprint hash DB + LMDB)
- Thin FastAPI wrapper exposing ingest/query/compare/stats/health
- Its own Dockerfile, requirements.txt, and health check

The fingerprint service is intentionally dumb. It knows nothing about Phaze's domain model, state machine, or PostgreSQL. It accepts file paths, returns match results. All intelligence lives in the main codebase.

## Configuration Additions

```python
class Settings(BaseSettings):
    # ... existing settings ...

    # Fingerprint service
    fingerprint_service_url: str = "http://fingerprint:8001"
    fingerprint_ingest_timeout: int = 120  # seconds per file
    fingerprint_query_timeout: int = 60

    # 1001tracklists scraping
    tracklist_scrape_delay: float = 3.0  # seconds between requests
    tracklist_refresh_days: int = 30  # minimum days between refreshes
    tracklist_refresh_jitter_days: int = 7  # random jitter added to refresh interval
    tracklist_max_refresh_per_run: int = 50  # max tracklists to refresh per cron run
```

## Migration Strategy

v2.0 requires one Alembic migration adding four tables and two columns:

```python
# alembic/versions/005_v2_tracklists_and_fingerprints.py
def upgrade():
    # New tables
    op.create_table("tracklists", ...)
    op.create_table("tracklist_entries", ...)
    op.create_table("fingerprint_matches", ...)
    op.create_table("tracklist_file_links", ...)

    # Modify existing
    op.add_column("metadata", sa.Column("duration_seconds", sa.Float, nullable=True))
    op.add_column("metadata", sa.Column("track_number", sa.Integer, nullable=True))
```

## Anti-Patterns to Avoid

### Anti-Pattern 1: Embedding Panako in the Python Image

**What:** Trying to run Panako via Py4J or JNI bridge inside the main container.
**Why bad:** JDK 17 bloats the image. JNI bridges are fragile. LMDB concurrent access from multiple workers will corrupt the database.
**Instead:** Separate container with HTTP API. Single writer process.

### Anti-Pattern 2: Scraping 1001Tracklists Without Rate Limiting

**What:** Blasting requests to 1001tracklists.com in parallel.
**Why bad:** IP ban within minutes. The site has anti-scraping measures.
**Instead:** Sequential requests with 2-5 second delays. fake-useragent rotation. Respect rate limits. Cache aggressively.

### Anti-Pattern 3: Blocking Pipeline on Fingerprinting

**What:** Requiring fingerprint ingestion to complete before a file can proceed to ANALYZED state.
**Why bad:** Fingerprint service may be slow (bulk ingestion of 200K files). Pipeline stalls.
**Instead:** Fingerprinting runs as a parallel enrichment path. Files proceed through analysis and proposals without waiting for fingerprints. Fingerprint data enhances dedup and tracklist matching but is not on the critical path.

### Anti-Pattern 4: Storing Fingerprint Databases in PostgreSQL

**What:** Putting audfprint landmarks or Panako hashes in Postgres.
**Why bad:** Fingerprint databases are optimized for their specific access patterns (hash table lookup, LMDB B-tree). PostgreSQL would be slower and more complex for these workloads.
**Instead:** Let each engine manage its own database. Store only match results in PostgreSQL.

### Anti-Pattern 5: Writing a Custom 1001Tracklists Scraper from Scratch

**What:** Building a full scraper with session management, CSRF handling, etc.
**Why bad:** 1001tracklists has documented POST endpoints that work with simple HTTP requests + fake-useragent. Over-engineering the scraper wastes time.
**Instead:** Use httpx + BeautifulSoup + fake-useragent. Keep it simple. The existing community scrapers confirm this approach works.

## Build Order (Dependency-Aware)

This order respects both data dependencies and the existing v1.0 pipeline:

```
Phase 1: Audio Tag Extraction (mutagen -> FileMetadata)
  Depends on: v1.0 FileMetadata model (exists, unpopulated)
  Unlocks: Richer LLM context for proposals, fuzzy matching for tracklists

Phase 2: AI Destination Path Proposals
  Depends on: Phase 1 (metadata enriches LLM context)
  Unlocks: Complete file organization (proposed_path already wired in v1.0)

Phase 3: Duplicate Resolution UI
  Depends on: Phase 1 (metadata for comparing duplicates)
  Unlocks: Clean library before fingerprinting

Phase 4: 1001Tracklists Scraping + Storage
  Depends on: Phase 1 (metadata for fuzzy matching tracklists to files)
  Unlocks: Tracklist data for fingerprint cross-referencing

Phase 5: Periodic Tracklist Refresh
  Depends on: Phase 4 (tracklists must exist before refreshing)
  Unlocks: Kept-current tracklist data

Phase 6: Fingerprint Service Container
  Depends on: Phase 3 (cleaner library = better fingerprint DB)
  Unlocks: Track identification in live sets, acoustic dedup

Phase 7: Fingerprint Integration (ingest + query + match UI)
  Depends on: Phase 4 (tracklist data for cross-referencing matches), Phase 6 (service running)
  Unlocks: Full tracklist identification workflow
```

## Sources

- [audfprint on GitHub](https://github.com/dpwe/audfprint) -- landmark-based fingerprinting, Python, compressed hash database
- [Panako on GitHub](https://github.com/JorenSix/Panako) -- tempo-robust fingerprinting, Java, LMDB storage
- [Panako documentation](https://0110.be/releases/Panako/Panako-latest/readme.html) -- CLI commands, LMDB details
- [Panako paper](https://github.com/JorenSix/Panako/blob/master/paper.md) -- algorithm comparison, tempo robustness up to 10%
- [1001-tracklists-api](https://github.com/leandertolksdorf/1001-tracklists-api) -- BeautifulSoup-based scraping pattern
- [1001tracklists-scraper](https://github.com/GodLesZ/1001tracklists-scraper) -- JavaScript scraper, confirms POST endpoints work
- [Docker Panako](https://github.com/Pixelartist/docker-panako) -- community Docker wrapper for Panako
- [Python 3.13 readiness](https://pyreadiness.org/3.13/) -- NumPy 2.2+ and SciPy support Python 3.13

---
*Architecture patterns for: Phaze v2.0 -- tracklist integration, fingerprinting, and metadata enrichment*
*Researched: 2026-03-30*
