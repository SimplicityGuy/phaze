"""Successful and terminal outcomes, cleanup, and callback primacy."""

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
from phaze.models.file import FileRecord
import phaze.tasks.reconcile_cloud_jobs as reconcile_mod
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from tests._backends_patch import patch_backends_get_settings
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter
from tests.kube_fakes import fake_job


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class GetJobSpy:
    """Monkeypatch stand-in for ``kube_staging.get_job`` returning a per-call canned Job.

    Successive calls walk the supplied responses and then stick on the last one. The re-drive path
    calls ``get_job`` twice (the initial terminal read, then the confirm-gone check), so a two-element
    sequence models "non-terminal, then gone (None)" vs "non-terminal, then still terminating (Job)".
    """

    def __init__(self, *responses: Any) -> None:
        self._responses = responses if responses else (None,)
        self.calls: list[str] = []

    async def __call__(self, name: str, kube: Any = None) -> Any:  # noqa: ARG002 -- MKUE-01 seam signature
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(name)
        return self._responses[idx]


class GetWorkloadSpy:
    """Monkeypatch stand-in for ``kube_staging.get_workload_for`` returning a fixed canned Workload."""

    def __init__(self, workload: Any = None) -> None:
        self.workload = workload
        self.calls: list[str] = []

    async def __call__(self, uid: str, kube: Any = None) -> Any:  # noqa: ARG002 -- MKUE-01 seam signature
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

    async def __call__(self, name: str, kube: Any = None) -> None:  # noqa: ARG002 -- MKUE-01 seam signature
        self.calls.append(name)
        self.events.append("delete_job")
        if self.engine is not None:
            sm = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
            async with sm() as snap_session:
                cj = (await snap_session.execute(select(CloudJob).where(CloudJob.kueue_workload == name))).scalar_one_or_none()
                # Post-MIG-04 there is no files.state to snapshot: reconcile's at-cap spill re-stamps ONLY
                # the cloud_job sidecar (D-04/D-12), so the committed cloud_status + attempts are the invariant.
                snap: dict[str, Any] = {"cloud_status": getattr(cj, "status", None), "attempts": getattr(cj, "attempts", None)}
                self.snapshots.append(snap)


class ListPodsSpy:
    """Monkeypatch stand-in for ``kube_staging.list_pods_for_job`` returning a fixed canned pod list.

    phaze-202e: the pod list is the ONLY input to the wedge verdict. The default (``[]``) is the
    "unknown" shape -- the classifier reads it as STARTING, so every pre-existing test that does not
    opt in keeps its row HELD, exactly as an unreadable pod list should.
    """

    def __init__(self, *pods: Any) -> None:
        self.pods = list(pods)
        self.calls: list[str] = []

    async def __call__(self, name: str, kube: Any = None) -> list[Any]:  # noqa: ARG002 -- MKUE-01 seam signature
        self.calls.append(name)
        return self.pods


class S3DeleteSpy:
    """Monkeypatch stand-in for ``s3_staging.delete_staged_object`` -- records call order + file ids.

    Phase 70 (MKUE-02): the delete now takes ``(file_id, bucket)``; the spy records the file_id and the
    resolved bucket id so the at-cap terminal can be proven to act on the RECORDED staging bucket.
    """

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[uuid.UUID] = []
        self.buckets: list[str] = []

    async def __call__(self, file_id: uuid.UUID, bucket: Any = None) -> None:
        self.calls.append(file_id)
        self.buckets.append(getattr(bucket, "id", None))
        self.events.append("s3_delete")


_KUEUE_BACKEND_ID = "kueue-x64"

_STAGING_BUCKET_ID = "staging-a"


