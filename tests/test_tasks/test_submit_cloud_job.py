"""Tests for the fast Kube-submit producer (Phase 54, Plan 05 -- KSUBMIT-01/02/06).

``submit_cloud_job(ctx, file_id)`` does ONE kube POST (``kube_staging.submit_job``, monkeypatched
here) and upserts the ``cloud_job`` row (status=SUBMITTED + ``kueue_workload=<job-name>``) keyed by
``file_id``. It returns promptly after the single POST -- it never awaits analysis and writes no
``AnalysisResult``. CRITICALLY it seeds NO ``SchedulingLedger`` ``process_file:<id>`` row
(KSUBMIT-06, the CLOUDROUTE-02 hazard: a ledger row would let ``recover_orphaned_work`` re-enqueue
a K8s file onto a LOCAL agent queue).

A re-submit for the same ``file_id`` is idempotent: the upsert keeps a single row and the seam's
deterministic Job name + 409->refresh means no duplicate Job (modeled here by the spy returning the
same name on every call).

``ctx`` mirrors the controller worker shape: ``async_session`` (a sessionmaker bound to the test
engine), exactly like ``recover_orphaned_work`` / ``stage_cloud_window``.
"""

from __future__ import annotations

import ast
import pathlib
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
import phaze.tasks.submit_cloud_job as submit_mod
from phaze.tasks.submit_cloud_job import submit_cloud_job, submit_cloud_job_key


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class _SubmitSpy:
    """A monkeypatch stand-in for ``kube_staging.submit_job``.

    Records each call and always returns the SAME ``(name, uid)`` -- modeling the seam's
    deterministic Job name + 409->refresh idempotency (a re-submit yields no duplicate Job).
    """

    def __init__(self, name: str = "phaze-analyze-job", uid: str = "uid-1") -> None:
        self.name = name
        self.uid = uid
        self.calls: list[uuid.UUID] = []

    async def __call__(self, file_id: uuid.UUID) -> tuple[str, str]:
        self.calls.append(file_id)
        return self.name, self.uid


def _make_file(*, state: str = FileState.AWAITING_CLOUD) -> FileRecord:
    """Build a fully-populated FileRecord (the cloud_job FK target)."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.flac",
        original_filename=f"{uid.hex}.flac",
        current_path=f"/music/{uid.hex}.flac",
        file_type="flac",
        file_size=1000,
        state=state,
    )


def _make_ctx(async_engine: AsyncEngine) -> dict[str, Any]:
    """Build a controller-shaped ctx: just ``async_session`` (the submit task's only ctx need)."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm}


@pytest.mark.asyncio
async def test_submit_creates_submitted_cloud_job_with_kueue_workload(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One submit -> one cloud_job row at SUBMITTED with ``kueue_workload`` set; one kube POST."""
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id

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


def test_submit_cloud_job_key_is_deterministic() -> None:
    """The deterministic enqueue key mirrors the ``s3_upload:<id>`` / ``push_file:<id>`` idiom."""
    fid = uuid.uuid4()
    assert submit_cloud_job_key(fid) == f"submit_cloud_job:{fid}"
