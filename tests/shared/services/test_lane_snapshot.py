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
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.config_backends import ComputeBackend as ComputeEntry
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
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
from tests._queue_fakes import seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# --------------------------------------------------------------------------- seeds


def _file(i: int) -> FileRecord:
    """Build a minimal FileRecord seed (CloudJob.file_id is a unique FK to files.id)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"d{i:063d}"[:64],
        original_path=f"/music/lane{i}.mp3",
        original_filename=f"lane{i}.mp3",
        current_path=f"/music/lane{i}.mp3",
        file_type="mp3",
        file_size=1000,
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


class _DoubleExplodingSession:
    """An AsyncSession stand-in whose ``execute`` AND ``rollback`` both raise (guarded-rollback path)."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002 -- AsyncSession signature stand-in
        raise RuntimeError("execute boom")

    async def rollback(self) -> None:
        raise RuntimeError("rollback boom")


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


@pytest.mark.asyncio
async def test_admission_degrades_when_rollback_also_fails() -> None:
    """A failed guarded rollback is swallowed too -> still {} (the poll never sees the error)."""
    assert await _admission_by_backend_id(_DoubleExplodingSession()) == {}  # type: ignore[arg-type]


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


class _RollbackTrackingPoisonSession:
    """Tracks ``rollback`` calls; ``execute`` (via ``is_available``) is what a probe would poison."""

    def __init__(self) -> None:
        self.rollback_count = 0

    async def rollback(self) -> None:
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_probe_one_rolls_back_the_session_on_a_failed_probe() -> None:
    """phaze-ntr8s: ``_probe_one`` clears the poison it may have caused BEFORE returning.

    A compute probe's cancelled/failed DB read can leave the shared session unusable for the very next
    statement. The failing branch must roll back immediately -- not rely on some later, one-time
    cleanup -- so a SEQUENCE of probes on the same session (``_probe_availability``) never inherits a
    poisoned session from an earlier lane in the SAME sweep.
    """
    session = _RollbackTrackingPoisonSession()
    result = await _probe_one(session, _RaisingBackend("k8s-bad"))  # type: ignore[arg-type]

    assert result == ("k8s-bad", False)
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_probe_one_swallows_a_rollback_failure_after_a_failed_probe() -> None:
    """A rollback failure in the cleanup branch must never mask the original probe failure (degrade-safe)."""

    class _RollbackAlsoFailsSession:
        async def rollback(self) -> None:
            raise RuntimeError("rollback boom")

    result = await _probe_one(_RollbackAlsoFailsSession(), _RaisingBackend("k8s-bad"))  # type: ignore[arg-type]
    assert result == ("k8s-bad", False)  # the probe failure still degrades cleanly, no raise


class _CascadeVictimLane:
    """A backend whose ``is_available`` fails IFF the shared session is (still) poisoned.

    Models a sibling compute probe's own DB read failing on an already-aborted session -- the exact
    cascade phaze-ntr8s describes: backend A's cancelled probe poisons the session, and backend B's
    UNRELATED, otherwise-healthy probe immediately fails too because nothing rolled back in between.
    """

    def __init__(self, backend_id: str, rank: int) -> None:
        self.id = backend_id
        self.rank = rank
        self.cap = 1

    async def is_available(self, session: _PoisonRecoverSession) -> bool:
        if session.poisoned:
            raise RuntimeError("query failed: current transaction is aborted")
        return True


@pytest.mark.asyncio
async def test_probe_availability_isolates_a_poisoned_probe_from_the_next_sibling_lane() -> None:
    """phaze-ntr8s ACCEPTANCE: a poisoned probe must NOT cascade to the NEXT lane in the same sweep.

    ``_probe_availability`` runs every backend's probe SEQUENTIALLY on ONE shared session (Pitfall 1).
    Pre-fix, ``_probe_one`` never rolled back on its own failure -- only a rollback AFTER the whole
    fan-out cleared the poison, which is too late for a sibling probed WITHIN this same sweep. Ordering
    the poisoning lane FIRST and a lane that only fails while the session is poisoned SECOND reproduces
    exactly that: without the per-probe rollback, the second (otherwise healthy) lane would also read
    False, reporting a false 'offline' for a lane that never actually failed.
    """
    session = _PoisonRecoverSession()
    poison = _DbPoisoningLane("compute-a", 10)
    victim = _CascadeVictimLane("compute-b", 20)

    result = await _probe_availability(session, [poison, victim])  # type: ignore[arg-type]

    assert result == {"compute-a": False, "compute-b": True}  # the sibling lane is UNAFFECTED
    assert session.rollbacks >= 1  # the per-probe rollback ran between the two probes


