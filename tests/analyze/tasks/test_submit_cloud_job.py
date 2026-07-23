"""Tests for the fast Kube-submit producer (Phase 54, Plan 05 -- KSUBMIT-01/02/06).

``submit_cloud_job(ctx, file_id)`` does ONE kube POST (``kube_staging.submit_job``, monkeypatched
here) and upserts the ``cloud_job`` row (status=SUBMITTED + ``kueue_workload=<job-name>``) keyed by
``file_id``. It returns promptly after the single POST -- it never awaits analysis and writes no
``AnalysisResult``. CRITICALLY it seeds NO ``SchedulingLedger`` ``process_file:<id>`` row
(KSUBMIT-06, the CLOUDROUTE-02 hazard: a ledger row would let ``recover_orphaned_work`` re-enqueue
a K8s file onto a LOCAL agent queue).

Phase 70 (MKUE-01/D-04): the submit resolves THIS file's owning backend cluster from the recorded
``cloud_job.backend_id`` (stamped at dispatch) BEFORE the POST, and threads that backend's
``KubeConfig`` into ``kube_staging.submit_job``. A submit with no owning kueue backend is a
misconfiguration -> ``KubeStagingError``. Each DB test therefore seeds a ``cloud_job`` row carrying
``backend_id`` and pins ``get_settings`` to a one-kueue registry stub whose id matches.

A re-submit for the same ``file_id`` is idempotent: the upsert keeps a single row and the seam's
deterministic Job name + 409->refresh means no duplicate Job (modeled here by the spy returning the
same name on every call).

``ctx`` mirrors the controller worker shape: ``async_session`` (a sessionmaker bound to the test
engine), exactly like ``recover_orphaned_work`` / ``stage_cloud_window``.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
import pathlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, update

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services import kube_staging
import phaze.tasks.submit_cloud_job as submit_mod
from phaze.tasks.submit_cloud_job import submit_cloud_job, submit_cloud_job_key


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


# SCHED-05 / MKUE-01: the submit resolves the file's backend via cloud_job.backend_id against the
# registry; the stub carries a single kueue entry whose id matches the seeded row's backend_id.
_KUEUE_BACKEND_ID = "kueue-x64"


class _SubmitSpy:
    """A monkeypatch stand-in for ``kube_staging.submit_job``.

    Records each call's ``file_id`` AND the threaded ``kube`` (MKUE-01) and always returns the SAME
    ``(name, uid)`` -- modeling the seam's deterministic Job name + 409->refresh idempotency (a
    re-submit yields no duplicate Job).
    """

    def __init__(self, name: str = "phaze-analyze-job", uid: str = "uid-1") -> None:
        self.name = name
        self.uid = uid
        self.calls: list[uuid.UUID] = []
        self.kubes: list[Any] = []

    async def __call__(self, file_id: uuid.UUID, kube: Any) -> tuple[str, str]:
        self.calls.append(file_id)
        self.kubes.append(kube)
        return self.name, self.uid


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin ``submit_cloud_job.get_settings`` to a one-kueue registry whose id == the seeded backend_id."""
    from phaze.config_backends import KubeConfig

    kube = KubeConfig(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq")
    settings = SimpleNamespace(backends=[SimpleNamespace(kind="kueue", id=_KUEUE_BACKEND_ID, kube=kube)])
    monkeypatch.setattr("phaze.tasks.submit_cloud_job.get_settings", lambda: settings)
    return kube


def _make_file() -> FileRecord:
    """Build a fully-populated FileRecord (the cloud_job FK target)."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.flac",
        original_filename=f"{uid.hex}.flac",
        current_path=f"/music/{uid.hex}.flac",
        file_type="flac",
        file_size=1000,
    )


async def _seed_cloud_job(session: AsyncSession, fid: uuid.UUID, *, backend_id: str | None = _KUEUE_BACKEND_ID) -> None:
    """Seed the dispatch-stamped cloud_job row (backend_id set) the submit resolves its cluster from."""
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=fid,
            backend_id=backend_id,
            s3_key=f"phaze-staging/{fid}",
            status=CloudJobStatus.UPLOADED.value,
        )
    )
    await session.commit()


def _make_ctx(async_engine: AsyncEngine) -> dict[str, Any]:
    """Build a controller-shaped ctx: just ``async_session`` (the submit task's only ctx need).

    92-04 (CLEAN-02): the ctx ``async_session`` is sourced from ``phaze.database.async_session`` -- which the
    ``session`` fixture's ``_route_stats_fanout`` monkeypatches to a factory BOUND to the per-test
    ``_db_connection`` (``join_transaction_mode="create_savepoint"``), exactly as the production controller
    worker wires ``ctx["async_session"]``. Under create_savepoint isolation this lets the task SEE seeded rows
    and makes its own commits visible to sibling verify reads on the same connection (a fresh
    ``async_sessionmaker(async_engine)`` would open a DIFFERENT pool connection and read ZERO/STALE).
    """
    from phaze.database import async_session

    return {"async_session": async_session}


@pytest.mark.asyncio
async def test_submit_creates_submitted_cloud_job_with_kueue_workload(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One submit -> the cloud_job row flips to SUBMITTED with ``kueue_workload`` set; one kube POST."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    _patch_settings(monkeypatch)

    spy = _SubmitSpy(name=f"phaze-analyze-{fid}")
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)

    result = await submit_cloud_job(_make_ctx(async_engine), fid)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == CloudJobStatus.SUBMITTED.value
    assert rows[0].kueue_workload == f"phaze-analyze-{fid}"
    # Exactly one kube POST, keyed by the file_id.
    assert spy.calls == [fid]
    assert result["kueue_workload"] == f"phaze-analyze-{fid}"


@pytest.mark.asyncio
async def test_submit_resolves_backend_kube_from_recorded_backend_id(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MKUE-01: the POST is threaded THIS file's backend cluster, resolved via cloud_job.backend_id."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    kube = _patch_settings(monkeypatch)

    spy = _SubmitSpy(name=f"phaze-analyze-{fid}")
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)

    await submit_cloud_job(_make_ctx(async_engine), fid)

    # The seam received the registry-resolved KubeConfig for the recorded backend_id (not a global).
    assert spy.kubes == [kube]
    assert spy.kubes[0].api_url == "https://kube.example.com"


@pytest.mark.asyncio
async def test_submit_raises_when_no_owning_backend(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submit whose file has no cloud_job (no recorded backend_id) is a misconfig -> KubeStagingError."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    # No cloud_job seeded -> backend_id resolves to None.
    _patch_settings(monkeypatch)
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", _SubmitSpy())

    with pytest.raises(kube_staging.KubeStagingError):
        await submit_cloud_job(_make_ctx(async_engine), fid)


@pytest.mark.asyncio
async def test_resubmit_is_idempotent_single_row(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second submit for the same file_id upserts (one row) and re-hits the idempotent seam."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    _patch_settings(monkeypatch)

    spy = _SubmitSpy(name=f"phaze-analyze-{fid}")
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)
    ctx = _make_ctx(async_engine)

    await submit_cloud_job(ctx, fid)
    await submit_cloud_job(ctx, fid)

    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalars().all()
    assert len(rows) == 1  # unique file_id FK -- the re-submit updated, never duplicated
    assert rows[0].kueue_workload == f"phaze-analyze-{fid}"
    # Both submits hit the seam (the 409->refresh inside the seam makes the duplicate POST safe).
    assert spy.calls == [fid, fid]


@pytest.mark.asyncio
async def test_resubmit_bumps_updated_at_not_created_at(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-7634: a re-submit (conflicting upsert) bumps CloudJob.updated_at; created_at stays pinned.

    Same defect class as phaze-c8nz on the CAS-guarded submit upsert (phaze-kzto,
    ``where=status IN ('uploaded','submitted')``): the `set_` clause used to omit `updated_at`.
    The CAS predicate itself is untouched by the fix, only which columns the guarded UPDATE
    writes. Backdate both columns, re-submit, and assert updated_at moves forward while
    created_at is untouched.
    """
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    _patch_settings(monkeypatch)

    spy = _SubmitSpy(name=f"phaze-analyze-{fid}")
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)
    ctx = _make_ctx(async_engine)

    await submit_cloud_job(ctx, fid)

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. cloud_job.created_at/updated_at are TIMESTAMP WITHOUT TIME ZONE columns --
    # use a naive UTC value so asyncpg doesn't reject the aware/naive mismatch.
    outage_time = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(hours=12)
    await session.execute(update(CloudJob).where(CloudJob.file_id == fid).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_resubmit = datetime.now(UTC).replace(tzinfo=None)

    await submit_cloud_job(ctx, fid)

    session.expire_all()
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_resubmit - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_status", [CloudJobStatus.AWAITING.value, CloudJobStatus.SUCCEEDED.value, CloudJobStatus.FAILED.value])
async def test_late_submit_does_not_resurrect_non_advanceable_row(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    stale_status: str,
) -> None:
    """phaze-kzto: a delayed submit MUST NOT flip a spilled ('awaiting') or terminal row to SUBMITTED.

    The CAS guards the upsert on status IN ('uploaded','submitted'); a row already advanced past the
    submit window is left untouched and the doomed Job we POSTed is deleted (no phantom cap slot).
    """
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    # Seed a row that a reconcile tick already moved OUT of the submit window.
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=fid,
            backend_id=_KUEUE_BACKEND_ID,
            s3_key=f"phaze-staging/{fid}",
            status=stale_status,
        )
    )
    await session.commit()
    _patch_settings(monkeypatch)

    spy = _SubmitSpy(name=f"phaze-analyze-{fid}")
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)
    deleted: list[str] = []

    async def _fake_delete(name: str, kube: Any) -> None:
        deleted.append(name)

    monkeypatch.setattr("phaze.services.kube_staging.delete_job", _fake_delete)

    result = await submit_cloud_job(_make_ctx(async_engine), fid)

    session.expire_all()
    rows = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalars().all()
    assert len(rows) == 1
    # The row is NOT resurrected to SUBMITTED -- it keeps its advanced status.
    assert rows[0].status == stale_status
    # The doomed Job we POSTed is torn down (no phantom cap slot / orphaned pod).
    assert deleted == [f"phaze-analyze-{fid}"]
    assert result.get("status") == "skipped"


