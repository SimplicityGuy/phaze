"""D-01 golden side-effect snapshot for ``stage_cloud_window`` -- the BACK-04 acceptance gate.

This is a **characterization test captured on the CURRENT post-67 code** (D-01: the a1/k8s dispatch
paths were never deployed live, so the code IS the reference -- there is no prod trace to match). It
drives the UNMODIFIED ``stage_cloud_window`` over the matrix ``{compute, kueue, local} x {agent up,
agent down}`` and pins the observable side-effect log per cell against an INLINE expected-dict.

The Phase-68 behavior-preserving refactor (Waves 1-3) re-homes the ``if active_cloud_kind ==
compute/kueue`` fork into a ``Backend`` protocol. This snapshot asserts the OBSERVABLE side effects
(gate checked-vs-skipped, staging call, FileState transition, cloud_job upsert, enqueue task, tally),
NOT the internal branch structure -- so it stays green across the refactor and PROVES the re-home
changed nothing (that is the BACK-04 proof).

D-01a asymmetry (a first-class assertion here): **compute requires a live compute agent** (GATE-1 in
``stage_cloud_window``) while **kueue deliberately skips that gate** (ephemeral Kueue pods, no
persistent compute agent). The snapshot records exactly which agent-gate kinds the drain requested per
cell (the compute GATE-1 via the per-entry ``select_agent_by_id`` since Phase 72, the fileserver GATE-2
via ``select_active_agent``), so ``compute`` appears for the compute cell and is ABSENT for the kueue cell.

Forward-compatible mocking (kept green across the refactor without adding a tracked side effect):
``services.kube_staging.get_local_queue`` is stubbed to resolve "available". It is UNCALLED on
current code (harmless), but the post-refactor ``KueueBackend.is_available`` probes it during the
drain; stubbing it now keeps the snapshot green after Wave 2 lands. The ONLY tracked gate observation
is the agent-selector call log (compute GATE-1 ``select_agent_by_id`` + fileserver GATE-2
``select_active_agent``), never the kube probe.

The ONE tracked expected value that legitimately changes across the phase is the compute-cell
``cloud_job`` upsert: it is ABSENT on current code (``tasks/push.py`` writes no ``cloud_job`` row) and
becomes PRESENT once D-03/D-08 land the in-txn compute ``cloud_job`` write in Wave 3 (plan 68-04).
That single expected field carries a ``TODO(68-04)`` marker below; EVERY other asserted field must
stay byte-identical -- that byte-identity is the BACK-04 characterization proof.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.services import backends as backends_mod, enqueue_router, kube_staging, s3_staging
from phaze.tasks import release_awaiting_cloud
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent
from tests.kube_fakes import fake_local_queue


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


# --- registry-derived settings stub (matches what the CURRENT drain reads) --------------


class _StubCfg:
    """Stand-in for the registry-derived reads ``stage_cloud_window`` makes.

    The Phase-68 refactor drives the cron off ``cloud_enabled`` (the on/off gate) + the resolved
    ``backends`` registry (``resolve_backends(cfg)`` yields the single non-local dispatch backend, whose
    ``.cap`` is the former ``active_cap``). Per cell we set one non-local backend of the cell's kind; the
    local cell sets ``cloud_enabled=False`` (the implicit all-local registry). ``active_cap`` /
    ``active_cloud_kind`` stay as legacy shims for the readers not yet rewired, but the drain now reads
    ``backends``. Each ``backends`` entry duck-types the Phase-67 submodel fields
    (``kind`` / ``id`` / ``rank`` / ``cap``) that ``resolve_backends`` binds each impl to.
    """

    def __init__(self, *, active_cap: int, cloud_enabled: bool, active_cloud_kind: str | None) -> None:
        self.active_cap = active_cap
        self.cloud_enabled = cloud_enabled
        self.active_cloud_kind = active_cloud_kind
        # Phase 69: the tiered drain routes each candidate through the pure select_backend policy, which
        # reads these two bounded knobs (D-04 attempt-exclusion + D-01/D-03 staleness gate on local
        # spill). The single-non-local cells here never exercise either path (0 attempts, no local
        # backend), so the golden side-effect baseline stays byte-identical -- that is the BACK-04 proof.
        self.cloud_submit_max_attempts = 3
        self.cloud_spill_to_local_after_seconds = 900
        # Phase 70 (MKUE-02): KueueBackend.dispatch picks a per-file bucket over ``config.buckets`` and
        # resolves its BucketConfig via ``resolve_bucket_config(get_settings(), id)`` -- so carry a
        # ``buckets`` registry and bind the kueue backend entry to it (a no-op for compute/local cells).
        self.buckets = [SimpleNamespace(id="staging-1", bucket="phaze-staging")]
        if active_cloud_kind is None:
            self.backends = [SimpleNamespace(kind="local", id="local", rank=0, cap=active_cap)]
        elif active_cloud_kind == "kueue":
            # Phase 70 (MKUE-01/D-04): KueueBackend.is_available/reconcile thread self.config.kube into
            # kube_staging; carry a minimal kube (the get_local_queue seam is stubbed in _run_cell).
            self.backends = [
                SimpleNamespace(
                    kind="kueue",
                    id="kueue-1",
                    rank=10,
                    cap=active_cap,
                    buckets=["staging-1"],
                    kube=SimpleNamespace(api_url="https://kube.test", namespace="phaze", local_queue="phaze-lq"),
                )
            ]
        else:
            # Phase 72 (MCOMP-01/D-02): a compute backend's is_available resolves THIS entry's bound
            # ``agent_ref`` against Agent.id, so bind the compute stub to the ``cloud-1`` compute agent
            # ``_run_cell`` seeds online when ``compute_up`` (the up/down axis).
            self.backends = [
                SimpleNamespace(
                    kind=active_cloud_kind,
                    id=f"{active_cloud_kind}-1",
                    rank=10,
                    cap=active_cap,
                    agent_ref="cloud-1",
                    # Phase 73 (D-02): compute dispatch stamps push_host/scratch_dir off the bound config.
                    push_host="cloud-1.push.example",
                    scratch_dir="/srv/scratch",
                    ssh_user=None,
                )
            ]


def _make_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated AWAITING_CLOUD FileRecord row."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=FileState.AWAITING_CLOUD,
    )


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter) -> dict[str, Any]:
    """Build the controller-shaped ctx the cron consumes (async_session + task_router)."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": DedupFakeQueue("controller"), "task_router": router}


