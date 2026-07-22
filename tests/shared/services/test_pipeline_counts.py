"""Tests for the Phase 54 (D-06) degrade-safe ``get_inadmissible_count`` dashboard reader, the
Phase 55 ``get_cloud_phase_counts`` admission-state reader, and ``derive_cloud_hold_reason`` (the
Cloud Routing card's truthful hold-reason sub-caption).

``get_inadmissible_count`` drives the Inadmissible operator alert: it returns
``COUNT(cloud_job WHERE inadmissible = true)`` and, mirroring
:func:`get_awaiting_cloud_count`, degrades to 0 on any DB error so the hot 5s
``/pipeline/stats`` poll never 500s (T-54-10).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.models.route_control import RouteControl
from phaze.services import backends as backends_mod
from phaze.services.backends import ComputeAgentBackend, LocalBackend, derive_cloud_hold_reason
from phaze.services.pipeline import get_cloud_phase_counts, get_inadmissible_count
from tests._queue_fakes import seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _file(i: int) -> FileRecord:
    """Build a minimal FileRecord seed (CloudJob.file_id is a unique FK to files.id)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"c{i:063d}"[:64],
        original_path=f"/music/cloud{i}.mp3",
        original_filename=f"cloud{i}.mp3",
        current_path=f"/music/cloud{i}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _cloud_job(file_id: uuid.UUID, *, inadmissible: bool, status: str = CloudJobStatus.SUBMITTED.value) -> CloudJob:
    """Build a CloudJob seed flagged inadmissible (or not) for the given file_id."""
    return CloudJob(
        id=uuid.uuid4(),
        file_id=file_id,
        s3_key=f"phaze-staging/{file_id}",
        status=status,
        inadmissible=inadmissible,
    )


@pytest.mark.asyncio
async def test_get_inadmissible_count_happy_path(session: AsyncSession) -> None:
    """Counts exactly the cloud_job rows with inadmissible=true; admissible rows are excluded."""
    files = [_file(i) for i in range(3)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            _cloud_job(files[0].id, inadmissible=True),
            _cloud_job(files[1].id, inadmissible=True),
            _cloud_job(files[2].id, inadmissible=False),
        ]
    )
    await session.commit()

    assert await get_inadmissible_count(session) == 2


@pytest.mark.asyncio
async def test_get_inadmissible_count_excludes_terminal_rows(session: AsyncSession) -> None:
    """CR-01: a terminal row (SUCCEEDED/FAILED) that was transiently inadmissible never inflates the alert.

    cloud_job rows are never deleted, so a row flagged inadmissible that later succeeds would keep the
    banner lit if the count weren't scoped to in-flight (SUBMITTED/RUNNING) rows."""
    files = [_file(i) for i in range(3)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            _cloud_job(files[0].id, inadmissible=True, status=CloudJobStatus.SUBMITTED.value),  # counted
            _cloud_job(files[1].id, inadmissible=True, status=CloudJobStatus.SUCCEEDED.value),  # terminal -> excluded
            _cloud_job(files[2].id, inadmissible=True, status=CloudJobStatus.FAILED.value),  # terminal -> excluded
        ]
    )
    await session.commit()

    assert await get_inadmissible_count(session) == 1


@pytest.mark.asyncio
async def test_get_inadmissible_count_zero_when_all_admissible(session: AsyncSession) -> None:
    """Healthy Pending workloads (inadmissible=false) count 0 — the alert stays silent (D-06)."""
    file = _file(0)
    session.add(file)
    await session.flush()
    session.add(_cloud_job(file.id, inadmissible=False))
    await session.commit()

    assert await get_inadmissible_count(session) == 0


