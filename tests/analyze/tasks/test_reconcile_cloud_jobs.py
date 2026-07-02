"""Tests for the ``*/5`` in-flight K8s reconcile cron (Phase 54, Plan 06 -- KSUBMIT-02..06, D-01..D-08).

``reconcile_cloud_jobs(ctx)`` iterates the ``cloud_job`` in-flight registry (status IN SUBMITTED,
RUNNING -- D-02), reads each Job + paired Kueue Workload through the monkeypatched ``kube_staging``
seam, and maps the ``(type, status, reason)`` condition tuples to an outcome. The exhaustive
state-machine coverage lives HERE, driven by the shared ``tests.kube_fakes`` factories with ZERO HTTP
(Layer-1 monkeypatched seam, RESEARCH §Fake-Kube Test Harness).

The load-bearing properties under test:
  * delete-after-record ordering (D-04): the Job delete follows the committed outcome; the S3 delete
    precedes the Job delete on the no-callback terminal; the success path makes ZERO S3 calls.
  * bounded re-drive (D-08): a no-callback terminal under the cap increments ``attempts`` and re-drives
    a fresh ``submit_cloud_job``; at the cap the FileRecord is marked ANALYSIS_FAILED (no fallback).
  * re-drive race guard: the prior Job is deleted AND confirmed gone (``get_job`` -> None) BEFORE the
    fresh submit is enqueued; a still-terminating Job defers the re-drive with no extra attempt burned.
  * Inadmissible holds + alerts and NEVER consumes the cap (D-06/D-07); healthy Pending is silent.
  * reconcile NEVER writes an analysis result (KSUBMIT-03).

``ctx`` mirrors the controller worker shape: ``async_session`` (a sessionmaker bound to the test
engine), ``queue`` (a ``DedupFakeQueue`` controller-queue stand-in the re-drive enqueues onto) and
``task_router`` (unused here, present for ctx parity).
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord, FileState
import phaze.tasks.reconcile_cloud_jobs as reconcile_mod
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from phaze.tasks.submit_cloud_job import submit_cloud_job_key
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter
from tests.kube_fakes import ADMITTED, EVICTED, INADMISSIBLE, PENDING, QUOTA_RESERVED, fake_job


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


# --- Seam spies -------------------------------------------------------------------------------------


class GetJobSpy:
    """Monkeypatch stand-in for ``kube_staging.get_job`` returning a per-call canned Job.

    Successive calls walk the supplied responses and then stick on the last one. The re-drive path
    calls ``get_job`` twice (the initial terminal read, then the confirm-gone check), so a two-element
    sequence models "non-terminal, then gone (None)" vs "non-terminal, then still terminating (Job)".
    """

    def __init__(self, *responses: Any) -> None:
        self._responses = responses if responses else (None,)
        self.calls: list[str] = []

    async def __call__(self, name: str) -> Any:
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(name)
        return self._responses[idx]


class GetWorkloadSpy:
    """Monkeypatch stand-in for ``kube_staging.get_workload_for`` returning a fixed canned Workload."""

    def __init__(self, workload: Any = None) -> None:
        self.workload = workload
        self.calls: list[str] = []

    async def __call__(self, uid: str) -> Any:
        self.calls.append(uid)
        return self.workload


class DeleteJobSpy:
    """Monkeypatch stand-in for ``kube_staging.delete_job`` -- records call order + a DB snapshot.

    Appending ``"delete_job"`` to the shared ``events`` list (alongside the S3 spy's ``"s3_delete"``)
    proves the relative ordering. When an ``engine`` is supplied, each call snapshots the committed
    ``cloud_job``/``FileRecord`` state at delete time -- proving the outcome was committed BEFORE the
    Job delete (D-04).
    """

    def __init__(self, events: list[str], engine: AsyncEngine | None = None) -> None:
        self.events = events
        self.engine = engine
        self.calls: list[str] = []
        self.snapshots: list[dict[str, Any]] = []

    async def __call__(self, name: str) -> None:
        self.calls.append(name)
        self.events.append("delete_job")
        if self.engine is not None:
            sm = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
            async with sm() as snap_session:
                cj = (await snap_session.execute(select(CloudJob).where(CloudJob.kueue_workload == name))).scalar_one_or_none()
                snap: dict[str, Any] = {"cloud_status": getattr(cj, "status", None), "attempts": getattr(cj, "attempts", None), "file_state": None}
                if cj is not None:
                    fr = (await snap_session.execute(select(FileRecord).where(FileRecord.id == cj.file_id))).scalar_one_or_none()
                    snap["file_state"] = getattr(fr, "state", None)
                self.snapshots.append(snap)


class S3DeleteSpy:
    """Monkeypatch stand-in for ``s3_staging.delete_staged_object`` -- records call order + file ids."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[uuid.UUID] = []

    async def __call__(self, file_id: uuid.UUID) -> None:
        self.calls.append(file_id)
        self.events.append("s3_delete")


# --- Fixtures / builders ----------------------------------------------------------------------------


def _patch_cap(monkeypatch: pytest.MonkeyPatch, cap: int = 3) -> None:
    """Pin reconcile's ``get_settings()`` so ``cloud_submit_max_attempts`` is deterministic."""
    monkeypatch.setattr("phaze.tasks.reconcile_cloud_jobs.get_settings", lambda: SimpleNamespace(cloud_submit_max_attempts=cap))


def _patch_seam(
    monkeypatch: pytest.MonkeyPatch,
    *,
    get_job: GetJobSpy,
    get_workload: GetWorkloadSpy | None = None,
    delete_job: DeleteJobSpy | None = None,
    s3_delete: S3DeleteSpy | None = None,
) -> tuple[GetJobSpy, GetWorkloadSpy, DeleteJobSpy, S3DeleteSpy]:
    """Monkeypatch the four kube/S3 seam functions reconcile calls; return the spies for assertions."""
    gw = get_workload or GetWorkloadSpy()
    dj = delete_job or DeleteJobSpy([])
    s3 = s3_delete or S3DeleteSpy([])
    monkeypatch.setattr("phaze.services.kube_staging.get_job", get_job)
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", gw)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", dj)
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)
    return get_job, gw, dj, s3


