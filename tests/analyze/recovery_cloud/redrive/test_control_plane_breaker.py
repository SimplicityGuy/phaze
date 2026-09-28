"""An unreachable control plane charges no budget and holds the backend (phaze-j0ixx).

Spike ``phaze-79mu7``: on 2026-09-26/27 every burst pod failed its presign request -- 4 files while the
app host was down, 298 while the burst node's cluster DNS had no upstream -- and each exit was charged
against the file's ``attempts`` as ``EXIT_DOWNLOAD`` (10), so 302 healthy files spent their whole cloud
budget. The pod now exits ``EXIT_CONTROL_PLANE_UNREACHABLE`` (14) for that case; reconcile charges
nothing for it and feeds the per-backend breaker, which holds the backend in the drain.

Rule 3: the pods reach reconcile through the REAL ``list_pods_for_job`` -> kr8s ``Pod`` path. Only the
kube HTTP transport is stubbed (respx serves each Job's PodList as the API server would, from
``_terminated_pod_manifest`` -- the kubelet's field set for a Failed ``restartPolicy: Never`` pod).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select, update

from phaze.models.backend_breaker import BackendBreaker
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services import backend_breaker
from phaze.tasks import release_awaiting_cloud
from phaze.tasks.reconcile_cloud_jobs import BUDGET_UNCHARGED, reconcile_cloud_jobs
from tests.analyze.recovery_cloud.redrive.test_reconcile import (
    _KUEUE_BACKEND_ID,
    _PODS_PATH,
    S3DeleteSpy,
    _make_ctx,
    _patch_real_kube,
    _read_cloud_job,
    _seed,
    _terminated_pod_manifest,
)
from tests.kube_fakes import fake_job, fake_local_queue


if TYPE_CHECKING:
    from httpx import Request, Response
    from sqlalchemy.ext.asyncio import AsyncSession


_UNREACHABLE = 14


class _Cluster:
    """``get_job`` / ``delete_job`` stand-ins keyed by Job name, with each Job's pod served over the kube wire.

    A name in ``exited`` is a Failed Job whose pod terminated with that exit code; a name in ``running``
    is a live Job (its Workload is unreadable, so reconcile holds it untouched); anything else 404s. A
    delete forgets the Job, which is what the re-drive's confirm-gone read then sees.
    """

    def __init__(self) -> None:
        self.exited: dict[str, int] = {}
        self.running: set[str] = set()
        self.deleted: list[str] = []

    async def get_job(self, name: str, kube: Any = None) -> Any:  # noqa: ARG002 -- MKUE-01 seam signature
        if name in self.exited:
            return fake_job(failed=1, name=name)
        if name in self.running:
            return fake_job(active=1, name=name)
        return None

    async def delete_job(self, name: str, kube: Any = None) -> None:  # noqa: ARG002 -- MKUE-01 seam signature
        self.deleted.append(name)
        self.exited.pop(name, None)
        self.running.discard(name)

    def pods(self, request: Request) -> Response:
        """Serve the PodList the API server would return for the request's job-name label selector."""
        from httpx import Response

        selector = request.url.params.get("labelSelector", "")
        name = selector.split("=", 1)[1] if "=" in selector else ""
        items = [_terminated_pod_manifest(name, self.exited[name])] if name in self.exited else []
        return Response(200, json={"apiVersion": "v1", "kind": "PodList", "metadata": {}, "items": items})


def _install(monkeypatch: pytest.MonkeyPatch, kube_respx: Any) -> tuple[_Cluster, S3DeleteSpy]:
    """Wire the cluster fake into the kube seam, the real kr8s pod path, and an S3 delete spy."""
    _patch_real_kube(monkeypatch)
    cluster = _Cluster()
    kube_respx.get(_PODS_PATH).mock(side_effect=cluster.pods)
    s3 = S3DeleteSpy([])
    monkeypatch.setattr("phaze.services.kube_staging.get_job", cluster.get_job)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", cluster.delete_job)
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", AsyncMock(return_value=None))
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)
    return cluster, s3


async def _resubmit(session: AsyncSession, cluster: _Cluster, file_id: uuid.UUID, name: str, *, exit_code: int | None) -> None:
    """What the enqueued ``submit_cloud_job`` does for a re-driven row: re-stamp the Job name and create the Job.

    ``exit_code`` None leaves the fresh Job running; otherwise its pod has already exited with it.
    """
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(kueue_workload=name))
    await session.commit()
    if exit_code is None:
        cluster.running.add(name)
    else:
        cluster.exited[name] = exit_code


