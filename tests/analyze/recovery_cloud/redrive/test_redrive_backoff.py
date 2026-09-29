"""A charged re-drive waits out a backoff, holding its slot, before its fresh submit is enqueued (phaze-d28sn).

Spike ``phaze-79mu7``: reconcile re-drove a failed submit on the next one-minute tick, so a file spent one
submit and three re-drives in about three minutes, and any fault longer than that exhausted every
in-flight file. A charged re-drive now stamps ``cloud_job.redrive_after`` from the
``cloud_redrive_backoff_sec`` schedule and enqueues nothing until a tick finds that instant passed.

Rule 3: every failure reaches reconcile through the REAL ``list_pods_for_job`` -> kr8s ``Pod`` path, with
only the kube HTTP transport stubbed (the breaker tests' ``_Cluster`` serves each Job's PodList as the API
server would, from ``_terminated_pod_manifest``). Time is never slept: a test moves ``redrive_after`` into
the past to stand for the wait elapsing, and asserts the stamped deadline exactly against ``last_failed_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import update
import structlog

from phaze.config import ControlSettings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.tasks import reconcile_cloud_jobs as reconcile_mod
from phaze.tasks.reconcile_cloud_jobs import PENDING_SUBMIT_CONFIRMATION_SECONDS, reconcile_cloud_jobs, redrive_backoff_seconds
from phaze.tasks.submit_cloud_job import submit_cloud_job_key
from tests._queue_fakes import DedupFakeQueue
from tests.analyze.recovery_cloud.redrive.test_control_plane_breaker import _install, _resubmit
from tests.analyze.recovery_cloud.redrive.test_reconcile import _make_ctx, _read_cloud_job, _seed


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


_EXIT_DOWNLOAD = 10  # job_runner.EXIT_DOWNLOAD -- the exit the 09-26/27 incident charged, and still a charged one
_UNREACHABLE = 14  # job_runner.EXIT_CONTROL_PLANE_UNREACHABLE -- uncharged, bounded by the breaker instead
_SCHEDULE = (120, 600, 1800)


def _set_backoff(schedule: tuple[int, ...]) -> None:
    """Give the settings stub ``_install`` put in place the schedule under test."""
    reconcile_mod.get_settings().cloud_redrive_backoff_sec = schedule  # type: ignore[attr-defined]  # the SimpleNamespace stub


async def _elapse_backoff(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Stand for the wait running out: move the row's deadline one second into the past."""
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(redrive_after=datetime.now(UTC) - timedelta(seconds=1)))
    await session.commit()


def _submits(queue: DedupFakeQueue) -> list[str]:
    return [task for task, _ in queue.captured]


def test_the_schedule_walks_its_entries_and_repeats_the_last() -> None:
    """Entry n is the wait before the n-th charged re-drive; past the end the last entry repeats."""
    assert [redrive_backoff_seconds(_SCHEDULE, n) for n in (1, 2, 3, 4, 7)] == [120, 600, 1800, 1800, 1800]
    assert redrive_backoff_seconds((0,), 1) == 0
    assert redrive_backoff_seconds((), 1) == 0
    assert redrive_backoff_seconds(_SCHEDULE, 0) == 0


def test_the_default_schedule_spans_at_least_30_minutes_across_all_attempts() -> None:
    """Acceptance (a): the default waits sum to >= 30 min over every re-drive the default cap allows."""
    fields = ControlSettings.model_fields
    schedule = fields["cloud_redrive_backoff_sec"].default
    cap = fields["cloud_submit_max_attempts"].default
    assert schedule == _SCHEDULE
    assert sum(redrive_backoff_seconds(schedule, n) for n in range(1, cap + 1)) == 42 * 60


