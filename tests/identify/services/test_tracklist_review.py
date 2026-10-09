"""Offline recognition is distinct from importing track rows; exercise the real record consumer."""

from datetime import date
from typing import Any
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.proposal_context import build_file_context
from phaze.services.tracklist_review import detect_embedded_tracklist, get_file_tracklist_review


@pytest.mark.parametrize(
    "tags,expected",
    [
        (None, False),
        ({}, False),
        ({"artist": "00:00 A\n01:00 B\n02:00 C"}, False),
        ({"comment": "00:00 A\n01:00 B"}, False),
        ({"comment": 42}, False),
        ({"unsynced_lyrics": [None, "00:00 A\n01:00 B\n02:00 C"]}, True),
        ({"description": "00:00 A\n01:00 B\n02:00 C"}, True),
        ({"comment": "x" * 12000 + "\n00:00 A\n01:00 B\n02:00 C"}, False),
    ],
)
def test_descriptive_tag_recognition_is_bounded(tags: Any, expected: bool) -> None:
    assert detect_embedded_tracklist(tags) is expected


async def test_embedded_metadata_reaches_review_and_proposal_without_requests(
    session: AsyncSession, make_file: Any, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await make_file()
    tags = {"comment": "00:00 Artist - First\n01:00 Artist - Second\n02:00 Artist - Third"}
    metadata = FileMetadata(file_id=file.id, raw_tags=tags)
    session.add(metadata)
    await session.commit()

    async def reject_request(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("offline review attempted an HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", reject_request)
    review = await get_file_tracklist_review(session, file.id)
    assert review is not None and review.local_sources == ("embedded tracklist",)
    assert review.tracks == () and review.tracklist is None
    assert build_file_context(file, None, [], metadata=metadata)["tags"]["raw_tags"] == tags
    monkeypatch.undo()
    response = await client.get(f"/record/{file.id}")
    assert response.status_code == 200
    assert "embedded tracklist" in response.text
    assert "Recognition does not create stored track rows" in response.text


async def test_linked_text_recognition_uses_current_features_only(session: AsyncSession, make_file: Any) -> None:
    media = await make_file()
    sidecar = await make_file(original_filename="set.txt", file_type="txt")
    session.add(FileCompanion(media_id=media.id, companion_id=sidecar.id))
    features = CompanionContentFeatures(
        file_id=sidecar.id,
        agent_id=sidecar.agent_id,
        fingerprint=sidecar.sha256_hash,
        encoding="utf-8",
        byte_size=100,
        is_tracklist=True,
        extractor_version=1,
    )
    session.add(features)
    await session.commit()
    review = await get_file_tracklist_review(session, media.id)
    assert review is not None and review.local_sources == ("text companion tracklist",)
    assert review.tracks == ()
    features.fingerprint = "f" * 64
    await session.commit()
    review = await get_file_tracklist_review(session, media.id)
    assert review is not None and review.local_sources == ()


async def test_stored_metadata_null_tracks_and_local_source_coexist(session: AsyncSession, make_file: Any, client: httpx.AsyncClient) -> None:
    file = await make_file()
    cue = await make_file(original_filename="set.cue", file_type="cue")
    session.add(FileCompanion(media_id=file.id, companion_id=cue.id))
    tracklist = Tracklist(
        external_id=uuid.uuid4().hex,
        source_url="https://example.invalid/private-source",
        file_id=file.id,
        artist="Set Artist",
        event="Set Event",
        date=date(2024, 7, 1),
        propagated_from_set_key="k" * 64,
    )
    session.add(tracklist)
    await session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=2)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id
    session.add(TracklistTrack(version_id=version.id, position=1, artist=None, title=None, timestamp="00:00"))
    await session.commit()
    review = await get_file_tracklist_review(session, file.id)
    assert review is not None and review.is_propagated and review.local_sources == ("CUE companion",)
    assert len(review.tracks) == 1 and review.tracks[0].artist is None and review.tracks[0].title is None
    body = (await client.get(f"/record/{file.id}")).text
    assert "Set Artist" in body and "Set Event" in body and "2024-07-01" in body
    assert "imported " in body and "inherited from a duplicate" in body
    assert "https://example.invalid/private-source" not in body
    assert "data-track-row" in body


async def test_missing_and_empty_review_are_distinct(session: AsyncSession, make_file: Any) -> None:
    assert await get_file_tracklist_review(session, uuid.uuid4()) is None
    file = await make_file()
    review = await get_file_tracklist_review(session, file.id)
    assert review is not None and review.local_sources == () and review.tracks == ()
    tracklist = Tracklist(external_id=uuid.uuid4().hex, source_url="", file_id=file.id)
    session.add(tracklist)
    await session.commit()
    review = await get_file_tracklist_review(session, file.id)
    assert review is not None and review.latest_version is None and review.tracks == ()
