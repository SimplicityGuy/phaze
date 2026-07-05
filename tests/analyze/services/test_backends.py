"""Layer 3 per-backend protocol unit tests + the Layer 2 D-02 equivalence invariant.

GUARDED SCAFFOLD. The production target ``phaze.services.backends`` (the ``Backend`` protocol +
``LocalBackend`` / ``ComputeAgentBackend`` / ``KueueBackend`` + ``resolve_backends``) lands in Wave 2.
Until then the module-top ``pytest.importorskip`` makes this file COLLECT cleanly (reported skipped)
so Wave 0 is green; it lights up automatically the moment ``backends.py`` appears -- no hand-managed
skip markers to flip.

The cells are authored correct-by-construction against design §4.2 and the 68-PATTERNS re-home map:

* ``is_available`` -- Local: always True; Compute: True only when a compute agent is online via
  ``select_active_agent(kind="compute")`` (GATE-1), False (never raises) when absent; Kueue: a kube /
  LocalQueue probe with NO compute-agent dependency (D-01a), returns bool, never raises.
* ``in_flight_count`` -- ``COUNT(cloud_job WHERE backend_id == self.id AND status IN
  {UPLOADING, UPLOADED, SUBMITTED, RUNNING})`` (D-10); Local is always 0 (no cloud_job rows).
* ``dispatch`` D-03 atomicity -- the ``FileState -> PUSHING`` flip and the ``cloud_job`` upsert land in
  the SAME caller-passed session, so there is never a committed in-flight FileState without a live
  non-terminal ``cloud_job`` row (no limbo row).
* ``reconcile`` -- Kueue cron read; Local/Compute callback-driven (no-op in the unit cells).

Layer 2 (D-02): ``sum(in_flight_count(b) for b in backends)`` equals the FileState ``{PUSHING,
PUSHED}`` window count for the single-backend case, over constructed FileState / ``cloud_job`` states
(Phase 69 / D-05 retired the global ``get_cloud_window_count`` helper; the window is counted inline).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.services import kube_staging, s3_staging
from tests._queue_fakes import DedupFakeTaskRouter, seed_active_agent
from tests.kube_fakes import fake_local_queue


# Wave 2 target -- skip the whole module until it exists (collects clean in Wave 0).
backends = pytest.importorskip("phaze.services.backends")


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# D-10 (Q3): the exact non-terminal in-flight status set in_flight_count counts. Terminal =
# {SUCCEEDED, FAILED}. Pinned here so a Wave-2 drift from this set fails these cells loudly.
IN_FLIGHT_STATUSES = (
    CloudJobStatus.UPLOADING,
    CloudJobStatus.UPLOADED,
    CloudJobStatus.SUBMITTED,
    CloudJobStatus.RUNNING,
)
TERMINAL_STATUSES = (CloudJobStatus.SUCCEEDED, CloudJobStatus.FAILED)


# --- backend factories (Wave 2 finalizes the exact constructor signatures) ---------------


def _local(**kw: Any) -> Any:
    """Construct a LocalBackend (id/rank/cap; is_available always True, in_flight_count 0)."""
    return backends.LocalBackend(id=kw.get("id", "local"), rank=kw.get("rank", 0), cap=kw.get("cap", 0))


def _compute(**kw: Any) -> Any:
    """Construct a ComputeAgentBackend bound to a single registry entry.

    Phase 72 (MCOMP-01/D-02): ``is_available`` resolves ``self.config.agent_ref`` against ``Agent.id``,
    so the backend must carry a real ``ComputeBackend`` config. ``agent_ref`` defaults to the backend id
    (the byte-identical single-compute deploy binds agent_ref == the online agent's id); pass
    ``agent_ref=`` to bind a specific / mismatched node, or ``config=None`` to exercise the unbound
    fail-loud accessor path.
    """
    from phaze.config_backends import ComputeBackend as ComputeEntry

    bid = kw.get("id", "compute-a1")
    rank = kw.get("rank", 10)
    cap = kw.get("cap", 2)
    if "config" in kw:
        config = kw["config"]
    else:
        config = ComputeEntry(
            kind="compute",
            id=bid,
            rank=rank,
            cap=cap,
            agent_ref=kw.get("agent_ref", bid),
            scratch_dir=kw.get("scratch_dir", "/srv/scratch"),
        )
    return backends.ComputeAgentBackend(id=bid, rank=rank, cap=cap, config=config)


def _kueue(**kw: Any) -> Any:
    """Construct a KueueBackend bound to a registry entry carrying a ``[kube]`` config (MKUE-01/D-04).

    Phase 70: ``is_available`` / ``reconcile`` thread ``self.config.kube`` into every ``kube_staging``
    verb, so the backend must carry a ``config`` with a ``KubeConfig``. The ``kube_staging`` seam is
    monkeypatched in these unit cells, so a minimal KubeConfig (api_url/namespace/local_queue) suffices.
    """
    from phaze.config_backends import KubeConfig, KueueBackend as KueueEntry

    bid = kw.get("id", "kueue-x64")
    rank = kw.get("rank", 20)
    cap = kw.get("cap", 5)
    entry = KueueEntry(
        kind="kueue",
        id=bid,
        rank=rank,
        cap=cap,
        kube=KubeConfig(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq"),
        buckets=list(kw.get("buckets", [])),
    )
    return backends.KueueBackend(id=bid, rank=rank, cap=cap, config=entry)


def _kueue_with_buckets(backends_toml_env: Any, *, bucket_ids: list[str], backend_id: str = "kueue-x64") -> Any:
    """Build a KueueBackend bound to ``bucket_ids`` via a real registry (MKUE-02 dispatch picks a bucket).

    ``KueueBackend.dispatch`` now resolves the D-06 bucket via ``pick_bucket`` over ``self.config.buckets``
    and ``s3_staging.resolve_bucket_config`` over ``get_settings().buckets`` -- so the backend must carry a
    real ``config`` (its bound bucket id-list) AND the process registry must resolve those ids. This helper
    writes a one-kueue backends.toml (via the shared conftest fixture, which points the env + clears the
    get_settings cache) and returns the resolved ``KueueBackend`` whose ``self.config`` is that entry.
    """
    from phaze.config import ControlSettings

    id_array = ", ".join(f'"{bid}"' for bid in bucket_ids)
    bucket_blocks = "".join(
        f"""
        [[buckets]]
        id = "{bid}"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-{bid}"
        """
        for bid in bucket_ids
    )
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "{backend_id}"
        rank = 20
        cap = 5
        buckets = [{id_array}]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"
        {bucket_blocks}
        """
    )
    settings = ControlSettings()
    [backend] = [b for b in backends.resolve_backends(settings) if b.id == backend_id]
    return backend