@pytest.mark.asyncio
async def test_submit_seeds_no_scheduling_ledger_row(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KSUBMIT-06: the submit path writes ZERO SchedulingLedger rows (no process_file:<id> seed)."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    _patch_settings(monkeypatch)

    monkeypatch.setattr("phaze.services.kube_staging.submit_job", _SubmitSpy(name=f"phaze-analyze-{fid}"))

    await submit_cloud_job(_make_ctx(async_engine), fid)

    # No process_file:<id> ledger row (the CLOUDROUTE-02 hazard recover_orphaned_work would replay).
    seeded = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{fid}"))).scalar_one_or_none()
    assert seeded is None
    # And NO ledger row of any kind -- the submit path is ledger-free.
    assert (await session.execute(select(SchedulingLedger))).scalars().all() == []


@pytest.mark.asyncio
async def test_submit_writes_no_analysis_result(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fast-return submit never awaits analysis -> it writes no AnalysisResult row (KSUBMIT-02)."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    _patch_settings(monkeypatch)

    monkeypatch.setattr("phaze.services.kube_staging.submit_job", _SubmitSpy(name=f"phaze-analyze-{fid}"))

    await submit_cloud_job(_make_ctx(async_engine), fid)

    results = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == fid))).scalars().all()
    assert results == []


