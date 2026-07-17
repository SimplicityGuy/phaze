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

import json
from typing import TYPE_CHECKING
import uuid

import pytest
from saq.utils import now_seconds
from sqlalchemy import text

import phaze.tasks._shared.stage_control as stage_control_module
from phaze.services.stage_control import pause_stage, resume_stage
from phaze.tasks._shared.stage_control import SENTINEL


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_SCHEDULED_SQL = text("SELECT scheduled FROM saq_jobs WHERE key = :k")
_JOB_BLOB_SQL = text("SELECT job FROM saq_jobs WHERE key = :k")


async def _scheduled(session: AsyncSession, key: str) -> int:
    """Return the ``saq_jobs.scheduled`` epoch-seconds value for the row keyed by ``key``."""
    return int((await session.execute(_SCHEDULED_SQL, {"k": key})).scalar_one())


async def _job_blob(session: AsyncSession, key: str) -> dict:
    """Return the deserialized JSON ``job`` BYTEA blob for the row keyed by ``key``."""
    raw = (await session.execute(_JOB_BLOB_SQL, {"k": key})).scalar_one()
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    return json.loads(raw)


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


async def test_resume_unparks_the_job_blob_not_only_the_column(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """phaze-01aq: resume must un-park the serialized ``job`` BLOB, not only the ``scheduled`` column.

    A job enqueued WHILE the stage is paused is stamped ``job.scheduled = SENTINEL`` by the
    ``apply_stage_control`` before-enqueue hook, and SAQ serializes that into the JSON blob (SENTINEL
    != the default 0 that ``Job.to_dict`` omits). The old resume reset only the column, leaving the
    blob at SENTINEL -- so on dequeue SAQ deserialized job.scheduled back to SENTINEL and, because this
    project never overrides ``retry_delay``, ``_retry``'s ``scheduled = job.scheduled or now_seconds()``
    re-parked the row at SENTINEL forever, invisible to every recovery path. The fix strips the
    ``scheduled`` key from the blob so the deserialized job.scheduled is 0 (dequeueable).
    """
    queue, session_factory = stage_env

    key = f"process_file:{uuid.uuid4()}"

    async with session_factory() as session:
        # Pause the stage in the control table BEFORE enqueue so the hook parks the NEW job.
        await session.execute(text("UPDATE pipeline_stage_control SET paused = true WHERE stage = 'analyze'"))
        await session.commit()
    # Force the before-enqueue hook to read the just-flipped paused state (drop its 5s TTL cache).
    stage_control_module._cache.clear()
    stage_control_module._cache_expires_at = 0.0

    # Enqueue WHILE paused -> apply_stage_control stamps job.scheduled = SENTINEL into BOTH the column
    # and the serialized blob. This is the exact precondition the old resume could not recover.
    await queue.enqueue("process_file", key=key, file_id=key.split(":", 1)[1])

    async with session_factory() as session:
        assert await _scheduled(session, key) == SENTINEL, "the column is parked at SENTINEL"
        parked_blob = await _job_blob(session, key)
        assert parked_blob["scheduled"] == SENTINEL, "the blob is ALSO parked at SENTINEL (the bug precondition)"

        await resume_stage(session, "analyze")
        await session.commit()

        # Column un-parked ...
        assert await _scheduled(session, key) == 0
        # ... AND the blob's scheduled key is stripped (SAQ omits default-0), so deserialize yields 0.
        blob = await _job_blob(session, key)
        assert "scheduled" not in blob, "resume must clear the SENTINEL from the serialized job blob"

        # A REAL retry now stays dequeueable: SAQ's _retry falls to `job.scheduled or now_seconds()`,
        # and with the blob's scheduled cleared to the default 0, that is now_seconds() -- NOT SENTINEL.
        deserialized_scheduled = blob.get("scheduled", 0)
        retry_scheduled = deserialized_scheduled or int(now_seconds())
        assert retry_scheduled != SENTINEL
        assert retry_scheduled <= int(now_seconds()) + 1, "a forced retry schedules the job to run now, not in year 2286"
