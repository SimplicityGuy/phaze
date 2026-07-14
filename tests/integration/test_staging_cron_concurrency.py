"""WR-04 / SCHED-02: overlapping ``stage_cloud_window`` ticks never overshoot the cap (real PG, 92-04).

Moved here from ``tests/analyze/core/test_staging_cron.py`` by plan 92-04 (Option B): these three cells
race TWO concurrent staging ticks under ``asyncio.gather`` and rely on a transaction-scoped
``pg_advisory_xact_lock`` to serialize the count+claim so the committed PUSHING / in-flight set never
exceeds ``cloud_max_in_flight``. Real serialization requires two INDEPENDENT DB connections that see each
other's COMMITTED writes -- which the hermetic single-connection ``create_savepoint`` ``session`` fixture
(92-03) fundamentally cannot provide (a second connection reads ZERO/STALE and no advisory lock spans two
real transactions). They therefore live in ``tests/integration/`` on the ``committed_db`` fixture, where
each tick opens its OWN pool connection off a real ``async_sessionmaker`` (exactly like production's
controller ctx) and the advisory lock actually blocks the second tick until the first commits.

Assertions are preserved byte-for-byte from the donor; only fixture/engine acquisition changed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import kube_staging, s3_staging
from phaze.tasks.release_awaiting_cloud import stage_cloud_window
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent
from tests.kube_fakes import fake_local_queue


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


class _StubCfg:
    """Minimal stand-in for the registry-derived reads stage_cloud_window makes (copied from the donor)."""

    def __init__(
        self,
        *,
        active_cap: int = 2,
        cloud_enabled: bool = True,
        active_cloud_kind: str | None = "compute",
        cloud_submit_max_attempts: int = 3,
        cloud_spill_to_local_after_seconds: int = 900,
    ) -> None:
        self.active_cap = active_cap
        self.cloud_enabled = cloud_enabled
        self.active_cloud_kind = active_cloud_kind
        self.cloud_submit_max_attempts = cloud_submit_max_attempts
        self.cloud_spill_to_local_after_seconds = cloud_spill_to_local_after_seconds
        self.buckets = [SimpleNamespace(id="staging-1", bucket="phaze-staging")]
        if active_cloud_kind is None:
            self.backends = [SimpleNamespace(kind="local", id="local", rank=0, cap=active_cap)]
        elif active_cloud_kind == "kueue":
            self.backends = [
                SimpleNamespace(
                    kind="kueue",
                    id="kueue-1",
                    rank=10,
                    cap=active_cap,
                    buckets=["staging-1"],
                    kube=SimpleNamespace(api_url="https://kube.test", namespace="phaze", local_queue="phaze-lq"),
                )
            ]
        else:
            self.backends = [
                SimpleNamespace(
                    kind=active_cloud_kind,
                    id=f"{active_cloud_kind}-1",
                    rank=10,
                    cap=active_cap,
                    agent_ref="cloud-1",
                    push_host="cloud-1.push.example",
                    scratch_dir="/srv/scratch",
                    ssh_user=None,
                )
            ]


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, max_in_flight: int = 2, cloud_kind: str | None = "compute") -> None:
    """Pin stage_cloud_window's get_settings() to a registry-derived stub (copied from the donor)."""
    stub = _StubCfg(active_cap=max_in_flight, cloud_enabled=cloud_kind is not None, active_cloud_kind=cloud_kind)
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: stub)
    monkeypatch.setattr("phaze.services.backends.get_settings", lambda: stub)


def _patch_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the S3 SDK + Kueue LocalQueue probe the k8s branch's ``_stage_file_to_s3`` core makes (copied)."""
    monkeypatch.setattr(s3_staging, "create_multipart_upload", AsyncMock(return_value="upload-xyz"))
    monkeypatch.setattr(s3_staging, "presign_upload_parts", AsyncMock(return_value=["https://s3.test/part?1"]))
    monkeypatch.setattr(kube_staging, "get_local_queue", AsyncMock(return_value=fake_local_queue()))


