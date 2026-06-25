"""Tests for the Phase-49 held-file release producer (phaze.tasks.release_awaiting_cloud).

``release_awaiting_cloud(ctx)`` is the drain half of CLOUDROUTE-02. Plan 02's per-file router
HOLDS a long file in ``FileState.AWAITING_CLOUD`` (enqueuing NOTHING, D-02) when no compute agent
is online; this producer releases the held set once a compute agent comes online -- automatically,
within ~5 min, via a SINGLE narrow ``CronJob(release_awaiting_cloud, "*/5 * * * *")`` registered on
the controller.

It does a STATE-driven scan (``get_files_by_state(AWAITING_CLOUD)``) and, when a compute agent is
online, enqueues each held file to that compute agent's per-agent queue via the shared
``enqueue_process_file`` producer (deterministic key ``process_file:<id>``) AND resets each released
file to ``FileState.DISCOVERED`` (D-03/D-03a). It is NOT the deleted reenqueue auto-advance cron and
NOT a ledger replay (held files have no ledger row).

  - compute agent online: each held file enqueues on the compute queue + resets to DISCOVERED,
  - no compute agent online: clean no-op (nothing enqueued, nothing raised, no state change, D-02),
  - empty held set: clean no-op,
  - a held file whose ``process_file:<id>`` key is already live dedups to a skipped no-op, but its
    state reset STILL applies so the dashboard held-count stays honest (D-03a).

``ctx`` mirrors the controller worker shape: ``async_session`` (a sessionmaker bound to the test
engine), ``queue`` (a controller-queue stand-in, unused by release), ``task_router`` (a
``DedupFakeTaskRouter`` modeling SAQ deterministic-key dedup).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.file import FileRecord, FileState
from phaze.services.analysis_enqueue import process_file_job_key
from phaze.tasks.release_awaiting_cloud import release_awaiting_cloud
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


_MODELS_PATH = "/models"


class _StubCfg:
    """Minimal stand-in for the control settings release_awaiting_cloud reads (models_path only)."""

    def __init__(self, *, models_path: str = _MODELS_PATH) -> None:
        self.models_path = models_path


def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin release_awaiting_cloud's get_settings() deterministically (models_path)."""
    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: _StubCfg())


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + per-agent dedup router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": controller_queue, "task_router": router}


def _make_file(*, state: str = FileState.AWAITING_CLOUD, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated FileRecord row (AWAITING_CLOUD by default) for the release seed."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=state,
    )


async def _states_for(session: AsyncSession, ids: list[uuid.UUID]) -> set[str]:
    """Re-read the persisted state for ``ids`` from the DB (expiring the identity map first)."""
    session.expire_all()
    rows = (await session.execute(select(FileRecord).where(FileRecord.id.in_(ids)))).scalars().all()
    return {r.state for r in rows}


# --- Release + reset with a compute agent online (D-03/D-03a) ---------------------------


@pytest.mark.asyncio
async def test_release_enqueues_to_compute_and_resets_state(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held files + a compute agent online -> each enqueues on the compute queue + resets to DISCOVERED."""
    _patch_settings(monkeypatch)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    files = [_make_file() for _ in range(2)]
    session.add_all(files)
    await session.commit()
    ids = [f.id for f in files]

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await release_awaiting_cloud(_make_ctx(async_engine, router, controller_queue))

    assert result == {"released": 2, "skipped": 0}
    # Enqueued onto the compute agent's per-agent queue, never the controller queue.
    compute_queue = router.queues["cloud-1"]
    assert [t for t, _ in compute_queue.captured] == ["process_file", "process_file"]
    assert controller_queue.captured == []
    # Each released file was reset to DISCOVERED and committed.
    assert await _states_for(session, ids) == {FileState.DISCOVERED}


# --- No-op when no compute agent is online (D-02) ---------------------------------------


@pytest.mark.asyncio
async def test_no_op_when_no_compute_agent_online(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held files but only a FILESERVER agent online -> clean no-op: nothing enqueued, no reset, no raise."""
    _patch_settings(monkeypatch)
    # A fileserver agent is online, but kind="compute" selection finds none -> NoActiveAgentError.
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    files = [_make_file() for _ in range(3)]
    session.add_all(files)
    await session.commit()
    ids = [f.id for f in files]

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await release_awaiting_cloud(_make_ctx(async_engine, router, controller_queue))

    assert result == {"released": 0, "skipped": 0}
    assert router.queues == {}
    assert controller_queue.captured == []
    # Held files are untouched -- they stay AWAITING_CLOUD for a later tick.
    assert await _states_for(session, ids) == {FileState.AWAITING_CLOUD}


# --- No-op when the held set is empty ---------------------------------------------------


@pytest.mark.asyncio
async def test_no_op_when_no_held_files(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No AWAITING_CLOUD files -> clean no-op even with a compute agent online."""
    _patch_settings(monkeypatch)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    result = await release_awaiting_cloud(_make_ctx(async_engine, router, controller_queue))

    assert result == {"released": 0, "skipped": 0}
    assert router.queues == {}
    assert controller_queue.captured == []


# --- Dedup of an already-live key still resets state (D-03a) ----------------------------


@pytest.mark.asyncio
async def test_dedup_already_live_key_still_resets_state(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held file whose process_file key is already live dedups to a skipped no-op, but still resets.

    The deterministic-key dedup collapses the enqueue to ``None`` (counted as skipped), yet the state
    reset to DISCOVERED still applies so the file leaves the AWAITING_CLOUD scan set and the dashboard
    held-count stays honest (D-03a).
    """
    _patch_settings(monkeypatch)
    agent = await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    f = _make_file()
    session.add(f)
    await session.commit()
    fid = f.id

    router = DedupFakeTaskRouter()
    controller_queue = DedupFakeQueue("controller")
    # Pre-enqueue the deterministic key on the compute queue so the release call dedups to None.
    live_queue = router.queue_for(agent.id)
    await live_queue.enqueue("process_file", key=process_file_job_key(fid))
    router.queue_for_calls.clear()  # reset so the release call's bookkeeping is clean

    result = await release_awaiting_cloud(_make_ctx(async_engine, router, controller_queue))

    assert result == {"released": 0, "skipped": 1}
    # The state reset applies despite the dedup.
    assert await _states_for(session, [fid]) == {FileState.DISCOVERED}


# --- Controller registration: function + narrow */5 cron --------------------------------


def test_release_registered_in_controller_functions_and_cron() -> None:
    """release_awaiting_cloud is in controller settings['functions'] AND a single CronJob('*/5 ...')."""
    from phaze.tasks import controller
    from phaze.tasks.release_awaiting_cloud import release_awaiting_cloud as rac

    assert rac in controller.settings["functions"], "release_awaiting_cloud not registered in settings['functions']"

    release_crons = [cj for cj in controller.settings["cron_jobs"] if cj.function is rac]
    assert len(release_crons) == 1, "release_awaiting_cloud must be registered as exactly one CronJob"
    assert release_crons[0].cron == "*/5 * * * *", "release cron must run every 5 minutes"


def test_release_module_is_fastapi_free() -> None:
    """The release module must stay control-only: no fastapi / phaze.routers import (import boundary)."""
    import phaze.tasks.release_awaiting_cloud as mod

    src = mod.__file__
    assert src is not None
    import pathlib

    text = pathlib.Path(src).read_text(encoding="utf-8")
    assert "import fastapi" not in text and "from fastapi" not in text, "release module must not import fastapi"
    assert "phaze.routers" not in text, "release module must not import phaze.routers"
