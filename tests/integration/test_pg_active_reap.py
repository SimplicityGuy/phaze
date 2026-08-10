"""phaze-o0n6: a stranded ``active`` row blocks re-enqueue, and the reaper unblocks it. Real broker.

The assertion that would have caught the bug, pinned against the LIVE ``saq/queue/postgres.py``
``_enqueue`` rather than a test double. Sibling of ``test_pg_dedup.py``, which pins the same upsert's
behaviour for the ``queued`` case; this file pins the ``active`` case AND the remedy.

    INSERT ... ON CONFLICT (key) DO UPDATE
      SET ...
      WHERE saq_jobs.status IN ('aborted','complete','failed')
        AND %(scheduled)s > saq_jobs.scheduled
      RETURNING 1

``'active'`` is not in that allowlist. So a row abandoned in ``active`` -- which
``PostgresQueue._dequeue`` creates in bulk and buffers in-process, meaning ANY worker death strands
every buffered row -- makes every subsequent ``enqueue`` of its deterministic key
``process_file:<file_id>`` return ``None``. Not an error, not a retry: a silent no-op, forever. On the
live deployment that had reached 2,413 rows, keying 2,411 files that had never been analyzed and could
not be re-enqueued by ANY path, including the recovery CLI.

``reap_stranded_active_jobs`` DELETEs the row, vacating the key so the next enqueue is a plain INSERT.
Note the third test: DELETE (rather than a transition to a terminal status) is what makes the replay
work UNCONDITIONALLY. A row parked at ``'complete'`` satisfies the status allowlist but still has to
clear the SECOND conflict predicate, ``new.scheduled > old.scheduled``. A replay's ``scheduled``
defaults to ``now_seconds()``, which usually clears that -- but not for a row whose own ``scheduled``
is in the FUTURE, which is precisely what SAQ's retry backoff writes. Deletion has no such second
gate, which is why it is the established precedent.

The whole ``tests/integration/`` package is auto-marked ``integration`` (see ``tests/conftest.py``).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
import uuid

import pytest
import pytest_asyncio
from saq.job import Status
from saq.utils import now_seconds
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.tasks.active_reaper import reap_stranded_active_jobs
from tests.db_guard import integration_dsns


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from saq.queue.postgres import PostgresQueue


pytestmark = pytest.mark.integration


BROKER_DSN, SA_DSN = integration_dsns()

# The real production bound: services/analysis_enqueue.py enqueues process_file with a 7200s timeout.
_PROCESS_FILE_TIMEOUT = 7200
_SLACK = 900


class _StubCfg:
    """Minimal stand-in for the settings object the reaper reads."""

    active_reap_slack_seconds = _SLACK


@pytest_asyncio.fixture
async def broker() -> AsyncGenerator[tuple[PostgresQueue, async_sessionmaker[AsyncSession]]]:
    """Yield ``(queue, session_factory)``: a real connected ``PostgresQueue`` + a session on the SAME DB.

    The queue is the production broker under test (``connect()`` runs ``init_db()``, creating
    ``saq_jobs``); the session factory is how the control-side reaper reaches the same table, exactly
    as ``ctx["async_session"]`` does in the controller. Probes connectivity first and ``pytest.skip``s
    when Postgres is down, so a bare ``uv run pytest`` (no ``just test-db``) skips rather than errors.
    """
    import psycopg
    from saq.queue.postgres import PostgresQueue

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    queue = PostgresQueue.from_url(BROKER_DSN, name=f"itest-active-reap-{uuid.uuid4().hex[:8]}")
    await queue.connect()
    engine = create_async_engine(SA_DSN)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield queue, session_factory
    finally:
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as conn:
                # Parameterized query -- queue.name is bound, never interpolated.
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()
        await engine.dispose()


async def _strand(queue: PostgresQueue, key: str, *, age_seconds: int) -> None:
    """Force ``key``'s row into the stranded shape: ``status='active'`` with a FROZEN old ``started``.

    Rewrites both the ``status`` column AND the serialized blob's ``status``/``started``, because the
    reaper reads the column (its CAS) and the blob (its age bound) and the two must agree, exactly as
    they do on a row ``_dequeue`` marked and a dying worker abandoned. ``touched`` is left at NOW --
    the sweeper bumps it on every pass, so this also proves the bound reads ``started``.
    """
    async with queue.pool.connection() as conn:
        await conn.execute(
            """
            UPDATE saq_jobs
               SET status = 'active',
                   job = convert_to(
                       jsonb_set(
                           jsonb_set(convert_from(job, 'UTF8')::jsonb, '{status}', '"active"'),
                           '{started}',
                           to_jsonb((EXTRACT(EPOCH FROM NOW()) * 1000)::bigint - %s * 1000)
                       )::text,
                       'UTF8'
                   )
             WHERE key = %s
            """,
            (age_seconds, key),
        )


async def _reap(session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    monkeypatch.setattr("phaze.tasks.active_reaper.get_settings", lambda: _StubCfg())
    return await reap_stranded_active_jobs({"async_session": session_factory})


async def _status(session_factory: async_sessionmaker[AsyncSession], key: str) -> str | None:
    async with session_factory() as session:
        return (await session.execute(text("SELECT status FROM saq_jobs WHERE key = :k"), {"k": key})).scalar_one_or_none()


async def test_stranded_active_row_blocks_reenqueue_until_reaped(
    broker: tuple[PostgresQueue, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE REGRESSION: a stranded 'active' key silently swallows every re-enqueue; reaping restores it."""
    queue, session_factory = broker
    key = f"process_file:{uuid.uuid4()}"

    first = await queue.enqueue("process_file", key=key, timeout=_PROCESS_FILE_TIMEOUT)
    assert first is not None
    await _strand(queue, key, age_seconds=_PROCESS_FILE_TIMEOUT + _SLACK + 300)

    # The defect, at the SQL level: the key is held by a row nothing will ever finalize, so the
    # upsert's status allowlist rejects the overwrite and enqueue reports success-shaped nothing.
    assert await queue.enqueue("process_file", key=key, timeout=_PROCESS_FILE_TIMEOUT) is None

    assert await _reap(session_factory, monkeypatch) == {"reaped": 1}
    assert await _status(session_factory, key) is None  # the row is GONE, not parked in a terminal status

    # The key is vacant, so this is a plain INSERT -- no ON CONFLICT clause to satisfy at all.
    reenqueued = await queue.enqueue("process_file", key=key, timeout=_PROCESS_FILE_TIMEOUT)
    assert reenqueued is not None
    assert reenqueued.key == key


