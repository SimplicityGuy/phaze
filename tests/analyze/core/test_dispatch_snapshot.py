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
persistent compute agent). The snapshot records exactly which ``select_active_agent`` kinds the drain
requested per cell, so ``compute`` appears for the compute cell and is ABSENT for the kueue cell.

Forward-compatible mocking (kept green across the refactor without adding a tracked side effect):
``services.kube_staging.get_local_queue`` is stubbed to resolve "available". It is UNCALLED on
current code (harmless), but the post-refactor ``KueueBackend.is_available`` probes it during the
drain; stubbing it now keeps the snapshot green after Wave 2 lands. The ONLY tracked gate observation
is the ``select_active_agent`` call log, never the kube probe.

The ONE tracked expected value that legitimately changes across the phase is the compute-cell
``cloud_job`` upsert: it is ABSENT on current code (``tasks/push.py`` writes no ``cloud_job`` row) and
becomes PRESENT once D-03/D-08 land the in-txn compute ``cloud_job`` write in Wave 3 (plan 68-04).
That single expected field carries a ``TODO(68-04)`` marker below; EVERY other asserted field must
stay byte-identical -- that byte-identity is the BACK-04 characterization proof.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord, FileState
from phaze.services import enqueue_router, kube_staging, s3_staging
from phaze.tasks import release_awaiting_cloud
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent
from tests.kube_fakes import fake_local_queue


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


# --- registry-derived settings stub (matches what the CURRENT drain reads) --------------


class _StubCfg:
    """Stand-in for the registry-derived reads ``stage_cloud_window`` makes on current code.

    The Phase-67 rewire drives the cron off ``cloud_enabled`` (the on/off gate), ``active_cap`` (the
    former ``cloud_max_in_flight``) and the transitional ``active_cloud_kind`` (``"compute"`` /
    ``"kueue"`` / ``None``). Per cell we set a single non-local backend of the cell's kind; the local
    cell sets ``cloud_enabled=False`` (the implicit all-local registry).
    """

    def __init__(self, *, active_cap: int, cloud_enabled: bool, active_cloud_kind: str | None) -> None:
        self.active_cap = active_cap
        self.cloud_enabled = cloud_enabled
        self.active_cloud_kind = active_cloud_kind


def _make_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated AWAITING_CLOUD FileRecord row."""
    uid = uuid.uuid4()
    return FileRecord(
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
    """Wrap the REAL ``select_active_agent`` so we record the kinds the drain requests (D-01a).

    Delegating to the real selector keeps behavior identical to production (a genuine DB lookup that
    raises ``NoActiveAgentError`` when the agent is absent); the wrapper only appends the requested
    ``kind`` so the snapshot can assert the compute gate was checked (compute cell) vs skipped (kueue
    cell). Only the drain's calls are spied -- ``cloud_staging``'s internal fileserver lookup uses its
    own module reference and is deliberately NOT recorded (the tracked observation is GATE-1 only).
    """
    real = enqueue_router.select_active_agent

    async def _wrapped(session: AsyncSession, kind: str | None = None) -> Any:
        calls.append(kind if kind is not None else "<any>")
        return await real(session, kind=kind)

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
    monkeypatch.setattr(
        release_awaiting_cloud,
        "get_settings",
        lambda: _StubCfg(active_cap=active_cap, cloud_enabled=cloud_enabled, active_cloud_kind=kind),
    )

    # D-01a spy: record the kinds the drain's GATE-1/GATE-2 request select_active_agent for.
    gate_kinds: list[str] = []
    monkeypatch.setattr(release_awaiting_cloud, "select_active_agent", _spy_select_active_agent(gate_kinds))

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
    # TODO(68-04 / Wave 3): flips to 2 when D-03/D-08 land the in-txn compute cloud_job write.
    # Every OTHER field in this dict MUST stay byte-identical across the refactor (BACK-04 proof).
    "cloud_job_count": 0,
    "tally": {"staged": 2, "skipped": 0},
}

_COMPUTE_DOWN_EXPECTED = {
    "gate_kinds": ["compute"],  # GATE-1 holds and returns before GATE-2 is reached
    "compute_gate_checked": True,
    "staging_tasks": [],
    "state_counts": {"pushing": 0, "awaiting_cloud": 3},
    "cloud_job_count": 0,
    "tally": {"staged": 0, "skipped": 0},  # compute+down -> no-op hold (GATE-1)
}

_KUEUE_UP_EXPECTED = {
    "gate_kinds": ["fileserver"],  # D-01a: kueue SKIPS the compute gate entirely
    "compute_gate_checked": False,
    "staging_tasks": ["s3_upload"],  # kueue S3-staging leg
    "state_counts": {"pushing": 2, "awaiting_cloud": 1},
    "cloud_job_count": 2,  # kueue already upserts cloud_job today (UPLOADING)
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
    "cloud_job_count": 0,
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