def test_the_schedule_is_configurable_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance (a): ``PHAZE_CLOUD_REDRIVE_BACKOFF_SEC`` is a comma-separated list; bad entries fail at boot."""
    monkeypatch.setenv("PHAZE_CLOUD_REDRIVE_BACKOFF_SEC", "60, 300")
    assert ControlSettings(_env_file=None).cloud_redrive_backoff_sec == (60, 300)  # type: ignore[call-arg]
    for bad in ("", "-1", "86401", "soon"):
        monkeypatch.setenv("PHAZE_CLOUD_REDRIVE_BACKOFF_SEC", bad)
        with pytest.raises(ValueError, match=r"(?i)cloud_redrive_backoff_sec"):
            ControlSettings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_a_failing_file_is_re_driven_on_the_schedule_and_never_before(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any
) -> None:
    """Acceptance (b)(c): each charged re-drive waits its schedule entry, holding the slot, charged once per failure.

    The whole chain of the default cap: submit + three re-drives, then the spill. Between each failure and
    its re-drive, a tick that has not reached ``redrive_after`` re-drives nothing and writes nothing.
    """
    cluster, _s3 = _install(monkeypatch, kube_respx)
    _set_backoff(_SCHEDULE)
    fid, name = await _seed(session, attempts=0)
    cluster.exited[name] = _EXIT_DOWNLOAD
    queue = DedupFakeQueue("controller")

    for attempt, wait in enumerate(_SCHEDULE, start=1):
        # The failure tick: the Job is deleted and the attempt charged, but no submit is enqueued.
        with structlog.testing.capture_logs() as logs:
            tally = await reconcile_cloud_jobs(_make_ctx(queue))
        assert (tally["redriven"], tally["backoff_held"]) == (0, 1)
        assert _submits(queue) == ["submit_cloud_job"] * (attempt - 1)
        assert name in cluster.deleted
        cj = await _read_cloud_job(session, fid)
        assert cj.attempts == attempt
        assert cj.status == CloudJobStatus.SUBMITTED.value  # HOLD: still in-flight, so the slot stays taken
        assert cj.kueue_workload is None
        assert cj.cloud_phase is None
        assert (cj.last_exit_code, cj.last_failure_reason) == (_EXIT_DOWNLOAD, "Error")
        assert cj.last_failed_at is not None and cj.redrive_after is not None
        assert cj.redrive_after - cj.last_failed_at == timedelta(seconds=wait)  # the clock is last_failed_at
        [deferred] = [e for e in logs if str(e.get("event", "")).startswith("reconcile_cloud_jobs: re-drive charged, waiting out its backoff")]
        assert (deferred["attempt"], deferred["budget"], deferred["backoff_seconds"]) == (attempt, "attempts", wait)

        # A tick inside the wait: nothing enqueued, nothing charged, nothing written.
        waiting_since = cj.updated_at
        tally = await reconcile_cloud_jobs(_make_ctx(queue))
        assert (tally["redriven"], tally["backoff_held"]) == (0, 1)
        assert _submits(queue) == ["submit_cloud_job"] * (attempt - 1)
        cj = await _read_cloud_job(session, fid)
        assert (cj.attempts, cj.updated_at, cj.status) == (attempt, waiting_since, CloudJobStatus.SUBMITTED.value)

        # The wait runs out: the next tick enqueues the submit, still without charging anything.
        await _elapse_backoff(session, fid)
        tally = await reconcile_cloud_jobs(_make_ctx(queue))
        assert (tally["redriven"], tally["backoff_held"]) == (1, 0)
        assert _submits(queue) == ["submit_cloud_job"] * attempt
        assert queue.captured_policy[-1]["key"] == submit_cloud_job_key(fid)
        cj = await _read_cloud_job(session, fid)
        assert (cj.attempts, cj.redrive_after) == (attempt, None)

        # submit_cloud_job runs; the fresh pod fails the same way.
        queue.finish(submit_cloud_job_key(fid))
        await _resubmit(session, cluster, fid, name, exit_code=_EXIT_DOWNLOAD)

    # The fourth failure exceeds the cap of 3: the ordinary at-cap spill, with no backoff left to wait.
    tally = await reconcile_cloud_jobs(_make_ctx(queue))
    assert (tally["failed"], tally["backoff_held"], tally["redriven"]) == (1, 0, 0)
    cj = await _read_cloud_job(session, fid)
    assert (cj.status, cj.attempts, cj.redrive_after) == (CloudJobStatus.AWAITING.value, 3, None)


@pytest.mark.asyncio
async def test_a_backoff_longer_than_the_pending_bound_never_ages_the_row_into_a_vanished_terminal(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any
) -> None:
    """``redrive_after`` separates a waiting row from a pending-confirmation one, whose age bound would charge it again.

    A row that has waited longer than :data:`PENDING_SUBMIT_CONFIRMATION_SECONDS` (``updated_at`` aged past
    it) is re-driven when its backoff runs out -- not terminalized as a vanished submit, which would spend
    a second attempt on a submit that was never enqueued.
    """
    cluster, _s3 = _install(monkeypatch, kube_respx)
    _set_backoff((PENDING_SUBMIT_CONFIRMATION_SECONDS * 2,))
    fid, name = await _seed(session, attempts=0)
    cluster.exited[name] = _EXIT_DOWNLOAD
    queue = DedupFakeQueue("controller")
    await reconcile_cloud_jobs(_make_ctx(queue))
    assert (await _read_cloud_job(session, fid)).attempts == 1

    long_ago = datetime.now(UTC) - timedelta(seconds=PENDING_SUBMIT_CONFIRMATION_SECONDS * 2)
    await session.execute(update(CloudJob).where(CloudJob.file_id == fid).values(updated_at=long_ago))
    await session.commit()
    tally = await reconcile_cloud_jobs(_make_ctx(queue))
    assert (tally["redriven"], tally["failed"], tally["backoff_held"]) == (0, 0, 1)

    await session.execute(
        update(CloudJob).where(CloudJob.file_id == fid).values(updated_at=long_ago, redrive_after=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    tally = await reconcile_cloud_jobs(_make_ctx(queue))
    assert (tally["redriven"], tally["failed"]) == (1, 0)
    cj = await _read_cloud_job(session, fid)
    assert (cj.attempts, cj.status, cj.redrive_after) == (1, CloudJobStatus.SUBMITTED.value, None)
    # The enqueue moved updated_at, so the pending-confirmation bound now runs from the enqueue.
    assert (datetime.now(UTC) - cj.updated_at).total_seconds() < PENDING_SUBMIT_CONFIRMATION_SECONDS


@pytest.mark.asyncio
async def test_an_uncharged_unreachable_exit_is_re_driven_at_once_whatever_the_schedule(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any
) -> None:
    """The backoff applies to CHARGED re-drives only; exit 14 stays the breaker's to bound (phaze-j0ixx)."""
    cluster, _s3 = _install(monkeypatch, kube_respx)
    _set_backoff(_SCHEDULE)
    fid, name = await _seed(session, attempts=1)
    cluster.exited[name] = _UNREACHABLE
    queue = DedupFakeQueue("controller")

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    assert (tally["redriven"], tally["backoff_held"], tally["unreachable_held"]) == (1, 0, 0)
    assert _submits(queue) == ["submit_cloud_job"]
    cj = await _read_cloud_job(session, fid)
    assert (cj.attempts, cj.redrive_after, cj.last_exit_code) == (1, None, _UNREACHABLE)


@pytest.mark.asyncio
async def test_a_running_pod_is_never_held_or_re_driven_by_the_backoff(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any
) -> None:
    """Acceptance (d), D-08: the backoff is read only for a row with no Job, so no deadline can end a running analysis.

    Even a row carrying a stray, long-expired ``redrive_after`` is left to its running Job: not deleted,
    not re-submitted, and not charged.
    """
    cluster, _s3 = _install(monkeypatch, kube_respx)
    _set_backoff(_SCHEDULE)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value, attempts=2)
    cluster.running.add(name)
    await session.execute(update(CloudJob).where(CloudJob.file_id == fid).values(redrive_after=datetime.now(UTC) - timedelta(days=1)))
    await session.commit()
    queue = DedupFakeQueue("controller")

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    assert (tally["redriven"], tally["backoff_held"], tally["failed"]) == (0, 0, 0)
    assert queue.captured == []
    assert cluster.deleted == []
    cj = await _read_cloud_job(session, fid)
    assert (cj.status, cj.attempts, cj.kueue_workload) == (CloudJobStatus.RUNNING.value, 2, name)
