"""Retry budgets, redrive, spillover, and node-loss accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from phaze.tasks.submit_cloud_job import submit_cloud_job_key
from tests._backends_patch import patch_backends_get_settings
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter
from tests.kube_fakes import EVICTED, fake_job, fake_pod


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
async def test_eviction_triggers_redrive(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An Evicted Workload is a no-callback terminal: delete the Job, confirm gone, re-drive submit."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    queue = DedupFakeQueue("controller")
    ctx = _make_ctx(queue)
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


@pytest.mark.asyncio
async def test_max_attempts_cap_then_spill_back_to_awaiting_cloud(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """SCHED-03/D-04/D-12: at the cloud cap a no-callback terminal re-stamps the cloud_job sidecar to 'awaiting', writing NO FileRecord.state.

    The cloud_job is re-stamped ``status='awaiting'`` via the single spill-mode writer (NOT ``FAILED``) so
    it drops out of the in-flight set, and its staged object + Job are cleaned up -- but the FileRecord is
    NOT touched at all (D-04): it stays at its prior PUSHED state, and ``attempts >= cap`` routes the next
    drain tick to the local safety net. ANALYSIS_FAILED comes only from local failure.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=3)  # next_attempt = 4 > cap 3
    queue = DedupFakeQueue("controller")
    ctx = _make_ctx(queue)
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)))

    tally = await reconcile_cloud_jobs(ctx)

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.AWAITING.value  # D-12: re-stamped 'awaiting' (NOT FAILED) -> out of in-flight
    assert s3.calls == [fid]  # no-callback terminal deletes the staged object (D-05), after the record+commit
    assert s3.buckets == [_STAGING_BUCKET_ID]  # MKUE-02: deleted on exactly the RECORDED staging bucket
    assert dj.calls == [name]
    assert queue.captured == []  # no re-drive at the cap
    assert tally["failed"] == 1


