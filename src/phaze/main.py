"""FastAPI application factory with lifespan management."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import redis.asyncio as redis_async
from saq import Queue
from sqlalchemy import text

from phaze.config import settings
from phaze.database import async_session, engine, run_migrations
from phaze.logging_config import configure_logging
from phaze.routers import (
    admin_agents,
    agent_analysis,
    agent_exec_batches,
    agent_execution,
    agent_files,
    agent_fingerprint,
    agent_heartbeat,
    agent_identity,
    agent_metadata,
    agent_proposals,
    agent_scan_batches,
    agent_tracklists,
    companion,
    cue,
    duplicates,
    execution,
    health,
    pipeline,
    pipeline_scans,
    preview,
    proposals,
    scan,
    search,
    tags,
    tracklists,
)
from phaze.services.agent_bootstrap import ensure_dev_agent
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifespan: verify DB; create SAQ queue + task_router + redis on startup; dispose on shutdown.

    Resources wired (in order):
        1. ``app.state.controller_queue`` -- the named ``controller`` SAQ Queue consumed by the
                                         application-server ``phaze-worker``. Phase 30 replaced the
                                         Phase 25 unnamed default queue here: that queue had NO
                                         consumer, so every control-plane enqueue routed to it was
                                         stranded (the v4.0.6 incident). Registers the project
                                         ``apply_project_job_defaults`` before_enqueue hook so jobs
                                         inherit the policy timeout/retry budget (mirrors controller.py).
        2. ``app.state.task_router``  -- Phase 26 D-20 AgentTaskRouter for per-agent SAQ enqueues
                                         (used by agent_files.py auto-enqueue path).
        3. ``app.state.redis``        -- Phase 26 D-27 shared async Redis client for the tracklists
                                         idempotency cache (decode_responses=True so .get/.set yield str).

    Shutdown is reverse-order (task_router, redis, controller_queue) so the SAQ-backed router
    closes before the underlying controller queue's Redis pool is gone.

    Phase 27 UAT Gap 2/3: ``run_migrations`` and ``ensure_dev_agent`` run BEFORE
    the queue / task_router / redis are wired so a fresh docker compose stack
    can boot to a working state with one ``docker compose up``. Both are gated
    by config knobs (``settings.auto_migrate`` and ``settings.dev_seed_agent``)
    so operators can opt out in production.
    """
    # PR3 observability: configure the central structlog pipeline FIRST -- before
    # run_migrations() or any DB access -- so every startup log line (including
    # migration / connectivity failures) flows through the JSON/console pipeline.
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    # Phase 27 UAT Gap 2: bring the schema to head BEFORE the engine is used
    # for normal traffic. Idempotent + gated by settings.auto_migrate.
    await run_migrations()

    # Verify connectivity. The SELECT 1 also raises early with a clear error if
    # the database is unreachable -- the lifespan crash surface aborts FastAPI
    # startup instead of letting routers 500 later.
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))

    # Phase 27 UAT Gap 3: seed a dev agent on a fresh agents table so the
    # watcher can authenticate on first start. No-op on existing deployments
    # (idempotent) and disabled by default (settings.dev_seed_agent=false).
    async with async_session() as bootstrap_session:
        await ensure_dev_agent(bootstrap_session)

    # Named controller queue (Phase 30) -- consumed by the application-server phaze-worker
    # (phaze.tasks.controller.settings). Replaces the Phase 25 unnamed default queue, which had
    # no consumer and stranded every control-plane enqueue routed to it (the v4.0.6 incident).
    _app.state.controller_queue = Queue.from_url(settings.redis_url, name="controller")
    # Register the policy before_enqueue hook so API-side enqueues inherit the longer
    # timeout / retry budget, exactly like controller.py's module-level queue.
    _app.state.controller_queue.register_before_enqueue(apply_project_job_defaults)
    # AgentTaskRouter -- per-agent SAQ enqueuer (Phase 26 Plan 04, D-20)
    _app.state.task_router = AgentTaskRouter(redis_url=settings.redis_url)
    # Shared Redis client for tracklists idempotency cache (Phase 26 Plan 07, D-27).
    # decode_responses=True so .get/.set return str (matches agent_tracklists.py expectations).
    _app.state.redis = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)
    yield
    # Shutdown in reverse construction order.
    await _app.state.task_router.close()
    await _app.state.redis.aclose()
    await _app.state.controller_queue.disconnect()
    await engine.dispose()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Phaze", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(scan.router)
    app.include_router(companion.router)
    app.include_router(proposals.router)
    app.include_router(execution.router)
    app.include_router(preview.router)
    app.include_router(duplicates.router)
    app.include_router(tracklists.router)
    app.include_router(pipeline.router)
    app.include_router(search.router)
    app.include_router(tags.router)
    app.include_router(cue.router)
    # Phase 25 internal-agent routers (D-10)
    app.include_router(agent_files.router)
    app.include_router(agent_metadata.router)
    app.include_router(agent_fingerprint.router)
    app.include_router(agent_execution.router)
    app.include_router(agent_heartbeat.router)
    # Phase 26 internal-agent routers (D-15, D-26, D-27, D-28)
    app.include_router(agent_identity.router)
    app.include_router(agent_analysis.router)
    app.include_router(agent_tracklists.router)
    app.include_router(agent_proposals.router)
    # Phase 27 internal-agent router (D-10).
    app.include_router(agent_scan_batches.router)
    # Phase 28 internal-agent router (D-05): per-proposal terminal-state progress reporting
    # — the single mutation point for exec:{batch_id} Redis hash (D-02).
    app.include_router(agent_exec_batches.router)
    # Phase 27 admin-UI router (D-05..D-08): POST /pipeline/scans + the HTMX
    # poll partial + the agent-roots swap. Distinct from `pipeline.router`,
    # which serves the dashboard page and existing pipeline-stage triggers.
    app.include_router(pipeline_scans.router)
    # Phase 29 admin-UI router (D-11..D-14): GET /admin/agents (operator-facing
    # liveness page) + GET /admin/agents/_table (HTMX 5s poll partial). The
    # router is read-only and does NOT use get_authenticated_agent (consistent
    # with other admin-UI routers on the private LAN).
    app.include_router(admin_agents.router)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app


app = create_app()
