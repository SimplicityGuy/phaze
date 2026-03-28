# Stack Research

**Domain:** Music file management, audio analysis, metadata extraction, AI-powered organization
**Researched:** 2026-03-27
**Confidence:** HIGH

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.13 | Runtime | Project constraint. All recommended libraries support 3.13. |
| FastAPI | >=0.135.2 | Web framework / API | De facto standard for async Python APIs. Native async, auto-generated OpenAPI docs, Pydantic integration, SSE support for real-time UI updates. Massive ecosystem and community. |
| SQLAlchemy | >=2.0.48 | ORM / database toolkit | Industry standard Python ORM. Full async support via `create_async_engine` + asyncpg driver. Declarative models, relationship management, migration support via Alembic. |
| asyncpg | >=0.30.0 | PostgreSQL async driver | Fastest Python PostgreSQL driver. Purpose-built for asyncio. Used as SQLAlchemy's async backend. |
| Alembic | >=1.18.4 | Database migrations | Official SQLAlchemy migration tool. Async template support (`alembic init -t async`). Autogenerate from model changes. |
| PostgreSQL | 16+ | Primary database | Project constraint. Handles 200K+ file metadata, complex queries, JSON columns for flexible metadata, full-text search for future features. |
| Redis | 7+ | Task queue broker / cache | Required by arq task queue. Also useful for caching analysis results and rate-limiting LLM API calls. |
| Docker Compose | 2.x | Deployment orchestration | Project constraint. Runs PostgreSQL, Redis, API server, worker processes as separate containers. |

### Audio / Music Libraries

| Library | Version | Purpose | Why Recommended |
|---------|---------|---------|-----------------|
| mutagen | >=1.47.0 | Audio metadata read/write | The standard for audio tag manipulation in Python. Supports ID3v1/v2, Vorbis, MP4, FLAC, OGG, AIFF. Zero dependencies. Read AND write capability needed for renaming workflows. |
| librosa | >=0.11.0 | Audio feature extraction (BPM, key, mood features) | Industry-standard MIR library. Beat tracking, tempo estimation, chroma features, spectral analysis. Python 3.13 support confirmed (requires `standard-aifc` and `standard-sunau` extras). |
| pyacoustid | >=1.3.0 | Audio fingerprinting | Python bindings for Chromaprint/AcoustID. Identifies tracks via acoustic fingerprint, enables deduplication of differently-named identical audio. Complements sha256 hash dedup. |
| chromaprint (system) | latest | Fingerprint generation | C library required by pyacoustid. Install via system package manager or include in Docker image. Provides `fpcalc` binary. |
| FFmpeg (system) | 8.x | Audio/video processing | Required by librosa for audio decoding. Also needed for video stream metadata extraction via ffprobe. Install in Docker image. |

### Web UI

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Jinja2 | >=3.1 | Server-side templating | Ships with FastAPI. Server-rendered HTML means no separate frontend build, no SPA complexity. Perfect for admin-only tool. |
| HTMX | 2.x (CDN) | Dynamic UI interactions | Eliminates need for React/Vue/Angular. Adds SPA-like interactivity (approve/reject buttons, live search, pagination) via HTML attributes. Zero build step. 90% of SPA functionality, 10% of complexity. |
| Tailwind CSS | 3.x (CDN) | Styling | Utility-first CSS. Use via CDN (no build step) for a single-user admin tool. DaisyUI component library optional for pre-built components. |
| Alpine.js | 3.x (CDN) | Lightweight JS interactions | 3KB library for dropdown menus, modals, toggling states. Complements HTMX for client-side state that HTMX doesn't handle. |

### Task Processing

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| arq | >=0.27.0 | Async task queue | Purpose-built for asyncio + Redis. 7x faster than RQ, simpler than Celery. Perfect for file analysis jobs (BPM, fingerprinting, metadata extraction). Supports retries with backoff, job results, cron jobs. Single-user app doesn't need Celery's complexity. |

### AI / LLM Integration

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| litellm | >=1.82.6 (pin exact) | Unified LLM API client | Single interface to 100+ LLM providers (OpenAI, Anthropic, local models). Avoids vendor lock-in. Use for filename/path proposals. **IMPORTANT:** Pin exact version due to March 2026 supply chain incident on versions 1.82.7-1.82.8. Verify checksums. |
| pydantic | >=2.10 | Data validation / LLM structured output | Already a FastAPI dependency. Use for validating LLM responses (proposed filenames, paths). Structured output parsing. |

