"""Tests for the control-side reboot re-enqueue task (phaze.tasks.reenqueue.reenqueue_discovered).

The task queries Postgres for ``FileState.DISCOVERED`` files and re-enqueues
``process_file`` for each onto the ACTIVE agent's per-agent queue through the Wave-1
shared helper (``phaze.services.analysis_enqueue.enqueue_process_file``). It must:

  - re-enqueue every DISCOVERED file when an agent is live and the queue is fresh
    (startup recovery),
  - dedup a file whose deterministic key is already in flight to a no-op counted as
    ``skipped`` (idempotent on every cron tick),
  - degrade to a logged WARNING + zero count when no agent is live (never raise),
  - return zeros without touching the router when there are no DISCOVERED files,
  - carry the complete 5-field ``ProcessFilePayload`` + ``timeout=14400`` / ``retries=2``.

ctx is built like ``test_scan_reaper.py``'s ``_make_ctx`` -- ``ctx["async_session"]`` is a
sessionmaker bound to the test engine -- plus a Wave-0 ``DedupFakeTaskRouter`` under
``ctx["task_router"]`` that models SAQ's deterministic-key dedup. Rows seeded + committed
through the ``session`` fixture (same engine) are visible to the task's own session.

``get_settings`` is monkeypatched per test so ``models_path`` is deterministic regardless
of the ambient role/env (mirrors ``test_scan_reaper.py``'s ``_patch_threshold``).
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.file import FileRecord, FileState
from phaze.services.analysis_enqueue import process_file_job_key
from phaze.tasks.reenqueue import reenqueue_discovered
from tests._queue_fakes import DedupFakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


_MODELS_PATH = "/models"


class _StubCfg:
    """Minimal stand-in for the settings object the task reads (only ``models_path``)."""

    def __init__(self, models_path: str = _MODELS_PATH) -> None:
        self.models_path = models_path


def _patch_models_path(monkeypatch: pytest.MonkeyPatch, models_path: str = _MODELS_PATH) -> None:
    """Pin reenqueue_discovered's ``get_settings().models_path`` deterministically."""
    monkeypatch.setattr("phaze.tasks.reenqueue.get_settings", lambda: _StubCfg(models_path))


def _make_ctx(async_engine: AsyncEngine, router: DedupFakeTaskRouter) -> dict[str, Any]:
    """Build a SAQ-shaped ctx: async_session bound to the test engine + a dedup router."""
    sm = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    return {"async_session": sm, "task_router": router}


