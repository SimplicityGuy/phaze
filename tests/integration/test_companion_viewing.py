"""Real stored read/import/review and page/drawer HTML transport."""

import uuid

from bs4 import BeautifulSoup
import pytest

from phaze.main import create_app
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderSourceObject, ProviderSourceObservation
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.local_source_import import import_local_source
from tests.integration.test_local_source_import import CUE, decision, inventory


TEXT = 'EVENT: Example Festival\nDATE: 2024-06-02\nVENUE: Example Hall\nLABEL: Example Records\nQUALITY: 320 kbps\n01. Artist - Opening\n02. Other - Finale\n<script>alert("synthetic")</script>\n'
HX = {"HX-Request": "true"}


@pytest.mark.parametrize("kind", ["mp3", "mp4"])
async def test_music_video_both_interpretations_visible_page_drawer_and_lazy_text(client, session, kind):
    media, companion, raw = await inventory(session, TEXT, "nfo")
    media.file_type = kind
    result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await session.flush()
    rendered = []
    for path in (f"/files/{media.id}", f"/record/{media.id}"):
        response = await client.get(path)
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        assert soup.select_one("[data-visible-track]").get_text().startswith("1. Artist")
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
    text_url = BeautifulSoup(detail.text, "html.parser").find("button", string="Read original companion text")["hx-get"]
    assert text_url.endswith(f"/{raw}/text")
    assert "&lt;script&gt;" in (await client.get(text_url, headers=HX)).text
    text = await client.get(f"/files/{media.id}/companion-observations/{raw}/text", headers=HX)
    assert "&lt;script&gt;" in text.text and "<script>alert(" not in text.text and "whitespace-pre-wrap" in text.text
    assert f"/files/{companion.id}#companion-sources" in detail.text
    reverse = await client.get(f"/files/{companion.id}/linked-media", headers=HX)
    assert media.original_filename in reverse.text


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
    assert reimport.status_code == 200 and "source review remains separate" in reimport.text
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
    assert "Stored text imported" in response.text
    page = await client.get(f"/files/{media.id}")
    assert "Tag Title" in page.text and "Import stored Comments tag" in page.text
    unparsed = await client.get(f"/files/{media.id}/companion-observations/{raw}", headers=HX)
    assert "No parsed track rows" in unparsed.text and "Read original companion text" in unparsed.text


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
    assert [source["data-source-object"] for source in first.select("[data-source-object]")] == [
        source["data-source-object"] for source in link_page.select("[data-source-object]")
    ]
    assert [link.get_text() for link in link_page.select("[data-companion-link]")] == [
        link.get_text() for link in source_page.select("[data-companion-link]")
    ]
    assert "source_offset=0" in link_page.find("button", string="Next companions")["hx-get"]
    assert "link_offset=20" in source_page.find("button", string="Next sources")["hx-get"]


async def test_cue_review_form_requires_explicit_file_mapping(client, session):
    media, _companion, raw = await inventory(session, CUE, "cue")
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
    values["cue_file_ordinal"] = "2"
    selected = await client.post(form["hx-post"], data=values, headers=HX)
    assert "Source selected" in selected.text
    actual = (await client.get(f"/api/local-sources/recordings/{media.id}/selected")).json()
    assert [row["title"] for row in actual["tracks"]] == ["Second"]
