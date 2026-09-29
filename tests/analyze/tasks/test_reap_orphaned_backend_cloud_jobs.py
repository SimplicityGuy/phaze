"""Tests for the backend-registry-orphan reaper (``phaze.tasks.reap_orphaned_backend_cloud_jobs``).

phaze-pnt12. Every reconcile / stranded-row reaper under ``phaze.services.backends`` is scoped to ONE
resolved backend (``backend_id == self.id``), so a ``cloud_job`` row whose ``backend_id`` names a
RETIRED backend (removed from ``[[backends]]`` config) is invisible to all of them at once. Measured in
production: 35 rows (32 ``uploaded``, 3 ``uploading``) carrying ``backend_id='xenolab'`` stuck since
2026-08-08 -- see the module's own docstring for the full incident writeup and the rule this reaper
implements.

These tests use the SAME helpers the split ``services/backends`` protocol suite uses
(``tests.analyze.services.backends.protocol._shared``) so the seeded shapes (a staging row aged via a
raw ``updated_at`` rewind, a live ``saq_jobs`` broker row) are identical to the ones
``KueueBackend._reap_stranded_staging`` is itself tested against -- this reaper is a deliberately
narrower, registry-agnostic version of that same mechanism.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import update as sa_update

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services.backends.admission import hold_awaiting_cloud as real_hold_awaiting_cloud
import phaze.tasks.reap_orphaned_backend_cloud_jobs as reap_orphan_mod
from phaze.tasks.reap_orphaned_backend_cloud_jobs import (
    _orphaned_backend_clause,
    _requeue_orphaned_staging_rows,
    _seconds_since_last_staging_write,
    reap_orphaned_backend_cloud_jobs,
)
from tests.analyze.services.backends.protocol._shared import (
    _cloud_job_for,
    _seed_live_saq_job,
    _seed_staging_cloud_job,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


def _make_ctx() -> dict[str, Any]:
    from phaze.database import async_session

    return {"async_session": async_session}


async def test_requeues_a_stale_uploading_row_on_a_retired_backend(session: AsyncSession) -> None:
    """The measured production shape: an UPLOADING row whose backend was removed from config entirely.

    No ``[[backends]]`` is configured in this test's process settings at all (the default test
    environment resolves an EMPTY registry), so ``backend_id='xenolab'`` is orphaned by construction --
    exactly the incident: xenolab was retired, so no CURRENT registry names it.
    """
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=90_000)

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 1, "surfaced": 0}
    row = await _cloud_job_for(session, file_id)
    assert row.status == CloudJobStatus.AWAITING.value  # spilled back onto the drain
    assert row.cloud_phase is None  # cleared off the "Running" tile (D-12), mirrors KueueBackend's own reap
    assert row.attempts == 1  # one re-drive attempt spent -- the loop stays bounded
    assert row.backend_id == "xenolab"  # the stale stamp is left in place; a fresh dispatch overwrites it


async def test_requeues_a_stale_uploaded_row_on_a_retired_backend(session: AsyncSession) -> None:
    """An UPLOADED orphan spills within its OWN (tighter) bound -- proves the two bounds are read per-status."""
    # 3600s: past the 900s UPLOADED bound but well under the 21600s UPLOADING bound.
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADED, age_sec=3_600)

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 1, "surfaced": 0}
    row = await _cloud_job_for(session, file_id)
    assert row.status == CloudJobStatus.AWAITING.value


async def test_never_reaps_an_orphaned_row_younger_than_its_bound(session: AsyncSession) -> None:
    """The callback path stays primary: a young orphaned row is left completely alone."""
    uploading_fid = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=60)
    uploaded_fid = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADED, age_sec=60)

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 0, "surfaced": 0}
    assert (await _cloud_job_for(session, uploading_fid)).status == CloudJobStatus.UPLOADING.value
    assert (await _cloud_job_for(session, uploaded_fid)).status == CloudJobStatus.UPLOADED.value


async def test_never_touches_a_row_whose_backend_is_still_configured(session: AsyncSession, backends_toml_env: Any) -> None:
    """A row is only orphaned relative to the CURRENT registry -- a live backend's row is out of scope.

    That row is legitimately ``KueueBackend._reap_stranded_staging``'s job, not this reaper's; this
    reaper touching it too would be a second, competing writer for the same row.
    """
    backends_toml_env(
        """
        [[backends]]
        kind = "kueue"
        id = "kueue-x64"
        rank = 20
        cap = 5
        buckets = ["staging-a"]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "staging-a"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-staging-a"
        """
    )
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=90_000)

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 0, "surfaced": 0}
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.UPLOADING.value


async def test_skips_an_orphaned_uploading_row_whose_s3_upload_job_is_live(session: AsyncSession) -> None:
    """A live ``s3_upload:<file_id>`` broker key means the callback path still owns the row -- never reap."""
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=90_000)
    await _seed_live_saq_job(session, key=f"s3_upload:{file_id}")

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 0, "surfaced": 0}
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.UPLOADING.value


async def test_skips_an_orphaned_uploaded_row_whose_submit_job_is_live(session: AsyncSession) -> None:
    """An UPLOADED row is owned by ``submit_cloud_job:<file_id>``, not ``s3_upload:<file_id>``."""
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADED, age_sec=3_600)
    await _seed_live_saq_job(session, key=f"submit_cloud_job:{file_id}")

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 0, "surfaced": 0}
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.UPLOADED.value


async def test_surfaces_a_submitted_orphan_without_mutating_it(session: AsyncSession) -> None:
    """phaze-202e: no wall clock may kill a run. A SUBMITTED orphan is logged, never spilled.

    Unlike STAGING, this reaper has no cluster access left to check whether the underlying Kueue Job is
    still genuinely running -- age-based reaping here would be exactly the wall-clock kill
    ``reconcile_cloud_jobs.py`` documents as forbidden. Production currently has ZERO rows in this
    shape (the measured 35 are all staging), so the conservative surface-only default costs nothing today.
    """
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.SUBMITTED, age_sec=999_999)

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 0, "surfaced": 1}
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.SUBMITTED.value


async def test_surfaces_a_running_orphan_without_mutating_it(session: AsyncSession) -> None:
    """The RUNNING twin of the SUBMITTED case above -- same rule, same reason."""
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.RUNNING, age_sec=999_999)

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 0, "surfaced": 1}
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.RUNNING.value


async def test_terminal_rows_are_never_touched(session: AsyncSession) -> None:
    """SUCCEEDED/FAILED orphaned rows are out of scope entirely -- neither requeued nor surfaced."""
    succeeded_fid = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.SUCCEEDED, age_sec=999_999)
    failed_fid = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.FAILED, age_sec=999_999)

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 0, "surfaced": 0}
    assert (await _cloud_job_for(session, succeeded_fid)).status == CloudJobStatus.SUCCEEDED.value
    assert (await _cloud_job_for(session, failed_fid)).status == CloudJobStatus.FAILED.value


async def test_reproduces_the_measured_incident_shape(session: AsyncSession) -> None:
    """The exact aggregate the spike measured: 32 uploaded + 3 uploading, all on one retired backend id."""
    uploaded_ids = [await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADED, age_sec=3_600) for _ in range(32)]
    uploading_ids = [await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=90_000) for _ in range(3)]

    tally = await reap_orphaned_backend_cloud_jobs(_make_ctx())

    assert tally == {"requeued": 35, "surfaced": 0}
    for file_id in uploaded_ids + uploading_ids:
        assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value


def test_seconds_since_last_staging_write_coerces_a_naive_timestamp() -> None:
    """``updated_at`` is timestamptz in production (always aware), but the coercion is defensive.

    Mirrors ``KueueBackend``'s own identical helper: a naive datetime (the shape asyncpg would hand
    back for a plain TIMESTAMP WITHOUT TIME ZONE column) must be assumed-UTC rather than raising when
    subtracted from an aware ``now``.
    """
    naive_past = datetime(2026, 1, 1, 0, 0, 0)  # deliberately naive, exercising the coercion
    cloud_job = CloudJob(id=uuid.uuid4(), file_id=uuid.uuid4(), status=CloudJobStatus.UPLOADING.value, updated_at=naive_past)
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    age = _seconds_since_last_staging_write(cloud_job, now)

    assert age == pytest.approx(3600.0)


async def test_orphaned_backend_clause_with_an_empty_registry(session: AsyncSession) -> None:
    """An empty resolved-id set still orphans every non-null ``backend_id`` row (no SAWarning path).

    Exercised through the real reap path (not just the clause builder) so the empty-set branch is
    proven to behave identically to the populated-set branch, not merely to avoid raising.
    """
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=90_000)
    cfg = cast("ControlSettings", get_settings())

    requeued = await _requeue_orphaned_staging_rows(session, cfg, resolved_ids=set())
    await session.commit()

    assert requeued == 1
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value

    # The clause itself: no resolved ids at all still reads as "every non-null backend_id" via
    # ``is_not(None)`` alone -- never a bare ``notin_(())`` (SAWarning-triggering empty IN list).
    clause_sql = str(_orphaned_backend_clause(set()))
    assert "NOT IN" not in clause_sql.upper()


async def test_reap_skips_a_row_that_left_staging_between_snapshot_and_reread(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row terminalized by a callback AFTER the sweep's snapshot is re-read fresh and skipped.

    Mirrors ``KueueBackend``'s identical race guard test: the snapshot captures ids up front, then
    re-reads each row fresh inside the per-row lock, so a callback landing mid-sweep is honoured.
    Called directly against the fixture's own ``session`` (rather than through the top-level
    ``reap_orphaned_backend_cloud_jobs(ctx)`` entry point, which opens its OWN session instance from
    ``ctx["async_session"]``) so patching ``session.get`` actually reaches the code under test.
    """
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=90_000)
    cfg = cast("ControlSettings", get_settings())

    real_get = session.get

    async def _advance_then_get(entity: Any, ident: Any, **kwargs: Any) -> Any:
        await session.execute(sa_update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUCCEEDED.value))
        session.expire_all()
        return await real_get(entity, ident, **kwargs)

    monkeypatch.setattr(session, "get", _advance_then_get)

    requeued = await _requeue_orphaned_staging_rows(session, cfg, resolved_ids=set())

    assert requeued == 0
    monkeypatch.undo()
    # The skip branch rolls back (releasing the advisory lock), which also undoes this cell's
    # simulated mid-sweep advance -- the load-bearing assertion is the reaper never spilled it.
    assert (await _cloud_job_for(session, file_id)).status != CloudJobStatus.AWAITING.value


