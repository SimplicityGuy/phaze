"""Tests for `services/pipeline/cloud.py` (split from test_pipeline.py, phaze-7l8jh).

get_awaiting_cloud_count, get_cloud_phase_counts, get_pushing_count/get_pushed_count, get_cloud_staging_candidates, backfill candidates -- `services/pipeline/cloud.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.services.pipeline._shared import (
    _KUEUE_AND_COMPUTE_TOML,
    _KUEUE_ONLY_TOML,
    CloudJob,
    CloudJobStatus,
    _failed_analysis_for,
    _file,
    _metadata_for,
    _NullSavepoint,
    _seed_cloud_job,
    _seed_process_file_ledger,
    count_backfill_candidates,
    get_awaiting_cloud_count,
    get_backfill_candidates,
    get_pushed_count,
    get_pushing_count,
    pipeline_cloud_mod,
    pytest,
    uuid,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_awaiting_cloud_count_happy_path(session: AsyncSession) -> None:
    """Counts exactly the genuinely-parked awaiting cloud_job rows; other states are excluded (Phase 83, D-15)."""
    a, b, discovered = _file(4), _file(5), _file(6)
    session.add_all([a, b, discovered])
    await session.commit()
    # Phase 83: the count derives from cloud_job(status='awaiting'), not FileRecord.state -- the two held
    # files carry their sidecar rows; the DISCOVERED file has none (and would not be a drain candidate).
    session.add_all(
        [
            CloudJob(id=uuid.uuid4(), file_id=a.id, status=CloudJobStatus.AWAITING.value),
            CloudJob(id=uuid.uuid4(), file_id=b.id, status=CloudJobStatus.AWAITING.value),
        ]
    )
    await session.commit()

    assert await get_awaiting_cloud_count(session) == 2


@pytest.mark.asyncio
async def test_get_awaiting_cloud_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the count to 0 (poll-safe via _safe_count), never raising."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_awaiting_cloud_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_pushing_count_happy_path(session: AsyncSession) -> None:
    """DERIVED "staged" count (phaze-zyoag): STAGING (uploading/uploaded) + SUBMITTED with no registered
    kueue attribution (the historical/no-cloud-registry reading -- see the backend-kind tests below for
    the kueue-attributed carve-out). An ``awaiting`` cloud_job is excluded, proving the status membership.
    """
    await _seed_cloud_job(session, 40, CloudJobStatus.UPLOADING)
    await _seed_cloud_job(session, 41, CloudJobStatus.SUBMITTED)
    await _seed_cloud_job(session, 42, CloudJobStatus.UPLOADED)
    await _seed_cloud_job(session, 43, CloudJobStatus.AWAITING)
    await session.commit()

    assert await get_pushing_count(session) == 3


@pytest.mark.asyncio
async def test_get_pushed_count_happy_path(session: AsyncSession) -> None:
    """DERIVED "analyzing" count (phaze-zyoag): RUNNING only, absent a registered kueue backend to
    attribute a SUBMITTED row to (see the backend-kind tests below). Both ``uploaded`` rows moved OFF
    this card in the phaze-zyoag re-seam -- an uploaded-not-yet-submitted row is pre-submit/staged, never
    "analyzing (landed)".
    """
    await _seed_cloud_job(session, 44, CloudJobStatus.UPLOADED)
    await _seed_cloud_job(session, 45, CloudJobStatus.RUNNING)
    await _seed_cloud_job(session, 46, CloudJobStatus.UPLOADED)
    await _seed_cloud_job(session, 47, CloudJobStatus.UPLOADING)
    await session.commit()

    assert await get_pushed_count(session) == 1


@pytest.mark.asyncio
async def test_get_pushing_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the PUSHING count to 0 (poll-safe via _safe_count)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_pushing_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_pushed_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the PUSHED count to 0 (poll-safe via _safe_count)."""

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("files table unavailable")

    assert await get_pushed_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# phaze-zyoag acceptance 1/2/3: the per-backend-kind seam. SUBMITTED means opposite things on kueue
