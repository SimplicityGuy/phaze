"""REQ-37-2: live backlog reprioritization + the priority lower-bound (real PG).

Pins behaviors only observable against SAQ's real Postgres dequeue
(``ORDER BY priority, scheduled ... WHERE priority BETWEEN plow AND phigh``):

* ``set_stage_priority`` UPDATEs the ``status='queued'`` analyze backlog live, and a lower
  ``priority`` integer then dequeues BEFORE a higher-priority sibling -- reorder takes effect
  on the already-enqueued backlog with no requeue (REQ-37-2);
* the helper writes the literal value passed (it does NOT clamp -- clamping is the endpoint +
  the ``pipeline_stage_control`` CHECK's job, Plan 04). The lower bound MATTERS: a priority
  below 0 falls outside SAQ's ``priority BETWEEN 0 AND 32767`` dequeue window and the job
  silently never dequeues (Pitfall 2), while priority 0 is the safe, dequeueable floor.

Run with real PG via ``just integration-test``. Package auto-marked ``integration`` by
``tests/conftest.py``; the explicit ``pytestmark`` is the Plan 37-03 artifact contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.services.stage_control import set_stage_priority


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_PRIORITY_SQL = text("SELECT priority FROM saq_jobs WHERE key = :k")


async def _priority(session: AsyncSession, key: str) -> int:
    """Return the ``saq_jobs.priority`` for the row keyed by ``key``."""
    return int((await session.execute(_PRIORITY_SQL, {"k": key})).scalar_one())


async def test_set_stage_priority_reorders_backlog(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """Lowering analyze's priority makes its queued job dequeue BEFORE a higher-priority job."""
    queue, session_factory = stage_env

    # A non-stage comparison job at priority 30 (apply_stage_control leaves non-stage jobs).
    cmp_key = f"noop-cmp-{uuid.uuid4().hex[:8]}"
    await queue.enqueue("noop", key=cmp_key, priority=30)

    # An analyze job; apply_stage_control stamps it at the seeded priority 50.
    file_id = str(uuid.uuid4())
    analyze_key = f"process_file:{file_id}"
    await queue.enqueue("process_file", file_id=file_id)

    async with session_factory() as session:
        assert await _priority(session, analyze_key) == 50  # hook-stamped baseline

        await set_stage_priority(session, "analyze", 5)
        await session.commit()

        assert await _priority(session, analyze_key) == 5  # backlog reprioritized live

    # The now-lower analyze job dequeues first; the priority-30 comparison job follows. The
    # dequeue ORDER reflects the updated saq_jobs.priority COLUMN (what `ORDER BY priority`
    # reads); note the deserialized Job.priority still mirrors the serialized blob's stamp (a
    # raw column UPDATE does not rewrite the job BYTEA), which is irrelevant to ordering.
    first = await queue.dequeue(timeout=2.0)
    second = await queue.dequeue(timeout=2.0)
    assert first is not None and second is not None
    assert first.key == analyze_key
    assert second.key == cmp_key


async def test_priority_below_zero_is_undequeueable_and_zero_is_the_floor(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """Priority -1 is written literally but never dequeues; priority 0 is the dequeueable floor."""
    queue, session_factory = stage_env

    file_id = str(uuid.uuid4())
    analyze_key = f"process_file:{file_id}"
    await queue.enqueue("process_file", file_id=file_id)

    async with session_factory() as session:
        # The helper does NOT clamp -- it writes the literal value. Below 0 falls outside the
        # dequeue's BETWEEN 0 AND 32767 window (Pitfall 2): the row exists but never dequeues.
        await set_stage_priority(session, "analyze", -1)
        await session.commit()
        assert await _priority(session, analyze_key) == -1

    assert await queue.dequeue(timeout=0.5) is None  # un-dequeueable at priority -1

    async with session_factory() as session:
        # 0 is the safe lower bound the endpoint clamps to: in-range and immediately dequeueable.
        await set_stage_priority(session, "analyze", 0)
        await session.commit()
        assert await _priority(session, analyze_key) == 0

    # Dequeue eligibility is driven by the saq_jobs.priority COLUMN (BETWEEN 0 AND 32767), now 0
    # -> eligible. (Job.priority off the deserialized blob is irrelevant to eligibility/order.)
    dequeued = await queue.dequeue(timeout=2.0)
    assert dequeued is not None
    assert dequeued.key == analyze_key