def _make_ctx(async_engine: AsyncEngine, queue: DedupFakeQueue | None = None) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + (unused) task router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": queue or DedupFakeQueue("controller"), "task_router": DedupFakeTaskRouter()}


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


async def _seed(session: AsyncSession, *, status: str = CloudJobStatus.SUBMITTED.value, attempts: int = 0) -> tuple[uuid.UUID, str]:
    """Seed a FileRecord + its in-flight cloud_job; return ``(file_id, kueue_workload_name)``."""
    file = _make_file()
    session.add(file)
    await session.flush()
    name = f"phaze-analyze-{file.id}"
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=f"phaze-staging/{file.id}",
            status=status,
            kueue_workload=name,
            attempts=attempts,
        )
    )
    await session.commit()
    return file.id, name


async def _read_cloud_job(session: AsyncSession, file_id: uuid.UUID) -> CloudJob:
    session.expire_all()
    return (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()


async def _read_file(session: AsyncSession, file_id: uuid.UUID) -> FileRecord:
    session.expire_all()
    return (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()


# --- Pending: silent -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_is_silent(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy Pending waits indefinitely: no attempts change, no inadmissible flag, no delete/enqueue."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(PENDING))

    tally = await reconcile_cloud_jobs(_make_ctx(async_engine))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert cj.attempts == 0
    assert cj.inadmissible is False
    assert dj.calls == []
    assert s3.calls == []
    assert tally["pending"] == 1


# --- Inadmissible: loud, holds, no cap -------------------------------------------------------------


@pytest.mark.asyncio
async def test_inadmissible_alerts_without_cap(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inadmissible sets the alert flag + holds; attempts UNCHANGED, Job untouched (D-06/D-07)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(INADMISSIBLE))

    tally = await reconcile_cloud_jobs(_make_ctx(async_engine))

    cj = await _read_cloud_job(session, fid)
    assert cj.inadmissible is True
    assert cj.attempts == 0
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert dj.calls == []
    assert s3.calls == []
    assert tally["inadmissible"] == 1


@pytest.mark.asyncio
async def test_inadmissible_never_consumes_cap(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Across MANY ticks an Inadmissible Workload never increments attempts nor marks ANALYSIS_FAILED."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session)
    _, _, dj, _ = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(INADMISSIBLE))
    ctx = _make_ctx(async_engine)

    for _ in range(8):  # well over the cap of 3
        await reconcile_cloud_jobs(ctx)

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 0
    assert cj.inadmissible is True
    assert cj.status == CloudJobStatus.SUBMITTED.value
    file = await _read_file(session, fid)
    assert file.state != FileState.ANALYSIS_FAILED
    assert dj.calls == []  # the Job is never deleted on an Inadmissible hold


# --- Admission -> success sequence -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_to_success_sequence(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pending -> Admitted(RUNNING) -> still RUNNING -> Succeeded reconciles correctly each tick."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    ctx = _make_ctx(async_engine)
    events: list[str] = []
    dj = DeleteJobSpy(events)
    s3 = S3DeleteSpy(events)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", dj)
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)

    # Tick 1: Pending -> silent, stays SUBMITTED.
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(name=name)))
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", GetWorkloadSpy(PENDING))
    await reconcile_cloud_jobs(ctx)
    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.SUBMITTED.value

    # Tick 2: Admitted -> RUNNING.
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", GetWorkloadSpy(ADMITTED))
    await reconcile_cloud_jobs(ctx)
    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.RUNNING.value

    # Tick 3: still Admitted -> stays RUNNING (idempotent).
    await reconcile_cloud_jobs(ctx)
    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.RUNNING.value

    # Tick 4: Job Succeeded -> SUCCEEDED + Job delete; no S3 delete, no result write.
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(succeeded=1, name=name)))
    await reconcile_cloud_jobs(ctx)
    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value
    assert dj.calls == [name]
    assert s3.calls == []  # success path never deletes S3 (the callback already did)