async def _breaker(session: AsyncSession) -> BackendBreaker | None:
    session.expire_all()
    return (await session.execute(select(BackendBreaker).where(BackendBreaker.backend_id == _KUEUE_BACKEND_ID))).scalar_one_or_none()


def test_the_pod_and_the_breaker_agree_on_the_exit_code() -> None:
    """The breaker restates the pod's constant to avoid importing the pod entrypoint; this pins them together."""
    import phaze.job_runner as jr

    assert jr.EXIT_CONTROL_PLANE_UNREACHABLE == backend_breaker.UNREACHABLE_EXIT_CODE == _UNREACHABLE
    others = {jr.EXIT_OK, jr.EXIT_DOWNLOAD, jr.EXIT_INTEGRITY, jr.EXIT_ANALYSIS, jr.EXIT_CALLBACK, jr.EXIT_CONFIG}
    assert _UNREACHABLE not in others


@pytest.mark.asyncio
async def test_incident_four_files_unreachable_across_ticks_hold_the_backend_and_charge_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any
) -> None:
    """Acceptance (d): the 09-26 shape. Four in-flight files exit 14 across several ticks; none reaches cap; the backend is held.

    Seeded one attempt short of the cap on two rows, so under the old accounting (exit 10, charged) the
    first failure alone would have pushed them to the cap and the next spilled them to local.
    """
    import structlog

    cluster, s3 = _install(monkeypatch, kube_respx)
    seeded = [await _seed(session, attempts=attempts) for attempts in (0, 1, 2, 2)]
    (fid_a, name_a), (fid_b, name_b), (_fid_c, name_c), (_fid_d, name_d) = seeded
    cluster.running.update({name_c, name_d})

    # Tick 1: A and B exit 14. Two distinct files is under the trip rule -> each re-driven, uncharged.
    cluster.exited.update({name_a: _UNREACHABLE, name_b: _UNREACHABLE})
    cluster.running -= {name_a, name_b}
    with structlog.testing.capture_logs() as logs:
        tally = await reconcile_cloud_jobs(_make_ctx())
    assert tally["redriven"] == 2
    assert tally["unreachable_held"] == 0
    closed = await _breaker(session)
    assert closed is not None and closed.tripped_at is None  # the row exists (fed twice), still closed
    redrives = [e for e in logs if str(e.get("event", "")).startswith("reconcile_cloud_jobs: re-driving submit_cloud_job")]
    assert {e["budget"] for e in redrives} == {BUDGET_UNCHARGED}
    assert {e["exit_code"] for e in redrives} == {_UNREACHABLE}

    # submit_cloud_job runs for A and B; their fresh pods hit the same fault. C exits 14 in the same tick.
    await _resubmit(session, cluster, fid_a, name_a, exit_code=_UNREACHABLE)
    await _resubmit(session, cluster, fid_b, name_b, exit_code=_UNREACHABLE)
    cluster.running.discard(name_c)
    cluster.exited[name_c] = _UNREACHABLE

    # Tick 2: the breaker trips on whichever of the three it reconciles first; all three spill uncharged.
    with structlog.testing.capture_logs() as logs:
        tally = await reconcile_cloud_jobs(_make_ctx())
    assert tally["unreachable_held"] == 3
    breaker = await _breaker(session)
    assert breaker is not None and breaker.tripped_at is not None
    assert breaker.next_probe_at is not None
    assert any(str(e.get("event", "")).startswith("backend breaker OPEN") for e in logs)

    # Tick 3: D exits 14 into an already-open breaker -> spilled uncharged, no re-drive.
    cluster.running.discard(name_d)
    cluster.exited[name_d] = _UNREACHABLE
    tally = await reconcile_cloud_jobs(_make_ctx())
    assert tally["unreachable_held"] == 1
    assert tally["redriven"] == 0

    for (fid, _name), seeded_attempts in zip(seeded, (0, 1, 2, 2), strict=True):
        cj = await _read_cloud_job(session, fid)
        assert cj.status == CloudJobStatus.AWAITING.value  # out of in-flight, slot released
        assert cj.attempts == seeded_attempts  # NOTHING charged -- and no row at the cap of 3
        assert cj.node_loss_redrives == 0
        assert (cj.last_exit_code, cj.last_failure_reason) == (_UNREACHABLE, "Error")
    assert sorted(s3.calls) == sorted(fid for fid, _ in seeded)  # each spill cleaned its staged object

    # The drain now stages nothing to the held backend. It returns before even fetching candidates
    # (skipped 0), where the same tick with the breaker closed fetches a first page of the backend's 3
    # free slots and holds them at the fileserver gate (skipped 3; no fileserver agent is seeded).
    settings = _drain_settings(monkeypatch)
    ctx = {"async_session": _make_ctx()["async_session"], "task_router": SimpleNamespace()}
    with structlog.testing.capture_logs() as logs:
        held = await release_awaiting_cloud.stage_cloud_window(ctx)
    assert held == {"staged": 0, "skipped": 0}
    assert any(str(e.get("event", "")).startswith("stage_cloud_window: backend held") for e in logs)

    await session.execute(update(BackendBreaker).values(tripped_at=None, next_probe_at=None))
    await session.commit()
    released = await release_awaiting_cloud.stage_cloud_window(ctx)
    assert released == {"staged": 0, "skipped": settings.backends[0].cap}


