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
from phaze.database import engine
from phaze.routers import (
    agent_analysis,
    agent_execution,
    agent_files,
    agent_fingerprint,
    agent_heartbeat,
    agent_identity,
    agent_metadata,
    agent_proposals,
    agent_tracklists,
    companion,
    cue,
    duplicates,
    execution,
    health,
    pipeline,
    preview,
    proposals,
    scan,
    search,
    tags,
    tracklists,
)
from phaze.services.agent_task_router import AgentTaskRouter


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifespan: verify DB; create SAQ queue + task_router + redis on startup; dispose on shutdown.

    Resources wired (in order):
        1. ``app.state.queue``        -- existing default-queue SAQ Queue (Phase 25; used by routers/scan.py).
        2. ``app.state.task_router``  -- Phase 26 D-20 AgentTaskRouter for per-agent SAQ enqueues
                                         (used by agent_files.py auto-enqueue path).
        3. ``app.state.redis``        -- Phase 26 D-27 shared async Redis client for the tracklists
                                         idempotency cache (decode_responses=True so .get/.set yield str).

    Shutdown is reverse-order (task_router, redis, queue) so the SAQ-backed router closes
    before the underlying default queue's Redis pool is gone.
    """
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    # SAQ default-queue (Phase 25) -- used by routers/scan.py
    _app.state.queue = Queue.from_url(settings.redis_url)
    # AgentTaskRouter -- per-agent SAQ enqueuer (Phase 26 Plan 04, D-20)
    _app.state.task_router = AgentTaskRouter(redis_url=settings.redis_url)
    # Shared Redis client for tracklists idempotency cache (Phase 26 Plan 07, D-27).
    # decode_responses=True so .get/.set return str (matches agent_tracklists.py expectations).
    _app.state.redis = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)
    yield
    # Shutdown in reverse construction order.
    await _app.state.task_router.close()
    await _app.state.redis.aclose()
    await _app.state.queue.disconnect()
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
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app


app = create_app()
