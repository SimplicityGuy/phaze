"""Selecting a SECTIONED remote snapshot maps one text section to the recording, exactly as a local selection does.

Read back through the real consumer, ``get_selected_recording_source``: a selection is only useful if it projects
the section's tracks. Snapshots are stored with the real persistence functions; every value is synthetic. The
last cases use the real ``MixesDbInterpreter`` output, the input class that exposed the gap (a MixesDB page under
an ``== Tracklist ==`` heading selected with zero tracks).
"""

from datetime import UTC, datetime
from typing import Any
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.provider_source import ProviderRecordingSelection
from phaze.schemas.remote_lookup import RemoteSourceDecision
from phaze.services.local_source_import import get_selected_recording_source, selection_token
from phaze.services.provider_persistence import add_candidate, store_snapshot
from phaze.services.remote_lookup import decide_remote_candidate
from phaze.tracklist_providers.domain import (
    Completeness,
    LoadBudget,
    ProviderCandidate,
    ProviderTrack,
    Snapshot,
    SourceIdentity,
    SourceRead,
    TrackSection,
)
from phaze.tracklist_providers.mixesdb import RIGHTS_EVIDENCE
from phaze.tracklist_providers.mixesdb_parser import MixesDbInterpreter


pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, tzinfo=UTC)
COMPLETE = Completeness(state="complete", reason="Section scanned")


def section(ordinal: int, title: str, **values: Any) -> TrackSection:
    return TrackSection(ordinal=ordinal, title=title, completeness=values.pop("completeness", COMPLETE), **values)


def snapshot(*sections: TrackSection, groups: tuple[int, ...] = (), scan_complete: bool = True, native_id: str = "set-01") -> Snapshot:
    """Tracks 1..n; ``groups[i]`` is the section of track i+1 (none when the snapshot has no sections)."""
    tracks = tuple(
        ProviderTrack(position=index, section_ordinal=group if sections else None, artist="Example Artist", title=f"Track {index}")
        for index, group in enumerate(groups or (1, 1, 1), 1)
    )
    return Snapshot(
        identity=SourceIdentity(provider_id="example_catalog", native_id=native_id),
        revision="rev-a",
        retrieved_at=NOW,
        tracks=tracks,
        sections=sections,
        section_scan_complete=scan_complete and bool(sections),
        source_format="example_set_json",
        parser_version="example-v1",
        completeness=Completeness(state="complete", reason="rows exhausted"),
    )


async def seed_media(sessions: async_sessionmaker[AsyncSession]) -> FileRecord:
    async with sessions() as session:
        if await session.get(Agent, "test-fileserver") is None:
            session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
            await session.flush()
        media_id = uuid.uuid4()
        media = FileRecord(
            id=media_id,
            agent_id="test-fileserver",
            original_path=f"/synthetic/{media_id}/recording.mp3",
            current_path=f"/synthetic/{media_id}/recording.mp3",
            original_filename="recording.mp3",
            file_type="mp3",
            sha256_hash="a" * 64,
            file_size=1,
        )
        session.add(media)
        await session.commit()
        return media


async def loaded(sessions: async_sessionmaker[AsyncSession], media: FileRecord, value: Snapshot) -> uuid.UUID:
    async with sessions() as session, session.begin():
        stored = await store_snapshot(session, value, status="found", channel="remote")
        await add_candidate(session, media.id, stored.observation.id, evidence=("kind:tracklist",))
        return stored.observation.id