def _drain_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the drain at the same one-kueue-backend stub reconcile ran under, with a reachable LocalQueue."""
    from phaze.tasks import reconcile_cloud_jobs as reconcile_mod

    settings = reconcile_mod.get_settings()
    settings.cloud_spill_to_local_after_seconds = 900
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: settings)
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", AsyncMock(return_value=fake_local_queue()))
    return settings


@pytest.mark.asyncio
async def test_three_distinct_files_in_one_tick_trip_the_breaker(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any) -> None:
    """The distinct-files clause alone: three first-time exits trip it, so the third file is held rather than re-driven."""
    cluster, _s3 = _install(monkeypatch, kube_respx)
    seeded = [await _seed(session, attempts=0) for _ in range(3)]
    for _fid, name in seeded:
        cluster.exited[name] = _UNREACHABLE

    tally = await reconcile_cloud_jobs(_make_ctx())

    assert (tally["redriven"], tally["unreachable_held"]) == (2, 1)
    breaker = await _breaker(session)
    assert breaker is not None and breaker.tripped_at is not None
    assert breaker.trip_reason is not None and breaker.trip_reason.startswith("3 files exited 14")
    for fid, _name in seeded:
        assert (await _read_cloud_job(session, fid)).attempts == 0


@pytest.mark.asyncio
async def test_a_lone_file_trips_the_breaker_on_its_second_unreachable_exit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any
) -> None:
    """ "Not charged" must still be bounded: one file alone costs at most two pods before the backend is held."""
    cluster, _s3 = _install(monkeypatch, kube_respx)
    fid, name = await _seed(session, attempts=1)
    cluster.exited[name] = _UNREACHABLE

    first = await reconcile_cloud_jobs(_make_ctx())
    assert (first["redriven"], first["unreachable_held"]) == (1, 0)
    await _resubmit(session, cluster, fid, name, exit_code=_UNREACHABLE)
    second = await reconcile_cloud_jobs(_make_ctx())

    assert (second["redriven"], second["unreachable_held"]) == (0, 1)
    cj = await _read_cloud_job(session, fid)
    assert (cj.status, cj.attempts) == (CloudJobStatus.AWAITING.value, 1)
    breaker = await _breaker(session)
    assert breaker is not None and breaker.trip_reason is not None and "twice" in breaker.trip_reason


@pytest.mark.asyncio
async def test_exit_10_is_still_charged_alongside_an_open_breaker(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any) -> None:
    """The hold is scoped to exit 14: a failed object GET on a held backend still spends the file's attempt."""
    cluster, _s3 = _install(monkeypatch, kube_respx)
    fid, name = await _seed(session, attempts=0)
    session.add(BackendBreaker(backend_id=_KUEUE_BACKEND_ID, tripped_at=datetime.now(UTC), next_probe_at=datetime.now(UTC) + timedelta(minutes=15)))
    await session.commit()
    cluster.exited[name] = 10

    tally = await reconcile_cloud_jobs(_make_ctx())

    assert (tally["redriven"], tally["unreachable_held"]) == (1, 0)
    assert (await _read_cloud_job(session, fid)).attempts == 1


