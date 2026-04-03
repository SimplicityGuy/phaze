"""FastAPI application factory with lifespan management."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from saq import Queue
from sqlalchemy import text

from phaze.config import settings
from phaze.database import engine
from phaze.routers import companion, duplicates, execution, health, pipeline, preview, proposals, scan, search, tags, tracklists


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifespan: verify DB, create SAQ queue on startup; dispose on shutdown."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    # Create SAQ queue for enqueuing jobs from API endpoints
    _app.state.queue = Queue.from_url(settings.redis_url)
    yield
    # Shutdown: disconnect queue, then dispose DB engine
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
    return app


app = create_app()
