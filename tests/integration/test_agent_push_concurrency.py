"""D-05 (AR-73-02 / T-73-13 / WR-04 / HARD-02): concurrent /mismatch RMW under a real advisory lock (92-04).

Moved here from ``tests/agents/routers/test_agent_push.py`` by plan 92-04 (Option B). Both cells drive the
control-side ``report_push_mismatch`` endpoint over the real app and prove properties that only exist
across INDEPENDENT, committed-visible DB connections:

* ``test_mismatch_concurrent_no_lost_update`` -- two concurrent /mismatch requests, each in its OWN DB
  transaction, must increment ``push_attempt`` to EXACTLY 2. The advisory-locked read->+1->write-back
  serializes only if request B truly blocks on request A's committed lock on a SECOND connection.
* ``test_mismatch_real_enqueue_hook_does_not_deadlock`` -- the under-cap re-drive must not self-deadlock
  against the real ``before_enqueue`` ledger upsert running in its OWN session on the same engine.

The hermetic single-connection ``create_savepoint`` ``session`` fixture (92-03) cannot express either: the
requests would share one connection (no real contention, no independent commit visibility). So these live
on the real-PG ``committed_db`` fixture. Test-local helpers are copied from the donor; assertions preserved.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, update

from phaze.config import ControlSettings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_push import router as agent_push_router
from phaze.services.scheduling_ledger import upsert_ledger_entry
from phaze.tasks._shared.deterministic_key import apply_deterministic_key
from tests._queue_fakes import _JOB_CONTROL_FIELDS, FakeQueue, FakeTaskRouter, _lane_key, _lane_name, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration


_SCRATCH_DIR = "/srv/scratch"

# Reporter registry: the compute backend's agent_ref == the reporting agent id ("test-agent-01") so the
# token-authed reporter passes the D-07 gate and the re-drive path is exercised. Copied from the donor.
_COMPUTE_REPORTER_REGISTRY = f"""
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 10
    cap = 2
    agent_ref = "test-agent-01"
    scratch_dir = "{_SCRATCH_DIR}"
    push_host = "oci-a1.push.example"
"""


def _patch_settings(monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any, *, registry: str = _COMPUTE_REPORTER_REGISTRY) -> None:
    """Pin the router's ``get_settings()`` to a real ControlSettings off ``registry`` (copied from the donor)."""
    backends_toml_env(registry)
    settings = ControlSettings()
    monkeypatch.setattr("phaze.routers.agent_push.get_settings", lambda: settings)


def _make_app(session: AsyncSession, task_router: FakeTaskRouter) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_push_router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.task_router = task_router
    return app


def _make_client(session: AsyncSession, task_router: FakeTaskRouter, token: str | None = None) -> AsyncClient:
    app = _make_app(session, task_router)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_reporter_agent(session: AsyncSession) -> tuple[str, str]:
    """Seed the ``test-agent-01`` reporter with a known token (committed). Returns ``(agent_id, raw_token)``.

    Replicates the suite's ``seed_test_agent`` fixture (which binds to the hermetic ``session``) so this
    integration file can commit the reporter on the real engine.
    """
    raw_token = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session.add(Agent(id="test-agent-01", name="test-agent-01", token_hash=token_hash, scan_roots=["/test/music"]))
    await session.commit()
    return "test-agent-01", raw_token


async def _seed_file(session: AsyncSession, agent_id: str) -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="a" * 64,
            original_path=f"/test/music/{file_id}.flac",
            original_filename=f"{file_id}.flac",
            current_path=f"/test/music/{file_id}.flac",
            file_type="flac",
            file_size=4096,
        )
    )
    await session.commit()
    return file_id


async def _seed_push_ledger(session: AsyncSession, file_id: uuid.UUID, *, push_attempt: int | None = None) -> None:
    payload: dict[str, Any] = {"file_id": str(file_id)}
    await upsert_ledger_entry(session, key=f"push_file:{file_id}", function="push_file", kwargs=payload)
    # phaze-2jl1: the push_attempt counter lives in the dedicated `redrive_attempt` column, not payload.
    if push_attempt is not None:
        await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == f"push_file:{file_id}").values(redrive_attempt=push_attempt))
    await session.commit()


async def _seed_cloud_job(session: AsyncSession, file_id: uuid.UUID, *, status: CloudJobStatus = CloudJobStatus.SUBMITTED) -> None:
    """Seed the compute cloud_job sidecar row ComputeAgentBackend.dispatch writes (copied from the donor)."""
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_id, backend_id="oci-a1", s3_key=None, status=status.value))
    await session.commit()


