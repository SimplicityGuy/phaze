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
from sqlalchemy import func as sa_func, select, update

from phaze.config_backends import KubeConfig
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services import kube_staging
import phaze.tasks.submit_cloud_job as submit_mod
from phaze.tasks.submit_cloud_job import submit_cloud_job, submit_cloud_job_key
from phaze.telemetry import bootstrap, slots


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


# SCHED-05 / MKUE-01: the submit resolves the file's backend via cloud_job.backend_id against the
# registry; the stub carries a single kueue entry whose id matches the seeded row's backend_id.
_KUEUE_BACKEND_ID = "kueue-x64"

# phaze-w15ju: the stub registry's per-backend concurrency cap, and therefore the exclusive bound on
# a burst telemetry slot. Deliberately > 1 so "two concurrent submissions get DIFFERENT slots" is a
# real claim rather than a pool that could only ever hand out 0.
_KUEUE_CAP = 4

# A fully-populated KubeConfig for the spy to build a real manifest from. ``_patch_settings``'s
# KubeConfig deliberately omits the image/request fields (nothing in those tests POSTs), and
# ``build_job_manifest`` fails loud without them.
_MANIFEST_KUBE = KubeConfig(
    api_url="https://kube.example.com",
    namespace="phaze",
    local_queue="phaze-lq",
    job_image="phaze/job-runner:test",
    cpu_request="1500m",
    memory_request="3Gi",
)


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
        # phaze-w15ju: the REAL manifest this submit would have POSTed, built here from the same
        # arguments the seam would have passed to ``build_job_manifest``. Recording the manifest
        # rather than the slot integer is deliberate -- the acceptance criterion is about what
        # reaches the pod, and the allocator's own return value is exactly the bookkeeping the
        # criterion says not to assert against.
        self.manifests: list[dict[str, Any]] = []

    async def __call__(
        self, file_id: uuid.UUID, kube: Any, *, telemetry_slot: int | None = None, telemetry_slot_max: int | None = None
    ) -> tuple[str, str]:
        self.calls.append(file_id)
        self.kubes.append(kube)
        self.manifests.append(
            kube_staging.build_job_manifest(file_id, _MANIFEST_KUBE, telemetry_slot=telemetry_slot, telemetry_slot_max=telemetry_slot_max)
        )
        return self.name, self.uid

    def injected_env(self, index: int) -> dict[str, str]:
        """The container ``env`` of the ``index``-th recorded manifest, as a plain mapping."""
        return {entry["name"]: entry["value"] for entry in self.manifests[index]["spec"]["template"]["spec"]["containers"][0]["env"]}


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin ``submit_cloud_job.get_settings`` to a one-kueue registry whose id == the seeded backend_id."""
    kube = KubeConfig(api_url="https://kube.example.com", namespace="phaze", local_queue="phaze-lq")
    # ``cap`` is not decoration here: phaze-w15ju derives the telemetry slot bound from the kueue
    # backends' caps (``burst_telemetry_slots.burst_slot_bound``), so a registry stub without one is
    # not a registry entry the submit path can read.
    settings = SimpleNamespace(backends=[SimpleNamespace(kind="kueue", id=_KUEUE_BACKEND_ID, kube=kube, cap=_KUEUE_CAP)])
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
    # well in the past. Bind a tz-AWARE value: since phaze-cz3m / migration 049 every timestamp
    # column is timestamptz, and a NAIVE datetime bound to one is silently reinterpreted as the
    # session's local time rather than UTC -- a same-shape defect to the one 049 fixed.
    outage_time = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=12)
    await session.execute(update(CloudJob).where(CloudJob.file_id == fid).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    # phaze-30ssq: read the reference from the SAME TRANSACTION as the write -- func.now() is
    # transaction-start time, not the Python wall clock -- rather than a wall-clock read plus a
    # slack fudge that fails whenever the suite is slow enough to blow it.
    before_resubmit = (await session.execute(select(sa_func.now()))).scalar_one()

    await submit_cloud_job(ctx, fid)

    session.expire_all()
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_resubmit, "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"


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
async def test_late_submit_against_a_running_row_leaves_the_live_job_alone(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-exxt: a SAQ retry racing a row reconcile already advanced to RUNNING must NOT delete the
    live Job.

    Unlike ``AWAITING``/``SUCCEEDED``/``FAILED`` (phaze-kzto, no live owner), a ``RUNNING`` row proves a
    live owner already exists for the deterministic Job name -- e.g. this exact scenario: a worker
    crashes after ``submit_cloud_job``'s first attempt commits, SAQ's stuck-job sweep retries, and in
    the interim reconcile advances the row SUBMITTED -> RUNNING. The retry's CAS correctly refuses (not
    in {uploaded, submitted}), but pre-fix the rowcount==0 arm unconditionally deleted the Job it just
    POSTed (409-refreshed onto the live one) -- killing an in-flight analysis and burning a cloud
    attempt on the next reconcile tick's no-callback terminal.
    """
    file = _make_file()
    session.add(file)
    await session.commit()
    fid = file.id
    # Seed a row reconcile already advanced to RUNNING -- a live owner exists for the deterministic name.
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=fid,
            backend_id=_KUEUE_BACKEND_ID,
            s3_key=f"phaze-staging/{fid}",
            status=CloudJobStatus.RUNNING.value,
            kueue_workload=f"phaze-analyze-{fid}",
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
    assert rows[0].status == CloudJobStatus.RUNNING.value  # untouched
    # The live, in-flight Job is NOT deleted.
    assert deleted == []
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


