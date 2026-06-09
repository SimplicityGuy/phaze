"""Tests for the control-side stall reaper (phaze.tasks.scan_reaper.reap_stalled_scans).

The reaper marks RUNNING ScanBatch rows whose progress heartbeat
(``last_progress_at``) is older than ``scan_stall_seconds`` as FAILED. It must:

  - reap a genuinely-stalled RUNNING row (FAILED + "stalled" error_message +
    frozen completed_at + WARNING log),
  - leave a fresh RUNNING row untouched,
  - NEVER touch a LIVE sentinel row, even an ancient one,
  - respect the threshold boundary (a row exactly at threshold is NOT reaped; a
    row just past it IS).

ctx is built with ``ctx["async_session"]`` = a sessionmaker bound to the real
test engine (mirrors test_heartbeat_cron.py's hand-built ctx). The reaper opens
its own session via that sessionmaker; rows seeded + committed through the
``session`` fixture (same engine) are visible to it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import LEGACY_AGENT_ID
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.tasks.scan_reaper import reap_stalled_scans


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class _StubCfg:
    """Minimal stand-in for the settings object the reaper reads."""

    def __init__(self, scan_stall_seconds: int) -> None:
        self.scan_stall_seconds = scan_stall_seconds


def _patch_threshold(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    """Pin reap_stalled_scans' scan_stall_seconds threshold deterministically."""
    monkeypatch.setattr("phaze.tasks.scan_reaper.get_settings", lambda: _StubCfg(seconds))


def _make_ctx(async_engine: AsyncEngine) -> dict[str, Any]:
    """Build a SAQ-shaped ctx whose async_session is bound to the test engine."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm}


async def _seed(
    session: AsyncSession,
    *,
    status: ScanStatus,
    last_progress_at: datetime,
    scan_path: str = "/music/scan",
) -> uuid.UUID:
    """Seed a ScanBatch with an explicit last_progress_at heartbeat; return its id."""
    batch_id = uuid.uuid4()
    session.add(
        ScanBatch(
            id=batch_id,
            agent_id=LEGACY_AGENT_ID,
            scan_path=scan_path,
            status=status.value,
            total_files=0,
            processed_files=0,
            last_progress_at=last_progress_at,
        )
    )
    await session.commit()
    return batch_id


async def _reload(session: AsyncSession, batch_id: uuid.UUID) -> ScanBatch:
    """Re-read a batch from the DB (expire identity-map cache first)."""
    session.expire_all()
    return (await session.execute(select(ScanBatch).where(ScanBatch.id == batch_id))).scalar_one()


@pytest.mark.asyncio
async def test_reaps_stalled_running_batch(
    async_engine: AsyncEngine,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A RUNNING batch quiet past scan_stall_seconds -> FAILED + stalled message + completed_at + WARNING."""
    # Default scan_stall_seconds is 600; a 700s-old heartbeat is unambiguously stalled.
    now = datetime.now(UTC)
    stalled_id = await _seed(session, status=ScanStatus.RUNNING, last_progress_at=now - timedelta(seconds=700))

    with caplog.at_level("WARNING", logger="phaze.tasks.scan_reaper"):
        result = await reap_stalled_scans(_make_ctx(async_engine))

    assert result == {"reaped": 1}
    b = await _reload(session, stalled_id)
    assert b.status == ScanStatus.FAILED.value
    assert b.error_message is not None
    assert "stalled" in b.error_message
    assert b.completed_at is not None
    assert "scan reaped: stalled" in caplog.text


@pytest.mark.asyncio
async def test_fresh_running_batch_untouched(
    async_engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """A RUNNING batch with a recent heartbeat is left RUNNING."""
    now = datetime.now(UTC)
    fresh_id = await _seed(session, status=ScanStatus.RUNNING, last_progress_at=now - timedelta(seconds=30))

    result = await reap_stalled_scans(_make_ctx(async_engine))

    assert result == {"reaped": 0}
    b = await _reload(session, fresh_id)
    assert b.status == ScanStatus.RUNNING.value
    assert b.completed_at is None


@pytest.mark.asyncio
async def test_live_sentinel_never_reaped(
    async_engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """A LIVE watcher sentinel is NEVER reaped, even with an ancient heartbeat."""
    now = datetime.now(UTC)
    live_id = await _seed(
        session,
        status=ScanStatus.LIVE,
        last_progress_at=now - timedelta(days=30),
        scan_path="/music/live-watcher",
    )

    result = await reap_stalled_scans(_make_ctx(async_engine))

    assert result == {"reaped": 0}
    b = await _reload(session, live_id)
    assert b.status == ScanStatus.LIVE.value
    assert b.completed_at is None


@pytest.mark.asyncio
async def test_threshold_boundary(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row exactly AT the threshold is NOT reaped; a row just PAST it IS.

    The reaper predicate is strict ``<`` against ``now - threshold``, so a
    heartbeat exactly at the cutoff survives and one a few seconds older fails.
    """
    _patch_threshold(monkeypatch, seconds=300)
    now = datetime.now(UTC)
    # ~at threshold: 290s old < 300s window -> survives.
    at_id = await _seed(session, status=ScanStatus.RUNNING, last_progress_at=now - timedelta(seconds=290))
    # just past: 320s old > 300s window -> reaped.
    past_id = await _seed(session, status=ScanStatus.RUNNING, last_progress_at=now - timedelta(seconds=320))

    result = await reap_stalled_scans(_make_ctx(async_engine))

    assert result == {"reaped": 1}
    # Extract the status to a plain str immediately after each reload -- _reload
    # calls expire_all(), so holding a second ORM object across a later reload
    # would re-expire the first and trigger a lazy load outside async context.
    at_status = (await _reload(session, at_id)).status
    past_status = (await _reload(session, past_id)).status
    assert at_status == ScanStatus.RUNNING.value
    assert past_status == ScanStatus.FAILED.value


@pytest.mark.asyncio
async def test_no_running_rows_returns_zero(
    async_engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    """With no RUNNING rows at all the reaper returns reaped=0 and commits cleanly."""
    now = datetime.now(UTC)
    await _seed(session, status=ScanStatus.COMPLETED, last_progress_at=now - timedelta(days=1))

    result = await reap_stalled_scans(_make_ctx(async_engine))

    assert result == {"reaped": 0}