def _spy_select_active_agent(calls: list[str]) -> Any:
    """Wrap the REAL ``select_active_agent`` so we record the kinds the DRAIN requests (D-01a).

    Delegating to the real selector keeps behavior identical to production (a genuine DB lookup that
    raises ``NoActiveAgentError`` when the agent is absent); the wrapper only appends the requested
    ``kind``. Patched on ``release_awaiting_cloud`` this records the drain's own GATE-2 fileserver
    lookup. ``cloud_staging``'s internal fileserver lookup uses its own module reference and is
    deliberately NOT recorded (the tracked observation is a GATE check, not an internal agent lookup).
    """
    real = enqueue_router.select_active_agent

    async def _wrapped(session: AsyncSession, kind: str | None = None) -> Any:
        calls.append(kind if kind is not None else "<any>")
        return await real(session, kind=kind)

    return _wrapped


def _spy_backends_gate1(calls: list[str]) -> Any:
    """Wrap the REAL ``select_agent_by_id`` on the ``backends`` module, recording the compute GATE-1 probe.

    Phase 72 (MCOMP-01/D-02) moved the compute GATE-1 off the kind-ordered ``select_active_agent(
    kind="compute")`` onto the per-entry ``select_agent_by_id(agent_ref, kind="compute")`` -- each
    compute backend now resolves ITS bound agent by id, not "the freshest compute agent". The gate still
    lives inside ``ComputeAgentBackend.is_available`` (the ``services.backends`` module reference), so we
    spy that reference to keep the D-01a observation byte-identical across the seam move, recording every
    ``kind=="compute"`` GATE-1 probe. ``select_agent_by_id`` is ONLY called by that gate (dispatch's
    fileserver lookup still goes through ``select_active_agent``), so no internal dispatch lookup leaks
    into the tally. Kueue's ``is_available`` probes the cluster (kube), never this selector, so
    ``compute`` correctly never appears for the kueue cell.
    """
    real = enqueue_router.select_agent_by_id

    async def _wrapped(session: AsyncSession, agent_id: str, *, kind: str | None = None) -> Any:
        if kind == "compute":
            calls.append(kind)
        return await real(session, agent_id, kind=kind)

    return _wrapped


