# Phaze

A music alignment tool that organizes ~200K music and concert files through AI-powered renaming with human-in-the-loop approval.

## Overview

Phaze ingests music files (mp3, m4a, ogg) and concert videos, analyzes them for BPM/mood/style, uses AI to propose better filenames, and provides a web UI for reviewing and approving renames. All file operations use a safe copy-verify-delete protocol with full audit trails.

## Architecture

- **FastAPI** async API server
- **PostgreSQL 16** primary database
- **Redis 7** task queue broker
- **Alembic** database migrations
- **arq** async task queue (Phase 4)

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [just](https://just.systems/) (command runner, optional)
- Python 3.13

## Getting Started

1. **Clone and install dependencies:**
   ```bash
   git clone <repo-url>
   cd phaze
   uv sync
   ```

2. **Set up environment:**
   ```bash
   cp .env.example .env
   # Edit .env if you need to change defaults
   ```

3. **Start services:**
   ```bash
   docker compose up -d
   # Or: just up
   ```

4. **Run database migrations:**
   ```bash
   uv run alembic upgrade head
   # Or: just db-upgrade
   ```

5. **Verify health:**
   ```bash
   curl http://localhost:8000/health
   # Expected: {"status": "ok"}
   ```

## Scanning Files

Phaze scans mounted directories to discover music, video, and companion files.

### Configuration

Set `SCAN_PATH` in `.env` to the directory containing music files. In Docker, this is mounted read-only at `/data/music`.

### Triggering a Scan

```bash
curl -X POST http://localhost:8000/api/v1/scan
# Or with a custom path:
curl -X POST http://localhost:8000/api/v1/scan \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/music/subset"}'
```

### Checking Scan Status

```bash
curl http://localhost:8000/api/v1/scan/{batch_id}
```

### Supported File Types

| Category   | Extensions                                                  |
|------------|-------------------------------------------------------------|
| Music      | mp3, m4a, ogg, flac, wav, aiff, wma, aac                   |
| Video      | mp4, mkv, avi, webm, mov, wmv, flv                         |
| Companion  | cue, nfo, txt, jpg, jpeg, png, gif, m3u, m3u8, pls, sfv, md5 |

## Development

### Common Commands

Using `just` (recommended):

| Command | Description |
|---------|-------------|
| `just up` | Start all Docker services |
| `just down` | Stop all services |
| `just test` | Run tests |
| `just test-cov` | Run tests with coverage |
| `just lint` | Run ruff linter |
| `just fmt` | Format code |
| `just typecheck` | Run mypy |
| `just check` | Run lint + typecheck + test |
| `just scan` | Trigger a file scan |
| `just scan-status ID` | Check scan status by batch ID |
| `just db-upgrade` | Apply migrations |
| `just db-revision "msg"` | Create new migration |

Or using uv directly:

```bash
uv run pytest tests/ -x -q          # Run tests
uv run ruff check .                  # Lint
uv run ruff format .                 # Format
uv run mypy .                        # Type check
uv run alembic upgrade head          # Migrations
```

### Running Tests

Tests require PostgreSQL. Start it first:

```bash
docker compose up -d postgres
```

The test suite uses a separate `phaze_test` database. Run:

```bash
uv run pytest tests/ -x -q
uv run pytest --cov=phaze --cov-report=term-missing  # With coverage
```

### Pre-commit Hooks

```bash
pre-commit install
pre-commit run --all-files
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| API | 8000 | FastAPI application server |
| PostgreSQL | 5432 | Primary database |
| Redis | 6379 | Task queue broker |
| Worker | - | Background task processor (Phase 4) |

## Project Structure

```
phaze/
├── src/phaze/               # Application package
│   ├── config.py            # Settings (pydantic-settings)
│   ├── constants.py         # File categories, extension map, tuning constants
│   ├── database.py          # Async SQLAlchemy engine
│   ├── main.py              # FastAPI app factory
│   ├── models/              # SQLAlchemy ORM models
│   │   ├── scan_batch.py    # Scan batch tracking
│   │   └── file.py          # File record model
│   ├── routers/             # API route handlers
│   │   ├── health.py        # Health check endpoint
│   │   └── scan.py          # Scan trigger and status endpoints
│   ├── schemas/             # Pydantic request/response models
│   │   └── scan.py          # Scan API schemas
│   └── services/            # Business logic
│       └── ingestion.py     # File discovery, hashing, and bulk upsert
├── tests/                   # Test suite
├── alembic/                 # Database migrations
├── docker-compose.yml       # Service orchestration
├── Dockerfile               # Container image
├── justfile                 # Developer commands
└── pyproject.toml           # Project configuration
```

## License

MIT