def test_module_seeds_no_ledger_and_writes_no_result() -> None:
    """Source-level invariants: no SchedulingLedger seed, no analysis-result writer, no web layer.

    KSUBMIT-06 grep test (``SchedulingLedger`` absent) plus a guard that the thin producer never
    imports the FastAPI/result-writer surface -- it reads kube state ONLY to write the cloud_job row.
    """
    src = pathlib.Path(submit_mod.__file__).read_text(encoding="utf-8")
    assert "SchedulingLedger" not in src, "submit path must seed no SchedulingLedger row (KSUBMIT-06)"
    assert "AnalysisResult" not in src, "submit path must write no analysis result (KSUBMIT-02)"
    assert "put_analysis" not in src and "report_analysis_failed" not in src

    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imported)
    assert not any(name.startswith("phaze.routers") for name in imported)


@pytest.mark.asyncio
async def test_submit_seeds_cloud_phase_queued_behind_quota(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inserted SUBMITTED row carries cloud_phase=queued_behind_quota (the admission seed, D-04)."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    _patch_settings(monkeypatch)

    monkeypatch.setattr("phaze.services.kube_staging.submit_job", _SubmitSpy(name=f"phaze-analyze-{fid}"))

    await submit_cloud_job(_make_ctx(async_engine), fid)

    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    assert row.cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value


@pytest.mark.asyncio
async def test_resubmit_resets_cloud_phase_to_queued_behind_quota(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-submit upsert resets a previously-advanced cloud_phase back to queued_behind_quota (D-04)."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    await _seed_cloud_job(session, fid)
    _patch_settings(monkeypatch)

    monkeypatch.setattr("phaze.services.kube_staging.submit_job", _SubmitSpy(name=f"phaze-analyze-{fid}"))
    ctx = _make_ctx(async_engine)

    await submit_cloud_job(ctx, fid)

    # Simulate reconcile having advanced the admission phase to running.
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    row.cloud_phase = CloudPhase.RUNNING.value
    await session.commit()

    # A re-submit (on_conflict_do_update) resets the progression back to queued_behind_quota.
    await submit_cloud_job(ctx, fid)
    session.expire_all()
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    assert row.cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value


def test_submit_cloud_job_key_is_deterministic() -> None:
    """The deterministic enqueue key mirrors the ``s3_upload:<id>`` / ``push_file:<id>`` idiom."""
    fid = uuid.uuid4()
    assert submit_cloud_job_key(fid) == f"submit_cloud_job:{fid}"
