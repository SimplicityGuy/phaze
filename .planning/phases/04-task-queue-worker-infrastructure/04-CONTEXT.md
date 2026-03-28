# Phase 4: Task Queue & Worker Infrastructure - Context

**Gathered:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Set up arq + Redis as the task queue system with a bounded worker pool. Workers process one-file-at-a-time jobs with configurable concurrency, retry with backoff on failure, and use a process pool for CPU-bound audio analysis. This is pure infrastructure — no analysis logic yet (that's Phase 5).

</domain>

<decisions>
## Implementation Decisions

### Worker Concurrency Model
- **D-01:** Claude's discretion on worker topology. Recommended approach: single arq worker process with configurable `max_jobs` setting (default 8). Scale by adjusting `max_jobs` or adding Docker container replicas. Keep docker-compose simple with one worker service.

### Task Granularity
- **D-02:** One arq job per file. Each file gets its own job for maximum parallelism, granular retry tracking, and simple progress visibility. ~200K jobs is well within arq + Redis capacity.

### Retry & Failure Handling
- **D-03:** 3 retries with exponential backoff using arq's built-in retry mechanism. After 3 failures, mark the job as permanently failed. Failed jobs are stored for human review via the future approval UI (Phase 7). No unlimited retries.

### Process Pool for CPU Work
- **D-04:** CPU-bound work (librosa audio analysis in Phase 5) must not block the async event loop. Use `asyncio.get_event_loop().run_in_executor(ProcessPoolExecutor)` inside arq job functions. ProcessPoolExecutor size configurable via settings.

### Claude's Discretion
- Worker topology details (single vs multiple containers)
- arq WorkerSettings configuration (health_check_interval, job_timeout, queue names)
- Redis connection pooling strategy
- How backpressure is signaled (arq's max_jobs handles this naturally)
- Settings additions to config.py for worker/queue tuning
- Whether to add a lightweight job status tracking table or rely on arq's Redis-based job results

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, code quality rules, arq selected as task queue
- `.planning/PROJECT.md` — Project vision, constraints (200K files, Docker Compose deployment)
- `.planning/REQUIREMENTS.md` — INF-02 (task queue), ANL-03 (parallel analysis)

### Existing Code
- `src/phaze/config.py` — Settings with `redis_url` already configured
- `src/phaze/database.py` — Async engine pattern to follow
- `src/phaze/main.py` — FastAPI app (may need startup/shutdown hooks for worker)
- `docker-compose.yml` — Worker service placeholder, Redis service already running

### Prior Phase Context
- `.planning/phases/01-infrastructure-project-setup/01-CONTEXT.md` — D-02 (separate worker layer), D-07 (everything in Docker)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `Settings` class in `config.py` — already has `redis_url`, extend with worker settings
- `docker-compose.yml` — worker service stubbed (`echo "Worker placeholder - arq added in Phase 4"`), Redis with healthcheck
- Async patterns throughout codebase (SQLAlchemy async, FastAPI async endpoints)

### Established Patterns
- Pydantic settings for configuration (`pydantic_settings.BaseSettings`)
- Service layer pattern (`src/phaze/services/`) for business logic
- Alembic for database migrations

### Integration Points
- Replace worker placeholder command in docker-compose.yml with real arq worker
- Add arq dependency to pyproject.toml
- Add worker settings to config.py
- Wire arq startup in FastAPI lifespan (for enqueuing jobs from API)
- New `src/phaze/worker.py` or `src/phaze/tasks/` for task definitions

</code_context>

<specifics>
## Specific Ideas

- Worker must handle ~200K files — arq + Redis handles this volume fine with one-per-file jobs
- CPU-bound audio analysis (Phase 5) needs process pool — infrastructure set up here, used there
- Single-user system — no multi-tenancy concerns for queue isolation

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 04-task-queue-worker-infrastructure*
*Context gathered: 2026-03-28*
