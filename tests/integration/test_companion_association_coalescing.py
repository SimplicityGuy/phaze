"""phaze-spd83: automatic association runs coalesce per agent and window on a REAL PostgresQueue.

``services/companion_autolink.py`` claims two things the fake queues cannot show, because both rest
on SAQ's Postgres ``_enqueue`` (``INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE status IN
('aborted','complete','failed')``) and on its dequeue rule (``now >= scheduled``):

1. every request for one agent within one window lands on ONE job -- a scan posting many chunks
   enqueues one run, not one per chunk;
2. a request made AFTER that run has started is never dropped against the active job: the run was
   dequeued no earlier than its window's end, so the later request names a later window and a key of
   its own.

The central ``before_enqueue`` key hook is registered on the queue, so the keys are the production
ones. SAQ's ``saq_jobs.key`` is unique across queues, so every test names agents of its own. Skips
when the broker is down, like the other ``test_pg_*`` modules.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from saq.job import Status
from saq.queue.postgres import PostgresQueue
from saq.utils import now_seconds

from phaze.services.companion_autolink import ASSOCIATION_TASK, ASSOCIATION_WINDOW_SECONDS, association_window, enqueue_association
from phaze.tasks._shared.deterministic_key import apply_deterministic_key
from tests.db_guard import integration_dsns


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


BROKER_DSN, _ = integration_dsns()


@pytest_asyncio.fixture
async def keyed_queue() -> AsyncGenerator[PostgresQueue]:
    """A connected real PostgresQueue, per-test name, with the production deterministic-key hook."""
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    queue = PostgresQueue.from_url(BROKER_DSN, name=f"itest-autolink-{uuid.uuid4().hex[:8]}")
    queue.register_before_enqueue(apply_deterministic_key)
    await queue.connect()
    try:
        yield queue
    finally:
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as conn:
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()


def _agent() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


async def _rows(queue: PostgresQueue) -> list[tuple[str, str]]:
    async with queue.pool.connection() as conn:
        cursor = await conn.execute("SELECT key, status FROM saq_jobs WHERE queue = %s ORDER BY key", (queue.name,))
        return [(str(key), str(status)) for key, status in await cursor.fetchall()]


@pytest.mark.integration
async def test_requests_in_one_window_coalesce_onto_one_job(keyed_queue: PostgresQueue) -> None:
    agent_a, agent_b = sorted([_agent(), _agent()])
    start = association_window(now_seconds()) * ASSOCIATION_WINDOW_SECONDS
    outcomes = [await enqueue_association(keyed_queue, agent_a, at=start + offset) for offset in (0, 1, 30, ASSOCIATION_WINDOW_SECONDS - 1)]
    other_agent = await enqueue_association(keyed_queue, agent_b, at=start)

    assert outcomes == [True, False, False, False]
    assert other_agent  # coalescing is per agent
    window = association_window(start)
    assert await _rows(keyed_queue) == [
        (f"{ASSOCIATION_TASK}:{agent_a}:{window}", "queued"),
        (f"{ASSOCIATION_TASK}:{agent_b}:{window}", "queued"),
    ]


@pytest.mark.integration
async def test_a_request_after_the_run_started_gets_its_own_job(keyed_queue: PostgresQueue) -> None:
    """The run is only dequeueable once its window has ended, so a later request is in a later window."""
    agent = _agent()
    now = now_seconds()
    past = (association_window(now) - 3) * ASSOCIATION_WINDOW_SECONDS  # a window's first second
    assert await enqueue_association(keyed_queue, agent, at=past)
    assert await keyed_queue.dequeue(timeout=1) is not None  # the worker took it: ACTIVE now

    assert await enqueue_association(keyed_queue, agent, at=past + 1) is False  # its own window: what the active run covers
    later = await enqueue_association(keyed_queue, agent, at=now)

    assert later  # NOT dropped against the active job
    assert await _rows(keyed_queue) == sorted(
        [
            (f"{ASSOCIATION_TASK}:{agent}:{association_window(past)}", "active"),
            (f"{ASSOCIATION_TASK}:{agent}:{association_window(now)}", "queued"),
        ]
    )


@pytest.mark.integration
async def test_a_queued_run_is_not_dequeued_before_its_window_ends(keyed_queue: PostgresQueue) -> None:
    """The half of the invariant SAQ owns: ``scheduled`` is the window's end, and nothing runs before it."""
    agent = _agent()
    assert await enqueue_association(keyed_queue, agent)
    assert await keyed_queue.dequeue(timeout=1) is None


@pytest.mark.integration
async def test_a_finished_windows_key_does_not_block_the_next_window(keyed_queue: PostgresQueue) -> None:
    agent = _agent()
    past = now_seconds() - 3 * ASSOCIATION_WINDOW_SECONDS
    assert await enqueue_association(keyed_queue, agent, at=past)
    job = await keyed_queue.dequeue(timeout=1)
    assert job is not None
    await keyed_queue.finish(job, Status.COMPLETE)

    assert await enqueue_association(keyed_queue, agent)
