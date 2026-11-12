"""phaze-2dxhd: bulk review of unambiguous CUE link candidates.

Every test drives the real capture -> import path (``tests/_selected_cue.py``) so candidates,
observations and companion links are the rows the product writes, and every decision goes through
the HTTP route the page posts to. "Approved" is asserted on the authority a consumer reads
(``get_selected_recording_source`` and its projected offsets) and on the audit row, never on the
route's own message.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup
import pytest
from sqlalchemy import select

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderRecordingSelection, ProviderSelectionEvent
from phaze.services.cue_link_review import load_cue_link_review
from phaze.services.local_source_import import get_selected_recording_source
from tests._selected_cue import cue_sheet, import_cue, media_row, select_cue


if TYPE_CHECKING:
    import uuid


pytestmark = pytest.mark.asyncio

ONE_FILE = cue_sheet(("Artist A", "Opener", "00:00:00"), ("Artist B", "Second", "05:00:00"), ("Artist C", "Closer", "10:30:00"))


def _two_files(first: str, second: str) -> str:
    return (
        'PERFORMER "Synthetic Set"\nTITLE "Synthetic Live Set"\n'
        f'FILE "{first}" WAVE\n TRACK 01 AUDIO\n  TITLE "Part one opener"\n  INDEX 01 00:00:00\n'
        ' TRACK 02 AUDIO\n  TITLE "Part one closer"\n  INDEX 01 04:00:00\n'
        f'FILE "{second}" WAVE\n TRACK 03 AUDIO\n  TITLE "Part two opener"\n  INDEX 01 00:00:00\n'
    )


async def _page(client) -> BeautifulSoup:
    response = await client.get("/pipeline/cue-link-review")
    assert response.status_code == 200
    return BeautifulSoup(response.text, "html.parser")


def _token(soup: BeautifulSoup, candidate_id: uuid.UUID) -> str:
    row = soup.select_one(f'[data-cue-link-row="{candidate_id}"] [data-cue-link-select]')
    assert row is not None, f"candidate {candidate_id} is not offered for bulk review"
    return row["value"]


async def _candidate(session, media) -> ProviderRecordingCandidate:
    rows = (
        await session.scalars(
            select(ProviderRecordingCandidate).where(ProviderRecordingCandidate.media_id == media.id).execution_options(populate_existing=True)
        )
    ).all()
    (tracklist,) = [row for row in rows if "kind:tracklist" in row.evidence]
    return tracklist


async def _post(client, action: str, *tokens: str):
    return await client.post(
        "/pipeline/cue-link-review/decide", data={"action": action, "review_tokens": list(tokens)}, headers={"Origin": "http://test"}
    )


async def _events(session, media) -> list[ProviderSelectionEvent]:
    return list((await session.scalars(select(ProviderSelectionEvent).where(ProviderSelectionEvent.media_id == media.id))).all())


async def test_an_unambiguous_candidate_is_proposed_grouped_by_link_method_and_nothing_is_selected(client, session):
    referenced = await media_row(session, filename="recording.mp3")
    await import_cue(session, referenced, ONE_FILE, method="reference")
    by_folder = await media_row(session, filename="other.mp3")
    await import_cue(session, by_folder, ONE_FILE.replace("Opener", "Folder opener"), method="folder")
    await session.commit()

    soup = await _page(client)

    groups = [section["data-cue-link-group"] for section in soup.select("[data-cue-link-group]")]
    assert groups == ["reference", "folder"], "strongest link evidence first"
    assert (
        soup.select_one(f'[data-cue-link-row="{(await _candidate(session, referenced)).id}"] [data-cue-link-ordinal]')["data-cue-link-ordinal"] == "1"
    )
    assert soup.select_one("[data-cue-link-proposed-total]").get_text() == "2"
    # Showing the page decides nothing: no selection, no audit row, the candidates stay pending.
    assert (await session.scalars(select(ProviderRecordingSelection))).all() == []
    assert (await _candidate(session, referenced)).status == "pending"
    assert all(not box.has_attr("checked") for box in soup.select("[data-cue-link-select]")), "nothing is pre-checked"


async def test_approving_checked_rows_selects_each_through_the_audited_single_review_path(client, session):
    approved = await media_row(session, filename="approved.mp3")
    await import_cue(session, approved, ONE_FILE, method="reference")
    untouched = await media_row(session, filename="untouched.mp3")
    await import_cue(session, untouched, ONE_FILE.replace("Opener", "Other opener"), method="reference")
    await session.commit()
    token = _token(await _page(client), (await _candidate(session, approved)).id)

    response = await _post(client, "select", token)

    assert response.status_code == 200
    assert "Approved 1 CUE link" in response.text
    selected = await get_selected_recording_source(session, approved.id)
    assert selected is not None and selected.availability == "current" and selected.actor == "admin"
    assert selected.target_mapping["cue_file_ordinal"] == 1
    offsets = [
        track.timestamp.offset.as_fraction() for track in selected.tracks if track.timestamp and track.timestamp.offset_usability == "qualified"
    ]
    assert offsets == [0, 300, 630], "the approved CUE's offsets are now qualified for this recording"
    (event,) = await _events(session, approved)
    assert (event.action, event.actor, event.target_mapping["cue_file_ordinal"]) == ("select", "admin", 1)
    assert event.decision_id is not None
    assert (await _candidate(session, approved)).status == "accepted"
    # The row the operator did not check is untouched.
    assert (await _candidate(session, untouched)).status == "pending"
    assert await _events(session, untouched) == []


async def test_rejecting_checked_rows_skips_them_with_an_audit_row_and_no_selection(client, session):
    media = await media_row(session)
    await import_cue(session, media, ONE_FILE, method="stem")
    await session.commit()
    token = _token(await _page(client), (await _candidate(session, media)).id)

    response = await _post(client, "reject", token)

    assert response.status_code == 200
    assert "Rejected 1 CUE link" in response.text
    assert await get_selected_recording_source(session, media.id) is None
    assert (await _candidate(session, media)).status == "rejected"
    (event,) = await _events(session, media)
    assert (event.action, event.actor, event.target_mapping) == ("reject", "admin", None)
    assert event.decision_payload["cue_file_ordinal"] is None, "a rejection records no FILE mapping"
    assert (await load_cue_link_review(session)).proposed == [], "a rejected candidate leaves the review"


async def test_a_row_that_changed_after_it_was_shown_refuses_the_whole_batch(client, session):
    still_good = await media_row(session, filename="good.mp3")
    await import_cue(session, still_good, ONE_FILE, method="reference")
    changed = await media_row(session, filename="changed.mp3")
    await import_cue(session, changed, ONE_FILE.replace("Opener", "Changed opener"), method="reference")
    await session.commit()
    soup = await _page(client)
    tokens = [_token(soup, (await _candidate(session, still_good)).id), _token(soup, (await _candidate(session, changed)).id)]
    changed.sha256_hash = "f" * 64  # the recording's content changed after the page was rendered
    await session.commit()

    response = await _post(client, "select", *tokens)

    assert response.status_code == 409
    assert "nothing was applied" in response.text
    assert (await session.scalars(select(ProviderRecordingSelection))).all() == []
    assert await _events(session, still_good) == [] and await _events(session, changed) == []


async def test_a_token_for_a_candidate_that_was_never_proposed_applies_nothing(client, session):
    media = await media_row(session)
    await import_cue(session, media, ONE_FILE, method="reference")
    await import_cue(session, media, ONE_FILE.replace("Opener", "Rival opener"), method="folder", name="rival.cue")
    await session.commit()
    candidate = (await load_cue_link_review(session)).excluded[0]
    forged = f"{candidate.candidate_id}.{'0' * 32}"

    response = await _post(client, "select", forged)

    assert response.status_code == 409
    assert (await session.scalars(select(ProviderRecordingSelection))).all() == []


async def test_more_than_one_current_cue_for_a_recording_is_excluded_as_ambiguous_with_its_reason(client, session):
    media = await media_row(session)
    await import_cue(session, media, ONE_FILE, method="reference")
    await import_cue(session, media, ONE_FILE.replace("Opener", "Rival opener"), method="reference", name="rival.cue")
    await session.commit()

    soup = await _page(client)

    excluded = soup.select("[data-cue-link-excluded]")
    assert {row["data-reason"] for row in excluded} == {"multiple_cues"}
    assert len(excluded) == 2, "both CUEs are listed; choosing between them is not a bulk decision"
    assert all("Ambiguous:" in row.get_text() and "2 current CUE files" in row.get_text() for row in excluded)
    assert soup.select("[data-cue-link-row]") == []
    assert soup.select_one('[data-cue-link-excluded-count="multiple_cues"]') is not None


async def test_a_multi_file_cue_maps_only_the_file_that_names_the_recording(session):
    by_name = await media_row(session, filename="Part B.mp3")
    await import_cue(session, by_name, _two_files("part a.mp3", "part b.MP3"), method="reference")
    by_stem = await media_row(session, filename="Part B.flac")
    await import_cue(session, by_stem, _two_files("Part A.wav", "Part B.wav"), method="reference")
    undetermined = await media_row(session, filename="Whole set.mp3")
    await import_cue(session, undetermined, _two_files("Part A.wav", "Part B.wav"), method="reference")
    await session.commit()

    review = await load_cue_link_review(session)

    proposed = {row.media_id: row for row in review.proposed}
    assert (proposed[by_name.id].cue_file_ordinal, proposed[by_name.id].ordinal_basis) == (2, "file_name")
    assert (proposed[by_stem.id].cue_file_ordinal, proposed[by_stem.id].ordinal_basis) == (2, "file_stem")
    assert proposed[by_name.id].track_count == 1, "only the mapped FILE's tracks are counted, never the whole CUE"
    (ambiguous,) = review.excluded
    assert (ambiguous.media_id, ambiguous.reason, ambiguous.detail) == (undetermined.id, "file_ordinal", "2 FILE entries")


async def test_a_file_name_shared_by_two_entries_is_not_guessed(session):
    media = await media_row(session, filename="set.mp3")
    await import_cue(session, media, _two_files("set.mp3", "SET.mp3"), method="reference")
    await session.commit()

    (row,) = (await load_cue_link_review(session)).excluded
    assert row.reason == "file_ordinal"


async def test_a_recording_with_a_selected_tracklist_is_excluded_rather_than_replaced(session):
    media = await media_row(session)
    await select_cue(session, media, ONE_FILE)
    first = (await session.scalars(select(ProviderRecordingSelection))).one()
    assert first.media_id == media.id
    # The selected CUE's file later changed on disk, so it no longer competes as a current CUE.
    link = (await session.scalars(select(FileCompanion).where(FileCompanion.media_id == media.id))).one()
    (await session.get(FileRecord, link.companion_id)).sha256_hash = "e" * 64
    await import_cue(session, media, ONE_FILE.replace("Opener", "Second CUE"), method="reference", name="second.cue")
    await session.commit()

    (row,) = (await load_cue_link_review(session)).excluded
    assert row.reason == "already_selected"


async def test_a_cue_whose_file_is_missing_is_excluded_as_not_current(session):
    media = await media_row(session)
    companion, _ = await import_cue(session, media, ONE_FILE, method="reference")
    companion.missing_at = datetime.now(UTC)
    await session.commit()

    (row,) = (await load_cue_link_review(session)).excluded
    assert (row.reason, row.detail) == ("not_current", "missing")


async def test_a_malformed_token_is_refused_without_applying_anything(client, session):
    media = await media_row(session)
    await import_cue(session, media, ONE_FILE, method="reference")
    await session.commit()

    response = await _post(client, "select", "not-a-token")

    assert response.status_code == 422
    assert (await session.scalars(select(ProviderRecordingSelection))).all() == []


async def test_deciding_with_nothing_checked_applies_nothing(client, session):
    response = await _post(client, "select")

    assert response.status_code == 200
    assert "Select at least one candidate first." in response.text


async def test_a_cross_site_post_is_refused(client, session):
    media = await media_row(session)
    await import_cue(session, media, ONE_FILE, method="reference")
    await session.commit()
    token = _token(await _page(client), (await _candidate(session, media)).id)

    response = await client.post(
        "/pipeline/cue-link-review/decide", data={"action": "select", "review_tokens": [token]}, headers={"Origin": "http://evil.example"}
    )

    assert response.status_code == 403
    assert await get_selected_recording_source(session, media.id) is None


async def test_the_cue_workspace_loads_the_bulk_review(client):
    response = await client.get("/s/cue", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert re.search(r'id="cue-link-review" hx-get="/pipeline/cue-link-review"', response.text)
