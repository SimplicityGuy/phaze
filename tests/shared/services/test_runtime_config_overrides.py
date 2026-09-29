"""Tests for the DB-override layer's reader/writer (phaze-mvq8z.6).

* ``get_runtime_config_overrides`` -- the reader: empty table -> ``{}``; seeded rows
  -> ``{key: value}``; a DB exception -> SAVEPOINT rollback, then RAISES (phaze-mvq8z.19): a
  failed read is not "no overrides", and only a raise lets the store keep its last-good snapshot.
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

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from phaze.config import ControlSettings
from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
from phaze.services.runtime_config_overrides import clear_runtime_config_override, get_runtime_config_overrides, set_runtime_config_override


if TYPE_CHECKING:
    from pathlib import Path

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
    exception raised inside the ``async with`` block out to the caller, exactly as a
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
async def test_get_overrides_raises_on_db_error_rather_than_reporting_no_overrides() -> None:
    """phaze-mvq8z.19 finding 1: a failed read must NOT come back as ``{}``.

    ``{}`` is also the honest answer for "no overrides are set", so a reader that degrades to it
    makes a transient DB failure indistinguishable from an operator clearing every override --
    and the store, seeing a changed digest, would swap and run every applier on it.
    """
    with pytest.raises(RuntimeError, match="simulated DB failure"):
        await get_runtime_config_overrides(_RaisingSession())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failed_override_read_keeps_the_last_good_override_in_the_store(session: AsyncSession, tmp_path: Path) -> None:
    """phaze-mvq8z.19 finding 1, the api/control-plane half: the store the DB layer is installed
    into (``install_runtime_config_overrides``) keeps the override it last resolved when the next
    read fails, rather than swapping to a snapshot with the override layer silently missing."""
    await set_runtime_config_override(session, "worker_max_jobs", 12)
    reader: dict[str, Any] = {"session": session}
    store = RuntimeConfigStore(ControlSettings(), runtime_toml=tmp_path / RUNTIME_TOML_NAME, physical_cores=lambda: 64)
    store.set_override_provider(lambda: get_runtime_config_overrides(reader["session"]))
    assert (await store.reload("startup")).outcome == "applied"
    assert store.current().worker_max_jobs == 12
    before = store.snapshot()

    reader["session"] = _RaisingSession()
    result = await store.reload("poll")

    assert result.outcome == "rejected"
    assert "simulated DB failure" in (result.error or "")
    assert store.snapshot() is before
    assert store.current().worker_max_jobs == 12
    assert store.snapshot().sources["worker_max_jobs"] == "override"
