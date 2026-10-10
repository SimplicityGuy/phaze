"""Independent Postgres sessions verify idempotent imports and reviewed decision races."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import (
    ProviderRecordingCandidate,
    ProviderRecordingSelection,
    ProviderSelectionEvent,
    ProviderSourceObject,
    ProviderSourceObservation,
)
from phaze.schemas.agent_companion_capture import CaptureReport, CaptureTarget
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.companion_capture import store_capture_report
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source
from phaze.services.provider_persistence import lock_source_bucket
from phaze.services.scan_deletion import delete_file_cascade
from phaze.tracklist_providers.domain import SourceIdentity, SourceRead
from tests.db_guard import BLOCKED_WAITER_SQL
from tests.integration.test_local_source_import import decision, inventory


async def wait_for_backend_lock(db, backend_pid):
    """Prove this importer has queued on its DB lock, not merely not been scheduled."""
    sql = BLOCKED_WAITER_SQL.replace("a.datname = current_database()", "a.datname = current_database() AND a.pid = :pid")
    async with asyncio.timeout(5):
        while not await db.scalar(text(sql), {"pid": backend_pid}):
            await asyncio.sleep(0.005)


async def cleanup(maker, media, companion):
    # async_engine uses metadata create_all (no immutable migration triggers); migration
    # tests use a separate DB and reset the schema instead of deleting retained evidence.
    async with maker.begin() as db:
        objects = select(ProviderSourceObject.id).where(ProviderSourceObject.original_file_id.in_([media.id, companion.id]))
        observations = select(ProviderSourceObservation.id).where(ProviderSourceObservation.object_id.in_(objects))
        await db.execute(delete(ProviderSelectionEvent).where(ProviderSelectionEvent.observation_id.in_(observations)))
        await db.execute(delete(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
        await db.execute(delete(ProviderRecordingCandidate).where(ProviderRecordingCandidate.media_id == media.id))
        # Remove child interpretations before their raw parent.
        await db.execute(
            delete(ProviderSourceObservation).where(
                ProviderSourceObservation.object_id.in_(objects), ProviderSourceObservation.parent_id.is_not(None)
            )
        )
        await db.execute(delete(ProviderSourceObservation).where(ProviderSourceObservation.object_id.in_(objects)))
        await db.execute(delete(ProviderSourceObject).where(ProviderSourceObject.id.in_(objects)))
        await delete_file_cascade(db, media.id)
        await delete_file_cascade(db, companion.id)


async def test_concurrent_imports_converge_and_only_one_initial_decision_wins(async_engine):
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker.begin() as db:
        media, companion, raw = await inventory(db)
    try:

        async def import_one():
            async with maker.begin() as db:
                return await import_local_source(db, ImportLocalSource(media_id=media.id, raw_observation_id=raw))

        imports = await asyncio.wait_for(asyncio.gather(*(import_one() for _ in range(4))), timeout=10)
        assert len({result.tracklist_observation_id for result in imports}) == 1
        commands = [decision(media, companion, imports[0].tracklist_observation_id) for _ in range(2)]

        async def choose(command):
            async with maker.begin() as db:
                return await decide_local_source(db, command, actor="reviewer")

        outcomes = await asyncio.wait_for(asyncio.gather(*(choose(command) for command in commands), return_exceptions=True), timeout=10)
        assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 1
        winner = next(outcome for outcome in outcomes if not isinstance(outcome, Exception))
        winning_command = next(command for command in commands if command.decision_id == winner.selection_token)
        async with maker.begin() as db:
            assert (await decide_local_source(db, winning_command, actor="reviewer")).selection_token == winner.selection_token
            assert await db.scalar(select(func.count()).select_from(ProviderSelectionEvent).where(ProviderSelectionEvent.media_id == media.id)) == 1
    finally:
        await cleanup(maker, media, companion)


@pytest.mark.parametrize("change", ["relink", "delete"])
async def test_import_waits_for_inventory_change_then_refuses_stale_applicability(async_engine, change):
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker.begin() as db:
        media, companion, raw = await inventory(db)
        first = await import_local_source(db, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
        selected = await decide_local_source(db, decision(media, companion, first.tracklist_observation_id), actor="reviewer")
    task = None
    waiter_pid = asyncio.get_running_loop().create_future()
    try:
        async with maker.begin() as changing:
            await changing.execute(select(FileRecord.id).where(FileRecord.id == companion.id).with_for_update())

            async def importing():
                async with maker.begin() as db:
                    waiter_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                    return await import_local_source(db, ImportLocalSource(media_id=media.id, raw_observation_id=raw))

            task = asyncio.create_task(importing())
            await wait_for_backend_lock(changing, await waiter_pid)
            if change == "delete":
                await asyncio.wait_for(delete_file_cascade(changing, companion.id), timeout=5)
            else:
                await changing.execute(delete(FileCompanion).where(FileCompanion.companion_id == companion.id))
        with pytest.raises(ValueError, match=r"no longer|not current|owned raw"):
            await asyncio.wait_for(task, timeout=5)
        async with maker() as db:
            retained = await get_selected_recording_source(db, media.id)
            assert retained.observation_id == selected.observation_id
            assert retained.availability in {"missing", "unlinked"}
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await cleanup(maker, media, companion)


async def test_report_and_import_share_bucket_before_globally_sorted_inventory(async_engine):
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    async with maker.begin() as db:
        media, companion, raw = await inventory(db)
        media.original_path = "/synthetic/a-recording.mp3"
        companion.original_path = "/synthetic/z-notes.txt"
    reporting = importing = None
    release = asyncio.Event()
    ready = asyncio.Event()
    waiter_pid = asyncio.get_running_loop().create_future()
    try:

        async def report():
            async with maker.begin() as db:
                await lock_source_bucket(db, SourceIdentity(provider_id="local", native_id=f"companion:{companion.id}"))
                await db.execute(select(FileRecord.id).where(FileRecord.id == companion.id).with_for_update())
                ready.set()
                await release.wait()
                return await store_capture_report(
                    db,
                    media.agent_id,
                    CaptureReport(
                        target=CaptureTarget(
                            file_id=companion.id,
                            path=companion.current_path,
                            expected_sha256=companion.sha256_hash,
                            expected_size=companion.file_size,
                        ),
                        read=SourceRead(status="unavailable", code="offline", scope=str(companion.id), retrieved_at=datetime.now(UTC)),
                    ),
                )

        async def import_one():
            async with maker.begin() as db:
                waiter_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                return await import_local_source(db, ImportLocalSource(media_id=media.id, raw_observation_id=raw))

        reporting = asyncio.create_task(report())
        await ready.wait()
        importing = asyncio.create_task(import_one())
        async with maker() as observer:
            await wait_for_backend_lock(observer, await waiter_pid)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(reporting, importing), timeout=5)
        assert results[1].tracklist_status == "found"
    finally:
        release.set()
        for task in (reporting, importing):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (reporting, importing) if task is not None), return_exceptions=True)
        await cleanup(maker, media, companion)
