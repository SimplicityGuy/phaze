"""D-11 (T-83-02): concurrent under-cap /upload-failed RMW under a real advisory lock (92-04).

Moved here from ``tests/agents/routers/test_agent_s3.py`` by plan 92-04 (Option B). The cell drives the
control-side ``report_s3_upload_failed`` endpoint over the real app and proves the ``s3_upload_attempt``
read->+1->write-back is serialized by a ``pg_advisory_xact_lock(hashtext(ledger_key))`` -- a property that
only exists across two INDEPENDENT, committed-visible DB transactions. The hermetic single-connection
``create_savepoint`` ``session`` fixture (92-03) would put both requests on one connection (no real
contention, no independent commit visibility), so this lives on the real-PG ``committed_db`` fixture.

Test-local helpers are copied from the donor (which keeps them for its hermetic cells); the assertion is
preserved verbatim.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from typing import TYPE_CHECKING, Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.config import ControlSettings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_s3 import router as agent_s3_router
from phaze.services import cloud_staging
from phaze.services.scheduling_ledger import upsert_ledger_entry
from tests._queue_fakes import FakeQueue, FakeTaskRouter


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration


# A one-kueue registry so ``active_cloud_kind == "kueue"``; the /upload-failed under-cap re-drive path is
# kind-agnostic, but this matches the donor's default ``_patch_settings`` fixture. Copied from the donor.
_KUEUE_REGISTRY = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "kueue"
    id = "kueue-cluster"
    rank = 10
    cap = 4
    buckets = ["shared-bucket"]

    [backends.kube]
    api_url = "https://kube.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq"

    [[buckets]]
    id = "shared-bucket"
    scope = "shared"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-staging"
"""


def _patch_settings(monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any) -> None:
    """Build a real ControlSettings off the one-kueue registry and pin the router's get_settings (copied)."""
    backends_toml_env(_KUEUE_REGISTRY)
    settings = ControlSettings()
    monkeypatch.setattr("phaze.routers.agent_s3.get_settings", lambda: settings)


def _make_client(
    session: AsyncSession,
    task_router: FakeTaskRouter,
    token: str | None = None,
    *,
    controller_queue: FakeQueue | None = None,
) -> AsyncClient:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_s3_router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.task_router = task_router
    app.state.controller_queue = controller_queue if controller_queue is not None else FakeQueue("controller")
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


async def _seed_cloud_job(
    session: AsyncSession,
    file_id: uuid.UUID,
    *,
    status: CloudJobStatus = CloudJobStatus.UPLOADING,
    staging_bucket: str | None = "shared-bucket",
) -> None:
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            s3_key=f"phaze-staging/{file_id}",
            status=status.value,
            upload_id="upload-xyz",
            staging_bucket=staging_bucket,
        )
    )
    await session.commit()


async def _seed_ledger(session: AsyncSession, file_id: uuid.UUID, *, attempt: int | None = None) -> None:
    payload: dict[str, Any] = {"file_id": str(file_id)}
    if attempt is not None:
        payload["s3_upload_attempt"] = attempt
    await upsert_ledger_entry(session, key=f"s3_upload:{file_id}", function="s3_upload", kwargs=payload)
    await session.commit()


async def _ledger_row(session: AsyncSession, key: str) -> SchedulingLedger | None:
    stmt = select(SchedulingLedger).where(SchedulingLedger.key == key).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def test_failed_concurrent_under_cap_no_lost_update(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """D-11 (T-83-02): two concurrent under-cap /upload-failed increment ``s3_upload_attempt`` to EXACTLY 2.

    Mirrors the /mismatch T-73-13 donor. The attempt RMW on the ``s3_upload:<file_id>`` ledger payload
    must be serialized by a ``pg_advisory_xact_lock(hashtext(ledger_key))`` (NOT a row lock -- that would
    self-deadlock against ``stage_file_to_s3``'s ``before_enqueue`` hook upserting the same row). Request A
    holds the advisory lock across its transaction (parked at ``redrive_upload``) while request B blocks on
    it, so B reads A's committed value and adds the second increment. Without the advisory lock both read 0
    and write 1 (a lost update -> final 1); with it the persisted counter is exactly 2, so no file can
    silently exceed its bounded upload budget.
    """
    _engine, session_factory = committed_db
    _patch_settings(monkeypatch, backends_toml_env)
    async with session_factory() as seed_session:
        agent_id, raw_token = await _seed_reporter_agent(seed_session)
        file_id = await _seed_file(seed_session, agent_id)
        await _seed_cloud_job(seed_session, file_id, status=CloudJobStatus.UPLOADING)
        await _seed_ledger(seed_session, file_id, attempt=0)  # both requests take the under-cap re-drive path (cap 3)

    reached = asyncio.Event()  # set once request A is parked mid-transaction (advisory lock held)
    proceed = asyncio.Event()  # released by the test to let request A finish + drop the lock

    async def _gated_redrive(_sess: AsyncSession, _file: FileRecord, router: Any) -> None:
        # Park ONLY request A (its router carries the events) so A holds the advisory lock open while B
        # is launched; request B's plain router runs straight through once it acquires the lock.
        park_reached = getattr(router, "park_reached", None)
        park_proceed = getattr(router, "park_proceed", None)
        if park_reached is not None and park_proceed is not None:
            park_reached.set()
            await park_proceed.wait()

    monkeypatch.setattr(cloud_staging, "redrive_upload", _gated_redrive)

    async with session_factory() as session_a, session_factory() as session_b:
        router_a = FakeTaskRouter()
        router_a.park_reached = reached  # type: ignore[attr-defined]
        router_a.park_proceed = proceed  # type: ignore[attr-defined]
        client_a = _make_client(session_a, router_a, raw_token)
        router_b = FakeTaskRouter()
        client_b = _make_client(session_b, router_b, raw_token)

        async with client_a, client_b:
            task_a = asyncio.create_task(client_a.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "a"}))
            # Wait until A is parked at redrive_upload with the advisory lock held in its open transaction.
            await asyncio.wait_for(reached.wait(), timeout=10.0)

            # Launch B: it must block on A's advisory lock at the attempt-RMW read (fixed code). Give it
            # time to reach and queue on the lock (or, on unfixed code, to read the stale 0 and race ahead).
            task_b = asyncio.create_task(client_b.post(f"/api/internal/agent/s3/{file_id}/failed", json={"detail": "b"}))
            await asyncio.sleep(0.25)

            # Release A: it writes s3_upload_attempt=1 and commits, dropping the lock so B reads 1 -> writes 2.
            proceed.set()
            resp_a, resp_b = await asyncio.gather(task_a, task_b)

    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert resp_a.json()["cleared"] is False
    assert resp_b.json()["cleared"] is False

    # The advisory-locked RMW applied each increment exactly once: no lost update.
    async with session_factory() as session:
        row = await _ledger_row(session, f"s3_upload:{file_id}")
        assert row is not None
        assert row.payload.get("s3_upload_attempt") == 2, "two concurrent under-cap /upload-failed must increment s3_upload_attempt to exactly 2"
