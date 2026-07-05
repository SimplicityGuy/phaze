"""Tests for the BEUI-01 read-only backend-lane snapshot service (Phase 71, Plan 01).

Covers ``services.backends.get_backend_lane_snapshot`` + its helpers
``_admission_by_backend_id`` / ``_probe_availability`` / ``_probe_one`` / ``_kind_of``:

* per-``backend_id`` admission attribution via ``GROUP BY`` (D-03) — two Kueue lanes with
  distinct ``backend_id`` own distinct quota-wait / Inadmissible counts;
* bounded concurrent availability probes (D-02) — one hung backend times out to offline
  without stalling the shared read; the ``LocalBackend`` probe is short-circuited (no I/O);
* the composed rank-ascending, secret-free snapshot (D-06) degrading to ``[]`` on any error
  so it never raises into the hot 5s ``/pipeline/stats`` poll (T-71-03).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord, FileState
from phaze.services import backends as backends_mod
from phaze.services.backends import (
    ComputeAgentBackend,
    KueueBackend,
    LocalBackend,
    _admission_by_backend_id,
    _kind_of,
    _probe_availability,
    _probe_one,
    get_backend_lane_snapshot,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------- seeds


def _file(i: int) -> FileRecord:
    """Build a minimal FileRecord seed (CloudJob.file_id is a unique FK to files.id)."""
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash=f"d{i:063d}"[:64],
        original_path=f"/music/lane{i}.mp3",
        original_filename=f"lane{i}.mp3",
        current_path=f"/music/lane{i}.mp3",
        file_type="mp3",
        file_size=1000,
        state=FileState.PUSHED,
    )


def _cloud_job(
    file_id: uuid.UUID,
    *,
    backend_id: str | None,
    status: str = CloudJobStatus.SUBMITTED.value,
    cloud_phase: str | None = None,
    inadmissible: bool = False,
) -> CloudJob:
    """Build a CloudJob seed for the given file/backend with the given admission attributes."""
    return CloudJob(
        id=uuid.uuid4(),
        file_id=file_id,
        s3_key=f"phaze-staging/{file_id}",
        status=status,
        backend_id=backend_id,
        cloud_phase=cloud_phase,
        inadmissible=inadmissible,
    )


class _ExplodingSession:
    """An AsyncSession stand-in whose ``execute`` always raises (degrade-path probe)."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002 -- AsyncSession signature stand-in
        raise RuntimeError("boom")

    async def rollback(self) -> None:
        return None


class _SlowBackend:
    """Non-local backend whose ``is_available`` hangs past the probe timeout (DoS surrogate)."""

    def __init__(self, backend_id: str) -> None:
        self.id = backend_id
        self.rank = 10
        self.cap = 2

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- probe surrogate
        await asyncio.sleep(5.0)
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- probe surrogate
        return 0


class _FastBackend:
    """Non-local backend that is immediately available (probe stays online)."""

    def __init__(self, backend_id: str, *, available: bool = True) -> None:
        self.id = backend_id
        self.rank = 20
        self.cap = 2
        self._available = available

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- probe surrogate
        return self._available

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- probe surrogate
        return 0


class _RaisingBackend:
    """Non-local backend whose ``is_available`` raises -> probe degrades it to offline."""

    def __init__(self, backend_id: str) -> None:
        self.id = backend_id
        self.rank = 30
        self.cap = 2

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- probe surrogate
        raise ValueError("probe blew up")

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- probe surrogate
        return 0


class _ExplodingLocal(LocalBackend):
    """A LocalBackend whose ``is_available`` MUST NOT be awaited (proves short-circuit)."""

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- must never run
        raise AssertionError("LocalBackend probe must be short-circuited (no I/O)")


# --------------------------------------------------------------------------- Task 1: admission


