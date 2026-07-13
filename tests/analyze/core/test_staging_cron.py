"""Tests for the Phase-50 "stay one ahead" bounded staging cron (CLOUDPIPE-01 / -05).

``stage_cloud_window(ctx)`` is the SINGLE driver that introduces new push work to the compute
agent. It tops the ≤N window up to ``cloud_max_in_flight`` by staging ``push_file`` for the oldest
held ``AWAITING_CLOUD`` files (FIFO by ``created_at``). The window is counted from COMMITTED
cloud_job truth (in-flight sidecar rows, D-08), so a 144-file backlog can never blow up the
single compute scratch disk -- the cron stages at most ``slots = cloud_max_in_flight - window``.

Gates (both wrapped in ``try/except NoActiveAgentError`` -> clean no-op, T-50-cron-raise):
  * no online COMPUTE agent (the analysis consumer)  -> staged=0, files stay AWAITING_CLOUD.
  * no online FILESERVER agent (the push initiator)   -> staged=0, skipped=len(candidates), held.

Staged files transition AWAITING_CLOUD -> PUSHING and enqueue ``push_file`` on the fileserver queue;
a double-tick collapses via the ``push_file:<id>`` deterministic key (counted as skipped).

``ctx`` mirrors the controller worker shape: ``async_session`` (a sessionmaker bound to the test
engine), ``queue`` (a controller-queue stand-in, unused) and ``task_router`` (a
``DedupFakeTaskRouter`` modeling SAQ deterministic-key dedup).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import cloud_staging, kube_staging, s3_staging
from phaze.tasks.release_awaiting_cloud import push_file_job_key, stage_cloud_window
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent
from tests.kube_fakes import fake_local_queue


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class _StubCfg:
    """Minimal stand-in for the registry-derived reads stage_cloud_window makes (Phase 67 rewire).

    The Phase-67 rewire (REG-04, D-14) moved the cron off the flat ``cloud_target`` /
    ``cloud_max_in_flight`` onto the registry-derived reads: ``cloud_enabled`` (the on/off gate),
    ``active_cap`` (the former cloud_max_in_flight) and the transitional ``active_cloud_kind``
    (``"compute"`` / ``"kueue"`` / ``None``). The former target→kind mapping is local→disabled,
    a1→compute, k8s→kueue; the defaults model a single compute backend so the Phase-50 rsync-push
    regressions keep exercising the ON path.
    """

    def __init__(
        self,
        *,
        active_cap: int = 2,
        cloud_enabled: bool = True,
        active_cloud_kind: str | None = "compute",
        backends: list[Any] | None = None,
        cloud_submit_max_attempts: int = 3,
        cloud_spill_to_local_after_seconds: int = 900,
    ) -> None:
        self.active_cap = active_cap
        self.cloud_enabled = cloud_enabled
        self.active_cloud_kind = active_cloud_kind
        # Phase 69: the pure select_backend policy the drain calls reads these two bounded knobs
        # (D-04 attempt-exclusion + D-01/D-03 staleness gate on local spill).
        self.cloud_submit_max_attempts = cloud_submit_max_attempts
        self.cloud_spill_to_local_after_seconds = cloud_spill_to_local_after_seconds
        # Phase 70 (MKUE-02): KueueBackend.dispatch picks a per-file bucket over ``config.buckets`` and
        # resolves its BucketConfig via ``resolve_bucket_config(get_settings(), id)`` -- so the stub carries
        # a ``buckets`` registry and the kueue backend entry binds that id-list.
        self.buckets = [SimpleNamespace(id="staging-1", bucket="phaze-staging")]
        # Phase 68/69: the drain resolves its dispatch backends via resolve_backends(cfg), which reads
        # the registry-shaped ``backends`` list (each entry duck-types the Phase-67 submodel's
        # kind/id/rank/cap). ``backends`` (explicit) lets a multi-backend cell seed N entries; otherwise
        # one non-local backend of the cell's kind, or a local entry when cloud disabled.
        if backends is not None:
            self.backends = backends
        elif active_cloud_kind is None:
            self.backends = [SimpleNamespace(kind="local", id="local", rank=0, cap=active_cap)]
        elif active_cloud_kind == "kueue":
            # Phase 70 (MKUE-01/D-04): KueueBackend.is_available threads self.config.kube into
            # kube_staging.get_local_queue; carry a minimal kube (the seam is stubbed in these cells).
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
            # Phase 72 (MCOMP-01/D-02): a compute backend's is_available resolves THIS entry's bound
            # ``agent_ref`` against Agent.id per-call, so the default compute stub binds the ``cloud-1``
            # compute agent every default-single-compute cell seeds online.
            self.backends = [
                SimpleNamespace(
                    kind=active_cloud_kind,
                    id=f"{active_cloud_kind}-1",
                    rank=10,
                    cap=active_cap,
                    agent_ref="cloud-1",
                    # Phase 73 (D-02): ComputeAgentBackend.dispatch reads push_host/scratch_dir off the
                    # bound config to stamp the push destination -- the duck-typed stub must carry them.
                    push_host="cloud-1.push.example",
                    scratch_dir="/srv/scratch",
                    ssh_user=None,
                )
            ]


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, max_in_flight: int = 2, cloud_kind: str | None = "compute") -> None:
    """Pin stage_cloud_window's get_settings() to a registry-derived stub.

    ``cloud_kind`` selects the reduction the cron reads: ``None`` -> cloud disabled (all-local, the
    implicit-local registry); ``"compute"`` -> the rsync-push path (GATE-1 compute probe active);
    ``"kueue"`` -> the S3 path (GATE-1 skipped). ``max_in_flight`` becomes the stub's ``active_cap``.
    """
    stub = _StubCfg(active_cap=max_in_flight, cloud_enabled=cloud_kind is not None, active_cloud_kind=cloud_kind)
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: stub)
    # Phase 70 (MKUE-02): KueueBackend.dispatch resolves the picked bucket via ``backends.get_settings()``;
    # pin it to the SAME stub so ``resolve_bucket_config`` finds the stub's ``buckets`` registry.
    monkeypatch.setattr("phaze.services.backends.get_settings", lambda: stub)


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + per-agent dedup router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3", created_at: datetime | None = None) -> FileRecord:
    """Build a fully-populated FileRecord row (AWAITING_CLOUD by default)."""
    uid = uuid.uuid4()
    kwargs: dict[str, Any] = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        **kwargs,
    )


# Post-MIG-04 the ``files.state`` dual-write is gone, so a file's effective drain state is DERIVED
# purely from its ``cloud_job`` sidecar. These string labels are exactly the values the retired
# ``_HELD`` / ``_DISPATCHED`` StrEnum members carried, so every assertion below
# keeps its original meaning.
_HELD = "awaiting_cloud"
_DISPATCHED = "pushing"


async def _states_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Derive each file's effective drain state from its ``cloud_job`` sidecar (the sole authority).

    A file is DISPATCHED (``pushing``) iff it carries a NON-awaiting (in-flight) cloud_job row; otherwise
    -- no cloud_job row at all, or a row still at ``status='awaiting'`` -- it is HELD (``awaiting_cloud``).

    ``populate_existing`` refreshes the identity-mapped cloud_job rows (the drain mutated them in its own
    session) WITHOUT ``expire_all()`` -- expiring the caller's FileRecords would trigger a MissingGreenlet
    lazy-reload on the ``.id`` reads in the assertions.
    """
    stmt = select(CloudJob).where(CloudJob.file_id.in_(ids)).execution_options(populate_existing=True)
    jobs = {r.file_id: r.status for r in (await session.execute(stmt)).scalars().all()}
    out: dict[uuid.UUID, str] = {}
    for fid in ids:
        status = jobs.get(fid)
        out[fid] = _DISPATCHED if (status is not None and status != CloudJobStatus.AWAITING.value) else _HELD
    return out