# (post-upload, admitted-or-queued, D-10) vs compute (mid-rsync, D-10) -- the two window-count cards
# must split it by the row's OWN ``backend_id``, resolved through the SAME registry projection
# (``non_local_backend_kinds``) the per-file lane badges already use.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kueue_submitted_row_is_never_staged_mid_transfer(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """Acceptance 1: a kueue SUBMITTED row (upload done, Job created, waiting on quota) renders 0 in Staged.

    Reproduces the exact bug report shape: one ``vox`` (kueue) row at status=submitted, upload long
    finished, parked on cluster quota. It must NOT be counted as "Staged (pushing) -- mid-transfer".
    """
    backends_toml_env(_KUEUE_ONLY_TOML)
    await _seed_cloud_job(session, 50, CloudJobStatus.SUBMITTED, backend_id="vox")
    await session.commit()

    assert await get_pushing_count(session) == 0, "a kueue SUBMITTED row must never render under the staged/mid-transfer card"
    assert await get_pushed_count(session) == 1, "it belongs in Analyzing (cloud) -- post-submit, in the cloud window"


@pytest.mark.asyncio
async def test_kueue_uploaded_row_is_never_analyzing_landed(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """Acceptance 2: a kueue UPLOADED row (upload finished, submit_cloud_job not yet run) counts as staged.

    ``KueueBackend._reap_stranded_staging`` exists precisely because this window is not necessarily
    brief -- a dead agent or a lost ``s3_upload`` job can strand a row here for hours. It must never be
    counted "Analyzing (cloud) -- landed" while nothing is actually analyzing it.
    """
    backends_toml_env(_KUEUE_ONLY_TOML)
    await _seed_cloud_job(session, 51, CloudJobStatus.UPLOADED, backend_id="vox")
    await session.commit()

    assert await get_pushed_count(session) == 0, "an UPLOADED row must never render under the analyzing/landed card"
    assert await get_pushing_count(session) == 1, "it is still pre-submit -- staged"


@pytest.mark.asyncio
async def test_compute_submitted_row_stays_staged_alongside_a_kueue_submitted_row(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """Acceptance 3: fixing kueue must not break compute. One row of EACH kind, same registry, same poll.

    ``a1`` (compute) SUBMITTED is genuinely mid-rsync (D-10, ``ComputeAgentBackend.dispatch`` writes it
    at dispatch time) and must stay staged; ``vox`` (kueue) SUBMITTED is post-upload and must move to
    analyzing. Seeding both under ONE registry proves the split is per-row, not a global kueue-vs-compute
    toggle.
    """
    backends_toml_env(_KUEUE_AND_COMPUTE_TOML)
    await _seed_cloud_job(session, 52, CloudJobStatus.SUBMITTED, backend_id="a1")
    await _seed_cloud_job(session, 53, CloudJobStatus.SUBMITTED, backend_id="vox")
    await session.commit()

    assert await get_pushing_count(session) == 1, "the compute SUBMITTED row (mid-rsync) must stay staged"
    assert await get_pushed_count(session) == 1, "the kueue SUBMITTED row (post-upload) must move to analyzing"


@pytest.mark.asyncio
async def test_cloud_window_partition_matches_in_flight_exactly(session: AsyncSession, backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """Acceptance 4: Staged + Analyzing partition backends.IN_FLIGHT EXACTLY -- no row in neither, none in both.

    Pins the invariant the phaze-zyoag design doc requires: for every :data:`phaze.services.backends.IN_FLIGHT`
    status, on EITHER backend kind, the row is counted by EXACTLY ONE of the two cards. A future status-enum
    member landing in ``IN_FLIGHT`` without an update here would leave this sum short of the total.
    """
    from phaze.services.backends import IN_FLIGHT

    backends_toml_env(_KUEUE_AND_COMPUTE_TOML)
    idx = 60
    for status in IN_FLIGHT:
        for backend_id in ("a1", "vox"):
            await _seed_cloud_job(session, idx, status, backend_id=backend_id)
            idx += 1
    await session.commit()

    total_rows = len(IN_FLIGHT) * 2
    staged = await get_pushing_count(session)
    analyzing = await get_pushed_count(session)

    assert staged + analyzing == total_rows, "every in-flight row must land in EXACTLY one of the two cards"


@pytest.mark.asyncio
async def test_backfill_candidates_filters_by_state_and_duration(session: AsyncSession) -> None:
    """Only ANALYSIS_FAILED files whose joined duration >= threshold are candidates (D-09/D-10).

    A long ANALYSIS_FAILED file qualifies; a short one, a null-duration one, and a long file
    in another state are all EXCLUDED — proving the explicit duration filter that closes the
    over-enqueue class (NOT a bare ANALYSIS_FAILED count).
    """
    threshold = 5400

    long_failed = _file(7)
    short_failed = _file(8)
    null_failed = _file(9)
    long_other = _file(10)
    session.add_all([long_failed, short_failed, null_failed, long_other])
    await session.flush()
    session.add_all(
        [
            _metadata_for(long_failed.id, 6000.0),
            _metadata_for(short_failed.id, 120.0),
            _metadata_for(null_failed.id, None),
            _metadata_for(long_other.id, 6000.0),
        ]
    )
    # Phase 90 (PR-A): the candidate predicate DERIVES the failure from the ``analysis.failed_at`` marker
    # (``failed_clause(ANALYZE)``), not ``files.state`` -- so the three failed files carry the marker; the
    # long DISCOVERED file (negative control) has none and is excluded by the failure clause, not by state.
    session.add_all([_failed_analysis_for(long_failed.id), _failed_analysis_for(short_failed.id), _failed_analysis_for(null_failed.id)])
    await _seed_process_file_ledger(session, long_failed, short_failed, null_failed)
    await session.commit()

    assert await count_backfill_candidates(session, threshold) == 1

    rows = await get_backfill_candidates(session, threshold)
    assert len(rows) == 1
    record, duration = rows[0]
    assert record.id == long_failed.id
    assert duration == 6000.0


@pytest.mark.asyncio
async def test_backfill_candidates_boundary_is_inclusive(session: AsyncSession) -> None:
    """A file exactly at the threshold qualifies (>=, not >)."""
    threshold = 5400
    at_threshold = _file(11)
    session.add(at_threshold)
    await session.flush()
    session.add(_metadata_for(at_threshold.id, float(threshold)))
    session.add(_failed_analysis_for(at_threshold.id))  # Phase 90 (PR-A): DERIVED failure marker
    await _seed_process_file_ledger(session, at_threshold)
    await session.commit()

    assert await count_backfill_candidates(session, threshold) == 1


def test_kueue_backend_ids_degrades_to_empty_frozenset_on_registry_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings/registry read failure degrades ``_kueue_backend_ids`` to an empty frozenset.

    With no known kueue ids, ``_cloud_window_clauses`` falls back to the pre-fix "SUBMITTED is
    staged" reading everywhere -- wrong only for a live kueue deployment whose registry momentarily
    failed to resolve, never a crash.
    """

    def _boom() -> None:
        msg = "registry TOML unreadable"
        raise RuntimeError(msg)

    monkeypatch.setattr(pipeline_cloud_mod, "get_settings", _boom)

    assert pipeline_cloud_mod._kueue_backend_ids() == frozenset()
