"""Tests for the phaze-5c6i2 lane-card metrics: queued / working / processed / totals.

Covers the four new ``services.backends`` reads that replace the misleading
``{in_flight}/{cap}`` numeral (see the bead description for the full rationale):

* ``_local_lane_queued_working`` -- the LOCAL lane's SAQ ``count("queued")``/``count("active")``,
  kept SEPARATE (acceptance rule 3).
* ``_cloud_lane_queued_working`` -- a CLOUD lane's queued/working split via the phaze-zyoag seam,
  covering every in-flight ``cloud_job`` status (acceptance rule 4).
* ``_lane_processed_counts`` -- completions attributed by EXECUTION, not by the fileserver agent
  that owns the file record (acceptance rule 6), windowed 24h + lifetime (acceptance rule 7).
* ``get_analyze_queue_totals`` -- the global TOTAL QUEUED (analyze) figure (acceptance rule 2).

And the degrade-safe discipline every one of these reads must follow: an unknown value is ``None``,
never a fabricated 0 (acceptance rule 8) -- and the cross-page consistency phaze-5c6i2 buys by
routing both ``services.backends`` (the ``/s/analyze`` lane cards) and ``services.agent_liveness``
(the ``/admin/agents`` merged table) through the SAME ``services.pipeline._cloud_window_clauses``
seam (acceptance rule 9).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import agent_liveness as liveness_mod, pipeline as pipeline_mod
from phaze.services.agent_liveness import derive_compute_lane_identities

# phaze-dr9df: ``services.backends`` is a PACKAGE now, and the names this module patches live in
# TWO different submodules -- ``_cloud_window_clauses`` / ``_safe_bucket_counts`` are
# ``lane_metrics``' own imports, while ``resolve_backends`` / ``_probe_availability`` are resolved
# by ``get_backend_lane_snapshot`` out of ``lane_snapshot``. Patching the facade reaches neither.
from phaze.services.backends import (
    _cloud_job_succeeded_for_backend,  # noqa: F401 -- imported for symbol-existence coverage below
    _cloud_lane_queued_working,
    _lane_processed_counts,
    _local_lane_queued_working,
    get_analyze_queue_totals,
    get_backend_lane_snapshot,
    lane_metrics as backends_metrics_mod,
    lane_snapshot as backends_snapshot_mod,
)
from tests._queue_fakes import FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------- seeds


def _file(*, agent_id: str = "test-fileserver", file_type: str = "mp3") -> FileRecord:
    """Build a minimal FileRecord seed with a fresh id/hash/path per call."""
    fid = uuid.uuid4()
    return FileRecord(
        agent_id=agent_id,
        id=fid,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{fid}.mp3",
        original_filename=f"{fid}.mp3",
        current_path=f"/music/{fid}.mp3",
        file_type=file_type,
        file_size=1000,
    )


def _cloud_job(file_id: uuid.UUID, *, backend_id: str | None, status: str) -> CloudJob:
    return CloudJob(id=uuid.uuid4(), file_id=file_id, s3_key=f"staging/{file_id}", status=status, backend_id=backend_id)


def _analysis(file_id: uuid.UUID, *, completed_at: datetime | None) -> AnalysisResult:
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, analysis_completed_at=completed_at)


def _mixed_registry_settings() -> SimpleNamespace:
    """A1=compute, k8s=kueue -- the registry every test below monkeypatches ``get_settings`` to."""
    return SimpleNamespace(backends=[SimpleNamespace(id="a1", kind="compute"), SimpleNamespace(id="k8s", kind="kueue")])


# --------------------------------------------------------------------------- AC3: local lane queued/working


@pytest.mark.asyncio
async def test_local_lane_queued_working_stays_separate_not_summed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The local lane's queued/working come from SAQ's OWN count("queued")/count("active"), kept apart."""
    agent = await seed_active_agent(session, "nox", kind="fileserver")
    router = FakeTaskRouter()
    router.queue_for(agent.id, "analyze").set_counts(queued=7, active=3)
    app_state = SimpleNamespace(task_router=router)

    queued, working = await _local_lane_queued_working(session, app_state)

    # Two DISTINCT figures -- never re-summed into one number (that summation is exactly the
    # get_agent_lane_depths defect this bead fixes).
    assert (queued, working) == (7, 3)
    assert queued + working != queued  # sanity: not silently collapsed to one of the two


@pytest.mark.asyncio
async def test_local_lane_queued_working_degrades_to_none_without_app_state(session: AsyncSession) -> None:
    """No app_state (a caller that only needs availability, e.g. derive_cloud_hold_reason) -> (None, None)."""
    assert await _local_lane_queued_working(session, None) == (None, None)


