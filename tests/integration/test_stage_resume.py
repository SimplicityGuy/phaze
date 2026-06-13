"""REQ-37-3: sentinel-guarded resume un-parks ONLY pause-parked rows (real PG).

``resume_stage`` runs ``UPDATE saq_jobs SET scheduled = 0 WHERE status='queued'
AND key LIKE '<fn>:%' AND scheduled = SENTINEL``. The ``scheduled = SENTINEL`` guard is
load-bearing: a genuine retry backoff sets ``scheduled = now + delay`` (a near-future value
that is NEVER == SENTINEL), so resume must leave it untouched. This test proves it against a
live ``saq_jobs`` table:

* enqueue analyze jobs, ``pause_stage`` to park them all at SENTINEL;
* mutate ONE row to a retry-backoff ``scheduled = now + delay`` (simulating a job that failed
  and is legitimately backing off);
* ``resume_stage`` -> the SENTINEL-parked rows reset to ``scheduled = 0`` (immediately
  dequeueable), while the retry-backoff row is UNCHANGED (still future) -- REQ-37-3.

Run with real PG via ``just integration-test``. Package auto-marked ``integration`` by
``tests/conftest.py``; the explicit ``pytestmark`` is the Plan 37-03 artifact contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from saq.utils import now_seconds
from sqlalchemy import text

from phaze.services.stage_control import pause_stage, resume_stage
from phaze.tasks._shared.stage_control import SENTINEL


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_SCHEDULED_SQL = text("SELECT scheduled FROM saq_jobs WHERE key = :k")


async def _scheduled(session: AsyncSession, key: str) -> int:
    """Return the ``saq_jobs.scheduled`` epoch-seconds value for the row keyed by ``key``."""
    return int((await session.execute(_SCHEDULED_SQL, {"k": key})).scalar_one())


async def test_resume_unparks_sentinel_only_and_preserves_retry_backoff(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """Resume resets SENTINEL-parked rows to 0 but leaves a retry-backoff (now+delay) row alone."""
    queue, session_factory = stage_env

    keys = [f"process_file:{uuid.uuid4()}" for _ in range(3)]
    for key in keys:
        await queue.enqueue("process_file", file_id=key.split(":", 1)[1])

    parked_keys, retry_key = keys[:2], keys[2]
    retry_backoff = int(now_seconds()) + 3600  # near-future, never == SENTINEL

    async with session_factory() as session:
        # Park the whole analyze backlog at SENTINEL.
        await pause_stage(session, "analyze")
        await session.commit()
        for key in keys:
            assert await _scheduled(session, key) == SENTINEL

        # Simulate ONE row entering a legitimate retry backoff after the pause.
        await session.execute(text("UPDATE saq_jobs SET scheduled = :s WHERE key = :k"), {"s": retry_backoff, "k": retry_key})
        await session.commit()
        assert await _scheduled(session, retry_key) == retry_backoff

        await resume_stage(session, "analyze")
        await session.commit()

        # SENTINEL-parked rows are un-parked (scheduled -> 0 == "run now").
        for key in parked_keys:
            assert await _scheduled(session, key) == 0

        # The retry-backoff row is structurally protected by the scheduled = SENTINEL guard.
        assert await _scheduled(session, retry_key) == retry_backoff
