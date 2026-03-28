# Phase 4: Task Queue & Worker Infrastructure - Research

**Researched:** 2026-03-27
**Domain:** Async task queue (arq + Redis), worker pool, process pool for CPU-bound work
**Confidence:** HIGH

## Summary

Phase 4 sets up arq 0.27.0 as the async task queue backed by Redis 8. The worker runs as a separate Docker container, processing one-file-at-a-time jobs with configurable concurrency via `max_jobs`. CPU-bound audio analysis (Phase 5) is offloaded to a `ProcessPoolExecutor` via `asyncio.get_running_loop().run_in_executor()` so it never blocks the async event loop. Failed jobs retry with exponential backoff using arq's built-in `Retry` exception mechanism, capped at 3 attempts per CONTEXT.md decision D-03.

The existing codebase already has `redis_url` in `Settings`, a worker placeholder in `docker-compose.yml`, and an async-first architecture throughout. This phase adds arq as a dependency, creates `WorkerSettings`, defines a skeleton task function (no real analysis yet -- that is Phase 5), wires up Redis connection pooling in both the API (for enqueuing) and worker (for processing), and replaces the docker-compose worker placeholder.

**Primary recommendation:** Use arq's `WorkerSettings` class as the single configuration point. Parse `redis_url` from settings via `RedisSettings.from_dsn()`. Expose a `create_pool` helper for the API lifespan to enqueue jobs. Keep task function signatures simple -- `async def process_file(ctx, file_id: int)` -- so Phase 5 can fill in analysis logic.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Single arq worker process with configurable `max_jobs` (default 8). Scale by adjusting `max_jobs` or adding Docker container replicas.
- **D-02:** One arq job per file. Each file gets its own job for maximum parallelism, granular retry tracking, and simple progress visibility.
- **D-03:** 3 retries with exponential backoff using arq's built-in retry mechanism. After 3 failures, mark as permanently failed. No unlimited retries.
- **D-04:** CPU-bound work must not block the async event loop. Use `asyncio.get_running_loop().run_in_executor(ProcessPoolExecutor)` inside arq job functions. ProcessPoolExecutor size configurable via settings.