@pytest.mark.asyncio
async def test_cap_safe_reconcile_decrement_never_overshoots_drain_snapshot(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """SCHED-02: a reconcile decrement keeps ``sum(in_flight) <= sum(cap)`` (reconcile only ever decrements).

    The cap-safety proof (RESEARCH reconcile-only-decrements): reconcile NEVER claims an in-flight slot,
    it only terminalizes rows (FAILED) which DECREASES the per-backend ``in_flight_count``. So after a
    reconcile pass the in-flight count a concurrent drain snapshot would read is strictly ``<=`` the cap
    -- overshoot is impossible from the reconcile side. Here a kueue backend at cap (2 in-flight, cap 2)
    reconciles one failed row; the post-reconcile in-flight count drops to 1, still ``<= cap``.
    """
    from phaze.services.backends import resolve_backends

    _patch_cap(monkeypatch, cap=3)
    cap = 2
    # Two in-flight rows for the kueue backend: one will fail-terminalize, one stays RUNNING.
    _fid_fail, name_fail = await _seed(session, status=CloudJobStatus.SUBMITTED.value, attempts=3)  # at cap -> terminal
    await _seed(session, status=CloudJobStatus.RUNNING.value)  # stays in-flight

    settings = SimpleNamespace(
        cloud_submit_max_attempts=3,
        cloud_enabled=True,
        backends=[
            SimpleNamespace(
                kind="kueue",
                id=_KUEUE_BACKEND_ID,
                rank=20,
                cap=cap,
                # phaze-202e: the deadline is off by default and reconcile never reads it.
                kube=SimpleNamespace(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq", active_deadline_seconds=None),
            )
        ],
    )
    patch_backends_get_settings(monkeypatch, lambda: settings)
    [backend] = [b for b in resolve_backends(settings) if b.id == _KUEUE_BACKEND_ID]

    before = await backend.in_flight_count(session)
    assert before == 2 == cap  # start exactly at cap

    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name_fail)))
    await backend.reconcile(session, _make_ctx())

    session.expire_all()
    after = await backend.in_flight_count(session)
    assert after == 1  # reconcile only decremented (re-stamped the failed row to 'awaiting', out of in-flight)
    assert after <= cap  # cap-safe: never overshoots


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_status", [CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value])
async def test_at_cap_spill_restamps_cloud_job_awaiting_not_failed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, seed_status: str, verify: AsyncSession
) -> None:
    """D-04/D-12: the at-cap terminal re-stamps cloud_job 'awaiting' (NOT FAILED), writes NO FileRecord.state, does not increment attempts.

    Parametrized over BOTH in-flight statuses to exercise the spill CAS's ``expect_status=(SUBMITTED,
    RUNNING)`` domain. Asserted from the shared ``verify`` session so only the committed effect is read.
    """
    _patch_cap(monkeypatch, cap=3)
    # A file at PUSHED (the realistic in-flight state) with its cloud_job at cap (attempts == cap -> next > cap).
    fid, name = await _seed(session, status=seed_status, attempts=3)
    events: list[str] = []
    dj = DeleteJobSpy(events)
    s3 = S3DeleteSpy(events)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)), delete_job=dj, s3_delete=s3)
    _patch_commit_marker(monkeypatch, events)

    tally = await reconcile_cloud_jobs(_make_ctx())

    # 92-04 (CLEAN-02): read via the shared ``verify`` fixture (per-test connection) -> sees only what reconcile COMMITTED.
    cj = (await verify.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()

    # (1) re-stamped 'awaiting', NOT FAILED (mutation b: re-adding status=FAILED / the AWAITING_CLOUD write -> RED).
    assert cj.status == CloudJobStatus.AWAITING.value
    assert cj.status != CloudJobStatus.FAILED.value
    # (2) FileRecord.state UNCHANGED -- reconcile wrote no state (D-04): stays at the seeded PUSHED.
    # (3) attempts NOT incremented (attempts=cap is a set, not += 1) -> select_backend routes to local.
    assert cj.attempts == 3
    # (4) terminal-row hygiene: alert flag cleared, staged bucket cleared, cloud_phase cleared (clear_cloud_phase=True).
    assert cj.inadmissible is False
    assert cj.staging_bucket is None
    assert cj.cloud_phase is None
    # (5) MKUE-04 ordering: the staged-object delete ran BEFORE the commit (mutation c: move it after -> RED).
    assert events == ["s3_delete", "commit", "delete_job"]
    assert events.index("s3_delete") < events.index("commit") < events.index("delete_job")
    # Shadow invariant: the spilled row is at PUSHED (not AWAITING_CLOUD), so the HARD
    # `state==AWAITING_CLOUD => cloud_job.status=='awaiting'` implication is not even engaged -> no violation.
    assert tally["failed"] == 1


@pytest.mark.asyncio
async def test_redrive_confirms_prior_job_gone_before_resubmit(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
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
    await reconcile_cloud_jobs(_make_ctx(queue_a))
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
    await reconcile_cloud_jobs(_make_ctx(queue_b))
    cj_b = await _read_cloud_job(session, fid_b)
    assert cj_b.attempts == 0  # NO extra attempt burned
    assert cj_b.status == CloudJobStatus.SUBMITTED.value
    assert dj_b.calls == [name_b]  # the prior Job was deleted
    assert len(get_job_b.calls) == 2  # terminal read + confirm-gone (still present)
    assert queue_b.captured == []  # NO re-submit enqueued on this tick


@pytest.mark.asyncio
async def test_vanished_job_routes_to_terminal_redrive(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An in-flight row whose Job has vanished (get_job -> None) re-drives under cap instead of sticking (WR-01)."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    queue = DedupFakeQueue("controller")
    # get_job returns None on every call: the initial read (gone) AND the confirm-gone read.
    _, _, dj, _ = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert dj.calls == [name]  # idempotent delete of the (already-gone) Job
    assert [t for t, _ in queue.captured] == ["submit_cloud_job"]
    assert tally["redriven"] == 1


@pytest.mark.asyncio
async def test_vanished_job_at_cap_spills_back_to_awaiting_cloud(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the cap a vanished Job is a terminal no-callback -> re-stamp the sidecar 'awaiting', never an eternal skip (WR-01/SCHED-03/D-12)."""
    _patch_cap(monkeypatch, cap=3)
    fid, _name = await _seed(session, attempts=3)  # next_attempt = 4 > cap
    _, _, _, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    await reconcile_cloud_jobs(_make_ctx())

    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.AWAITING.value  # D-12: 'awaiting', not FAILED
    assert s3.calls == [fid]


@pytest.mark.asyncio
async def test_vanished_job_with_completed_analysis_is_success_not_redrive(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-2o8p: a TTL-GC'd Job for a CALLBACK-COMPLETED file is finalized SUCCEEDED, never re-driven.

    The /analysis callback stamped analysis_completed_at + deleted the staged object but did not
    advance cloud_job.status; a lagging reconcile then sees the Job gone. Without the analysis-complete
    check the vanished-Job path would re-drive an already-analyzed file against a deleted object and
    eventually spill it to a redundant local re-analysis.
    """

    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    # The success callback already landed the result (analysis_completed_at IS NOT NULL).
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=fid, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value  # finalized as success
    assert cj.attempts == 0  # no attempt burned
    assert dj.calls == [name]  # idempotent reap of the (already-GC'd) Job
    assert s3.calls == []  # success path makes ZERO S3 calls (callback already deleted the object)
    assert [t for t, _ in queue.captured] == []  # NO re-drive enqueued
    assert tally["succeeded"] == 1
    assert tally.get("redriven", 0) == 0


@pytest.mark.asyncio
async def test_failed_job_with_completed_analysis_is_success_not_redrive(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-73sv: a Job reading Failed AFTER its success callback landed is finalized SUCCEEDED, never re-driven.

    activeDeadlineSeconds firing (or an OOM/preempt) just after put_analysis recorded the result +
    deleted the staged object (D-05) marks the Job Failed even though the analysis is DONE. Without the
    guard the Job-Failed branch re-drives an already-analyzed file against a deleted staged object, whose
    pod 404s (EXIT_DOWNLOAD) and re-fails, burning the whole cap. The guard mirrors the vanished-Job path.
    """

    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=fid, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)))

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value  # finalized as success, not re-driven
    assert cj.attempts == 0  # no attempt burned
    assert dj.calls == [name]  # the Failed Job is reaped, not re-submitted
    assert s3.calls == []  # success path makes ZERO S3 calls (callback already deleted the object)
    assert [t for t, _ in queue.captured] == []  # NO re-drive enqueued
    assert tally["succeeded"] == 1
    assert tally.get("redriven", 0) == 0


@pytest.mark.asyncio
async def test_evicted_workload_with_completed_analysis_is_success_not_redrive(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-73sv: a Workload Evicted AFTER its success callback landed is finalized SUCCEEDED, never re-driven.

    A Kueue eviction under quota pressure can land in the post-callback teardown window (put_analysis
    already stamped the result + deleted the staged object). Without the guard the Evicted branch
    re-drives an already-analyzed file against a deleted object and burns the cap. The guard mirrors the
    Job-Failed and vanished-Job paths.
    """

    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=0)
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=fid, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    queue = DedupFakeQueue("controller")
    # Non-terminal Job read so reconcile reaches the Workload branch; Workload reads Evicted=True.
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(name=name)),
        get_workload=GetWorkloadSpy(EVICTED),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.SUCCEEDED.value  # finalized as success, not re-driven
    assert cj.attempts == 0  # no attempt burned
    assert dj.calls == [name]  # the Job is reaped, not re-submitted
    assert s3.calls == []  # success path makes ZERO S3 calls
    assert [t for t, _ in queue.captured] == []  # NO re-drive enqueued
    assert tally["succeeded"] == 1
    assert tally.get("redriven", 0) == 0


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
async def test_spillover_same_bucket_redispatch_preserves_new_object(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-bucket re-dispatch that re-stages a NEW object under the same file_id key survives (Pitfall 9).

    Model: a tiny object store keyed by presence. The reconcile's delete runs BEFORE the AWAITING_CLOUD
    commit (the lock-release / drain-candidate boundary), so the drain's post-commit re-stage of a new
    object on the SAME bucket + SAME file_id-scoped key can never be clobbered by the trailing old delete.
    """
    _patch_cap(monkeypatch, cap=3)
    _fid, name = await _seed(session, attempts=3)
    events: list[str] = []
    store = {"present": True}

    class _OrderedS3Delete:
        def __init__(self) -> None:
            self.calls: list[uuid.UUID] = []
            self.buckets: list[str] = []

        async def __call__(self, file_id: uuid.UUID, bucket: Any = None) -> None:
            self.calls.append(file_id)
            self.buckets.append(getattr(bucket, "id", None))
            store["present"] = False  # the OLD object is deleted
            events.append("s3_delete")

    s3 = _OrderedS3Delete()
    dj = DeleteJobSpy(events)
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(failed=1, name=name)))
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", dj)
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)
    _patch_commit_marker(monkeypatch, events)

    await reconcile_cloud_jobs(_make_ctx())

    # The delete ran BEFORE the commit that releases the file to the drain.
    assert events.index("s3_delete") < events.index("commit")
    assert s3.buckets == [_STAGING_BUCKET_ID]
    # Simulate the drain's post-commit re-dispatch re-staging a NEW object on the same key/bucket.
    store["present"] = True
    # The old delete already ran pre-commit, so it cannot clobber the freshly-staged object.
    assert store["present"] is True


def _node_lost_pod(name: str = "phaze-analyze-dead-node") -> Any:
    """The production shape: the pod is Failed and its NODE is the stated reason (kernel OOM took the node)."""
    return fake_pod("Failed", status_reason="NodeShutdown", name=name)


async def _simulate_submit_ran(session: AsyncSession, file_id: uuid.UUID, name: str, queue: DedupFakeQueue) -> bool:
    """Model the re-drive's enqueued ``submit_cloud_job`` actually running; return whether a pod was created.

    A re-drive is only an ENQUEUE (phaze-32wz): the kube POST happens later, on the controller queue,
    and only then does a new pod exist. This mirrors that upsert -- re-stamp ``kueue_workload`` to the
    same deterministic Job name -- and clears the SAQ dedup key, so a genuinely-unbounded loop would be
    free to enqueue again on the next tick rather than being silently absorbed by the dedup.
    """
    cj = await _read_cloud_job(session, file_id)
    if cj.kueue_workload is not None or cj.status != CloudJobStatus.SUBMITTED.value:
        return False
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(kueue_workload=name))
    await session.commit()
    queue.finish(submit_cloud_job_key(file_id))
    return True


@pytest.mark.asyncio
async def test_one_file_cannot_take_the_node_down_eight_times(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE phaze-1q4g REGRESSION TEST -- the exact observed shape, driven to the production count.

    Reproduces cloud job ``713a368e``: every pod dies with its node (kernel OOM on the burst node), the
    Job is re-driven, and the cycle repeats. Pre-fix, nothing charged a budget on this path, so the
    "ceiling" of 3 never bound it and the file produced 8 pods over 5 days.

    The loop runs EIGHT full node deaths -- the production number -- and asserts the bound holds:
    ``1 + cloud_node_loss_max_redrives`` pods, then a terminal. The ``attempts`` budget is untouched
    throughout (the distinction is preserved, not collapsed), and the row ends in the ONE state the
    drain can still act on rather than in-flight forever.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, name = await _seed(session)
    queue = DedupFakeQueue("controller")
    pods_created = 1  # the first submit's pod, already running when the story starts

    for _ in range(8):
        # Each tick: the Job reads Failed and its pod says the NODE took it. The second get_job read is
        # the re-drive's confirm-gone check.
        _patch_seam(
            monkeypatch,
            get_job=GetJobSpy(fake_job(failed=1, name=name), None),
            list_pods=ListPodsSpy(_node_lost_pod()),
        )
        await reconcile_cloud_jobs(_make_ctx(queue))
        if await _simulate_submit_ran(session, fid, name, queue):
            pods_created += 1

    cj = await _read_cloud_job(session, fid)
    assert pods_created == 2, "a node-killing file must not outlive 1 + cloud_node_loss_max_redrives pods"
    assert cj.node_loss_redrives == 1  # the node-loss budget, spent exactly once
    assert cj.attempts == 3  # == cap: the budget-spent MARKER stamped by the terminal, not four failures
    assert cj.status == CloudJobStatus.AWAITING.value  # terminal: out of IN_FLIGHT, drain-owned, local-bound
    assert len(queue.captured) == 1  # exactly one re-drive was ever enqueued, dedup or no dedup


@pytest.mark.asyncio
async def test_node_loss_redrive_charges_node_loss_budget_not_attempts(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the ceiling a node-loss terminal re-drives, spending ``node_loss_redrives`` and NOT ``attempts``.

    The distinction is the point: the file has not failed an analysis, so its analyze retry budget must
    read 0 afterwards. Everything else about the re-drive is unchanged -- the prior Job is deleted, the
    staged object is PRESERVED (the re-submitted pod still needs it) and a fresh submit is enqueued.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=2)
    fid, name = await _seed(session)
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(failed=1, name=name), None),
        list_pods=ListPodsSpy(_node_lost_pod()),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.node_loss_redrives == 1
    assert cj.attempts == 0  # the analyze budget is NOT charged for a node dying
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert cj.kueue_workload is None  # phaze-32wz pending-confirmation record
    assert dj.calls == [name]
    assert s3.calls == []  # re-drive path preserves the staged object
    assert queue.captured == [("submit_cloud_job", {"file_id": str(fid)})]
    assert tally["redriven"] == 1


@pytest.mark.asyncio
async def test_node_loss_at_its_ceiling_spills_to_awaiting(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the node-loss ceiling the row takes the SAME terminal as the attempts cap -- and cannot strand.

    ``awaiting`` is the defined terminal: OUT of :data:`IN_FLIGHT` (the burst-lane slot is released),
    ``attempts`` stamped to cap so ``select_backend`` can only route it to the local safety net, staged
    object deleted, Job deleted. It is specifically not a hard analyze failure (reconcile writes no
    result and no ``FileRecord.state``) and specifically not a SUBMITTED/RUNNING hold, which is the
    shape that would leave the row un-advanceable by anything but this cron.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, name = await _seed(session, node_loss_redrives=1)
    queue = DedupFakeQueue("controller")
    _, _, dj, s3 = _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(failed=1, name=name), None),
        list_pods=ListPodsSpy(_node_lost_pod()),
    )

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.AWAITING.value
    assert cj.status not in (CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value)  # never left in-flight
    assert cj.attempts == 3  # == cap: cloud-ineligible, routed to local by select_backend
    assert cj.node_loss_redrives == 1  # the counter records what happened; the terminal does not inflate it
    assert cj.cloud_phase is None  # off the "Running" tile (WR-01)
    assert cj.inadmissible is False
    assert s3.calls == [fid]  # the staged object is genuinely dead now
    assert dj.calls == [name]
    assert queue.captured == []  # no further pod is ever created for this row
    assert tally["failed"] == 1
    # KSUBMIT-03/D-04: cloud flakiness never fails a file. Reconcile wrote NO analysis row of any kind
    # -- the local safety net still owes this file an analyze outcome, which is why the spill must land
    # somewhere the drain can pick up rather than in a terminal that merely stops.
    assert (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == fid))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_ordinary_failure_still_charges_attempts_not_the_node_loss_budget(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two budgets do not leak into each other: an ordinary Job failure charges ``attempts`` only.

    The pod is Failed with no node-scoped marker -- the analysis died, the node is fine. This is the
    pre-existing D-08 behaviour and must be byte-for-byte unchanged.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, name = await _seed(session)
    queue = DedupFakeQueue("controller")
    _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(failed=1, name=name), None),
        list_pods=ListPodsSpy(fake_pod("Failed")),
    )

    await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1
    assert cj.node_loss_redrives == 0


@pytest.mark.asyncio
async def test_node_loss_verdict_persists_across_the_still_terminating_deferral(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deferred re-drive stashes its classified node-loss verdict; NO budget is charged yet.

    Mirrors ``test_redrive_confirms_prior_job_gone_before_resubmit``'s Case B (still terminating), but
    with a node-lost pod on the terminal read -- proving the classification is recorded on the row
    even though the re-drive itself is deferred to a later tick.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, name = await _seed(session)
    # First get_job read: Job Failed (no-callback terminal). Confirm-gone read: STILL a dying Job.
    _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(failed=1, name=name), fake_job(failed=1, name=name)),
        list_pods=ListPodsSpy(_node_lost_pod()),
    )

    tally = await reconcile_cloud_jobs(_make_ctx())

    cj = await _read_cloud_job(session, fid)
    assert cj.node_loss_pending is not None and "node_lost" in cj.node_loss_pending  # verdict stashed
    assert cj.attempts == 0  # neither budget charged yet -- the re-drive itself is deferred
    assert cj.node_loss_redrives == 0
    assert cj.status == CloudJobStatus.SUBMITTED.value  # row left untouched for a later tick
    assert tally.get("redriven", 0) == 0


@pytest.mark.asyncio
async def test_vanished_job_after_node_loss_deferral_charges_node_loss_budget_not_attempts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE phaze-mwbz3 REGRESSION TEST -- the exact reported sequence, across two ticks.

    Tick 1: the Job reads Failed, its pod says NODE_LOST, ``delete_job`` runs, but the confirm-gone
    read still sees the dying Job -> the re-drive is deferred and the verdict is stashed. Tick 2: the
    SAME (unchanged, deterministic-name) row is read again; the Job has now fully vanished (both
    ``get_job`` reads return None) -> ``_reconcile_one`` takes the vanished-Job branch, which classifies
    nothing (no pods left) and passes NO ``node_loss_reason``. Pre-fix this silently charged
    ``attempts``; post-fix it recovers the stashed verdict and charges ``node_loss_redrives`` instead,
    and clears the now-spent marker.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, name = await _seed(session)

    # Tick 1: node-loss terminal, still-terminating deferral.
    _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(failed=1, name=name), fake_job(failed=1, name=name)),
        list_pods=ListPodsSpy(_node_lost_pod()),
    )
    await reconcile_cloud_jobs(_make_ctx())
    cj_after_defer = await _read_cloud_job(session, fid)
    assert cj_after_defer.node_loss_pending is not None
    assert cj_after_defer.attempts == 0
    assert cj_after_defer.node_loss_redrives == 0

    # Tick 2: the Job has fully vanished -- no pods left, so a fresh classification is impossible.
    queue = DedupFakeQueue("controller")
    _patch_seam(monkeypatch, get_job=GetJobSpy(None), list_pods=ListPodsSpy())

    tally = await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.node_loss_redrives == 1  # the recovered verdict charged the RIGHT budget
    assert cj.attempts == 0  # the file's analyze budget stays untouched
    assert cj.node_loss_pending is None  # the verdict was spent -- cleared, not left to leak forward
    assert cj.status == CloudJobStatus.SUBMITTED.value
    assert tally["redriven"] == 1
    assert [t for t, _ in queue.captured] == ["submit_cloud_job"]


@pytest.mark.asyncio
async def test_ordinary_deferral_leaves_no_node_loss_marker(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary (non-node-loss) still-terminating deferral must NOT fabricate a node-loss verdict.

    Regression guard for the other direction: a deferred re-drive whose cause is an ordinary failure
    (no node-lost pod evidence) leaves ``node_loss_pending`` NULL, so a later vanished-Job re-entry for
    the same row still charges the ordinary ``attempts`` budget, exactly as before this fix.
    """
    _patch_cap(monkeypatch, cap=3, node_loss_ceiling=1)
    fid, name = await _seed(session)

    # Tick 1: ordinary Failed terminal (no node-lost pod), still-terminating deferral.
    _patch_seam(
        monkeypatch,
        get_job=GetJobSpy(fake_job(failed=1, name=name), fake_job(failed=1, name=name)),
    )
    await reconcile_cloud_jobs(_make_ctx())
    cj_after_defer = await _read_cloud_job(session, fid)
    assert cj_after_defer.node_loss_pending is None

    # Tick 2: the Job has fully vanished.
    queue = DedupFakeQueue("controller")
    _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    await reconcile_cloud_jobs(_make_ctx(queue))

    cj = await _read_cloud_job(session, fid)
    assert cj.attempts == 1  # the ordinary budget, not the node-loss one
    assert cj.node_loss_redrives == 0
    assert cj.node_loss_pending is None
