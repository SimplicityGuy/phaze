# Architecture Patterns

**Domain:** Music file management, batch processing, and AI-assisted organization
**Researched:** 2026-03-27

## Recommended Architecture

**Pattern:** Async monolith with background workers, deployed as multiple Docker Compose services sharing a PostgreSQL database.

This is NOT a microservices architecture. A single Python codebase produces two runtime modes: the API server (FastAPI + HTMX UI) and the worker process (arq consuming jobs from Redis). They share models, schemas, and business logic.

### System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Admin Web UI (HTMX + Jinja2)                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ Approval UI  │  │ Batch Status │  │  Dashboard   │               │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
├─────────┴──────────────────┴──────────────────┴─────────────────────┤
│                      API Layer (FastAPI)                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ Ingest   │  │ Approval │  │ Analysis │  │ Files    │             │
│  │ Router   │  │ Router   │  │ Router   │  │ Router   │             │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
├───────┴──────────────┴──────────────┴──────────────┴────────────────┤
│                    Service Layer                                      │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐     │
│  │ Ingestion  │  │  Analysis  │  │ AI Naming  │  │ File Ops   │     │
│  │ Service    │  │  Service   │  │ Service    │  │ Service    │     │
│  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘     │
├────────┴───────────────┴───────────────┴───────────────┴────────────┤
│                    Task Queue (arq + Redis)                           │
│  ┌────────────────────────────────────────────────────────────┐      │
│  │  Workers: ingest | analyze | fingerprint | propose | move  │      │
│  └────────────────────────────────────────────────────────────┘      │
├─────────────────────────────────────────────────────────────────────┤
│                    Data Layer                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ PostgreSQL   │  │    Redis     │  │  Filesystem   │               │
│  │ (metadata)   │  │ (task queue) │  │ (music files) │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
└─────────────────────────────────────────────────────────────────────┘
         |                                      |
    [LLM API]                            [chromaprint/fpcalc]
  (OpenAI/Anthropic/local)
