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
* ``dispatch`` D-03 atomicity -- the ``cloud_job`` upsert lands in the caller-passed session, so there
  is never a committed in-flight marker without a live
  non-terminal ``cloud_job`` row (no limbo row).
* ``reconcile`` -- Kueue cron read; Local/Compute callback-driven (no-op in the unit cells).

Layer 2 (D-02): ``sum(in_flight_count(b) for b in backends)`` equals the derived in-flight window
count for the single-backend case, over constructed ``cloud_job`` states
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
from phaze.models.file import FileRecord
from phaze.services import kube_staging, s3_staging
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent
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
            push_host=kw.get("push_host", f"{bid}.push.example"),
            ssh_user=kw.get("ssh_user"),
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


def _make_file(*, file_type: str = "mp3") -> FileRecord:
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


async def _seed_cloud_job(session: AsyncSession, *, backend_id: str | None, status: CloudJobStatus) -> uuid.UUID:
    """Insert one cloud_job row (with its FK file) at ``status``; return the file id."""
    file = _make_file()
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


class _RaisingQueue(DedupFakeQueue):
    """A queue whose ``enqueue`` always raises -- models SAQ's ``PostgresQueue`` (its OWN psycopg
    pool, phaze-uciu.3) blowing up AFTER ``dispatch``/``_stage_file_to_s3`` has already upserted the
    ``cloud_job`` row in THIS test's asyncpg session. ``connect()`` (inherited) still succeeds, so the
    raise fires exactly where the real enqueue failure fires.
    """

    async def enqueue(self, task_name: str, **kwargs: Any) -> Any:  # noqa: ARG002 -- fake signature parity
        raise RuntimeError("saq enqueue blew up")


class _RaisingTaskRouter:
    """A task router whose every ``queue_for`` hands back a :class:`_RaisingQueue`."""

    def __init__(self) -> None:
        self.queue_for_calls: list[str] = []

    def queue_for(self, agent_id: str, lane: str | None = None) -> _RaisingQueue:  # noqa: ARG002 -- fake signature parity
        self.queue_for_calls.append(agent_id)
        return _RaisingQueue(f"raising-{agent_id}")


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


