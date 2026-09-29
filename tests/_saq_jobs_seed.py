"""Seed a real-shaped ``saq_jobs`` table for queue-view tests (phaze-lwz8n).

The test database has no ``saq_jobs`` table (SAQ creates it on ``connect()``, which tests never do), so
tests that read the broker create one inside their own transaction. The column set and the ``job``
blob shape mirror SAQ's own (``saq/queue/postgres_migrations.py`` and ``Job.to_dict``): the blob is JSON
in a BYTEA column, ``attempts`` is ABSENT until the worker starts the job, and ``started`` / ``touched``
are epoch milliseconds. ``DROP ... IF EXISTS`` first, because sibling modules create a two-column
``saq_jobs`` of their own and a leftover of that shape would silently break these reads.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import text


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_DROP = text("DROP TABLE IF EXISTS saq_jobs")
_CREATE = text(
    """
    CREATE TABLE saq_jobs (
        key TEXT PRIMARY KEY,
        lock_key SERIAL NOT NULL,
        job BYTEA NOT NULL,
        queue TEXT NOT NULL,
        status TEXT NOT NULL,
        priority SMALLINT NOT NULL DEFAULT 0,
        group_key TEXT,
        scheduled BIGINT NOT NULL DEFAULT 0,
        expire_at BIGINT
    )
    """
)


async def now_ms(session: AsyncSession) -> int:
    """The current time in SAQ's epoch-milliseconds unit, read from the DATABASE's clock (phaze-neo4z).

    Every reader that ages a ``saq_jobs`` row -- the reapers, queue introspection, lane detail's
    claim-overdue / heartbeat-lost predicates, and (since phaze-jz0fm) ``get_running_analyses`` /
    ``get_waiting_page``'s own ``db_now`` -- does so as ``EXTRACT(EPOCH FROM NOW())`` /
    ``func.now()``, i.e. Postgres's bare ``NOW()``, never ``clock_timestamp()``. Seeding
    ``started``/``touched`` from the host's ``time.time()`` put a second clock into every margin a
    caller asserts: measured 2026-09-28, the container ran 50 ms ahead of the host, and a simulated
    host-clock skew of the same shape reproduced a real verify-main gate failure (phaze-0vlnp).

    **Read ``NOW()``, not ``clock_timestamp()``** (phaze-jz0fm correction). ``NOW()``/
    ``CURRENT_TIMESTAMP`` is fixed to the CURRENT TRANSACTION's start and stays constant for its
    whole duration; ``clock_timestamp()`` advances with real wall-clock time even mid-transaction.
    This suite runs each test inside one shared transaction (D-07), so within one test ``NOW()`` here
    and ``db_now``'s own ``NOW()`` read the IDENTICAL value, however much real time separates the two
    calls -- ``clock_timestamp()`` would drift between them by that real elapsed time instead.

    **A caller still owes its OWN margin** (phaze-jz0fm, measured): with both ends pinned to the same
    ``NOW()``, an assertion engineered to land EXACTLY on a humanize-format bucket boundary (e.g.
    ``started_ago_s=900`` expecting the "15m" bucket, where ``900 // 60 == 15`` with zero slack) is at
    the mercy of ``::bigint``'s ROUND-to-nearest-millisecond on this cast: it rounds up roughly half
    the time, and a millisecond rounded up out of ``started`` reads back as ``elapsed`` a fraction of
    a millisecond SHORT of 900.000s -- enough for ``int(899.9996 // 60) == 14`` to flip the bucket.
    Observed: ~2/3 of runs red with no simulated skew at all. This is not a defect in ``now_ms`` or
    ``db_now`` -- SAQ's own wire format is integer epoch-ms, so real production ``started`` values are
    rounded exactly the same way, and a real request's ``NOW()`` is never pinned to the same instant
    as the write it is aging, so real elapsed time always swamps this sub-millisecond artifact. It is
    visible here only because the exact-boundary assertion removed every other source of slack; the
    fix is the caller's margin, not this function -- pick a value that is not a multiple of the next
    humanize threshold up to rounding error (e.g. 900 + a few seconds), never adjust this clock choice
    back to ``clock_timestamp()`` to paper over it.
    """
    return int(await session.scalar(text("SELECT (EXTRACT(EPOCH FROM NOW()) * 1000)::bigint")))


async def drop_saq_jobs(session: AsyncSession) -> None:
    """Drop any ``saq_jobs`` in the caller's transaction, for a test that needs an unreadable broker."""
    await session.execute(_DROP)


async def create_saq_jobs(session: AsyncSession) -> None:
    """(Re)create a SAQ-shaped ``saq_jobs`` table in the caller's transaction."""
    await session.execute(_DROP)
    await session.execute(_CREATE)


async def seed_job(session: AsyncSession, queue: str, *, key: str, status: str, blob: dict[str, Any] | None = None, scheduled: int = 0) -> None:
    """Insert one ``saq_jobs`` row whose blob carries ``blob`` on top of the fields SAQ always serializes."""
    payload = json.dumps({"function": "process_file", "queue": queue, "status": status, **(blob or {})}).encode("utf-8")
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, :status, :scheduled)"),
        {"key": key, "job": payload, "queue": queue, "status": status, "scheduled": scheduled},
    )


async def seed_saq_jobs(session: AsyncSession, queue: str, *, queued: int = 0, running: int = 0, claimed: int = 0) -> None:
    """Create the table and seed anonymous ``process_file`` rows: ``queued``, started, and claimed-but-unrun."""
    await create_saq_jobs(session)
    now = await now_ms(session)
    for _ in range(queued):
        await seed_job(session, queue, key=f"process_file:{uuid.uuid4()}", status="queued")
    for _ in range(running):
        await seed_job(
            session,
            queue,
            key=f"process_file:{uuid.uuid4()}",
            status="active",
            blob={"attempts": 1, "started": now, "touched": now, "timeout": 0, "heartbeat": 3600},
        )
    for _ in range(claimed):
        await seed_job(
            session,
            queue,
            key=f"process_file:{uuid.uuid4()}",
            status="active",
            blob={"started": now, "touched": now, "timeout": 0, "heartbeat": 3600},
        )


async def seed_bulk_queued(session: AsyncSession, queue: str, count: int) -> None:
    """Insert ``count`` anonymous queued ``process_file`` rows in ONE statement (a realistic backlog, cheaply)."""
    await session.execute(
        text(
            """
            INSERT INTO saq_jobs (key, job, queue, status, scheduled)
            SELECT 'process_file:' || gen_random_uuid(),
                   convert_to(json_build_object('function', 'process_file', 'queue', CAST(:queue AS text), 'status', 'queued')::text, 'UTF8'),
                   CAST(:queue AS text), 'queued', n
            FROM generate_series(1, :count) AS n
            """
        ),
        {"queue": queue, "count": count},
    )
