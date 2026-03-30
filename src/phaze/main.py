"""FastAPI application factory with lifespan management."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from sqlalchemy import text

from phaze.config import settings
from phaze.database import engine
from phaze.routers import companion, execution, health, pipeline, proposals, scan


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Manage application lifespan: verify DB, create arq pool on startup; dispose on shutdown."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    # Create arq Redis pool for enqueuing jobs from API endpoints
    _app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    # Shutdown: close arq pool, then dispose DB engine
    await _app.state.arq_pool.close()
    await engine.dispose()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Phaze", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(scan.router)
    app.include_router(companion.router)
    app.include_router(proposals.router)
    app.include_router(execution.router)
    app.include_router(pipeline.router)
    return app


app = create_app()