# --- Eviction -> re-drive --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eviction_triggers_redrive(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An Evicted Workload is a no-callback terminal: delete the Job, confirm gone, re-drive submit."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    queue = DedupFakeQueue("controller")
    ctx = _make_ctx(async_engine, queue)
    # get_job: initial read (non-terminal), then confirm-gone returns None.
    get_job, _, dj, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name), None),
        get_workload=GetWorkloadSpy(EVICTED),
    )

    tally = await reconcile_cloud_jobs(ctx)

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert dj.calls == [name]
    assert len(get_job.calls) == 2  # terminal read + confirm-gone
    # A fresh submit_cloud_job enqueued onto the controller queue with the deterministic dedup key.
    assert [t for t, _ in queue.captured] == ["submit_cloud_job"]
    assert queue.captured[0][1] == {"file_id": str(fid)}
    assert queue.captured_policy[0]["key"] == submit_cloud_job_key(fid)
    assert tally["redriven"] == 1


# --- Cap reached -> ANALYSIS_FAILED ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_attempts_cap_then_analysis_failed(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the cap a no-callback terminal marks the FileRecord ANALYSIS_FAILED (no cross-target fallback)."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=3)  # next_attempt = 4 > cap 3
    queue = DedupFakeQueue("controller")
    ctx = _make_ctx(async_engine, queue)
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)))

    tally = await reconcile_cloud_jobs(ctx)

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.FAILED.value
    file = await _read_file(session, fid)
    assert file.state == FileState.ANALYSIS_FAILED
    assert s3.calls == [fid]  # no-callback terminal deletes the staged object (D-05)
    assert dj.calls == [name]
    assert queue.captured == []  # no re-drive at the cap
    assert tally["failed"] == 1


