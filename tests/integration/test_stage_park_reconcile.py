"""phaze-uqyn: the every-minute cross-process retro-heal sweep (real PG).

``reconcile_stale_stage_parks`` re-runs the sentinel-guarded ``resume_stage`` un-park for every
stage whose durable ``pipeline_stage_control`` row CURRENTLY reads unpaused. It exists to close
the TOCTOU window between ``resume_stage``'s one-shot un-park and the TTL-cached park writers in
every OTHER process: a row stamped ``scheduled = SENTINEL`` by a stale per-process pause cache
AFTER a real resume already ran is otherwise wedged forever (no automatic recovery path ever
picks it back up -- it reads ``status='queued'``, i.e. "live").

These assertions are only observable against a live ``saq_jobs`` table + a live
``pipeline_stage_control`` row:

* a stray SENTINEL-parked row on a stage that durably reads UNPAUSED is retro-healed
  (``scheduled -> 0``) and counted in the sweep's return value;
* a SENTINEL-parked row on a stage that durably reads PAUSED is left completely untouched --
  the sweep must never disturb a genuinely paused stage's parked backlog;
* a genuine retry-backoff row (``scheduled = now + delay``, never ``== SENTINEL``) is preserved
  regardless of the stage's paused state (the same sentinel guard ``resume_stage`` itself relies
  on, REQ-37-3);
* a healthy backlog (nothing SENTINEL-parked) sweeps to an empty result -- a safe no-op every
  tick indefinitely.

Run with real PG via ``just integration-test``. The package is auto-marked ``integration`` by
``tests/conftest.py``; the explicit ``pytestmark`` below is the documented Plan 37-03 artifact
contract this suite follows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest
from saq.utils import now_seconds
from sqlalchemy import text

from phaze.tasks._shared.stage_control import SENTINEL
from phaze.tasks.stage_park_reconcile import reconcile_stale_stage_parks


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_SCHEDULED_SQL = text("SELECT scheduled FROM saq_jobs WHERE key = :k")
_SET_PAUSED_SQL = text("UPDATE pipeline_stage_control SET paused = :p WHERE stage = :stage")
_SET_SCHEDULED_SQL = text("UPDATE saq_jobs SET scheduled = :s WHERE key = :k")


async def _scheduled(session: AsyncSession, key: str) -> int:
    return int((await session.execute(_SCHEDULED_SQL, {"k": key})).scalar_one())


async def _set_paused(session: AsyncSession, stage: str, *, paused: bool) -> None:
    await session.execute(_SET_PAUSED_SQL, {"p": paused, "stage": stage})
    await session.commit()


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Build the worker-shaped ``ctx`` the cron task expects (mirrors the controller wiring)."""
    return {"async_session": session_factory}


async def test_reconcile_unparks_a_stray_sentinel_row_on_an_unpaused_stage(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """A SENTINEL-parked row on a durably-UNPAUSED stage is retro-healed and counted."""
    queue, session_factory = stage_env
    key = f"process_file:{uuid.uuid4()}"
    await queue.enqueue("process_file", file_id=key.split(":", 1)[1])

    async with session_factory() as session:
        # analyze reads unpaused (stage_env's baseline seed) -- stamp the row SENTINEL directly,
        # modeling the exact race this sweep exists for: a stale per-process cache parked this
        # row AFTER the real resume already ran, so nothing else will ever un-park it again.
        await session.execute(_SET_SCHEDULED_SQL, {"s": SENTINEL, "k": key})
        await session.commit()
        assert await _scheduled(session, key) == SENTINEL

        healed = await reconcile_stale_stage_parks(_ctx(session_factory))

        assert healed.get("analyze") == 1
        assert await _scheduled(session, key) == 0


async def test_reconcile_never_touches_a_genuinely_paused_stage(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """A SENTINEL-parked row on a stage that durably reads PAUSED is left completely alone."""
    queue, session_factory = stage_env
    key = f"extract_file_metadata:{uuid.uuid4()}"
    await queue.enqueue("extract_file_metadata", file_id=key.split(":", 1)[1])

    async with session_factory() as session:
        await session.execute(_SET_SCHEDULED_SQL, {"s": SENTINEL, "k": key})
        await session.commit()
        await _set_paused(session, "metadata", paused=True)

        healed = await reconcile_stale_stage_parks(_ctx(session_factory))

        assert "metadata" not in healed
        assert await _scheduled(session, key) == SENTINEL  # still parked -- genuinely paused


async def test_reconcile_preserves_a_genuine_retry_backoff(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """A retry-backoff row (scheduled = now + delay, never == SENTINEL) survives the sweep untouched."""
    queue, session_factory = stage_env
    key = f"process_file:{uuid.uuid4()}"
    await queue.enqueue("process_file", file_id=key.split(":", 1)[1])
    backoff = int(now_seconds()) + 3600

    async with session_factory() as session:
        await session.execute(_SET_SCHEDULED_SQL, {"s": backoff, "k": key})
        await session.commit()

        healed = await reconcile_stale_stage_parks(_ctx(session_factory))

        assert healed == {}
        assert await _scheduled(session, key) == backoff


async def test_reconcile_is_a_noop_on_a_healthy_backlog(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """Nothing SENTINEL-parked anywhere -> the sweep matches zero rows and returns empty."""
    queue, session_factory = stage_env
    key = f"process_file:{uuid.uuid4()}"
    await queue.enqueue("process_file", file_id=key.split(":", 1)[1])

    healed = await reconcile_stale_stage_parks(_ctx(session_factory))

    assert healed == {}
    async with session_factory() as session:
        assert await _scheduled(session, key) != SENTINEL  # untouched -- never parked to begin with


async def test_reconcile_isolates_one_stages_failure_from_the_others(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One stage's un-park blowing up is degraded (logged + skipped), never the whole sweep.

    ``STAGE_TO_FUNCTION`` iterates metadata BEFORE analyze, so failing metadata's un-park proves
    both halves of the degrade contract at once: the exception is contained to metadata's own
    SAVEPOINT (lines the coverage floor flagged), and the session stays usable for analyze's heal
    in the same tick.
    """
    from phaze.services.stage_control import resume_stage as real_resume_stage
    from phaze.tasks import stage_park_reconcile as sut

    queue, session_factory = stage_env
    key = f"process_file:{uuid.uuid4()}"
    await queue.enqueue("process_file", file_id=key.split(":", 1)[1])

    async def _explode_on_metadata(session: AsyncSession, stage: str) -> int:
        if stage == "metadata":
            msg = "synthetic un-park failure (degrade-path test)"
            raise RuntimeError(msg)
        return await real_resume_stage(session, stage)

    monkeypatch.setattr(sut, "resume_stage", _explode_on_metadata)

    async with session_factory() as session:
        await session.execute(_SET_SCHEDULED_SQL, {"s": SENTINEL, "k": key})
        await session.commit()

        healed = await reconcile_stale_stage_parks(_ctx(session_factory))

        assert "metadata" not in healed  # its failure was contained, not raised
        assert healed.get("analyze") == 1  # the later stage still healed in the same tick
        assert await _scheduled(session, key) == 0
