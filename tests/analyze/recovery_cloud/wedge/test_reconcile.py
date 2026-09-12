"""Pod/workload wedge and phantom-workload observation."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
import pathlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.services import kube_staging
import phaze.tasks.reconcile_cloud_jobs as reconcile_mod
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from tests._backends_patch import patch_backends_get_settings
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter
from tests.kube_fakes import ADMITTED, fake_job, fake_pod


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


async def _backdate(session: AsyncSession, file_id: uuid.UUID, seconds: int) -> None:
    """Push the row's ``updated_at`` clock ``seconds`` into the past.

    ``updated_at`` carries ``onupdate=func.now()``, but an UPDATE that names the column explicitly
    uses the supplied value. Post-phaze-202e this only matters to the pending-submit bound; a
    Job-backed row's age is not an input to ANY decision in reconcile.
    """
    cj = await _read_cloud_job(session, file_id)
    now = datetime.now(UTC)
    stamped = cj.updated_at
    if stamped is not None and stamped.tzinfo is None:
        now = now.replace(tzinfo=None)
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(updated_at=now - timedelta(seconds=seconds)))
    await session.commit()


@pytest.mark.parametrize("age_seconds", [3600, 10800, 4 * 3600, 6 * 3600, 30 * 86400])
@pytest.mark.asyncio
async def test_running_pod_is_never_terminalized_regardless_of_age(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, age_seconds: int) -> None:
    """phaze-202e ACCEPTANCE / THE INVARIANT: a Running pod survives any age.

    THE REGRESSION TEST for the 2026-07-28 incident, at the reconcile layer. The parametrization walks
    a 3h row (the old ``activeDeadlineSeconds``), a 4-6 h concert set (the real work that was being
    killed) and a month. Every one of them must come out RUNNING with ``attempts == 0``: no Job delete,
    no S3 delete, no re-drive enqueue. Restoring ANY age-based terminal for a Job-backed row turns the
    later parametrizations red.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    await _backdate(session, fid, age_seconds)
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Running")),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.RUNNING.value
    assert cj.attempts == 0  # no cloud attempt burned on a healthy long analyze
    assert dj.calls == []
    assert s3.calls == []
    assert queue.captured == []
    assert tally["running"] == 1
    assert tally["redriven"] == 0
    assert tally["failed"] == 0


@pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "InvalidImageName"])
@pytest.mark.asyncio
async def test_dead_before_start_pod_terminalizes_immediately(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """phaze-202e ACCEPTANCE: a pod that can never start is a no-callback terminal -> bounded re-drive.

    This is the phaze-1b39 wedge shape (a bad image, or the missing operator ConfigMap/Secret the pod's
    ``envFrom`` needs) preserved WITHOUT a wall clock. Note the row is deliberately FRESH -- age is not
    consulted at all, so the verdict lands on the very first tick instead of hours later.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    queue = DedupFakeQueue("controller")
    # Non-terminal Job on the first read, gone on the confirm-gone read (the re-drive race guard).
    _, _, dj, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1), None),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Pending", waiting_reason=reason)),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1  # a real terminal outcome, not another silent RUNNING re-affirm
    assert cj.status == CloudJobStatus.SUBMITTED.value  # re-driven
    assert dj.calls == [name]  # the wedged Job was deleted
    assert [t for t, _ in queue.captured] == ["submit_cloud_job"]
    assert tally["redriven"] == 1
    assert tally["running"] == 0


@pytest.mark.asyncio
async def test_dead_before_start_pod_at_cap_spills_and_frees_the_lane_slot(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-202e ACCEPTANCE: at cap the wedged row spills to 'awaiting' -- the cap SLOT IS RECOVERED.

    ``'awaiting'`` is NOT an in-flight status, so ``in_flight_count`` drops and the burst-lane slot the
    un-startable pod was squatting on is returned -- the "lane returns to 0 without kubectl surgery"
    half of the phaze-1b39 acceptance, carried forward intact.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value, attempts=3)  # at cap
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Pending", waiting_reason="CreateContainerConfigError")),
    )

    tally = await reconcile_cloud_jobs(_make_ctx())

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.AWAITING.value  # D-12 spill: no longer in-flight -> cap slot freed
    assert cj.attempts == 3  # budget-spent marker, not double-counted
    assert s3.calls == [fid]  # the staged object of the abandoned burst is cleaned up
    assert dj.calls == [name]
    assert tally["failed"] == 1


@pytest.mark.asyncio
async def test_persistently_unschedulable_pod_terminalizes(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pod unschedulable past the scheduling probe is a wedge; the clock is the POD CONDITION's."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    stamp = (datetime.now(UTC) - timedelta(seconds=kube_staging.UNSCHEDULABLE_PROBE_SECONDS + 60)).isoformat().replace("+00:00", "Z")
    queue = DedupFakeQueue("controller")
    _, _, dj, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1), None),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Pending", unschedulable_since=stamp)),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    assert (await _read_cloud_job(session, fid)).attempts == 1
    assert dj.calls == [name]
    assert tally["redriven"] == 1


@pytest.mark.asyncio
async def test_briefly_unschedulable_pod_is_held(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside the scheduling probe the row is held -- a normal autoscaler scale-up must not burn an attempt."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    stamp = (datetime.now(UTC) - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Pending", unschedulable_since=stamp)),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    assert (await _read_cloud_job(session, fid)).attempts == 0
    assert dj.calls == []
    assert s3.calls == []
    assert queue.captured == []
    assert tally["running"] == 1


@pytest.mark.asyncio
async def test_empty_pod_list_alone_never_terminalizes(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty pod list is UNKNOWN, not dead -- a pod-label drift must not mass-kill the burst lane.

    ``list_pods_for_job`` returns ``[]`` both for a genuinely pod-less Job AND for a cluster whose pod
    labels this code does not recognise. Here the Job independently reports ``active=1`` (a pod DOES
    exist), so the zero-pod probe is refused outright and the row is simply re-affirmed RUNNING, no
    matter how old it is.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    await _backdate(session, fid, 30 * 86400)
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(),  # empty
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    assert (await _read_cloud_job(session, fid)).attempts == 0
    assert dj.calls == []
    assert s3.calls == []
    assert queue.captured == []
    assert tally["running"] == 1


@pytest.mark.asyncio
async def test_unsuspended_job_with_no_pod_past_the_probe_terminalizes(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 'Workload wedged Admitted with no pod' shape: un-suspended, zero active pods, past the probe.

    Safe without a run clock precisely because nothing is running: the Job itself reports
    ``status.active == 0`` AND the pod list is empty AND ``spec.suspend`` is false AND
    ``status.startTime`` is older than the probe. All four must agree before the row is terminalized.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    started = (datetime.now(UTC) - timedelta(seconds=reconcile_mod.NO_POD_PROBE_SECONDS + 60)).isoformat().replace("+00:00", "Z")
    queue = DedupFakeQueue("controller")
    _, _, dj, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=0, start_time=started), None),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    assert (await _read_cloud_job(session, fid)).attempts == 1
    assert dj.calls == [name]
    assert tally["redriven"] == 1


@pytest.mark.asyncio
async def test_unsuspended_job_with_no_pod_inside_the_probe_is_held(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Job un-suspended seconds ago has not had time to create its pod -- hold, do not kill."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    started = (datetime.now(UTC) - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=0, start_time=started)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(),
    )

    tally = await reconcile_cloud_jobs(_make_ctx())

    assert (await _read_cloud_job(session, fid)).attempts == 0
    assert dj.calls == []
    assert s3.calls == []
    assert tally["running"] == 1


@pytest.mark.asyncio
async def test_suspended_job_with_no_pod_is_never_terminalized(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A still-Kueue-gated Job has no pod BY DESIGN -- the zero-pod probe must never fire on it."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    started = (datetime.now(UTC) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    _, _, dj, _ = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, suspend=True, active=0, start_time=started)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(),
    )

    tally = await reconcile_cloud_jobs(_make_ctx())

    assert (await _read_cloud_job(session, fid)).attempts == 0
    assert dj.calls == []
    assert tally["running"] == 1


@pytest.mark.asyncio
async def test_wedged_pod_with_landed_callback_is_not_terminalized(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedge-looking row whose analysis result ALREADY landed must never be re-driven (would redo work).

    The callback (KSUBMIT-03) is the authoritative result writer and keys off ``file_id``; if
    ``analysis_completed_at`` is set the burst genuinely finished, so the wedge verdict stands down and
    leaves the row to the normal terminal paths.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    session.add(AnalysisResult(file_id=fid, analysis_completed_at=datetime.now(UTC).replace(tzinfo=None)))
    await session.commit()
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Pending", waiting_reason="ImagePullBackOff")),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    assert (await _read_cloud_job(session, fid)).attempts == 0
    assert dj.calls == []
    assert s3.calls == []
    assert queue.captured == []
    assert tally["running"] == 1


@pytest.mark.asyncio
async def test_first_admission_after_long_healthy_quota_wait_is_not_terminalized(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-uui9, carried forward: a Job admitted after a long healthy Pending quota wait is not killed.

    D-07 designs a healthy Pending quota wait as indefinite, and the Pending branch issues no UPDATE
    while waiting, so ``updated_at`` reflects the QUEUE wait rather than any run time. phaze-uui9 had to
    bolt a "only if already observed RUNNING" gate onto the 1b39 wall clock to stop that frozen clock
    from killing a just-admitted pod. phaze-202e deleted the gate along with the clock -- pod state is
    equally valid on the first admitted tick and the thousandth -- so this now passes structurally
    rather than by special case.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, status=CloudJobStatus.SUBMITTED.value)  # never yet observed RUNNING
    await _backdate(session, fid, 30 * 86400)  # a very long healthy quota wait
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1)),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Running")),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.RUNNING.value  # admitted and stamped RUNNING, not re-driven
    assert cj.attempts == 0  # no cloud attempt burned
    assert dj.calls == []  # the freshly admitted Job was NOT deleted
    assert s3.calls == []
    assert queue.captured == []
    assert tally["running"] == 1
    assert tally["redriven"] == 0


def test_reconcile_carries_no_wall_clock_deadline_reference() -> None:
    """phaze-202e guard: reconcile must not re-acquire a run deadline by reading the kube config.

    A source-level assertion because the failure mode is a silent reintroduction: any future
    ``kube.active_deadline_seconds`` read here would rebuild exactly the bound that caused the
    2026-07-28 incident. The pending-submit bound is the one permitted age rule, and it is named
    explicitly so this guard cannot be satisfied by renaming the old one back in.
    """
    source = pathlib.Path(reconcile_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    reads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "active_deadline_seconds"]

    assert reads == []
    assert "_staleness_cutoff_seconds" not in source
    assert reconcile_mod.PENDING_SUBMIT_CONFIRMATION_SECONDS > 0


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
async def test_stale_phantom_row_without_workload_terminalizes(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-1b39: a NULL-``kueue_workload`` row past the pending-submit bound is terminalized, not skipped forever.

    Pre-fix this row logged 'missing kueue_workload; skipping' on EVERY tick and stayed in-flight
    permanently -- an unrecoverable phantom holding a cap slot, since no terminal signal can ever exist
    for a Job that was never recorded. Post-fix it re-drives (under cap) with NO delete_job call (there
    is no Job to delete).
    """
    _patch_cap(monkeypatch, cap=3)
    fid = await _seed_phantom(session)
    await _backdate(session, fid, reconcile_mod.PENDING_SUBMIT_CONFIRMATION_SECONDS + 60)
    queue = DedupFakeQueue("controller")
    _, _, dj, _ = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert dj.calls == []  # no Job name -> nothing to delete (and no crash on a None name)
    assert [t for t, _ in queue.captured] == ["submit_cloud_job"]
    assert tally["redriven"] == 1


@pytest.mark.asyncio
async def test_stale_phantom_row_at_cap_spills_to_awaiting(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """At cap the phantom spills to 'awaiting' (out of the in-flight set) with no Job delete attempted."""
    _patch_cap(monkeypatch, cap=3)
    fid = await _seed_phantom(session, status=CloudJobStatus.RUNNING.value, attempts=3)
    await _backdate(session, fid, reconcile_mod.PENDING_SUBMIT_CONFIRMATION_SECONDS + 60)
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    tally = await reconcile_cloud_jobs(_make_ctx())

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.AWAITING.value
    assert s3.calls == [fid]
    assert dj.calls == []
    assert tally["failed"] == 1


@pytest.mark.asyncio
async def test_stale_phantom_row_with_landed_callback_records_success(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A phantom whose callback landed anyway (it keys off file_id, not the Job) finalizes as SUCCEEDED."""
    _patch_cap(monkeypatch, cap=3)
    fid = await _seed_phantom(session, status=CloudJobStatus.RUNNING.value)
    session.add(AnalysisResult(file_id=fid, analysis_completed_at=datetime.now(UTC).replace(tzinfo=None)))
    await session.commit()
    await _backdate(session, fid, reconcile_mod.PENDING_SUBMIT_CONFIRMATION_SECONDS + 60)
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    tally = await reconcile_cloud_jobs(_make_ctx())

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value
    assert cj.cloud_phase == CloudPhase.FINISHED.value
    assert dj.calls == []  # no Job to delete
    assert s3.calls == []  # success path makes ZERO S3 calls (the callback deleted the object inline)
    assert tally["succeeded"] == 1


@pytest.mark.asyncio
async def test_node_loss_seen_through_the_pod_wedge_charges_the_node_loss_budget(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wedge path classifies node loss too: an admitted Job whose pod died with its node.

    A node can take the pod down without the Job ever reading Failed (the Job is still Admitted and
    non-terminal). ``_pod_wedge_reason`` reaches the same verdict from pod state, so the same budget is
    charged whichever surface notices first.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=2)
    fid, name = await _seed(session, status=CloudJobStatus.RUNNING.value)
    queue = DedupFakeQueue("controller")
    _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name, active=1), None),
        get_workload=GetWorkloadSpy(ADMITTED),
        list_pods=ListPodsSpy(fake_pod("Failed", disruption_target_reason="DeletionByTaintManager")),
    )

    await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.node_loss_redrives == 1
    assert cj.attempts == 0


@pytest.mark.asyncio
async def test_absence_of_pods_is_never_read_as_node_loss(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vanished Job with no readable pods charges ``attempts``, NOT the node-loss budget.

    An empty pod list is indistinguishable from a pod label this code does not know, so inferring node
    loss from an ABSENCE would hand the whole burst lane the looser budget on a single label drift. The
    node-loss verdict requires a POSITIVE marker on a pod; without one, the ordinary meter runs.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, _name = await _seed(session)
    queue = DedupFakeQueue("controller")
    _patch_seam(monkeypatch, get_job=GetJobSpy(None), list_pods=ListPodsSpy())

    await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.node_loss_redrives == 0


@pytest.mark.asyncio
async def test_an_unreadable_pod_list_degrades_to_the_ordinary_budget(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising pod list must not abort a terminal the caller already decided on.

    The node-loss classification is a REFINEMENT of a terminal, so a kube hiccup during it may not
    escape: letting it raise would hand the row to the per-row rollback guard and leave it in-flight
    holding a burst-lane slot -- trading a bounded retry for a wedged row. It degrades to "cannot prove
    node loss" -> charge ``attempts``, exactly as before phaze-1q4g.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, name = await _seed(session)
    queue = DedupFakeQueue("controller")

    async def _raising_list_pods(job_name: str, kube: Any = None) -> list[Any]:
        raise RuntimeError("kube API unreachable")

    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name), None))
    monkeypatch.setattr("phaze.services.kube_staging.list_pods_for_job", _raising_list_pods)

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1  # the terminal still happened -- the row is NOT left wedged in-flight
    assert cj.node_loss_redrives == 0
    assert tally["redriven"] == 1
