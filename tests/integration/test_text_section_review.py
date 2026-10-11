"""Text collection review never makes unrelated sections authoritative."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import literal, select

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObservation
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source
from phaze.services.selected_source_consumers import selected_track_membership_clause
from phaze.tracklist_providers.domain import LoadBudget
from tests.integration.test_local_source_import import decision, inventory


COLLECTION = "Part 1: First Set\n01. Artist - Opening\n02. Other - Next\nPart 2: Second Set\n01. Third - Closing\n"


async def test_framed_tracklist_import_and_review_preserve_rows_and_source(session) -> None:
    text = "| Tracklist:\n| 01. Artist - Opening\n| 02. Other - Closing\n"
    media, companion, raw = await inventory(session, text)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert imported.tracklist_status == "found"
    selected = await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id), actor="reviewer")
    assert selected.snapshot.text == text
    assert [(track.artist, track.title) for track in selected.tracks] == [("Artist", "Opening"), ("Other", "Closing")]
    assert selected.target_mapping["text_section_ordinal"] == 1


async def test_explicit_section_mapping_persists_and_filters_sql_membership(session) -> None:
    media, companion, raw = await inventory(session, COLLECTION)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    command = decision(media, companion, imported.tracklist_observation_id)
    with pytest.raises(ValueError, match="explicit text section"):
        await decide_local_source(session, command, actor="reviewer")
    selected = await decide_local_source(session, command.model_copy(update={"text_section_ordinal": 2}), actor="reviewer")
    assert selected.target_mapping["text_section_ordinal"] == 2
    assert [track.title for track in selected.tracks] == ["Closing"]
    assert [track.position for track in selected.tracks] == [3]
    assert len(selected.snapshot.tracks) == 3
    for position, expected in ((1, False), (2, False), (3, True)):
        query = (
            select(selected_track_membership_clause(literal(position)))
            .select_from(ProviderSourceObservation)
            .join(ProviderRecordingSelection, ProviderRecordingSelection.observation_id == ProviderSourceObservation.id)
            .where(ProviderRecordingSelection.media_id == media.id)
        )
        assert await session.scalar(query) is expected
    await session.commit()
    assert (await get_selected_recording_source(session, media.id)).tracks == selected.tracks


async def test_absent_section_does_not_block_review_of_complete_section(session) -> None:
    media, companion, raw = await inventory(session, "Part 1:\nTracklist unknown\nPart 2:\n01. Artist - Title\n")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert imported.tracklist_status == "incomplete"
    command = decision(media, companion, imported.tracklist_observation_id)
    with pytest.raises(ValueError, match="Absent or incomplete"):
        await decide_local_source(session, command.model_copy(update={"text_section_ordinal": 1}), actor="reviewer")
    selected = await decide_local_source(session, command.model_copy(update={"text_section_ordinal": 2}), actor="reviewer")
    assert [track.title for track in selected.tracks] == ["Title"]


@pytest.mark.parametrize("budget", [LoadBudget(max_tracks=1), LoadBudget(max_lines=2), LoadBudget(max_characters=50)])
async def test_truncated_collection_cannot_bypass_completeness_by_mapping(session, budget) -> None:
    media, companion, raw = await inventory(session, COLLECTION)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw, budget=budget))
    command = decision(media, companion, imported.tracklist_observation_id).model_copy(update={"text_section_ordinal": 1})
    with pytest.raises(ValueError, match="complete parsed"):
        await decide_local_source(session, command, actor="reviewer")
    assert await get_selected_recording_source(session, media.id) is None


async def test_structural_unknowns_need_explicit_ack_and_remain_unknown(session) -> None:
    media, companion, raw = await inventory(session, "Tracklist:\n01. A - B / C - D\n02. Other - Closing\n")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    command = decision(media, companion, imported.tracklist_observation_id)
    with pytest.raises(ValueError, match="explicit acknowledgement"):
        await decide_local_source(session, command, actor="reviewer")
    selected = await decide_local_source(session, command.model_copy(update={"accept_text_warnings": True}), actor="reviewer")
    assert selected.tracks[0].artist is selected.tracks[0].title is None
    assert selected.tracks[1].title == "Closing"
    assert selected.tracks[0].timestamp is None


async def test_native_form_can_choose_a_section_and_shows_absent_sections(session) -> None:
    media, companion, raw = await inventory(session, "Part 1:\nNo tracklist\nPart 2:\n01. Artist - Title\n")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await session.commit()
    app = create_app()

    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(f"/files/{media.id}/companion-observations/{imported.tracklist_observation_id}", headers={"HX-Request": "true"})
        assert page.status_code == 200
        assert "text_section_ordinal" in page.text and "Section 1" in page.text and "Section 2" in page.text
        assert "no_tracklist_present" in page.text
        command = decision(media, companion, imported.tracklist_observation_id).model_copy(update={"text_section_ordinal": 2})
        payload = {key: str(value) if value is not None else "" for key, value in command.model_dump().items()}
        # Browser forms omit unchecked booleans, matching the native controls.
        payload.pop("accept_text_warnings")
        payload.pop("accept_conflict")
        response = await client.post("/api/local-sources/decision", data=payload, headers={"HX-Request": "true", "Origin": "http://test"})
        assert response.status_code == 200
        assert (await get_selected_recording_source(session, media.id)).target_mapping["text_section_ordinal"] == 2


async def test_review_replay_cannot_change_section_intent(session) -> None:
    media, companion, raw = await inventory(session, COLLECTION)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    command = decision(media, companion, imported.tracklist_observation_id, identifier=uuid.uuid4()).model_copy(update={"text_section_ordinal": 1})
    await decide_local_source(session, command, actor="reviewer")
    with pytest.raises(ValueError, match="different intent"):
        await decide_local_source(session, command.model_copy(update={"text_section_ordinal": 2}), actor="reviewer")
