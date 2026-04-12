# Phaze

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="design/assets/banner_dark.png">
  <source media="(prefers-color-scheme: light)" srcset="design/assets/banner_light.png">
  <img alt="Phaze — Align Your Music" src="design/assets/banner_dark.png" width="600">
</picture>

<br><br>

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

## Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [just](https://just.systems/) (command runner)
- Python 3.13

### Setup

```bash
git clone https://github.com/SimplicityGuy/phaze.git
cd phaze
uv sync
cp .env.example .env          # Edit to configure paths and API keys
just download-models           # Required for audio analysis
just up                        # Start all services
just db-upgrade                # Run database migrations
curl http://localhost:8000/health   # Verify: {"status": "ok"}
```

## Services

| Service      | Port | Description                        |
|--------------|------|------------------------------------|
| **api**      | 8000 | FastAPI application server         |
| **worker**   | --   | SAQ async background task processor|
| **postgres** | 5432 | Primary database                   |
| **redis**    | 6379 | Task queue broker and cache        |
| **audfprint**| 8001 | Landmark-based audio fingerprinting|
| **panako**   | 8002 | Tempo-robust audio fingerprinting  |

## Supported File Types

| Category   | Extensions                                                       |
|------------|------------------------------------------------------------------|
| Music      | mp3, m4a, ogg, flac, wav, aiff, wma, aac, opus                  |
| Video      | mp4, mkv, avi, webm, mov, wmv, flv                              |
| Companion  | cue, nfo, txt, jpg, jpeg, png, gif, m3u, m3u8, pls, sfv, md5    |

## Development

```bash
just install          # Install dependencies
just up / just down   # Start / stop services
just test             # Run tests
just test-cov         # Tests with coverage (85% min)
just check            # Lint + typecheck + test
just pre-commit       # Run all pre-commit hooks
```

See `just --list` for the full command reference.

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

## Documentation

- [API Reference](docs/api.md)
- [Configuration](docs/configuration.md)
- [Database Schema & Migrations](docs/database.md)
- [Project Structure](docs/project-structure.md)

## License

[MIT](LICENSE)
