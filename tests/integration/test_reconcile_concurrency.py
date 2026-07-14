"""D-04 ordering + drain-lock concurrency for ``reconcile_cloud_jobs`` (real PG, 92-04).

Moved here from ``tests/analyze/tasks/test_reconcile_cloud_jobs.py`` by plan 92-04 (Option B). Both cells
prove a property that only exists ACROSS independent, committed-visible DB connections:

* ``test_delete_after_record_ordering`` -- a ``DeleteJobSpy(engine=...)`` opens a SEPARATE connection at
  Job-delete time and reads the COMMITTED ``cloud_job`` state, proving the outcome was committed BEFORE
  the Kube Job was deleted (D-04).
* ``test_drain_reconcile_concurrency_delete_runs_under_advisory_lock`` -- from a SEPARATE connection a
  ``pg_try_advisory_xact_lock`` on the drain key must FAIL while reconcile holds it during the delete,
  then SUCCEED after the reconcile transaction commits.

The hermetic single-connection ``create_savepoint`` ``session`` fixture (92-03) cannot express either:
reconcile's writes are never truly committed (savepoint release inside an uncommitted outer txn) and a
second connection would read ZERO/STALE. So these live on the real-PG ``committed_db`` fixture where
reconcile runs on its OWN pool connection and the probe/snapshot connection sees committed truth.

Test-local helpers (``GetJobSpy`` / ``DeleteJobSpy`` / ``S3DeleteSpy`` / ``_patch_cap`` / ``_patch_seam``
/ ``_seed`` / ...) are COPIED from the donor (which still uses them for its own hermetic cells); only the
ctx/engine acquisition changed. Assertions are preserved verbatim.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.tasks.reconcile_cloud_jobs import reconcile_cloud_jobs
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter
from tests.kube_fakes import fake_job


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


pytestmark = pytest.mark.integration


# SCHED-05: KueueBackend.reconcile is backend_id-scoped; MKUE-02: the at-cap staged-object delete acts on
# the recorded staging_bucket. Copied from the donor.
_KUEUE_BACKEND_ID = "kueue-x64"
_STAGING_BUCKET_ID = "staging-a"
# The drain's transaction-scoped advisory-lock key reconcile takes across the clean-before-flip delete.
_DRAIN_ADVISORY_LOCK_KEY = 5_000_504


# --- Seam spies (copied from the donor) ------------------------------------------------------------


class GetJobSpy:
    """Monkeypatch stand-in for ``kube_staging.get_job`` returning a per-call canned Job."""

    def __init__(self, *responses: Any) -> None:
        self._responses = responses if responses else (None,)
        self.calls: list[str] = []

    async def __call__(self, name: str, kube: Any = None) -> Any:  # noqa: ARG002 -- MKUE-01 seam signature
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append(name)
        return self._responses[idx]


class DeleteJobSpy:
    """Monkeypatch stand-in for ``kube_staging.delete_job`` -- records call order + a committed DB snapshot.

    When an ``engine`` is supplied, each call snapshots the committed ``cloud_job`` state at delete time
    (on its OWN connection) -- proving the outcome was committed BEFORE the Job delete (D-04).
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
                snap: dict[str, Any] = {"cloud_status": getattr(cj, "status", None), "attempts": getattr(cj, "attempts", None)}
                self.snapshots.append(snap)