async def _ledger_row(session: AsyncSession, key: str) -> SchedulingLedger | None:
    session.expire_all()
    return (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == key))).scalar_one_or_none()


# --- Gated router: parks request A mid-transaction so B genuinely contends on the advisory lock -----


class _GatedQueue(FakeQueue):
    """A ``FakeQueue`` whose ``connect()`` parks the request mid-transaction (copied from the donor)."""

    def __init__(self, name: str, capture: Any = None, *, reached: asyncio.Event, proceed: asyncio.Event) -> None:
        super().__init__(name, capture)
        self._reached = reached
        self._proceed = proceed

    async def connect(self) -> None:
        self._reached.set()
        await self._proceed.wait()


class _GatedTaskRouter(FakeTaskRouter):
    """A ``FakeTaskRouter`` whose per-agent queues are :class:`_GatedQueue` instances (copied from the donor)."""

    def __init__(self, *, reached: asyncio.Event, proceed: asyncio.Event) -> None:
        super().__init__()
        self._reached = reached
        self._proceed = proceed

    def queue_for(self, agent_id: str, lane: str | None = None) -> FakeQueue:  # type: ignore[override]
        self.queue_for_calls.append(agent_id)
        key = _lane_key(agent_id, lane)
        if key not in self.queues:
            self.queues[key] = _GatedQueue(_lane_name(agent_id, lane), self.captures, reached=self._reached, proceed=self._proceed)
        return self.queues[key]


