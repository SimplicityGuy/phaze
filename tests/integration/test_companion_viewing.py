"""Real stored read/import/review and page/drawer HTML transport."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from bs4 import BeautifulSoup
from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.main import create_app
from phaze.models.agent import Agent
from phaze.models.companion_import import ProviderAcquisitionAttempt
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObject, ProviderSourceObservation
from phaze.schemas.companion_details import DetailPage
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.companion_capture import store_capture_report
from phaze.services.companion_details import get_companion_details
from phaze.services.companion_view import source_review_context, visible_parsed_content
from phaze.services.local_source_import import decide_local_source, import_local_source
from phaze.services.provider_persistence import add_candidate, store_snapshot
from phaze.tracklist_providers.domain import LoadBudget, Snapshot
from phaze.tracklist_providers.local_parser import PARSER_VERSION
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSION
from tests.integration.test_companion_import_backfill import report
from tests.integration.test_local_source_import import CUE, decision, inventory


TEXT = 'EVENT: Example Festival\nDATE: 2024-06-02\nVENUE: Example Hall\nLABEL: Example Records\nQUALITY: 320 kbps\n01. Artist - Opening\n02. Other - Finale\n<script>alert("synthetic")</script>\n'
HX = {"HX-Request": "true", "Origin": "http://test"}


def _primary_text(html: str) -> str:
    """Closed native disclosures keep diagnostic identifiers out of the main summary."""
    soup = BeautifulSoup(html, "html.parser")
    for details in soup.select("details"):
        details.decompose()
    return soup.get_text(" ", strip=True)


@pytest.mark.parametrize("kind", ["release_metadata", "tracklist", "cue"])
async def test_workspace_source_summary_keeps_exact_version_in_details(client: AsyncClient, session: AsyncSession, kind: str) -> None:
    media, companion, raw = await inventory(session, CUE if kind == "cue" else TEXT, "cue" if kind == "cue" else "nfo")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    observation_id = imported.release_observation_id if kind == "release_metadata" else imported.tracklist_observation_id
    observation = await session.get(ProviderSourceObservation, observation_id)
    assert observation is not None
    response = await client.get(f"/pipeline/local-source-sets?kind={kind}", headers=HX)
    soup = BeautifulSoup(response.text, "html.parser")
    row = next(row for row in soup.select("tbody tr") if str(observation_id) in row.get_text())
    primary = _primary_text(str(row))
    assert "Companion file" in primary and "Current source version" in primary and "Awaiting review" in primary
    assert str(observation_id) not in primary and companion.sha256_hash not in primary and observation.parser_version not in primary
    details = row.select_one("details")
    assert details is not None and not details.has_attr("open")
    assert str(observation_id) in details.get_text() and companion.sha256_hash in details.get_text()
    assert observation.parser_version in details.get_text() and "full" in details.get_text()
    assert row.find("a", href=f"/files/{media.id}#companion-sources") is not None
    if kind == "cue":
        # The CUE workspace contains separate release-info and tracklist interpretations.
        for identifier in (imported.tracklist_observation_id, imported.release_observation_id):
            interpretation = next(row for row in soup.select("tbody tr") if str(identifier) in row.get_text())
            assert "CUE source" in _primary_text(str(interpretation))
            assert "CUE tracklist" not in _primary_text(str(interpretation))


@pytest.mark.parametrize("state", ["stale", "missing", "unknown_binding"])
async def test_selected_source_warning_visible_on_workspace_and_both_record_surfaces(client: AsyncClient, session: AsyncSession, state: str) -> None:
    from sqlalchemy import select

    media, companion, raw = await inventory(session)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id), actor="reviewer")
    if state == "stale":
        companion.sha256_hash = "b" * 64
    elif state == "missing":
        companion.missing_at = datetime.now(UTC)
    else:
        selection = await session.scalar(select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
        assert selection is not None
        selection.target_mapping = None
    await session.flush()
    expected = {"stale": "Source version changed", "missing": "Source file missing", "unknown_binding": "Recording binding not verified"}[state]
    for path in ("/pipeline/local-source-sets?kind=tracklist", f"/files/{media.id}", f"/record/{media.id}"):
        response = await client.get(path, headers=HX)
        soup = BeautifulSoup(response.text, "html.parser")
        container = soup.select_one("tbody tr") if path.startswith("/pipeline") else soup.select_one(f"#tracklist-review-{media.id}")
        assert container is not None
        assert expected in _primary_text(str(container))
        assert "Current source version" not in _primary_text(str(container))
        assert str(imported.tracklist_observation_id) in container.select_one("details").get_text()
    if state != "unknown_binding":
        detail = await client.get(f"/files/{media.id}/companion-observations/{imported.tracklist_observation_id}", headers=HX)
        assert expected in _primary_text(detail.text)
        assert BeautifulSoup(detail.text, "html.parser").find("button", string="Use this tracklist") is None


async def test_partial_source_warning_precedes_valid_section_choice_and_selected_record_summary(client: AsyncClient, session: AsyncSession) -> None:
    text = "Part 1: Broken\n01. Artist - First\n01. Artist - Duplicate\nPart 2: Complete\n01. Other - Closing\n"
    media, companion, raw = await inventory(session, text)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert imported.tracklist_status == "incomplete"
    detail = await client.get(f"/files/{media.id}/companion-observations/{imported.tracklist_observation_id}", headers=HX)
    soup = BeautifulSoup(detail.text, "html.parser")
    assert "Partial extraction" in _primary_text(detail.text)
    version = soup.find("summary", string="Version details and review evidence").parent
    assert str(imported.tracklist_observation_id) in version.get_text()
    assert companion.sha256_hash in version.get_text() and media.sha256_hash in version.get_text()
    assert str(imported.tracklist_observation_id) not in _primary_text(detail.text)
    form = soup.select_one("[data-source-decision]")
    assert form is not None and form.find("button", string="Use this tracklist") is not None
    values = {field["name"]: field.get("value", "") for field in form.select('input[type="hidden"]')}
    values.update(action="select", text_section_ordinal="2")
    selected = await client.post(form["hx-post"], data=values, headers=HX)
    assert "Source selected" in selected.text
    for path in (f"/files/{media.id}", f"/record/{media.id}"):
        record = BeautifulSoup((await client.get(path)).text, "html.parser")
        tracks = record.select_one(f"#tracklist-review-{media.id}")
        assert "Partial extraction" in _primary_text(str(tracks))
        assert "Selected tracklist · 1 tracks" in _primary_text(str(tracks))
        assert "Stored tracklist" not in _primary_text(str(tracks))
        assert "Closing" in tracks.get_text() and "Duplicate" not in tracks.get_text()


async def test_cue_preview_retains_pin_in_details_and_refuses_stale_source(client: AsyncClient, session: AsyncSession) -> None:
    from tests.integration.test_selected_source_consumers import CUE as TIMED_CUE

    media, companion, raw = await inventory(session, TIMED_CUE, "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    response = await client.get(f"/cue/files/{media.id}/preview", headers=HX)
    soup = BeautifulSoup(response.text, "html.parser")
    primary = _primary_text(response.text)
    assert "Selected tracklist · current source version" in primary
    assert str(imported.tracklist_observation_id) not in primary and companion.sha256_hash not in primary
    assert "INDEX 01 00:01:01" in primary
    details = soup.select_one("details")
    assert str(imported.tracklist_observation_id) in details.get_text() and companion.sha256_hash in details.get_text()
    companion.sha256_hash = "b" * 64
    await session.flush()
    stale = await client.get(f"/cue/files/{media.id}/preview", headers=HX)
    assert "Selected source is stale" in stale.text
    assert "Generate reviewed CUE artifact" not in stale.text and "<pre" not in stale.text


async def test_unknown_extraction_is_visible_without_claiming_partial_or_complete(client: AsyncClient, session: AsyncSession) -> None:
    media, _companion, raw = await inventory(session)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    observation = await session.get(ProviderSourceObservation, imported.tracklist_observation_id)
    assert observation is not None
    observation.payload = {**observation.payload, "completeness": None}
    await session.flush()
    for path in ("/pipeline/local-source-sets?kind=tracklist", f"/files/{media.id}/companion-observations/{observation.id}"):
        response = await client.get(path, headers=HX)
        primary = _primary_text(response.text)
        assert "Extraction completeness unknown" in primary
        assert "Partial extraction" not in primary
        assert "complete section" not in primary


@pytest.mark.parametrize("kind", ["mp3", "mp4"])
async def test_music_video_both_interpretations_visible_page_drawer_and_lazy_text(client, session, kind):
    media, companion, raw = await inventory(session, TEXT, "nfo" if kind == "mp3" else "txt")
    media.file_type = kind
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await session.flush()
    rendered = []
    for path in (f"/files/{media.id}", f"/record/{media.id}"):
        response = await client.get(path)
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        assert "Artist" in soup.select_one("[data-visible-track]").get_text()
        facts = soup.select("[data-visible-release-field]")
        assert any("Example Festival" in fact.get_text() for fact in facts)
        assert any("Example Records" in fact.get_text() for fact in facts)
        assert not soup.select("[data-companion-text]")
        rendered.append(soup.select_one("[data-record-content]").get_text())
    assert rendered[0] == rendered[1]
    history = await client.get(
        f"/files/{media.id}/companion-sources/{(await session.get(ProviderSourceObservation, raw)).object_id}/observations",
        headers=HX,
    )
    assert str(result.tracklist_observation_id) in history.text and str(result.release_observation_id) in history.text
    detail = await client.get(f"/files/{media.id}/companion-observations/{result.release_observation_id}", headers=HX)
    assert "Example Festival" in detail.text and "data-source-decision" in detail.text
    assert "&lt;script&gt;" in detail.text and "data-companion-text" in detail.text
    text = await client.get(f"/files/{media.id}/companion-observations/{raw}/text", headers=HX)
    assert "&lt;script&gt;" in text.text and "<script>alert(" not in text.text and "whitespace-pre-wrap" in text.text
    assert f"/files/{companion.id}#companion-sources" in detail.text
    reverse = await client.get(f"/files/{companion.id}/linked-media", headers=HX)
    assert media.original_filename in reverse.text


async def test_current_text_sections_warnings_and_native_mapping_visible_page_drawer(client, session):
    text = "Part 1: First\n01. A - B / C - D\nPart 2: Second\n01. Other - Closing\n"
    media, companion, raw = await inventory(session, text)
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    for path in (f"/files/{media.id}", f"/record/{media.id}"):
        page = await client.get(path)
        assert page.status_code == 200
        soup = BeautifulSoup(page.text, "html.parser")
        sections = soup.select_one("[data-visible-text-sections]").get_text()
        assert "Section 1" in sections and "Section 2" in sections and "ambiguous_artist_title" in sections
        assert "[section 2]" in soup.select("[data-visible-track]")[1].get_text()
    detail_url = f"/files/{media.id}/companion-observations/{result.tracklist_observation_id}"
    detail = await client.get(detail_url, headers=HX)
    assert 'name="text_section_ordinal"' in detail.text and 'name="accept_text_warnings"' in detail.text
    command = decision(media, companion, result.tracklist_observation_id).model_copy(update={"text_section_ordinal": 1})
    payload = {key: str(value) if value is not None else "" for key, value in command.model_dump().items()}
    payload.pop("accept_text_warnings")
    refused = await client.post("/api/local-sources/decision", data=payload, headers=HX)
    assert 'role="alert"' in refused.text and "explicit acknowledgement" in refused.text
    payload["accept_text_warnings"] = "true"
    accepted = await client.post("/api/local-sources/decision", data=payload, headers=HX)
    assert accepted.status_code == 200 and 'role="alert"' not in accepted.text
    selected = await client.get(detail_url, headers=HX)
    rows = BeautifulSoup(selected.text, "html.parser").select("[data-local-track]")
    assert len(rows) == 1 and "[section 1]" in rows[0].get_text() and "Title unknown" in rows[0].get_text()


@pytest.mark.parametrize(
    "stored_sections",
    ["missing", None, True, 7, "[]", {"ordinal": 1}, []],
    ids=["missing", "null", "boolean", "number", "string", "object", "empty-array"],
)
async def test_legacy_sections_shapes_preserve_current_and_legacy_record_views(
    client: AsyncClient, session: AsyncSession, stored_sections: object
) -> None:
    """Real JSONB rows must not turn absent or non-array historical sections into page failures."""
    media, companion, raw = await inventory(session, "Artist: Example Artist\nPart 1: Opening\n01. Artist - First\n", "nfo")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    current_rows = [
        await session.get(ProviderSourceObservation, identifier)
        for identifier in (imported.tracklist_observation_id, imported.release_observation_id)
    ]
    expected = {}
    retained_payloads = {}
    for current in current_rows:
        legacy_version = "local-release-v1" if current.parser_version == RELEASE_PARSER_VERSION else "local-tracklist-v1"
        historical = Snapshot.model_validate({**current.payload, "retrieved_at": current.retrieved_at, "parser_version": legacy_version})
        stored = await store_snapshot(session, historical, source_file_id=companion.id, channel="companion", parent_id=raw)
        legacy = stored.observation
        # These synthetic payloads represent persisted older data, without today's model defaults.
        payload = {key: value for key, value in legacy.payload.items() if key != "sections"}
        if stored_sections != "missing":
            payload["sections"] = stored_sections
        legacy.payload = payload
        await add_candidate(
            session, media.id, legacy.id, evidence=(f"kind:{'release_metadata' if legacy_version.startswith('local-release') else 'tracklist'}",)
        )
        expected[legacy.id] = (legacy_version, 0, ())
        expected[current.id] = (
            current.parser_version,
            len(current.payload["sections"]),
            tuple(item["ordinal"] for item in current.payload["sections"]),
        )
        retained_payloads[legacy.id] = payload.copy()
    await session.flush()
    details = await get_companion_details(session, media.id)
    visible = await visible_parsed_content(session, details)
    assert {item.summary.observation_id for item in visible} == set(expected)
    assert [item.summary.parser_version for item in visible] == sorted(version for version, _count, _ordinals in expected.values())
    for item in visible:
        version, count, ordinals = expected[item.summary.observation_id]
        assert item.summary.parser_version == version
        assert item.section_count == count
        assert tuple(section.ordinal for section in item.sections) == ordinals
        assert item.summary.availability == "current"
    for path in (f"/files/{media.id}", f"/record/{media.id}"):
        response = await client.get(path)
        assert response.status_code == 200
        assert "First" in response.text and "Example Artist" in response.text
    companion.sha256_hash = "b" * 64
    await session.flush()
    stale = await visible_parsed_content(session, details)
    assert {item.summary.observation_id for item in stale} == set(expected)
    assert {item.summary.availability for item in stale} == {"stale"}
    outside_page = await get_companion_details(session, media.id, source_page=DetailPage(offset=100000))
    assert outside_page.sources == () and await visible_parsed_content(session, outside_page) == ()
    for identifier, payload in retained_payloads.items():
        await session.refresh(await session.get(ProviderSourceObservation, identifier))
        assert (await session.get(ProviderSourceObservation, identifier)).payload == payload


async def test_valid_section_array_keeps_full_count_bounded_preview_and_other_pagination(client: AsyncClient, session: AsyncSession) -> None:
    """Counting all valid sections must not expand the 64-section, 3-track or 6-fact previews."""
    text = "Artist: Example Artist\nAlbum: Example Album\nGenre: Electronic\nSource: Soundboard\n" + TEXT
    text = text.replace("02. Other - Finale", "02. Other - Finale\n03. Artist - Third\n04. Artist - Fourth")
    media, _companion, raw = await inventory(session, text, "nfo")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    sections = [
        {"ordinal": ordinal, "title": f"Section {ordinal}", "completeness": {"state": "complete", "reason": "Synthetic section"}}
        for ordinal in range(1, 71)
    ]
    identifiers = {imported.tracklist_observation_id, imported.release_observation_id}
    for identifier in identifiers:
        observation = await session.get(ProviderSourceObservation, identifier)
        observation.payload = {**observation.payload, "sections": sections}
    await session.flush()
    details = await get_companion_details(session, media.id)
    visible = await visible_parsed_content(session, details)
    assert {item.summary.observation_id for item in visible} == identifiers
    for item in visible:
        assert item.section_count == 70
        assert tuple(section.ordinal for section in item.sections) == tuple(range(1, 65))
        assert len(item.tracks) == min(3, item.summary.track_count)
        assert len(item.release_facts) == min(6, item.summary.release_fact_count)
        assert item.next_track_offset == (3 if item.summary.track_count > 3 else None)
        assert item.next_fact_offset == (6 if item.summary.release_fact_count > 6 else None)
    assert any(item.next_track_offset == 3 for item in visible)
    assert any(item.next_fact_offset == 6 for item in visible)
    for path in (f"/files/{media.id}", f"/record/{media.id}"):
        response = await client.get(path)
        assert response.status_code == 200
        section_preview = BeautifulSoup(response.text, "html.parser").select_one("[data-visible-text-sections]")
        assert "Section 64" in section_preview.get_text() and "Section 65" not in section_preview.get_text()


async def test_json_and_form_contracts_selection_stale_errors_and_reimport(client, session):
    media, companion, raw = await inventory(session)
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    command = decision(media, companion, result.tracklist_observation_id)
    response = await client.post("/api/local-sources/decision", json=command.model_dump(mode="json"))
    assert response.status_code == 200 and response.json()["observation_id"] == str(result.tracklist_observation_id)
    html = await client.get(f"/api/local-sources/recordings/{media.id}/selected", headers=HX)
    assert "Selected tracklist" in html.text
    stale = command.model_copy(update={"decision_id": uuid.uuid4(), "expected_selection_token": None})
    refused = await client.post(
        "/api/local-sources/decision", data={key: str(value) if value is not None else "" for key, value in stale.model_dump().items()}, headers=HX
    )
    assert refused.status_code == 200 and 'role="alert"' in refused.text and "Selected source changed" in refused.text
    reimport = await client.post("/api/local-sources/reimport", data={"media_id": str(media.id), "raw_observation_id": str(raw)}, headers=HX)
    assert reimport.status_code == 200 and "Review it below" in reimport.text
    assert (await client.post("/api/local-sources/import", content="{bad", headers={"Content-Type": "application/json"})).status_code == 422
    assert (await client.post("/api/local-sources/decision", json={"media_id": str(media.id)})).status_code == 422
    invalid_form = await client.post(
        "/api/local-sources/import", data={"media_id": str(media.id), "embedded_channel": "password", "unexpected": "x"}, headers=HX
    )
    assert 'role="alert"' in invalid_form.text
    schema = create_app().openapi()
    for path, title in [("/api/local-sources/import", "ImportLocalSource"), ("/api/local-sources/decision", "SourceDecision")]:
        body = schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body["title"] == title and "media_id" in body["required"]


async def test_native_embedded_import_and_empty_text_are_inspectable(client, session):
    media, _companion, raw = await inventory(session, "Unrecognized release prose\n", "txt")
    session.add(FileMetadata(file_id=media.id, raw_tags={"Comments": "01. Tag Artist - Tag Title"}))
    await session.flush()
    response = await client.post("/api/local-sources/import", data={"media_id": str(media.id), "embedded_channel": "Comments"}, headers=HX)
    assert "Content extracted" in response.text
    page = await client.get(f"/files/{media.id}")
    assert "Tag Title" in page.text and "Extract Comments tag" in page.text
    unparsed = await client.get(f"/files/{media.id}/companion-observations/{raw}", headers=HX)
    assert "No tracks recognized" in unparsed.text and "Unrecognized release prose" in unparsed.text


async def test_companion_and_source_cursors_are_independent(client, session):
    media, _companion, _raw = await inventory(session)
    for index in range(51):
        identifier = uuid.uuid4()
        session.add(
            FileRecord(
                id=identifier,
                agent_id=media.agent_id,
                original_path=f"/synthetic/notes-{index}.txt",
                current_path=f"/synthetic/notes-{index}.txt",
                original_filename=f"notes-{index}.txt",
                file_type="txt",
                sha256_hash="b" * 64,
                file_size=0,
            )
        )
        await session.flush()
        session.add(FileCompanion(companion_id=identifier, media_id=media.id))
        session.add(
            ProviderSourceObject(
                provider_id="local",
                native_id=f"companion:{identifier}",
                native_digest="c" * 64,
                source_file_id=identifier,
                original_file_id=identifier,
                channel="companion",
            )
        )
    await session.flush()

    async def rendered(link_offset, source_offset):
        response = await client.get(f"/files/{media.id}/companion-details?link_offset={link_offset}&source_offset={source_offset}", headers=HX)
        assert response.status_code == 200
        return BeautifulSoup(response.text, "html.parser")

    first = await rendered(0, 0)
    link_page = await rendered(20, 0)
    source_page = await rendered(20, 20)
    assert [link.get_text() for link in first.select("[data-companion-link]")] != [
        link.get_text() for link in link_page.select("[data-companion-link]")
    ]
    first_json = (await client.get(f"/files/{media.id}/companion-details?link_offset=0&source_offset=0")).json()
    link_json = (await client.get(f"/files/{media.id}/companion-details?link_offset=20&source_offset=0")).json()
    assert [source["source_object_id"] for source in first_json["sources"]] == [source["source_object_id"] for source in link_json["sources"]]
    assert [link.find("p").get_text() for link in link_page.select("[data-companion-link]")] == [
        link.find("p").get_text() for link in source_page.select("[data-companion-link]")
    ]
    assert "source_offset=0" in link_page.find("button", string="Next companions")["hx-get"]
    assert "link_offset=20" in source_page.find("button", string="Next sources")["hx-get"]


@pytest.mark.parametrize("multi", [False, True])
async def test_cue_review_form_requires_explicit_file_mapping(client, session, multi):
    media, _companion, raw = await inventory(session, CUE if multi else CUE.split('FILE "two.mp3"')[0], "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    response = await client.get(f"/files/{media.id}/companion-observations/{imported.tracklist_observation_id}", headers=HX)
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.select_one("[data-source-decision]")
    assert form.select_one('input[name="cue_file_ordinal"]') is not None
    assert "Multiple FILE parts are not concatenated" in response.text
    values = {field["name"]: field.get("value", "") for field in form.select('input[type="hidden"]')}
    values["action"] = "select"
    refused = await client.post(form["hx-post"], data=values, headers=HX)
    assert 'role="alert"' in refused.text
    values["cue_file_ordinal"] = "2" if multi else "1"
    selected = await client.post(form["hx-post"], data=values, headers=HX)
    assert "Source selected" in selected.text
    actual = (await client.get(f"/api/local-sources/recordings/{media.id}/selected")).json()
    assert [row["title"] for row in actual["tracks"]] == ["Second" if multi else "First"]


async def test_partial_parser_detail_reads_complete_original_parent_text(client, session):
    source_text = "01. Artist - First\n02. Artist - Retained tail\n"
    media, _companion, raw = await inventory(session, source_text)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw, budget=LoadBudget(max_lines=1)))
    assert imported.tracklist_status == "incomplete"
    detail = await client.get(f"/files/{media.id}/companion-observations/{imported.tracklist_observation_id}", headers=HX)
    parsed = BeautifulSoup(detail.text, "html.parser")
    assert parsed.find("button", string="Use this tracklist") is None
    assert "Retained tail" in detail.text


@pytest.mark.parametrize("source_text", ["", "Unrecognized notes\n"])
async def test_empty_and_unrecognized_text_remain_visible_without_selection(client, session, source_text):
    media, _companion, raw = await inventory(session, source_text)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    for identifier in (raw, imported.tracklist_observation_id, imported.release_observation_id):
        detail = await client.get(f"/files/{media.id}/companion-observations/{identifier}", headers=HX)
        assert "data-companion-text" in detail.text or "Stored text is empty" in detail.text
        assert "Use this" not in detail.text
    original = await client.get(f"/files/{media.id}/companion-observations/{raw}/text", headers=HX)
    assert ("Stored text is empty" if not source_text else "Unrecognized notes") in original.text


async def test_refresh_uses_real_receipt_ordinal_and_db_clock_replayed_observation(client, session):
    media, companion, _raw = await inventory(session)
    # Agent clock is deliberately ahead. It must never make an old receipt seem new.
    old = report(companion, status="unavailable", at=datetime.now(UTC) + timedelta(hours=1))
    first = await store_capture_report(session, companion.agent_id, old)
    factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    router = SimpleNamespace(queue_for=lambda *_: queue)
    client._transport.app.state.task_router = router
    with patch("phaze.routers.record.async_session", factory):
        refreshed = await client.post(f"/files/{media.id}/companions/{companion.id}/refresh", headers=HX)
    assert "Read requested" in refreshed.text
    url = BeautifulSoup(refreshed.text, "html.parser").select_one("[data-capture-status]")["hx-get"]
    params = parse_qs(urlsplit(url).query)
    assert int(params["after_ordinal"][0]) == 1
    waiting = await client.get(url, headers=HX)
    assert "Waiting for the storage agent" in waiting.text and "A new read report" not in waiting.text
    await store_capture_report(session, companion.agent_id, report(companion))
    success = await client.get(url, headers=HX)
    assert "found" in success.text and "may belong to another read" in success.text
    again = report(companion, status="unavailable", at=old.read.retrieved_at)
    last = await store_capture_report(session, companion.agent_id, again)
    assert last.observation_id == first.observation_id
    failure = await client.get(url, headers=HX)
    assert "unavailable" in failure.text and "Read report details" in failure.text
    attempt = await session.get(ProviderAcquisitionAttempt, again.attempt_id)
    params.update(since=[attempt.received_at.isoformat()], after_ordinal=[str(attempt.ordinal)])
    after = urlsplit(url).path + "?" + urlencode(params, doseq=True)
    # HTTP retry of the same physical read adds no receipt/ordinal and cannot satisfy a new baseline.
    await store_capture_report(session, companion.agent_id, again)
    assert "Waiting for the storage agent" in (await client.get(after, headers=HX)).text
    # All overview entry points use actual receipt history, even when semantic UUID is reused.
    for route in (f"/files/{media.id}", f"/record/{media.id}", f"/files/{media.id}/companion-details"):
        rendered = BeautifulSoup((await client.get(route, headers=HX)).text, "html.parser")
        assert "unavailable" in rendered.select_one("[data-latest-attempt]").get_text()
    params["since"] = [(attempt.received_at - timedelta(minutes=3)).isoformat()]
    expired = urlsplit(url).path + "?" + urlencode(params, doseq=True)
    assert "storage agent is online" in (await client.get(expired, headers=HX)).text
    params["since"] = [datetime.now(UTC).replace(tzinfo=None).isoformat()]
    assert (await client.get(urlsplit(url).path, params=params, headers=HX)).status_code == 422
    session.add(Agent(id="other-ui-owner", name="other-ui-owner", kind="fileserver", scan_roots=[]))
    await session.flush()
    companion.agent_id = "other-ui-owner"
    await session.flush()
    revoked = await client.get(url, headers=HX)
    assert 'role="alert"' in revoked.text and "offline" not in revoked.text


async def test_a_receipt_newer_by_ordinal_is_found_when_the_db_clock_stepped_back(client, session):
    """phaze-esjwp: received order is the attempt ordinal, not ``received_at`` against ``since``.

    ``clock_timestamp()`` can step backwards between the refresh baseline and the receipt (measured:
    105.5 ms on the test database under load). The step is replayed exactly here by moving the
    baseline's ``since`` past the receipt's own ``received_at``; no sleep and no wall clock decide it.
    """
    media, companion, _raw = await inventory(session)
    await store_capture_report(session, companion.agent_id, report(companion, status="unavailable"))
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    client._transport.app.state.task_router = SimpleNamespace(queue_for=lambda *_: queue)
    factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    with patch("phaze.routers.record.async_session", factory):
        refreshed = await client.post(f"/files/{media.id}/companions/{companion.id}/refresh", headers=HX)
    url = BeautifulSoup(refreshed.text, "html.parser").select_one("[data-capture-status]")["hx-get"]
    params = parse_qs(urlsplit(url).query)
    found = report(companion)
    await store_capture_report(session, companion.agent_id, found)
    receipt = await session.get(ProviderAcquisitionAttempt, found.attempt_id)
    assert receipt.ordinal == int(params["after_ordinal"][0]) + 1
    params["since"] = [(receipt.received_at + timedelta(milliseconds=105)).isoformat()]
    stepped_back = await client.get(urlsplit(url).path, params=params, headers=HX)
    assert "A new read report arrived" in stepped_back.text and "found" in stepped_back.text
    assert "Waiting for the storage agent" not in stepped_back.text


async def test_refresh_enqueue_failure_keeps_retained_text(client, session):
    media, companion, raw = await inventory(session)
    factory = async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock(side_effect=OSError("synthetic broker unavailable")))
    client._transport.app.state.task_router = SimpleNamespace(queue_for=lambda *_: queue)
    with patch("phaze.routers.record.async_session", factory):
        refused = await client.post(f"/files/{media.id}/companions/{companion.id}/refresh", headers=HX)
    assert 'role="alert"' in refused.text and "could not be queued" in refused.text
    assert "First" in (await client.get(f"/files/{media.id}/companion-observations/{raw}/text", headers=HX)).text


async def test_new_raw_review_hides_old_candidates_and_keeps_current_selection(client, session):
    """A reread opens its own text; the reviewed observation stays pinned above it."""
    from phaze.services.local_source_import import decide_local_source

    media, companion, raw = await inventory(session)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id), actor="reviewer")
    source_id = (await session.get(ProviderSourceObservation, raw)).object_id
    unchanged = await client.get(f"/files/{media.id}/companion-details", headers=HX)
    assert "data-unselected-source-version" not in unchanged.text
    companion.sha256_hash = "b" * 64
    await session.flush()
    envelope = report(companion)
    envelope = envelope.model_copy(update={"read": envelope.read.model_copy(update={"text": "01. New Artist - New version\n"})})
    new_capture = await store_capture_report(session, companion.agent_id, envelope)
    overview = await client.get(f"/files/{media.id}/companion-details?review_source_id={source_id}", headers=HX)
    soup = BeautifulSoup(overview.text, "html.parser")
    current = soup.select_one('[data-current-source="tracklist"]')
    assert "First" in current.get_text()
    card = soup.select_one(f'[data-source-object="{source_id}"]')
    assert "New version — not selected" in card.get_text()
    assert "Previous tracklist is still in use" in card.get_text()
    evidence = current.select_one('[hx-get*="/api/local-sources/recordings/"]')
    assert evidence is not None and evidence.find_parent("details") is not None
    assert evidence["hx-get"].endswith("?kind=tracklist")
    assert "Selected tracklist" in (await client.get(evidence["hx-get"], headers=HX)).text
    assert not soup.select_one('[data-current-source="release_metadata"]').select("[data-selection-evidence]")
    review = soup.select_one("[data-source-review]")
    assert "New version" in review.select_one("[data-companion-text]").get_text()
    assert not review.select("[data-source-decision]")
    assert "Extract tracklist and release info" in review.get_text()
    assert str(new_capture.observation_id) in str(review)
    pinned = await client.get(f"/files/{media.id}/companion-observations/{imported.tracklist_observation_id}", headers=HX)
    assert "First" in pinned.text and "New version" not in pinned.text
    assert "Use this tracklist" not in pinned.text


async def test_new_version_notice_names_only_the_selection_still_using_previous_text(client: AsyncClient, session: AsyncSession) -> None:
    """Independent tracklist/release choices must not label a partly selected version unselected everywhere."""
    media, companion, raw = await inventory(session, "Artist: Original\nAlbum: Old release\n01. Artist - First\n", "nfo")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    old_track = await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id), actor="reviewer")
    await decide_local_source(session, decision(media, companion, imported.release_observation_id, kind="release_metadata"), actor="reviewer")
    companion.sha256_hash = "b" * 64
    await session.flush()
    envelope = report(companion)
    envelope = envelope.model_copy(
        update={"read": envelope.read.model_copy(update={"text": "Artist: New\nAlbum: New release\n01. Artist - Latest\n"})}
    )
    captured = await store_capture_report(session, companion.agent_id, envelope)
    latest = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=captured.observation_id))
    command = decision(media, companion, latest.tracklist_observation_id, token=old_track.selection_token)
    await decide_local_source(session, command.model_copy(update={"accept_conflict": True}), actor="reviewer")
    response = await client.get(f"/files/{media.id}/companion-details", headers=HX)
    soup = BeautifulSoup(response.text, "html.parser")
    notice = soup.select_one("[data-unselected-source-version]")
    assert "not selected for release info" in notice.get_text()
    assert "tracklist" not in notice.get_text()
    assert "Latest" in soup.select_one('[data-current-source="tracklist"]').get_text()
    assert "Old release" in soup.select_one('[data-current-source="release_metadata"]').get_text()


async def test_source_review_includes_supported_parser_generations_for_same_capture(client, session):
    media, companion, raw = await inventory(session, "Artist: Example Artist\n01. Artist - Opening\n")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    expected = {imported.tracklist_observation_id, imported.release_observation_id}
    for identifier in tuple(expected):
        current = await session.get(ProviderSourceObservation, identifier)
        assert current.parser_version in {PARSER_VERSION, RELEASE_PARSER_VERSION}
        legacy_parser = "local-release-v1" if current.parser_version == RELEASE_PARSER_VERSION else "local-tracklist-v1"
        historical = Snapshot.model_validate({**current.payload, "retrieved_at": current.retrieved_at, "parser_version": legacy_parser})
        stored = await store_snapshot(session, historical, source_file_id=companion.id, channel="companion", parent_id=raw)
        kind = "release_metadata" if current.parser_version.startswith("local-release") else "tracklist"
        await add_candidate(session, media.id, stored.observation.id, evidence=(f"kind:{kind}",))
        expected.add(stored.observation.id)
    source_id = (await session.get(ProviderSourceObservation, raw)).object_id
    review = await source_review_context(session, media.id, source_id)
    assert review["raw_id"] == raw
    assert {item["detail"].summary.observation_id for item in review["interpretations"]} == expected
    response = await client.get(f"/files/{media.id}/companion-details?review_source_id={source_id}", headers=HX)
    assert response.status_code == 200
    panel = BeautifulSoup(response.text, "html.parser").select_one("[data-source-review]")
    assert len(panel.select("[data-source-decision]")) == 4
    assert "Example Artist" in panel.get_text() and "Opening" in panel.get_text()


async def test_cue_invalid_timing_warning_survives_page_drawer_and_native_source_review(client, session):
    text = 'FILE "recording.mp3" MP3\nTRACK 01 AUDIO\nTITLE "Opening"\nINDEX 01 00:01:01\nTRACK 02 AUDIO\nTITLE "Untimed"\nINDEX 01 01:02:77\n'
    media, companion, raw = await inventory(session, text, "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    source_id = (await session.get(ProviderSourceObservation, raw)).object_id
    await session.flush()
    for path in (f"/files/{media.id}", f"/record/{media.id}"):
        response = await client.get(path)
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        visible = soup.select_one("[data-visible-companion-tracklist]")
        assert "Untimed" in visible.get_text() and "invalid source timing 01:02:77" in visible.get_text()
    response = await client.get(f"/files/{media.id}/companion-details?review_source_id={source_id}", headers=HX)
    panel = BeautifulSoup(response.text, "html.parser").select_one("[data-source-review]")
    warning = panel.select_one("[data-invalid-source-timing]")
    assert "01:02:77" in warning.get_text() and "without a usable offset" in warning.get_text()
    forms = panel.select("[data-source-decision]")
    track_form = next(form for form in forms if form.select_one('[name="kind"]')["value"] == "tracklist")
    assert track_form.select_one('[name="observation_id"]')["value"] == str(imported.tracklist_observation_id)
    assert track_form.select_one('[name="cue_file_ordinal"]') is not None
    assert "Use this tracklist" in track_form.get_text()
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=1), actor="reviewer")
    await session.flush()
    response = await client.get(f"/files/{media.id}/companion-details?review_source_id={source_id}", headers=HX)
    current = BeautifulSoup(response.text, "html.parser").select_one('[data-current-source="tracklist"]')
    assert "01:02:77" in current.select_one("[data-current-invalid-timing]").get_text()


async def test_review_text_is_bounded_and_preview_is_read_only(client, session):
    text = "01. Artist - Opening\n" + "synthetic prose\n" * 1000
    media, _companion, raw = await inventory(session, text)
    source_id = (await session.get(ProviderSourceObservation, raw)).object_id
    with patch("phaze.routers.record.enqueue_companion_capture", side_effect=AssertionError("preview must not acquire")):
        response = await client.get(f"/files/{media.id}/companion-sources/{source_id}/observations", headers=HX)
    soup = BeautifulSoup(response.text, "html.parser")
    assert len(soup.select_one("[data-companion-text]").get_text()) == 8192
    assert soup.find("button", string="More stored text")
    assert soup.find("button", string="Extract tracklist and release info")
    assert not soup.select("[data-source-decision]")


async def test_auto_refresh_keeps_open_review_outside_first_page(client, session):
    media, _companion, raw = await inventory(session)
    source_id = (await session.get(ProviderSourceObservation, raw)).object_id
    response = await client.get(
        f"/files/{media.id}/companion-details?link_offset=100000&source_offset=100000&review_source_id={source_id}", headers=HX
    )
    soup = BeautifulSoup(response.text, "html.parser")
    assert not soup.select("[data-source-object]")
    assert "First" in soup.select_one("[data-source-review] [data-companion-text]").get_text()
    # Link metadata still resolves retained content when the independent source page is elsewhere.
    paged = await client.get(f"/files/{media.id}/companion-details?source_offset=100000", headers=HX)
    assert "Review content" in paged.text and "Not read yet" not in paged.text


async def test_incomplete_parser_child_with_text_keeps_review_as_next_action(client, session):
    """Parser completeness must never masquerade as a failed acquisition."""
    media, _companion, raw = await inventory(session, "Artist: Example Artist\n01. Artist - Opening\n01. Other - Closing\n", "nfo")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    assert imported.tracklist_status == "incomplete"
    child = await session.get(ProviderSourceObservation, imported.tracklist_observation_id)
    child.retrieved_at += timedelta(seconds=1)
    await session.flush()
    response = await client.get(f"/files/{media.id}/companion-details", headers=HX)
    card = BeautifulSoup(response.text, "html.parser").select_one("[data-companion-link]")
    assert "Text available" in card.get_text() and "No readable text" not in card.get_text()
    review = card.find("button", string="Review content")
    assert "bg-action-brand" in review["class"]
    assert card.find("button", string="Read file again").find_parent("details") is not None


async def test_diagnostic_history_pagination_preserves_history_view(client, session):
    media, _companion, raw = await inventory(session)
    await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    source_id = (await session.get(ProviderSourceObservation, raw)).object_id
    first = await client.get(f"/files/{media.id}/companion-sources/{source_id}/observations?history_only=true&limit=1", headers=HX)
    next_url = BeautifulSoup(first.text, "html.parser").find("button", string="Next observations")["hx-get"]
    assert "history_only=true" in next_url
    next_page = await client.get(next_url, headers=HX)
    assert "data-observation-history" in next_page.text and "data-source-review" not in next_page.text