# --------------------------------------------------------------------------- Task 2: kind


def test_kind_of_dispatch() -> None:
    """_kind_of derives local/compute/kueue via isinstance (mirrors resolve_backends)."""
    assert _kind_of(LocalBackend(id="local", rank=99, cap=1)) == "local"
    assert _kind_of(ComputeAgentBackend(id="a1", rank=10, cap=2)) == "compute"
    assert _kind_of(KueueBackend(id="k8s", rank=20, cap=2)) == "kueue"


def test_kind_of_unknown_fallback() -> None:
    """A structurally-conforming backend of no known impl class falls back to "unknown"."""
    assert _kind_of(_FastBackend("mystery")) == "unknown"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Task 2: snapshot


@pytest.mark.asyncio
async def test_snapshot_shape_and_rank_order(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 3-backend registry returns 3 rank-ascending secret-free lane dicts with live counts."""
    from phaze.services.pipeline import cloud as pipeline_mod

    local = LocalBackend(id="local", rank=99, cap=1)
    compute = ComputeAgentBackend(id="a1", rank=10, cap=2)
    kueue = KueueBackend(id="k8s", rank=20, cap=3)
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [local, compute, kueue])
    # phaze-5c6i2: _cloud_lane_queued_working resolves kueue-ness via `_cloud_window_clauses`, which
    # reads pipeline.get_settings() -- a SEPARATE bound name from the resolve_backends mock above, so
    # it needs its own deterministic registry (a1=compute, k8s=kueue) for the new queued/working split.
    monkeypatch.setattr(
        pipeline_mod,
        "get_settings",
        lambda: SimpleNamespace(backends=[SimpleNamespace(id="a1", kind="compute"), SimpleNamespace(id="k8s", kind="kueue")]),
    )

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

    # app_state=None (default) -- the local lane's SAQ queued/working are unavailable without a real
    # task_router, so they degrade to None (explicit unknown), exactly like every pre-phaze-5c6i2 caller.
    lanes = await get_backend_lane_snapshot(session)

    assert [lane["id"] for lane in lanes] == ["a1", "k8s", "local"]  # rank-ascending
    expected_keys = {
        "id",
        "kind",
        "rank",
        "cap",
        "in_flight",
        "available",
        "quota_wait",
        "inadmissible",
        "queued",
        "working",
        "processed_24h",
        "processed_lifetime",
    }
    for lane in lanes:
        assert set(lane) == expected_keys  # secret-free: no config / SecretStr / token key

    by_id = {lane["id"]: lane for lane in lanes}
    # phaze-5c6i2: a1 is compute-kind under the mocked registry above, so its SUBMITTED row is
    # pre-execution ("staged") -> queued=1, working=0. No AnalysisResult rows exist -> processed all 0.
    assert by_id["a1"] == {
        "id": "a1",
        "kind": "compute",
        "rank": 10,
        "cap": 2,
        "in_flight": 1,
        "available": True,
        "quota_wait": 0,
        "inadmissible": 0,
        "queued": 1,
        "working": 0,
        "processed_24h": 0,
        "processed_lifetime": 0,
    }
    # k8s is kueue-kind, so its SUBMITTED row is post-submit ("analyzing") -> working=1, queued=0.
    assert by_id["k8s"] == {
        "id": "k8s",
        "kind": "kueue",
        "rank": 20,
        "cap": 3,
        "in_flight": 1,
        "available": False,
        "quota_wait": 1,
        "inadmissible": 0,
        "queued": 0,
        "working": 1,
        "processed_24h": 0,
        "processed_lifetime": 0,
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
        "queued": None,
        "working": None,
        "processed_24h": 0,
        "processed_lifetime": 0,
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


class _PoisonRecoverSession:
    """A session stand-in modelling a compute-probe DB error: ``execute`` is aborted while poisoned; ``rollback`` clears it.

    Deterministic and DB-free (real asyncpg poison-then-recover depends on prepared-statement/pool state and is
    non-hermetic). ``poisoned`` flips True when the bad probe runs; a lane's ``in_flight_count`` raises while poisoned.
    """

    def __init__(self) -> None:
        self.poisoned = False
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.poisoned = False
        self.rollbacks += 1


class _HealthyLane:
    """A non-local backend that probes online and whose ``in_flight_count`` reads cleanly (unless the session is poisoned)."""

    def __init__(self, backend_id: str, rank: int) -> None:
        self.id = backend_id
        self.rank = rank
        self.cap = 1

    async def is_available(self, session: Any) -> bool:  # noqa: ARG002 -- probe surrogate
        return True

    async def in_flight_count(self, session: _PoisonRecoverSession) -> int:
        if session.poisoned:
            raise RuntimeError("in_flight_count read on an aborted transaction")
        return 0


class _DbPoisoningLane:
    """A compute backend whose ``is_available`` fails at the DB layer, poisoning the shared session (the WR-01 trigger)."""

    def __init__(self, backend_id: str, rank: int) -> None:
        self.id = backend_id
        self.rank = rank
        self.cap = 2

    async def is_available(self, session: _PoisonRecoverSession) -> bool:
        session.poisoned = True
        raise RuntimeError("compute is_available probe failed at the DB layer")

    async def in_flight_count(self, session: _PoisonRecoverSession) -> int:
        if session.poisoned:
            raise RuntimeError("in_flight_count read on an aborted transaction")
        return 0


@pytest.mark.asyncio
async def test_snapshot_db_poisoning_probe_isolated_not_grid_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compute probe that fails at the DB layer degrades ONLY its lane; the grid still renders (T-71-02, WR-01).

    The probe poisons the shared session; the post-fan-out ``rollback`` clears it so the subsequent
    ``in_flight_count`` reads succeed. Without that rollback the aborted transaction makes the next
    read raise and the WHOLE snapshot collapses to ``[]`` (the entire grid vanishes for one bad lane).
    """
    healthy = _HealthyLane("local", 99)
    poison = _DbPoisoningLane("a1", 10)
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [healthy, poison])

    async def _no_admission(_session: Any) -> dict[str, dict[str, int]]:
        return {}

    monkeypatch.setattr(backends_mod, "_admission_by_backend_id", _no_admission)

    sess = _PoisonRecoverSession()
    lanes = await get_backend_lane_snapshot(sess)  # type: ignore[arg-type]

    assert [lane["id"] for lane in lanes] == ["a1", "local"]  # rank-ascending; grid did NOT collapse to []
    assert {lane["id"]: lane["available"] for lane in lanes} == {"a1": False, "local": True}  # bad lane offline, healthy lane online
    assert sess.rollbacks >= 1  # the post-probe rollback ran


