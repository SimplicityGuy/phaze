"""Tests for the Phase-50 "stay one ahead" bounded staging cron (CLOUDPIPE-01 / -05).

``stage_cloud_window(ctx)`` is the SINGLE driver that introduces new push work to the compute
agent. It tops the ≤N window up to ``cloud_max_in_flight`` by staging ``push_file`` for the oldest
held ``AWAITING_CLOUD`` files (FIFO by ``created_at``). The window is counted from COMMITTED
FileState truth (``state IN {PUSHING, PUSHED}``, D-08), so a 144-file backlog can never blow up the
single compute scratch disk -- the cron stages at most ``slots = cloud_max_in_flight - window``.

Gates (both wrapped in ``try/except NoActiveAgentError`` -> clean no-op, T-50-cron-raise):
  * no online COMPUTE agent (the analysis consumer)  -> staged=0, files stay AWAITING_CLOUD.
  * no online FILESERVER agent (the push initiator)   -> staged=0, skipped=len(candidates), held.

Staged files transition AWAITING_CLOUD -> PUSHING and enqueue ``push_file`` on the fileserver queue;
a double-tick collapses via the ``push_file:<id>`` deterministic key (counted as skipped).

``ctx`` mirrors the controller worker shape: ``async_session`` (a sessionmaker bound to the test
engine), ``queue`` (a controller-queue stand-in, unused) and ``task_router`` (a
``DedupFakeTaskRouter`` modeling SAQ deterministic-key dedup).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.file import FileRecord, FileState
from phaze.tasks.release_awaiting_cloud import push_file_job_key, stage_cloud_window
from tests._queue_fakes import DedupFakeQueue, DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class _StubCfg:
    """Minimal stand-in for the control settings stage_cloud_window reads (cloud_max_in_flight + toggle).

    ``cloud_burst_enabled`` defaults True here so the existing Phase-50 staging tests keep exercising
    the ON behavior; the Phase-51 disabled case constructs the stub with the toggle off.
    """

    def __init__(self, *, cloud_max_in_flight: int = 2, cloud_burst_enabled: bool = True) -> None:
        self.cloud_max_in_flight = cloud_max_in_flight
        self.cloud_burst_enabled = cloud_burst_enabled


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, max_in_flight: int = 2, cloud_burst_enabled: bool = True) -> None:
    """Pin stage_cloud_window's get_settings() deterministically (cloud_max_in_flight + cloud_burst_enabled)."""
    monkeypatch.setattr(
        "phaze.tasks.release_awaiting_cloud.get_settings",
        lambda: _StubCfg(cloud_max_in_flight=max_in_flight, cloud_burst_enabled=cloud_burst_enabled),
    )


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter, controller_queue: DedupFakeQueue) -> dict[str, Any]:
    """Build a controller-shaped ctx: async_session + controller queue + per-agent dedup router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "queue": controller_queue, "task_router": router}


def _make_file(*, state: str = FileState.AWAITING_CLOUD, file_type: str = "mp3", created_at: datetime | None = None) -> FileRecord:
    """Build a fully-populated FileRecord row (AWAITING_CLOUD by default)."""
    uid = uuid.uuid4()
    kwargs: dict[str, Any] = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=state,
        **kwargs,
    )


async def _states_for(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Re-read the persisted state for ``ids`` from the DB (expiring the identity map first)."""
    session.expire_all()
    rows = (await session.execute(select(FileRecord).where(FileRecord.id.in_(ids)))).scalars().all()
    return {r.id: r.state for r in rows}


# --- Window full -> stage 0 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_full_stages_zero(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """With N already in flight (PUSHING+PUSHED), the cron stages 0 new files."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # Window already full: 1 PUSHING + 1 PUSHED == N=2.
    session.add_all([_make_file(state=FileState.PUSHING), _make_file(state=FileState.PUSHED)])
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 0}
    assert router.queues == {}
    # Held files untouched.
    assert set((await _states_for(session, ids)).values()) == {FileState.AWAITING_CLOUD}


# --- One free slot -> stage exactly 1 ---------------------------------------------------


