"""Real broker -> owning-agent subprocess -> authenticated HTTP -> persistent source text."""

import asyncio
import contextlib
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import AsyncClient
import pytest
from saq import Worker
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.config import AgentSettings
from phaze.models.agent import Agent
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.provider_source import ProviderSourceObject, ProviderSourceObservation, ProviderSourceRead
from phaze.schemas.agent_companion_capture import CaptureReport, CaptureTarget
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.companion_capture import enqueue_companion_capture, store_capture_report
from phaze.services.provider_persistence import resolve_source
from phaze.tasks.companion_capture import capture_companion_source
from phaze.tracklist_providers.domain import SourceIdentity, SourceRead
from tests.db_guard import integration_dsns
from tests.discovery.routers.test_agent_companion_capture import seed_pair


pytestmark = pytest.mark.integration


async def test_owning_agent_capture_crosses_real_broker_and_authenticated_storage(
    session: AsyncSession,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, token = seed_test_agent
    path = tmp_path / "notes.txt"
    raw = "Artist: Example\r\nVenue: Café\n01. Intro\n".encode()
    path.write_bytes(raw)
    source, media = await seed_pair(session, agent, path, raw)
    dsn, _ = integration_dsns()
    router = AgentTaskRouter(queue_url=dsn, cache_redis_url=os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0"))
    queue = router.queue_for(agent.id, "meta")
    api = PhazeAgentClient(base_url="http://test", token=token, _client=authenticated_client)
    worker = Worker(queue=queue, functions=[capture_companion_source], concurrency=1, dequeue_timeout=0.5)
    worker.context.update(api_client=api, agent_identity=SimpleNamespace(agent_id=agent.id))
    config = MagicMock(spec=AgentSettings)
    config.scan_roots = [str(tmp_path)]
    monkeypatch.setattr("phaze.tasks.companion_capture.get_settings", lambda: config)
    try:
        factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
        await enqueue_companion_capture(factory, router, source.id, media_id=media.id)
        await asyncio.wait_for(worker.process(), timeout=15)
        observation = await session.scalar(select(ProviderSourceObservation))
        assert observation is not None and observation.decoded_text == raw.decode()
        assert observation.revision == source.sha256_hash and observation.status == "found"
        assert observation.payload["evidence"] == [f"capture:inventory_sha256:{source.sha256_hash}", "revision:sha256:full"]
        # Stored source text is independent of subsequent agent availability.
        assert (await session.get(ProviderSourceObservation, observation.id)).decoded_text == raw.decode()
    finally:
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as connection:
                await connection.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await router.close()


@pytest.mark.parametrize("scenario", ["import", "cached", "delete"])
async def test_bucket_lock_order_and_cached_inventory_revalidation(async_engine: Any, tmp_path: Path, scenario: str) -> None:
    """Actual independent PG sessions detect lock inversion and stale identity-map reads."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    source = FileRecord(
        id=uuid.uuid4(),
        agent_id="test-fileserver",
        file_type="txt",
        original_filename="notes.txt",
        original_path=str(tmp_path / "notes.txt"),
        current_path=str(tmp_path / "notes.txt"),
        file_size=0,
        sha256_hash=hashlib.sha256(b"").hexdigest(),
    )
    identity = SourceIdentity(provider_id="local", native_id=f"companion:{source.id}")
    body = CaptureReport(
        target=CaptureTarget(file_id=source.id, path=source.current_path, expected_sha256=source.sha256_hash, expected_size=0),
        read=SourceRead(status="unavailable", code="offline", scope=str(source.id), retrieved_at=datetime.now(UTC)),
    )
    object_id = None
    try:
        async with factory.begin() as db:
            db.add(source)
            await db.flush()
            obj = await resolve_source(db, identity, source_file_id=source.id, channel="companion")
            object_id = obj.id
        if scenario == "delete":
            async with factory.begin() as deleting:
                await deleting.execute(select(FileRecord.id).where(FileRecord.id == source.id).with_for_update())

                async def deleted_callback() -> None:
                    async with factory.begin() as db:
                        with pytest.raises(PermissionError):
                            await store_capture_report(db, source.agent_id, body)

                reporting = asyncio.create_task(deleted_callback())
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(reporting), timeout=0.05)
                # This cascades SET NULL onto the existing object while report waits for
                # inventory. Object-before-inventory makes this a real PG deadlock.
                await asyncio.wait_for(deleting.execute(delete(FileRecord).where(FileRecord.id == source.id)), timeout=5)
            await asyncio.wait_for(reporting, timeout=5)
            async with factory() as db:
                retained = await db.get(ProviderSourceObject, object_id)
                assert retained is not None and retained.source_file_id is None
        elif scenario == "cached":
            async with factory() as db:
                held = await db.get(FileRecord, source.id)
                assert held is not None and held.file_size == 0
                async with factory.begin() as other:
                    await other.execute(update(FileRecord).where(FileRecord.id == source.id).values(file_size=1))
                result = await store_capture_report(db, source.agent_id, body)
                assert result.freshness == "stale" and held.file_size == 1
                await db.commit()
        else:
            bucket_locked, acquire_inventory = asyncio.Event(), asyncio.Event()

            async def importer() -> None:
                async with factory.begin() as db:
                    await resolve_source(db, identity, source_file_id=source.id, channel="companion")
                    bucket_locked.set()
                    await acquire_inventory.wait()
                    await db.execute(select(FileRecord.id).where(FileRecord.id == source.id).with_for_update())

            async def callback() -> None:
                async with factory.begin() as db:
                    await store_capture_report(db, source.agent_id, body)

            importing = asyncio.create_task(importer())
            await bucket_locked.wait()
            reporting = asyncio.create_task(callback())
            try:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(reporting), timeout=0.05)
            finally:
                acquire_inventory.set()
            await asyncio.wait_for(asyncio.gather(importing, reporting), timeout=5)
    finally:
        async with factory.begin() as db:
            if object_id is not None:
                await db.execute(delete(ProviderSourceRead).where(ProviderSourceRead.object_id == object_id))
                await db.execute(delete(ProviderSourceObservation).where(ProviderSourceObservation.object_id == object_id))
                await db.execute(delete(ProviderSourceObject).where(ProviderSourceObject.id == object_id))
            await db.execute(delete(FileRecord).where(FileRecord.id == source.id))


@pytest.mark.parametrize("scenario", ["owner", "long_key", "nul_key", "missing", "ambiguous", "current_junk", "old_junk"])
async def test_capture_dispatch_inventory_and_key_guards(
    session: AsyncSession, seed_test_agent: tuple[Agent, str], tmp_path: Path, scenario: str
) -> None:
    """Actual PG inventory; substitute queue proves no enqueue, not no connection."""
    agent, _ = seed_test_agent
    source, media = await seed_pair(session, agent, tmp_path / "notes.txt", b"notes")
    if scenario == "missing":
        source.missing_at = datetime.now(UTC)
    elif scenario == "ambiguous":
        source.companion_ambiguous_at = datetime.now(UTC)
    elif scenario in {"current_junk", "old_junk"}:
        session.add(
            CompanionContentFeatures(
                file_id=source.id,
                agent_id=agent.id,
                fingerprint=source.sha256_hash if scenario == "current_junk" else "f" * 64,
                encoding="ascii",
                byte_size=5,
                is_tracklist=False,
                junk_class="site_ad",
                extractor_version=1,
            )
        )
    await session.commit()
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    router = SimpleNamespace(queue_for=MagicMock(return_value=queue))
    factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    key = "x" * 129 if scenario == "long_key" else "bad\0key" if scenario == "nul_key" else "x" * 128
    expected_owner = "previous-owner" if scenario == "owner" else agent.id
    if scenario == "old_junk":
        payload = await enqueue_companion_capture(factory, router, source.id, media_id=media.id, request_key=key, expected_agent_id=expected_owner)
        queue.enqueue.assert_awaited_once_with("capture_companion_source", **payload.model_dump(), key=key)
        router.queue_for.assert_called_once_with(agent.id, "meta")
    else:
        message = {
            "owner": "owner changed",
            "long_key": "request key",
            "nul_key": "request key",
            "missing": "unavailable or ambiguous",
            "ambiguous": "unavailable or ambiguous",
            "current_junk": "Current junk",
        }[scenario]
        with pytest.raises(ValueError, match=message):
            await enqueue_companion_capture(factory, router, source.id, media_id=media.id, request_key=key, expected_agent_id=expected_owner)
        queue.enqueue.assert_not_awaited()
    assert await session.scalar(select(ProviderSourceObservation.id)) is None