@pytest.mark.asyncio
async def test_exits_before_the_last_close_do_not_count(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, kube_respx: Any) -> None:
    """After a close, the failures that opened the breaker are retired: one fresh exit does not re-trip it."""
    cluster, _s3 = _install(monkeypatch, kube_respx)
    now = datetime.now(UTC)
    for _ in range(3):
        old_fid, _old_name = await _seed(session, attempts=0)
        await session.execute(
            update(CloudJob)
            .where(CloudJob.file_id == old_fid)
            .values(status=CloudJobStatus.AWAITING.value, last_exit_code=_UNREACHABLE, last_failed_at=now - timedelta(minutes=2))
        )
    session.add(BackendBreaker(backend_id=_KUEUE_BACKEND_ID, reset_at=now - timedelta(minutes=1)))
    await session.commit()
    fid, name = await _seed(session, attempts=0)
    cluster.exited[name] = _UNREACHABLE

    tally = await reconcile_cloud_jobs(_make_ctx())

    assert (tally["redriven"], tally["unreachable_held"]) == (1, 0)
    breaker = await _breaker(session)
    assert breaker is not None and breaker.tripped_at is None
    assert (await _read_cloud_job(session, fid)).attempts == 0


@pytest.mark.asyncio
async def test_a_row_without_a_backend_id_is_held_uncharged(session: AsyncSession) -> None:
    """Defence in depth: a row that cannot feed a breaker takes the held branch -- a spill that charges nothing and cannot loop."""
    from phaze.tasks import reconcile_cloud_jobs as reconcile_mod
    from phaze.tasks.cloud_reconcile_observation import TerminalFailure

    fid, _name = await _seed(session, attempts=2)
    cloud_job = await _read_cloud_job(session, fid)
    cloud_job.backend_id = None
    await session.commit()
    spilled: list[int] = []

    async def _fake_spill(_cfg: Any, _row: Any, _name: Any, *, attempts: int, failure: Any) -> None:
        spilled.append(attempts)

    row = SimpleNamespace(cloud_job=cloud_job, session=session, tally={"unreachable_held": 0})
    original = reconcile_mod._spill_to_awaiting
    reconcile_mod._spill_to_awaiting = _fake_spill  # type: ignore[assignment]
    try:
        await reconcile_mod._hold_control_plane_unreachable(SimpleNamespace(), row, None, failure=TerminalFailure(_UNREACHABLE, "Error"))  # type: ignore[arg-type]
    finally:
        reconcile_mod._spill_to_awaiting = original  # type: ignore[assignment]
    assert spilled == [2]
    assert row.tally["unreachable_held"] == 1


# --- the drain's gate --------------------------------------------------------------------------------


def _open(next_probe_at: datetime | None) -> backend_breaker.OpenBreaker:
    return backend_breaker.OpenBreaker(backend_id="kueue-x", tripped_at=datetime.now(UTC), trip_reason="r", next_probe_at=next_probe_at)


@pytest.mark.parametrize(
    ("breaker", "remaining", "expected"),
    [
        (None, 3, (3, False)),
        ("not_due", 3, (0, False)),
        ("due", 3, (1, True)),
        ("due", 0, (0, False)),
        ("unset", 2, (1, True)),
    ],
    ids=["closed", "open-not-due", "open-probe-due", "open-due-but-full", "open-no-probe-time"],
)
def test_gate_remaining(breaker: str | None, remaining: int, expected: tuple[int, bool]) -> None:
    """Closed passes through; open holds at 0 until the probe is due, then grants exactly one slot."""
    now = datetime.now(UTC)
    state = {
        None: None,
        "not_due": _open(now + timedelta(minutes=5)),
        "due": _open(now - timedelta(seconds=1)),
        "unset": _open(None),
    }[breaker]
    assert backend_breaker.gate_remaining(state, remaining, now) == expected


class _FakeBackend:
    """The two probe surfaces the drain snapshot reads, and nothing else."""

    def __init__(self, backend_id: str, cap: int) -> None:
        self.id = backend_id
        self.cap = cap

    async def is_available(self, session: Any) -> bool:  # noqa: ARG002
        return True

    async def in_flight_count(self, session: Any) -> int:  # noqa: ARG002
        return 0


@pytest.mark.asyncio
async def test_snapshot_holds_a_tripped_backend_as_full_and_arms_one_probe(session: AsyncSession) -> None:
    """Held reads as available-but-full (so local spill stays staleness-gated), and a due probe gets one slot once."""
    now = datetime.now(UTC)
    session.add(BackendBreaker(backend_id="held", tripped_at=now, trip_reason="3 files", next_probe_at=now - timedelta(seconds=1)))
    session.add(BackendBreaker(backend_id="waiting", tripped_at=now, trip_reason="3 files", next_probe_at=now + timedelta(minutes=10)))
    session.add(BackendBreaker(backend_id="healthy", reset_at=now))
    await session.commit()

    backends = [_FakeBackend("held", 4), _FakeBackend("waiting", 4), _FakeBackend("healthy", 4)]
    first = await release_awaiting_cloud._snapshot_backend_slots(backends, session)  # type: ignore[arg-type]
    await session.commit()
    second = await release_awaiting_cloud._snapshot_backend_slots(backends, session)  # type: ignore[arg-type]

    assert {k: (v["available"], v["remaining"]) for k, v in first.items()} == {"held": (True, 1), "waiting": (True, 0), "healthy": (True, 4)}
    assert second["held"]["remaining"] == 0  # the probe slot was spent: next_probe_at moved an interval on
    session.expire_all()
    armed = (await session.execute(select(BackendBreaker.next_probe_at).where(BackendBreaker.backend_id == "held"))).scalar_one()
    assert armed is not None and armed > now + timedelta(seconds=backend_breaker.PROBE_INTERVAL_SECONDS - 60)


