"""phaze-1kowg: phantom ``process_file`` ledger rows must not pin the local lane full.

THE BUG (spike phaze-tch4d, 2026-09-27). ``LocalBackend.in_flight_count`` counted bare
``process_file:<file_id>`` scheduling-ledger rows. Production held 2374 of them, enqueued
2026-07-16..08-08, with ZERO ``process_file`` jobs in the broker. Local ``cap=1``, so the drain's
``remaining = max(0, 1 - 2374)`` was 0 on every tick. A file that has spent its cloud-attempt budget is
cloud-INELIGIBLE and may ONLY route local -- so it could never move, and those held files accumulated at
the FIFO head.

These tests drive the REAL drain (``stage_cloud_window``) with the REAL ``LocalBackend`` -- not the
``_FullLocalBackend`` stub the head-of-line tests pin at cap -- so the capacity read under test is the one
production runs. The pair is discriminating: the only difference between the admitting test and its
negative control is whether ONE ledger row has a live broker job behind it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from phaze.models.cloud_job import CloudJobStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.backends import LocalBackend
from phaze.tasks import release_awaiting_cloud
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeTaskRouter, seed_active_agent
from tests.analyze.tasks.test_drain_head_of_line import (
    _BASE_TIME,
    _Cfg,
    _CloudStub,
    _make_ctx,
    _make_file,
    _patch_backends,
    _seed,
    _statuses,
)


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


_PHANTOMS = 5

# ``saq_jobs`` is SAQ-owned and unknown to ``Base.metadata``; DROP + CREATE pins the shape this module
# controls (the tests/analyze/tasks/test_ledger_reaper.py idiom), rolled back with the test transaction.
_CREATE_SAQ_JOBS = (
    text("DROP TABLE IF EXISTS saq_jobs"),
    text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"),
)


@pytest.fixture(autouse=True)
def _fresh_resume_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start at the FIFO head; the drain's cross-tick cursor is process-local module state (phaze-1l03t)."""
    monkeypatch.setattr(release_awaiting_cloud, "_resume_cursor", None)


async def _seed_phantom_lane(session: AsyncSession) -> tuple[list[str], uuid.UUID]:
    """Seed ``_PHANTOMS`` ``process_file`` ledger rows with no broker job, and ONE cloud-exhausted awaiting file.

    Returns ``(phantom_keys, exhausted_file_id)``. ``saq_jobs`` exists and is empty -- the measured
    production broker state for ``process_file``.
    """
    for stmt in _CREATE_SAQ_JOBS:
        await session.execute(stmt)
    await session.commit()

    phantom_files = [_make_file(_BASE_TIME) for _ in range(_PHANTOMS)]
    session.add_all(phantom_files)
    await session.commit()
    keys = [f"process_file:{f.id}" for f in phantom_files]
    session.add_all(SchedulingLedger(key=key, function="process_file", routing="agent", payload={"file_id": key.split(":", 1)[1]}) for key in keys)
    await session.commit()

    [exhausted] = await _seed(session, [_make_file(_BASE_TIME)], attempts=_Cfg.cloud_submit_max_attempts)
    return keys, exhausted


def _process_file_enqueues(router: DedupFakeTaskRouter) -> list[str]:
    """The ``file_id`` of every ``process_file`` that landed on any agent queue this tick."""
    return [str(payload.get("file_id")) for queue in router.queues.values() for task, payload in queue.captured if task == "process_file"]


@pytest.mark.asyncio
async def test_a_cloud_exhausted_file_is_admitted_local_when_only_phantom_rows_exist(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five phantom ledger rows, local cap=1: the exhausted file still reaches the local lane in one tick.

    Pre-fix ``LocalBackend.in_flight_count`` read 5 here, ``remaining`` was 0, and this tick returned
    ``{"staged": 0, "skipped": 1}`` forever -- the production shape at 2374 phantoms.
    """
    cloud = _CloudStub()
    local = LocalBackend(id="local", rank=99, cap=1)
    _patch_backends(monkeypatch, [cloud, local])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    _, exhausted = await _seed_phantom_lane(session)

    assert await local.in_flight_count(session) == 0
    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(router))

    assert result == {"staged": 1, "skipped": 0}
    # Local, not cloud: the attempts cap keeps it off the cloud lane entirely.
    assert cloud.dispatched == []
    assert _process_file_enqueues(router) == [str(exhausted)]


@pytest.mark.asyncio
async def test_a_live_local_job_still_holds_the_lane_against_the_exhausted_file(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: give ONE of the ledger rows a live ``active`` broker job and the cap=1 lane is full again.

    Proves the admission above is the liveness probe at work, not the lane ignoring the ledger: a genuinely
    running local analysis still occupies its slot, and the exhausted file is held, untouched.
    """
    cloud = _CloudStub()
    local = LocalBackend(id="local", rank=99, cap=1)
    _patch_backends(monkeypatch, [cloud, local])
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    keys, exhausted = await _seed_phantom_lane(session)
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES (:key, 'active')"), {"key": keys[0]})
    await session.commit()

    assert await local.in_flight_count(session) == 1
    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(router))

    assert result == {"staged": 0, "skipped": 1}
    assert _process_file_enqueues(router) == []
    assert (await _statuses(session, [exhausted]))[exhausted] == CloudJobStatus.AWAITING.value
