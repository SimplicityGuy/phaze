"""phaze-cxjx: pause/resume must take the control-row lock so no-net-change writes still serialize (real PG).

WR-02 added ``lock=True`` (``SELECT ... FOR UPDATE``) only to ``set_priority``. ``pause`` and
``resume`` used the default ``lock=False``, relying on SQLAlchemy's autoflush UPDATE to provide
serialization -- except that UPDATE is documented to be ELIDED when the attribute has no net
change (a repeat Pause reading an already-``paused=true`` row, or a second dashboard tab whose
poll is stale). An elided flush takes NO row lock, so that Pause's unguarded ``_PAUSE_SQL``
backlog park could commit AFTER a concurrent Resume, leaving the durable control row at
``paused=false`` while the entire queued backlog sat parked at ``SENTINEL`` forever -- see the
bead body for the full interleaving.

These tests prove the fix directly: ``pause`` and ``resume`` now take ``SELECT ... FOR UPDATE``
on the control row EVERY time, regardless of whether the write is a net change, so a concurrent
control action on the SAME stage must block behind it. A hermetic single-connection fixture
cannot express a real block (there is no second transaction to contend with), so this lives on
the real-PG ``stage_env`` fixture where each call opens its OWN pool connection and races for
real -- the same style as ``test_scan_reaper_concurrency.py``'s row-lock regression: a waiter is
observed via ``pg_locks`` (deterministic), not guessed via a sleep duration.

Run with real PG via ``just integration-test``. Package auto-marked ``integration`` by
``tests/conftest.py``; the explicit ``pytestmark`` is the Plan 37-03 artifact contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from phaze.models import PipelineStageControl
from phaze.routers import pipeline_stages
from tests._lock_barrier import run_contender_behind_held_lock


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration


async def test_pause_blocks_on_concurrently_held_control_row_lock(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """``pause`` must take ``FOR UPDATE`` on the control row -- it blocks behind a concurrent holder.

    The row is pre-seeded ``paused=true`` so ``pause``'s ``row.paused = True`` write is a
    NO-NET-CHANGE -- exactly the elision hazard from the bug report. Before the fix
    (``lock=False``), that no-op write means SQLAlchemy's autoflush emits no control-row UPDATE
    at all, so ``pause`` never issues a locking SELECT, is never observed as a waiter here, and
    completes immediately regardless of the held lock -- which the barrier reports on the pre-fix
    code as ``LockBarrierError`` ("the contender finished without ever being observed queued"),
    same as the analogous ``resume`` test below.
    """
    _queue, session_factory = stage_env

    async with session_factory() as seed_session:
        await pipeline_stages.pause(stage="analyze", session=seed_session)

    # Hold the control row's write lock on its own connection BEFORE pause is even launched --
    # simulates a concurrent control action (e.g. an in-flight Resume) already owning the row.
    holder_session = session_factory()
    await holder_session.execute(select(PipelineStageControl).where(PipelineStageControl.stage == "analyze").with_for_update())

    async def _call_pause() -> dict[str, object]:
        async with session_factory() as session:
            return await pipeline_stages.pause(stage="analyze", session=session)

    pause_result = await run_contender_behind_held_lock(session_factory, holder_session, _call_pause(), release=holder_session.commit)

    assert pause_result == {"stage": "analyze", "priority": 50, "paused": True}


async def test_resume_blocks_on_concurrently_held_control_row_lock(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """``resume`` must also take ``FOR UPDATE`` on the control row (the same fix as ``pause``)."""
    _queue, session_factory = stage_env

    holder_session = session_factory()
    await holder_session.execute(select(PipelineStageControl).where(PipelineStageControl.stage == "analyze").with_for_update())

    async def _call_resume() -> dict[str, object]:
        async with session_factory() as session:
            return await pipeline_stages.resume(stage="analyze", session=session)

    resume_result = await run_contender_behind_held_lock(session_factory, holder_session, _call_resume(), release=holder_session.commit)

    assert resume_result == {"stage": "analyze", "priority": 50, "paused": False}