### Configuration / Infrastructure

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| pydantic-settings | >=2.13.1 | Configuration management | Type-safe config from env vars, .env files, Docker secrets. Native Pydantic integration. Supports `SecretStr` for API keys. |
| uvicorn | >=0.34.0 | ASGI server | Standard production server for FastAPI. Use with `--workers` for multi-process or behind gunicorn for production. |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| uv | Package management | Project constraint. Fast, deterministic. Use `uv run` prefix for all commands. |
| ruff | Linting + formatting | Already configured in CLAUDE.md. Replaces flake8, black, isort. |
| mypy | Type checking | Already configured. Strict mode excluding tests. |
| pytest | Testing | With pytest-asyncio for async tests, pytest-cov for coverage. |
| pytest-asyncio | Async test support | Required for testing async endpoints, database operations, task queue jobs. |
| httpx | HTTP test client | FastAPI's recommended test client. Use `AsyncClient` for async endpoint testing. |
| pre-commit | Git hooks | Already configured in CLAUDE.md. |

## Installation

```bash
# Core application
uv add fastapi uvicorn sqlalchemy asyncpg alembic pydantic-settings redis arq jinja2

# Audio processing
uv add mutagen librosa pyacoustid

# AI integration
uv add litellm

# Dev dependencies
uv add --dev pytest pytest-asyncio pytest-cov httpx mypy ruff pre-commit

# System dependencies (Dockerfile)
# apt-get install -y ffmpeg chromaprint-tools
```

Note: librosa on Python 3.13 requires additional packages:
```bash
uv add standard-aifc standard-sunau
```

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| FastAPI | Litestar | If you want more explicit DI and slightly lower memory usage. FastAPI wins on ecosystem size, docs quality, and community support. |
| SQLAlchemy | SQLModel | If models are simple and you want less boilerplate. SQLModel is a thin FastAPI-aligned wrapper over SQLAlchemy but has fewer features and weaker async story. Stick with SQLAlchemy for a 200K-record system. |
| arq | Celery | If you need multi-broker support, complex routing, or canvas workflows. Overkill for a single-user app. Celery's config complexity is not justified here. |
| arq | Dramatiq | If you want RabbitMQ support or more mature retry/middleware. Dramatiq is sync-first which conflicts with our async stack. |
| HTMX + Jinja2 | React/Vue SPA | If you need offline capability, complex client-side state, or multiple developers on frontend. A single-user admin tool does not need SPA complexity or a separate build pipeline. |
| litellm | Direct OpenAI SDK | If you are committed to a single LLM provider forever. litellm provides flexibility to switch between local/cloud models with zero code changes. |
| mutagen | tinytag | If you only need read-only metadata. We need write capability to update tags after renaming, so mutagen is required. |
| librosa | essentia | If you need real-time audio processing or more algorithms. Essentia has a harder install (C++ compilation) and worse Python 3.13 story. Librosa covers BPM, key detection, and spectral features adequately. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| ffmpeg-python (pip: `ffmpeg-python`) | Last PyPI release was 2022. Effectively abandoned. 500+ open issues on GitHub. | Use `subprocess.run(["ffprobe", ...])` directly for metadata extraction. Or `python-ffmpeg` (pip: `python-ffmpeg`) which is actively maintained. |
| SQLite | Cannot handle concurrent writes from multiple worker processes analyzing files in parallel. No JSON operators for flexible metadata queries. | PostgreSQL (project constraint). |
| Celery | Massive dependency tree, complex configuration, sync-first design. Overkill for single-user app with Redis already in stack. | arq for async task queue. |
| Django | Full MVC framework with ORM, admin, auth -- all unnecessary when you have FastAPI + SQLAlchemy + custom admin UI. Sync-first design conflicts with async processing needs. | FastAPI. |
| LangChain | Enormous abstraction layer for LLM calls. This project just needs "send prompt, get structured response." LangChain adds complexity without benefit for simple classification/naming tasks. | litellm for provider abstraction + raw Pydantic for structured output. |
| React/Next.js | Requires separate build pipeline, Node.js in Docker, npm dependencies. Completely unnecessary for a single-user admin approval UI. | HTMX + Jinja2 + Tailwind CSS via CDN. |
| tinytag | Read-only metadata extraction. Cannot write updated tags back to files after renaming. | mutagen for read+write. |
| psycopg2 | Sync driver. Blocks the event loop. Cannot be used with async SQLAlchemy. | asyncpg for async PostgreSQL access. |

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| SQLAlchemy >=2.0.48 | asyncpg >=0.30.0 | Use `postgresql+asyncpg://` connection string. Some older asyncpg versions (0.29.x) had issues with `create_async_engine`. |
| librosa >=0.11.0 | Python 3.13 | Requires `standard-aifc` and `standard-sunau` pip packages on Python 3.13 (stdlib modules removed in 3.13). |
| FastAPI >=0.135.2 | Pydantic >=2.10 | FastAPI requires Pydantic v2. Do not install Pydantic v1. |
| FastAPI >=0.135.2 | Starlette >=0.46.0 | Pinned by FastAPI. Do not override. |
| Alembic >=1.18.4 | SQLAlchemy >=2.0 | Use `alembic init -t async` for async template. Import all models in `env.py` for autogenerate to work. |
| litellm | ALL | **Pin exact version.** Supply chain attack on 1.82.7/1.82.8 (March 2026). Use >=1.82.6,<1.82.7 or wait for verified post-incident release. Verify SHA checksums. |
| arq >=0.27.0 | Redis 7+ | arq is in maintenance mode but stable. No breaking changes expected. |
| pyacoustid >=1.3.0 | chromaprint (system) | Requires `fpcalc` binary on PATH. Install `chromaprint-tools` in Docker. |