# --- Delete-after-record ordering (D-04) -----------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_after_record_ordering(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """No-callback terminal ordering: outcome committed BEFORE Job delete; S3 delete BEFORE Job delete."""
    _patch_cap(monkeypatch, cap=3)
    _fid, name = await _seed(session, attempts=3)  # at cap -> the clean terminal ordering path
    events: list[str] = []
    dj = DeleteJobSpy(events, engine=async_engine)
    s3 = S3DeleteSpy(events)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)), delete_job=dj, s3_delete=s3)

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    # S3 delete precedes Job delete.
    assert events == ["s3_delete", "delete_job"]
    # The outcome was already committed when the Job delete fired (the snapshot reads committed state).
    assert dj.snapshots == [{"cloud_status": CloudJobStatus.FAILED.value, "attempts": 3, "file_state": FileState.ANALYSIS_FAILED}]


# --- S3 delete only on the no-callback terminal ----------------------------------------------------


@pytest.mark.asyncio
async def test_s3_delete_only_on_no_callback_terminal(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The success path makes ZERO S3 calls; the no-callback (at-cap) terminal deletes the object."""
    _patch_cap(monkeypatch, cap=3)
    # Success path: a Succeeded Job -> no S3 delete.
    _fid_ok, name_ok = await _seed(session)
    _, _, _, s3_ok = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(succeeded=1, name=name_ok)))
    await reconcile_cloud_jobs(_make_ctx(async_engine))
    assert s3_ok.calls == []

    # No-callback terminal (at cap): a Failed Job -> exactly one S3 delete for that file.
    fid_fail, name_fail = await _seed(session, attempts=3)
    _, _, _, s3_fail = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name_fail)))
    await reconcile_cloud_jobs(_make_ctx(async_engine))
    assert s3_fail.calls == [fid_fail]


# --- Reconcile never writes a result (KSUBMIT-03) --------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_never_writes_result(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Complete Job whose callback already landed only cleans up -- reconcile writes no result row."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    # A Job reporting Complete via condition (not just the counter) -- still only a cleanup trigger.
    complete_job = fake_job(name=name)
    complete_job.status["conditions"] = [{"type": "Complete", "status": "True", "reason": ""}]
    _patch_seam(monkeypatch, get_job=GetJobSpy(complete_job))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    # No AnalysisResult row written by reconcile (the callback is the sole result writer).
    results = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == fid))).scalars().all()
    assert results == []
    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value


def test_reconcile_module_calls_no_result_writer() -> None:
    """KSUBMIT-03 source guard: the reconcile module never calls put_analysis/report_analysis_failed.

    Mirrors the submit-task source guard -- a grep test that proves the result-writer surface is absent,
    so the out-of-band callback stays the sole authoritative result channel.
    """
    src = pathlib.Path(reconcile_mod.__file__).read_text(encoding="utf-8")
    assert "put_analysis" not in src
    assert "report_analysis_failed" not in src
    assert "AnalysisResult" not in src

    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imported)
    assert not any(name.startswith("phaze.routers") for name in imported)


# --- Re-drive race guard: confirm prior Job gone before re-submit ----------------------------------


@pytest.mark.asyncio
async def test_redrive_confirms_prior_job_gone_before_resubmit(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The under-cap re-drive re-submits ONLY after confirming the prior Job is gone (race guard, D-08).

    Case A (gone): ``get_job`` returns None on the confirm-gone read -> attempts increments + a fresh
    ``submit_cloud_job`` is enqueued.
    Case B (still terminating): the confirm-gone read still returns the dying Job -> NO re-submit on
    this tick and NO extra attempt burned (the row is left untouched for a later tick).
    """
    _patch_cap(monkeypatch, cap=3)

    # Case A: confirm-gone returns None -> re-drive proceeds.
    fid_a, name_a = await _seed(session, attempts=0)
    queue_a = DedupFakeQueue("controller")
    get_job_a, _, dj_a, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name_a), None),
        get_workload=GetWorkloadSpy(EVICTED),
    )
    await reconcile_cloud_jobs(_make_ctx(async_engine, queue_a))
    cj_a = await _read_cloud_job(session, fid_a)
    assert cj_a.attempts == 1
    assert dj_a.calls == [name_a]
    assert len(get_job_a.calls) == 2
    assert [t for t, _ in queue_a.captured] == ["submit_cloud_job"]
    # Take case A's row out of the in-flight set so case B's tick reconciles ONLY case B's row.
    cj_a.status = CloudJobStatus.SUCCEEDED.value
    await session.commit()

    # Case B: confirm-gone still returns the terminating Job -> defer, no re-submit, no attempt burned.
    fid_b, name_b = await _seed(session, attempts=0)
    queue_b = DedupFakeQueue("controller")
    get_job_b, _, dj_b, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name_b), fake_job(failed=1, name=name_b)),
        get_workload=GetWorkloadSpy(EVICTED),
    )
    await reconcile_cloud_jobs(_make_ctx(async_engine, queue_b))
    cj_b = await _read_cloud_job(session, fid_b)
    assert cj_b.attempts == 0  # NO extra attempt burned
    assert cj_b.status == CloudJobStatus.SUBMITTED.value
    assert dj_b.calls == [name_b]  # the prior Job was deleted
    assert len(get_job_b.calls) == 2  # terminal read + confirm-gone (still present)
    assert queue_b.captured == []  # NO re-submit enqueued on this tick