async def _seed_awaiting_rows(session: AsyncSession, files: list[FileRecord]) -> None:
    """Give each held AWAITING_CLOUD file its ``cloud_job(status='awaiting')`` sidecar row (Phase 83, D-05).

    The sidecar drain (``get_cloud_staging_candidates``) no longer reads ``FileRecord.state`` (SC#1); it
    INNER-joins ``cloud_job`` on ``status='awaiting'``. A bare ``state`` write is therefore no longer a drain
    candidate -- the go-forward writer (``hold_awaiting_cloud``) + migration 034 give every held file this row.
    """
    for f in files:
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()


async def _seed_in_flight(
    session: AsyncSession,
    *,
    backend_id: str,
    count: int,
    status: CloudJobStatus = CloudJobStatus.SUBMITTED,
) -> None:
    """Seed ``count`` in-flight cloud_job rows for ``backend_id`` (Phase 69 per-backend in_flight_count).

    Phase 69 (D-05) counts a backend's in-flight window from its ``cloud_job`` rows
    (``backend_id`` + status in the in-flight set), NOT the old scalar ``{PUSHING, PUSHED}`` window.
    Each seeded row is a PUSHING file with a matching cloud_job so ``Backend.in_flight_count`` sees it.
    """
    for _ in range(count):
        f = _make_file()
        session.add(f)
        await session.flush()
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, backend_id=backend_id, s3_key=None, status=status.value))
    await session.commit()


# --- Window full -> stage 0 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_full_stages_zero(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the backend's cap already in flight, the cron stages 0 new files (Phase 69: per-backend count)."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # Window already full: 2 in-flight cloud_job rows for the compute backend == cap=2 (D-05 count).
    await _seed_in_flight(session, backend_id="compute-1", count=2)
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 0}
    assert router.queues == {}
    # Held files untouched.
    assert set((await _states_for(session, ids)).values()) == {_HELD}


