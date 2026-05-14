"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from phaze.config import settings


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


logger = logging.getLogger(__name__)


engine = create_async_engine(
    str(settings.database_url),
    echo=settings.debug,
    pool_size=5,
    max_overflow=10,
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