@pytest.mark.asyncio
async def test_admission_per_backend_group_by(session: AsyncSession) -> None:
    """Two distinct backend_ids own DISTINCT quota_wait / inadmissible counts (D-03 GROUP BY).

    quota_wait counts cloud_phase == QUEUED_BEHIND_QUOTA; inadmissible counts inadmissible rows
    scoped to in-flight status {SUBMITTED, RUNNING}. NULL-backend_id rows contribute to NEITHER
    lane (they have no owning backend), and a terminal inadmissible row is excluded.
    """
    files = [_file(i) for i in range(7)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            # backend "k8s-a": 2 quota_wait + 1 inadmissible (in-flight RUNNING)
            _cloud_job(files[0].id, backend_id="k8s-a", cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value),
            _cloud_job(files[1].id, backend_id="k8s-a", cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value),
            _cloud_job(files[2].id, backend_id="k8s-a", status=CloudJobStatus.RUNNING.value, inadmissible=True),
            # backend "k8s-b": 1 quota_wait + 1 inadmissible; a terminal inadmissible row is excluded
            _cloud_job(files[3].id, backend_id="k8s-b", cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value),
            _cloud_job(files[4].id, backend_id="k8s-b", status=CloudJobStatus.SUBMITTED.value, inadmissible=True),
            _cloud_job(files[5].id, backend_id="k8s-b", status=CloudJobStatus.SUCCEEDED.value, inadmissible=True),
            # NULL backend_id (legacy / unattributed) row -> owned by no lane
            _cloud_job(files[6].id, backend_id=None, cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value),
        ]
    )
    await session.commit()

    admission = await _admission_by_backend_id(session)

    assert admission["k8s-a"] == {"quota_wait": 2, "inadmissible": 1}
    assert admission["k8s-b"] == {"quota_wait": 1, "inadmissible": 1}
    assert None not in admission  # NULL-backend_id rows are never attributed to a lane


@pytest.mark.asyncio
async def test_admission_empty_when_no_rows(session: AsyncSession) -> None:
    """No cloud_job rows -> an empty attribution map (every lane falls back to zero)."""
    assert await _admission_by_backend_id(session) == {}


@pytest.mark.asyncio
async def test_admission_degrades_to_empty_on_db_error() -> None:
    """A DB error degrades the attribution to {} (never raises into the poll)."""
    assert await _admission_by_backend_id(_ExplodingSession()) == {}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Task 1: probes


@pytest.mark.asyncio
async def test_probe_timeout_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """One hung backend times out to offline; a healthy lane stays online; the read stays bounded.

    The whole fan-out is bounded to ~one timeout even though the slow probe would sleep 5s, proving
    a hung Kueue cluster can never stall the shared 5s poll (T-71-02).
    """
    monkeypatch.setattr(backends_mod, "_PROBE_TIMEOUT_SEC", 0.05)
    slow = _SlowBackend("k8s-slow")
    fast = _FastBackend("k8s-fast")

    start = time.monotonic()
    result = await _probe_availability(None, [slow, fast])  # type: ignore[arg-type]
    elapsed = time.monotonic() - start

    assert result == {"k8s-slow": False, "k8s-fast": True}
    assert elapsed < 1.0  # bounded to ~one 0.05s timeout, not the 5s sleep