async def test_reap_loses_the_race_to_a_live_callback_and_takes_a_full_noop(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy-path callback WINS the race: the reaper's CAS misses and it takes a full no-op.

    Mirrors ``KueueBackend``'s identical acceptance test: simulate ``report_uploaded`` landing between
    the reaper's read and its update, so the ``expect_status``-pinned CAS inside ``hold_awaiting_cloud``
    matches 0 rows. Called directly (see the sibling test above for why).
    """
    file_id = await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=90_000)
    cfg = cast("ControlSettings", get_settings())

    async def _callback_wins_first(*args: Any, **kwargs: Any) -> bool:
        await session.execute(sa_update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUBMITTED.value))
        return await real_hold_awaiting_cloud(*args, **kwargs)

    monkeypatch.setattr(reap_orphan_mod, "hold_awaiting_cloud", _callback_wins_first)

    requeued = await _requeue_orphaned_staging_rows(session, cfg, resolved_ids=set())

    assert requeued == 0
    assert (await _cloud_job_for(session, file_id)).status != CloudJobStatus.AWAITING.value


async def test_reap_per_row_guard_survives_a_bad_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """One exploding row never aborts the sweep (per-row rollback guard). Called directly (see above)."""
    await _seed_staging_cloud_job(session, backend_id="xenolab", status=CloudJobStatus.UPLOADING, age_sec=90_000)
    cfg = cast("ControlSettings", get_settings())

    monkeypatch.setattr(reap_orphan_mod, "hold_awaiting_cloud", AsyncMock(side_effect=RuntimeError("boom")))

    requeued = await _requeue_orphaned_staging_rows(session, cfg, resolved_ids=set())  # must NOT raise

    assert requeued == 0
