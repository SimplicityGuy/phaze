"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
import structlog

from alembic import command
from phaze.config import settings


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


logger = structlog.get_logger(__name__)


# quick-260707-ryn: every pool kwarg is sourced from config (the module-level `settings`
# singleton is ControlSettings, which inherits the BaseSettings db_* knobs) so an operator
# can re-tune without a code change. max_overflow drops from a hardcoded 10 to the config
# default 5, and the three hygiene kwargs (pool_timeout / pool_recycle / pool_pre_ping) are
# NEW. INCIDENT: phaze reaches Postgres through PgBouncer in SESSION mode, where every client
# connection pins one upstream server connection for its whole lifetime; the shared
# (phaze,phaze) session pool (cap ~55) deadlocked under normal multi-worker load and /health
# hung behind the exhausted pool. pool_pre_ping drops dead server conns before checkout,
# pool_recycle=1800 frees an idle server slot after 30 min instead of pinning it, and
# pool_timeout=10 bounds the acquire wait so a saturated pool fails fast. Homelab raises the
# pooler cap to ~80 in parallel, so these app-side reductions are HEADROOM, not a hard fit.
engine = create_async_engine(
    str(settings.database_url),
    echo=settings.debug,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=settings.db_pool_pre_ping,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async database session."""
    async with async_session() as session:
        yield session


# Resolve alembic.ini once at import time; the path is stable and the operation
# is cheap. ``parents[2]`` walks src/phaze/database.py -> src/phaze -> src -> repo-root.
_ALEMBIC_INI_PATH: Path = Path(__file__).resolve().parents[2] / "alembic.ini"


def _run_upgrade_head_sync() -> None:
    """Synchronous wrapper around ``alembic.command.upgrade(cfg, 'head')``.

    Kept private and sync because ``alembic/env.py`` already calls
    ``asyncio.run(run_async_migrations())`` under the hood; invoking it from a
    sync wrapper running inside ``asyncio.to_thread`` (the public entry point
    below) sidesteps the nested-event-loop conflict.
    """
    cfg = Config(str(_ALEMBIC_INI_PATH))
    # alembic/env.py overrides sqlalchemy.url from settings.database_url on every
    # run, so we don't need to set it here -- but setting it makes the cfg
    # honest for any caller that reads it back. The env-side override remains
    # authoritative.
    cfg.set_main_option("sqlalchemy.url", str(settings.database_url))
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    """Run ``alembic upgrade head`` against the configured database.

    Phase 27 UAT Gap 2: the api lifespan must bring the schema to head on a
    fresh DB before any router or session-using code touches the engine.
    Idempotent -- safe to call when already at head (alembic is a no-op).

    Invoked from :mod:`phaze.main` lifespan startup. Gated by
    ``settings.auto_migrate`` so operators can disable the auto-upgrade in
    environments that prefer a manual migration window.
    """
    if not settings.auto_migrate:
        logger.info("phaze.database.run_migrations skipped: settings.auto_migrate=false")
        return

    logger.info("phaze.database.run_migrations: running `alembic upgrade head`")
    # Run the sync alembic command in a worker thread to avoid nested asyncio
    # event-loop conflicts (alembic/env.py uses ``asyncio.run`` internally).
    await asyncio.to_thread(_run_upgrade_head_sync)
    logger.info("phaze.database.run_migrations: schema at head")


__all__ = [
    "async_session",
    "engine",
    "get_session",
    "run_migrations",
]