```

### Component Boundaries

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| **API Server** (FastAPI) | HTTP endpoints, admin UI rendering, job submission, SSE for progress | PostgreSQL (read/write), Redis (enqueue jobs) |
| **Worker Process** (arq) | Audio analysis, metadata extraction, fingerprinting, LLM calls, file operations | PostgreSQL (read/write), Redis (dequeue jobs), filesystem (read audio), LLM API (HTTP) |
| **PostgreSQL** | All persistent state: files, metadata, analysis results, proposals, approval status, execution log | API Server, Worker |
| **Redis** | Job queue broker, job results, optional caching | API Server (enqueue), Worker (dequeue) |
| **Filesystem** (Docker volume) | Source music files (read-only until approved move), organized output directory | Worker (read for analysis), Worker (write for approved moves) |

## Recommended Project Structure

```
phaze/
├── src/
│   └── phaze/
│       ├── __init__.py
│       ├── main.py                 # FastAPI app factory
│       ├── config.py               # Settings via pydantic-settings
│       ├── database.py             # Async SQLAlchemy engine + session factory
│       │
│       ├── models/                 # SQLAlchemy ORM models
│       │   ├── __init__.py
│       │   ├── base.py             # DeclarativeBase with naming conventions
│       │   ├── file.py             # File record (path, hash, state)
│       │   ├── metadata.py         # Extracted tag metadata
│       │   ├── analysis.py         # BPM, key, mood, fingerprint
│       │   ├── proposal.py         # Rename/move proposals
│       │   └── execution.py        # Execution log (append-only)
│       │
│       ├── schemas/                # Pydantic request/response models
│       │   ├── __init__.py
│       │   ├── file.py
│       │   ├── proposal.py
│       │   └── batch.py
│       │
│       ├── repositories/           # Data access layer
│       │   ├── __init__.py
│       │   ├── file_repo.py
│       │   ├── proposal_repo.py
│       │   └── analysis_repo.py
│       │
│       ├── services/               # Business logic
│       │   ├── __init__.py
│       │   ├── ingestion.py        # Directory scanning, hashing, registration
│       │   ├── analysis.py         # Wrapper around existing analysis prototypes
│       │   ├── fingerprint.py      # Chromaprint/AcoustID integration
│       │   ├── naming.py           # AI rename/path proposal generation
│       │   ├── dedup.py            # SHA-256 + acoustic dedup
│       │   └── fileops.py          # Safe rename/move with audit trail
│       │
│       ├── routers/                # FastAPI route handlers
│       │   ├── __init__.py
│       │   ├── ingest.py
│       │   ├── analysis.py
│       │   ├── proposals.py
│       │   ├── files.py
│       │   └── ui.py               # HTML template routes for admin UI
│       │
│       ├── workers/                # arq task definitions
│       │   ├── __init__.py
│       │   ├── settings.py         # arq WorkerSettings
│       │   ├── ingest_tasks.py
│       │   ├── analysis_tasks.py
│       │   ├── naming_tasks.py
│       │   └── fileops_tasks.py
│       │
│       └── templates/              # Jinja2 + HTMX templates
│           ├── base.html           # Layout with Tailwind CSS + HTMX + Alpine.js
│           ├── dashboard.html
│           ├── proposals/
│           │   ├── list.html       # Paginated proposal list
│           │   ├── detail.html     # Single proposal review
│           │   └── partials/       # HTMX partial fragments
│           └── batches/
│               ├── status.html
│               └── partials/
│
├── tests/
│   ├── conftest.py                 # Fixtures: async DB, test client, factories
│   ├── test_services/
│   ├── test_routers/
│   └── test_workers/
│
├── alembic/                        # Database migrations (async template)
│   ├── env.py
│   └── versions/
│
├── docker-compose.yml              # API, worker, PostgreSQL, Redis
├── Dockerfile
├── pyproject.toml
└── alembic.ini
```

### Structure Rationale

- **`models/` separate from `schemas/`:** SQLAlchemy models (DB shape) and Pydantic schemas (API shape) serve different purposes. Decouples DB structure from API contracts.
- **`repositories/` for data access:** Isolates SQL queries from business logic. Makes services testable by mocking repositories.
- **`services/` for business logic:** Routers stay thin (parse request, call service, return response). Services contain all logic and are independently testable without HTTP.
- **`workers/` for task definitions:** arq tasks are thin wrappers calling services. Same business logic works from both HTTP and background workers.
- **Domain-based file splitting:** Rather than one huge models.py, split by domain. Each domain has model, schema, repository, router, and service files.
- **`templates/partials/`:** HTMX swaps HTML fragments. Partials directory keeps full-page templates separate from HTMX response fragments.

## Core Patterns

### Pattern 1: Pipeline State Machine

Every file progresses through a state machine tracked in PostgreSQL.

```
DISCOVERED -> METADATA_EXTRACTED -> FINGERPRINTED -> ANALYZED ->
PROPOSAL_GENERATED -> APPROVED/REJECTED -> EXECUTED/SKIPPED
```

```python
class FileState(enum.StrEnum):
    DISCOVERED = "discovered"
    METADATA_EXTRACTED = "metadata_extracted"
    FINGERPRINTED = "fingerprinted"
    ANALYZED = "analyzed"
    PROPOSAL_GENERATED = "proposal_generated"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


async def transition_state(
    session: AsyncSession,
    file_id: int,
    from_state: FileState,
    to_state: FileState,
) -> bool:
    """Atomic state transition. Returns False if file not in expected state."""
    result = await session.execute(
        update(FileRecord)
        .where(FileRecord.id == file_id, FileRecord.state == from_state)
        .values(state=to_state, updated_at=func.now())
    )
    await session.commit()
    return result.rowcount == 1
```

**Why:** Enables resumability. Crashed batch restarts from last completed state. UI shows per-file progress. Idempotent -- workers check state before acting.

### Pattern 2: Fan-Out/Fan-In Batch Processing

A batch job (e.g., "ingest /music/unsorted") fans out into N individual file tasks. Each runs independently. Batch tracks completion via counter.

```python
async def start_batch_ingest(directory: Path, batch_id: int) -> None:
    files = [f for f in directory.rglob("*") if f.suffix.lower() in AUDIO_EXTENSIONS]

    async with get_session() as session:
        records = [
            FileRecord(original_path=str(f), batch_id=batch_id, state=FileState.DISCOVERED)
            for f in files
        ]
        session.add_all(records)
        await session.flush()

        await session.execute(
            update(Batch).where(Batch.id == batch_id).values(total=len(records))
        )
        await session.commit()

        # Fan out to workers
        pool = await create_pool(RedisSettings())
        for record in records:
            await pool.enqueue_job("hash_file", record.id)
