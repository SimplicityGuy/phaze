"""REQ-37-4: no double-pickup / no deadlock under concurrent admin UPDATE vs dequeue (real PG).

The no-double-pickup guarantee is provided entirely by Postgres row locking: the admin
mutation's ``WHERE status='queued'`` guard contends with the dequeue's ``FOR UPDATE SKIP
LOCKED`` on the same row lock. This races a real service-helper ``set_stage_priority`` UPDATE
against a real ``queue.dequeue(...)`` under ``asyncio.gather`` and asserts the invariants that
hold for BOTH safe interleavings documented in 37-RESEARCH (worker-locks-first /
admin-locks-first):

* the gather completes with no exception, deadlock, or timeout;
* each job ends in EXACTLY one state -- dequeued-and-``active`` (at most one row) OR
  ``queued``-with-the-priority-mutation-applied -- never both (no double-pickup);
* row count is conserved (queued + active == enqueued), so nothing is lost or duplicated.

Run with real PG via ``just integration-test``. Package auto-marked ``integration`` by
``tests/conftest.py``; the explicit ``pytestmark`` is the Plan 37-03 artifact contract.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.services.stage_control import set_stage_priority


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_NEW_PRIORITY = 5
_ALL_ROWS_SQL = text("SELECT key, status, priority FROM saq_jobs WHERE queue = :q AND key LIKE 'process_file:%'")


async def test_concurrent_admin_update_vs_dequeue_no_double_pickup(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """Racing set_stage_priority against dequeue: no deadlock, no double-pickup, count conserved."""
    queue, session_factory = stage_env

    keys = {f"process_file:{uuid.uuid4()}" for _ in range(4)}
    for key in keys:
        await queue.enqueue("process_file", file_id=key.split(":", 1)[1])

    async def admin_reprioritize() -> None:
        # A separate session/connection so it truly races the dequeue's connection at the
        # Postgres row-lock level (the admin UPDATE owns its own transaction + commit).
        async with session_factory() as session:
            await set_stage_priority(session, "analyze", _NEW_PRIORITY)
            await session.commit()

    # Race them. gather raising would signal a deadlock/timeout; reaching the asserts proves none.
    _admin_result, dequeued = await asyncio.gather(admin_reprioritize(), queue.dequeue(timeout=2.0))

    async with session_factory() as session:
        rows = (await session.execute(_ALL_ROWS_SQL, {"q": queue.name})).all()

    by_status: dict[str, list[tuple[str, int]]] = {"queued": [], "active": []}
    for row in rows:
        by_status.setdefault(row.status, []).append((row.key, int(row.priority)))

    active = by_status["active"]
    queued = by_status["queued"]

    # Conservation: no row lost or duplicated across the race.
    assert len(active) + len(queued) == len(keys)

    # No double-pickup: at most one row went active, and it is exactly the dequeued job.
    expected_active = 1 if dequeued is not None else 0
    assert len(active) == expected_active
    if dequeued is not None:
        assert dequeued.key in keys
        assert {key for key, _ in active} == {dequeued.key}

    # Every row that remained queued carries the admin mutation (its priority COLUMN is the new
    # value) -- the UPDATE landed on the live backlog regardless of interleaving.
    assert all(priority == _NEW_PRIORITY for _key, priority in queued)
