"""Kueue Running-now rows show liveness from ``analysis.updated_at`` (phaze-rvpym).

phaze-x85mi made a pod re-POST its window counts about once a minute while its child is alive, which
moves ``analysis.updated_at``. These tests drive the REAL builder over real rows. Display only: nothing
here (or in the code under test) kills, requeues or bounds a run (D-08, phaze-1b39).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze import job_runner
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import analysis_sizing
from phaze.services.backends import get_running_analyses, lane_detail
from phaze.services.backends.lane_detail import cloud_heartbeat_lost_after_seconds, db_now


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_LANES = [{"id": "k8s", "kind": "kueue"}]


async def _running_pod(session: AsyncSession, name: str, *, analysis: dict[str, int] | None, updated_ago_s: float, now: datetime) -> FileRecord:
    record = FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/test/music/{name}",
        original_filename=name,
        current_path=f"/test/music/{name}",
        file_type="mp3",
        file_size=1024,
    )
    session.add(record)
    await session.flush()
    # The RUNNING stamp predates the analysis row by default, so the row counts as this attempt's.
    session.add(
        CloudJob(file_id=record.id, s3_key="staging/x", status=CloudJobStatus.RUNNING.value, backend_id="k8s", updated_at=now - timedelta(days=1))
    )
    if analysis is not None:
        session.add(AnalysisResult(file_id=record.id, updated_at=now - timedelta(seconds=updated_ago_s), **analysis))
    await session.flush()
    return record


_COUNTS = {"fine_windows_analyzed": 4, "fine_windows_total": 40, "coarse_windows_analyzed": 0, "coarse_windows_total": 10}


@pytest.mark.asyncio
async def test_kueue_row_renders_heartbeat_from_analysis_updated_at(session: AsyncSession) -> None:
    """AC1: heartbeat_at is analysis.updated_at, not the hard-coded None."""
    now = await db_now(session)
    record = await _running_pod(session, "set-01.mp3", analysis=_COUNTS, updated_ago_s=20, now=now)
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == record.id))).scalar_one()
    job.started_at = now - timedelta(minutes=26)
    await session.flush()
    running = await get_running_analyses(session, None, _LANES, now=now)
    assert running is not None and len(running) == 1
    assert running[0].heartbeat_at == now - timedelta(seconds=20)
    assert running[0].started_at == now - timedelta(minutes=26)


def test_cloud_threshold_is_derived_from_the_job_runner_surface_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: one constant, read by both sides -- job_runner's value IS the shared one, and the threshold scales with it."""
    assert job_runner._HEARTBEAT_SURFACE_INTERVAL_SEC == analysis_sizing.HEARTBEAT_SURFACE_INTERVAL_SEC
    assert cloud_heartbeat_lost_after_seconds() > job_runner._HEARTBEAT_SURFACE_INTERVAL_SEC * 2, "must clear the worst healthy POST gap"
    monkeypatch.setattr(lane_detail, "HEARTBEAT_SURFACE_INTERVAL_SEC", 10.0)
    assert lane_detail.cloud_heartbeat_lost_after_seconds() == 10.0 * analysis_sizing.CLOUD_HEARTBEAT_LOST_INTERVALS


@pytest.mark.asyncio
async def test_kueue_heartbeat_fresh_versus_lost(session: AsyncSession) -> None:
    """AC2: a recent update is fresh; silence past the derived threshold is lost."""
    now = await db_now(session)
    threshold = cloud_heartbeat_lost_after_seconds()
    await _running_pod(session, "fresh.mp3", analysis=_COUNTS, updated_ago_s=threshold - 30, now=now)
    await _running_pod(session, "lost.mp3", analysis=_COUNTS, updated_ago_s=threshold + 30, now=now)
    running = await get_running_analyses(session, None, _LANES, now=now)
    assert running is not None
    by_label = {run.label: run for run in running}
    assert by_label["fresh.mp3"].heartbeat_lost is False
    assert by_label["lost.mp3"].heartbeat_lost is True


@pytest.mark.asyncio
async def test_kueue_pod_with_no_counts_is_not_heartbeat_lost(session: AsyncSession) -> None:
    """AC3: no analysis row, or one not yet sized, however stale, has no heartbeat and is never lost."""
    now = await db_now(session)
    await _running_pod(session, "no-row.mp3", analysis=None, updated_ago_s=0, now=now)
    await _running_pod(session, "unsized.mp3", analysis={}, updated_ago_s=3 * 3600, now=now)
    running = await get_running_analyses(session, None, _LANES, now=now)
    assert running is not None and len(running) == 2
    for run in running:
        assert run.windows_total is None
        assert (run.heartbeat_at, run.heartbeat_lost) == (None, False)


@pytest.mark.asyncio
async def test_kueue_redriven_pod_does_not_inherit_the_prior_attempts_heartbeat(session: AsyncSession) -> None:
    """A re-driven file keeps its old counts; an analysis row older than the cloud_job's RUNNING stamp is not this attempt's beat."""
    now = await db_now(session)
    threshold = cloud_heartbeat_lost_after_seconds()
    record = await _running_pod(session, "redriven.mp3", analysis=_COUNTS, updated_ago_s=3 * 3600, now=now)
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == record.id))).scalar_one()
    job.updated_at = now - timedelta(seconds=threshold * 3)  # flipped to RUNNING long after the prior attempt's last beat
    job.started_at = now - timedelta(seconds=threshold * 3)
    await session.flush()
    running = await get_running_analyses(session, None, _LANES, now=now)
    assert running is not None and len(running) == 1
    assert (running[0].heartbeat_at, running[0].heartbeat_lost) == (None, False)
    assert running[0].windows_total is not None, "the counts themselves still render"
