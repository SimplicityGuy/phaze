"""Real committed metadata callbacks between review and durable tag authorization.

The broker/disk are not run: FakeTaskRouter consumes the real SAQ serializer.
Sessions here use independent Postgres connections, not the hermetic savepoint fixture.
"""

from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, func, select, update

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.routers import tags as tags_module
from phaze.routers.agent_metadata import put_metadata
from phaze.schemas.agent_metadata import MetadataWriteRequest
from phaze.services.tag_writer import enqueue_tag_write
from tests._queue_fakes import FakeTaskRouter
from tests.integration.test_tag_bulk_write_advisory_lock import _review_tokens, _seed_bulk_qualifying_file


@pytest.mark.parametrize("boundary", ["bulk", "noop", "row", "file", "insert", "delete"])
async def test_committed_revision_change_refused_before_durable_authorization(committed_db, boundary):
    _engine, factory = committed_db
    async with factory() as seed:
        seed.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
        await seed.commit()
        first = await _seed_bulk_qualifying_file(seed, filename="A Artist - First.mp3")
        later = await _seed_bulk_qualifying_file(seed, filename="B Artist - Later.mp3")
        # The title is copied from metadata, while an absent artist supplies a qualifying change.
        await seed.execute(update(FileMetadata).where(FileMetadata.file_id == later).values(title="Reviewed title"))
        if boundary == "noop":
            await seed.execute(update(FileMetadata).where(FileMetadata.file_id == later).values(artist="B Artist"))
        if boundary == "insert":
            await seed.execute(delete(FileMetadata).where(FileMetadata.file_id == later))
        await seed.commit()
        tokens = await _review_tokens(seed, [first, later])

    callback_pids = []

    async def change_metadata():
        async with factory() as callback:
            callback_pids.append(await callback.scalar(select(func.pg_backend_pid())))
            if boundary == "file":
                await callback.execute(update(FileRecord).where(FileRecord.id == later).values(current_path="/test/music/relocated.mp3"))
                await callback.commit()
            elif boundary == "delete":
                await callback.execute(delete(FileMetadata).where(FileMetadata.file_id == later))
                await callback.commit()
            else:
                agent = await callback.get(Agent, "test-fileserver")
                await put_metadata(later, MetadataWriteRequest(title="Corrected title"), agent, callback)
        # The callback really committed; a separate connection sees the new facts.
        async with factory() as verify:
            if boundary == "delete":
                assert await verify.scalar(select(FileMetadata.id).where(FileMetadata.file_id == later)) is None
            elif boundary != "file":
                assert await verify.scalar(select(FileMetadata.title).where(FileMetadata.file_id == later)) == "Corrected title"

    router = FakeTaskRouter()
    real_dispatch = router.enqueue_for_agent

    async def dispatch_then_callback(**kwargs):
        result = await real_dispatch(**kwargs)
        if kwargs["payload"].file_id == first:
            # A's audit is already durable when its dispatch is reached. B was initially
            # validated but has not yet entered its durable authorization transaction.
            async with factory() as verify:
                assert await verify.scalar(select(TagWriteLog.status).where(TagWriteLog.file_id == first)) == "queued"
            await change_metadata()
        return result

    router.enqueue_for_agent = dispatch_then_callback
    app = FastAPI()
    app.include_router(tags_module.router)
    app.state.task_router = router
    authorization_pids = []

    async def request_session():
        async with factory() as session:
            authorization_pids.append(await session.scalar(select(func.pg_backend_pid())))
            yield session

    app.dependency_overrides[get_session] = request_session

    async def change_before_enqueue(*args, **kwargs):
        await change_metadata()
        return await enqueue_tag_write(*args, **kwargs)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if boundary == "row":
            with patch.object(tags_module, "enqueue_tag_write", side_effect=change_before_enqueue):
                response = await client.post(f"/tags/{later}/write", data={"review_token": tokens[1]})
        else:
            response = await client.post("/tags/bulk-write-no-discrepancies", data={"review_tokens": tokens})
    assert response.status_code == 200
    assert ("Tag write failed" if boundary == "row" else "1 could not be dispatched") in response.text
    assert callback_pids and callback_pids[0] != authorization_pids[0]
    async with factory() as verify:
        assert await verify.scalar(select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == later)) == 0
        if boundary != "row":
            assert await verify.scalar(select(TagWriteLog.status).where(TagWriteLog.file_id == first)) == TagWriteStatus.QUEUED.value
    captures = router.captures
    assert len(captures) == (0 if boundary == "row" else 1)
    assert all(payload["file_id"] != str(later) for _, _, payload in captures)


