"""REQ-36-3: deterministic-key dedup no-op on a real PostgresQueue.

Runs against the ephemeral integration-test Postgres broker (``just test-db`` /
``just integration-test``, host port 5433). Pins the production SAQ dedup contract
the ``DedupFakeQueue`` test double models (``tests/_queue_fakes.py``) -- and that
``reenqueue_discovered`` relies on to count an in-flight file as ``skipped`` -- against
the live Postgres broker (``saq/queue/postgres.py`` ``_enqueue``, lines 700-755):

    INSERT ... ON CONFLICT (key) DO UPDATE
      SET ...
      WHERE saq_jobs.status IN ('aborted','complete','failed')
        AND %(scheduled)s > saq_jobs.scheduled
      RETURNING 1

So a SECOND enqueue of an in-flight (``queued``/``active``) deterministic key updates
no row and ``RETURNING`` yields nothing -> ``enqueue`` returns ``None`` (a clean no-op:
no raise, no overwrite, the payload never re-lands). Once the job reaches a terminal
status (here ``complete`` via ``finish``), the same key enqueues again -- provided the
new ``scheduled`` is strictly greater than the old one (the second ON CONFLICT clause).

The harness derives the raw libpq broker DSN from ``PHAZE_QUEUE_URL`` or the integration
harness' ``TEST_DATABASE_URL`` (``+asyncpg`` dialect stripped). The whole
``tests/integration/`` package is auto-marked ``integration`` (see ``tests/conftest.py``).
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from saq.job import Status
from saq.queue.postgres import PostgresQueue
from saq.utils import now_seconds


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# Raw libpq broker DSN (NOT the +asyncpg dialect form psycopg3 cannot parse).
BROKER_DSN = os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze").replace(
    "postgresql+asyncpg://", "postgresql://"
)


@pytest_asyncio.fixture
async def pg_queue() -> AsyncGenerator[PostgresQueue]:
    """Yield a connected real ``PostgresQueue`` with a per-test-unique name.

    Probes broker connectivity first and ``pytest.skip``s when Postgres is down, so a
    bare ``uv run pytest`` (no ``just test-db``) skips rather than errors. ``connect()``
    opens the psycopg3 pool and runs ``init_db()`` (creates ``saq_jobs``). Teardown
    best-effort-deletes this queue's rows, then disconnects the pool.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    queue = PostgresQueue.from_url(BROKER_DSN, name=f"itest-dedup-{uuid.uuid4().hex[:8]}")
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
async def test_in_flight_duplicate_key_returns_none(pg_queue: PostgresQueue) -> None:
    """A second enqueue of an in-flight ``process_file:<id>`` key is a no-op returning None."""
    key = f"process_file:{uuid.uuid4()}"

    first = await pg_queue.enqueue("process_file", key=key)
    assert first is not None  # initial enqueue lands

    second = await pg_queue.enqueue("process_file", key=key)
    assert second is None  # ON CONFLICT no-op: still queued, nothing updated


@pytest.mark.integration
async def test_key_reenqueues_after_completion(pg_queue: PostgresQueue) -> None:
    """After the job finishes (terminal status), the same deterministic key enqueues again.

    Mirrors ``reenqueue_discovered``'s contract: an in-flight key is ``skipped`` (no-op),
    but once the prior job completes the key is eligible again. The re-enqueue carries a
    strictly-greater ``scheduled`` so it satisfies the second ON CONFLICT clause
    (``new.scheduled > old.scheduled``).
    """
    key = f"process_file:{uuid.uuid4()}"

    first = await pg_queue.enqueue("process_file", key=key)
    assert first is not None
    assert await pg_queue.enqueue("process_file", key=key) is None  # in-flight dedup

    # Drive the job to a terminal status; the default ttl (600s) keeps the row as
    # 'complete' rather than deleting it, so the re-enqueue exercises the ON CONFLICT
    # terminal-status update path (not a fresh INSERT into a vacated key).
    await pg_queue.finish(first, Status.COMPLETE)

    reenqueued = await pg_queue.enqueue(
        "process_file",
        key=key,
        scheduled=int(now_seconds()) + 1,  # strictly greater than the original scheduled
    )
    assert reenqueued is not None  # terminal -> key is enqueueable again