async def _run_cell(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str | None,
    compute_up: bool,
    active_cap: int = 2,
    held: int = 3,
) -> dict[str, Any]:
    """Drive the UNMODIFIED ``stage_cloud_window`` for one matrix cell; return its side-effect log.

    ``kind`` selects the cell (``"compute"`` / ``"kueue"`` / ``None`` for local); ``compute_up``
    toggles whether a compute agent is online (the up/down axis -- the fileserver is always online so
    a non-local cell that clears GATE-1 can actually stage).
    """
    cloud_enabled = kind is not None
    stub = _StubCfg(active_cap=active_cap, cloud_enabled=cloud_enabled, active_cloud_kind=kind)
    monkeypatch.setattr(release_awaiting_cloud, "get_settings", lambda: stub)
    # Phase 70 (MKUE-02): KueueBackend.dispatch resolves the picked bucket via ``backends.get_settings()``;
    # pin it to the SAME stub so ``resolve_bucket_config`` finds the stub's ``buckets`` registry.
    monkeypatch.setattr(backends_mod, "get_settings", lambda: stub)

    # D-01a spy: record the gate kinds. GATE-2 (fileserver) fires through the drain's own reference
    # (select_active_agent); GATE-1 (compute) now fires through ComputeAgentBackend.is_available via the
    # per-entry select_agent_by_id (Phase 72). Spy BOTH into one shared ordered list -- the backends spy
    # records ONLY the compute GATE-1 probe.
    gate_kinds: list[str] = []
    monkeypatch.setattr(release_awaiting_cloud, "select_active_agent", _spy_select_active_agent(gate_kinds))
    monkeypatch.setattr(backends_mod, "select_agent_by_id", _spy_backends_gate1(gate_kinds))

    # Real staging bodies run (strongest golden capture): stub the S3 SDK the kueue core calls.
    monkeypatch.setattr(s3_staging, "create_multipart_upload", AsyncMock(return_value="upload-xyz"))
    monkeypatch.setattr(s3_staging, "presign_upload_parts", AsyncMock(return_value=["https://s3.test/part?1"]))
    # Forward-compatible: stub the Kueue LocalQueue probe boundary the post-refactor
    # KueueBackend.is_available will call. Uncalled on current code -> harmless; NOT a tracked side
    # effect (the tracked gate observation is the select_active_agent call log only).
    monkeypatch.setattr(kube_staging, "get_local_queue", AsyncMock(return_value=fake_local_queue()))

    # The fileserver (push initiator) is always online; the compute agent follows the up/down axis.
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    if compute_up:
        await seed_active_agent(session, agent_id="cloud-1", kind="compute")

    files = [_make_file() for _ in range(held)]
    session.add_all(files)
    await session.commit()
    # Phase 83 (D-05): every held AWAITING_CLOUD file carries a cloud_job(status='awaiting') sidecar row --
    # the sidecar drain INNER-joins on it (no FileRecord.state read, SC#1). A dispatched file's row is
    # upserted in place (on_conflict_do_update); a held file keeps its awaiting row, so cloud_job_count
    # equals the held count in every cell.
    for f in files:
        session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=CloudJobStatus.AWAITING.value))
    await session.commit()
    ids = [f.id for f in files]

    router = DedupFakeTaskRouter()
    tally = await release_awaiting_cloud.stage_cloud_window(_make_ctx(async_engine, router))

    # Re-read committed FileState truth for the seeded rows.
    session.expire_all()
    rows = (await session.execute(select(FileRecord).where(FileRecord.id.in_(ids)))).scalars().all()
    state_counts = {"pushing": 0, "awaiting_cloud": 0}
    for r in rows:
        if r.state == FileState.PUSHING:
            state_counts["pushing"] += 1
        elif r.state == FileState.AWAITING_CLOUD:
            state_counts["awaiting_cloud"] += 1

    # Distinct enqueue task names that landed on any per-agent queue (push_file vs s3_upload).
    staging_tasks = sorted({task for q in router.queues.values() for task, _ in q.captured})

    cloud_job_count = int((await session.execute(select(func.count(CloudJob.id)))).scalar() or 0)

    return {
        "gate_kinds": list(gate_kinds),
        "compute_gate_checked": "compute" in gate_kinds,
        "staging_tasks": staging_tasks,
        "state_counts": state_counts,
        "cloud_job_count": cloud_job_count,
        "tally": tally,
    }


