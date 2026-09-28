"""Tests for the DB-override layer's reader/writer (phaze-mvq8z.6).

* ``get_runtime_config_overrides`` -- the DEGRADE-SAFE reader: empty table -> ``{}``; seeded rows
  -> ``{key: value}``; any DB exception -> SAVEPOINT rollback -> ``{}``, NEVER raises. Mirrors
  ``tests/shared/routers/test_routing.py``'s ``test_route_control_degrades_on_db_error`` shape.
* ``set_runtime_config_override`` -- creates or updates a row.
* ``clear_runtime_config_override`` -- removes a row if present; returns whether it did.

Real ``pg_notify`` DELIVERY (a listener actually receiving the notification) is NOT covered here
-- this suite's ``session`` fixture wraps every test in a per-test SAVEPOINT that is rolled back
at teardown (tests/conftest.py), so a "commit" here never reaches a real top-level COMMIT and a
queued NOTIFY payload is never delivered. That is covered by
``tests/shared/config/test_runtime_config_notify.py``, which uses real commits for exactly this
reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.services.runtime_config_overrides import clear_runtime_config_override, get_runtime_config_overrides, set_runtime_config_override


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_overrides_empty_table(session: AsyncSession) -> None:
    """No rows -> ``{}`` (every key falls through to the next layer)."""
    assert await get_runtime_config_overrides(session) == {}


@pytest.mark.asyncio
async def test_set_then_get_round_trips_int_and_str_values(session: AsyncSession) -> None:
    """Both value shapes RELOADABLE_KEYS actually use -- int and str -- round-trip through JSONB."""
    await set_runtime_config_override(session, "worker_max_jobs", 12)
    await set_runtime_config_override(session, "log_level", "DEBUG")

    overrides = await get_runtime_config_overrides(session)

    assert overrides == {"worker_max_jobs": 12, "log_level": "DEBUG"}


@pytest.mark.asyncio
async def test_set_override_updates_an_existing_row_in_place(session: AsyncSession) -> None:
    """Setting the same key twice updates the row rather than erroring on a duplicate PK."""
    await set_runtime_config_override(session, "worker_max_jobs", 4)
    await set_runtime_config_override(session, "worker_max_jobs", 9)

    overrides = await get_runtime_config_overrides(session)
    assert overrides == {"worker_max_jobs": 9}
    rows = (await session.execute(select(RuntimeConfigOverride))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_clear_override_removes_the_row_and_returns_true(session: AsyncSession) -> None:
    await set_runtime_config_override(session, "worker_max_jobs", 4)

    removed = await clear_runtime_config_override(session, "worker_max_jobs")

    assert removed is True
    assert await get_runtime_config_overrides(session) == {}


@pytest.mark.asyncio
async def test_clear_override_on_an_absent_key_is_a_no_op_returning_false(session: AsyncSession) -> None:
    """Clearing a key with no override row is idempotent, not an error."""
    removed = await clear_runtime_config_override(session, "worker_max_jobs")

    assert removed is False


class _NullSavepoint:
    """Async-context-manager stand-in for ``session.begin_nested()`` -- mirrors
    ``tests/shared/routers/test_routing.py``'s helper of the same name and purpose: propagate the
    exception raised inside the ``async with`` block out to the degrade ``except``, exactly as a
    real SAVEPOINT does after ``ROLLBACK TO SAVEPOINT``.
    """

    async def __aenter__(self) -> _NullSavepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _RaisingSession:
    """Fake session whose ``execute`` always raises, wrapped by a real (no-op) SAVEPOINT stand-in."""

    def begin_nested(self) -> _NullSavepoint:
        return _NullSavepoint()

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated DB failure")


@pytest.mark.asyncio
async def test_get_overrides_degrades_to_empty_on_db_error() -> None:
    """ANY DB exception during the read -> ``{}``, never raises (mirrors get_route_control)."""
    assert await get_runtime_config_overrides(_RaisingSession()) == {}  # type: ignore[arg-type]