@pytest.mark.asyncio
async def test_get_inadmissible_count_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades the count to 0 (poll-safe via _safe_count), never raising.

    Mirrors the :func:`get_awaiting_cloud_count` degrade discipline: the hot 5s
    ``/pipeline/stats`` poll must keep serving instead of 500ing when the cloud_job read fails.
    """

    class _ExplodingSession:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("cloud_job table unavailable")

        async def rollback(self) -> None:
            return None

    assert await get_inadmissible_count(_ExplodingSession()) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Phase 55 (55-05, D-04 / KROUTE-06): get_cloud_phase_counts — the degrade-safe
# per-cloud_phase reader driving the admission-state dashboard card. Mirrors
# get_inadmissible_count: each of the four counts (queued_behind_quota / admitted
# / running / finished) is a _safe_count-backed read that degrades to 0 on a DB
# error so the hot 5s /pipeline/stats poll never 500s. NULL cloud_phase rows
# (a1/local deploys) count toward none.
# ---------------------------------------------------------------------------


def _cloud_job_phase(file_id: uuid.UUID, *, cloud_phase: str | None) -> CloudJob:
    """Build a CloudJob seed in the given cloud_phase (NULL for a1/local rows)."""
    return CloudJob(
        id=uuid.uuid4(),
        file_id=file_id,
        s3_key=f"phaze-staging/{file_id}",
        status=CloudJobStatus.SUBMITTED.value,
        cloud_phase=cloud_phase,
    )


@pytest.mark.asyncio
async def test_get_cloud_phase_counts_per_phase(session: AsyncSession) -> None:
    """Each count reflects only its matching cloud_phase; NULL (a1/local) rows count toward none."""
    files = [_file(i) for i in range(7)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            _cloud_job_phase(files[0].id, cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value),
            _cloud_job_phase(files[1].id, cloud_phase=CloudPhase.ADMITTED.value),
            _cloud_job_phase(files[2].id, cloud_phase=CloudPhase.ADMITTED.value),
            _cloud_job_phase(files[3].id, cloud_phase=CloudPhase.RUNNING.value),
            _cloud_job_phase(files[4].id, cloud_phase=CloudPhase.FINISHED.value),
            _cloud_job_phase(files[5].id, cloud_phase=None),  # a1/local — counts toward none
            _cloud_job_phase(files[6].id, cloud_phase=None),  # a1/local — counts toward none
        ]
    )
    await session.commit()

    counts = await get_cloud_phase_counts(session)

    assert counts == {
        "queued_behind_quota": 1,
        "admitted": 2,
        "running": 1,
        "finished": 1,
    }


@pytest.mark.asyncio
async def test_get_cloud_phase_counts_all_zero_when_no_k8s_rows(session: AsyncSession) -> None:
    """With only NULL cloud_phase rows (a1/local deploys) every count is 0 — the card stays quiet."""
    file = _file(0)
    session.add(file)
    await session.flush()
    session.add(_cloud_job_phase(file.id, cloud_phase=None))
    await session.commit()

    counts = await get_cloud_phase_counts(session)

    assert counts == {"queued_behind_quota": 0, "admitted": 0, "running": 0, "finished": 0}


@pytest.mark.asyncio
async def test_get_cloud_phase_counts_degrades_to_zero_on_db_error() -> None:
    """A forced read error degrades EVERY count to 0 (poll-safe via _safe_count), never raising.

    Mirrors :func:`get_inadmissible_count`: the hot 5s ``/pipeline/stats`` poll must keep serving
    the quiet empty carrier rather than 500ing when the cloud_job read fails (T-55-CARD-01).
    """

    class _ExplodingSession:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("cloud_job table unavailable")

        async def rollback(self) -> None:
            return None

    counts = await get_cloud_phase_counts(_ExplodingSession())  # type: ignore[arg-type]

    assert counts == {"queued_behind_quota": 0, "admitted": 0, "running": 0, "finished": 0}


# ---------------------------------------------------------------------------
# derive_cloud_hold_reason -- the Cloud Routing card's truthful hold-reason sub-caption
# (services.backends.derive_cloud_hold_reason). Table-driven: one test per branch of the drain's
# own gate ladder (cloud disabled -> force-local -> no lane reachable -> every lane full -> no
# fileserver agent online -> genuinely queued), plus the degrade-to-neutral path. ``resolve_backends``
# / ``_probe_availability`` are monkeypatched (mirrors tests/shared/services/test_lane_snapshot.py's
# idiom) so each cell controls exactly one gate without a real registry TOML or a live probe;
# ``get_settings`` is monkeypatched only for the ``cloud_enabled`` flag each cell needs.
# ---------------------------------------------------------------------------


def _cloud_hold_settings(*, cloud_enabled: bool) -> Any:
    """A minimal ``cloud_enabled``-carrying stand-in -- derive_cloud_hold_reason reads only this."""
    return SimpleNamespace(cloud_enabled=cloud_enabled)


def _cloud_job_backend(file_id: uuid.UUID, *, backend_id: str, status: str) -> CloudJob:
    """Build an in-flight ``cloud_job`` row attributed to ``backend_id`` (a compute lane, no S3 key)."""
    return CloudJob(id=uuid.uuid4(), file_id=file_id, s3_key=None, status=status, backend_id=backend_id)


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_cloud_disabled(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE 1: ``cloud_enabled`` False -> "cloud routing disabled", before any DB/route-control read."""
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=False))

    assert await derive_cloud_hold_reason(session) == "cloud routing disabled"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_force_local(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE 2: a persisted ``force_local`` override holds regardless of lane health."""
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=True))
    session.add(RouteControl(id="global", force_local=True))
    await session.commit()

    assert await derive_cloud_hold_reason(session) == "held — cloud routing paused (force-local)"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_no_lane_available(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE 3: every registered lane offline -> "held — no cloud backend reachable"."""
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=True))
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [ComputeAgentBackend(id="a1", rank=10, cap=2)])
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={"a1": False}))

    assert await derive_cloud_hold_reason(session) == "held — no cloud backend reachable"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_all_lanes_at_capacity(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE 4: every AVAILABLE lane is full -> "held — all lanes at capacity (N/N slots busy)"."""
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=True))
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [ComputeAgentBackend(id="a1", rank=10, cap=2)])
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={"a1": True}))
    file_a, file_b = _file(0), _file(1)
    session.add_all([file_a, file_b])
    await session.flush()
    session.add_all(
        [
            _cloud_job_backend(file_a.id, backend_id="a1", status=CloudJobStatus.SUBMITTED.value),
            _cloud_job_backend(file_b.id, backend_id="a1", status=CloudJobStatus.RUNNING.value),
        ]
    )
    await session.commit()

    assert await derive_cloud_hold_reason(session) == "held — all lanes at capacity (2/2 slots busy)"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_ignores_always_available_local_lane_when_cloud_full(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-g4fh: a local lane (always available, in_flight=0) must NOT count as free cloud capacity.

    Registry = local (cap 1, always available/idle) + one cloud lane (cap 4, fully busy). Before the
    fix, the local lane's free slot made ``available_lanes``/``free_slots`` never reflect the cloud
    lane being full, so this fell through to a false "queued -- 1 free slots" caption. The drain
    itself gates local behind ``cloud_spill_to_local_after_seconds`` staleness -- it is NOT capacity
    the drain would dispatch to next tick -- so the truthful caption is "all lanes at capacity".
    """
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=True))
    monkeypatch.setattr(
        backends_mod,
        "resolve_backends",
        lambda _settings: [LocalBackend(id="local", rank=0, cap=1), ComputeAgentBackend(id="a1", rank=10, cap=4)],
    )
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={"local": True, "a1": True}))
    files = [_file(i) for i in range(4)]
    session.add_all(files)
    await session.flush()
    session.add_all([_cloud_job_backend(f.id, backend_id="a1", status=CloudJobStatus.RUNNING.value) for f in files])
    await session.commit()

    assert await derive_cloud_hold_reason(session) == "held — all lanes at capacity (4/4 slots busy)"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_no_cloud_backend_reachable_even_with_local_lane(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-g4fh: every CLOUD lane offline -> "held -- no cloud backend reachable", local notwithstanding.

    Before the fix this branch was dead code: the always-available local lane made
    ``available_lanes`` never empty, so this exact "cloud is entirely down" state fell through to a
    false "queued" caption instead of the truthful reachability hold.
    """
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=True))
    monkeypatch.setattr(
        backends_mod,
        "resolve_backends",
        lambda _settings: [LocalBackend(id="local", rank=0, cap=1), ComputeAgentBackend(id="a1", rank=10, cap=4)],
    )
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={"local": True, "a1": False}))

    assert await derive_cloud_hold_reason(session) == "held — no cloud backend reachable"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_no_fileserver_agent(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE 5: free capacity exists but no fileserver agent is online to initiate the push."""
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=True))
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [ComputeAgentBackend(id="a1", rank=10, cap=2)])
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={"a1": True}))
    file_a = _file(0)
    session.add(file_a)
    await session.flush()
    session.add(_cloud_job_backend(file_a.id, backend_id="a1", status=CloudJobStatus.SUBMITTED.value))
    await session.commit()

    assert await derive_cloud_hold_reason(session) == "held — no fileserver agent online"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_queued_with_free_slots(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Else branch: free capacity AND a live fileserver agent -> genuinely queued, no hold."""
    monkeypatch.setattr(backends_mod, "get_settings", lambda: _cloud_hold_settings(cloud_enabled=True))
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [ComputeAgentBackend(id="a1", rank=10, cap=2)])
    monkeypatch.setattr(backends_mod, "_probe_availability", AsyncMock(return_value={"a1": True}))
    file_a = _file(0)
    session.add(file_a)
    await session.flush()
    session.add(_cloud_job_backend(file_a.id, backend_id="a1", status=CloudJobStatus.SUBMITTED.value))
    await session.commit()
    await seed_active_agent(session, kind="fileserver")

    assert await derive_cloud_hold_reason(session) == "queued — 1 free slots, dispatching on next drain tick (~5 min)"


@pytest.mark.asyncio
async def test_derive_cloud_hold_reason_degrades_to_neutral_on_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """ANY unexpected exception collapses to the neutral "held" copy -- no causal claim, never a 500."""

    def _boom() -> None:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(backends_mod, "get_settings", _boom)

    assert await derive_cloud_hold_reason(session) == "held"
