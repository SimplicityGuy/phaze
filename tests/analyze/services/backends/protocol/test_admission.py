"""Tests for `services/backends/admission.py` (split from test_backends.py, phaze-7l8jh).

`hold_awaiting_cloud` (D-03) -- `services/backends/admission.py`.
"""

from __future__ import annotations

from tests.analyze.services.backends.protocol._shared import *


# === hold_awaiting_cloud(): the shared go-forward awaiting writer (D-01/D-02/D-03/D-13) =====


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_fresh_hold_writes_one_awaiting_row(session: AsyncSession) -> None:
    """D-02: a fresh hold inserts exactly one ``awaiting`` cloud_job row.

    Phase 90 (D-09): the paired AWAITING_CLOUD files.state flip was removed; the cloud_job row is the sole
    authority. The row is visible WITHIN the uncommitted caller session (the helper never commits -- the
    caller owns the commit boundary), so the assertions see it without any commit.
    """
    from sqlalchemy import select

    file = _make_file()
    session.add(file)
    await session.flush()

    await backends.hold_awaiting_cloud(session, file)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == CloudJobStatus.AWAITING.value
    assert rows[0].attempts == 0


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_respamps_failed_spill_row_retaining_spent_budget(session: AsyncSession) -> None:
    """D-03: re-stamping a terminalized FAILED row upserts THE SAME row back to ``awaiting`` (no second row).

    ``uq_cloud_job_file_id`` holds one row per file, so the spill path re-stamps via
    ``on_conflict_do_update`` rather than inserting a fresh row. Passing
    ``attempts=cloud_submit_max_attempts`` retains the budget-spent marker ``select_backend`` reads to
    route the file to local.
    """
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.FAILED.value, attempts=max_attempts))
    await session.flush()

    await backends.hold_awaiting_cloud(session, file, attempts=max_attempts)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalars().all()
    assert len(rows) == 1  # uq_cloud_job_file_id -> still one row (re-stamped, not duplicated)
    assert rows[0].status == CloudJobStatus.AWAITING.value
    assert rows[0].attempts == max_attempts


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_hold_mode_bumps_updated_at_on_conflict(session: AsyncSession) -> None:
    """phaze-ekgk: the hold-mode ON CONFLICT re-stamp resets ``updated_at``, not just status/attempts.

    ``cloud_job.updated_at`` is surfaced as ``lane_entered_at`` and drives ``select_backend``'s D-01/D-03
    local-spill staleness gate (``waited = now - lane_entered_at >= cloud_spill_to_local_after_seconds``).
    ``onupdate=func.now()`` is client-side and NOT injected into an ON CONFLICT DO UPDATE SET, so pre-fix a
    re-held row carried a frozen (days-old) clock, making ``waited`` immediately True and defeating the wait
    window. The fix stamps ``updated_at=func.now()`` in the set_. Here a stale pre-existing row is re-held;
    the clock must pull forward to ~now.
    """
    from sqlalchemy import select, text as sa_text

    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.FAILED.value, attempts=2))
    await session.commit()
    # Force the pre-existing row's lane-entry clock a week into the past.
    await session.execute(
        sa_text("UPDATE cloud_job SET updated_at = now() - make_interval(days => 7) WHERE file_id = :fid"),
        {"fid": file.id},
    )
    await session.commit()
    stale = (
        (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id).execution_options(populate_existing=True))).scalar_one().updated_at
    )

    await backends.hold_awaiting_cloud(session, file)  # hold-mode re-stamp (ON CONFLICT branch)

    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id).execution_options(populate_existing=True))).scalar_one()
    assert row.status == CloudJobStatus.AWAITING.value  # re-held
    assert row.updated_at > stale  # the lane-entry staleness clock was reset off the week-old value


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_hold_branch_returns_true(session: AsyncSession) -> None:
    """D-02: the hold branch (``expect_status is None``) always writes, so it returns ``True``."""
    file = _make_file()
    session.add(file)
    await session.flush()

    result = await backends.hold_awaiting_cloud(session, file)

    assert result is True  # Phase 90 (D-09): the hold writes the cloud_job row (no files.state flip)


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_spill_cas_hit_restamps_clears_phase_and_leaves_state(session: AsyncSession) -> None:
    """Spill branch CAS HIT: an in-flight ``uploading`` row is re-stamped to ``awaiting`` (D-03), ``cloud_phase`` cleared (D-12/WR-01).

    The helper's spill branch does NOT touch ``file.state`` (the caller owns the gated dual-write behind
    the returned bool), so the seeded ``PUSHING`` state is left untouched here.
    """
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.UPLOADING.value, attempts=0, cloud_phase="running"))
    await session.flush()

    result = await backends.hold_awaiting_cloud(
        session,
        file,
        attempts=max_attempts,
        expect_status=(CloudJobStatus.UPLOADING.value, CloudJobStatus.UPLOADED.value),
        clear_cloud_phase=True,
    )

    assert result is True
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert row.status == CloudJobStatus.AWAITING.value
    assert row.attempts == max_attempts
    assert row.cloud_phase is None  # D-12/WR-01: cleared on the s3 spill path


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_spill_cas_miss_is_full_noop(session: AsyncSession) -> None:
    """Spill branch CAS MISS: an already-advanced row (``succeeded``) matches 0 rows -> ``False`` + row UNCHANGED.

    This is the discriminating guard test (SC#2 / T-83-PUSH-CLOBBER): if the spill CAS were replaced by an
    unconditional upsert, this row would be clobbered back to ``awaiting`` and the assertions below would go
    RED. The caller keeps its FULL no-op on a ``False`` return (D-10).
    """
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.SUCCEEDED.value, attempts=1))
    await session.flush()

    result = await backends.hold_awaiting_cloud(session, file, attempts=max_attempts, expect_status=(CloudJobStatus.SUBMITTED.value,))

    assert result is False
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert row.status == CloudJobStatus.SUCCEEDED.value  # UNCHANGED: the CAS matched 0 rows
    assert row.attempts == 1  # attempts NOT bumped -- no unconditional write happened


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_spill_preserves_cloud_phase_when_flag_omitted(session: AsyncSession) -> None:
    """D-12: the spill branch leaves ``cloud_phase`` UNTOUCHED when ``clear_cloud_phase`` is omitted (the push path)."""
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.SUBMITTED.value, attempts=0, cloud_phase="running"))
    await session.flush()

    result = await backends.hold_awaiting_cloud(session, file, attempts=max_attempts, expect_status=(CloudJobStatus.SUBMITTED.value,))

    assert result is True
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert row.status == CloudJobStatus.AWAITING.value
    assert row.cloud_phase == "running"  # D-12: push spill must NOT touch cloud_phase


def test_awaiting_status_is_not_in_the_in_flight_set() -> None:
    """D-03: ``'awaiting'`` stays OUT of :data:`backends.IN_FLIGHT` so a re-stamped hold never inflates a lane.

    ``in_flight_count`` counts ``status IN IN_FLIGHT``; keeping ``awaiting`` out of that tuple is what lets a
    spill re-stamp (or an inert LocalBackend hold-over row, D-13/D-14) exist without corrupting any backend's
    per-lane in-flight accounting.
    """
    assert CloudJobStatus.AWAITING not in backends.IN_FLIGHT
    assert CloudJobStatus.AWAITING.value not in {status.value for status in backends.IN_FLIGHT}