@pytest.mark.asyncio
async def test_one_free_slot_stages_one(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """With N-1 in flight, exactly one AWAITING_CLOUD file is staged to push_file + flipped to PUSHING."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    session.add(_make_file(state=FileState.PUSHING))  # 1 in flight, 1 slot free
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 1, "skipped": 0}
    # Exactly one push_file enqueued onto the FILESERVER agent's per-agent queue.
    push_queue = router.queues["nox"]
    assert [t for t, _ in push_queue.captured] == ["push_file"]
    # WR-03: the push_file job carries an explicit SAQ job-net timeout (above the asyncio outer
    # guard), NOT the inherited 600s role default that equalled push_timeout_sec.
    from phaze.tasks.push import PUSH_FILE_SAQ_TIMEOUT_SEC

    assert push_queue.captured_policy[0]["timeout"] == PUSH_FILE_SAQ_TIMEOUT_SEC
    # Exactly one held file flipped to PUSHING; the rest stay AWAITING_CLOUD.
    states = await _states_for(session, ids)
    assert sorted(states.values()) == sorted([FileState.PUSHING, FileState.AWAITING_CLOUD, FileState.AWAITING_CLOUD])


# --- No compute agent -> no-op ----------------------------------------------------------


@pytest.mark.asyncio
async def test_no_compute_agent_is_noop(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no compute agent online the cron is a clean no-op -- nothing staged, files stay AWAITING_CLOUD."""
    _patch_settings(monkeypatch, max_in_flight=2)
    # Only a fileserver agent online; the compute gate finds none -> NoActiveAgentError.
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 0}
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {FileState.AWAITING_CLOUD}


# --- No fileserver agent -> no-op (compute online, fileserver absent) --------------------


@pytest.mark.asyncio
async def test_no_fileserver_agent_is_noop(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compute online but NO fileserver (the push initiator) -> clean no-op, files stay AWAITING_CLOUD.

    A fileserver offline during a rolling restart must be a clean hold (staged=0, skipped=len(candidates)),
    NOT a raise -- the held files re-stage on the next tick once the fileserver returns.
    """
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")  # no fileserver online
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    # Two slots free, two candidates locked, but no fileserver -> held, never raised.
    assert result == {"staged": 0, "skipped": 2}
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {FileState.AWAITING_CLOUD}


# --- Phase 51 (D-03): cloud_burst_enabled gate on the staging cron -----------------------


@pytest.mark.asyncio
async def test_cloud_burst_disabled_stages_nothing(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF: with cloud_burst_enabled False the cron is a clean no-op BEFORE the window logic (D-03).

    Both agents are online and the window is wide open (3 held, N=2), so the cron WOULD stage if the
    toggle were on. With it off it returns {"staged": 0, "skipped": 0}, takes no advisory lock, stages
    no push_file, and leaves every held file AWAITING_CLOUD.
    """
    _patch_settings(monkeypatch, max_in_flight=2, cloud_burst_enabled=False)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 0}
    assert router.queues == {}
    assert set((await _states_for(session, ids)).values()) == {FileState.AWAITING_CLOUD}


@pytest.mark.asyncio
async def test_cloud_burst_enabled_stages_normally(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: with cloud_burst_enabled True the cron stages as before (Phase 50 regression)."""
    _patch_settings(monkeypatch, max_in_flight=2, cloud_burst_enabled=True)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    held = [_make_file() for _ in range(3)]
    session.add_all(held)
    await session.commit()
    ids = [f.id for f in held]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 2, "skipped": 0}
    states = await _states_for(session, ids)
    assert sum(1 for st in states.values() if st == FileState.PUSHING) == 2


# --- FIFO: oldest AWAITING_CLOUD first ---------------------------------------------------


@pytest.mark.asyncio
async def test_fifo_oldest_awaiting_cloud_first(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging order is FIFO -- the oldest AWAITING_CLOUD file (by created_at) goes first."""
    _patch_settings(monkeypatch, max_in_flight=1)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    # ``files.created_at`` is TIMESTAMP WITHOUT TIME ZONE -> seed naive datetimes.
    base = datetime.now() - timedelta(hours=3)
    oldest = _make_file(created_at=base)
    middle = _make_file(created_at=base + timedelta(hours=1))
    newest = _make_file(created_at=base + timedelta(hours=2))
    # Insert out of order to prove the ORDER BY (not insertion order) drives selection.
    session.add_all([newest, oldest, middle])
    await session.commit()

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 1, "skipped": 0}
    states = await _states_for(session, [oldest.id, middle.id, newest.id])
    assert states[oldest.id] == FileState.PUSHING
    assert states[middle.id] == FileState.AWAITING_CLOUD
    assert states[newest.id] == FileState.AWAITING_CLOUD


# --- 144-file backlog never exceeds N ---------------------------------------------------


@pytest.mark.asyncio
async def test_backlog_of_144_stages_at_most_n(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 144-file AWAITING_CLOUD backlog with N=2 and an empty window stages AT MOST 2 in one tick."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backlog = [_make_file() for _ in range(144)]
    session.add_all(backlog)
    await session.commit()
    ids = [f.id for f in backlog]

    router = DedupFakeTaskRouter()
    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 2, "skipped": 0}
    states = await _states_for(session, ids)
    pushing = [fid for fid, st in states.items() if st == FileState.PUSHING]
    assert len(pushing) == 2  # never the whole backlog
    assert sum(1 for st in states.values() if st == FileState.AWAITING_CLOUD) == 142


