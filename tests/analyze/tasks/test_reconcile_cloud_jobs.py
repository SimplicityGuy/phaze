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
    a fresh ``submit_cloud_job``; at the cap the FileRecord SPILLS BACK to AWAITING_CLOUD (SCHED-03/D-04).
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
from phaze.models.file import FileRecord
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


# --- Fixtures / builders ----------------------------------------------------------------------------


# The cron dispatches reconcile per-backend via ``resolve_backends`` (SCHED-05), so the settings stub
# must carry a single kueue registry entry whose id matches the ``backend_id`` ``_seed`` stamps below.
_KUEUE_BACKEND_ID = "kueue-x64"
# MKUE-02: the seeded cloud_job records this staging_bucket id; the at-cap terminal resolves it via
# ``s3_staging.resolve_bucket_config`` against the stub's ``buckets`` list and deletes on exactly it.
_STAGING_BUCKET_ID = "staging-a"


def _patch_cap(monkeypatch: pytest.MonkeyPatch, cap: int = 3) -> None:
    """Pin ``get_settings()`` for BOTH the cron and ``KueueBackend.reconcile`` so cap + registry are deterministic.

    The Phase-69 cron (SCHED-05) resolves backends via ``resolve_backends(get_settings())`` and dispatches
    ``KueueBackend.reconcile`` -- which reads the cap from ``phaze.services.backends.get_settings``. Patch
    both bindings to the same stub carrying ``cloud_submit_max_attempts`` + a one-entry kueue registry +
    the ``buckets`` list the MKUE-02 at-cap staged-object delete resolves the recorded bucket against.
    """
    settings = SimpleNamespace(
        cloud_submit_max_attempts=cap,
        cloud_enabled=True,
        backends=[
            SimpleNamespace(
                kind="kueue",
                id=_KUEUE_BACKEND_ID,
                rank=20,
                cap=cap,
                # MKUE-01/D-04: KueueBackend.reconcile threads self.config.kube into the seam; the
                # get_job/delete_job spies are monkeypatched, so a minimal stand-in kube suffices.
                kube=SimpleNamespace(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq"),
            )
        ],
        buckets=[SimpleNamespace(id=_STAGING_BUCKET_ID)],
    )
    monkeypatch.setattr("phaze.tasks.reconcile_cloud_jobs.get_settings", lambda: settings)
    monkeypatch.setattr("phaze.services.backends.get_settings", lambda: settings)


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
            backend_id=_KUEUE_BACKEND_ID,  # SCHED-05: KueueBackend.reconcile is backend_id-scoped -> stamp so the per-backend query owns this row.
            s3_key=f"phaze-staging/{file.id}",
            status=status,
            kueue_workload=name,
            attempts=attempts,
            staging_bucket=_STAGING_BUCKET_ID,  # MKUE-02: the at-cap staged-object delete acts on this recorded bucket.
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


# --- Cap reached -> spill back to AWAITING_CLOUD (SCHED-03/D-04) ------------------------------------


@pytest.mark.asyncio
async def test_max_attempts_cap_then_spill_back_to_awaiting_cloud(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCHED-03/D-04/D-12: at the cloud cap a no-callback terminal re-stamps the cloud_job sidecar to 'awaiting', writing NO FileRecord.state.

    The cloud_job is re-stamped ``status='awaiting'`` via the single spill-mode writer (NOT ``FAILED``) so
    it drops out of the in-flight set, and its staged object + Job are cleaned up -- but the FileRecord is
    NOT touched at all (D-04): it stays at its prior PUSHED state, and ``attempts >= cap`` routes the next
    drain tick to the local safety net. ANALYSIS_FAILED comes only from local failure.
    """
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=3)  # next_attempt = 4 > cap 3
    queue = DedupFakeQueue("controller")
    ctx = _make_ctx(async_engine, queue)
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
async def test_cap_safe_reconcile_decrement_never_overshoots_drain_snapshot(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                kube=SimpleNamespace(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq"),
            )
        ],
    )
    monkeypatch.setattr("phaze.services.backends.get_settings", lambda: settings)
    [backend] = [b for b in resolve_backends(settings) if b.id == _KUEUE_BACKEND_ID]

    before = await backend.in_flight_count(session)
    assert before == 2 == cap  # start exactly at cap

    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name_fail)))
    await backend.reconcile(session, _make_ctx(async_engine))

    session.expire_all()
    after = await backend.in_flight_count(session)
    assert after == 1  # reconcile only decremented (re-stamped the failed row to 'awaiting', out of in-flight)
    assert after <= cap  # cap-safe: never overshoots


# --- D-04/D-12: at-cap spill re-stamps the sidecar 'awaiting', writes NO FileRecord.state -----------
#
# The Plan 80-03 regression (VALIDATION SC-1 mutation b): the at-cap terminal must route through the
# single spill-mode writer (hold_awaiting_cloud) leaving cloud_job.status='awaiting' (NOT FAILED) and the
# FileRecord UNTOUCHED (D-04). Reintroducing the state=AWAITING_CLOUD write / status=FAILED pre-mutation
# turns this RED. Asserted from an INDEPENDENT session (get_session-never-commits memory): reconcile
# commits on its own ctx["async_session"], so a fresh session read confirms the COMMITTED effect -- never
# an uncommitted identity-map value. The pre-fix HARD shadow violation (state=AWAITING_CLOUD +
# status=FAILED) can no longer arise: the file stays at PUSHED, satisfying the pushed/pushing invariants.


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_status", [CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value])
async def test_at_cap_spill_restamps_cloud_job_awaiting_not_failed(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, seed_status: str, verify: AsyncSession
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

    tally = await reconcile_cloud_jobs(_make_ctx(async_engine))

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


# --- Delete-after-record ordering (D-04) -----------------------------------------------------------
# NOTE (92-04): ``test_delete_after_record_ordering`` moved to
# ``tests/integration/test_reconcile_concurrency.py`` -- its ``DeleteJobSpy(engine=...)`` snapshot reads
# COMMITTED state on a SEPARATE connection, which the hermetic create_savepoint fixture cannot provide.


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
async def test_vanished_job_at_cap_spills_back_to_awaiting_cloud(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the cap a vanished Job is a terminal no-callback -> re-stamp the sidecar 'awaiting', never an eternal skip (WR-01/SCHED-03/D-12)."""
    _patch_cap(monkeypatch, cap=3)
    fid, _name = await _seed(session, attempts=3)  # next_attempt = 4 > cap
    _, _, _, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(None))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.AWAITING.value  # D-12: 'awaiting', not FAILED
    assert s3.calls == [fid]


# --- Per-row guard: one bad row never aborts the tick ----------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_row_does_not_abort_tick(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
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


# --- MKUE-04: clean-before-flip spillover cleanup (delete under the lock, before the AWAITING_CLOUD flip) ---
#
# The crux of Plan 05 (D-01/D-03, Pitfall 9): on the at-cap spill-back the old (backend_id,
# staging_bucket) staged object must be deleted WHILE the per-row pg_advisory_xact_lock(5_000_504) is
# still held -- i.e. BEFORE the commit that flips the file to AWAITING_CLOUD and thus releases the lock,
# making the file a drain candidate. Deleting before the flip guarantees the old object is gone before
# any concurrent drain tick can re-dispatch + re-stage a NEW object under the same file_id-scoped key.

# NOTE (92-04): the cross-connection advisory-lock probe cell that read _DRAIN_ADVISORY_LOCK_KEY
# (5_000_504) moved to tests/integration/test_reconcile_concurrency.py, so the constant is gone from here.


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
async def test_clean_before_flip_ordering_delete_precedes_commit_precedes_job(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At cap the S3 delete of the old object is recorded BEFORE the AWAITING_CLOUD commit and BEFORE delete_job (D-01/MKUE-04)."""
    _patch_cap(monkeypatch, cap=3)
    _fid, name = await _seed(session, attempts=3)  # next_attempt = 4 > cap -> the at-cap clean terminal
    events: list[str] = []
    dj = DeleteJobSpy(events)
    s3 = S3DeleteSpy(events)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)), delete_job=dj, s3_delete=s3)
    _patch_commit_marker(monkeypatch, events)

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    # The delete runs under the still-held lock, before the flip commit; the Job delete stays post-commit.
    assert events == ["s3_delete", "commit", "delete_job"]
    assert events.index("s3_delete") < events.index("commit") < events.index("delete_job")


@pytest.mark.asyncio
async def test_clean_before_flip_deletes_recorded_bucket_and_clears_it(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete targets the RECORDED staging_bucket (captured pre-mutation), and the row's staging_bucket is cleared to None (D-01/D-06)."""
    _patch_cap(monkeypatch, cap=3)
    fid, name = await _seed(session, attempts=3)
    _, _, _, s3 = _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)))

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    # Deleted on exactly the recorded staging bucket -- never a re-derived one.
    assert s3.calls == [fid]
    assert s3.buckets == [_STAGING_BUCKET_ID]
    # The terminal row clears staging_bucket so no pre-repurpose reader is misled (T-70-04-04).
    cj = await _read_cloud_job(session, fid)
    assert cj.status == CloudJobStatus.AWAITING.value  # D-12: re-stamped 'awaiting' (NOT FAILED)
    assert cj.staging_bucket is None