async def select_it(sessions: async_sessionmaker[AsyncSession], media: FileRecord, observation_id: uuid.UUID, **values: Any) -> Any:
    async with sessions() as session, session.begin():
        current = await session.scalar(select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
        command = RemoteSourceDecision(
            media_id=media.id,
            observation_id=observation_id,
            decision_id=uuid.uuid4(),
            expected_selection_token=selection_token(current) if current else None,
            expected_revision=values.pop("expected_revision", "rev-a"),
            expected_media_sha256=media.sha256_hash,
            **values,
        )
        await decide_remote_candidate(session, command, actor="operator")
    async with sessions() as session:
        return await get_selected_recording_source(session, media.id)


def positions(selected: Any) -> list[int]:
    return [track.position for track in selected.tracks]


async def test_a_single_section_is_mapped_automatically_and_its_tracks_are_projected(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    selected = await select_it(sessions, media, await loaded(sessions, media, snapshot(section(1, "Tracklist"))))
    assert positions(selected) == [1, 2, 3] and selected.target_mapping["text_section_ordinal"] == 1


async def test_several_sections_without_an_ordinal_are_refused(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    observation_id = await loaded(sessions, media, snapshot(section(1, "Part 1"), section(2, "Part 2"), groups=(1, 1, 2)))
    with pytest.raises(ValueError, match="explicit text section-to-recording mapping"):
        await select_it(sessions, media, observation_id)
    async with sessions() as session:
        assert await get_selected_recording_source(session, media.id) is None


async def test_an_explicit_ordinal_selects_only_that_sections_tracks(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    observation_id = await loaded(sessions, media, snapshot(section(1, "Part 1"), section(2, "Part 2"), groups=(1, 1, 2)))
    selected = await select_it(sessions, media, observation_id, text_section_ordinal=2)
    assert positions(selected) == [3] and selected.target_mapping["text_section_ordinal"] == 2
    with pytest.raises(ValueError, match="explicit text section-to-recording mapping"):
        await select_it(sessions, media, observation_id, text_section_ordinal=3)


async def test_a_section_with_warnings_needs_explicit_acknowledgement(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    observation_id = await loaded(sessions, media, snapshot(section(1, "Tracklist", warnings=("missing_separator: lines 3",))))
    with pytest.raises(ValueError, match="Unresolved text row structure"):
        await select_it(sessions, media, observation_id)
    selected = await select_it(sessions, media, observation_id, accept_text_warnings=True)
    assert positions(selected) == [1, 2, 3]


async def test_an_incomplete_section_scan_or_section_is_refused(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    unscanned = await loaded(sessions, media, snapshot(section(1, "Tracklist"), scan_complete=False))
    with pytest.raises(ValueError, match="Incomplete text scan"):
        await select_it(sessions, media, unscanned)
    partial = snapshot(section(1, "Tracklist", completeness=Completeness(state="incomplete", reason="flawed")), native_id="set-02")
    with pytest.raises(ValueError, match="Absent or incomplete text section"):
        await select_it(sessions, media, await loaded(sessions, media, partial))


async def test_an_unsectioned_snapshot_is_unchanged(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    observation_id = await loaded(sessions, media, snapshot())
    with pytest.raises(ValueError, match="only valid for sectioned"):
        await select_it(sessions, media, observation_id, text_section_ordinal=1)
    selected = await select_it(sessions, media, observation_id)
    assert positions(selected) == [1, 2, 3] and "text_section_ordinal" not in selected.target_mapping


def test_the_new_fields_leave_existing_decision_payloads_unchanged() -> None:
    values = {
        "media_id": uuid.uuid4(),
        "observation_id": uuid.uuid4(),
        "decision_id": uuid.uuid4(),
        "expected_revision": None,
        "expected_media_sha256": "a" * 64,
    }
    dumped = RemoteSourceDecision(**values).model_dump(mode="json")
    # A replayed decision compares payloads, so a caller that sends neither field gets exactly the old payload.
    assert "text_section_ordinal" not in dumped and "accept_text_warnings" not in dumped
    mapped = RemoteSourceDecision(**values, text_section_ordinal=2, accept_text_warnings=True).model_dump(mode="json")
    assert mapped["text_section_ordinal"] == 2 and mapped["accept_text_warnings"] is True


def mixesdb_snapshot(wikitext: str, native_id: str) -> Snapshot:
    candidate = ProviderCandidate(identity=SourceIdentity(provider_id="mixesdb", native_id=native_id), url="https://www.mixesdb.com/w/example")
    content = SourceRead(status="found", code="http_200", scope=native_id, text=wikitext, revision="rev-a", retrieved_at=NOW)
    outcome = MixesDbInterpreter(rights_evidence=RIGHTS_EVIDENCE).interpret(candidate, content, LoadBudget())
    assert outcome.status == "found" and outcome.snapshot is not None
    return outcome.snapshot


async def test_a_mixesdb_tracklist_heading_selects_its_tracks(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    page = mixesdb_snapshot("== Tracklist ==\n# [00] Example Artist - Opening Track\n# [07] Other Artist - Closing Track\n", "Example_Set_01")
    assert [item.title for item in page.sections] == ["Tracklist"]
    selected = await select_it(sessions, media, await loaded(sessions, media, page))
    assert positions(selected) == [1, 2] and [track.title for track in selected.tracks] == ["Opening Track", "Closing Track"]


async def test_a_mixesdb_page_with_several_headings_needs_its_section_named(committed_db: Any) -> None:
    _, sessions = committed_db
    media = await seed_media(sessions)
    text = "== CD1 ==\n# Example Artist - First Track\n# Example Artist - Second Track\n== CD2 ==\n# Other Artist - Third Track\n"
    observation_id = await loaded(sessions, media, mixesdb_snapshot(text, "Example_Set_02"))
    with pytest.raises(ValueError, match="explicit text section-to-recording mapping"):
        await select_it(sessions, media, observation_id)
    selected = await select_it(sessions, media, observation_id, text_section_ordinal=2)
    assert positions(selected) == [3] and [track.title for track in selected.tracks] == ["Third Track"]