@pytest.mark.asyncio
async def test_snapshot_degrades_to_empty_on_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any top-level error -> [] (the snapshot never raises into the /pipeline/stats poll, T-71-03)."""

    def _boom(_settings: Any) -> Any:
        raise RuntimeError("registry resolution failed")

    monkeypatch.setattr(backends_mod, "resolve_backends", _boom)
    assert await get_backend_lane_snapshot(session) == []


@pytest.mark.asyncio
async def test_snapshot_degrades_when_rollback_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed guarded rollback after a top-level error is swallowed too -> still [] (T-71-03)."""

    def _boom(_settings: Any) -> Any:
        raise RuntimeError("registry resolution failed")

    monkeypatch.setattr(backends_mod, "resolve_backends", _boom)
    assert await get_backend_lane_snapshot(_DoubleExplodingSession()) == []  # type: ignore[arg-type]


# ------------------------------------------- Plan 74-03: N-lane compute parity (MCOMP-07, D-04)


@pytest.mark.asyncio
async def test_snapshot_renders_one_lane_per_compute_backend(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Variant A (deterministic): N≥2 compute backends each render their own lane -- no kind dedup (MCOMP-07, D-04, R-2).

    A registry of two ``ComputeAgentBackend`` (``a1-arm64`` rank 10, ``x86-spill`` rank 20) plus one
    ``LocalBackend`` (rank 99), with ``_probe_availability`` monkeypatched to an all-available shim, must
    yield EXACTLY two ``kind=="compute"`` lanes -- ids ``["a1-arm64", "x86-spill"]`` in rank order -- proving
    the BEUI-01 grid renders each compute agent as its own lane and never collapses on ``kind``. This is a
    verification test over the existing ``get_backend_lane_snapshot``; the deterministic probe keeps it green
    independent of the real-fan-out race question (Variant B / Plan 04).
    """
    arm64 = ComputeAgentBackend(id="a1-arm64", rank=10, cap=2)
    x86 = ComputeAgentBackend(id="x86-spill", rank=20, cap=1)
    local = LocalBackend(id="local", rank=99, cap=1)
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [arm64, x86, local])

    async def _all_available(_session: Any, _backends: Any) -> dict[str, bool]:
        return {"a1-arm64": True, "x86-spill": True, "local": True}

    monkeypatch.setattr(backends_mod, "_probe_availability", _all_available)

    lanes = await get_backend_lane_snapshot(session)

    compute_lanes = [lane for lane in lanes if lane["kind"] == "compute"]
    assert [lane["id"] for lane in compute_lanes] == ["a1-arm64", "x86-spill"]  # rank order, no kind collapse
    assert len(compute_lanes) == 2  # each compute backend gets its OWN lane (no dedup on kind)

    by_id = {lane["id"]: lane for lane in lanes}
    assert by_id["a1-arm64"]["rank"] == 10
    assert by_id["x86-spill"]["rank"] == 20
    assert by_id["local"]["kind"] == "local"  # the local lane is still present alongside the two compute lanes
    assert by_id["local"]["rank"] == 99
    assert [lane["id"] for lane in lanes] == ["a1-arm64", "x86-spill", "local"]  # sorted (rank, id)


def _bound_compute(bid: str, rank: int, cap: int) -> ComputeAgentBackend:
    """Build a ComputeAgentBackend carrying a REAL bound ComputeBackend config (agent_ref == bid).

    Mirrors the tests/analyze/services/backends/test_backends.py ``_compute`` factory so ``is_available`` resolves
    ``self.config.agent_ref`` against a seeded ``Agent.id`` via ``select_agent_by_id`` -- the real probe path
    Variant B must exercise (no ``config`` would fail loud in ``_agent_ref``).
    """
    config = ComputeEntry(
        kind="compute",
        id=bid,
        rank=rank,
        cap=cap,
        agent_ref=bid,
        scratch_dir="/srv/scratch",
        push_host=f"{bid}.push.example",
        ssh_user=None,
    )
    return ComputeAgentBackend(id=bid, rank=rank, cap=cap, config=config)


@pytest.mark.asyncio
async def test_compute_probe_real_fanout_keeps_both_lanes_online(session: AsyncSession) -> None:
    """HARD-01 (structural): serial ``_probe_availability`` over N≥2 ONLINE compute agents on ONE session (D-01, D-04).

    Two ``ComputeAgentBackend`` each bound to a real ``ComputeBackend`` config whose ``agent_ref`` matches a
    seeded ONLINE ``kind="compute"`` agent, probed by the REAL ``_probe_availability`` (no monkeypatch). Each
    ``ComputeAgentBackend.is_available`` runs ``select_agent_by_id`` -> ``session.execute`` on the ONE shared
    ``AsyncSession``. Because ``_probe_availability`` now awaits each ``_probe_one`` SEQUENTIALLY, those
    ``session.execute`` calls can never overlap -- the shared-session hazard (Pitfall 1 / WR-01) is closed by
    construction, not by the empirical Plan-74-03 arbiter. A healthy N≥2-compute deployment shows BOTH lanes
    online, so the probe yields a DETERMINISTIC ``{backend_id: available}`` dict with both ``True`` (asserted
    ONCE -- no repeated-runs arbiter needed now that execution is structurally serial).
    """
    arm64 = _bound_compute("a1-arm64", rank=10, cap=2)
    x86 = _bound_compute("x86-spill", rank=20, cap=1)

    # Seed TWO ONLINE compute agents whose ids MATCH the two backends' agent_ref (record-don't-rederive).
    await seed_active_agent(session, "a1-arm64", kind="compute")
    await seed_active_agent(session, "x86-spill", kind="compute")

    # Real serial fan-out over the shared session -- deterministic, no concurrent session.execute.
    availability = await _probe_availability(session, [arm64, x86])

    assert availability == {"a1-arm64": True, "x86-spill": True}  # both online -> both lanes available (deterministic)