async def test_mismatch_concurrent_no_lost_update(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """D-05 (AR-73-02 / T-73-13 / WR-04): two concurrent /mismatch increment push_attempt to EXACTLY 2.

    Both requests take the under-cap re-drive path (push_attempt starts at 0, cap is 3), each in its own
    DB transaction against the real port-5433 engine. The advisory-locked RMW serializes the
    read->+1->write-back: request A holds the advisory lock across its transaction while request B blocks
    on it, so B reads A's committed value and adds the second increment. Without the advisory lock both
    would read 0 and write 1 (a lost update -> final 1); with it the persisted counter is exactly 2.
    """
    _engine, session_factory = committed_db
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    async with session_factory() as seed_session:
        agent_id, raw_token = await _seed_reporter_agent(seed_session)
        file_id = await _seed_file(seed_session, agent_id)
        await _seed_push_ledger(seed_session, file_id, push_attempt=0)
        await _seed_cloud_job(seed_session, file_id)  # backend_id="oci-a1", agent_ref="test-agent-01"
        await seed_active_agent(seed_session, agent_id="fileserver-01", kind="fileserver")

    ledger_key = f"push_file:{file_id}"
    reached = asyncio.Event()  # set once request A is parked mid-transaction (advisory lock held)
    proceed = asyncio.Event()  # released by the test to let request A finish + drop the lock

    async with session_factory() as session_a, session_factory() as session_b:
        # Request A parks at connect() while holding the advisory-locked ledger SELECT.
        router_a = _GatedTaskRouter(reached=reached, proceed=proceed)
        client_a = _make_client(session_a, router_a, raw_token)
        # Request B uses a plain (non-parking) router so it runs straight through once it holds the lock.
        router_b = FakeTaskRouter()
        client_b = _make_client(session_b, router_b, raw_token)

        async with client_a, client_b:
            task_a = asyncio.create_task(client_a.post(f"/api/internal/agent/push/{file_id}/mismatch"))
            # Wait until A is parked at connect() with the ledger row locked in its open transaction.
            await asyncio.wait_for(reached.wait(), timeout=10.0)

            # Launch B: it must block on A's advisory lock at the ledger SELECT (fixed code). Give it time
            # to reach and queue on the lock (or, on unfixed code, to read the stale 0 and race ahead).
            task_b = asyncio.create_task(client_b.post(f"/api/internal/agent/push/{file_id}/mismatch"))
            await asyncio.sleep(0.25)

            # Release A: it writes push_attempt=1 and commits, dropping the lock so B can read 1 -> write 2.
            proceed.set()
            resp_a, resp_b = await asyncio.gather(task_a, task_b)

    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert resp_a.json()["cleared"] is False
    assert resp_b.json()["cleared"] is False

    # The advisory-locked RMW applied each increment exactly once: no lost update.
    async with session_factory() as session:
        row = await _ledger_row(session, ledger_key)
        assert row is not None
        assert row.redrive_attempt == 2, "two concurrent /mismatch must increment push_attempt to exactly 2"


# --- Real before_enqueue hook: the under-cap re-drive must not self-deadlock ------------------------


class _RealHookQueue(FakeQueue):
    """A ``FakeQueue`` whose ``enqueue()`` runs the REAL ``apply_deterministic_key`` before_enqueue WRITE
    hook (copied from the donor). The hook opens its OWN session off ``ledger_sessionmaker`` and upserts
    the SAME ``push_file:<file_id>`` ledger row while the request's transaction is still open -- the precise
    interaction a ledger *row* lock would self-deadlock against; the advisory lock keeps it lock-free.
    """

    def __init__(self, name: str, capture: Any = None, *, ledger_sessionmaker: async_sessionmaker) -> None:
        super().__init__(name, capture)
        self.ledger_sessionmaker = ledger_sessionmaker
        self.cache_redis = None  # best-effort enqueued-counter INCR degrades to a logged no-op

    async def connect(self) -> None:
        return None

    async def enqueue(self, task_name: str, **kwargs: Any) -> Any:
        job = SimpleNamespace(
            function=task_name,
            kwargs={k: v for k, v in kwargs.items() if k not in _JOB_CONTROL_FIELDS},
            key=kwargs.get("key"),
            timeout=kwargs.get("timeout"),
            retries=kwargs.get("retries"),
            queue=self,
        )
        await apply_deterministic_key(job)
        return await super().enqueue(task_name, **kwargs)


class _RealHookTaskRouter(FakeTaskRouter):
    """A ``FakeTaskRouter`` whose per-agent queues are :class:`_RealHookQueue` instances (copied from the donor)."""

    def __init__(self, *, ledger_sessionmaker: async_sessionmaker) -> None:
        super().__init__()
        self._sm = ledger_sessionmaker

    def queue_for(self, agent_id: str, lane: str | None = None) -> FakeQueue:  # type: ignore[override]
        self.queue_for_calls.append(agent_id)
        key = _lane_key(agent_id, lane)
        if key not in self.queues:
            self.queues[key] = _RealHookQueue(_lane_name(agent_id, lane), self.captures, ledger_sessionmaker=self._sm)
        return self.queues[key]


async def test_mismatch_real_enqueue_hook_does_not_deadlock(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """Regression (HARD-02): the under-cap re-drive must NOT deadlock against the real before_enqueue hook.

    ``report_push_mismatch`` re-enqueues ``push_file`` while its transaction is still open; ``push_file``
    is a registered key-builder, so ``apply_deterministic_key`` upserts the SAME ledger row in its own
    session. The advisory-lock RMW keeps that row UNlocked, so the hook's upsert proceeds and the request
    completes. A ``.with_for_update()`` row lock would hang here forever (no statement_timeout to break
    it), so the request is bounded by ``asyncio.wait_for`` — a timeout is the failure signal, not a pass.
    """
    _engine, session_factory = committed_db
    _patch_settings(monkeypatch, backends_toml_env, registry=_COMPUTE_REPORTER_REGISTRY)
    async with session_factory() as seed_session:
        agent_id, raw_token = await _seed_reporter_agent(seed_session)
        file_id = await _seed_file(seed_session, agent_id)
        await _seed_push_ledger(seed_session, file_id, push_attempt=0)
        await _seed_cloud_job(seed_session, file_id)  # backend_id="oci-a1", agent_ref="test-agent-01"
        await seed_active_agent(seed_session, agent_id="fileserver-01", kind="fileserver")

    ledger_key = f"push_file:{file_id}"
    async with session_factory() as req_session:
        router = _RealHookTaskRouter(ledger_sessionmaker=session_factory)
        client = _make_client(req_session, router, raw_token)
        async with client:
            # 15s hard bound: on the buggy row-lock version this request never returns.
            resp = await asyncio.wait_for(
                client.post(f"/api/internal/agent/push/{file_id}/mismatch"),
                timeout=15.0,
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"] is False
    # The request's post-enqueue write-back is the source of truth for the counter (it runs AFTER the
    # hook overwrote payload). It stamps the dedicated `redrive_attempt` column (phaze-2jl1), which the
    # hook never touches, so a single re-drive lands redrive_attempt == 1.
    async with session_factory() as session:
        row = await _ledger_row(session, ledger_key)
        assert row is not None
        assert row.redrive_attempt == 1