```

### Pattern 3: Service Layer Separation

Routers are thin. Services contain business logic. Workers call services.

```python
# router (thin)
@router.post("/ingest/scan")
async def scan_directory(
    request: ScanRequest,
    ingestion: IngestionService = Depends(get_ingestion_service),
) -> BatchResponse:
    batch = await ingestion.start_scan(request.directory)
    return BatchResponse(batch_id=batch.id, total=batch.total)

# worker (thin)
async def hash_file_task(ctx: dict[str, Any], file_id: int) -> None:
    async with get_session() as session:
        service = IngestionService(FileRepository(session))
        await service.hash_file(file_id)
```

### Pattern 4: Structured LLM Output with Pydantic

```python
class FilenameProposal(BaseModel):
    proposed_filename: str
    proposed_path: str
    confidence: float  # 0.0 to 1.0
    reasoning: str

    @field_validator("proposed_filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        if len(v) > 200:
            raise ValueError("Filename too long")
        if any(c in v for c in r'\/:*?"<>|'):
            raise ValueError("Invalid characters in filename")
        return v
```

### Pattern 5: Copy-Verify-Delete File Operations

```python
async def execute_move(source: Path, destination: Path) -> ExecutionResult:
    # 1. Ensure destination directory exists
    destination.parent.mkdir(parents=True, exist_ok=True)

    # 2. Copy (never move directly for cross-filesystem safety)
    shutil.copy2(str(source), str(destination))

    # 3. Verify destination hash matches source
    source_hash = await compute_sha256(source)
    dest_hash = await compute_sha256(destination)
    if source_hash != dest_hash:
        destination.unlink()  # Clean up bad copy
        raise IntegrityError(f"Hash mismatch: {source_hash} != {dest_hash}")

    # 4. Only now remove source
    source.unlink()

    return ExecutionResult(source=source, destination=destination, hash=source_hash)
```

## Anti-Patterns to Avoid

### Anti-Pattern 1: Processing Files in API Handlers

**What:** Running audio analysis or LLM calls inside FastAPI route handlers.
**Why bad:** Blocks event loop. 200K files at ~10 sec each = API unresponsive for weeks.
**Instead:** API handlers only enqueue jobs. Workers process asynchronously. Return batch ID immediately.

### Anti-Pattern 2: Storing File Content in Database

**What:** Putting audio binary data in PostgreSQL BLOBs.
**Why bad:** 200K files at ~5MB = 1TB in PostgreSQL. Destroys backup/restore, bloats WAL.
**Instead:** Store filesystem paths. Files stay on mounted volume.

### Anti-Pattern 3: One Giant Processing Task

**What:** Single task iterating over all 200K files sequentially.
**Why bad:** No parallelism, no partial progress, no resumability.
**Instead:** Fan out into individual file tasks. Each is independent, trackable, retryable.

### Anti-Pattern 4: One LLM Call Per File

**What:** 200K individual API calls for filename proposals.
**Why bad:** Rate limits, cost explosion (~$500+ at GPT-4 prices), 55+ hours at 1 req/sec.
**Instead:** Batch 20-50 files per prompt. Send metadata summaries. Use structured output for multiple proposals per response.

### Anti-Pattern 5: Moving Files Without Verification

**What:** Direct `os.rename()` or `shutil.move()` without hash verification.
**Why bad:** Cross-filesystem moves are copy+delete. Interrupted delete = data loss. Corrupt copy = data loss.
**Instead:** Copy-verify-delete protocol with append-only audit log.

### Anti-Pattern 6: CPU-Bound Work in Async Functions

**What:** Running librosa or chromaprint directly in async coroutines.
**Why bad:** CPU-bound C extensions block the event loop. Other coroutines starve.
**Instead:** Use `asyncio.to_thread()` or `ProcessPoolExecutor` for all audio processing.

## Database Schema Design

### Core Tables

```sql
-- Central file record
CREATE TABLE files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256_hash VARCHAR(64) NOT NULL,
    original_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    current_path TEXT NOT NULL,
    file_type VARCHAR(10) NOT NULL,  -- mp3, m4a, ogg, mp4, etc.
    file_size BIGINT NOT NULL,
    state VARCHAR(30) NOT NULL DEFAULT 'discovered',
    batch_id UUID REFERENCES batches(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_files_state ON files(state);
CREATE INDEX idx_files_sha256 ON files(sha256_hash);

-- Extracted tag metadata
CREATE TABLE metadata (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL UNIQUE REFERENCES files(id),
    artist TEXT,
    title TEXT,
    album TEXT,
    year INTEGER,
    genre TEXT,
    track_number INTEGER,
    duration_seconds FLOAT,
    raw_tags JSONB NOT NULL DEFAULT '{}'
);

-- Audio analysis results
CREATE TABLE analysis (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL UNIQUE REFERENCES files(id),
    bpm FLOAT,
    musical_key VARCHAR(10),
    mood VARCHAR(50),
    style VARCHAR(50),
    energy FLOAT,
    fingerprint TEXT,
    features JSONB NOT NULL DEFAULT '{}'
);

-- AI-generated proposals
CREATE TABLE proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL REFERENCES files(id),
    proposed_filename TEXT NOT NULL,
    proposed_path TEXT NOT NULL,
    confidence FLOAT NOT NULL,
    reasoning TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_proposals_status ON proposals(status);

-- Append-only execution log
CREATE TABLE execution_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id UUID NOT NULL REFERENCES proposals(id),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    sha256_verified BOOLEAN NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Design Decisions

- **JSONB for `raw_tags` and `features`:** Audio metadata varies wildly by format. JSONB accommodates flexible schemas without migrations for every new tag type.
- **Separate `analysis` table:** Analysis can be re-run without losing original metadata. Different lifecycle.
- **`execution_log` is append-only:** Never update or delete. Full audit trail and rollback capability.
- **`state` on `files`:** Single source of truth for pipeline progress. Indexed for efficient "get next batch" queries.
- **UUID primary keys:** Avoids sequential ID leakage. Works well with distributed workers.

## Docker Compose Layout

```yaml
services:
  api:
    build: .
    command: uvicorn phaze.main:app --host 0.0.0.0 --port 8000
    volumes:
      - music_source:/music:ro       # Read-only source
      - music_output:/organized       # Write destination
    depends_on:
      - postgres
      - redis

  worker:
    build: .
    command: arq phaze.workers.settings.WorkerSettings
    volumes:
      - music_source:/music:ro
      - music_output:/organized
    depends_on:
      - postgres
      - redis
    deploy:
      replicas: 4                     # Scale workers

  postgres:
    image: postgres:16
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
```

## Scalability Considerations

| Concern | At 200K files (current) | At 1M files (future) | Notes |
|---------|------------------------|----------------------|-------|
| Ingestion | Single worker, batch inserts (1000/batch), ~1-2 hrs | Multiple workers, partitioned scans | Use COPY for bulk inserts |
| Audio analysis | 4-8 parallel arq workers, ~2-3 days | More workers, GPU for ML models | CPU-bound, scales with cores |
| LLM proposals | Batch 20-50/prompt, ~4-10 hrs | Same batching, rate limit mgmt | Cost-bound, not compute-bound |
| Database queries | Standard indexes sufficient | Partition by state, materialized views | PostgreSQL handles 1M rows fine |
| UI rendering | HTMX pagination (50/page) | Same + server-side filtering | Never load all records |
| File moves | Sequential per-worker | Parallel on different dirs | I/O-bound (disk speed) |

## Sources

- [FastAPI best practices](https://github.com/zhanymkanov/fastapi-best-practices) -- project structure patterns
- [FastAPI + Async SQLAlchemy 2 + Alembic + Docker](https://berkkaraal.com/blog/2024/09/19/setup-fastapi-project-with-async-sqlalchemy-2-alembic-postgresql-and-docker/) -- async setup reference
- [arq documentation](https://arq-docs.helpmanual.io/) -- worker and task patterns
- [HTMX + FastAPI patterns](https://johal.in/htmx-fastapi-patterns-hypermedia-driven-single-page-applications-2025/) -- UI architecture
- [Fastest way to load data into PostgreSQL](https://hakibenita.com/fast-load-data-python-postgresql) -- batch insert optimization

---
*Architecture patterns for: Music file management and AI-powered organization*
*Researched: 2026-03-27*