@pytest.mark.asyncio
async def test_probe_local_short_circuit_no_io() -> None:
    """A LocalBackend probe returns True WITHOUT awaiting is_available (no I/O, D-02)."""
    local = _ExplodingLocal(id="local", rank=99, cap=1)
    assert await _probe_one(None, local) == ("local", True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_probe_failure_degrades_to_offline() -> None:
    """A probe that raises degrades that ONE lane to offline (never propagates)."""
    assert await _probe_one(None, _RaisingBackend("k8s-bad")) == ("k8s-bad", False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_probe_reports_backend_availability() -> None:
    """A healthy probe returns the backend's live is_available result verbatim."""
    assert await _probe_one(None, _FastBackend("k8s", available=False)) == ("k8s", False)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Task 2: kind


def test_kind_of_dispatch() -> None:
    """_kind_of derives local/compute/kueue via isinstance (mirrors resolve_backends)."""
    assert _kind_of(LocalBackend(id="local", rank=99, cap=1)) == "local"
    assert _kind_of(ComputeAgentBackend(id="a1", rank=10, cap=2)) == "compute"
    assert _kind_of(KueueBackend(id="k8s", rank=20, cap=2)) == "kueue"


# --------------------------------------------------------------------------- Task 2: snapshot


@pytest.mark.asyncio
async def test_snapshot_shape_and_rank_order(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 3-backend registry returns 3 rank-ascending secret-free lane dicts with live counts."""
    local = LocalBackend(id="local", rank=99, cap=1)
    compute = ComputeAgentBackend(id="a1", rank=10, cap=2)
    kueue = KueueBackend(id="k8s", rank=20, cap=3)
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [local, compute, kueue])

    async def _fake_probe(_session: Any, _backends: Any) -> dict[str, bool]:
        return {"local": True, "a1": True, "k8s": False}

    monkeypatch.setattr(backends_mod, "_probe_availability", _fake_probe)

    files = [_file(i) for i in range(2)]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            # a1: one in-flight SUBMITTED row -> in_flight 1
            _cloud_job(files[0].id, backend_id="a1", status=CloudJobStatus.SUBMITTED.value),
            # k8s: a quota-waiting SUBMITTED row -> in_flight 1 AND quota_wait 1
            _cloud_job(files[1].id, backend_id="k8s", status=CloudJobStatus.SUBMITTED.value, cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value),
        ]
    )
    await session.commit()

    lanes = await get_backend_lane_snapshot(session)

    assert [lane["id"] for lane in lanes] == ["a1", "k8s", "local"]  # rank-ascending
    expected_keys = {"id", "kind", "rank", "cap", "in_flight", "available", "quota_wait", "inadmissible"}
    for lane in lanes:
        assert set(lane) == expected_keys  # secret-free: no config / SecretStr / token key

    by_id = {lane["id"]: lane for lane in lanes}
    assert by_id["a1"] == {"id": "a1", "kind": "compute", "rank": 10, "cap": 2, "in_flight": 1, "available": True, "quota_wait": 0, "inadmissible": 0}
    assert by_id["k8s"] == {
        "id": "k8s",
        "kind": "kueue",
        "rank": 20,
        "cap": 3,
        "in_flight": 1,
        "available": False,
        "quota_wait": 1,
        "inadmissible": 0,
    }
    assert by_id["local"] == {
        "id": "local",
        "kind": "local",
        "rank": 99,
        "cap": 1,
        "in_flight": 0,
        "available": True,
        "quota_wait": 0,
        "inadmissible": 0,
    }


@pytest.mark.asyncio
async def test_snapshot_tie_break_by_id(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal-rank lanes are tie-broken by id (D-06 deterministic order)."""
    kueue_b = KueueBackend(id="k8s-b", rank=10, cap=2)
    kueue_a = KueueBackend(id="k8s-a", rank=10, cap=2)
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [kueue_b, kueue_a])

    async def _fake_probe(_session: Any, _backends: Any) -> dict[str, bool]:
        return {"k8s-a": True, "k8s-b": True}

    monkeypatch.setattr(backends_mod, "_probe_availability", _fake_probe)

    lanes = await get_backend_lane_snapshot(session)
    assert [lane["id"] for lane in lanes] == ["k8s-a", "k8s-b"]


@pytest.mark.asyncio
async def test_snapshot_degrades_to_empty_on_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any top-level error -> [] (the snapshot never raises into the /pipeline/stats poll, T-71-03)."""

    def _boom(_settings: Any) -> Any:
        raise RuntimeError("registry resolution failed")

    monkeypatch.setattr(backends_mod, "resolve_backends", _boom)
    assert await get_backend_lane_snapshot(session) == []