async def test_reaper_leaves_a_live_active_row_alone(
    broker: tuple[PostgresQueue, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that just went 'active' keeps its row -- the age bound, against the real broker.

    The failure mode this excludes is the expensive one: deleting the broker row of a job a worker is
    still executing would let a second copy of the SAME file be enqueued and analyzed concurrently.
    """
    queue, session_factory = broker
    key = f"process_file:{uuid.uuid4()}"
    assert await queue.enqueue("process_file", key=key, timeout=_PROCESS_FILE_TIMEOUT) is not None
    await _strand(queue, key, age_seconds=60)  # 60s into a 7200s job: nowhere near its own bound

    assert await _reap(session_factory, monkeypatch) == {"reaped": 0}
    assert await _status(session_factory, key) == "active"
    # ...and it still holds its key, which is CORRECT here: the job is running.
    assert await queue.enqueue("process_file", key=key, timeout=_PROCESS_FILE_TIMEOUT) is None


async def test_terminal_status_is_not_as_strong_a_release_as_delete(
    broker: tuple[PostgresQueue, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHY DELETE, NOT A TRANSITION (acceptance item 2), made executable.

    Moving a stranded row to a terminal status satisfies the upsert's status allowlist but leaves the
    SECOND conflict predicate in play: ``%(scheduled)s > saq_jobs.scheduled``. A replay's ``scheduled``
    defaults to ``now_seconds()`` (``postgres.py``: ``job.scheduled or int(now_seconds())``), which
    normally clears it -- but NOT when the stranded row's own ``scheduled`` is in the FUTURE, which is
    exactly what SAQ's retry backoff writes. Such a row is un-requeueable while looking remediated,
    and it is the same silent ``None`` the bead is about.

    DELETE has no second gate: the key is vacant, so the replay is a plain INSERT and lands
    unconditionally. Both halves are asserted on IDENTICAL rows, so the only variable is the remedy.
    """
    queue, session_factory = broker
    future = int(now_seconds()) + 3600

    # Half 1: the rejected remedy. Terminal status, future `scheduled` -> the replay still no-ops.
    parked_key = f"process_file:{uuid.uuid4()}"
    job = await queue.enqueue("process_file", key=parked_key, timeout=_PROCESS_FILE_TIMEOUT, scheduled=future)
    assert job is not None
    await queue.finish(job, Status.COMPLETE)
    assert await _status(session_factory, parked_key) == "complete"  # row still present, key still held
    assert await queue.enqueue("process_file", key=parked_key, timeout=_PROCESS_FILE_TIMEOUT) is None

    # Half 2: the implemented remedy, on the same shape. DELETE -> the replay lands.
    reaped_key = f"process_file:{uuid.uuid4()}"
    assert await queue.enqueue("process_file", key=reaped_key, timeout=_PROCESS_FILE_TIMEOUT, scheduled=future) is not None
    await _strand(queue, reaped_key, age_seconds=_PROCESS_FILE_TIMEOUT + _SLACK + 300)

    assert await _reap(session_factory, monkeypatch) == {"reaped": 1}
    assert await _status(session_factory, reaped_key) is None
    assert await queue.enqueue("process_file", key=reaped_key, timeout=_PROCESS_FILE_TIMEOUT) is not None


async def test_reaper_leaves_a_timeout_zero_scan_directory_row_alone(
    broker: tuple[PostgresQueue, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-mllxc REGRESSION, against the real broker: ``timeout=0`` means UNBOUNDED, never stranded.

    ``scan_directory`` enqueues with ``timeout=0`` because a full SHA-256 walk of a large
    network-mounted archive legitimately runs 1-2h (``routers/pipeline_scans.py``). SAQ's own
    ``Job.stuck`` treats 0 as falsy -- never sweep-eligible -- and the reaper must mirror that: a
    ``timeout: 0`` row aged well past ``active_reap_slack_seconds`` must still hold its key.
    """
    queue, session_factory = broker
    key = f"scan_directory:{uuid.uuid4()}"
    assert await queue.enqueue("scan_directory", key=key, timeout=0) is not None
    # An hour past slack -- far beyond any finite bound -- to prove the exemption is unconditional.
    await _strand(queue, key, age_seconds=_SLACK + 3600)

    assert await _reap(session_factory, monkeypatch) == {"reaped": 0}
    assert await _status(session_factory, key) == "active"
    # ...and the deterministic key is still held, exactly as it should be for a running scan.
    assert await queue.enqueue("scan_directory", key=key, timeout=0) is None


async def test_reaper_degrades_when_saq_jobs_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reaper hiccup returns ``reaped=0`` instead of aborting the controller's cron tick."""
    engine = create_async_engine(SA_DSN)
    try:
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("phaze.tasks.active_reaper._REAP_ACTIVE_SQL", text("DELETE FROM saq_jobs_does_not_exist RETURNING key"))
        outcome: dict[str, Any] = await _reap(session_factory, monkeypatch)
    finally:
        await engine.dispose()
    assert outcome == {"reaped": 0}