def _patch_cap(monkeypatch: pytest.MonkeyPatch, cap: int = 3, node_loss_ceiling: int = 1) -> None:
    """Pin ``get_settings()`` for BOTH the cron and ``KueueBackend.reconcile`` so cap + registry are deterministic.

    The Phase-69 cron (SCHED-05) resolves backends via ``resolve_backends(get_settings())`` and dispatches
    ``KueueBackend.reconcile`` -- which reads the cap from ``phaze.services.backends.get_settings``. Patch
    both bindings to the same stub carrying ``cloud_submit_max_attempts`` + a one-entry kueue registry +
    the ``buckets`` list the MKUE-02 at-cap staged-object delete resolves the recorded bucket against.
    """
    settings = SimpleNamespace(
        cloud_submit_max_attempts=cap,
        # phaze-1q4g: the SECOND re-drive budget -- node-loss re-drives spend this one, not ``attempts``.
        cloud_node_loss_max_redrives=node_loss_ceiling,
        cloud_enabled=True,
        backends=[
            SimpleNamespace(
                kind="kueue",
                id=_KUEUE_BACKEND_ID,
                rank=20,
                cap=cap,
                # MKUE-01/D-04: KueueBackend.reconcile threads self.config.kube into the seam; the
                # get_job/delete_job/list_pods spies are monkeypatched, so a minimal stand-in kube suffices.
                # phaze-202e: ``active_deadline_seconds=None`` mirrors the KubeConfig default -- reconcile
                # no longer reads it at all (liveness comes from pod state, not from any wall clock).
                kube=SimpleNamespace(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq", active_deadline_seconds=None),
            )
        ],
        buckets=[SimpleNamespace(id=_STAGING_BUCKET_ID)],
    )
    monkeypatch.setattr("phaze.tasks.reconcile_cloud_jobs.get_settings", lambda: settings)
    patch_backends_get_settings(monkeypatch, lambda: settings)


def _patch_seam(
    monkeypatch: pytest.MonkeyPatch,
    *,
    get_job: GetJobSpy,
    get_workload: GetWorkloadSpy | None = None,
    delete_job: DeleteJobSpy | None = None,
    s3_delete: S3DeleteSpy | None = None,
    list_pods: ListPodsSpy | None = None,
) -> tuple[GetJobSpy, GetWorkloadSpy, DeleteJobSpy, S3DeleteSpy]:
    """Monkeypatch the five kube/S3 seam functions reconcile calls; return the spies for assertions."""
    gw = get_workload or GetWorkloadSpy()
    dj = delete_job or DeleteJobSpy([])
    s3 = s3_delete or S3DeleteSpy([])
    monkeypatch.setattr("phaze.services.kube_staging.get_job", get_job)
    monkeypatch.setattr("phaze.services.kube_staging.get_workload_for", gw)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", dj)
    monkeypatch.setattr("phaze.services.kube_staging.list_pods_for_job", list_pods or ListPodsSpy())
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)
    return get_job, gw, dj, s3