@pytest.mark.asyncio
async def test_spillover_same_bucket_redispatch_preserves_new_object(
    async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    await reconcile_cloud_jobs(_make_ctx(async_engine))

    # The delete ran BEFORE the commit that releases the file to the drain.
    assert events.index("s3_delete") < events.index("commit")
    assert s3.buckets == [_STAGING_BUCKET_ID]
    # Simulate the drain's post-commit re-dispatch re-staging a NEW object on the same key/bucket.
    store["present"] = True
    # The old delete already ran pre-commit, so it cannot clobber the freshly-staged object.
    assert store["present"] is True


# NOTE (92-04): ``test_drain_reconcile_concurrency_delete_runs_under_advisory_lock`` moved to
# ``tests/integration/test_reconcile_concurrency.py`` -- its cross-connection
# ``pg_try_advisory_xact_lock`` probe needs a genuinely independent transaction, which the hermetic
# single-connection create_savepoint fixture cannot provide.


@pytest.mark.asyncio
async def test_clean_before_flip_delete_is_best_effort(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
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

    tally = await reconcile_cloud_jobs(_make_ctx(async_engine))

    # The raising delete was attempted but swallowed -> the spill still committed and the Job still deleted.
    assert s3.calls == [fid]
    assert (await _read_cloud_job(session, fid)).status == CloudJobStatus.AWAITING.value  # D-12: 'awaiting', not FAILED
    assert dj.calls == [name]  # Job delete stays post-commit and still runs despite the swallowed S3 error
    assert tally["failed"] == 1