# --- One free slot -> stage exactly 1 ---------------------------------------------------


@pytest.mark.asyncio
async def test_one_free_slot_stages_one(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """With cap-1 in flight, exactly one AWAITING_CLOUD file is staged to push_file + flipped to PUSHING."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await _seed_in_flight(session, backend_id="compute-1", count=1)  # 1 in flight (cloud_job), 1 slot free
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 1, "skipped": 0}
    # Exactly one push_file enqueued onto the FILESERVER agent's per-agent queue.
    push_queue = router.queues["nox-io"]
    assert [t for t, _ in push_queue.captured] == ["push_file"]
    # WR-03: the push_file job carries an explicit SAQ job-net timeout (above the asyncio outer
    # guard), NOT the inherited 600s role default that equalled push_timeout_sec.
    from phaze.tasks.push import PUSH_FILE_SAQ_TIMEOUT_SEC

    assert push_queue.captured_policy[0]["timeout"] == PUSH_FILE_SAQ_TIMEOUT_SEC
    # Exactly one held file flipped to PUSHING; the rest stay AWAITING_CLOUD.
    states = await _states_for(session, ids)
    assert sorted(states.values()) == sorted([_DISPATCHED, _HELD, _HELD])


# --- No compute agent -> no-op ----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_compute_agent_is_noop(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no compute agent online the cron is a clean no-op -- nothing staged, files stay AWAITING_CLOUD."""
    _patch_settings(monkeypatch, max_in_flight=2)
    # Only a fileserver agent online; the compute gate finds none -> NoActiveAgentError.
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 0}
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {_HELD}


# --- No fileserver agent -> no-op (compute online, fileserver absent) --------------------


@pytest.mark.asyncio
async def test_no_fileserver_agent_is_noop(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compute online but NO fileserver (the push initiator) -> clean no-op, files stay AWAITING_CLOUD.

    A fileserver offline during a rolling restart must be a clean hold (staged=0, skipped=len(candidates)),
    NOT a raise -- the held files re-stage on the next tick once the fileserver returns.
    """
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")  # no fileserver online
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    # Two slots free, two candidates locked, but no fileserver -> held, never raised.
    assert result == {"staged": 0, "skipped": 2}
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {_HELD}


# --- WR-02: fileserver vanishes mid-tick (present at GATE-2, gone at dispatch) -> clean hold ----


@pytest.mark.asyncio
async def test_fileserver_vanishes_mid_tick_holds_cleanly(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-02: a fileserver revoked between GATE-2 and the dispatch loop degrades to a clean hold, never a raise.

    ComputeAgentBackend.dispatch re-resolves the fileserver agent per file. Under READ COMMITTED a
    fileserver revoked by a concurrent session AFTER GATE-2 passes but BEFORE a later loop iteration
    raises NoActiveAgentError straight out of dispatch. The drain must catch it and hold the remaining
    candidates (staged=0, skipped=len(candidates)), leaving them AWAITING_CLOUD -- NOT propagate the raise
    (T-50-cron-raise). dispatch resolves the fileserver BEFORE any mutation, so the raising file is
    untouched.
    """
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="compute")
    # Both agents present so GATE-1 (compute is_available) and GATE-2 (fileserver) pass upfront.
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(2)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    # Simulate the mid-tick revocation: backends.select_active_agent (called INSIDE dispatch) raises for
    # the fileserver lookup while still resolving the compute agent GATE-1 (is_available) needs. GATE-2 in
    # release_awaiting_cloud uses its OWN imported select_active_agent (unpatched), so it still passes.
    from phaze.services import backends as backends_mod
    from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent

    async def _raise_for_fileserver(sess: AsyncSession, *, kind: str) -> Any:
        if kind == "fileserver":
            raise NoActiveAgentError(kind)
        return await select_active_agent(sess, kind=kind)

    monkeypatch.setattr(backends_mod, "select_active_agent", _raise_for_fileserver)

    router = DedupFakeTaskRouter()
    # Must NOT raise -- the cron degrades to a clean hold.
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 2}
    assert router.queues == {}
    # The raising file (and its peers) are untouched -- dispatch gates the fileserver BEFORE mutating.
    assert set((await _states_for(session, ids)).values()) == {_HELD}


# --- Phase 67 (REG-04, D-14): the registry cloud_enabled gate on the staging cron ---------


