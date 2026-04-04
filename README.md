# 🎵 Phaze

<div align="center">

[![CI](https://github.com/SimplicityGuy/phaze/actions/workflows/ci.yml/badge.svg)](https://github.com/SimplicityGuy/phaze/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/SimplicityGuy/phaze/branch/main/graph/badge.svg)](https://codecov.io/gh/SimplicityGuy/phaze)
![License: MIT](https://img.shields.io/github/license/SimplicityGuy/phaze)
![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)
[![uv](https://img.shields.io/badge/uv-package%20manager-orange?logo=python)](https://github.com/astral-sh/uv)
[![just](https://img.shields.io/badge/just-task%20runner-blue)](https://just.systems)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-checked-blue)](http://mypy-lang.org/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)
[![Docker](https://img.shields.io/badge/docker-ready-blue?logo=docker)](https://www.docker.com/)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-powered-orange?logo=anthropic&logoColor=white)](https://claude.ai/code)

**A music collection organizer that ingests music and concert files, fingerprints and analyzes them, uses AI to propose better filenames and destination paths, and provides a web UI to review and approve renames. All file operations use a safe copy-verify-delete protocol with full audit trails.**

</div>

## Architecture

```mermaid
graph TD
    UI["Web UI (HTMX + Tailwind)<br/>Proposals · Duplicates · Tracklists · Exec"]
    API["FastAPI (async, :8000)<br/>/api/v1/* · /proposals · /pipeline<br/>/execution · /duplicates · /tracklists"]
    PG["PostgreSQL 18-alpine<br/>:5432"]
    REDIS["Redis 8-alpine<br/>:6379"]
    WORKER["SAQ Worker<br/>(async)"]
    AUD["Audfprint :8001<br/>landmark fingerprint"]
    PAN["Panako :8002<br/>tempo-robust fingerprint"]

    UI --> API
    API --> PG
    API --> REDIS
    API --> WORKER
    WORKER --> AUD
    WORKER --> PAN
```

## File Processing Pipeline

Files progress through a state machine as they move through the pipeline:

```mermaid
stateDiagram-v2
    [*] --> DISCOVERED
    DISCOVERED --> METADATA_EXTRACTED : mutagen
    METADATA_EXTRACTED --> FINGERPRINTED : audfprint + panako
    FINGERPRINTED --> ANALYZED : essentia
    ANALYZED --> PROPOSAL_GENERATED : LLM via litellm
    PROPOSAL_GENERATED --> APPROVED : human review
    PROPOSAL_GENERATED --> REJECTED : human review
    APPROVED --> EXECUTED : copy‑verify‑delete
    APPROVED --> FAILED
    PROPOSAL_GENERATED --> DUPLICATE_RESOLVED
```

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [just](https://just.systems/) (command runner)
- Python 3.13

## Getting Started

1. **Clone and install dependencies:**
   ```bash
   git clone https://github.com/SimplicityGuy/phaze.git
   cd phaze
   uv sync
   ```

2. **Set up environment:**
   ```bash
   cp .env.example .env
   # Edit .env to configure paths and API keys
   ```

3. **Download ML models** (required for audio analysis):
   ```bash
   just download-models
   ```

4. **Start all services:**
   ```bash
   just up
   # Or: docker compose up -d
   ```

5. **Run database migrations:**
   ```bash
   just db-upgrade
   # Or: uv run alembic upgrade head
   ```

6. **Verify everything is running:**
   ```bash
   curl http://localhost:8000/health
   # {"status": "ok"}

   just docker-ps
   ```

## Services

| Service      | Port | Image                | Description                        |
|--------------|------|----------------------|------------------------------------|
| **api**      | 8000 | Custom (Dockerfile)  | FastAPI application server         |
| **worker**   | --   | Custom (Dockerfile)  | SAQ async background task processor|
| **postgres** | 5432 | postgres:18-alpine   | Primary database                   |
| **redis**    | 6379 | redis:8-alpine       | Task queue broker and cache        |
| **audfprint**| 8001 | Custom (Dockerfile)  | Landmark-based audio fingerprinting|
| **panako**   | 8002 | Custom (Dockerfile)  | Tempo-robust audio fingerprinting  |

## Configuration

All configuration is via environment variables (or `.env` file). See [`.env.example`](.env.example) for defaults.

### Core Settings

| Variable              | Default                                          | Description                        |
|-----------------------|--------------------------------------------------|------------------------------------|
| `DATABASE_URL`        | `postgresql+asyncpg://phaze:phaze@postgres:5432/phaze` | PostgreSQL connection string |
| `REDIS_URL`           | `redis://redis:6379/0`                           | Redis connection string            |
| `SCAN_PATH`           | `/data/music`                                    | Music directory (mounted read-only)|
| `OUTPUT_PATH`         | `/data/output`                                   | Destination for executed moves     |
| `MODELS_PATH`         | `./models`                                       | Essentia ML model directory        |
| `PHAZE_DEBUG`         | `false`                                          | Enable debug mode                  |
| `API_PORT`            | `8000`                                           | API server port                    |

### Worker Settings

| Variable                      | Default | Description                          |
|-------------------------------|---------|--------------------------------------|
| `WORKER_MAX_JOBS`             | `8`     | Concurrent SAQ jobs per worker       |
| `WORKER_JOB_TIMEOUT`          | `600`   | Per-file timeout (seconds)           |
| `WORKER_MAX_RETRIES`          | `4`     | Max retry attempts (1 initial + 3)   |
| `WORKER_PROCESS_POOL_SIZE`    | `4`     | CPU-bound worker pool size           |
| `WORKER_HEALTH_CHECK_INTERVAL`| `60`    | SAQ health check interval (seconds)  |

### LLM Settings

| Variable                  | Default                      | Description                          |
|---------------------------|------------------------------|--------------------------------------|
| `LLM_MODEL`              | `claude-sonnet-4-20250514`       | LLM model for proposals             |
| `ANTHROPIC_API_KEY`      | --                           | Anthropic API key                    |
| `OPENAI_API_KEY`         | --                           | OpenAI API key (alternative)         |
| `LLM_MAX_RPM`            | `30`                         | Max LLM requests per minute          |
| `LLM_BATCH_SIZE`         | `10`                         | Files per LLM call                   |
| `LLM_MAX_COMPANION_CHARS`| `3000`                       | Max chars per companion file content |

### Fingerprint Settings

| Variable         | Default                   | Description                 |
|------------------|---------------------------|-----------------------------|
| `AUDFPRINT_URL`  | `http://audfprint:8001`   | Audfprint service endpoint  |
| `PANAKO_URL`     | `http://panako:8002`      | Panako service endpoint     |

## API Reference

### Health

| Method | Path      | Description     |
|--------|-----------|-----------------|
| GET    | `/health` | Health check (verifies DB connectivity) |

### Scan (`/api/v1`)

| Method | Path                     | Description                          |
|--------|--------------------------|--------------------------------------|
| POST   | `/api/v1/scan`           | Start file discovery scan            |
| GET    | `/api/v1/scan/{batch_id}`| Get scan progress and status         |

### Pipeline (`/api/v1`, `/pipeline`)

| Method | Path                           | Description                              |
|--------|--------------------------------|------------------------------------------|
| POST   | `/api/v1/extract-metadata`     | Enqueue metadata extraction jobs         |
| POST   | `/api/v1/fingerprint`          | Enqueue fingerprint jobs                 |
| GET    | `/api/v1/fingerprint/progress` | Fingerprint processing progress          |
| POST   | `/api/v1/analyze`              | Enqueue audio analysis jobs              |
| POST   | `/api/v1/proposals/generate`   | Enqueue LLM proposal generation          |
| GET    | `/pipeline/`                   | Pipeline dashboard (HTML)                |
| GET    | `/pipeline/stats`              | Pipeline stats bar (HTMX partial)        |

### Proposals (`/proposals`)

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | `/proposals/`                 | List proposals (HTML, filterable)  |
| PATCH  | `/proposals/{id}/approve`     | Approve a proposal                 |
| PATCH  | `/proposals/{id}/reject`      | Reject a proposal                  |
| PATCH  | `/proposals/{id}/undo`        | Revert to pending                  |
| GET    | `/proposals/{id}/detail`      | Expanded detail panel              |
| PATCH  | `/proposals/bulk`             | Bulk approve/reject                |

### Execution (`/execution`, `/audit`)

| Method | Path                              | Description                          |
|--------|-----------------------------------|--------------------------------------|
| POST   | `/execution/start`                | Start batch execution (copy-verify-delete) |
| GET    | `/execution/progress/{batch_id}`  | SSE stream with real-time progress   |
| GET    | `/audit/`                         | Audit log (HTML, filterable)         |

### Duplicates (`/duplicates`)

| Method | Path                          | Description                        |
|--------|-------------------------------|------------------------------------|
| GET    | `/duplicates/`                | List duplicate groups (HTML)       |
| GET    | `/duplicates/{hash}/compare`  | Comparison table for a group       |
| POST   | `/duplicates/{hash}/resolve`  | Mark non-canonical as duplicates   |
| POST   | `/duplicates/{hash}/undo`     | Undo resolution                    |
| POST   | `/duplicates/resolve-all`     | Bulk resolve all groups            |
| POST   | `/duplicates/undo-all`        | Undo bulk resolution               |

### Tracklists (`/tracklists`)

| Method | Path                                    | Description                          |
|--------|-----------------------------------------|--------------------------------------|
| GET    | `/tracklists/`                          | List tracklists (HTML, filterable)   |
| GET    | `/tracklists/scan`                      | Show unscanned files                 |
| POST   | `/tracklists/scan`                      | Trigger fingerprint scan             |
| GET    | `/tracklists/scan/status`               | Scan progress                        |
| GET    | `/tracklists/{id}/tracks`               | View tracks in tracklist             |
| POST   | `/tracklists/{id}/link`                 | Manually link to file                |
| POST   | `/tracklists/{id}/unlink`               | Remove link                          |
| POST   | `/tracklists/{id}/rescrape`             | Re-scrape from 1001Tracklists        |
| POST   | `/tracklists/{id}/approve`              | Approve tracklist                    |
| POST   | `/tracklists/{id}/reject`               | Reject tracklist                     |
| GET    | `/tracklists/tracks/{id}/edit/{field}`  | Inline edit UI                       |
| PUT    | `/tracklists/tracks/{id}/edit/{field}`  | Save inline edit                     |
| DELETE | `/tracklists/tracks/{id}`               | Delete track                         |

### Companion Files (`/api/v1`)

| Method | Path                    | Description                              |
|--------|-------------------------|------------------------------------------|
| POST   | `/api/v1/associate`     | Link companion files to media files      |
| GET    | `/api/v1/duplicates`    | List duplicate groups by SHA256          |

### Preview (`/preview`)

| Method | Path        | Description                              |
|--------|-------------|------------------------------------------|
| GET    | `/preview/` | Directory tree of approved proposals     |

## Supported File Types

| Category   | Extensions                                                       |
|------------|------------------------------------------------------------------|
| Music      | mp3, m4a, ogg, flac, wav, aiff, wma, aac, opus                  |
| Video      | mp4, mkv, avi, webm, mov, wmv, flv                              |
| Companion  | cue, nfo, txt, jpg, jpeg, png, gif, m3u, m3u8, pls, sfv, md5    |

## Database Schema

### Tables

| Table                 | Description                                       |
|-----------------------|---------------------------------------------------|
| `files`               | Central file records with state machine           |
| `scan_batches`        | Scan operation progress tracking                  |
| `metadata`            | Audio tag metadata (1:1 with files)               |
| `analysis`            | BPM, key, mood, style results (1:1 with files)    |
| `proposals`           | AI-generated rename/move proposals                |
| `execution_log`       | Append-only audit trail for file operations       |
| `file_companions`     | Many-to-many: companion files to media files      |
| `fingerprint_results` | Per-engine fingerprint results (audfprint/panako) |
| `tracklists`          | Tracklist metadata from 1001Tracklists            |
| `tracklist_versions`  | Versioned tracklist snapshots                     |
| `tracklist_tracks`    | Individual tracks within a version                |

### Migrations

Managed by Alembic with async template:

```bash
just db-upgrade              # Apply all pending migrations
just db-revision "message"   # Create new migration (autogenerate)
just db-current              # Show current migration
just db-downgrade            # Roll back one migration
just db-history              # Show migration history
```

## File Execution Safety

File moves use a copy-verify-delete protocol with write-ahead audit logging:

1. **Log** -- Create `ExecutionLog` entry with `IN_PROGRESS` status (committed immediately)
2. **Copy** -- Copy source file to destination (preserving metadata)
3. **Verify** -- Compute SHA256 of copy, compare to original hash
4. **Delete** -- Remove original only after hash verification passes
5. **Update** -- Mark `FileRecord` as `EXECUTED`, update `ExecutionLog` to `COMPLETED`

If hash verification fails, the bad copy is deleted and the original is preserved. The write-ahead audit log persists even if the process crashes mid-operation.

## Development

### Quick Reference

| Command                  | Description                          |
|--------------------------|--------------------------------------|
| `just install`           | Install dependencies                 |
| `just up` / `just down`  | Start / stop Docker services         |
| `just rebuild`           | Rebuild and restart services         |
| `just logs`              | Follow all service logs              |
| `just test`              | Run tests                            |
| `just test-cov`          | Run tests with coverage              |
| `just test-file FILE`    | Run a specific test file             |
| `just lint`              | Lint with ruff                       |
| `just lint-fix`          | Lint with auto-fix                   |
| `just fmt`               | Format with ruff                     |
| `just typecheck`         | Type check with mypy                 |
| `just check`             | Run lint + typecheck + test          |
| `just pre-commit`        | Run all pre-commit hooks             |
| `just scan`              | Trigger a file scan                  |
| `just scan-status ID`    | Check scan status                    |
| `just fingerprint`       | Trigger fingerprint processing       |
| `just fingerprint-progress` | Check fingerprint progress        |
| `just worker-logs`       | Follow worker logs                   |
| `just worker-restart`    | Restart the worker                   |
| `just worker-health`     | Check SAQ worker health              |
| `just docker-build`      | Build Docker images                  |
| `just docker-shell`      | Shell into the API container         |
| `just docker-ps`         | List running containers              |
| `just pip-audit`         | Dependency vulnerability scan        |
| `just security`          | Python SAST with bandit              |
| `just security-all`      | All security checks                  |
| `just download-models`   | Download essentia ML models          |
| `just update-hooks`      | Update pre-commit hook SHAs          |
| `just lock-upgrade`      | Upgrade all dependency versions      |

### Running Tests

Tests require PostgreSQL:

```bash
docker compose up -d postgres     # Start test database
just test                         # Run tests
just test-cov                     # With coverage report
```

Minimum 85% code coverage is enforced.

### Code Quality

- **Linter/Formatter:** [Ruff](https://docs.astral.sh/ruff/) (150-char line length, double quotes)
- **Type checker:** [mypy](https://mypy-lang.org/) (strict mode, excludes tests)
- **Pre-commit hooks:** ruff, bandit, mypy, shellcheck, yamllint, actionlint, jsonschema validation
- All hooks use frozen SHAs for reproducibility

### CI/CD

GitHub Actions runs on every push and PR:

| Job          | Description                                              |
|--------------|----------------------------------------------------------|
| **Quality**  | Pre-commit hooks (ruff, mypy, yamllint, etc.)            |
| **Test**     | pytest with PostgreSQL, coverage upload to Codecov       |
| **Security** | pip-audit, bandit, Semgrep, TruffleHog, Trivy            |

## Project Structure

```
phaze/
├── src/phaze/                  # Application package
│   ├── config.py               # Pydantic settings (env vars)
│   ├── constants.py            # File categories, extension map, tuning constants
│   ├── database.py             # Async SQLAlchemy engine + session factory
│   ├── main.py                 # FastAPI app factory with lifespan
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── base.py             #   DeclarativeBase + TimestampMixin
│   │   ├── file.py             #   FileRecord + FileState enum
│   │   ├── scan_batch.py       #   ScanBatch progress tracking
│   │   ├── metadata.py         #   FileMetadata (audio tags)
│   │   ├── analysis.py         #   AnalysisResult (BPM, key, mood, style)
│   │   ├── fingerprint.py      #   FingerprintResult (per-engine)
│   │   ├── proposal.py         #   RenameProposal + ProposalStatus
│   │   ├── execution.py        #   ExecutionLog (audit trail)
│   │   ├── tracklist.py        #   Tracklist + TracklistVersion + TracklistTrack
│   │   └── file_companion.py   #   FileCompanion (companion-media join)
│   ├── routers/                # API + UI endpoints
│   │   ├── health.py           #   GET /health
│   │   ├── scan.py             #   File discovery scan
│   │   ├── pipeline.py         #   Pipeline dashboard + processing triggers
│   │   ├── proposals.py        #   Proposal review + approval UI
│   │   ├── execution.py        #   Batch execution + SSE progress
│   │   ├── preview.py          #   Directory tree preview
│   │   ├── duplicates.py       #   Duplicate resolution UI
│   │   ├── tracklists.py       #   Tracklist management UI
│   │   └── companion.py        #   Companion file association
│   ├── schemas/                # Pydantic request/response models
│   │   ├── scan.py             #   Scan API schemas
│   │   └── companion.py        #   Companion/duplicate schemas
│   ├── services/               # Business logic
│   │   ├── ingestion.py        #   File discovery, hashing, bulk upsert
│   │   ├── metadata.py         #   Tag extraction via mutagen
│   │   ├── analysis.py         #   BPM/key/mood via essentia
│   │   ├── fingerprint.py      #   Multi-engine fingerprint orchestrator
│   │   ├── proposal.py         #   LLM calling + context building
│   │   ├── proposal_queries.py #   Proposal queries + pagination
│   │   ├── execution.py        #   Copy-verify-delete with audit logging
│   │   ├── execution_queries.py#   Execution log queries + pagination
│   │   ├── companion.py        #   Companion file association
│   │   ├── dedup.py            #   Duplicate detection + resolution
│   │   ├── collision.py        #   Destination path collision detection
│   │   ├── pipeline.py         #   Pipeline stats + file state queries
│   │   ├── tracklist_scraper.py#   1001Tracklists web scraper
│   │   └── tracklist_matcher.py#   Fuzzy match tracklists to files
│   ├── tasks/                  # SAQ async background jobs
│   │   ├── worker.py           #   SAQ settings + startup/shutdown
│   │   ├── functions.py        #   process_file (full pipeline per file)
│   │   ├── metadata_extraction.py # extract_file_metadata
│   │   ├── fingerprint.py      #   fingerprint_file (multi-engine)
│   │   ├── proposal.py         #   generate_proposals (batch LLM)
│   │   ├── execution.py        #   execute_approved_batch
│   │   ├── scan.py             #   scan_live_set (fingerprint matching)
│   │   ├── tracklist.py        #   scrape/search/refresh tracklists
│   │   ├── pool.py             #   ProcessPoolExecutor for CPU work
│   │   └── session.py          #   Session utilities
│   ├── prompts/                # LLM prompt templates
│   └── templates/              # Jinja2 HTML templates (HTMX + Tailwind)
│       ├── pipeline/           #   Pipeline dashboard
│       ├── proposals/          #   Proposal approval UI
│       ├── execution/          #   Execution dashboard + audit log
│       ├── duplicates/         #   Duplicate resolution UI
│       ├── tracklists/         #   Tracklist management UI
│       └── preview/            #   Directory tree preview
├── services/                   # Fingerprint microservices
│   ├── audfprint/              #   Landmark-based fingerprinting
│   └── panako/                 #   Tempo-robust fingerprinting
├── tests/                      # Test suite (85%+ coverage)
│   ├── conftest.py             #   Fixtures + test DB setup
│   ├── test_models/            #   ORM model tests
│   ├── test_routers/           #   Endpoint integration tests
│   ├── test_services/          #   Business logic unit tests
│   └── test_tasks/             #   SAQ job tests
├── alembic/                    # Database migrations (async template)
│   └── versions/               #   Migration scripts (001-008)
├── .github/workflows/          # CI/CD pipelines
│   ├── ci.yml                  #   Main orchestrator
│   ├── code-quality.yml        #   Pre-commit hooks
│   ├── tests.yml               #   Pytest + Codecov
│   └── security.yml            #   pip-audit, bandit, Semgrep, Trivy
├── scripts/                    # Utility scripts
│   └── download-models.sh      #   Download essentia ML models
├── docker-compose.yml          # Service orchestration
├── docker-compose.override.yml # Local development overrides
├── Dockerfile                  # Multi-stage build (API + worker)
├── justfile                    # Developer commands
├── pyproject.toml              # Project config + tool settings
└── uv.lock                     # Frozen dependency versions
```

## Technology Stack

| Category       | Technology                              | Purpose                              |
|----------------|-----------------------------------------|--------------------------------------|
| **Runtime**    | Python 3.13                             | Application runtime                  |
| **Web**        | FastAPI + Uvicorn                       | Async API server                     |
| **Database**   | PostgreSQL 18 + SQLAlchemy + asyncpg    | Primary data store (async ORM)       |
| **Migrations** | Alembic (async template)                | Database schema management           |
| **Task Queue** | SAQ + Redis                             | Async background job processing      |
| **Audio Tags** | mutagen                                 | Read/write audio metadata            |
| **Analysis**   | essentia-tensorflow                     | BPM, key, mood, style detection      |
| **Fingerprint**| audfprint + Panako                      | Audio deduplication + identification |
| **AI/LLM**     | litellm (pinned <1.82.7)               | Unified LLM API for rename proposals |
| **Scraping**   | BeautifulSoup4 + lxml                   | 1001Tracklists integration           |
| **Matching**   | rapidfuzz                               | Fuzzy string matching                |
| **UI**         | Jinja2 + HTMX + Tailwind CSS + Alpine.js| Server-rendered interactive UI       |
| **Deploy**     | Docker Compose                          | Container orchestration              |

## License

[MIT](LICENSE)
