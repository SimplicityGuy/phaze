# Phase 1: Infrastructure & Project Setup - Research

**Researched:** 2026-03-27
**Domain:** Python project scaffolding, Docker Compose orchestration, async database setup, FastAPI skeleton
**Confidence:** HIGH

## Summary

Phase 1 establishes the foundational development environment for Phaze: a Docker Compose stack running FastAPI (API server), an arq worker process, PostgreSQL 16, and Redis 7. The phase includes the full initial database schema via Alembic migrations, a pydantic-settings configuration layer, and a health check endpoint confirming database connectivity. All subsequent phases build on this foundation.

The stack is well-established and thoroughly documented. FastAPI + async SQLAlchemy 2.0 + asyncpg + Alembic is the de facto standard for async Python APIs backed by PostgreSQL. The primary risk area is getting the async Alembic template configured correctly (importing all models in `env.py` for autogenerate) and Docker volume permissions (matching container UID/GID to host user).

**Primary recommendation:** Use `src/phaze/` layout with `alembic init -t async`, SQLAlchemy `DeclarativeBase` with naming conventions, pydantic-settings for config, and Docker Compose with health checks and `user: "${UID}:${GID}"` for file ownership safety.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Use `src/` layout with `src/phaze/` as the main package. Modern Python convention, works well with uv, Docker, and editable installs.
- **D-02:** Separate router/service/worker layers within the package (async monolith pattern per research).
- **D-03:** Full schema in the initial migration -- all tables (files, metadata, analysis, proposals, execution_log, audit) since they are known from requirements. Easier to develop against than incremental migrations.
- **D-04:** PostgreSQL 16+ as specified in research. Use JSONB columns for flexible metadata storage.
- **D-05:** Use pydantic-settings with `.env` file for configuration. `SecretStr` for API keys and sensitive values.
- **D-06:** Docker Compose profiles for dev vs prod differentiation.
- **D-07:** Everything runs in Docker -- FastAPI, workers, PostgreSQL, Redis all in Docker Compose. Volume mounts for source code + uvicorn `--reload` for hot reload during development.

### Claude's Discretion
- Project file layout within `src/phaze/` (routers, services, models, workers subdirectories)
- Alembic configuration details (async template, naming conventions)
- Docker base image selection (python:3.13-slim or similar)
- uvicorn configuration (port, workers, reload settings)
- Redis configuration defaults

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| INF-01 | All services run via Docker Compose (API, workers, PostgreSQL, Redis) | Docker Compose layout documented in Architecture Patterns section; health checks, volume mounts, UID/GID mapping all covered |
| INF-03 | Database migrations managed via Alembic | Async Alembic template pattern documented with code examples; naming conventions for constraints; full schema design from ARCHITECTURE.md |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

These directives are mandatory and override any conflicting recommendation:

- **Python 3.13 exclusively** -- all code, Docker images, and tooling must target 3.13
- **`uv` only** -- never use bare `pip`, `python`, `pytest`, or `mypy`; always prefix with `uv run`
- **Pre-commit hooks required** -- frozen SHAs, all hooks must pass before commits
- **Ruff config**: line length 150, target Python 3.13, double quotes, specific rule sets (ARG, B, C4, E, F, I, PLC, PTH, RUF, S, SIM, T20, TCH, UP, W, W191)
- **Mypy strict mode**: `disallow_untyped_defs`, `disallow_incomplete_defs`, `strict_equality`, etc. Excludes tests.
- **85% minimum code coverage**
- **pyproject.toml section order**: `[build-system]` -> `[project]` -> `[project.scripts]` -> `[tool.*]` -> `[dependency-groups]`
- **isort via ruff**: `lines-after-imports = 2`, `combine-as-imports = true`, `known-first-party = ["phaze"]`
- **CI pattern**: Reusable workflows via `workflow_call`, separate jobs for quality/tests/security, emoji prefixes on step names
- **Every feature gets its own PR** -- one PR per feature

## Standard Stack

### Core (Phase 1 Only)