def _make_file(*, state: str = FileState.AWAITING_CLOUD, file_type: str = "mp3") -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=state,
    )


async def _seed_cloud_job(session: AsyncSession, *, backend_id: str | None, status: CloudJobStatus) -> uuid.UUID:
    """Insert one cloud_job row (with its FK file) at ``status``; return the file id."""
    file = _make_file(state=FileState.PUSHING)
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=backend_id,
            s3_key=None if backend_id and "kueue" not in backend_id else f"staging/{file.id}",
            status=status.value,
        )
    )
    await session.commit()
    return file.id


def _stub_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_staging, "create_multipart_upload", AsyncMock(return_value="upload-xyz"))
    monkeypatch.setattr(s3_staging, "presign_upload_parts", AsyncMock(return_value=["https://s3.test/part?1"]))


def _stub_kube_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kube_staging, "get_local_queue", AsyncMock(return_value=fake_local_queue()))


async def _seed_agent_row(
    session: AsyncSession,
    *,
    agent_id: str,
    name: str | None = None,
    kind: str = "compute",
    online: bool = True,
    revoked: bool = False,
) -> Agent:
    """Insert one Agent row with explicit id / name / liveness so binding-key edge cases are seedable.

    ``seed_active_agent`` always sets ``name == agent_id`` and always-online, so it cannot express the
    name-only-match / revoked / never-seen fixtures the D-01 selector must reject. This helper does.
    """
    now = datetime.now(UTC)
    agent = Agent(
        id=agent_id,
        name=name if name is not None else agent_id,
        token_hash=None,
        kind=kind,
        scan_roots=[],
        last_seen_at=now if online else None,
        revoked_at=(now - timedelta(hours=1)) if revoked else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


# === select_agent_by_id (per-entry binding, D-01) ========================================


@pytest.mark.asyncio
async def test_select_agent_by_id_returns_agent_matched_on_id(session: AsyncSession) -> None:
    """D-01: select_agent_by_id resolves the online agent whose Agent.id equals the arg."""
    from phaze.services.enqueue_router import select_agent_by_id

    await _seed_agent_row(session, agent_id="oci-a1", kind="compute")
    agent = await select_agent_by_id(session, "oci-a1", kind="compute")
    assert agent.id == "oci-a1"


@pytest.mark.asyncio
async def test_select_agent_by_id_matches_id_only_never_name(session: AsyncSession) -> None:
    """D-01 (no fallback): an agent whose NAME (not id) equals the arg does NOT match -> raises."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    # id="oci-real", name="oci-a1" -- the arg "oci-a1" collides with the free-form NAME only.
    await _seed_agent_row(session, agent_id="oci-real", name="oci-a1", kind="compute")
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_revoked_agent_raises(session: AsyncSession) -> None:
    """A matching-id agent that is revoked (revoked_at set) does NOT match -> raises (liveness filter)."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    await _seed_agent_row(session, agent_id="oci-a1", kind="compute", revoked=True)
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_never_seen_agent_raises(session: AsyncSession) -> None:
    """A matching-id agent that never checked in (last_seen_at NULL) does NOT match -> raises."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    await _seed_agent_row(session, agent_id="oci-a1", kind="compute", online=False)
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_absent_agent_raises(session: AsyncSession) -> None:
    """An id with NO matching agent raises NoActiveAgentError (the degrade-to-hold signal D-05 consumes)."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "nope", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_honors_kind_filter(session: AsyncSession) -> None:
    """When kind is given, a same-id agent of a different kind does not cross-match -> raises."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    # A fileserver agent with the same id must NOT satisfy a kind="compute" lookup.
    await _seed_agent_row(session, agent_id="oci-a1", kind="fileserver")
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


# === is_available (3 impls) ==============================================================


@pytest.mark.asyncio
async def test_local_is_available_always_true(session: AsyncSession) -> None:
    """LocalBackend.is_available is unconditionally True -- local dispatch needs no remote agent."""
    assert await _local().is_available(session) is True


@pytest.mark.asyncio
async def test_compute_is_available_true_when_bound_agent_online(session: AsyncSession) -> None:
    """D-02: is_available is True when the bound ``agent_ref`` names an ONLINE compute agent (Agent.id)."""
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    # Bind the backend to THIS agent's id -- the per-entry reference, not "the single active compute agent".
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is True


@pytest.mark.asyncio
async def test_compute_is_available_false_when_bound_agent_absent(session: AsyncSession) -> None:
    """D-05: bound agent absent / not-yet-registered -> is_available False, NEVER raises (degrade-to-hold)."""
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is False


@pytest.mark.asyncio
async def test_compute_is_available_false_when_online_agent_id_mismatches_ref(session: AsyncSession) -> None:
    """D-02 behavior change: a compute agent is online but its id != agent_ref -> False (not the retired pick).

    The intended change vs the retired ``select_active_agent(kind="compute")`` single-active pick: a
    DIFFERENT online compute agent no longer satisfies THIS backend's binding. Only the specifically-bound
    node counts.
    """
    await seed_active_agent(session, agent_id="some-other-compute", kind="compute")
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is False


@pytest.mark.asyncio
async def test_compute_is_available_reads_bound_ref_not_single_active_pick(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-02 record-don't-rederive: is_available resolves the bound ref and does NOT call select_active_agent."""
    import phaze.services.backends as backends_mod

    sentinel = AsyncMock(side_effect=AssertionError("is_available must not use the single-active pick"))
    monkeypatch.setattr(backends_mod, "select_active_agent", sentinel)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is True
    sentinel.assert_not_awaited()


@pytest.mark.asyncio
async def test_compute_is_available_fails_loud_when_no_agent_ref_bound(session: AsyncSession) -> None:
    """A defensively-unbound compute backend (no agent_ref) fails loud via the accessor, mirroring _kube()."""
    # config=None -> the accessor has nothing to resolve -> a clear raise (NOT a silent False).
    backend = _compute(id="compute-a1", config=None)
    with pytest.raises(ValueError, match="compute-a1"):
        await backend.is_available(session)


@pytest.mark.asyncio
async def test_kueue_is_available_probes_kube_with_no_compute_dependency(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """KueueBackend.is_available probes the LocalQueue and has NO compute-agent dependency (D-01a)."""
    _stub_kube_available(monkeypatch)
    # Deliberately NO compute agent online -- kueue must still report available.
    assert await _kueue().is_available(session) is True


@pytest.mark.asyncio
async def test_kueue_is_available_false_on_probe_error_never_raises(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A kube probe failure degrades to False, never propagates (returns bool, never raises)."""
    monkeypatch.setattr(kube_staging, "get_local_queue", AsyncMock(side_effect=RuntimeError("kube down")))
    assert await _kueue().is_available(session) is False


# === in_flight_count (3 impls, D-10 status set) ==========================================


@pytest.mark.asyncio
async def test_local_in_flight_count_is_zero(session: AsyncSession) -> None:
    """LocalBackend has no cloud_job rows -> in_flight_count is always 0."""
    assert await _local().in_flight_count(session) == 0


@pytest.mark.asyncio
async def test_compute_in_flight_count_filters_by_backend_id_and_status(session: AsyncSession) -> None:
    """Compute in_flight_count counts only its own backend_id rows in the D-10 in-flight set."""
    backend = _compute(id="compute-a1")
    for status in IN_FLIGHT_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)
    for status in TERMINAL_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)  # excluded (terminal)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.RUNNING)  # other backend
    assert await backend.in_flight_count(session) == len(IN_FLIGHT_STATUSES)


@pytest.mark.asyncio
async def test_kueue_in_flight_count_filters_by_backend_id(session: AsyncSession) -> None:
    """Kueue in_flight_count counts only its own backend_id rows in the in-flight set."""
    backend = _kueue(id="kueue-x64")
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    await _seed_cloud_job(session, backend_id="compute-a1", status=CloudJobStatus.RUNNING)  # other backend
    assert await backend.in_flight_count(session) == 2


# === dispatch (3 impls; D-03 atomicity) ==================================================


@pytest.mark.asyncio
async def test_compute_dispatch_flips_pushing_and_writes_cloud_job_in_txn(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-03: compute dispatch flips PUSHING AND upserts a non-terminal cloud_job in the SAME session.

    The row must be visible (via autoflush) within the uncommitted transaction -- there is never a
    committed in-flight FileState without a live cloud_job row (Pitfall 4 limbo guard).
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1")
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    assert file.state == FileState.PUSHING
    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.backend_id == "compute-a1"
    assert job.status not in {s.value for s in TERMINAL_STATUSES}


@pytest.mark.asyncio
async def test_kueue_dispatch_stages_s3_and_upserts_uploading(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any) -> None:
    """Kueue dispatch runs the no-commit S3 core: cloud_job UPLOADING + s3_upload enqueue, no commit."""
    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file = _make_file(state=FileState.PUSHING, file_type="flac")
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert [t for t, _ in router.queues["nox"].captured] == ["s3_upload"]


@pytest.mark.asyncio
async def test_kueue_dispatch_records_picked_staging_bucket_and_backend_id(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """MKUE-02/D-06: dispatch stamps staging_bucket == pick_bucket(file.id, sorted(config.buckets)) + backend_id.

    Over an N=2-bucket backend the recorded bucket is EXACTLY the deterministic pick over the sorted
    bound set, and backend_id is this backend's id -- both written in the same uncommitted session.
    """
    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    bucket_ids = ["staging-b", "staging-a"]  # unsorted on purpose -- pick_bucket sorts internally
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=bucket_ids, backend_id="kueue-x64")
    file = _make_file(state=FileState.PUSHING, file_type="flac")
    session.add(file)
    await session.commit()

    await backend.dispatch(file, session, DedupFakeTaskRouter())

    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.backend_id == "kueue-x64"
    assert job.staging_bucket == s3_staging.pick_bucket(file.id, bucket_ids)  # authoritative D-06 pick


@pytest.mark.asyncio
async def test_kueue_dispatch_bucket_is_deterministic_per_file(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """D-06: the same file always lands on the same bucket; two files may land on different buckets.

    Determinism is proven by re-staging the SAME file (idempotent FK upsert) -- the recorded bucket is
    stable -- and by the pure ``pick_bucket`` mapping two distinct ids into the 2-bucket set (at least one
    of many random files lands on each member, so the set is genuinely partitioned, not collapsed to one).
    """
    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    bucket_ids = ["staging-a", "staging-b"]
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=bucket_ids, backend_id="kueue-x64")
    file = _make_file(state=FileState.PUSHING, file_type="flac")
    session.add(file)
    await session.commit()

    from sqlalchemy import select

    await backend.dispatch(file, session, DedupFakeTaskRouter())
    first = (await session.execute(select(CloudJob.staging_bucket).where(CloudJob.file_id == file.id))).scalar_one()
    await backend.dispatch(file, session, DedupFakeTaskRouter())  # idempotent re-stage
    second = (await session.execute(select(CloudJob.staging_bucket).where(CloudJob.file_id == file.id))).scalar_one()
    assert first == second == s3_staging.pick_bucket(file.id, bucket_ids)  # same file -> same bucket

    # The 2-bucket set is genuinely partitioned across many files (not collapsed to a single member).
    landed = {s3_staging.pick_bucket(uuid.uuid4(), bucket_ids) for _ in range(200)}
    assert landed == set(bucket_ids)


@pytest.mark.asyncio
async def test_kueue_dispatch_no_fileserver_agent_leaves_file_untouched(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """CR-01 (gate-before-mutate): no fileserver agent -> dispatch raises, file stays AWAITING_CLOUD, no cloud_job.

    Regression for the pre-fix limbo bug: KueueBackend.dispatch used to flip ``file.state = PUSHING``
    UNCONDITIONALLY as its first statement, before ``_stage_file_to_s3`` gated on the fileserver agent.
    Under SQLAlchemy autoflush that pending PUSHING change was flushed as a side effect of the gate's
    SELECT, so a ``NoActiveAgentError`` (then a break-without-rollback in the drain) committed a PUSHING
    file with NO ``cloud_job`` row -- exactly the Pitfall-4 limbo the module docstring forbids. Post-fix
    the flip lands only AFTER ``_stage_file_to_s3`` returns, so the raising path is mutation-free: the
    file stays AWAITING_CLOUD and no cloud_job row exists even after the drain's post-loop commit.
    """
    from sqlalchemy import select

    from phaze.services.enqueue_router import NoActiveAgentError

    _stub_s3(monkeypatch)  # unreached: the fileserver gate raises before any S3 call
    # Deliberately NO fileserver agent seeded.
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file = _make_file(state=FileState.AWAITING_CLOUD, file_type="flac")
    session.add(file)
    await session.commit()
    file_id = file.id  # capture before expire_all() so the re-read query builds without a lazy load

    with pytest.raises(NoActiveAgentError):
        await backend.dispatch(file, session, DedupFakeTaskRouter())

    # Emulate the drain's single post-loop commit + a fresh DB read: no PUSHING flip may survive.
    await session.commit()
    session.expire_all()
    refreshed = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    assert refreshed.state == FileState.AWAITING_CLOUD
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    assert job is None


@pytest.mark.asyncio
async def test_local_dispatch_writes_no_cloud_job_row(session: AsyncSession) -> None:
    """LocalBackend.dispatch stays on the local process_file path -- it writes no cloud_job row."""
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    from sqlalchemy import func, select

    count = int((await session.execute(select(func.count(CloudJob.id)).where(CloudJob.file_id == file.id))).scalar() or 0)
    assert count == 0


# === CR-01 (SCHED-01/03): LocalBackend.dispatch removes the file from the AWAITING_CLOUD set =====


@pytest.mark.asyncio
async def test_local_dispatch_flips_to_local_analyzing(session: AsyncSession) -> None:
    """CR-01: LocalBackend.dispatch flips an AWAITING_CLOUD file to LOCAL_ANALYZING in the caller session.

    Mirrors the compute/kueue ``FileState -> PUSHING`` flip (backends.py:252/:316): a locally-spilled
    file must leave the ``AWAITING_CLOUD`` candidate predicate atomically so it is not re-selected on a
    later drain tick and double-dispatched to a cloud backend while its ``process_file`` is in flight.
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file(state=FileState.AWAITING_CLOUD)
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    assert file.state == FileState.LOCAL_ANALYZING


@pytest.mark.asyncio
async def test_local_dispatch_excluded_from_staging_candidates(session: AsyncSession) -> None:
    """CR-01: after a local dispatch the file is absent from ``get_cloud_staging_candidates`` (no re-selection).

    ``get_cloud_staging_candidates`` (pipeline.py) selects ``state == AWAITING_CLOUD``; the state flip
    performed by ``LocalBackend.dispatch`` is exactly what removes the file from that candidate set --
    the missing link the Phase-69 verifier flagged (LocalBackend.dispatch -> get_cloud_staging_candidates).
    """
    from phaze.services.pipeline import get_cloud_staging_candidates

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file(state=FileState.AWAITING_CLOUD)
    session.add(file)
    await session.commit()
    fid = file.id

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)
    await session.commit()

    candidates = await get_cloud_staging_candidates(session, limit=10)
    assert fid not in {c.id for c in candidates}


@pytest.mark.asyncio
async def test_local_dispatch_returns_true_on_enqueue(session: AsyncSession) -> None:
    """WR-01: a genuine ``process_file`` enqueue reports a truthy dispatch (new work staged)."""
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file(state=FileState.AWAITING_CLOUD)
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    assert await backend.dispatch(file, session, router) is True


@pytest.mark.asyncio
async def test_local_dispatch_returns_false_on_dedup_noop(session: AsyncSession) -> None:
    """WR-01: a deterministic-key ``process_file:<id>`` dedup no-op reports False (not newly staged).

    ``enqueue_process_file`` returns ``None`` when SAQ dedups the deterministic key (the file is already
    being analyzed locally); LocalBackend.dispatch must report that as ``False`` so the drain's staged
    tally is honest -- mirroring ``ComputeAgentBackend.dispatch``'s ``return job is not None``.
    """
    from phaze.services.analysis_enqueue import process_file_job_key

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file(state=FileState.AWAITING_CLOUD)
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    # Pre-enqueue the deterministic key on the fileserver's queue so dispatch's enqueue dedups to None.
    live_queue = router.queue_for("nox")
    await live_queue.enqueue("process_file", key=process_file_job_key(file.id))
    router.queue_for_calls.clear()

    assert await backend.dispatch(file, session, router) is False


# === reconcile (3 impls) =================================================================


@pytest.mark.asyncio
async def test_local_reconcile_is_noop(session: AsyncSession) -> None:
    """LocalBackend.reconcile is a no-op (local completion is synchronous, no cron read)."""
    assert await _local().reconcile(session) is None


@pytest.mark.asyncio
async def test_compute_reconcile_is_callback_driven_noop(session: AsyncSession) -> None:
    """Compute terminalization is the /pushed callback path -> reconcile is a no-op cron read."""
    assert await _compute().reconcile(session) is None


@pytest.mark.asyncio
async def test_kueue_reconcile_reads_own_backend_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kueue.reconcile iterates its own {SUBMITTED, RUNNING} cloud_job rows and returns a per-backend tally."""
    _stub_kube_available(monkeypatch)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    # Phase 69 (SCHED-02/05): a backend_id-aware reconcile runs cleanly under the per-row advisory lock and
    # returns its tally (the cron aggregates it) rather than None.
    tally = await _kueue(id="kueue-x64").reconcile(session)
    assert tally is not None
    assert tally["reconciled"] == 1


@pytest.mark.asyncio
async def test_kueue_reconcile_scope_ignores_other_backend_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """SCHED-05: KueueBackend.reconcile touches ONLY its own ``backend_id`` rows; a compute row stays untouched.

    Removing the cron's global un-scoped ``cloud_job`` query means a compute row is owned solely by its
    ``/pushed`` callback. Proven here: a kueue SUBMITTED row is reconciled (tally ``reconciled == 1``)
    while a sibling compute SUBMITTED row's status is byte-untouched by the kueue reconcile pass.
    """
    from sqlalchemy import select

    _stub_kube_available(monkeypatch)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    compute_fid = await _seed_cloud_job(session, backend_id="compute-a1", status=CloudJobStatus.SUBMITTED)

    tally = await _kueue(id="kueue-x64").reconcile(session)

    assert tally is not None
    assert tally["reconciled"] == 1  # only the kueue-scoped row was reconciled
    session.expire_all()
    compute_row = (await session.execute(select(CloudJob).where(CloudJob.file_id == compute_fid))).scalar_one()
    assert compute_row.status == CloudJobStatus.SUBMITTED.value  # the compute row is left for its /pushed callback


# === Layer 2: D-02 equivalence invariant =================================================


@pytest.mark.asyncio
async def test_in_flight_equivalence(session: AsyncSession) -> None:
    """D-02: sum(in_flight_count(b)) == the FileState {PUSHING, PUSHED} window for the single-backend case.

    Construct a set of in-flight cloud_job rows for one compute backend whose files are all in the
    FileState window (PUSHING); the per-backend cloud_job count must equal the FileState-window count.
    A divergence is the Pitfall-1 double/under-count bug. Phase 69 (D-05) retired the global
    ``get_cloud_window_count`` helper, so the FileState window is counted inline here (the invariant the
    Phase-68 substrate proved still holds -- every in-flight row maps to exactly one windowed file).
    """
    from sqlalchemy import func, select

    backend = _compute(id="compute-a1")
    for status in IN_FLIGHT_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)

    resolved = [backend]
    per_backend = sum([await b.in_flight_count(session) for b in resolved])
    window = int(
        (await session.execute(select(func.count(FileRecord.id)).where(FileRecord.state.in_([FileState.PUSHING, FileState.PUSHED])))).scalar() or 0
    )
    assert per_backend == window


# === resolved_non_local_kind: N-Kueue-safe (any-kueue) + compute-only fail-fast ===========


def test_resolved_non_local_kind_returns_compute_for_multiple_compute_only(backends_toml_env: Any) -> None:
    """The compute-only ``>1`` fail-fast is RETIRED (D-03): two COMPUTE backends (no kueue) return "compute".

    Phase 70 (MKUE-01) generalized ``resolved_non_local_kind`` to tolerate N Kueue backends; Phase 72
    (MCOMP-01, D-03) generalizes the compute-only branch the same way -- N compute backends resolve to
    "compute" with NO raise (per-agent dispatch attribution lands in Phase 73). The discretion
    confirmation that the compute-only branch still yields "compute" for N compute.
    """
    from phaze.config import ControlSettings

    backends_toml_env(
        """
        [[backends]]
        kind = "compute"
        id = "compute-a"
        rank = 10
        cap = 2
        agent_ref = "agent-a"
        scratch_dir = "/scratch/a"

        [[backends]]
        kind = "compute"
        id = "compute-b"
        rank = 20
        cap = 2
        agent_ref = "agent-b"
        scratch_dir = "/scratch/b"
        """
    )
    settings = ControlSettings()
    assert settings.cloud_enabled is True
    # D-03: the compute-only >1 fail-fast is retired; N compute resolves to "compute" without raising.
    assert backends.resolved_non_local_kind(settings) == "compute"


_LOCAL_2KUEUE_HEAD = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "kueue"
    id = "kueue-a"
    rank = 10
    cap = 4
    buckets = ["bkt-a"]

    [backends.kube]
    api_url = "https://kube-a.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-a"

    [[backends]]
    kind = "kueue"
    id = "kueue-b"
    rank = 20
    cap = 4
    buckets = ["bkt-b"]

    [backends.kube]
    api_url = "https://kube-b.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-b"
"""

_TWO_BUCKETS = """
    [[buckets]]
    id = "bkt-a"
    scope = "cluster-specific"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-a"

    [[buckets]]
    id = "bkt-b"
    scope = "cluster-specific"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-b"
"""


def test_resolved_non_local_kind_returns_kueue_for_n_kueue(backends_toml_env: Any) -> None:
    """MKUE-01: ANY-kueue registry resolves to "kueue" with NO raise -- the literal N-cluster scenario.

    A local + 2-Kueue registry (the milestone target) previously 500'd every ``resolved_non_local_kind``
    call site (report_uploaded / build_dashboard_context / backfill) via the old blanket ``>1`` raise.
    The generalized helper returns "kueue" (the callers only ask "is the cloud lane kueue"). Adding a
    compute backend to the mix STILL returns "kueue" (any-kueue wins).
    """
    from phaze.config import ControlSettings

    backends_toml_env(_LOCAL_2KUEUE_HEAD + _TWO_BUCKETS)
    settings = ControlSettings()
    assert backends.resolved_non_local_kind(settings) == "kueue"

    compute_block = """
    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 30
    cap = 2
    agent_ref = "compute-agent-01"
    scratch_dir = "/srv/scratch"
"""
    backends_toml_env(_LOCAL_2KUEUE_HEAD + compute_block + _TWO_BUCKETS)
    settings = ControlSettings()
    assert backends.resolved_non_local_kind(settings) == "kueue"


# === SCHED-01: resolve_backends supports N non-local backends (Phase-69 guard removal) ====


def test_resolve_backends_returns_all_non_local(backends_toml_env: Any) -> None:
    """SCHED-01: a registry of 2+ non-local backends resolves to a list of that length -- no ValueError.

    Phase 69 removed the Phase-68 ``>1``-non-local boot guard from :func:`resolve_backends` (multi-backend
    simultaneous dispatch is exactly this phase's job). The registry must now resolve cleanly to N
    ``Backend`` impls so the tiered drain can snapshot + route across all of them. The single-kind
    fail-fast survives only in :func:`resolved_non_local_kind` (asserted above), never here.
    """
    from phaze.config import ControlSettings

    backends_toml_env(
        """
        [[backends]]
        kind = "compute"
        id = "compute-a"
        rank = 10
        cap = 2
        agent_ref = "agent-a"
        scratch_dir = "/scratch/a"

        [[backends]]
        kind = "compute"
        id = "compute-b"
        rank = 20
        cap = 3
        agent_ref = "agent-b"
        scratch_dir = "/scratch/b"

        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 4
        """
    )
    settings = ControlSettings()
    resolved = backends.resolve_backends(settings)

    # All three entries resolve (2 non-local + 1 local) -- no ValueError on the 2 non-local backends.
    assert len(resolved) == 3
    non_local = [b for b in resolved if not isinstance(b, backends.LocalBackend)]
    assert len(non_local) == 2
    assert {b.id for b in non_local} == {"compute-a", "compute-b"}


# === Pitfall 1: active_compute_scratch_dir on a single-compute reduction ==================


def test_active_compute_scratch_dir_resolves_under_local_2kueue_1compute(backends_toml_env: Any) -> None:
    """Pitfall 1: local + 2 Kueue + 1 compute resolves the compute scratch_dir -- no >1-non-local raise.

    Before Phase 70 ``active_compute_scratch_dir`` reduced through ``_single_non_local`` which raised
    the moment ≥2 non-local backends coexisted, 500ing the ``/pushed`` callback. Re-based on a
    single-COMPUTE reduction, the milestone's target deploy resolves cleanly to the sole compute
    backend's scratch_dir.
    """
    from phaze.config import ControlSettings

    compute_block = """
    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 30
    cap = 2
    agent_ref = "compute-agent-01"
    scratch_dir = "/srv/scratch"
"""
    backends_toml_env(_LOCAL_2KUEUE_HEAD + compute_block + _TWO_BUCKETS)
    settings = ControlSettings()
    assert settings.active_compute_scratch_dir == "/srv/scratch"