@pytest.mark.asyncio
async def test_cloud_disabled_stages_nothing(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF: with the implicit-local registry (cloud_enabled False) the cron is a clean no-op (D-14).

    Both agents are online and the window is wide open (3 held, N=2), so the cron WOULD stage if the
    registry held a cloud backend. All-local returns {"staged": 0, "skipped": 0}, takes no advisory
    lock, stages no push_file, and leaves every held file AWAITING_CLOUD (byte-identical to the former
    ``cloud_target == "local"`` no-op).
    """
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind=None)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 0}
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {_HELD}


@pytest.mark.asyncio
async def test_forced_local_drain_noop(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """FORCED-LOCAL: with the route_control 'global' row force_local True the drain is a clean no-op (D-08).

    Mirrors :func:`test_cloud_disabled_stages_nothing` but the registry still holds a compute backend
    (cloud_enabled True) -- the force-local override alone flips the drain to the all-local path. Both
    agents are online and the window is wide open (3 held, N=2), so the cron WOULD stage if not forced.
    Forced-local returns {"staged": 0, "skipped": 0} BEFORE the advisory lock, stages no push_file, and
    leaves every held file AWAITING_CLOUD (already-held files stay held while forced -- runbook A4).
    """
    from phaze.models.route_control import RouteControl

    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="compute")
    session.add(RouteControl(id="global", force_local=True))
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 0}
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {_HELD}


@pytest.mark.asyncio
async def test_cloud_compute_stages_normally(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: a single compute backend (active_cloud_kind='compute') stages as before (Phase 50 rsync-push regression)."""
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="compute")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 2, "skipped": 0}
    states = await _states_for(session, ids)
    assert sum(1 for st in states.values() if st == _DISPATCHED) == 2


# --- Phase 55 (D-01a): the k8s S3-staging branch -----------------------------------------


def _patch_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the S3 SDK calls the k8s branch's ``_stage_file_to_s3`` core makes (no live backend).

    Phase 68: also stub the Kueue LocalQueue probe -- the drain now clears GATE-1 through
    ``KueueBackend.is_available``, which probes ``kube_staging.get_local_queue`` (a cluster reach test
    with NO compute dependency, D-01a). Stub it "reachable" so the kueue cells proceed exactly as the
    pre-refactor drain did (which took no such probe on the k8s branch).
    """
    monkeypatch.setattr(s3_staging, "create_multipart_upload", AsyncMock(return_value="upload-xyz"))
    monkeypatch.setattr(s3_staging, "presign_upload_parts", AsyncMock(return_value=["https://s3.test/part?1"]))
    monkeypatch.setattr(kube_staging, "get_local_queue", AsyncMock(return_value=fake_local_queue()))


@pytest.mark.asyncio
async def test_k8s_branch_skips_compute_gate_and_stages_to_s3(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """k8s: GATE-1 (compute) is SKIPPED (L2) -- with NO compute agent the held files still reach PUSHING.

    The k8s branch stages via ``_stage_file_to_s3`` (enqueues ``s3_upload``, NOT ``push_file``) and a
    single post-loop commit fires. With max_in_flight=2 and 3 held, exactly 2 stage.
    """
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="kueue")
    _patch_s3(monkeypatch)
    # NO compute agent online -- only a fileserver. On a1 this would wedge; on k8s GATE-1 is skipped.
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 2, "skipped": 0}
    # The k8s branch enqueues s3_upload (NOT push_file) onto the fileserver's per-agent queue.
    upload_queue = router.queues["nox-io"]
    assert [t for t, _ in upload_queue.captured] == ["s3_upload", "s3_upload"]
    # Exactly two held files flipped to PUSHING; the third stays AWAITING_CLOUD.
    states = await _states_for(session, ids)
    assert sum(1 for st in states.values() if st == _DISPATCHED) == 2
    assert sum(1 for st in states.values() if st == _HELD) == 1


@pytest.mark.asyncio
async def test_k8s_branch_holds_with_no_fileserver(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """k8s with NO fileserver online: GATE-2 holds -- nothing staged, files stay AWAITING_CLOUD."""
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="kueue")
    _patch_s3(monkeypatch)
    # No agents online at all (compute skipped on k8s; fileserver gate still holds).
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    await _seed_awaiting_rows(session, held)
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 2}  # two slots, two candidates locked, no fileserver
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {_HELD}


@pytest.mark.asyncio
async def test_k8s_overlapping_ticks_never_exceed_window(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-04 under the k8s branch: two concurrent ticks staging to S3 must not overshoot the ≤N cap.

    The k8s branch calls the NO-COMMIT ``_stage_file_to_s3`` core (L1), so the advisory lock is held
    across the whole tick and the committed PUSHING set never exceeds cloud_max_in_flight.
    """
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="kueue")
    _patch_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backlog = [_make_file() for _ in range(20)]
    session.add_all(backlog)
    await session.commit()
    ids = [f.id for f in backlog]

    router = DedupFakeTaskRouter()
    ctx = _make_ctx(async_engine, router, DedupFakeQueue("controller"))
    results = await asyncio.gather(stage_cloud_window(ctx), stage_cloud_window(ctx))

    states = await _states_for(session, ids)
    pushing = [fid for fid, st in states.items() if st == _DISPATCHED]
    assert len(pushing) <= 2, f"k8s window overshot: {len(pushing)} files PUSHING (cap is 2)"
    assert sum(r["staged"] for r in results) <= 2, "concurrent k8s ticks staged more than the cap"