def _make_ctx(queue: DedupFakeQueue | None = None) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + (unused) task router.

    92-04 (CLEAN-02): ``async_session`` is sourced from ``phaze.database.async_session`` -- monkeypatched by the
    ``session`` fixture's ``_route_stats_fanout`` to a factory BOUND to the per-test ``_db_connection``
    (create_savepoint), exactly as the production controller wires ``ctx["async_session"]``. This lets reconcile
    SEE seeded rows and makes its commits visible to sibling reads under create_savepoint isolation.
    """
    from phaze.database import async_session

    return {"async_session": async_session, "queue": queue or DedupFakeQueue("controller"), "task_router": DedupFakeTaskRouter()}


def _make_file() -> FileRecord:
    """Build a fully-populated FileRecord (the cloud_job FK target).

    Defaults to ``PUSHED`` -- the realistic state of a file whose cloud_job is in-flight
    (SUBMITTED/RUNNING): dispatch flips it to PUSHING, the S3-upload callback advances it to PUSHED.
    Phase 80 (D-04): reconcile's at-cap spill NO LONGER writes ``FileRecord.state``, so the file must
    stay at this seeded state for the at-cap tests to prove the no-state-write invariant.
    """
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


async def _seed(
    session: AsyncSession, *, status: str = CloudJobStatus.SUBMITTED.value, attempts: int = 0, node_loss_redrives: int = 0
) -> tuple[uuid.UUID, str]:
    """Seed a FileRecord + its in-flight cloud_job; return ``(file_id, kueue_workload_name)``."""
    file = _make_file()
    session.add(file)
    await session.flush()
    name = f"phaze-analyze-{file.id}"
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=_KUEUE_BACKEND_ID,  # SCHED-05: KueueBackend.reconcile is backend_id-scoped -> stamp so the per-backend query owns this row.
            s3_key=f"phaze-staging/{file.id}",
            status=status,
            kueue_workload=name,
            attempts=attempts,
            node_loss_redrives=node_loss_redrives,  # phaze-1q4g: the independent node-loss budget already spent.
            staging_bucket=_STAGING_BUCKET_ID,  # MKUE-02: the at-cap staged-object delete acts on this recorded bucket.
        )
    )
    await session.commit()
    return file.id, name


async def _read_cloud_job(session: AsyncSession, file_id: uuid.UUID) -> CloudJob:
    session.expire_all()
    return (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()


@pytest.mark.asyncio
async def test_s3_delete_only_on_no_callback_terminal(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The success path makes ZERO S3 calls; the no-callback (at-cap) terminal deletes the object."""
    _patch_cap(monkeypatch, cap=3)
    # Success path: a Succeeded Job -> no S3 delete.
    _fid_ok, name_ok = await _seed(session)
    _, _, _, s3_ok = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(succeeded=1, name=name_ok)))
    await reconcile_cloud_jobs(_make_ctx())
    assert s3_ok.calls == []

    # No-callback terminal (at cap): a Failed Job -> exactly one S3 delete for that file.
    fid_fail, name_fail = await _seed(session, attempts=3)
    _, _, _, s3_fail = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name_fail)))
    await reconcile_cloud_jobs(_make_ctx())
    assert s3_fail.calls == [fid_fail]