@pytest.mark.asyncio
async def test_snapshot_still_grants_the_probe_when_arming_fails(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed arm write is contained in its SAVEPOINT: the probe slot stands and the tick's transaction survives."""
    now = datetime.now(UTC)
    session.add(BackendBreaker(backend_id="held", tripped_at=now, next_probe_at=now - timedelta(seconds=1)))
    await session.commit()
    monkeypatch.setattr(backend_breaker, "arm_next_probe", AsyncMock(side_effect=RuntimeError("boom")))

    snapshot = await release_awaiting_cloud._snapshot_backend_slots([_FakeBackend("held", 4)], session)  # type: ignore[list-item]

    assert snapshot["held"]["remaining"] == 1
    assert (await session.execute(select(BackendBreaker.backend_id))).scalars().all() == ["held"]


@pytest.mark.asyncio
async def test_open_breakers_read_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A breaker read that cannot complete reports every backend closed rather than silently stopping the lane."""

    class _Broken:
        def begin_nested(self) -> Any:
            raise RuntimeError("db down")

    monkeypatch.setattr(backend_breaker.logger, "warning", lambda *_a, **_k: None)
    assert await backend_breaker.load_open_breakers(_Broken()) == {}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_close_breaker_resets_only_an_open_one(session: AsyncSession) -> None:
    """Close clears the trip and stamps reset_at; closing a closed (or absent) breaker is a no-op that reports False."""
    now = datetime.now(UTC)
    session.add(BackendBreaker(backend_id="open", tripped_at=now - timedelta(minutes=5), trip_reason="r", next_probe_at=now))
    session.add(BackendBreaker(backend_id="closed"))
    await session.commit()

    assert await backend_breaker.close_breaker(session, "open", now, evidence="test") is True
    assert await backend_breaker.close_breaker(session, "closed", now, evidence="test") is False
    assert await backend_breaker.close_breaker(session, "absent", now, evidence="test") is False
    await session.commit()

    session.expire_all()
    row = (await session.execute(select(BackendBreaker).where(BackendBreaker.backend_id == "open"))).scalar_one()
    assert (row.tripped_at, row.trip_reason, row.next_probe_at) == (None, None, None)
    assert row.reset_at is not None


@pytest.mark.asyncio
async def test_record_on_an_open_breaker_pushes_the_probe_out(session: AsyncSession) -> None:
    """Fresh unreachable evidence while open keeps it open and moves the next probe an interval past now."""
    now = datetime.now(UTC)
    session.add(BackendBreaker(backend_id="b", tripped_at=now - timedelta(minutes=20), trip_reason="orig", next_probe_at=now - timedelta(minutes=1)))
    await session.commit()

    verdict = await backend_breaker.record_unreachable_exit(session, "b", uuid.uuid4(), now, previous_exit_code=None, previous_failed_at=None)
    await session.commit()

    assert verdict == backend_breaker.UnreachableVerdict(held=True, tripped_now=False, reason="orig")
    session.expire_all()
    row = (await session.execute(select(BackendBreaker).where(BackendBreaker.backend_id == "b"))).scalar_one()
    assert row.next_probe_at == now + timedelta(seconds=backend_breaker.PROBE_INTERVAL_SECONDS)


@pytest.mark.asyncio
async def test_record_treats_naive_timestamps_as_utc(session: AsyncSession) -> None:
    """A naive previous-failure time (the scan_reaper convention) still counts toward the repeat clause."""
    now = datetime.now(UTC)
    verdict = await backend_breaker.record_unreachable_exit(
        session, "naive", uuid.uuid4(), now, previous_exit_code=_UNREACHABLE, previous_failed_at=(now - timedelta(minutes=1)).replace(tzinfo=None)
    )
    assert verdict.held and verdict.tripped_now