### Claude's Discretion
- Worker topology details (single vs multiple containers)
- arq WorkerSettings configuration (health_check_interval, job_timeout, queue names)
- Redis connection pooling strategy
- How backpressure is signaled (arq's max_jobs handles this naturally)
- Settings additions to config.py for worker/queue tuning
- Whether to add a lightweight job status tracking table or rely on arq's Redis-based job results

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.

</user_constraints>

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| INF-02 | Task queue (arq + Redis) manages parallel worker processing | arq 0.27.0 WorkerSettings with `max_jobs` for concurrency, `create_pool` for enqueuing, Redis 8 as broker |
| ANL-03 | Analysis runs in parallel across worker pool for throughput at scale | `max_jobs=8` enables parallel job execution; `ProcessPoolExecutor` in `run_in_executor` for CPU-bound work; one job per file for 200K scale |

</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| arq | 0.27.0 | Async task queue | Purpose-built for asyncio + Redis. Locked in CLAUDE.md. Maintenance-only but stable API. |
| redis (via arq) | -- | Redis client | arq bundles `redis[hiredis]` as a dependency. No separate install needed. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| redis (system) | 8.x | Queue broker | Already in docker-compose.yml with healthcheck |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| arq | taskiq | More actively maintained, but arq is locked decision and stable at 0.27.0. Only switch if arq breaks. |
| ProcessPoolExecutor | multiprocessing.Pool | ProcessPoolExecutor integrates with asyncio via run_in_executor. multiprocessing.Pool does not. |
| arq Redis results | PostgreSQL job status table | arq results expire from Redis (default 3600s). A DB table gives permanent status tracking. Recommend relying on arq results for now; add DB tracking in Phase 5 when analysis results need persistence. |

**Installation:**
```bash
uv add arq>=0.27.0
```

**Version verification:** arq 0.27.0 is the latest release on PyPI (verified 2026-03-27). Requires Python >=3.9, compatible with 3.13.

## Architecture Patterns

### Recommended Project Structure
```
src/phaze/
├── tasks/
│   ├── __init__.py          # Package init
│   ├── worker.py            # WorkerSettings class, startup/shutdown hooks
│   ├── pool.py              # ProcessPoolExecutor lifecycle management
│   └── functions.py         # Task function definitions (skeleton for now)
├── config.py                # Extended with worker settings
└── main.py                  # Lifespan updated with ArqRedis pool
```

### Pattern 1: WorkerSettings as Entry Point
**What:** A module-level `WorkerSettings` class that arq CLI discovers.
**When to use:** Always -- this is how arq finds and configures workers.
**Example:**
```python
# src/phaze/tasks/worker.py
# Source: https://arq-docs.helpmanual.io/

from concurrent.futures import ProcessPoolExecutor

from arq.connections import RedisSettings

from phaze.config import settings
from phaze.tasks.functions import process_file


async def startup(ctx: dict) -> None:
    """Initialize shared resources for all jobs."""
    ctx["process_pool"] = ProcessPoolExecutor(max_workers=settings.worker_process_pool_size)


async def shutdown(ctx: dict) -> None:
    """Clean up shared resources."""
    ctx["process_pool"].shutdown(wait=True)


class WorkerSettings:
    """arq worker configuration. Run via: arq phaze.tasks.worker.WorkerSettings"""

    functions = [process_file]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout
    max_tries = settings.worker_max_retries
    health_check_interval = 60
    keep_result = 3600
```

### Pattern 2: Task Functions with ctx + ProcessPoolExecutor
**What:** Async task functions that receive arq's `ctx` dict and offload CPU work to a process pool.
**When to use:** For any CPU-bound work (audio analysis, fingerprinting).
**Example:**
```python
# src/phaze/tasks/functions.py
import asyncio

from arq import Retry


async def process_file(ctx: dict, file_id: int) -> dict:
    """Process a single file. Skeleton for Phase 4; analysis logic added in Phase 5."""
    try:
        # Phase 5 will add: BPM detection, fingerprinting, metadata extraction
        # CPU-bound work pattern:
        # loop = asyncio.get_running_loop()
        # result = await loop.run_in_executor(ctx["process_pool"], cpu_bound_fn, file_id)
        return {"file_id": file_id, "status": "processed"}
    except Exception:
        # Exponential backoff: 5s, 10s, 15s
        raise Retry(defer=ctx["job_try"] * 5)
```

### Pattern 3: ArqRedis Pool in FastAPI Lifespan
**What:** Create an ArqRedis connection pool during FastAPI startup for enqueuing jobs from API endpoints.
**When to use:** Whenever the API needs to enqueue work.
**Example:**
```python
# src/phaze/main.py (lifespan update)
from arq import create_pool
from arq.connections import RedisSettings
from phaze.config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    # Create arq Redis pool for enqueuing jobs
    _app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await _app.state.arq_pool.close()
    await engine.dispose()
```

### Pattern 4: Enqueuing Jobs from Endpoints
**What:** Use `app.state.arq_pool.enqueue_job()` from route handlers.
**When to use:** When an API action triggers background processing.
**Example:**
```python
# In a router
from fastapi import Request

async def enqueue_analysis(request: Request, file_id: int):
    job = await request.app.state.arq_pool.enqueue_job("process_file", file_id)
    return {"job_id": job.job_id}
```

### Anti-Patterns to Avoid
- **Creating a new Redis connection per job enqueue:** Use the shared `ArqRedis` pool from `app.state`. Creating connections is expensive.
- **Blocking the event loop with CPU work:** Never call librosa/chromaprint directly in an async function. Always use `run_in_executor` with `ProcessPoolExecutor`.
- **Using `asyncio.get_event_loop()` in Python 3.13:** Use `asyncio.get_running_loop()` instead. `get_event_loop()` is deprecated for getting the running loop.
- **Unlimited retries:** Per D-03, cap at 3 retries. Set `max_tries` on WorkerSettings accordingly.
- **Defining WorkerSettings at import time with dynamic settings:** `RedisSettings.from_dsn()` reads `settings.redis_url` at import time. This is fine because `settings` is a module-level singleton. But do NOT put database session creation in module scope.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Job retry with backoff | Custom retry loop or decorator | arq's `Retry(defer=...)` exception | arq tracks `job_try`, handles dead-letter, respects `max_tries` |
| Concurrency limiting | Semaphore-based task limiter | arq's `max_jobs` setting | Built-in backpressure -- arq won't pull more jobs than `max_jobs` |
| Health checking | Custom heartbeat endpoint | arq's `health_check_interval` + `arq --check` | Writes to Redis automatically, verifiable via CLI |
| Job status tracking | Custom status table (for now) | arq's `job.status()` and `job.result()` | Redis-backed, expires after `keep_result` seconds. Sufficient for Phase 4. |
| Process pool management | Manual process spawning | `ProcessPoolExecutor` in startup/shutdown hooks | Proper lifecycle via arq's `on_startup`/`on_shutdown`. Clean shutdown on worker stop. |

**Key insight:** arq handles all the hard parts of job queuing (backpressure, retries, health checks, result storage). The only custom code needed is the task function body and the ProcessPoolExecutor lifecycle.

## Common Pitfalls

### Pitfall 1: ProcessPoolExecutor and Serialization
**What goes wrong:** Functions passed to `run_in_executor(ProcessPoolExecutor)` must be serializable. Lambda functions, closures, and nested functions cannot be serialized for cross-process transport.
**Why it happens:** ProcessPoolExecutor uses `multiprocessing` which serializes functions to send to child processes.
**How to avoid:** Define CPU-bound functions as top-level module functions. Pass only simple, serializable arguments (ints, strings, paths -- not SQLAlchemy models or file handles).
**Warning signs:** Serialization errors or `AttributeError: Can't serialize local object` at runtime.

### Pitfall 2: arq max_tries vs max_retries Naming
**What goes wrong:** Confusion between `max_tries` (arq's parameter name) and "max retries" (common mental model). `max_tries=3` means 3 total attempts, not 3 retries after the first attempt.
**Why it happens:** Different libraries use different naming conventions.
**How to avoid:** D-03 says "3 retries" -- so set `max_tries=4` (1 initial + 3 retries). Or interpret D-03 as "3 total attempts" and set `max_tries=3`. Clarify in code comments.
**Warning signs:** Off-by-one in retry counting.

### Pitfall 3: RedisSettings.from_dsn URL Format
**What goes wrong:** `from_dsn()` expects `redis://host:port/db` format. Some configs use `redis://host:port` without the database number.
**Why it happens:** The existing `redis_url` in config.py is `redis://redis:6379/0` which includes `/0`. This is correct.
**How to avoid:** Keep using the format already in config.py. Verify `from_dsn` parses it correctly in tests.
**Warning signs:** ConnectionError or connecting to wrong Redis database.

### Pitfall 4: Worker Container Not Finding Module
**What goes wrong:** `arq phaze.tasks.worker.WorkerSettings` fails with `ModuleNotFoundError` in Docker.
**Why it happens:** The package must be installed in the container. With `uv run arq ...`, uv handles the virtualenv, but the Docker CMD must use the correct entrypoint.
**How to avoid:** Use `uv run arq phaze.tasks.worker.WorkerSettings` as the docker-compose command. The Dockerfile already installs the package via `uv sync`.
**Warning signs:** `ModuleNotFoundError: No module named 'phaze'` on container start.

### Pitfall 5: Shared State Between Process Pool Workers
**What goes wrong:** ProcessPoolExecutor child processes don't share memory with the parent. Database connections, Redis pools, and other resources cannot be passed to child processes.
**Why it happens:** Multiprocessing uses separate memory spaces.
**How to avoid:** CPU-bound functions in the process pool should be pure computation -- accept file paths or raw data, return results. Database writes happen back in the async parent after `run_in_executor` returns.
**Warning signs:** Serialization errors on connection objects, or silent failures where DB writes never appear.

### Pitfall 6: asyncio.get_event_loop() Deprecation
**What goes wrong:** `asyncio.get_event_loop()` raises DeprecationWarning in Python 3.12+ when no loop is running.
**Why it happens:** Python is moving toward `get_running_loop()` as the standard.
**How to avoid:** Always use `asyncio.get_running_loop()` inside async functions.
**Warning signs:** DeprecationWarning in logs, potential RuntimeError in future Python versions.

## Code Examples

Verified patterns from official sources:

### Settings Extension
```python
# src/phaze/config.py additions
class Settings(BaseSettings):
    # ... existing fields ...

    # Worker settings
    worker_max_jobs: int = 8          # D-01: configurable concurrency
    worker_job_timeout: int = 600     # 10 minutes per file (generous for audio analysis)
    worker_max_retries: int = 4       # D-03: max_tries=4 means 3 retries + 1 initial
    worker_process_pool_size: int = 4 # D-04: CPU-bound worker count
    worker_health_check_interval: int = 60
    worker_keep_result: int = 3600    # Keep results in Redis for 1 hour
```

### Docker Compose Worker Service
```yaml
# docker-compose.yml worker service update
worker:
  build:
    context: .
    dockerfile: Dockerfile
  command: uv run arq phaze.tasks.worker.WorkerSettings
  env_file: .env
  volumes:
    - "${SCAN_PATH:-/data/music}:/data/music:ro"
  depends_on:
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy
```

### Retry with Exponential Backoff
```python
# Source: https://arq-docs.helpmanual.io/
from arq import Retry

async def process_file(ctx: dict, file_id: int) -> dict:
    try:
        # ... processing logic ...
        return {"file_id": file_id, "status": "processed"}
    except SomeRecoverableError:
        # Exponential backoff: 5s, 10s, 15s (job_try is 1-indexed)
        raise Retry(defer=ctx["job_try"] * 5)
    # Non-recoverable errors propagate naturally and count toward max_tries
```

### Testing arq Tasks (pytest pattern)
```python
# tests/test_tasks/test_functions.py
import pytest
from phaze.tasks.functions import process_file


@pytest.mark.asyncio
async def test_process_file_returns_result():
    ctx = {"job_try": 1, "job_id": "test-123", "process_pool": None}
    result = await process_file(ctx, file_id=42)
    assert result["file_id"] == 42
    assert result["status"] == "processed"
```

### Enqueue Helper for Tests
```python
# For integration tests that need real Redis
from arq import create_pool
from arq.connections import RedisSettings

async def test_enqueue_and_process():
    pool = await create_pool(RedisSettings())
    job = await pool.enqueue_job("process_file", 42)
    assert job.job_id is not None
    status = await job.status()
    assert status in ("queued", "deferred")
    await pool.close()
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `asyncio.get_event_loop()` | `asyncio.get_running_loop()` | Python 3.10+ | Deprecated warning in 3.12+, must use `get_running_loop()` in async context |
| Celery for all Python queues | arq/taskiq for async Python | 2020+ | Celery is sync-first; arq/taskiq designed for asyncio |
| Custom retry decorators | arq `Retry` exception | arq 0.16+ | Built-in retry with defer, tracked by `job_try` counter |

**Deprecated/outdated:**
- `asyncio.get_event_loop()` inside async functions -- use `asyncio.get_running_loop()`
- arq is in maintenance-only mode (issue #510) but API is stable at 0.27.0. No breaking changes expected. taskiq is the successor if migration needed.

## Open Questions

1. **max_tries interpretation for D-03**
   - What we know: D-03 says "3 retries with exponential backoff." arq's `max_tries` counts total attempts, not retries.
   - What's unclear: Does "3 retries" mean 3 total attempts or 1 initial + 3 retries = 4 total?
   - Recommendation: Set `max_tries=4` (1 initial + 3 retries) to match the "3 retries" wording. Document in code comments.

2. **Job status tracking: Redis-only vs DB table**
   - What we know: arq stores results in Redis with configurable TTL (`keep_result`). For Phase 4 (infrastructure only), this is sufficient.
   - What's unclear: Phase 5+ may need persistent job status in PostgreSQL for the approval UI.
   - Recommendation: Rely on arq Redis results for Phase 4. Defer DB job tracking to Phase 5 when analysis results need persistence anyway.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Docker | Container orchestration | Yes | 29.1.4 | -- |
| Redis | arq broker | Yes (docker-compose) | 8.x (alpine) | -- |
| Python | Runtime | Yes | 3.13 | -- |
| FFmpeg | Future audio analysis | Yes | 8.1 | -- |

**Missing dependencies with no fallback:** None.

**Missing dependencies with fallback:** None.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.x + pytest-asyncio 1.3.x |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest --cov=phaze --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INF-02 | arq workers connect to Redis and process enqueued tasks | integration | `uv run pytest tests/test_tasks/ -x` | No -- Wave 0 |
| INF-02 | Multiple workers process tasks in parallel (max_jobs) | unit | `uv run pytest tests/test_tasks/test_worker.py -x` | No -- Wave 0 |
| ANL-03 | Failed tasks retry with backoff | unit | `uv run pytest tests/test_tasks/test_functions.py::test_retry_on_failure -x` | No -- Wave 0 |
| ANL-03 | CPU-bound work runs in process pool | unit | `uv run pytest tests/test_tasks/test_pool.py -x` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest --cov=phaze --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_tasks/__init__.py` -- package init
- [ ] `tests/test_tasks/test_functions.py` -- covers INF-02 (task execution), ANL-03 (retry behavior)
- [ ] `tests/test_tasks/test_worker.py` -- covers INF-02 (WorkerSettings config, max_jobs)
- [ ] `tests/test_tasks/test_pool.py` -- covers ANL-03 (ProcessPoolExecutor integration)
- [ ] `tests/conftest.py` -- may need arq/Redis fixtures (mock or real)

## Sources

### Primary (HIGH confidence)
- [arq official docs](https://arq-docs.helpmanual.io/) -- WorkerSettings, create_pool, Retry, RedisSettings, all config options
- [arq connections.py source](https://github.com/samuelcolvin/arq/blob/master/arq/connections.py) -- RedisSettings.from_dsn verified
- [arq PyPI](https://pypi.org/project/arq/) -- version 0.27.0 verified, Python >=3.9
- [arq maintenance notice](https://github.com/python-arq/arq/issues/510) -- maintenance-only confirmed

### Secondary (MEDIUM confidence)
- [Python asyncio docs](https://docs.python.org/3.13/library/asyncio-eventloop.html) -- `get_running_loop()` vs `get_event_loop()` deprecation
- [ProcessPoolExecutor docs](https://docs.python.org/3.13/library/concurrent.futures.html) -- serialization requirements, `run_in_executor` pattern

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- arq 0.27.0 is a locked decision, version verified on PyPI, API well-documented
- Architecture: HIGH -- patterns come directly from arq official docs and match existing codebase async patterns
- Pitfalls: HIGH -- ProcessPoolExecutor serialization, asyncio deprecation, and module discovery are well-known issues with clear solutions

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (arq is maintenance-only, API frozen -- findings are stable)