@pytest.mark.asyncio
async def test_select_agent_by_id_treats_sql_metacharacters_as_a_literal_value(session: AsyncSession) -> None:
    """D-01: an ``agent_id`` shaped like a SQL-injection payload is bound as a literal, never executed.

    The docstring's "parameterized query" claim has no dedicated adversarial cell elsewhere in this
    suite -- every existing D-01 test passes an ordinary slug. This cell feeds a classic
    tautology/statement-injection payload as the ``agent_id`` argument and proves TWO things a
    string-interpolated (unparameterized) query would fail: (1) the lookup raises
    ``NoActiveAgentError`` -- the payload matches no row rather than short-circuiting a tautology like
    ``OR '1'='1'`` into matching every row -- and (2) a genuine, unrelated agent seeded in the SAME
    session survives the call untouched (a `; DROP TABLE agents; --`-shaped value never reaches the
    database as executable SQL).
    """
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    survivor = await _seed_agent_row(session, agent_id="oci-real", kind="compute")
    payload = "oci-real' OR '1'='1'; DROP TABLE agents; --"

    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, payload, kind="compute")

    # The unrelated legitimate agent must still resolve -- proof no injected statement executed.
    resolved = await select_agent_by_id(session, survivor.id, kind="compute")
    assert resolved.id == "oci-real"


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
async def test_mcomp02_two_compute_backends_only_the_online_bound_agent_is_available(session: AsyncSession) -> None:
    """MCOMP-02 (per-agent liveness): N compute backends -> an offline bound agent makes ONLY that lane unavailable.

    A local + 2-compute deploy where compute-a's bound agent (``cloud-a``) is ONLINE and compute-b's bound
    agent (``cloud-b``) is OFFLINE (never registered). Per-entry gating (Phase 72 ``is_available`` resolves
    ``self.config.agent_ref`` against ``Agent.id``, NOT a single-active pick) must report backend-a available
    and backend-b UNAVAILABLE -- so the drain snapshot leaves the healthy compute lane eligible while the
    offline one contributes 0 slots. This is the N-compute twin of the single-active liveness the retired
    ``select_active_agent(kind="compute")`` pick could not express.
    """
    # Only compute-a's bound node is online; compute-b's is absent.
    await seed_active_agent(session, agent_id="cloud-a", kind="compute")

    backend_a = _compute(id="compute-a", agent_ref="cloud-a")
    backend_b = _compute(id="compute-b", agent_ref="cloud-b")

    assert await backend_a.is_available(session) is True
    assert await backend_b.is_available(session) is False


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
async def test_compute_dispatch_writes_cloud_job_in_txn(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-03: compute dispatch upserts a non-terminal cloud_job in the SAME session.

    Phase 90 (D-09): the paired PUSHING files.state flip was removed; the cloud_job row is the sole
    in-flight authority. The row must be visible (via autoflush) within the uncommitted transaction --
    there is never a committed in-flight dispatch without a live cloud_job row (Pitfall 4 limbo guard).
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1")
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.backend_id == "compute-a1"
    assert job.status not in {s.value for s in TERMINAL_STATUSES}


@pytest.mark.asyncio
async def test_compute_dispatch_stamps_destination_on_push_payload(session: AsyncSession) -> None:
    """D-02: dispatch stamps dest_host/dest_scratch_dir/dest_ssh_user off self.config onto the push_file payload.

    Record-don't-rederive originates here: the enqueued push carries THIS backend's own push_host /
    scratch_dir / ssh_user (read off the bound ComputeBackend), so every downstream reader (the Plan-02
    rsync argv) uses the RECORDED destination rather than re-deriving it.
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1", scratch_dir="/srv/scratch", push_host="a1.push.example", ssh_user="phaze")
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    pushes = [(task, payload) for task, payload in router.queues["nox-io"].captured if task == "push_file"]
    assert len(pushes) == 1
    _task, payload = pushes[0]
    assert payload["dest_host"] == "a1.push.example"
    assert payload["dest_scratch_dir"] == "/srv/scratch"
    assert payload["dest_ssh_user"] == "phaze"


@pytest.mark.asyncio
async def test_compute_dispatch_stamps_none_ssh_user_when_unset(session: AsyncSession) -> None:
    """dest_ssh_user is None on the push payload when the backend omits ssh_user (D-01 optional)."""
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1", scratch_dir="/srv/scratch", push_host="a1.push.example")
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    _task, payload = next((t, p) for t, p in router.queues["nox-io"].captured if t == "push_file")
    assert payload["dest_host"] == "a1.push.example"
    assert payload["dest_ssh_user"] is None


@pytest.mark.asyncio
async def test_kueue_dispatch_stages_s3_and_upserts_uploading(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any) -> None:
    """Kueue dispatch runs the no-commit S3 core: cloud_job UPLOADING + s3_upload enqueue, no commit."""
    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    assert [t for t, _ in router.queues["nox-io"].captured] == ["s3_upload"]


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
    file = _make_file(file_type="flac")
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
    file = _make_file(file_type="flac")
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
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()
    file_id = file.id  # capture before expire_all() so the re-read query builds without a lazy load

    with pytest.raises(NoActiveAgentError):
        await backend.dispatch(file, session, DedupFakeTaskRouter())

    # Emulate the drain's single post-loop commit + a fresh DB read: no PUSHING flip may survive.
    await session.commit()
    session.expire_all()
    # Post-MIG-04 the atomicity guarantee is purely about the sidecar: a failed dispatch leaves NO cloud_job row.
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    assert job is None


# === phaze-uciu.3: a POST-WRITE enqueue raise rolls back ONLY the write (SAVEPOINT) =====================


@pytest.mark.asyncio
async def test_compute_dispatch_enqueue_failure_rolls_back_write_via_savepoint(session: AsyncSession) -> None:
    """A ``push_file`` enqueue failure AFTER the ``cloud_job`` upsert leaves the row re-pickable.

    Regression for phaze-uciu.3 (supersedes phaze-3e1i): before the fix, ``dispatch`` upserted
    ``status='submitted'`` + ``backend_id`` BEFORE the fallible enqueue with NO savepoint -- SAQ's
    ``PostgresQueue`` uses its own psycopg pool, so an enqueue raise does NOT poison this asyncpg
    session/transaction, and the drain's per-candidate handler deliberately does not roll back
    (Landmine L1: a mid-loop rollback would drop the tick's ``pg_advisory_xact_lock``). The un-savepointed
    upsert therefore SURVIVED into the drain's post-loop commit -- a stranded ``submitted`` row that
    reconcile/orphan-recovery both scope away from, permanently consuming an ``in_flight_count`` slot.
    Post-fix the upsert + enqueue run inside ``session.begin_nested()``: the raise still propagates (the
    caller sees it), but the SAVEPOINT rolls back ONLY the upsert, restoring the pre-dispatch
    ``status='awaiting'`` row -- re-pickable by the next tick, and NOT counted by ``in_flight_count``.
    """
    from sqlalchemy import select

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _compute(id="compute-a1")
    file = _make_file()
    session.add(file)
    await session.flush()
    # The real precondition: an AWAITING_CLOUD file already carries an ``awaiting`` cloud_job sidecar
    # row before the drain ever calls dispatch (Phase 77, D-04).
    await backends.hold_awaiting_cloud(session, file)
    await session.commit()

    router = _RaisingTaskRouter()
    with pytest.raises(RuntimeError, match="saq enqueue blew up"):
        await backend.dispatch(file, session, router)

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert job.status == CloudJobStatus.AWAITING.value  # rolled back to the pre-dispatch hold status
    assert job.backend_id is None  # the SAVEPOINT rollback also undid the backend_id stamp
    assert await backend.in_flight_count(session) == 0  # 'awaiting' is never in the in-flight set (D-10)


@pytest.mark.asyncio
async def test_kueue_dispatch_enqueue_failure_rolls_back_write_via_savepoint(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """An ``s3_upload`` enqueue failure AFTER the ``cloud_job`` upsert leaves the row re-pickable.

    The KueueBackend/``cloud_staging._stage_file_to_s3`` twin of
    :func:`test_compute_dispatch_enqueue_failure_rolls_back_write_via_savepoint`: pre-fix, the S3-staging
    core upserted ``status='uploading'`` (+ ``s3_key``/``upload_id``/``staging_bucket``) BEFORE the
    fallible ``s3_upload`` enqueue with no savepoint, so a raising enqueue left the row stranded. Post-fix
    the upsert + enqueue run inside ``session.begin_nested()`` (``cloud_staging.py``), so the raise rolls
    back ONLY that upsert -- restoring ``status='awaiting'`` -- and ``KueueBackend.dispatch``'s trailing
    ``backend_id``/``staging_bucket`` write never runs (the SAVEPOINT raise short-circuits ``dispatch``
    before that statement).
    """
    from sqlalchemy import select

    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.flush()
    await backends.hold_awaiting_cloud(session, file)
    await session.commit()

    router = _RaisingTaskRouter()
    with pytest.raises(RuntimeError, match="saq enqueue blew up"):
        await backend.dispatch(file, session, router)

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert job.status == CloudJobStatus.AWAITING.value
    assert job.backend_id is None
    assert job.staging_bucket is None
    assert job.s3_key is None
    assert await backend.in_flight_count(session) == 0


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
async def test_local_dispatch_writes_no_state_and_no_cloud_job(session: AsyncSession) -> None:
    """CR-01 / Phase 90 (D-09): LocalBackend.dispatch no longer writes files.state and creates no cloud_job.

    The former LOCAL_ANALYZING flip was removed; a locally-spilled file leaves the AWAITING_CLOUD
    candidate set via its ``process_file:<id>`` scheduling-ledger row (proven in
    test_local_dispatch_excluded_from_staging_candidates), NOT a state write.
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    # Phase 90 (D-09): files.state is left untouched, and local dispatch writes no cloud_job row.
    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is None


@pytest.mark.asyncio
async def test_local_dispatch_excluded_from_staging_candidates(session: AsyncSession) -> None:
    """CR-01 / D-05: after a local dispatch the file is excluded from ``get_cloud_staging_candidates``.

    Post Phase-83 the drain no longer reads ``FileRecord.state`` (SC#1): a candidate must carry a
    ``cloud_job(status='awaiting')`` row (INNER join) AND not be analyze-in-flight. ``LocalBackend.dispatch``
    writes NO cloud_job row and deletes none (D-05 rejects deletion -- the awaiting row is retained); the
    committed ``process_file:<id>`` ledger row (the ``before_enqueue`` hook's own write, seeded here since
    the DedupFakeQueue does not run that hook) is what makes ``~inflight_clause(ANALYZE)`` exclude the file.
    """
    from sqlalchemy import select

    from phaze.models.scheduling_ledger import SchedulingLedger
    from phaze.services.pipeline import get_cloud_staging_candidates

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    session.add(CloudJob(id=uuid.uuid4(), file_id=fid, status=CloudJobStatus.AWAITING.value))
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)
    session.add(SchedulingLedger(key=f"process_file:{fid}", function="process_file", routing="agent", payload={"file_id": str(fid)}))
    await session.commit()

    candidates = await get_cloud_staging_candidates(session, limit=10)
    assert fid not in {f.id for f, _ in candidates}  # excluded by ~inflight_clause(ANALYZE)
    # D-05: the awaiting row is retained (the conjunct excludes; it does not delete the row).
    retained = (await session.execute(select(CloudJob.status).where(CloudJob.file_id == fid))).scalar_one()
    assert retained == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_local_dispatch_returns_true_on_enqueue(session: AsyncSession) -> None:
    """WR-01: a genuine ``process_file`` enqueue reports a truthy dispatch (new work staged)."""
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _local()
    file = _make_file()
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
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    # Pre-enqueue the deterministic key on the fileserver's queue so dispatch's enqueue dedups to None.
    live_queue = router.queue_for("nox", "analyze")
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
    """D-02: sum(in_flight_count(b)) == the derived in-flight window for the single-backend case.

    Construct a set of in-flight cloud_job rows for one compute backend; the per-backend cloud_job
    count must equal the number of distinct files carrying an in-flight cloud_job row. Post-MIG-04
    the window derives ONLY from the ``cloud_job`` sidecar (there is no scalar ``{PUSHING, PUSHED}``
    state to count). A divergence is the Pitfall-1 double/under-count bug -- every in-flight row maps
    to exactly one windowed file.
    """
    from sqlalchemy import func, select

    backend = _compute(id="compute-a1")
    for status in IN_FLIGHT_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)

    resolved = [backend]
    per_backend = sum([await b.in_flight_count(session) for b in resolved])
    window = int(
        (
            await session.execute(
                select(func.count(func.distinct(CloudJob.file_id))).where(CloudJob.status.in_([s.value for s in IN_FLIGHT_STATUSES]))
            )
        ).scalar()
        or 0
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
        push_host = "a.push"

        [[backends]]
        kind = "compute"
        id = "compute-b"
        rank = 20
        cap = 2
        agent_ref = "agent-b"
        scratch_dir = "/scratch/b"
        push_host = "b.push"
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
    push_host = "oci-a1.push.example"
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
        push_host = "a.push"

        [[backends]]
        kind = "compute"
        id = "compute-b"
        rank = 20
        cap = 3
        agent_ref = "agent-b"
        scratch_dir = "/scratch/b"
        push_host = "b.push"

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


# === D-06: resolve_compute_backend inverse-lookup (backend_id -> ComputeBackend) ==========


def test_resolve_compute_backend(backends_toml_env: Any) -> None:
    """D-06: the authoritative inverse-lookup returns the compute entry by id; None for miss/non-compute.

    resolve_compute_backend(cfg, None) -> None; an unknown id -> None; a real compute id -> that
    ComputeBackend; a kueue/local id -> None (only kind==compute entries are considered). Every
    downstream scratch/terminalization reader resolves a recorded cloud_job.backend_id through this.
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
    push_host = "oci-a1.push.example"
"""
    backends_toml_env(_LOCAL_2KUEUE_HEAD + compute_block + _TWO_BUCKETS)
    settings = ControlSettings()

    assert backends.resolve_compute_backend(settings, None) is None
    assert backends.resolve_compute_backend(settings, "does-not-exist") is None
    hit = backends.resolve_compute_backend(settings, "oci-a1")
    assert hit is not None
    assert hit.id == "oci-a1"
    assert hit.kind == "compute"
    assert hit.push_host == "oci-a1.push.example"
    # A kueue id and the local id are NOT compute entries -> None (kind-filtered).
    assert backends.resolve_compute_backend(settings, "kueue-a") is None
    assert backends.resolve_compute_backend(settings, "local") is None


# === MCOMP-03: per-file compute scratch resolution under local + 2 Kueue + 1 compute ======


def test_resolve_compute_backend_scratch_under_local_2kueue_1compute(backends_toml_env: Any) -> None:
    """MCOMP-03: local + 2 Kueue + 1 compute resolves the compute scratch_dir per file -- no global accessor.

    The transitional ``active_compute_scratch_dir`` global was RETIRED in Phase 73: scratch is now
    resolved PER FILE from the recorded ``cloud_job.backend_id`` via ``resolve_compute_backend`` (the
    ``/pushed`` reader was rewired in Plan 03). This pins the milestone's target deploy (≥2 non-local
    backends) resolving cleanly to the sole compute backend's ``scratch_dir`` through the per-file path;
    a kueue id resolves to None (only ``kind == "compute"`` entries are considered).
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
    push_host = "oci-a1.push.example"
"""
    backends_toml_env(_LOCAL_2KUEUE_HEAD + compute_block + _TWO_BUCKETS)
    settings = ControlSettings()
    backend = backends.resolve_compute_backend(settings, "oci-a1")
    assert backend is not None
    assert backend.scratch_dir == "/srv/scratch"
    # A kueue id is not a compute entry -> None (per-file resolution never mis-attributes to a cluster).
    assert backends.resolve_compute_backend(settings, "kueue-a") is None


# === models_pvc_name: optional per-Kueue-backend PVC mount knob (round-trip through TOML) ====


def test_kube_models_pvc_name_round_trips_from_backends_toml(backends_toml_env: Any) -> None:
    """An optional ``models_pvc_name`` in ``[backends.kube]`` parses and round-trips onto the resolved
    backend's KubeConfig (a plain PVC object name -- build_job_manifest mounts it read-only at /models)."""
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    # Baseline: the helper omits models_pvc_name, so the default is None (byte-identical-manifest path).
    assert backend.config.kube.models_pvc_name is None

    from phaze.config import ControlSettings

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
        models_pvc_name = "phaze-essentia-models"

        [[buckets]]
        id = "staging-a"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-staging-a"
        """
    )
    settings = ControlSettings()
    [with_pvc] = [b for b in backends.resolve_backends(settings) if b.id == "kueue-x64"]
    assert with_pvc.config.kube.models_pvc_name == "phaze-essentia-models"


# === hold_awaiting_cloud(): the shared go-forward awaiting writer (D-01/D-02/D-03/D-13) =====


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_fresh_hold_writes_one_awaiting_row(session: AsyncSession) -> None:
    """D-02: a fresh hold inserts exactly one ``awaiting`` cloud_job row.

    Phase 90 (D-09): the paired AWAITING_CLOUD files.state flip was removed; the cloud_job row is the sole
    authority. The row is visible WITHIN the uncommitted caller session (the helper never commits -- the
    caller owns the commit boundary), so the assertions see it without any commit.
    """
    from sqlalchemy import select

    file = _make_file()
    session.add(file)
    await session.flush()

    await backends.hold_awaiting_cloud(session, file)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == CloudJobStatus.AWAITING.value
    assert rows[0].attempts == 0


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_respamps_failed_spill_row_retaining_spent_budget(session: AsyncSession) -> None:
    """D-03: re-stamping a terminalized FAILED row upserts THE SAME row back to ``awaiting`` (no second row).

    ``uq_cloud_job_file_id`` holds one row per file, so the spill path re-stamps via
    ``on_conflict_do_update`` rather than inserting a fresh row. Passing
    ``attempts=cloud_submit_max_attempts`` retains the budget-spent marker ``select_backend`` reads to
    route the file to local.
    """
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.FAILED.value, attempts=max_attempts))
    await session.flush()

    await backends.hold_awaiting_cloud(session, file, attempts=max_attempts)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalars().all()
    assert len(rows) == 1  # uq_cloud_job_file_id -> still one row (re-stamped, not duplicated)
    assert rows[0].status == CloudJobStatus.AWAITING.value
    assert rows[0].attempts == max_attempts


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_hold_branch_returns_true(session: AsyncSession) -> None:
    """D-02: the hold branch (``expect_status is None``) always writes, so it returns ``True``."""
    file = _make_file()
    session.add(file)
    await session.flush()

    result = await backends.hold_awaiting_cloud(session, file)

    assert result is True  # Phase 90 (D-09): the hold writes the cloud_job row (no files.state flip)


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_spill_cas_hit_restamps_clears_phase_and_leaves_state(session: AsyncSession) -> None:
    """Spill branch CAS HIT: an in-flight ``uploading`` row is re-stamped to ``awaiting`` (D-03), ``cloud_phase`` cleared (D-12/WR-01).

    The helper's spill branch does NOT touch ``file.state`` (the caller owns the gated dual-write behind
    the returned bool), so the seeded ``PUSHING`` state is left untouched here.
    """
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.UPLOADING.value, attempts=0, cloud_phase="running"))
    await session.flush()

    result = await backends.hold_awaiting_cloud(
        session,
        file,
        attempts=max_attempts,
        expect_status=(CloudJobStatus.UPLOADING.value, CloudJobStatus.UPLOADED.value),
        clear_cloud_phase=True,
    )

    assert result is True
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert row.status == CloudJobStatus.AWAITING.value
    assert row.attempts == max_attempts
    assert row.cloud_phase is None  # D-12/WR-01: cleared on the s3 spill path


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_spill_cas_miss_is_full_noop(session: AsyncSession) -> None:
    """Spill branch CAS MISS: an already-advanced row (``succeeded``) matches 0 rows -> ``False`` + row UNCHANGED.

    This is the discriminating guard test (SC#2 / T-83-PUSH-CLOBBER): if the spill CAS were replaced by an
    unconditional upsert, this row would be clobbered back to ``awaiting`` and the assertions below would go
    RED. The caller keeps its FULL no-op on a ``False`` return (D-10).
    """
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.SUCCEEDED.value, attempts=1))
    await session.flush()

    result = await backends.hold_awaiting_cloud(session, file, attempts=max_attempts, expect_status=(CloudJobStatus.SUBMITTED.value,))

    assert result is False
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert row.status == CloudJobStatus.SUCCEEDED.value  # UNCHANGED: the CAS matched 0 rows
    assert row.attempts == 1  # attempts NOT bumped -- no unconditional write happened


@pytest.mark.asyncio
async def test_hold_awaiting_cloud_spill_preserves_cloud_phase_when_flag_omitted(session: AsyncSession) -> None:
    """D-12: the spill branch leaves ``cloud_phase`` UNTOUCHED when ``clear_cloud_phase`` is omitted (the push path)."""
    from sqlalchemy import select

    from phaze.config import get_settings

    max_attempts = get_settings().cloud_submit_max_attempts
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, status=CloudJobStatus.SUBMITTED.value, attempts=0, cloud_phase="running"))
    await session.flush()

    result = await backends.hold_awaiting_cloud(session, file, attempts=max_attempts, expect_status=(CloudJobStatus.SUBMITTED.value,))

    assert result is True
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert row.status == CloudJobStatus.AWAITING.value
    assert row.cloud_phase == "running"  # D-12: push spill must NOT touch cloud_phase


def test_awaiting_status_is_not_in_the_in_flight_set() -> None:
    """D-03: ``'awaiting'`` stays OUT of :data:`backends.IN_FLIGHT` so a re-stamped hold never inflates a lane.

    ``in_flight_count`` counts ``status IN IN_FLIGHT``; keeping ``awaiting`` out of that tuple is what lets a
    spill re-stamp (or an inert LocalBackend hold-over row, D-13/D-14) exist without corrupting any backend's
    per-lane in-flight accounting.
    """
    assert CloudJobStatus.AWAITING not in backends.IN_FLIGHT
    assert CloudJobStatus.AWAITING.value not in {status.value for status in backends.IN_FLIGHT}


@pytest.mark.asyncio
async def test_local_dispatch_leaves_awaiting_row_present(session: AsyncSession) -> None:
    """D-13: LocalBackend.dispatch NEITHER writes NOR deletes a held file's inert awaiting cloud_job row.

    A held file carries an ``awaiting`` cloud_job row. LocalBackend stays a no-``cloud_job``-row
    writer/deleter (D-05 chose the drain predicate conjunct over row deletion), so after a local dispatch
    the inert ``awaiting`` row is still present, still ``status='awaiting'`` (it is reaped later by D-14, not
    here). Phase 90 (D-09): the former LOCAL_ANALYZING files.state flip was removed.
    """
    from sqlalchemy import select

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    file = _make_file()
    session.add(file)
    await session.flush()
    await backends.hold_awaiting_cloud(session, file)  # held: awaiting cloud_job row present
    await session.commit()

    backend = _local()
    await backend.dispatch(file, session, DedupFakeTaskRouter())

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None  # LocalBackend did NOT delete the inert awaiting row (D-13)
    assert job.status == CloudJobStatus.AWAITING.value  # nor re-write it