# --- CR-01: the Inadmissible alert flag clears once the Workload recovers ---------------------------


@pytest.mark.asyncio
async def test_inadmissible_clears_on_admission(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inadmissible tick sets the flag; a later Admitted tick clears it (CR-01 -- the alert must recover)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    ctx = _make_ctx(async_engine)
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(name=name)))

    # Tick 1: Inadmissible -> flag set.
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", GetWorkloadSpy(INADMISSIBLE))
    await reconcile_cloud_jobs(ctx)
    assert (await _read_cloud_job(session, fid)).inadmissible is True

    # Tick 2: operator fixed the LocalQueue -> Admitted -> flag cleared + RUNNING.
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", GetWorkloadSpy(ADMITTED))
    await reconcile_cloud_jobs(ctx)
    cj = await _read_cloud_job(session, fid)
    assert cj.inadmissible is False
    assert cj.status == CloudJobStatus.RUNNING.value


@pytest.mark.asyncio
async def test_inadmissible_clears_on_pending(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recovered Workload that returns to a healthy Pending wait clears the stale alert flag (CR-01)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    ctx = _make_ctx(async_engine)
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(name=name)))

    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", GetWorkloadSpy(INADMISSIBLE))
    await reconcile_cloud_jobs(ctx)
    assert (await _read_cloud_job(session, fid)).inadmissible is True

    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", GetWorkloadSpy(PENDING))
    await reconcile_cloud_jobs(ctx)
    assert (await _read_cloud_job(session, fid)).inadmissible is False


@pytest.mark.asyncio
async def test_inadmissible_clears_on_success(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transiently-Inadmissible row that then succeeds ends with the flag cleared (CR-01)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    ctx = _make_ctx(async_engine)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", DeleteJobSpy([]))

    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(name=name)))
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", GetWorkloadSpy(INADMISSIBLE))
    await reconcile_cloud_jobs(ctx)
    assert (await _read_cloud_job(session, fid)).inadmissible is True

    # Job later succeeds -> SUCCEEDED + flag cleared (so it never inflates the terminal-row count).
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(succeeded=1, name=name)))
    await reconcile_cloud_jobs(ctx)
    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value
    assert cj.inadmissible is False


# --- WR-01: a vanished Job (404 / None) is a no-callback terminal, not a stuck transient -------------