| Library | Version (Verified) | Purpose | Why Standard |
|---------|--------------------|---------|--------------|
| FastAPI | 0.135.2 | Web framework / API | De facto async Python API framework. Pydantic integration, auto-generated OpenAPI docs. |
| uvicorn | 0.42.0 | ASGI server | Standard production server for FastAPI. `--reload` for dev, `--workers` for prod. |
| SQLAlchemy | 2.0.48 | ORM / database toolkit | Industry standard. Full async support via `create_async_engine` + asyncpg. |
| asyncpg | 0.31.0 | PostgreSQL async driver | Fastest Python PostgreSQL driver. Purpose-built for asyncio. |
| Alembic | 1.18.4 | Database migrations | Official SQLAlchemy migration tool. Async template via `alembic init -t async`. |
| pydantic | 2.12.5 | Data validation | FastAPI dependency. Used for request/response schemas. |
| pydantic-settings | 2.13.1 | Configuration management | Type-safe config from env vars / `.env` files. `SecretStr` for sensitive values. |
| PostgreSQL | 16 (Docker image) | Primary database | Project constraint. JSONB, full-text search, handles 200K+ records. |
| Redis | 7 (Docker image) | Task queue broker / future cache | Required by arq. Alpine image for minimal footprint. |

### Development / Testing

| Library | Version (Verified) | Purpose |
|---------|--------------------|---------|
| pytest | 9.0.2 | Test runner |
| pytest-asyncio | 1.3.0 | Async test support |
| pytest-cov | 7.1.0 | Coverage reporting |
| httpx | 0.28.1 | FastAPI async test client |
| ruff | (per CLAUDE.md) | Linting + formatting |
| mypy | (per CLAUDE.md) | Type checking |
| pre-commit | (per CLAUDE.md) | Git hooks |

### Not Needed in Phase 1

These are in the full stack but should NOT be installed yet (YAGNI):
- arq (Phase 4: Task Queue & Worker Infrastructure)
- mutagen, librosa, pyacoustid (Phase 2/5: Ingestion/Analysis)
- litellm (Phase 6: AI Proposals)
- Jinja2, HTMX, Tailwind, Alpine.js (Phase 7: Approval UI)

**Exception:** The worker Docker Compose service should be defined in Phase 1 (for INF-01), but will use a placeholder entrypoint until arq is added in Phase 4.

**Installation (Phase 1 only):**
```bash
uv init --python 3.13
uv add fastapi uvicorn sqlalchemy asyncpg alembic pydantic-settings
uv add --dev pytest pytest-asyncio pytest-cov httpx mypy ruff pre-commit
```

## Architecture Patterns

### Recommended Project Structure (Phase 1)

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
│       │   ├── __init__.py         # Import all models (for Alembic autogenerate)
│       │   ├── base.py             # DeclarativeBase with naming conventions
│       │   ├── file.py             # File record (path, hash, state)
│       │   ├── metadata.py         # Extracted tag metadata
│       │   ├── analysis.py         # BPM, key, mood, fingerprint
│       │   ├── proposal.py         # Rename/move proposals
│       │   └── execution.py        # Execution log (append-only audit)
│       │
│       ├── routers/                # FastAPI route handlers (thin)
│       │   ├── __init__.py
│       │   └── health.py           # Health check endpoint
│       │
│       └── services/               # Business logic (empty stubs for now)
│           └── __init__.py
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # Fixtures: async DB session, test client
│   └── test_health.py             # Health endpoint test
│
├── alembic/                        # Database migrations (async template)
│   ├── env.py                      # Must import all models for autogenerate
│   ├── script.py.mako
│   └── versions/
│       └── 001_initial_schema.py   # Full initial migration
│
├── docker-compose.yml              # API, worker, PostgreSQL, Redis
├── docker-compose.override.yml     # Dev overrides (volume mounts, reload)
├── Dockerfile
├── alembic.ini
├── pyproject.toml
├── .env.example                    # Template for required env vars
├── .pre-commit-config.yaml
└── .github/
    └── workflows/                  # CI (reusable workflow pattern)
```

### Pattern 1: App Factory with Lifespan

**What:** FastAPI app created via factory function with async lifespan for startup/shutdown hooks (database pool, Redis connection).
**When to use:** Always -- required for proper connection lifecycle management.

```python
# src/phaze/main.py
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from phaze.database import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Startup: verify database connectivity
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    yield
    # Shutdown: dispose connection pool
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Phaze", lifespan=lifespan)
    # Register routers
    from phaze.routers import health
    app.include_router(health.router)
    return app


app = create_app()
```

Source: [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)

### Pattern 2: Async SQLAlchemy Engine + Session Factory

**What:** Centralized engine and session factory using `create_async_engine` and `async_sessionmaker`.

```python
# src/phaze/database.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.config import settings