class S3DeleteSpy:
    """Monkeypatch stand-in for ``s3_staging.delete_staged_object`` -- records call order + file ids."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[uuid.UUID] = []
        self.buckets: list[str] = []

    async def __call__(self, file_id: uuid.UUID, bucket: Any = None) -> None:
        self.calls.append(file_id)
        self.buckets.append(getattr(bucket, "id", None))
        self.events.append("s3_delete")


# --- Fixtures / builders (copied from the donor) ---------------------------------------------------


def _patch_cap(monkeypatch: pytest.MonkeyPatch, cap: int = 3) -> None:
    """Pin ``get_settings()`` for BOTH the cron and ``KueueBackend.reconcile`` so cap + registry are deterministic."""
    settings = SimpleNamespace(
        cloud_submit_max_attempts=cap,
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
        buckets=[SimpleNamespace(id=_STAGING_BUCKET_ID)],
    )
    monkeypatch.setattr("phaze.tasks.reconcile_cloud_jobs.get_settings", lambda: settings)
    monkeypatch.setattr("phaze.services.backends.get_settings", lambda: settings)


def _patch_seam(
    monkeypatch: pytest.MonkeyPatch,
    *,
    get_job: GetJobSpy,
    delete_job: DeleteJobSpy | None = None,
    s3_delete: Any | None = None,
) -> None:
    """Monkeypatch the kube/S3 seam functions reconcile calls."""
    dj = delete_job or DeleteJobSpy([])
    s3 = s3_delete or S3DeleteSpy([])
    monkeypatch.setattr("phaze.services.kube_staging.get_job", get_job)
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", dj)
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)


def _make_ctx(engine: AsyncEngine, queue: DedupFakeQueue | None = None) -> dict[str, Any]:
    """Build a controller-shaped ctx whose ``async_session`` is a REAL sessionmaker on ``engine``.

    Each ``async with ctx["async_session"]()`` opens its OWN pool connection (like the production
    controller), so reconcile's transaction + advisory lock are genuinely independent of the probe's.
    """
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": queue or DedupFakeQueue("controller"), "task_router": DedupFakeTaskRouter()}


def _make_file() -> FileRecord:
    """Build a fully-populated FileRecord (the cloud_job FK target); copied from the donor."""
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


async def _seed_fk_fileserver(session: AsyncSession) -> None:
    """Seed the ``test-fileserver`` FK-parent agent (in the hermetic suite ``async_engine`` seeds it once)."""
    session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
    await session.commit()


async def _seed(session: AsyncSession, *, status: str = CloudJobStatus.SUBMITTED.value, attempts: int = 0) -> tuple[uuid.UUID, str]:
    """Seed a FileRecord + its in-flight cloud_job; return ``(file_id, kueue_workload_name)`` (copied from the donor)."""
    file = _make_file()
    session.add(file)
    await session.flush()
    name = f"phaze-analyze-{file.id}"
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            backend_id=_KUEUE_BACKEND_ID,
            s3_key=f"phaze-staging/{file.id}",
            status=status,
            kueue_workload=name,
            attempts=attempts,
            staging_bucket=_STAGING_BUCKET_ID,
        )
    )
    await session.commit()
    return file.id, name


async def _read_cloud_job(session: AsyncSession, file_id: uuid.UUID) -> CloudJob:
    session.expire_all()
    return (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()


# --- Delete-after-record ordering (D-04) -----------------------------------------------------------


async def test_delete_after_record_ordering(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-callback terminal ordering: outcome committed BEFORE Job delete; S3 delete BEFORE Job delete."""
    engine, session_factory = committed_db
    _patch_cap(monkeypatch, cap=3)
    async with session_factory() as session:
        await _seed_fk_fileserver(session)
        _fid, name = await _seed(session, attempts=3)  # at cap -> the clean terminal ordering path
    events: list[str] = []
    dj = DeleteJobSpy(events, engine=engine)
    s3 = S3DeleteSpy(events)
    _patch_seam(monkeypatch, get_job=GetJobSpy(fake_job(failed=1, name=name)), delete_job=dj, s3_delete=s3)

    await reconcile_cloud_jobs(_make_ctx(engine))

    # S3 delete precedes Job delete.
    assert events == ["s3_delete", "delete_job"]
    # The outcome was already committed when the Job delete fired (the snapshot reads committed state):
    # cloud_job re-stamped 'awaiting' (D-12), attempts NOT incremented (still cap=3), FileRecord UNTOUCHED (PUSHED, D-04).
    assert dj.snapshots == [{"cloud_status": CloudJobStatus.AWAITING.value, "attempts": 3}]


# --- Drain-lock concurrency (Pitfall 2/9) ----------------------------------------------------------


async def test_drain_reconcile_concurrency_delete_runs_under_advisory_lock(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clean-before-flip delete executes WHILE reconcile holds pg_advisory_xact_lock(5_000_504) (Pitfall 2/9).

    Proof that a concurrent drain cannot claim the file until reconcile commits: from a SEPARATE session,
    ``pg_try_advisory_xact_lock`` on the drain's key must FAIL during the delete (reconcile holds it),
    then SUCCEED after the txn commits. Since the drain takes the same lock across its whole candidate
    claim, no file can end assigned to two backends and no object the new pod needs is deleted.
    """
    engine, session_factory = committed_db
    _patch_cap(monkeypatch, cap=3)
    async with session_factory() as session:
        await _seed_fk_fileserver(session)
        fid, name = await _seed(session, attempts=3)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    probe_states: dict[str, bool] = {}

    class _LockProbingS3Delete:
        def __init__(self) -> None:
            self.calls: list[uuid.UUID] = []

        async def __call__(self, file_id: uuid.UUID, bucket: Any = None) -> None:  # noqa: ARG002 -- seam signature
            self.calls.append(file_id)
            # From a distinct connection, try to grab the drain lock reconcile is currently holding.
            async with sm() as probe:
                got = (await probe.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _DRAIN_ADVISORY_LOCK_KEY})).scalar()
                probe_states["during_delete"] = bool(got)
                await probe.rollback()

    s3 = _LockProbingS3Delete()
    monkeypatch.setattr("phaze.services.kube_staging.get_job", GetJobSpy(fake_job(failed=1, name=name)))
    monkeypatch.setattr("phaze.services.kube_staging.delete_job", DeleteJobSpy([]))
    monkeypatch.setattr("phaze.services.s3_staging.delete_staged_object", s3)

    await reconcile_cloud_jobs(_make_ctx(engine))

    # During the delete the lock was held by reconcile -> the concurrent probe could NOT acquire it.
    assert probe_states["during_delete"] is False
    # After the reconcile txn commits, the lock is free again.
    async with sm() as probe:
        got = (await probe.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _DRAIN_ADVISORY_LOCK_KEY})).scalar()
        assert bool(got) is True
        await probe.rollback()
    assert s3.calls == [fid]
    # Single-owner: staging_bucket cleared, no double assignment. The FileRecord is UNTOUCHED (D-04): the
    # sidecar re-stamp to 'awaiting' is what makes the file a drain candidate, not a FileRecord.state write.
    async with session_factory() as session:
        assert (await _read_cloud_job(session, fid)).staging_bucket is None