# --- FIFO: oldest AWAITING_CLOUD first ---------------------------------------------------


@pytest.mark.asyncio
async def test_fifo_oldest_awaiting_cloud_first(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging order is FIFO -- the oldest AWAITING_CLOUD file (by created_at) goes first."""
    _patch_settings(monkeypatch, max_in_flight=1)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # ``files.created_at`` is TIMESTAMP WITHOUT TIME ZONE -> seed naive datetimes.
    base = datetime.now() - timedelta(hours=3)
    oldest = _make_file(created_at=base)
    middle = _make_file(created_at=base + timedelta(hours=1))
    newest = _make_file(created_at=base + timedelta(hours=2))
    # Insert out of order to prove the ORDER BY (not insertion order) drives selection.
    session.add_all([newest, oldest, middle])
    await session.commit()
    await _seed_awaiting_rows(session, [newest, oldest, middle])

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 1, "skipped": 0}
    states = await _states_for(session, [oldest.id, middle.id, newest.id])
    assert states[oldest.id] == _DISPATCHED
    assert states[middle.id] == _HELD
    assert states[newest.id] == _HELD


# --- 144-file backlog never exceeds N ---------------------------------------------------


@pytest.mark.asyncio
async def test_backlog_of_144_stages_at_most_n(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 144-file AWAITING_CLOUD backlog with N=2 and an empty window stages AT MOST 2 in one tick."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backlog = [_make_file() for _ in range(144)]
    session.add_all(backlog)
    await session.commit()
    await _seed_awaiting_rows(session, backlog)
    ids = [f.id for f in backlog]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 2, "skipped": 0}
    states = await _states_for(session, ids)
    pushing = [fid for fid, st in states.items() if st == _DISPATCHED]
    assert len(pushing) == 2  # never the whole backlog
    assert sum(1 for st in states.values() if st == _HELD) == 142


# --- WR-04: overlapping ticks never overshoot the ≤N window ------------------------------


@pytest.mark.asyncio
async def test_overlapping_ticks_never_exceed_window(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-04: two concurrent staging ticks must not each see window=0 and stage 2x the cap.

    The window COUNT reads committed truth, so without serialization two overlapping ticks could
    SKIP LOCKED past each other's uncommitted PUSHING flips and stage up to 2 * cloud_max_in_flight.
    A transaction-scoped advisory lock makes the count+claim atomic so the committed PUSHING set
    never exceeds the cap, even under concurrency.
    """
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backlog = [_make_file() for _ in range(20)]
    session.add_all(backlog)
    await session.commit()
    ids = [f.id for f in backlog]

    router = DedupFakeTaskRouter()
    ctx = _make_ctx(async_engine, router, DedupFakeQueue("controller"))
    # Two overlapping ticks driven concurrently on the same event loop (each opens its own session
    # from the sessionmaker, so they race exactly like two SAQ cron runs).
    results = await asyncio.gather(stage_cloud_window(ctx), stage_cloud_window(ctx))

    states = await _states_for(session, ids)
    pushing = [fid for fid, st in states.items() if st == _DISPATCHED]
    assert len(pushing) <= 2, f"window overshot: {len(pushing)} files PUSHING (cap is 2)"
    assert sum(r["staged"] for r in results) <= 2, "concurrent ticks staged more than the cap"


# --- Double-tick collapses via the deterministic push_file:<id> key ----------------------


@pytest.mark.asyncio
async def test_double_tick_dedups_via_deterministic_key(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file whose push_file:<id> key is already live dedups to a skipped no-op (T-50-double-enqueue)."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    fileserver = await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file()
    session.add(f)
    await session.commit()
    fid = f.id
    await _seed_awaiting_rows(session, [f])  # Phase 83: the held file carries its awaiting sidecar row

    router = DedupFakeTaskRouter()
    # Pre-enqueue the deterministic key on the fileserver queue so the cron's enqueue dedups to None.
    live_queue = router.queue_for(fileserver.id, "io")
    await live_queue.enqueue("push_file", key=push_file_job_key(fid))
    router.queue_for_calls.clear()

    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 1}
    # The state still flips to PUSHING (the file left the AWAITING_CLOUD scan set; the live push job
    # will land it). The window count stays honest on the next tick.
    assert (await _states_for(session, [fid]))[fid] == _DISPATCHED


# --- Controller registration: function + single narrow */5 cron (replaces the drain) -----


def test_stage_cloud_window_registered_in_controller_functions_and_cron() -> None:
    """stage_cloud_window is in controller settings['functions'] AND exactly one CronJob('*/5 ...').

    It REPLACES the deprecated release_awaiting_cloud drain cron, so the old symbol is gone from the
    controller registration entirely.
    """
    from phaze.tasks import controller
    from phaze.tasks.release_awaiting_cloud import stage_cloud_window as scw

    assert scw in controller.settings["functions"], "stage_cloud_window not registered in settings['functions']"

    stage_crons = [cj for cj in controller.settings["cron_jobs"] if cj.function is scw]
    assert len(stage_crons) == 1, "stage_cloud_window must be registered as exactly one CronJob"
    assert stage_crons[0].cron == "*/5 * * * *", "staging cron must run every 5 minutes"

    # The deprecated drain cron is gone -- no controller function is named release_awaiting_cloud.
    fn_names = {getattr(fn, "__name__", "") for fn in controller.settings["functions"]}
    assert "release_awaiting_cloud" not in fn_names, "the deprecated release_awaiting_cloud drain cron must be removed"


# --- Landmine L1: the extracted no-commit _stage_file_to_s3 core -------------------------


@pytest.mark.asyncio
async def test_stage_file_to_s3_core_does_not_commit(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_stage_file_to_s3`` does the full staging body but NEVER calls ``session.commit`` (L1).

    The cron's advisory-locked loop calls this no-commit core per candidate and commits ONCE after
    the loop; a mid-loop commit would release ``pg_advisory_xact_lock`` and re-open the over-stage
    class. Prove the core (a) does not commit, yet (b) still enqueues ``s3_upload`` and (c) upserts
    the ``cloud_job`` row.
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()

    monkeypatch.setattr(s3_staging, "create_multipart_upload", AsyncMock(return_value="upload-xyz"))
    monkeypatch.setattr(s3_staging, "presign_upload_parts", AsyncMock(return_value=["https://s3.test/part?1"]))
    commit_spy = AsyncMock()
    monkeypatch.setattr(session, "commit", commit_spy)

    router = DedupFakeTaskRouter()
    bucket = SimpleNamespace(id="staging-1", bucket="phaze-staging")
    await cloud_staging._stage_file_to_s3(session, file, router, bucket)

    # (a) The core defers the commit -- the caller (the cron loop / the public wrapper) owns it.
    commit_spy.assert_not_awaited()
    # (b) Exactly one s3_upload enqueued onto the fileserver's per-agent queue.
    upload_queue = router.queues["nox-io"]
    assert [t for t, _ in upload_queue.captured] == ["s3_upload"]
    # (c) The cloud_job row was upserted (visible via autoflush within this uncommitted transaction).
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.upload_id == "upload-xyz"


@pytest.mark.asyncio
async def test_public_stage_file_to_s3_still_commits(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public ``stage_file_to_s3`` wrapper still commits (the redrive_upload caller is unaffected)."""
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()

    monkeypatch.setattr(s3_staging, "create_multipart_upload", AsyncMock(return_value="upload-xyz"))
    monkeypatch.setattr(s3_staging, "presign_upload_parts", AsyncMock(return_value=["https://s3.test/part?1"]))
    commit_spy = AsyncMock()
    monkeypatch.setattr(session, "commit", commit_spy)

    router = DedupFakeTaskRouter()
    bucket = SimpleNamespace(id="staging-1", bucket="phaze-staging")
    await cloud_staging.stage_file_to_s3(session, file, router, bucket)

    commit_spy.assert_awaited_once()


def test_staging_module_is_fastapi_free() -> None:
    """The staging module must stay control-only: no fastapi / phaze.routers import (import boundary).

    Parse the actual ``import`` / ``from`` statements via AST (so a mention in the docstring/prose
    does not count) and assert none reference the web layer.
    """
    import ast
    import pathlib

    import phaze.tasks.release_awaiting_cloud as mod

    src = mod.__file__
    assert src is not None
    tree = ast.parse(pathlib.Path(src).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imported), "staging module must not import fastapi"
    assert not any(name.startswith("phaze.routers") for name in imported), "staging module must not import phaze.routers"


# --- Phase 69: tiered multi-backend drain, per-backend overshoot, AWAITING_CLOUD-untouched guard ---


def _patch_multi_backends(monkeypatch: pytest.MonkeyPatch, backends: list[Any], **cfg_kw: Any) -> None:
    """Pin stage_cloud_window's get_settings() to a registry with N explicit (SimpleNamespace) backends."""
    monkeypatch.setattr(
        "phaze.tasks.release_awaiting_cloud.get_settings",
        lambda: _StubCfg(backends=backends, **cfg_kw),
    )


async def _backend_ids_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """Return each file's ``cloud_job.backend_id`` (absent from the map when the file has no cloud_job)."""
    session.expire_all()
    rows = (await session.execute(select(CloudJob.file_id, CloudJob.backend_id).where(CloudJob.file_id.in_(ids)))).all()
    return dict(rows)


@pytest.mark.asyncio
async def test_multi_backend_tick_dispatches_rank_first_and_spills(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-01: one tick routes candidates rank-first across N backends; a full top rank spills to the next.

    Registry: compute-a (rank 10, cap 1) + compute-b (rank 20, cap 2). Both are available. Phase 72
    (MCOMP-01/D-02) binds each compute backend to a DISTINCT ``agent_ref`` (two compute backends may not
    share one agent -- the D-04 boot guard forbids it), so each seeds its own online compute agent
    (``cloud-a`` / ``cloud-b``) plus one fileserver. With 4 FIFO candidates the free-slot limit is
    1+2=3, so 3 stage in one tick: the OLDEST fills the single top-rank (compute-a) slot, then the next
    two spill to compute-b (compute-a is now full within the tick -- the local ``remaining`` decrement
    drives the spill). The 4th candidate is beyond total capacity and stays AWAITING_CLOUD.
    """
    _patch_multi_backends(
        monkeypatch,
        [
            SimpleNamespace(
                kind="compute", id="compute-a", rank=10, cap=1, agent_ref="cloud-a", push_host="a.push", scratch_dir="/scratch/a", ssh_user=None
            ),
            SimpleNamespace(
                kind="compute", id="compute-b", rank=20, cap=2, agent_ref="cloud-b", push_host="b.push", scratch_dir="/scratch/b", ssh_user=None
            ),
        ],
    )
    await seed_active_agent(session, agent_id="cloud-a", kind="compute")
    await seed_active_agent(session, agent_id="cloud-b", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    base = datetime.now() - timedelta(hours=4)
    oldest = _make_file(created_at=base)
    second = _make_file(created_at=base + timedelta(hours=1))
    third = _make_file(created_at=base + timedelta(hours=2))
    fourth = _make_file(created_at=base + timedelta(hours=3))
    session.add_all([fourth, oldest, third, second])  # insert out of order -- ORDER BY created_at drives it
    await session.commit()
    await _seed_awaiting_rows(session, [fourth, oldest, third, second])
    oldest_id, second_id, third_id, fourth_id = oldest.id, second.id, third.id, fourth.id  # capture before the drain expires the objects

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 3, "skipped": 0}
    backend_ids = await _backend_ids_for(session, [oldest_id, second_id, third_id, fourth_id])
    # Rank-first: the oldest candidate takes the single top-rank (compute-a) slot.
    assert backend_ids[oldest_id] == "compute-a"
    # Spill: compute-a's cap=1 is full within the tick, so the next two land on compute-b (rank 20).
    assert backend_ids[second_id] == "compute-b"
    assert backend_ids[third_id] == "compute-b"
    # Beyond total capacity (1+2=3): the 4th candidate is never fetched -- its awaiting cloud_job row is
    # retained (D-05 no-deletion) with backend_id still NULL (never dispatched), and it stays AWAITING_CLOUD.
    assert backend_ids[fourth_id] is None
    assert (await _states_for(session, [fourth_id]))[fourth_id] == _HELD


@pytest.mark.asyncio
async def test_overlapping_ticks_never_overshoot_per_backend_cap(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCHED-02: two overlapping ticks against the SAME backend never push its in_flight_count past cap.

    Phase 69 counts a backend's in-flight window from its ``cloud_job`` rows (backend_id-scoped), so the
    per-backend cap is the load-bearing bound. Two concurrent ticks serialize on the single advisory lock
    (WR-04), so the committed cloud_job rows for the backend never exceed its cap even under concurrency.
    """
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="compute")  # single compute-1 backend, cap 2
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backlog = [_make_file() for _ in range(20)]
    session.add_all(backlog)
    await session.commit()

    router = DedupFakeTaskRouter()
    ctx = _make_ctx(async_engine, router, DedupFakeQueue("controller"))
    results = await asyncio.gather(stage_cloud_window(ctx), stage_cloud_window(ctx))

    from sqlalchemy import func

    session.expire_all()
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


@pytest.mark.asyncio
async def test_held_awaiting_untouched_keeps_updated_at(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RESEARCH A3: a file the drain considers but HOLDS is never UPDATE-dirtied -- its updated_at stays put.

    The spill-to-local staleness gate reads ``now - file.updated_at`` as the file's wait duration, which
    is only correct if no non-drain writer touches a parked AWAITING_CLOUD row (its ``updated_at`` must
    stay equal to its entry timestamp). Here a file that has exhausted its cloud attempt budget (attempts
    == cloud_submit_max_attempts) with NO local backend in the registry makes ``select_backend`` return
    None -- a clean per-candidate hold. The drain must leave the row (and its updated_at) byte-untouched.
    """
    _patch_settings(monkeypatch, max_in_flight=2, cloud_kind="compute")  # single compute-1 backend; NO local
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # A parked file whose cloud budget is spent: post Phase-83 (D-03) a spilled row is re-stamped to
    # status='awaiting' (NOT terminal FAILED) while retaining attempts == max (3) as the budget-spent
    # marker. 'awaiting' is out of IN_FLIGHT so remaining stays 2, yet select_backend excludes it from
    # cloud (attempts exhausted) and finds no local -> a clean per-candidate hold.
    f = _make_file()
    f.updated_at = datetime.now() - timedelta(hours=5)  # backdated entry timestamp (naive, matches the column)
    session.add(f)
    await session.commit()
    fid = f.id
    session.add(CloudJob(id=uuid.uuid4(), file_id=fid, backend_id="compute-1", s3_key=None, status=CloudJobStatus.AWAITING.value, attempts=3))
    await session.commit()

    session.expire_all()
    before = (await session.execute(select(FileRecord.updated_at).where(FileRecord.id == fid))).scalar_one()

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    # Clean hold: the file is considered (it is the sole FIFO candidate) but held -- nothing staged.
    assert result == {"staged": 0, "skipped": 1}
    session.expire_all()
    after = (await session.execute(select(FileRecord.updated_at).where(FileRecord.id == fid))).scalar_one()
    assert after == before, "a held AWAITING_CLOUD row must not be UPDATE-dirtied (updated_at is the staleness clock)"
    assert (await _states_for(session, [fid]))[fid] == _HELD
    # The drain never touched the cloud_job either -- attempts stays 3, status stays awaiting.
    cj = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    assert cj.attempts == 3
    assert cj.status == CloudJobStatus.AWAITING.value


# --- CR-01 (SCHED-01/03): a file spilled to local is NOT re-dispatched to cloud on a later tick ---


@pytest.mark.asyncio
async def test_local_spill_not_redispatched_to_cloud(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-01 verifier scenario: spill-to-local then cloud-frees -> the file is NOT re-dispatched to cloud.

    Registry: compute-1 (rank 10) + local (rank 99). Tick 1 runs with the compute agent OFFLINE, so the
    sole held file spills to local immediately (D-03 offline->local, not staleness-gated) and
    LocalBackend.dispatch flips it to LOCAL_ANALYZING. Post Phase-83 the drain excludes it on tick 2 via
    ``~inflight_clause(ANALYZE)`` -- its committed ``process_file:<id>`` ledger row (the before_enqueue
    hook's own write, seeded here since the DedupFakeQueue does not run that hook), NOT the retired
    ``FileRecord.state`` read. Its ``cloud_job(status='awaiting')`` row is RETAINED (D-05 rejects deletion;
    the D-14 reaper clears it at the analyze-terminal seam, not the drain). Tick 2 brings the compute agent
    ONLINE with a free slot; the file must NOT be re-selected / cloud-dispatched (no cross-backend
    double-dispatch, no stranded SUBMITTED compute cloud_job / leaked cap slot).

    Before the cutover (RED): the ledger conjunct absent, tick 2 re-selects the still-awaiting file and
    dispatches it to the freed compute backend (PUSHING + a promoted cloud_job row) -- the exact double-dispatch.
    """
    _patch_multi_backends(
        monkeypatch,
        [
            SimpleNamespace(
                kind="compute", id="compute-1", rank=10, cap=2, agent_ref="cloud-1", push_host="c1.push", scratch_dir="/srv/scratch", ssh_user=None
            ),
            SimpleNamespace(kind="local", id="local", rank=99, cap=4),
        ],
    )
    # Tick 1: only the fileserver is online; the compute agent is OFFLINE (spill-to-local immediate).
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file()
    session.add(f)
    await session.commit()
    fid = f.id
    await _seed_awaiting_rows(session, [f])  # the held file carries its awaiting sidecar row (Phase 83)

    router = DedupFakeTaskRouter()
    result1 = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    # The file spilled to local: dispatched exactly once. Phase 90 (D-09): the LOCAL_ANALYZING files.state
    # flip was removed, so the derived proof is that its cloud_job row was NOT promoted to a compute row --
    # it stays 'awaiting' with no backend_id (D-05 retains it; LocalBackend writes no cloud_job).
    assert result1 == {"staged": 1, "skipped": 0}
    spilled = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    assert spilled.status == CloudJobStatus.AWAITING.value
    assert spilled.backend_id is None
    # Seed the committed process_file:<id> ledger row the before_enqueue hook would have written (the
    # DedupFakeQueue does not run that hook) -- the ~inflight_clause fact that excludes it on tick 2.
    from phaze.models.scheduling_ledger import SchedulingLedger

    session.add(SchedulingLedger(key=f"process_file:{fid}", function="process_file", routing="agent", payload={"file_id": str(fid)}))
    await session.commit()

    # Tick 2: the compute agent comes online with a free slot -- a would-be cloud re-dispatch opportunity.
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    result2 = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    # No candidate remains (~inflight_clause excludes the locally-dispatched file).
    assert result2 == {"staged": 0, "skipped": 0}
    # It grew no COMPUTE cloud_job row -- its awaiting row is retained (D-05 no-deletion), never promoted
    # to a compute SUBMITTED row (no cross-backend double-dispatch, no leaked compute cap slot).
    session.expire_all()
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    assert row.status == CloudJobStatus.AWAITING.value  # retained awaiting row, never a promoted compute row
    assert row.backend_id is None  # never dispatched to compute (no backend_id stamp)