@pytest.mark.asyncio
async def test_reconcile_never_writes_result(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Complete Job whose callback already landed only cleans up -- reconcile writes no result row."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    # A Job reporting Complete via condition (not just the counter) -- still only a cleanup trigger.
    complete_job = fake_job(name=name)
    complete_job.status["conditions"] = [{"type": "Complete", "status": "True", "reason": ""}]
    _patch_seam(monkeypatch, get_job=GetJobSpy(complete_job))

    await reconcile_cloud_jobs(_make_ctx())

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
    # phaze-2o8p: reconcile now READS AnalysisResult.analysis_completed_at to recognise a callback-
    # completed file whose Job was TTL-GC'd, but it must never WRITE a result row -- the out-of-band
    # callback stays the sole authoritative result writer (KSUBMIT-03).
    assert "AnalysisResult(" not in src  # never constructs a result row
    assert "update(AnalysisResult" not in src  # never updates a result row
    assert "insert(AnalysisResult" not in src  # never inserts a result row

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
async def test_one_bad_row_does_not_abort_tick(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row whose kube read raises is logged + skipped; the remaining rows still reconcile."""
    _patch_cap(monkeypatch)
    fid_bad, name_bad = await _seed(session)
    fid_ok, name_ok = await _seed(session)

    class _Flaky:
        async def __call__(self, name: str, kube: Any = None) -> Any:  # noqa: ARG002 -- MKUE-01 seam signature
            if name == name_bad:
                raise RuntimeError("transient kube API error")
            return fake_job(succeeded=1, name=name)

    _, _, dj, _ = _patch_seam(monkeypatch, get_job=_Flaky())  # type: ignore[arg-type]

    tally = await reconcile_cloud_jobs(_make_ctx())

    # The good row still reconciled to success despite the bad row raising.
    assert (await _read_cloud_job(session, fid_ok)).status == CloudJobStatus.SUCCEEDED.value
    assert (await _read_cloud_job(session, fid_bad)).status == CloudJobStatus.SUBMITTED.value
    assert name_ok in dj.calls
    assert tally["reconciled"] == 2
    assert tally["succeeded"] == 1


@pytest.mark.asyncio
async def test_success_sets_cloud_phase_finished(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Succeeded Job writes cloud_phase=finished before its commit (D-04)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", DeleteJobSpy([]))
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(succeeded=1, name=name)))

    await reconcile_cloud_jobs(_make_ctx())

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value
    assert cj.cloud_phase == CloudPhase.FINISHED.value


def _patch_commit_marker(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Record a ``"commit"`` marker into the shared ``events`` list on every ``AsyncSession.commit``.

    Lets the ordering assertion prove the S3 delete precedes the AWAITING_CLOUD commit (the lock-release
    boundary) which precedes the post-commit Job delete: ``index(s3_delete) < index(commit) < index(delete_job)``.
    Reconcile issues exactly one commit for the single at-cap row under test.
    """
    original = AsyncSession.commit

    async def _spy(self: AsyncSession) -> None:
        events.append("commit")
        await original(self)

    monkeypatch.setattr(AsyncSession, "commit", _spy)


@pytest.mark.asyncio
async def test_clean_before_flip_ordering_delete_precedes_commit_precedes_job(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """At cap the S3 delete of the old object is recorded BEFORE the AWAITING_CLOUD commit and BEFORE delete_job (D-01/MKUE-04)."""
    _patch_cap(monkeypatch, cap=3)
    _fid, name = await _seed(session, attempts=3)  # next_attempt = 4 > cap -> the at-cap clean terminal
    events: list[str] = []
    dj = DeleteJobSpy(events)
    s3 = S3DeleteSpy(events)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)), delete_job=dj, s3_delete=s3)
    _patch_commit_marker(monkeypatch, events)

    await reconcile_cloud_jobs(_make_ctx())

    # The delete runs under the still-held lock, before the flip commit; the Job delete stays post-commit.
    assert events == ["s3_delete", "commit", "delete_job"]
    assert events.index("s3_delete") < events.index("commit") < events.index("delete_job")


@pytest.mark.asyncio
async def test_clean_before_flip_deletes_recorded_bucket_and_clears_it(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The delete targets the RECORDED staging_bucket (captured pre-mutation), and the row's staging_bucket is cleared to None (D-01/D-06)."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=3)
    _, _, _, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)))

    await reconcile_cloud_jobs(_make_ctx())

    # Deleted on exactly the recorded staging bucket -- never a re-derived one.
    assert s3.calls == [fid]
    assert s3.buckets == [_STAGING_BUCKET_ID]
    # The terminal row clears staging_bucket so no pre-repurpose reader is misled (T-70-04-04).
    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.AWAITING.value  # D-12: re-stamped 'awaiting' (NOT FAILED)
    assert cj.staging_bucket is None


@pytest.mark.asyncio
async def test_clean_before_flip_delete_is_best_effort(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising delete_staged_object is swallowed and does NOT block the spill / re-dispatch (D-03, T-70-04-02)."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=3)

    class _RaisingS3Delete:
        def __init__(self) -> None:
            self.calls: list[uuid.UUID] = []

        async def __call__(self, file_id: uuid.UUID, bucket: Any = None) -> None:  # noqa: ARG002 -- seam signature
            self.calls.append(file_id)
            raise RuntimeError("S3 unreachable")

    s3 = _RaisingS3Delete()
    dj = DeleteJobSpy([])
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(failed=1, name=name)))
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", dj)
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)

    tally = await reconcile_cloud_jobs(_make_ctx())

    # The raising delete was attempted but swallowed -> the spill still committed and the Job still deleted.
    assert s3.calls == [fid]
    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.AWAITING.value  # D-12: 'awaiting', not FAILED
    assert dj.calls == [name]  # Job delete stays post-commit and still runs despite the swallowed S3 error
    assert tally["failed"] == 1
