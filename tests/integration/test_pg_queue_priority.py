"""REQ-36-2: native priority + scheduled-park ordering on a real PostgresQueue.

These run against the ephemeral integration-test Postgres broker (``just test-db`` /
``just integration-test``, host port 5433). They pin two behaviors that are ONLY
observable against a live Postgres broker and that the migration depends on
(``saq/queue/postgres.py`` ``_dequeue``, lines 644-682):

* **priority:** the dequeue CTE is ``... WHERE status='queued' ... ORDER BY
  priority, scheduled ... FOR UPDATE SKIP LOCKED``. A LOWER ``priority`` integer
  dequeues first. Enqueueing {50, 10, 90} therefore dequeues 10 -> 50 -> 90.
* **scheduled-park:** the same CTE gates on ``%(now)s >= scheduled``. A job whose
  ``scheduled`` is in the future is NOT eligible and stays parked while a ready
  (``scheduled<=now``) sibling -- even one with a numerically HIGHER priority --
  dequeues ahead of it.

The harness derives the raw libpq broker DSN from ``PHAZE_QUEUE_URL`` or, in the
integration harness, from ``TEST_DATABASE_URL`` by stripping the SQLAlchemy
``+asyncpg`` dialect suffix (psycopg3 cannot parse the dialect form). The whole
``tests/integration/`` package is auto-marked ``integration`` (see
``tests/conftest.py``), so ``pytest -m 'not integration'`` excludes it offline.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from saq.queue.postgres import PostgresQueue
from saq.utils import now_seconds


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# Raw libpq broker DSN (NOT the +asyncpg dialect form). Derived from the integration
# harness' TEST_DATABASE_URL the same way the other live-broker tests derive it.
BROKER_DSN = os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze").replace(
    "postgresql+asyncpg://", "postgresql://"
)


@pytest_asyncio.fixture
async def pg_queue() -> AsyncGenerator[PostgresQueue]:
    """Yield a connected real ``PostgresQueue`` with a per-test-unique name.

    Probes broker connectivity FIRST and ``pytest.skip``s if Postgres is not up, so a
    bare ``uv run pytest`` (no ``just test-db``) is a skip, not an error. ``connect()``
    opens the psycopg3 pool and runs ``init_db()`` (creates ``saq_jobs`` under the SAQ
    advisory lock). The unique queue name isolates rows from any other test; teardown
    still best-effort-deletes this queue's rows before disconnecting the pool.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    queue = PostgresQueue.from_url(BROKER_DSN, name=f"itest-prio-{uuid.uuid4().hex[:8]}")
    await queue.connect()
    try:
        yield queue
    finally:
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as conn:
                # Parameterized query -- queue.name is bound, never interpolated.
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()


@pytest.mark.integration
async def test_lower_priority_integer_dequeues_first(pg_queue: PostgresQueue) -> None:
    """Enqueue priorities {50, 10, 90}; dequeue order is 10 -> 50 -> 90 (lower first)."""
    await pg_queue.enqueue("noop", key="prio-mid", priority=50)
    await pg_queue.enqueue("noop", key="prio-low", priority=10)
    await pg_queue.enqueue("noop", key="prio-high", priority=90)

    first = await pg_queue.dequeue(timeout=2.0)
    second = await pg_queue.dequeue(timeout=2.0)
    third = await pg_queue.dequeue(timeout=2.0)

    assert first is not None and second is not None and third is not None
    assert [first.key, second.key, third.key] == ["prio-low", "prio-mid", "prio-high"]
    assert [first.priority, second.priority, third.priority] == [10, 50, 90]


@pytest.mark.integration
async def test_future_scheduled_job_parks(pg_queue: PostgresQueue) -> None:
    """A future-``scheduled`` job is NOT dequeued; a ready higher-priority sibling is.

    Even though the parked job carries the numerically-lower (more urgent) priority,
    the ``now >= scheduled`` gate makes it ineligible, so the ready job dequeues first
    and a second dequeue against the still-parked job returns ``None``.
    """
    future = now_seconds() + 3600  # one hour out -- safely parked for the test run
    await pg_queue.enqueue("noop", key="parked", priority=1, scheduled=int(future))
    await pg_queue.enqueue("noop", key="ready", priority=80)

    # The ready job dequeues first despite its higher priority int -- the parked job is
    # gated out by `now >= scheduled`.
    ready = await pg_queue.dequeue(timeout=2.0)
    assert ready is not None
    assert ready.key == "ready"

    # The parked job stays parked: a second dequeue finds nothing eligible.
    parked = await pg_queue.dequeue(timeout=0.5)
    assert parked is None
