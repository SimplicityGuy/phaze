"""REQ-37-1: drain-style pause parks the queued backlog while an active job drains (real PG).

These assertions are ONLY observable against a live Postgres ``saq_jobs`` table and SAQ's
real ``count()`` SQL -- a unit-level fake cannot model them:

* ``pause_stage`` sets ``scheduled = SENTINEL`` on every ``status='queued'`` analyze row, so
  each fails the dequeue's ``now >= scheduled`` gate and parks (REQ-37-1);
* a ``status='active'`` row (an in-flight job) is left UNTOUCHED -- it drains to completion,
  which is the whole point of drain semantics;
* the Pitfall-1 count interaction is pinned as a regression assertion: a paused stage's
  ``count("queued")`` drops to 0 (the parked rows fail ``now >= scheduled``) while
  ``count("incomplete")`` is UNCHANGED (parked rows are still ``status='queued'``) -- so the
  paused backlog is observable as "incomplete", not silently lost (T-37-02).

Run with real PG via ``just integration-test``. The package is auto-marked ``integration``
by ``tests/conftest.py``; the explicit ``pytestmark`` below is the documented Plan 37-03
artifact contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.services.stage_control import pause_stage
from phaze.tasks._shared.stage_control import SENTINEL


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_ROW_SQL = text("SELECT status, scheduled, priority FROM saq_jobs WHERE key = :k")


async def _row(session: AsyncSession, key: str) -> tuple[str, int, int]:
    """Return ``(status, scheduled, priority)`` for the ``saq_jobs`` row keyed by ``key``."""
    result = (await session.execute(_ROW_SQL, {"k": key})).one()
    return result.status, int(result.scheduled), int(result.priority)


async def test_pause_parks_queued_backlog_and_drains_active(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """Pause parks every queued analyze row at SENTINEL; the active row is untouched."""
    queue, session_factory = stage_env

    keys = [f"process_file:{uuid.uuid4()}" for _ in range(3)]
    for key in keys:
        # file_id becomes job.kwargs; apply_deterministic_key rebuilds key as process_file:<id>.
        await queue.enqueue("process_file", file_id=key.split(":", 1)[1])

    active_key, queued_keys = keys[0], keys[1:]

    async with session_factory() as session:
        # Simulate an in-flight job: flip one row to 'active' so pause must leave it to drain.
        await session.execute(text("UPDATE saq_jobs SET status = 'active' WHERE key = :k"), {"k": active_key})
        await session.commit()
        _status_before, active_scheduled_before, _prio = await _row(session, active_key)

        count_incomplete_before = await queue.count("incomplete")
        assert await queue.count("queued") == 2  # the two still-queued analyze rows
        assert count_incomplete_before == 3  # 2 queued + 1 active

        await pause_stage(session, "analyze")
        await session.commit()

        # (a) every queued analyze row is parked at SENTINEL
        for key in queued_keys:
            status, scheduled, _ = await _row(session, key)
            assert status == "queued"
            assert scheduled == SENTINEL

        # (b) the active row drains untouched -- status + scheduled unchanged
        active_status, active_scheduled_after, _ = await _row(session, active_key)
        assert active_status == "active"
        assert active_scheduled_after == active_scheduled_before

    # (c) Pitfall-1 semantic, pinned: paused stage's count("queued") -> 0, incomplete unchanged
    assert await queue.count("queued") == 0
    assert await queue.count("incomplete") == count_incomplete_before