@pytest.mark.asyncio
async def test_vanished_job_routes_to_terminal_redrive(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-flight row whose Job has vanished (get_job -> None) re-drives under cap instead of sticking (WR-01)."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    queue = DedupFakeQueue("controller")
    # get_job returns None on every call: the initial read (gone) AND the confirm-gone read.
    _, _, dj, _ = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    tally = await reconcile_cloud_jobs(_make_ctx(async_engine, queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert dj.calls == [name]  # idempotent delete of the (already-gone) Job
    assert [t for t, _ in queue.captured] == ["submit_cloud_job"]
    assert tally["redriven"] == 1


@pytest.mark.asyncio
async def test_vanished_job_at_cap_marks_analysis_failed(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the cap a vanished Job is a terminal no-callback -> ANALYSIS_FAILED, never an eternal skip (WR-01)."""
    _patch_cap(monkeypatch, cap=3)
    fid, _name = await _seed(session, attempts=3)  # next_attempt = 4 > cap
    _, _, _, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.FAILED.value
    assert (await _read_file(session, fid)).state == FileState.ANALYSIS_FAILED
    assert s3.calls == [fid]


# --- Per-row guard: one bad row never aborts the tick ----------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_row_does_not_abort_tick(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose kube read raises is logged + skipped; the remaining rows still reconcile."""
    _patch_cap(monkeypatch)
    fid_bad, name_bad = await _seed(session)
    fid_ok, name_ok = await _seed(session)

    class _Flaky:
        async def __call__(self, name: str) -> Any:
            if name == name_bad:
                raise RuntimeError("transient kube API error")
            return fake_job(succeeded=1, name=name)

    _, _, dj, _ = _patch_seam(monkeypatch, get_job=_Flaky())  # type: ignore[arg-type]

    tally = await reconcile_cloud_jobs(_make_ctx(async_engine))

    # The good row still reconciled to success despite the bad row raising.
    assert (await _read_cloud_job(session, fid_ok)).status == CloudJobStatus.SUCCEEDED.value
    assert (await _read_cloud_job(session, fid_bad)).status == CloudJobStatus.SUBMITTED.value
    assert name_ok in dj.calls
    assert tally["reconciled"] == 2
    assert tally["succeeded"] == 1


# --- D-04: cloud_phase admission progression co-write (orthogonal to inadmissible) -----------------


@pytest.mark.asyncio
async def test_pending_sets_cloud_phase_queued_behind_quota(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy-Pending Workload stamps cloud_phase=queued_behind_quota on a NULL-phase row (D-04)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)  # seeded cloud_phase is NULL (a fresh in-flight row)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(PENDING))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    assert (await _read_cloud_job(session, fid)).cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value


@pytest.mark.asyncio
async def test_quota_reserved_sets_cloud_phase_admitted(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """QuotaReserved=True (not yet Admitted) advances cloud_phase to admitted (D-04)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(QUOTA_RESERVED))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    cj = await _read_cloud_job(session, fid)
    assert cj.cloud_phase == CloudPhase.ADMITTED.value
    assert cj.status == CloudJobStatus.RUNNING.value  # status advance unchanged


@pytest.mark.asyncio
async def test_admitted_sets_cloud_phase_running(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An Admitted=True Workload advances cloud_phase to running alongside the SUBMITTED->RUNNING status write (D-04)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(ADMITTED))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    cj = await _read_cloud_job(session, fid)
    assert cj.cloud_phase == CloudPhase.RUNNING.value
    assert cj.status == CloudJobStatus.RUNNING.value


@pytest.mark.asyncio
async def test_success_sets_cloud_phase_finished(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Succeeded Job writes cloud_phase=finished before its commit (D-04)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", DeleteJobSpy([]))
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(succeeded=1, name=name)))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value
    assert cj.cloud_phase == CloudPhase.FINISHED.value


@pytest.mark.asyncio
async def test_inadmissible_does_not_touch_cloud_phase(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Inadmissible branch sets only the fault flag -- cloud_phase stays untouched (orthogonality, D-04)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)  # cloud_phase starts NULL
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(INADMISSIBLE))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    cj = await _read_cloud_job(session, fid)
    assert cj.inadmissible is True
    assert cj.cloud_phase is None  # the fault flag is orthogonal: it never repurposes the admission phase