async def test_metadata_callback_after_authorization_does_not_revoke_queued_work(committed_db):
    _engine, factory = committed_db
    async with factory() as seed:
        seed.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
        await seed.commit()
        file_id = await _seed_bulk_qualifying_file(seed, filename="Artist - Reviewed.mp3")
        await seed.execute(update(FileMetadata).where(FileMetadata.file_id == file_id).values(title="Authorized title"))
        await seed.commit()
        token = (await _review_tokens(seed, [file_id]))[0]
        file = await tags_module._get_file_with_metadata(seed, file_id)
        reviewed, proposed = await tags_module._validate_tag_review_token(seed, file, token)
        router = FakeTaskRouter()
        log = await enqueue_tag_write(seed, router, file, proposed, "proposal", review_source_versions=reviewed["sources"])
        log_id = log.id
    async with factory() as callback:
        agent = await callback.get(Agent, "test-fileserver")
        await put_metadata(file_id, MetadataWriteRequest(title="Later extraction"), agent, callback)
    async with factory() as verify:
        log = await verify.get(TagWriteLog, log_id)
        assert log.status == TagWriteStatus.QUEUED.value
        assert log.after_tags["title"] == "Authorized title"
    assert router.captures[0][2]["tags"]["title"] == "Authorized title"


@pytest.mark.parametrize("initial_metadata", [True, False])
async def test_authorization_fences_metadata_callback_until_commit(committed_db, initial_metadata):
    """Observe real update/FK-insert waiters, scoped to this callback and database."""
    import asyncio

    from sqlalchemy import text

    from phaze.services.tag_comparison import validate_reviewed_source_versions

    _engine, factory = committed_db
    async with factory() as seed:
        seed.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
        await seed.commit()
        file_id = await _seed_bulk_qualifying_file(seed, filename="Artist - Reviewed.mp3")
        if not initial_metadata:
            await seed.execute(delete(FileMetadata).where(FileMetadata.file_id == file_id))
            await seed.commit()
        token = (await _review_tokens(seed, [file_id]))[0]
        reviewed = tags_module._decode_tag_review_token(token, file_id)

    callback_pid = asyncio.get_running_loop().create_future()

    async def callback():
        async with factory() as session:
            callback_pid.set_result(await session.scalar(select(func.pg_backend_pid())))
            agent = await session.get(Agent, "test-fileserver")
            await put_metadata(file_id, MetadataWriteRequest(title="After authorization"), agent, session)

    async with factory() as authorization:
        await validate_reviewed_source_versions(authorization, file_id, reviewed["sources"])
        task = asyncio.create_task(callback())
        try:
            pid = await asyncio.wait_for(callback_pid, 5)

            async def blocked():
                async with factory() as observer:
                    while not await observer.scalar(
                        text(
                            "select exists (select 1 from pg_stat_activity where pid = :pid and datname = current_database() and wait_event_type = 'Lock')"
                        ),
                        {"pid": pid},
                    ):
                        # pg_stat_activity caches within a transaction. Refresh the snapshot.
                        await observer.commit()
                        await asyncio.sleep(0.01)

            await asyncio.wait_for(blocked(), 5)
            assert not task.done()
            await authorization.commit()
            await asyncio.wait_for(task, 5)
        finally:
            await authorization.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as verify:
        assert await verify.scalar(select(FileMetadata.title).where(FileMetadata.file_id == file_id)) == "After authorization"