## Confidence Assessment

| Area | Confidence | Reasoning |
|------|------------|-----------|
| Web framework (FastAPI) | HIGH | Verified current version, massive ecosystem, well-documented async patterns |
| Database (SQLAlchemy + asyncpg + Alembic) | HIGH | Standard production stack, verified versions, extensive async documentation |
| Audio metadata (mutagen) | HIGH | No real alternative for read+write. Stable, zero-dependency, widely used |
| Audio analysis (librosa) | HIGH | Industry standard MIR library, Python 3.13 confirmed with known workarounds |
| Audio fingerprinting (pyacoustid) | MEDIUM | Library works but hasn't released since 2023. Stable API, low maintenance risk, but monitor |
| Task queue (arq) | MEDIUM | Excellent fit for async stack but in "maintenance only" mode. If it becomes truly abandoned, migrate to taskiq or Dramatiq |
| LLM integration (litellm) | MEDIUM | Best abstraction layer but recent supply chain incident is concerning. Pin versions aggressively, verify checksums |
| Web UI (HTMX + Jinja2) | HIGH | Well-proven pattern for Python admin tools. No build step, no JS framework complexity |

## Sources

- [mutagen on PyPI](https://pypi.org/project/mutagen/) -- version 1.47.0 verified
- [librosa on PyPI](https://pypi.org/project/librosa/) -- version 0.11.0, Python 3.13 support confirmed
- [librosa Python 3.13 issue](https://github.com/librosa/librosa/issues/1883) -- compatibility workarounds documented
- [pyacoustid on PyPI](https://pypi.org/project/pyacoustid/) -- version 1.3.0 verified
- [FastAPI releases](https://github.com/fastapi/fastapi/releases) -- version 0.135.2 verified
- [SQLAlchemy on PyPI](https://pypi.org/project/SQLAlchemy/) -- version 2.0.48 verified
- [Alembic on PyPI](https://pypi.org/project/alembic/) -- version 1.18.4 verified
- [arq on PyPI](https://pypi.org/project/arq/) -- version 0.27.0, maintenance mode noted
- [litellm security incident](https://docs.litellm.ai/blog/security-update-march-2026) -- supply chain attack March 2026
- [pydantic-settings on PyPI](https://pypi.org/project/pydantic-settings/) -- version 2.13.1 verified
- [HTMX + FastAPI patterns](https://johal.in/htmx-fastapi-patterns-hypermedia-driven-single-page-applications-2025/) -- 2025 production patterns
- [Python task queue benchmarks](https://stevenyue.com/blogs/exploring-python-task-queue-libraries-with-load-test) -- arq/dramatiq/huey performance comparison

---
*Stack research for: Music file management and AI-powered organization*
*Researched: 2026-03-27*