@pytest.mark.asyncio
async def test_local_lane_queued_working_degrades_to_none_without_live_agent(session: AsyncSession) -> None:
    """No live fileserver agent -> (None, None), never (0, 0) (a dead fileserver is not an empty queue)."""
    app_state = SimpleNamespace(task_router=FakeTaskRouter())
    assert await _local_lane_queued_working(session, app_state) == (None, None)


@pytest.mark.asyncio
async def test_local_lane_queued_working_degrades_to_none_on_broker_error(session: AsyncSession) -> None:
    """A broker hiccup (queue.count raises) -> (None, None), never a fabricated 0."""
    agent = await seed_active_agent(session, "nox", kind="fileserver")
    router = FakeTaskRouter()
    router.queue_for(agent.id, "analyze").fail_count()
    app_state = SimpleNamespace(task_router=router)

    assert await _local_lane_queued_working(session, app_state) == (None, None)


# --------------------------------------------------------------------------- AC4: cloud lane queued/working


@pytest.mark.asyncio
async def test_cloud_lane_queued_working_partitions_every_in_flight_status(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixture spanning every in-flight cloud_job status partitions correctly per backend kind (phaze-zyoag seam)."""
    monkeypatch.setattr(pipeline_mod, "get_settings", _mixed_registry_settings)

    files = [_file() for _ in range(5)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            # a1 (compute): UPLOADING/UPLOADED are always staged; a compute SUBMITTED stays staged (D-10).
            _cloud_job(files[0].id, backend_id="a1", status=CloudJobStatus.UPLOADING.value),
            _cloud_job(files[1].id, backend_id="a1", status=CloudJobStatus.UPLOADED.value),
            _cloud_job(files[2].id, backend_id="a1", status=CloudJobStatus.SUBMITTED.value),
            # k8s (kueue): a kueue SUBMITTED is post-submit ("analyzing"); RUNNING is always analyzing.
            _cloud_job(files[3].id, backend_id="k8s", status=CloudJobStatus.SUBMITTED.value),
            _cloud_job(files[4].id, backend_id="k8s", status=CloudJobStatus.RUNNING.value),
        ]
    )
    await session.commit()

    a1_queued, a1_working = await _cloud_lane_queued_working(session, "a1")
    k8s_queued, k8s_working = await _cloud_lane_queued_working(session, "k8s")

    assert (a1_queued, a1_working) == (3, 0)
    assert (k8s_queued, k8s_working) == (0, 2)


@pytest.mark.asyncio
async def test_cloud_lane_queued_working_degrades_to_none_on_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A window-clause derivation failure degrades to (None, None), never a fabricated 0."""

    def _raise() -> tuple[object, object]:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(backends_metrics_mod, "_cloud_window_clauses", _raise)

    assert await _cloud_lane_queued_working(session, "a1") == (None, None)


# --------------------------------------------------------------------------- AC6/AC7: processed attribution + window


@pytest.mark.asyncio
async def test_processed_counts_attribute_by_execution_not_file_owner(session: AsyncSession) -> None:
    """A file owned by fileserver agent A but analyzed on cloud lane B credits B, never A (acceptance rule 6).

    ``_file()`` defaults to the conftest-seeded ``"test-fileserver"`` agent (the FILESERVER axis --
    ``FileRecord.agent_id``); the point under test is that this axis is IGNORED for attribution, which
    keys ONLY on ``cloud_job.backend_id`` (the EXECUTION axis). No second Agent row is needed to make
    that point -- "owned by A, executed by lane B" holds regardless of which agent A is.
    """
    owned_by_a = _file()
    session.add(owned_by_a)
    await session.flush()
    session.add(_cloud_job(owned_by_a.id, backend_id="lane-B", status=CloudJobStatus.SUCCEEDED.value))
    session.add(_analysis(owned_by_a.id, completed_at=datetime.now(UTC)))
    await session.commit()

    b_24h, b_lifetime = await _lane_processed_counts(session, backend_id="lane-B")
    local_24h, local_lifetime = await _lane_processed_counts(session, backend_id=None)
    other_24h, other_lifetime = await _lane_processed_counts(session, backend_id="lane-C")

    assert (b_24h, b_lifetime) == (1, 1)
    assert (local_24h, local_lifetime) == (0, 0)  # NOT credited to local just because agent-A owns it
    assert (other_24h, other_lifetime) == (0, 0)  # NOT credited to an uninvolved lane


@pytest.mark.asyncio
async def test_processed_counts_local_negation_excludes_succeeded_cloud_files(session: AsyncSession) -> None:
    """A genuinely local completion (no succeeded cloud_job) counts under backend_id=None, and ONLY there."""
    local_file = _file()
    session.add(local_file)
    await session.flush()
    session.add(_analysis(local_file.id, completed_at=datetime.now(UTC)))
    await session.commit()

    local_24h, local_lifetime = await _lane_processed_counts(session, backend_id=None)
    cloud_24h, cloud_lifetime = await _lane_processed_counts(session, backend_id="a1")

    assert (local_24h, local_lifetime) == (1, 1)
    assert (cloud_24h, cloud_lifetime) == (0, 0)


@pytest.mark.asyncio
async def test_processed_counts_24h_window_excludes_older_but_lifetime_includes_it(session: AsyncSession) -> None:
    """A completion older than 24h is excluded from processed_24h while still counting toward lifetime (acceptance rule 7)."""
    recent = _file()
    stale = _file()
    session.add_all([recent, stale])
    await session.flush()
    session.add(_analysis(recent.id, completed_at=datetime.now(UTC) - timedelta(hours=1)))
    session.add(_analysis(stale.id, completed_at=datetime.now(UTC) - timedelta(hours=30)))
    await session.commit()

    windowed, lifetime = await _lane_processed_counts(session, backend_id=None)

    assert windowed == 1  # only the 1h-old completion
    assert lifetime == 2  # both


# --------------------------------------------------------------------------- AC2: global total queued


@pytest.mark.asyncio
async def test_analyze_queue_totals_sums_unrouted_plus_per_lane_queued(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """total_queued = unrouted (not_started) + sum(lane.queued); the unrouted remainder is visible."""

    async def _fake_buckets(_session: AsyncSession, _stage: object) -> dict[str, int]:
        return {"not_started": 5, "in_flight": 0, "done": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(backends_metrics_mod, "_safe_bucket_counts", _fake_buckets)
    lanes = [{"queued": 2}, {"queued": 3}]

    totals = await get_analyze_queue_totals(session, lanes)

    assert totals == {"total_queued": 10, "unrouted_queued": 5}
    # AC2: total >= sum(per-lane queued), and the unrouted remainder (5) is the visible difference.
    assert totals["total_queued"] >= sum(lane["queued"] for lane in lanes)
    assert totals["total_queued"] - sum(lane["queued"] for lane in lanes) == totals["unrouted_queued"]


@pytest.mark.asyncio
async def test_analyze_queue_totals_degrades_to_none_when_any_lane_queued_is_unknown(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """One degraded (None) lane queued figure propagates to the WHOLE total -- never a silent undercount."""

    async def _fake_buckets(_session: AsyncSession, _stage: object) -> dict[str, int]:
        return {"not_started": 5, "in_flight": 0, "done": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(backends_metrics_mod, "_safe_bucket_counts", _fake_buckets)
    lanes = [{"queued": 2}, {"queued": None}]

    totals = await get_analyze_queue_totals(session, lanes)

    assert totals == {"total_queued": None, "unrouted_queued": 5}


# --------------------------------------------------------------------------- AC9: cross-page single source of truth


@pytest.mark.asyncio
async def test_lane_card_and_admin_agents_report_identical_queued_working_for_one_lane(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/s/analyze's lane card and /admin/agents' ComputeLane derive queued/working from ONE shared seam (rule 9)."""
    from phaze.services.backends import KueueBackend

    monkeypatch.setattr(pipeline_mod, "get_settings", _mixed_registry_settings)
    monkeypatch.setattr(liveness_mod, "get_settings", _mixed_registry_settings)
    monkeypatch.setattr(backends_snapshot_mod, "resolve_backends", lambda _settings: [KueueBackend(id="k8s", rank=10, cap=4)])

    async def _fake_probe(_session: object, _backends: object) -> dict[str, bool]:
        return {"k8s": True}

    monkeypatch.setattr(backends_snapshot_mod, "_probe_availability", _fake_probe)

    files = [_file() for _ in range(3)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            _cloud_job(files[0].id, backend_id="k8s", status=CloudJobStatus.SUBMITTED.value),
            _cloud_job(files[1].id, backend_id="k8s", status=CloudJobStatus.RUNNING.value),
            _cloud_job(
                files[2].id, backend_id="a1", status=CloudJobStatus.UPLOADING.value
            ),  # a different (compute) lane -- must not leak into k8s's figures
        ]
    )
    await session.commit()

    lanes = await get_backend_lane_snapshot(session, None)
    card_lane = next(lane for lane in lanes if lane["id"] == "k8s")

    compute_lanes = await derive_compute_lane_identities(session)
    admin_lane = next(lane for lane in compute_lanes if lane.backend_id == "k8s")

    assert card_lane["queued"] == admin_lane.queued
    assert card_lane["working"] == admin_lane.working
    assert (card_lane["queued"], card_lane["working"]) == (0, 2)
