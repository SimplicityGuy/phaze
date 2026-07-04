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

Layer 2 (D-02): ``sum(in_flight_count(b) for b in backends) == get_cloud_window_count(session)`` for
the single-backend case, over constructed FileState / ``cloud_job`` states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.services import kube_staging, s3_staging
from phaze.services.pipeline import get_cloud_window_count
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
    """Construct a ComputeAgentBackend bound to a single registry entry."""
    return backends.ComputeAgentBackend(id=kw.get("id", "compute-a1"), rank=kw.get("rank", 10), cap=kw.get("cap", 2))


def _kueue(**kw: Any) -> Any:
    """Construct a KueueBackend bound to a single registry entry (single-cluster, D-05)."""
    return backends.KueueBackend(id=kw.get("id", "kueue-x64"), rank=kw.get("rank", 20), cap=kw.get("cap", 5))


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


# === is_available (3 impls) ==============================================================


@pytest.mark.asyncio
async def test_local_is_available_always_true(session: AsyncSession) -> None:
    """LocalBackend.is_available is unconditionally True -- local dispatch needs no remote agent."""
    assert await _local().is_available(session) is True


@pytest.mark.asyncio
async def test_compute_is_available_true_when_agent_online(session: AsyncSession) -> None:
    """ComputeAgentBackend.is_available is True only when a compute agent is online (GATE-1)."""
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    assert await _compute().is_available(session) is True


@pytest.mark.asyncio
async def test_compute_is_available_false_when_agent_absent(session: AsyncSession) -> None:
    """No compute agent -> is_available returns False, NEVER raises (cron no-op discipline)."""
    assert await _compute().is_available(session) is False


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
async def test_kueue_dispatch_stages_s3_and_upserts_uploading(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kueue dispatch runs the no-commit S3 core: cloud_job UPLOADING + s3_upload enqueue, no commit."""
    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _kueue(id="kueue-x64")
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
    """Kueue.reconcile iterates its own {SUBMITTED, RUNNING} cloud_job rows without raising."""
    _stub_kube_available(monkeypatch)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    # A backend_id-aware reconcile must run cleanly (no advisory lock yet -- that is Phase 69).
    assert await _kueue(id="kueue-x64").reconcile(session) is None


# === Layer 2: D-02 equivalence invariant =================================================


@pytest.mark.asyncio
async def test_in_flight_equivalence(session: AsyncSession) -> None:
    """D-02: sum(in_flight_count(b)) == get_cloud_window_count() for the single-backend case.

    Construct a set of in-flight cloud_job rows for one compute backend whose files are all in the
    FileState window (PUSHING); the per-backend cloud_job count must equal the FileState-window count.
    A divergence is the Pitfall-1 double/under-count bug.
    """
    backend = _compute(id="compute-a1")
    for status in IN_FLIGHT_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)

    resolved = [backend]
    per_backend = sum([await b.in_flight_count(session) for b in resolved])
    window = await get_cloud_window_count(session)
    assert per_backend == window


# === WR-01: resolved_non_local_kind fail-fast on >1 non-local ============================


def test_resolved_non_local_kind_raises_on_multiple_non_local(backends_toml_env: Any) -> None:
    """WR-01: >1 non-local backend -> ValueError naming the offending ids, never silently non_local[0].

    Mirrors :func:`resolve_backends`'s boot guard (multi-backend dispatch is Phase 69 / SCHED). The
    retired ``_single_non_local`` accessor raised here; the Phase-68 replacement must preserve that
    single-non-local defense-in-depth for its three call sites (dashboard/backfill, agent_s3).
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
    with pytest.raises(ValueError, match=r"Phase 69"):
        backends.resolved_non_local_kind(settings)
