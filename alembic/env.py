"""Alembic async migration environment for Phaze."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from phaze.config import settings
from phaze.models import *  # noqa: F403
from phaze.models.base import Base


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override sqlalchemy.url from application settings. Alembic's Config is backed by a
# ConfigParser, which applies %-interpolation on every value it stores -- and a percent-encoded
# credential (e.g. a password containing '@' becomes '%40' in the URL) is a completely normal,
# valid SQLAlchemy URL that is NOT valid ConfigParser input. Escape '%' as '%%' before handing
# the raw URL to set_main_option, per Alembic's own documented workaround, so a real-world
# credential doesn't crash `set_main_option` with ValueError('invalid interpolation syntax')
# before any migration runs (phaze-7oya).
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    # disable_existing_loggers=False preserves application loggers across migration
    # runs. With the default True, fileConfig disables every Python logger not
    # listed in alembic.ini (only root/sqlalchemy/alembic), which kills pytest
    # caplog capture for phaze.* loggers in any test that runs after a migration
    # test in the same process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Target metadata for autogenerate support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _include_object(_obj: object, name: str | None, type_: str, _reflected: bool, _compare_to: object) -> bool:
    """Exclude migration-managed non-ORM tables from autogenerate so ``alembic check`` stays empty.

    ``files_state_archive`` is the forensic state snapshot created by migration 039 (Phase 90, D-10):
    it must PERSIST after the ``files.state`` drop so ``039.downgrade()`` can restore the column
    verbatim, yet it has no ORM model (by design). Without this exclusion autogenerate would forever
    want to ``DROP TABLE files_state_archive`` and ``alembic check`` would never be empty. Alembic calls
    this with a fixed 5-arg signature -- only ``name``/``type_`` are consulted.
    """
    return not (type_ == "table" and name == "files_state_archive")


def do_run_migrations(connection: Connection) -> None:
    """Configure context and run migrations within a connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
