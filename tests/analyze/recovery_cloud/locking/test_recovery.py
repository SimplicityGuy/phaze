"""Transaction release, commit ordering, and advisory-lock ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS
from phaze.tasks.reenqueue import (
    _replay_agent_rows_by_owner,
    recover_orphaned_work,
)
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


_MODELS_PATH = "/models"


class _StubCfg:
    """Minimal stand-in for the control settings recover_orphaned_work reads."""

    def __init__(self, *, models_path: str = _MODELS_PATH, llm_batch_size: int = 10) -> None:
        self.models_path = models_path
        self.llm_batch_size = llm_batch_size


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, llm_batch_size: int = 10) -> None:
    """Pin recover_orphaned_work's get_settings() deterministically (models_path + llm_batch_size)."""
    monkeypatch.setattr("phaze.tasks.reenqueue.get_settings", lambda: _StubCfg(llm_batch_size=llm_batch_size))


def _patch_inflight(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    """Stub the saq_jobs queue-loss detector to report ``value`` in-flight jobs."""

    async def _fake(_session: AsyncSession) -> int:
        return value

    monkeypatch.setattr("phaze.tasks.reenqueue.count_inflight_jobs", _fake)


def _patch_live_keys(monkeypatch: pytest.MonkeyPatch, keys: set[str]) -> None:
    """Stub get_live_job_keys to return a fixed set of live (queued/active) saq_jobs keys."""

    async def _fake(_session: AsyncSession) -> set[str]:
        return set(keys)

    monkeypatch.setattr("phaze.tasks.reenqueue.get_live_job_keys", _fake)


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + per-agent dedup router.

    92-04 (CLEAN-02): ``async_session`` is sourced from ``phaze.database.async_session`` -- monkeypatched by the
    ``session`` fixture's ``_route_stats_fanout`` to a factory BOUND to the per-test ``_db_connection``
    (create_savepoint), exactly as the production controller wires ``ctx["async_session"]``. This lets the task
    SEE seeded rows and makes its commits visible to sibling reads under create_savepoint isolation.
    """
    from phaze.database import async_session

    return {"async_session": async_session, "queue": controller_queue, "task_router": router}


def _make_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated FileRecord row for the recovery seed."""
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
    )


def _agent_payload(function: str, file_id: uuid.UUID) -> dict[str, Any]:
    """Build a minimal stored payload an agent-routed ledger row would carry for ``function``."""
    return {
        "file_id": str(file_id),
        "original_path": f"/music/{file_id}.mp3",
        "file_type": "mp3",
        "agent_id": "nox",
    }


async def _seed_ledger(
    session: AsyncSession,
    *,
    function: str,
    file_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    timeout: int | None = None,
    retries: int | None = None,
) -> str:
    """Upsert one ledger row for ``<function>:<file_id>`` and return its deterministic key."""
    builder = _KEY_BUILDERS[function]
    pay = payload if payload is not None else _agent_payload(function, file_id)
    key = f"{function}:{builder(pay)}"
    await upsert_ledger_entry(session, key=key, function=function, kwargs=pay, timeout=timeout, retries=retries)
    await session.commit()
    return key


@pytest.mark.asyncio
async def test_recover_orphaned_work_releases_read_txn_before_replay_loops(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-266lc: the read-phase transaction is committed before the enqueue replay loops.

    ``recover_orphaned_work`` wraps its whole body in one ``async with async_session()`` block.
    After the read phase (ledger rows / live keys / done sets / cloud exclusions) it must commit
    BEFORE entering the controller/agent replay loops -- each row's replay is a network-dependent
    ``queue.enqueue`` call, and on a large orphaned set the loop can run for minutes. Left open,
    the control-engine connection sits idle-in-transaction across the whole replay (the phaze-1v37
    pool-drain class). Spy on both the session commit and the per-agent queue's enqueue and assert
    a commit is recorded strictly before the first enqueue.
    """
    _patch_settings(monkeypatch)
    _patch_inflight(monkeypatch, 0)
    _patch_live_keys(monkeypatch, set())
    await seed_active_agent(session, agent_id="nox")
    f = _make_file()
    session.add(f)
    await session.commit()
    await _seed_ledger(session, function="process_file", file_id=f.id)

    order: list[str] = []
    real_commit = AsyncSession.commit
    real_enqueue = DedupFakeQueue.enqueue

    async def _spy_commit(self: AsyncSession) -> None:
        await real_commit(self)
        order.append("commit")

    async def _spy_enqueue(self: DedupFakeQueue, task_name: str, **kwargs: Any) -> Any:
        order.append("enqueue")
        return await real_enqueue(self, task_name, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", _spy_commit)
    monkeypatch.setattr(DedupFakeQueue, "enqueue", _spy_enqueue)

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await recover_orphaned_work(_make_ctx(async_engine, router, controller_queue))

    assert result["stages"]["process_file"]["reenqueued"] == 1
    assert "enqueue" in order
    enqueue_index = order.index("enqueue")
    assert "commit" in order[:enqueue_index], f"expected a read-phase commit before the first replay enqueue, got order={order}"


@pytest.mark.asyncio
async def test_batched_owner_lookup_holds_no_transaction_across_the_enqueue_loops(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-266lc: the owner lookup's transaction is committed BEFORE the first row replays.

    Batching moved the commit earlier (once for the whole partition, not once per owner), so the
    invariant it protects is asserted directly: at the moment ANY enqueue happens, this session must
    not be in a transaction. Left open, the control-engine connection sits idle-in-transaction across
    a replay that spans minutes of network calls on a large orphaned set -- the phaze-1v37 pool-drain
    class.
    MUTATION: deleting the ``await session.commit()`` after the batched lookup leaves the session
    in-transaction on the first enqueue -> RED.
    """
    _patch_settings(monkeypatch)
    await seed_active_agent(session, agent_id="fs-a", kind="fileserver")
    await seed_active_agent(session, agent_id="fs-b", kind="fileserver")

    in_transaction_at_enqueue: list[bool] = []

    class _TxProbeRouter(DedupFakeTaskRouter):
        def queue_for(self, agent_id: str, lane: str | None = None) -> DedupFakeQueue:
            in_transaction_at_enqueue.append(session.in_transaction())
            return super().queue_for(agent_id, lane)

    rows = [
        SchedulingLedger(key="process_file:a", function="process_file", routing="agent", payload={"file_id": "a", "agent_id": "fs-a"}),
        SchedulingLedger(key="process_file:b", function="process_file", routing="agent", payload={"file_id": "b", "agent_id": "fs-b"}),
    ]
    stages: dict[str, dict[str, int]] = {}

    await _replay_agent_rows_by_owner(session, _TxProbeRouter(), rows, stages, required_kind=None)

    assert in_transaction_at_enqueue == [False, False]
    assert stages["process_file"]["reenqueued"] == 2