# --- WR-04: overlapping ticks never overshoot the ≤N window ------------------------------


@pytest.mark.asyncio
async def test_overlapping_ticks_never_exceed_window(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-04: two concurrent staging ticks must not each see window=0 and stage 2x the cap.

    The window COUNT reads committed truth, so without serialization two overlapping ticks could
    SKIP LOCKED past each other's uncommitted PUSHING flips and stage up to 2 * cloud_max_in_flight.
    A transaction-scoped advisory lock makes the count+claim atomic so the committed PUSHING set
    never exceeds the cap, even under concurrency.
    """
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backlog = [_make_file() for _ in range(20)]
    session.add_all(backlog)
    await session.commit()
    ids = [f.id for f in backlog]

    router = DedupFakeTaskRouter()
    ctx = _make_ctx(async_engine, router, DedupFakeQueue("controller"))
    # Two overlapping ticks driven concurrently on the same event loop (each opens its own session
    # from the sessionmaker, so they race exactly like two SAQ cron runs).
    results = await asyncio.gather(stage_cloud_window(ctx), stage_cloud_window(ctx))

    states = await _states_for(session, ids)
    pushing = [fid for fid, st in states.items() if st == FileState.PUSHING]
    assert len(pushing) <= 2, f"window overshot: {len(pushing)} files PUSHING (cap is 2)"
    assert sum(r["staged"] for r in results) <= 2, "concurrent ticks staged more than the cap"


# --- Double-tick collapses via the deterministic push_file:<id> key ----------------------


@pytest.mark.asyncio
async def test_double_tick_dedups_via_deterministic_key(async_engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file whose push_file:<id> key is already live dedups to a skipped no-op (T-50-double-enqueue)."""
    _patch_settings(monkeypatch, max_in_flight=2)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    fileserver = await seed_active_agent(session, agent_id="nox", kind="fileserver")
    f = _make_file()
    session.add(f)
    await session.commit()
    fid = f.id

    router = DedupFakeTaskRouter()
    # Pre-enqueue the deterministic key on the fileserver queue so the cron's enqueue dedups to None.
    live_queue = router.queue_for(fileserver.id)
    await live_queue.enqueue("push_file", key=push_file_job_key(fid))
    router.queue_for_calls.clear()

    result = await stage_cloud_window(_make_ctx(async_engine, router, DedupFakeQueue("controller")))

    assert result == {"staged": 0, "skipped": 1}
    # The state still flips to PUSHING (the file left the AWAITING_CLOUD scan set; the live push job
    # will land it). The window count stays honest on the next tick.
    assert (await _states_for(session, [fid]))[fid] == FileState.PUSHING


# --- Controller registration: function + single narrow */5 cron (replaces the drain) -----


def test_stage_cloud_window_registered_in_controller_functions_and_cron() -> None:
    """stage_cloud_window is in controller settings['functions'] AND exactly one CronJob('*/5 ...').

    It REPLACES the deprecated release_awaiting_cloud drain cron, so the old symbol is gone from the
    controller registration entirely.
    """
    from phaze.tasks import controller
    from phaze.tasks.release_awaiting_cloud import stage_cloud_window as scw

    assert scw in controller.settings["functions"], "stage_cloud_window not registered in settings['functions']"

    stage_crons = [cj for cj in controller.settings["cron_jobs"] if cj.function is scw]
    assert len(stage_crons) == 1, "stage_cloud_window must be registered as exactly one CronJob"
    assert stage_crons[0].cron == "*/5 * * * *", "staging cron must run every 5 minutes"

    # The deprecated drain cron is gone -- no controller function is named release_awaiting_cloud.
    fn_names = {getattr(fn, "__name__", "") for fn in controller.settings["functions"]}
    assert "release_awaiting_cloud" not in fn_names, "the deprecated release_awaiting_cloud drain cron must be removed"


def test_staging_module_is_fastapi_free() -> None:
    """The staging module must stay control-only: no fastapi / phaze.routers import (import boundary).

    Parse the actual ``import`` / ``from`` statements via AST (so a mention in the docstring/prose
    does not count) and assert none reference the web layer.
    """
    import ast
    import pathlib

    import phaze.tasks.release_awaiting_cloud as mod

    src = mod.__file__
    assert src is not None
    tree = ast.parse(pathlib.Path(src).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imported), "staging module must not import fastapi"
    assert not any(name.startswith("phaze.routers") for name in imported), "staging module must not import phaze.routers"