def _make_ctx(engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx whose ``async_session`` is a REAL sessionmaker on ``engine``.

    Unlike the hermetic donor (which routed ``async_session`` onto the single per-test connection), each
    ``async with ctx["async_session"]()`` opens its OWN pool connection -- so two overlapping ticks race
    exactly like two SAQ cron runs and contend on the real ``pg_advisory_xact_lock``.
    """
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated FileRecord row (AWAITING_CLOUD by default; copied from the donor)."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
    )


_HELD = "awaiting_cloud"
_DISPATCHED = "pushing"


async def _states_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Derive each file's effective drain state from its ``cloud_job`` sidecar (copied from the donor)."""
    stmt = select(CloudJob).where(CloudJob.file_id.in_(ids)).execution_options(populate_existing=True)
    jobs = {r.file_id: r.status for r in (await session.execute(stmt)).scalars().all()}
    out: dict[uuid.UUID, str] = {}
    for fid in ids:
        status = jobs.get(fid)
        out[fid] = _DISPATCHED if (status is not None and status != CloudJobStatus.AWAITING.value) else _HELD
    return out


async def _seed_fk_fileserver(session: AsyncSession) -> None:
    """Seed the ``test-fileserver`` FK-parent agent (INACTIVE: no last_seen_at) that ``_make_file`` targets.

    In the hermetic suite this row is seeded once by the session-scoped ``async_engine`` fixture; here the
    ``committed_db`` fixture TRUNCATEs between tests, so each test seeds its own FK parent. It is left
    non-``active`` (no ``last_seen_at``) so ``select_active_agent`` picks the ``nox`` routing fileserver.
    """
    session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
    await session.commit()


async def test_overlapping_ticks_never_exceed_window(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-04: two concurrent staging ticks must not each see window=0 and stage 2x the cap.

    The window COUNT reads committed truth, so without serialization two overlapping ticks could
    SKIP LOCKED past each other's uncommitted PUSHING flips and stage up to 2 * cloud_max_in_flight.
    A transaction-scoped advisory lock makes the count+claim atomic so the committed PUSHING set
    never exceeds the cap, even under concurrency.
    """
    engine, session_factory = committed_db
    _patch_settings(monkeypatch, max_in_flight=2)
    async with session_factory() as session:
        await _seed_fk_fileserver(session)
        await seed_active_agent(session, agent_id="cloud-1", kind="compute")
        await seed_active_agent(session, agent_id="nox", kind="fileserver")
        backlog = [_make_file() for _ in range(20)]
        session.add_all(backlog)
        await session.commit()
        ids = [f.id for f in backlog]

    router = DedupFakeTaskRouter()
    ctx = _make_ctx(engine, router, DedupFakeQueue("controller"))
    # Two overlapping ticks driven concurrently on the same event loop (each opens its own session
    # from the sessionmaker, so they race exactly like two SAQ cron runs).
    results = await asyncio.gather(stage_cloud_window(ctx), stage_cloud_window(ctx))

    async with session_factory() as session:
        states = await _states_for(session, ids)
    pushing = [fid for fid, st in states.items() if st == _DISPATCHED]
    assert len(pushing) <= 2, f"window overshot: {len(pushing)} files PUSHING (cap is 2)"
    assert sum(r["staged"] for r in results) <= 2, "concurrent ticks staged more than the cap"


async def test_k8s_overlapping_ticks_never_exceed_window(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-04 under the k8s branch: two concurrent ticks staging to S3 must not overshoot the ≤N cap.

    The k8s branch calls the NO-COMMIT ``_stage_file_to_s3`` core (L1), so the advisory lock is held
    across the whole tick and the committed PUSHING set never exceeds cloud_max_in_flight.
    """
    engine, session_factory = committed_db
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="kueue")
    _patch_s3(monkeypatch)
    async with session_factory() as session:
        await _seed_fk_fileserver(session)
        await seed_active_agent(session, agent_id="nox", kind="fileserver")
        backlog = [_make_file() for _ in range(20)]
        session.add_all(backlog)
        await session.commit()
        ids = [f.id for f in backlog]

    router = DedupFakeTaskRouter()
    ctx = _make_ctx(engine, router, DedupFakeQueue("controller"))
    results = await asyncio.gather(stage_cloud_window(ctx), stage_cloud_window(ctx))

    async with session_factory() as session:
        states = await _states_for(session, ids)
    pushing = [fid for fid, st in states.items() if st == _DISPATCHED]
    assert len(pushing) <= 2, f"k8s window overshot: {len(pushing)} files PUSHING (cap is 2)"
    assert sum(r["staged"] for r in results) <= 2, "concurrent k8s ticks staged more than the cap"


async def test_overlapping_ticks_never_overshoot_per_backend_cap(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-02: two overlapping ticks against the SAME backend never push its in_flight_count past cap.

    Phase 69 counts a backend's in-flight window from its ``cloud_job`` rows (backend_id-scoped), so the
    per-backend cap is the load-bearing bound. Two concurrent ticks serialize on the single advisory lock
    (WR-04), so the committed cloud_job rows for the backend never exceed its cap even under concurrency.
    """
    engine, session_factory = committed_db
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="compute")  # single compute-1 backend, cap 2
    async with session_factory() as session:
        await _seed_fk_fileserver(session)
        await seed_active_agent(session, agent_id="cloud-1", kind="compute")
        await seed_active_agent(session, agent_id="nox", kind="fileserver")
        backlog = [_make_file() for _ in range(20)]
        session.add_all(backlog)
        await session.commit()

    router = DedupFakeTaskRouter()
    ctx = _make_ctx(engine, router, DedupFakeQueue("controller"))
    results = await asyncio.gather(stage_cloud_window(ctx), stage_cloud_window(ctx))

    async with session_factory() as session:
        in_flight = int(
            (
                await session.execute(
                    select(func.count(CloudJob.id)).where(
                        CloudJob.backend_id == "compute-1",
                        CloudJob.status.in_(
                            [s.value for s in (CloudJobStatus.UPLOADING, CloudJobStatus.UPLOADED, CloudJobStatus.SUBMITTED, CloudJobStatus.RUNNING)]
                        ),
                    )
                )
            ).scalar()
            or 0
        )
    assert in_flight <= 2, f"per-backend cap overshot: compute-1 has {in_flight} in-flight cloud_job rows (cap is 2)"
    assert sum(r["staged"] for r in results) <= 2, "concurrent ticks staged more than the per-backend cap"