# phaze-w15ju: the burst lane's telemetry identity, asserted where it actually lands


def _pod_identity(injected: dict[str, str]) -> tuple[str, int | None]:
    """Replay a burst pod on ``injected``; return its child's identity and any slot the POD allocated.

    **This walks the REAL consumer chain, not the manifest.** A Job env carrying a distinct
    ``PHAZE_TELEMETRY_SLOT`` per pod is necessary and was never sufficient: the pod runs
    ``run_analysis_subprocess``, which calls ``slots.assign(telemetry.child_environment())``, and a
    pod-local ``SlotPool`` -- a pool with exactly one competitor, because the pod shares memory with
    nobody -- would acquire index 0 and OVERWRITE the controller's decision on its way to the child.
    Every burst pod would then export under ``phaze-analysis-0`` behind a correct-looking manifest,
    which is exactly what ADR-0017 (telemetry export topology) section 8d predicted in writing and why a manifest-only assertion
    cannot close this bead.

    So this reproduces the pod: the injected env is the process environment, ``assign`` runs against
    a fresh pool, and ``bootstrap._instance_id`` reads the result. Three real functions; only
    ``os.environ`` is stood in for. The second element is what the pod allocated for ITSELF -- None
    when it correctly deferred to the controller's slot.
    """
    slots._reset_for_tests()
    environ, taken = slots.assign(dict(injected))
    return bootstrap._instance_id("phaze-analysis", environ), taken


