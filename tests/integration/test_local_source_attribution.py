"""Equal bounded interpretations retain the exact acquisition that produced them."""

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord
from phaze.models.provider_source import ProviderSourceObservation
from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureReport, CaptureTarget
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.companion_capture import store_capture_report
from phaze.services.companion_capture_worker import read_capture
from phaze.services.companion_import_backfill import classify_source
from phaze.services.companion_view import source_review_context
from phaze.services.local_source_import import import_local_source
from tests.integration.test_companion_import_backfill import features
from tests.integration.test_local_source_import import inventory


@pytest.mark.parametrize("second_cap", [140000, 160000])
async def test_equal_interpretations_keep_each_raw_attribution(session: AsyncSession, tmp_path: Path, second_cap: int) -> None:
    prefix = "Artist: Example Artist\nAlbum: Example Album\n01. Artist - Opening\n02. Artist - Closing\n"
    content = prefix + " " * (170000 - len(prefix))
    media, companion, _initial_raw = await inventory(session, prefix)
    path = tmp_path.resolve() / "notes.txt"
    path.write_text(content, encoding="utf-8")
    companion.original_path = companion.current_path = str(path)
    companion.sha256_hash = hashlib.sha256(content.encode()).hexdigest()
    companion.file_size = 170000
    await features(session, companion)
    target = CaptureTarget(
        file_id=companion.id,
        media_id=media.id,
        path=str(path),
        expected_sha256=companion.sha256_hash,
        expected_size=170000,
    )

    async def acquire(cap: int):
        budget = CaptureBudget(max_characters=cap)
        read = read_capture(target, budget, [str(tmp_path.resolve())])
        assert read.bytes_read == 170000 and read.revision_scope == "full"
        assert read.status == "incomplete" and len(read.text) == cap
        captured = await store_capture_report(session, companion.agent_id, CaptureReport(target=target, budget=budget, read=read))
        imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=captured.observation_id))
        return captured.observation_id, imported

    async with asyncio.timeout(10):
        first_raw, first = await acquire(140000)
        old_rows = {}
        for identifier in (first_raw, first.tracklist_observation_id, first.release_observation_id):
            row = await session.get(ProviderSourceObservation, identifier)
            old_rows[identifier] = (row.parent_id, row.payload, row.conflict_with_id)
        second_raw, second = await acquire(second_cap)
        assert (second_raw == first_raw) == (second_cap == 140000)
        assert (second.tracklist_observation_id == first.tracklist_observation_id) == (second_cap == 140000)
        assert (second.release_observation_id == first.release_observation_id) == (second_cap == 140000)
        current_ids = {second.tracklist_observation_id, second.release_observation_id}
        for old_id, new_id in zip(
            (first.tracklist_observation_id, first.release_observation_id),
            (second.tracklist_observation_id, second.release_observation_id),
            strict=True,
        ):
            old = await session.get(ProviderSourceObservation, old_id)
            new = await session.get(ProviderSourceObservation, new_id)
            assert new.payload == old.payload
            assert new.parent_id == second_raw and new.conflict_with_id is None
            assert len(new.decoded_text) == 131072
        await session.commit()

        async def fresh_consumers():
            # New identity map, same fixture connection/outer transaction; no cached ORM rows.
            async with AsyncSession(bind=session.bind, join_transaction_mode="create_savepoint") as fresh:
                current_file = await fresh.get(FileRecord, companion.id)
                row = await fresh.get(ProviderSourceObservation, second.tracklist_observation_id)
                context = await source_review_context(fresh, media.id, row.object_id)
                assert context["raw_id"] == second_raw
                assert {item["detail"].summary.observation_id for item in context["interpretations"]} == current_ids
                assert (await classify_source(fresh, current_file))[0] == "incomplete"
                retained = (await fresh.scalars(select(ProviderSourceObservation).where(ProviderSourceObservation.id.in_(old_rows)))).all()
                assert {row.id: (row.parent_id, row.payload, row.conflict_with_id) for row in retained} == old_rows

        await fresh_consumers()
        repeated = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=second_raw))
        assert repeated == second
        await session.commit()
        await fresh_consumers()
