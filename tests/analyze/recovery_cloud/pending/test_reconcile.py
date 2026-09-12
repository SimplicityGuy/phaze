"""Pending confirmation, quota holds, and observation without transition."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from tests._backends_patch import patch_backends_get_settings
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter
from tests.kube_fakes import EVICTED, PENDING, fake_job


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
async def test_pending_is_silent(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy Pending waits indefinitely: no attempts change, no inadmissible flag, no delete/enqueue."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(PENDING))

    tally = await reconcile_cloud_jobs(_make_ctx())

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert cj.attempts == 0
    assert cj.inadmissible is False
    assert dj.calls == []
    assert s3.calls == []
    assert tally["pending"] == 1


@pytest.mark.asyncio
async def test_pending_resubmit_after_redrive_is_not_double_charged(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION (phaze-32wz): a just-enqueued re-submit must not be re-read as a fresh vanished terminal.

    Tick 1 re-drives an Evicted row: attempts 0 -> 1, a fresh ``submit_cloud_job`` is enqueued onto the
    (capturing, non-executing) fake controller queue -- exactly as a real SAQ enqueue only schedules the
    work; the actual kube re-POST runs later, asynchronously, on the controller queue.

    Tick 2 reconciles the SAME row again BEFORE that enqueued submit has run (the fake queue never
    executes captured jobs, modelling the real controller-queue lag / backlog window). Pre-fix,
    ``cloud_job.kueue_workload`` still named the OLD (already-confirmed-gone) Job, so ``get_job`` on
    that name reads None again -- indistinguishable from a NEW no-callback terminal -- and
    ``_handle_no_callback_terminal`` fires a SECOND time, inflating ``attempts`` to 2 for a re-submit
    that never actually ran (let alone failed). A short loop of such ticks burns through the cap and
    spills a healthy file to local without a single real cloud Job ever executing in between.

    Post-fix, the re-drive clears ``kueue_workload`` in the SAME commit as the attempts bump, so tick 2
    reads the row as "pending confirmation" (the phantom-row hold path) and skips it with NO extra kube
    read, NO attempt increment, and NO spill -- until either the resubmit lands (new ``kueue_workload``)
    or the pending window itself goes stale (a SEPARATE, later, independently-charged attempt).
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    queue = DedupFakeQueue("controller")
    ctx = _make_ctx(queue)

    # Tick 1: Evicted Workload -> no-callback terminal -> under-cap re-drive.
    get_job, _, dj, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name), None),
        get_workload=GetWorkloadSpy(EVICTED),
    )
    tally_1 = await reconcile_cloud_jobs(ctx)
    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert cj.kueue_workload is None  # phaze-32wz: cleared -- the durable "pending confirmation" record
    assert dj.calls == [name]  # the prior (now-gone) Job was deleted as part of the re-drive
    assert len(get_job.calls) == 2  # terminal read + confirm-gone
    assert [t for t, _ in queue.captured] == ["submit_cloud_job"]  # enqueued, NOT yet executed
    assert tally_1["redriven"] == 1

    # Tick 2: reconcile runs again while the enqueued submit_cloud_job is STILL pending (the fake queue
    # never runs captured jobs -- the real controller-queue equivalent of a backlog/lag window). The
    # get_job seam is NOT re-armed with a 3rd response: if this tick reads it at all, that alone proves
    # the pending-vs-vanished distinction failed to hold (pre-fix behaviour), and the spy would stick on
    # its last response (None) -- reproducing the double-charge.
    tally_2 = await reconcile_cloud_jobs(ctx)

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1  # NOT inflated to 2 -- no real terminal event happened between the ticks
    assert cj.status == CloudJobStatus.SUBMITTED.value  # still in-flight
    assert cj.status != CloudJobStatus.AWAITING.value  # no premature spill to local
    assert len(get_job.calls) == 2  # unchanged -- tick 2 never re-reads the OLD (gone) Job name
    assert queue.captured == [("submit_cloud_job", {"file_id": str(fid)})]  # no second re-drive enqueued
    assert tally_2["redriven"] == 0
    assert tally_2["failed"] == 0


@pytest.mark.asyncio
async def test_pending_resubmit_resolves_once_fresh_job_is_observed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-32wz: once the enqueued resubmit actually lands, the pending window closes and reconcile resumes normally.

    Mirrors ``submit_cloud_job``'s own upsert re-stamping ``kueue_workload`` back to the (unchanged,
    deterministic) Job name once its kube POST completes. A subsequent tick then reads a live Job again
    -- proving the pending marker is self-clearing, not a permanent hold.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    ctx = _make_ctx()

    # Tick 1: Evicted -> re-drive; kueue_workload cleared (pending confirmation).
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name), None), get_workload=GetWorkloadSpy(EVICTED))
    await reconcile_cloud_jobs(ctx)
    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.kueue_workload is None

    # The deferred submit_cloud_job "runs" -- re-stamps kueue_workload (deterministic name unchanged).
    await session.execute(update(CloudJob).where(CloudJob.file_id == fid).values(kueue_workload=name))
    await session.commit()

    # Tick 2: the fresh Job is now observable and healthy (Pending) -- normal reconcile resumes, no
    # extra attempt burned for the resolved pending window.
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(PENDING))
    tally = await reconcile_cloud_jobs(ctx)

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1  # unchanged -- resolving the pending window is not itself an attempt
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert tally["pending"] == 1


@pytest.mark.asyncio
async def test_pending_sets_cloud_phase_queued_behind_quota(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy-Pending Workload stamps cloud_phase=queued_behind_quota on a NULL-phase row (D-04)."""
    _patch_cap(monkeypatch)
    fid, name = await _seed(session)  # seeded cloud_phase is NULL (a fresh in-flight row)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(name=name)), get_workload=GetWorkloadSpy(PENDING))

    await reconcile_cloud_jobs(_make_ctx())

    assert (await _read_cloud_job(session, fid)).cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value


async def _seed_phantom(session: AsyncSession, *, status: str = CloudJobStatus.SUBMITTED.value, attempts: int = 0) -> uuid.UUID:
    """Seed an in-flight cloud_job row with NO ``kueue_workload`` -- the phantom shape (submit crashed mid-flight)."""
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=_KUEUE_BACKEND_ID,
            s3_key=f"phaze-staging/{file.id}",
            status=status,
            kueue_workload=None,
            attempts=attempts,
            staging_bucket=_STAGING_BUCKET_ID,
        )
    )
    await session.commit()
    return file.id


@pytest.mark.asyncio
async def test_fresh_phantom_row_without_workload_is_still_held(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A just-inserted row whose submit is still mid-flight is held (unchanged behaviour) -- never stolen."""
    _patch_cap(monkeypatch, cap=3)
    fid = await _seed_phantom(session)
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 0
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert dj.calls == []
    assert s3.calls == []
    assert queue.captured == []