async def _seed_file(session: AsyncSession, *, agent_id: str, idx: int) -> uuid.UUID:
    """Seed one DISCOVERED FileRecord owned by ``agent_id``; return its id.

    Sets every NOT-NULL column (sha256_hash / original_filename / current_path /
    file_type / file_size) plus ``original_path`` (the field the payload carries).
    """
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            sha256_hash=f"{idx:064d}",
            original_path=f"/music/track-{idx}.mp3",
            original_filename=f"track-{idx}.mp3",
            current_path=f"/music/track-{idx}.mp3",
            file_type="mp3",
            file_size=1024 + idx,
            state=FileState.DISCOVERED,
            agent_id=agent_id,
        )
    )
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_startup_reenqueues_all_discovered(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All DISCOVERED + active agent + fresh queue -> every file re-enqueued onto phaze-agent-<id>."""
    _patch_models_path(monkeypatch)
    agent = await seed_active_agent(session, agent_id="nox")
    ids = [await _seed_file(session, agent_id=agent.id, idx=i) for i in range(3)]

    router = DedupFakeTaskRouter()
    result = await reenqueue_discovered(_make_ctx(async_engine, router))

    assert result == {"reenqueued": 3, "skipped": 0}
    queue = router.queues[agent.id]
    assert len(queue.captured) == 3
    assert {task for task, _ in queue.captured} == {"process_file"}
    captured_keys = {policy["key"] for policy in queue.captured_policy}
    assert captured_keys == {process_file_job_key(fid) for fid in ids}


@pytest.mark.asyncio
async def test_cron_reenqueues_stragglers(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subset already in flight counts as skipped; only the stragglers re-enqueue."""
    _patch_models_path(monkeypatch)
    agent = await seed_active_agent(session, agent_id="nox")
    ids = [await _seed_file(session, agent_id=agent.id, idx=i) for i in range(4)]

    router = DedupFakeTaskRouter()
    # Pre-enqueue (make "live") the first two files' deterministic keys on the agent queue.
    live_queue = router.queue_for(agent.id)
    for fid in ids[:2]:
        await live_queue.enqueue("process_file", key=process_file_job_key(fid))

    result = await reenqueue_discovered(_make_ctx(async_engine, router))

    # Two in-flight -> skipped; the remaining two -> reenqueued.
    assert result == {"reenqueued": 2, "skipped": 2}


@pytest.mark.asyncio
async def test_reenqueue_inflight_file_is_noop(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single file whose key is already live dedups -> skipped, no second payload lands."""
    _patch_models_path(monkeypatch)
    agent = await seed_active_agent(session, agent_id="nox")
    file_id = await _seed_file(session, agent_id=agent.id, idx=0)

    router = DedupFakeTaskRouter()
    queue = router.queue_for(agent.id)
    await queue.enqueue("process_file", key=process_file_job_key(file_id))
    assert len(queue.captured) == 1  # the pre-enqueue landed

    result = await reenqueue_discovered(_make_ctx(async_engine, router))

    assert result == {"reenqueued": 0, "skipped": 1}
    # The dedup no-op means NO second payload landed: still exactly one capture.
    assert len(queue.captured) == 1


@pytest.mark.asyncio
async def test_no_active_agent_skips(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DISCOVERED files but no live agent -> zeros, a WARNING, and NO raise."""
    _patch_models_path(monkeypatch)
    # Files owned by the conftest LEGACY agent (valid FK) but NO recently-seen agent exists,
    # so select_active_agent raises NoActiveAgentError.
    for i in range(2):
        await _seed_file(session, agent_id="legacy-application-server", idx=i)

    router = DedupFakeTaskRouter()
    with caplog.at_level("WARNING", logger="phaze.tasks.reenqueue"):
        result = await reenqueue_discovered(_make_ctx(async_engine, router))

    assert result == {"reenqueued": 0, "skipped": 0}
    assert "reenqueue skipped: no active agent" in caplog.text
    # No agent selected -> the router was never asked for a queue.
    assert router.queue_for_calls == []


@pytest.mark.asyncio
async def test_payload_is_complete(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each captured payload has exactly the 5 ProcessFilePayload fields + the bounded policy."""
    _patch_models_path(monkeypatch)
    agent = await seed_active_agent(session, agent_id="nox")
    file_id = await _seed_file(session, agent_id=agent.id, idx=7)

    router = DedupFakeTaskRouter()
    result = await reenqueue_discovered(_make_ctx(async_engine, router))

    assert result == {"reenqueued": 1, "skipped": 0}
    queue = router.queues[agent.id]
    _task, payload = queue.captured[0]
    assert set(payload.keys()) == {"file_id", "original_path", "file_type", "agent_id", "models_path"}
    assert payload["file_id"] == str(file_id)
    assert payload["agent_id"] == agent.id
    assert payload["models_path"] == _MODELS_PATH
    policy = queue.captured_policy[0]
    assert policy["timeout"] == 14400
    assert policy["retries"] == 2
    assert policy["key"] == process_file_job_key(file_id)


@pytest.mark.asyncio
async def test_empty_discovered_returns_zero(
    async_engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DISCOVERED rows -> zeros, and select_active_agent is never reached."""
    _patch_models_path(monkeypatch)
    # An active agent exists, but with zero DISCOVERED files the task must short-circuit
    # BEFORE selecting an agent or touching the router.
    await seed_active_agent(session, agent_id="nox")

    router = DedupFakeTaskRouter()
    result = await reenqueue_discovered(_make_ctx(async_engine, router))

    assert result == {"reenqueued": 0, "skipped": 0}
    assert router.queue_for_calls == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_broker_dedup_returns_none() -> None:
    """Against the real broker, the second enqueue of an in-flight deterministic key returns None.

    Pins the production SAQ dedup contract the DedupFakeQueue models: a repeat enqueue of a
    key still in the per-queue ``incomplete`` set is a clean no-op. Phase 36: the broker is
    PostgresQueue now, so this probes the Postgres broker (the cache-Redis counter INCR folded
    into the key hook is best-effort/swallowed, so the broker is the dependency under test).
    Skips when Postgres is unavailable; cleans the test key up after.
    """
    pytest.importorskip("redis")
    import psycopg

    from phaze.services.agent_task_router import AgentTaskRouter
    from phaze.services.analysis_enqueue import enqueue_process_file

    redis_url = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6379/0")
    queue_url = os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze").replace(
        "postgresql+asyncpg://", "postgresql://"
    )

    # Probe broker connectivity FIRST so the skip path creates no SAQ queue/connection to
    # clean up (a skip raised inside the main try would otherwise be overridden by close()
    # re-raising the same ConnectionError).
    try:
        probe = await psycopg.AsyncConnection.connect(queue_url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    router = AgentTaskRouter(queue_url=queue_url, cache_redis_url=redis_url)
    queue = router.queue_for("reenqueue-itest")

    # In-memory FileRecord is enough to build the payload (no DB needed); a unique id keeps
    # this run isolated from any prior leftover.
    file = FileRecord(
        id=uuid.uuid4(),
        sha256_hash="0" * 64,
        original_path="/music/itest.mp3",
        original_filename="itest.mp3",
        current_path="/music/itest.mp3",
        file_type="mp3",
        file_size=2048,
        state=FileState.DISCOVERED,
        agent_id="reenqueue-itest",
    )

    first = None
    try:
        first = await enqueue_process_file(queue, file, "reenqueue-itest", _MODELS_PATH)
        assert first is not None
        second = await enqueue_process_file(queue, file, "reenqueue-itest", _MODELS_PATH)
        assert second is None  # deterministic-key dedup no-op
    finally:
        if first is not None:
            # Best-effort cleanup -- never fail the test on a teardown hiccup.
            with contextlib.suppress(Exception):
                await queue.abort(first, "test cleanup")
        await router.close()
