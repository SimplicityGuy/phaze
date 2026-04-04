# Phase 4: Task Queue & Worker Infrastructure - Context

**Gathered:** 2026-03-28
**Updated:** 2026-03-28
**Status:** Ready for planning

<domain>
## Phase Boundary

Set up arq + Redis as the task queue system with a bounded worker pool. Workers process one-file-at-a-time jobs with configurable concurrency, retry with backoff on failure, and use a process pool for CPU-bound audio analysis. This is pure infrastructure — no analysis logic yet (that's Phase 5).

</domain>

<decisions>
## Implementation Decisions

### Worker Concurrency & Topology
- **D-01:** Single arq worker process with configurable max_jobs (default 8). Scale by adjusting max_jobs or adding Docker container replicas.
- **D-02:** ProcessPoolExecutor created in on_startup hook, shut down in on_shutdown hook. CPU-bound work runs via asyncio run_in_executor.
- **D-03:** ArqRedis connection pool wired into FastAPI lifespan for job enqueuing from API endpoints (app.state.arq_pool).

### Task Granularity
- **D-04:** One arq job per file. ~200K jobs is within arq + Redis capacity. Provides granular retry and progress tracking.

### Retry & Failure Handling
- **D-05:** 3 retries with exponential backoff. Analysis jobs: job_try * 5s. LLM proposal jobs: job_try * 10s (slower for rate limit recovery).
- **D-06:** After 3 failures, job marked permanently failed. Failed files visible in DB via state field for manual review.

### Claude's Discretion
- arq WorkerSettings configuration details (health_check_interval, job_timeout, queue names)
- Redis connection pooling strategy
- Settings additions to config.py for worker/queue tuning
- Docker compose worker service command

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project Configuration
- `CLAUDE.md` — Development setup, arq selected as task queue
- `.planning/REQUIREMENTS.md` — INF-02 (task queue), ANL-03 (parallel analysis)

### Existing Code
- `src/phaze/config.py` — Settings with redis_url
- `src/phaze/main.py` — FastAPI app lifespan (add ArqRedis pool)
- `docker-compose.yml` — Worker service, Redis service

### Prior Phase Context
- `.planning/phases/01-infrastructure-project-setup/01-CONTEXT.md` — D-01 (Docker services), D-08 (layer-based structure)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- Settings class with redis_url
- Docker Compose with Redis healthcheck
- Async patterns throughout codebase

### Established Patterns
- Pydantic settings for configuration
- Service layer pattern
- Lifespan context manager for startup/shutdown

### Integration Points
- New tasks/ package with worker.py, functions.py, pool.py
- ArqRedis pool on app.state for enqueuing
- Worker service command in docker-compose.yml

</code_context>

<specifics>
## Specific Ideas

- Single-user system — no multi-tenancy or queue isolation needed
- ~200K files for processing — one job per file
- CPU-bound audio analysis needs process pool (Phase 5 uses it)

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 04-task-queue-worker-infrastructure*
*Context gathered: 2026-03-28*
*Context updated: 2026-03-28*