engine = create_async_engine(
    str(settings.database_url),
    echo=settings.debug,
    pool_size=5,
    max_overflow=10,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with async_session() as session:
        yield session
```

Source: [SQLAlchemy Async Session](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)

### Pattern 3: DeclarativeBase with Naming Conventions

**What:** Custom base class with constraint naming conventions so Alembic migrations generate predictable, database-portable constraint names.

```python
# src/phaze/models/base.py
import uuid
from datetime import datetime

from sqlalchemy import MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
```

Source: [Alembic Naming Conventions](https://alembic.sqlalchemy.org/en/latest/naming.html)

### Pattern 4: Pydantic Settings Configuration

**What:** Type-safe configuration from environment variables and `.env` file.

```python
# src/phaze/config.py
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    database_url: str = "postgresql+asyncpg://phaze:phaze@postgres:5432/phaze"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Application
    debug: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Future: LLM API keys
    openai_api_key: SecretStr | None = None


settings = Settings()
```

Source: [pydantic-settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)

### Pattern 5: Async Alembic env.py

**What:** Alembic async migration environment that imports all models for autogenerate.

```python
# alembic/env.py (key parts)
import asyncio
from sqlalchemy.ext.asyncio import async_engine_from_config

# CRITICAL: Import all models so autogenerate discovers them
from phaze.models import *  # noqa: F401, F403
from phaze.models.base import Base

target_metadata = Base.metadata


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online():
    asyncio.run(run_async_migrations())
```

Source: [Alembic Async Template](https://alembic.sqlalchemy.org/en/latest/cookbook.html)

### Anti-Patterns to Avoid

- **No naming convention on Base:** Alembic cannot autogenerate `DROP CONSTRAINT` for unnamed constraints. Always set `MetaData(naming_convention=...)`.
- **Importing models lazily:** Alembic autogenerate only sees models imported at `env.py` load time. Import all model modules in `models/__init__.py` and import that in `env.py`.
- **Sync Alembic with async engine:** Using the default (sync) Alembic template with asyncpg will fail. Must use `alembic init -t async`.
- **`expire_on_commit=True` (default):** Causes `MissingGreenlet` errors when accessing attributes after commit in async code. Set `expire_on_commit=False` on the session factory.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Configuration from env vars | Custom os.environ parsing | pydantic-settings | Type validation, `.env` file support, `SecretStr`, nested models |
| Database migrations | Raw SQL migration scripts | Alembic with async template | Autogenerate from models, version tracking, up/down migrations |
| Constraint naming | Manual constraint names | SQLAlchemy naming conventions | Consistent, predictable names across all tables and all future migrations |
| Docker health checks | Custom startup scripts | Docker Compose `healthcheck` | Built-in retry logic, `depends_on: condition: service_healthy` |
| Connection pooling | Manual connection management | SQLAlchemy `create_async_engine` pool | Configurable pool_size, max_overflow, automatic connection recycling |

## Common Pitfalls

### Pitfall 1: Docker Volume Permissions Mismatch
**What goes wrong:** Container runs as root, files created on mounted volumes are owned by root on host.
**Why it happens:** Docker bind-mounts pass through host UID/GID. Default container user is root.
**How to avoid:** Use `user: "${UID}:${GID}"` in docker-compose.yml for services that write files. Define `UID` and `GID` in `.env`.
**Warning signs:** Files on host owned by `root:root` after container operations.

### Pitfall 2: Missing Health Checks on depends_on
**What goes wrong:** API container starts before PostgreSQL is ready to accept connections. Alembic migration or first request fails.
**Why it happens:** Docker Compose `depends_on` only waits for container start, not service readiness.
**How to avoid:** Add `healthcheck` to PostgreSQL and Redis services. Use `depends_on: postgres: condition: service_healthy`.
**Warning signs:** Intermittent connection refused errors on startup.

### Pitfall 3: Alembic Autogenerate Finds No Changes
**What goes wrong:** Running `alembic revision --autogenerate` generates an empty migration.
**Why it happens:** Models not imported in `env.py` at load time. Alembic cannot discover models it has not seen.
**How to avoid:** Create `models/__init__.py` that imports all model modules. Import `phaze.models` in Alembic `env.py`.
**Warning signs:** Empty `upgrade()` and `downgrade()` functions in generated migration.

### Pitfall 4: MissingGreenlet Error in Async SQLAlchemy
**What goes wrong:** `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called` when accessing model attributes after commit.
**Why it happens:** SQLAlchemy's default `expire_on_commit=True` triggers lazy loading after commit, which requires a sync context.
**How to avoid:** Set `expire_on_commit=False` on `async_sessionmaker`. Use eager loading (`selectinload`, `joinedload`) for relationships.
**Warning signs:** Greenlet errors in any async code path that accesses model attributes after `session.commit()`.

### Pitfall 5: Alembic Cannot DROP Unnamed Constraints
**What goes wrong:** Alembic generates migration that fails on SQLite or certain databases because constraint has no name.
**Why it happens:** SQLAlchemy does not auto-name constraints by default. The autogenerated migration references unnamed constraints.
**How to avoid:** Always configure `MetaData(naming_convention=...)` on the `DeclarativeBase` before creating any models or migrations.
**Warning signs:** Migration errors referencing `None` constraint names.

## Code Examples

### Docker Compose (docker-compose.yml)

```yaml
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile
    command: uvicorn phaze.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    build:
      context: .
      dockerfile: Dockerfile
    command: echo "Worker placeholder - arq added in Phase 4"
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: phaze
      POSTGRES_PASSWORD: phaze
      POSTGRES_DB: phaze
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U phaze"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  pgdata:
```

### Docker Compose Override (dev) (docker-compose.override.yml)

```yaml
services:
  api:
    command: uvicorn phaze.main:app --host 0.0.0.0 --port 8000 --reload
    volumes:
      - ./src:/app/src
    environment:
      - PHAZE_DEBUG=true

  worker:
    volumes:
      - ./src:/app/src
```

### Dockerfile

```dockerfile
FROM python:3.13-slim AS base

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .

# Install project
RUN uv sync --frozen --no-dev

# Non-root user
RUN useradd -m -r phaze
USER phaze

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "phaze.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Health Check Endpoint

```python
# src/phaze/routers/health.py
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session


router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
```

### SQLAlchemy Model Example (File Record)

```python
# src/phaze/models/file.py
import enum
import uuid

from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


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


class FileRecord(TimestampMixin, Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    current_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default=FileState.DISCOVERED)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        Index("ix_files_state", "state"),
        Index("ix_files_sha256_hash", "sha256_hash"),
    )
```

### Test Configuration (conftest.py)

```python
# tests/conftest.py
import asyncio
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.main import create_app
from phaze.models.base import Base
from phaze.database import get_session


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def async_engine():
    engine = create_async_engine("postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine) -> AsyncGenerator[AsyncSession]:
    async_session = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


@pytest_asyncio.fixture
async def client(session) -> AsyncGenerator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
```

## Database Schema

The full schema from ARCHITECTURE.md research should be implemented in the initial Alembic migration (per decision D-03). Tables:

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `files` | Central file record with state machine | id (UUID), sha256_hash, original_path, state, file_type, file_size |
| `metadata` | Extracted tag metadata (1:1 with files) | file_id (FK), artist, title, album, year, raw_tags (JSONB) |
| `analysis` | Audio analysis results (1:1 with files) | file_id (FK), bpm, musical_key, mood, fingerprint, features (JSONB) |
| `proposals` | AI-generated rename/move proposals | file_id (FK), proposed_filename, proposed_path, confidence, status |
| `execution_log` | Append-only audit trail | proposal_id (FK), source_path, destination_path, sha256_verified |

All tables use UUID primary keys, TIMESTAMPTZ for timestamps, and JSONB for flexible fields. The `files.state` and `proposals.status` columns should be indexed.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `@app.on_event("startup")` | `lifespan` context manager | FastAPI 0.109+ (2024) | Old events are deprecated. Use `lifespan` parameter. |
| `DeclarativeBase` (legacy) | `DeclarativeBase` class (SQLAlchemy 2.0) | SQLAlchemy 2.0 (2023) | `declarative_base()` function is legacy. Use class-based `DeclarativeBase`. |
| `sessionmaker` (sync) | `async_sessionmaker` | SQLAlchemy 2.0 | Must use `async_sessionmaker` with `AsyncSession` for async code. |
| Alembic sync template | `alembic init -t async` | Alembic 1.7+ | Required when using asyncpg driver. Sync template will fail. |

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| Config file | pyproject.toml (to be created in Wave 0) |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest --cov=phaze --cov-report=term-missing` |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INF-01 | Docker Compose starts all services | integration (manual) | `docker compose up -d && docker compose ps` | N/A (manual verification) |
| INF-01 | Health endpoint returns 200 | unit | `uv run pytest tests/test_health.py -x` | Wave 0 |
| INF-03 | Alembic migration applies cleanly | integration | `uv run alembic upgrade head` | Wave 0 |
| INF-03 | All tables created with correct columns | unit | `uv run pytest tests/test_models.py -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest --cov=phaze --cov-report=term-missing`
- **Phase gate:** Full suite green + `docker compose up` verified

### Wave 0 Gaps
- [ ] `pyproject.toml` -- project configuration with all tool settings
- [ ] `.pre-commit-config.yaml` -- pre-commit hook configuration
- [ ] `tests/conftest.py` -- async database fixtures, test client
- [ ] `tests/test_health.py` -- health endpoint test
- [ ] `tests/test_models.py` -- verify model definitions and table creation
- [ ] pytest-asyncio configuration in pyproject.toml (`asyncio_mode = "auto"`)

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Docker | Container orchestration | Yes | 29.1.4-rd | -- |
| Docker Compose | Service orchestration | Yes | 5.0.1 | -- |
| uv | Package management | Yes | 0.11.2 | -- |
| Python 3.13 | Runtime (host dev) | Yes | 3.13.12 (via homebrew) | -- |
| PostgreSQL | Database (Docker) | Via Docker image | 16-alpine | -- |
| Redis | Queue broker (Docker) | Via Docker image | 7-alpine | -- |

**Missing dependencies with no fallback:** None.

**Missing dependencies with fallback:** None.

Note: The host system has Python 3.14.3 as default `python3`. The project must pin Python 3.13 in `pyproject.toml` (`requires-python = ">=3.13,<3.14"`) and the Dockerfile must use `python:3.13-slim`.

## Open Questions

1. **pytest-asyncio mode configuration**
   - What we know: pytest-asyncio 1.3.0 supports `asyncio_mode = "auto"` in pyproject.toml
   - What's unclear: Whether the `event_loop` fixture scope handling has changed in this version
   - Recommendation: Use `asyncio_mode = "auto"` and test early. If issues arise, fall back to explicit `@pytest.mark.asyncio` decorators.

2. **Database test strategy: test database vs in-memory**
   - What we know: asyncpg requires a real PostgreSQL instance (no SQLite substitute)
   - What's unclear: Whether to require a running PostgreSQL for local tests or only in Docker
   - Recommendation: Tests require PostgreSQL running (via Docker). Use a separate `phaze_test` database. Document `docker compose up postgres` as a prerequisite for running tests locally.

## Sources

### Primary (HIGH confidence)
- [Alembic Naming Conventions](https://alembic.sqlalchemy.org/en/latest/naming.html) -- constraint naming patterns
- [SQLAlchemy Async Extension](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) -- async engine, session, usage patterns
- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) -- startup/shutdown lifecycle
- [pydantic-settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) -- configuration management

### Secondary (MEDIUM confidence)
- [Berk Karaal: FastAPI + Async SQLAlchemy 2 + Alembic + Docker](https://berkkaraal.com/blog/2024/09/19/setup-fastapi-project-with-async-sqlalchemy-2-alembic-postgresql-and-docker/) -- end-to-end project setup reference
- [DEV: Best Practices for Alembic and SQLAlchemy](https://dev.to/welel/best-practices-for-alembic-and-sqlalchemy-3b34) -- naming conventions, migration practices
- PyPI version verification (2026-03-27): fastapi 0.135.2, uvicorn 0.42.0, sqlalchemy 2.0.48, asyncpg 0.31.0, alembic 1.18.4, pydantic 2.12.5, pydantic-settings 2.13.1

### Tertiary (LOW confidence)
- None -- all findings verified against primary or secondary sources.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all versions verified against PyPI, well-established ecosystem
- Architecture: HIGH -- patterns from official documentation and verified community best practices
- Pitfalls: HIGH -- documented extensively in project research (PITFALLS.md, ARCHITECTURE.md)
- Database schema: HIGH -- designed in prior research phase, aligns with all v1 requirements

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (stable ecosystem, no fast-moving dependencies)
