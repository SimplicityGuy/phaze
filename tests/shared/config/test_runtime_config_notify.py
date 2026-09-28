"""Postgres LISTEN/NOTIFY propagation for the DB-override layer (phaze-mvq8z.6, ADR-0019 (runtime config hot-reload) §14).

Uses REAL commits against the shared test database, deliberately bypassing the per-test
``session`` fixture (tests/conftest.py): that fixture wraps every test in a SAVEPOINT rolled back
at teardown, so its ``commit()`` never reaches a real top-level Postgres COMMIT and a queued
``NOTIFY`` payload is never delivered -- there would be nothing here to observe. Every test opens
its own session bound to the session-scoped ``async_engine`` fixture instead, and the
``real_session_factory`` fixture deletes whatever rows it wrote on teardown, mirroring how the
migration round-trip tests clean up their own real commits.

* ``test_a_committed_override_reaches_the_store_via_notify`` -- the acceptance criterion's
  "override set -> reload (NOTIFY)" half.
* ``test_a_missed_notify_is_healed_by_the_fallback_poll`` -- a row written straight to the table
  with NO accompanying ``pg_notify`` (standing in for a dropped/missed notification) still reaches
  the store on the next poll tick -- the "fallback poll heals a missed NOTIFY" half.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.config import ControlSettings
from phaze.runtime_config import RuntimeConfigStore
from phaze.runtime_config_notify import install_runtime_config_overrides, start_runtime_config_listener
from phaze.services.runtime_config_overrides import set_runtime_config_override
from tests._async_settle import wait_until
from tests.conftest import TEST_DATABASE_URL


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest_asyncio.fixture
async def real_session_factory(async_engine: AsyncEngine) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """A sessionmaker whose sessions commit FOR REAL against the shared test database (module docstring)."""
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    async with factory() as cleanup:
        await cleanup.execute(text("DELETE FROM runtime_config_override"))
        await cleanup.commit()


@pytest_asyncio.fixture
async def store_and_sessions(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[RuntimeConfigStore, Callable[[], Awaitable[AsyncSession]]]]:
    """A real ``RuntimeConfigStore`` wired to the DB-override layer, with a live LISTEN connection
    and a fast (0.2s) fallback poll -- fast so the poll-healing test does not need a real 30s wait.
    """
    settings = ControlSettings()
    store = RuntimeConfigStore(settings, runtime_toml=None, physical_cores=lambda: 64)
    install_runtime_config_overrides(store, real_session_factory)
    handle = await start_runtime_config_listener(store=store, database_url=TEST_DATABASE_URL, poll_interval_sec=0.2)
    try:
        yield store, real_session_factory
    finally:
        await handle.stop()


@pytest.mark.asyncio
async def test_a_committed_override_reaches_the_store_via_notify(
    store_and_sessions: tuple[RuntimeConfigStore, async_sessionmaker[AsyncSession]],
) -> None:
    """A real commit through ``set_runtime_config_override`` NOTIFYs the listener, which reloads."""
    store, session_factory = store_and_sessions
    assert store.current().worker_max_jobs != 17

    async with session_factory() as session:
        await set_runtime_config_override(session, "worker_max_jobs", 17)

    await wait_until(lambda: store.current().worker_max_jobs == 17, timeout=5.0, description="the NOTIFY-triggered reload landing")
    assert store.last_result is not None
    assert store.last_result.source == "api"
    assert store.snapshot().sources["worker_max_jobs"] == "override"


@pytest.mark.asyncio
async def test_a_missed_notify_is_healed_by_the_fallback_poll(
    store_and_sessions: tuple[RuntimeConfigStore, async_sessionmaker[AsyncSession]],
) -> None:
    """A row written WITHOUT ``pg_notify`` (a stand-in for a dropped/missed NOTIFY) still reaches
    the store, on the next poll tick."""
    store, session_factory = store_and_sessions
    assert store.current().worker_max_jobs != 23

    async with session_factory() as session:
        # No pg_notify() -- this is deliberately the "NOTIFY missed" case the fallback poll heals.
        await session.execute(text("INSERT INTO runtime_config_override (key, value) VALUES ('worker_max_jobs', '23')"))
        await session.commit()

    await wait_until(lambda: store.current().worker_max_jobs == 23, timeout=5.0, description="the fallback poll's reload landing")
    assert store.last_result is not None
    assert store.last_result.source == "poll"