@pytest.mark.asyncio
async def test_two_concurrent_burst_submissions_reach_the_child_with_distinct_identities(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACCEPTANCE 1+2. Two in-flight burst submits carry DIFFERENT bounded slots into their pods.

    Asserted against the built Job manifest and then through the pod-side chain, never against the
    allocator's return value -- the allocator's own bookkeeping is the thing the criterion says not
    to trust, because the host lane's version of this bookkeeping was correct while the identity the
    child actually used was not.

    Both rows stay in flight for the duration: neither is terminalized between the two submits, so
    the second allocation genuinely has to route around the first.
    """
    first, second = _make_file(), _make_file()
    session.add_all((first, second))
    await session.commit()
    await _seed_cloud_job(session, first.id)
    await _seed_cloud_job(session, second.id)
    _patch_settings(monkeypatch)

    spy = _SubmitSpy()
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)
    ctx = _make_ctx(async_engine)

    await submit_cloud_job(ctx, first.id)
    await submit_cloud_job(ctx, second.id)

    envs = [spy.injected_env(0), spy.injected_env(1)]
    slot_values = [env[slots.SLOT_ENV] for env in envs]
    assert slot_values == ["0", "1"]
    # The bound travels with the slot, and it is the registry's cap -- not slots.DEFAULT_SLOT_MAX,
    # which the pod would fall back to and which would refuse any index the cap raised above 4.
    assert {env[slots.SLOT_MAX_ENV] for env in envs} == {str(_KUEUE_CAP)}

    replayed = [_pod_identity(env) for env in envs]
    # The pod allocated NOTHING for itself: it deferred to the controller's slot. Without that
    # deference both pods would report `phaze-analysis-0` from their own pools of one.
    assert [taken for _identity, taken in replayed] == [None, None]
    identities = [identity for identity, _taken in replayed]
    # phaze-7nl67: in the BURST lane's own identity space, never the host lane's `phaze-analysis-<n>`.
    assert identities == ["phaze-analysis-burst-0", "phaze-analysis-burst-1"]
    assert len(set(identities)) == 2


@pytest.mark.asyncio
async def test_a_freed_slot_is_reissued_to_the_next_burst_submission(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACCEPTANCE 1 (the release half), at the submit boundary: index 0 comes back after completion.

    The pool is bounded by the concurrency and NOT by the corpus precisely because of this -- if a
    finished Job's slot were not reissued, the 11,428th file would need the 11,428th identity and the
    cardinality argument for the whole scheme would collapse.
    """
    finished, successor = _make_file(), _make_file()
    session.add_all((finished, successor))
    await session.commit()
    await _seed_cloud_job(session, finished.id)
    await _seed_cloud_job(session, successor.id)
    _patch_settings(monkeypatch)

    spy = _SubmitSpy()
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)
    ctx = _make_ctx(async_engine)

    await submit_cloud_job(ctx, finished.id)
    await session.execute(update(CloudJob).where(CloudJob.file_id == finished.id).values(status=CloudJobStatus.SUCCEEDED.value))
    await session.commit()

    await submit_cloud_job(ctx, successor.id)

    assert spy.injected_env(0)[slots.SLOT_ENV] == "0"
    assert spy.injected_env(1)[slots.SLOT_ENV] == "0"


@pytest.mark.asyncio
async def test_an_exhausted_pool_still_submits_the_job_without_a_slot(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instrumentation never fails the work: a slotless submit still POSTs, and the pod degrades.

    The manifest carries NO telemetry key in that case -- byte-identical to the pre-phaze-w15ju form
    -- so the pod falls back to allocating from its own pool of one and reports
    ``phaze-analysis-0``, exactly as every burst pod did before this bead. **That is the degraded
    state, stated rather than dressed up:** an over-cap pod merges with whichever pod holds slot 0.
    It is still the right direction to fail in, because the alternative -- minting an unbounded
    identity -- would put 2,290 series per analyzed file into storage phaze does not own, and that
    cannot be undone once scraped.
    """
    files = [_make_file() for _ in range(_KUEUE_CAP + 1)]
    session.add_all(files)
    await session.commit()
    for file in files:
        await _seed_cloud_job(session, file.id)
    _patch_settings(monkeypatch)

    spy = _SubmitSpy()
    monkeypatch.setattr("phaze.services.kube_staging.submit_job", spy)
    ctx = _make_ctx(async_engine)

    for file in files:
        await submit_cloud_job(ctx, file.id)

    overflow = spy.injected_env(_KUEUE_CAP)
    assert slots.SLOT_ENV not in overflow
    assert slots.SLOT_MAX_ENV not in overflow
    assert set(overflow) == kube_staging.JOB_ENV_CODE_INJECTED_ALWAYS
    # The pod allocates its own slot 0 -- the pre-fix behaviour, reached only past the cap.
    # Still inside the burst lane (phaze-7nl67): the lane key is unconditional, so an over-cap pod
    # merges with another burst pod, never with a host-lane child.
    assert _pod_identity(overflow) == ("phaze-analysis-burst-0", 0)
    # The submit itself still happened for every file, slot or no slot.
    assert len(spy.calls) == _KUEUE_CAP + 1
