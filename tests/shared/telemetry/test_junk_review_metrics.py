"""The periodic observation through real Postgres and the real OTel SDK (phaze-co6ad)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select, update

from phaze.enums.junk_review import JunkReviewStatus
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.tasks.junk_review_metrics import observe_junk_review_execution


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.shared.telemetry.conftest import TelemetrySink


_COUNT = "phaze.junk_review.executing.count"
_AGE = "phaze.junk_review.executing.oldest_age"
_OBSERVED = "phaze.junk_review.executing.observed_timestamp"


def _review(status: JunkReviewStatus, now: datetime, *, age: int = 7200) -> CompanionJunkReview:
    return CompanionJunkReview(
        id=uuid.uuid4(),
        agent_id="metrics-agent",
        original_path=f"/synthetic/{uuid.uuid4()}.txt",
        sha256_hash="a" * 64,
        file_type="txt",
        file_size=1,
        reason="empty",
        content_group="a" * 64,
        status=status.value,
        created_at=now - timedelta(days=30),
        updated_at=now - timedelta(seconds=age),
        decided_at=None if status is JunkReviewStatus.PENDING else now - timedelta(days=7),
        executed_at=now if status in (JunkReviewStatus.QUARANTINED, JunkReviewStatus.FAILED) else None,
    )


def _ctx() -> dict[str, object]:
    from phaze.database import async_session

    return {"async_session": async_session}


@pytest.mark.asyncio
async def test_only_executing_rows_count_and_age_starts_at_dispatch(session: AsyncSession, telemetry_sink: TelemetrySink) -> None:
    now = datetime.now(UTC)
    rows = [_review(status, now) for status in JunkReviewStatus]
    newer = _review(JunkReviewStatus.EXECUTING, now, age=3600)
    session.add_all([*rows, newer])
    await session.commit()
    before = list((await session.execute(select(CompanionJunkReview.id, CompanionJunkReview.status, CompanionJunkReview.updated_at))).all())

    outcome = await observe_junk_review_execution(_ctx())

    assert outcome["executing"] == 2
    assert outcome["oldest_age_seconds"] == pytest.approx(7200, abs=5)
    points = telemetry_sink.collect()
    assert points[_COUNT][0].value == 2
    assert points[_AGE][0].value == pytest.approx(7200, abs=5)
    assert points[_OBSERVED][0].value == pytest.approx(now.timestamp(), abs=5)
    assert all(not point.attributes for name in (_COUNT, _AGE, _OBSERVED) for point in points[name])
    after = list((await session.execute(select(CompanionJunkReview.id, CompanionJunkReview.status, CompanionJunkReview.updated_at))).all())
    assert after == before, "observation must neither reap nor reset any row"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JunkReviewStatus.APPROVED, JunkReviewStatus.QUARANTINED, JunkReviewStatus.FAILED])
async def test_completion_or_manual_reset_clears_both_gauges(session: AsyncSession, telemetry_sink: TelemetrySink, status: JunkReviewStatus) -> None:
    row = _review(JunkReviewStatus.EXECUTING, datetime.now(UTC))
    session.add(row)
    await session.commit()
    await observe_junk_review_execution(_ctx())
    assert telemetry_sink.collect()[_COUNT][0].value == 1

    await session.execute(
        update(CompanionJunkReview)
        .where(CompanionJunkReview.id == row.id)
        .values(status=status.value, executed_at=None if status is JunkReviewStatus.APPROVED else datetime.now(UTC))
    )
    await session.commit()
    outcome = await observe_junk_review_execution(_ctx())

    assert outcome == {"executing": 0, "oldest_age_seconds": 0.0}
    points = telemetry_sink.collect()
    assert points[_COUNT][0].value == 0
    assert points[_AGE][0].value == 0
    assert points[_OBSERVED][0].value > 0


@pytest.mark.asyncio
async def test_future_transition_time_never_produces_a_negative_age(session: AsyncSession, telemetry_sink: TelemetrySink) -> None:
    session.add(_review(JunkReviewStatus.EXECUTING, datetime.now(UTC), age=-60))
    await session.commit()
    assert await observe_junk_review_execution(_ctx()) == {"executing": 1, "oldest_age_seconds": 0.0}
    assert telemetry_sink.collect()[_AGE][0].value == 0


@pytest.mark.asyncio
async def test_failed_read_does_not_publish_a_fresh_or_empty_observation(telemetry_sink: TelemetrySink) -> None:
    from phaze.telemetry.instruments import set_gauge

    set_gauge(_COUNT, 2)
    set_gauge(_AGE, 7200)
    set_gauge(_OBSERVED, 123)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.execute.side_effect = RuntimeError("database unavailable")
    context = {"async_session": lambda: session}
    with pytest.raises(RuntimeError, match="database unavailable"):
        await observe_junk_review_execution(context)
    points = telemetry_sink.collect()
    assert points[_COUNT][0].value == 2
    assert points[_AGE][0].value == 7200
    assert points[_OBSERVED][0].value == 123


def test_observer_runs_without_a_browser_and_is_not_a_recovery_cron() -> None:
    from phaze.tasks import controller
    from phaze.tasks.reenqueue import recover_orphaned_work

    assert observe_junk_review_execution in controller.settings["functions"]
    cron = next(job for job in controller.settings["cron_jobs"] if job.function is observe_junk_review_execution)
    assert cron.cron == "* * * * *"
    assert all(job.function is not recover_orphaned_work for job in controller.settings["cron_jobs"])