# The 6-cell golden matrix. Each inline expected-dict is the captured baseline on current post-67
# code; the refactor must leave every field byte-identical EXCEPT the one TODO-marked compute field.
_COMPUTE_UP_EXPECTED = {
    "gate_kinds": ["compute", "fileserver"],  # D-01a: compute checks GATE-1 then GATE-2
    "compute_gate_checked": True,
    "staging_tasks": ["push_file"],  # compute rsync-push leg
    "state_counts": {"pushing": 2, "awaiting_cloud": 1},
    # Phase 83 (D-05): every held file now carries a cloud_job(status='awaiting') sidecar row (the drain's
    # INNER-join candidacy). ComputeAgentBackend.dispatch upserts the 2 staged files' rows in place
    # (on_conflict_do_update -> SUBMITTED); the 1 held file keeps its awaiting row -> 3 cloud_job rows total.
    "cloud_job_count": 3,
    "tally": {"staged": 2, "skipped": 0},
}

_COMPUTE_DOWN_EXPECTED = {
    "gate_kinds": ["compute"],  # GATE-1 holds and returns before GATE-2 is reached
    "compute_gate_checked": True,
    "staging_tasks": [],
    "state_counts": {"pushing": 0, "awaiting_cloud": 3},
    "cloud_job_count": 3,  # Phase 83: 3 held files -> 3 retained awaiting rows (nothing dispatched)
    "tally": {"staged": 0, "skipped": 0},  # compute+down -> no-op hold (GATE-1)
}

_KUEUE_UP_EXPECTED = {
    "gate_kinds": ["fileserver"],  # D-01a: kueue SKIPS the compute gate entirely
    "compute_gate_checked": False,
    "staging_tasks": ["s3_upload"],  # kueue S3-staging leg
    "state_counts": {"pushing": 2, "awaiting_cloud": 1},
    "cloud_job_count": 3,  # Phase 83: 2 staged rows upserted (UPLOADING) + 1 retained awaiting row
    "tally": {"staged": 2, "skipped": 0},
}

# kueue+down is byte-identical to kueue+up: the compute agent is absent but IRRELEVANT (GATE-1
# skipped), so the kueue cell stages regardless -- that IS the D-01a asymmetry.
_KUEUE_DOWN_EXPECTED = dict(_KUEUE_UP_EXPECTED)

_LOCAL_EXPECTED = {
    "gate_kinds": [],  # cloud_enabled=False -> clean no-op BEFORE the advisory lock / any gate
    "compute_gate_checked": False,
    "staging_tasks": [],
    "state_counts": {"pushing": 0, "awaiting_cloud": 3},
    "cloud_job_count": 3,  # Phase 83: cloud_enabled=False no-op -> 3 held files keep their awaiting rows
    "tally": {"staged": 0, "skipped": 0},
}


@pytest.mark.parametrize(
    ("kind", "compute_up", "expected"),
    [
        pytest.param("compute", True, _COMPUTE_UP_EXPECTED, id="compute-agent-up"),
        pytest.param("compute", False, _COMPUTE_DOWN_EXPECTED, id="compute-agent-down"),
        pytest.param("kueue", True, _KUEUE_UP_EXPECTED, id="kueue-agent-up"),
        pytest.param("kueue", False, _KUEUE_DOWN_EXPECTED, id="kueue-agent-down"),
        pytest.param(None, True, _LOCAL_EXPECTED, id="local-agent-up"),
        pytest.param(None, False, _LOCAL_EXPECTED, id="local-agent-down"),
    ],
)
@pytest.mark.asyncio
async def test_dispatch_snapshot_matches_golden(
    kind: str | None,
    compute_up: bool,
    expected: dict[str, Any],
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed side-effect log for each matrix cell equals its inline golden baseline."""
    observed = await _run_cell(async_engine, session, monkeypatch, kind=kind, compute_up=compute_up)
    assert observed == expected


@pytest.mark.asyncio
async def test_d01a_gate_asymmetry_is_explicit(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-01a first-class assertion: compute CHECKS the compute gate; kueue does NOT.

    Both cells run with the compute agent DOWN. The compute cell requests ``select_active_agent(
    kind="compute")`` and holds ({"staged": 0}); the kueue cell never requests ``kind="compute"`` and
    proceeds to stage (GATE-1 deliberately skipped for ephemeral Kueue pods).
    """
    compute = await _run_cell(async_engine, session, monkeypatch, kind="compute", compute_up=False)
    assert compute["compute_gate_checked"] is True
    assert "compute" in compute["gate_kinds"]
    assert compute["tally"] == {"staged": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_d01a_kueue_skips_compute_gate_and_proceeds(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kueue half of the D-01a asymmetry: no compute gate, stages even with the compute agent down."""
    kueue = await _run_cell(async_engine, session, monkeypatch, kind="kueue", compute_up=False)
    assert kueue["compute_gate_checked"] is False
    assert "compute" not in kueue["gate_kinds"]
    assert kueue["tally"]["staged"] == 2
