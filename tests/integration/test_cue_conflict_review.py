"""phaze-drote: resolve recordings with more than one CUE file, and CUEs with an ambiguous FILE entry.

Every test drives the real capture -> import path (``tests/_selected_cue.py``) and every decision goes
through the HTTP route the card posts to. "Resolved" is asserted on the authority a consumer reads
(``get_selected_recording_source``), on the candidates' statuses and on the audit rows -- never on the
route's own message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
from pydantic import ValidationError
import pytest
from sqlalchemy import select, text

from phaze.models.provider_source import ProviderRecordingCandidate, ProviderSelectionEvent
from phaze.schemas.local_source_import import ImportLocalSource, SourceDecision
from phaze.services import cue_conflict_review
from phaze.services.cue_conflict_review import load_cue_conflicts, resolve_cue_conflict
from phaze.services.cue_link_review import load_cue_link_review
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source
from phaze.tracklist_providers import local_parser
from tests._selected_cue import cue_sheet, import_cue, media_row, select_cue
from tests.integration._cue_conflict_count import COUNT_SQL


if TYPE_CHECKING:
    from phaze.models.file import FileRecord
    from phaze.schemas.local_source_import import ImportResult


pytestmark = pytest.mark.asyncio

ONE_FILE = cue_sheet(("Artist A", "Opener", "00:00:00"), ("Artist B", "Second", "05:00:00"), ("Artist C", "Closer", "10:30:00"))
OTHER_CLOSER = ONE_FILE.replace("Closer", "Other closer")
ORIGIN = {"Origin": "http://test"}


def _two_files(first: str, second: str) -> str:
    return (
        'PERFORMER "Synthetic Set"\nTITLE "Synthetic Live Set"\n'
        f'FILE "{first}" WAVE\n TRACK 01 AUDIO\n  TITLE "Part one opener"\n  INDEX 01 00:00:00\n'
        ' TRACK 02 AUDIO\n  TITLE "Part one closer"\n  INDEX 01 04:00:00\n'
        f'FILE "{second}" WAVE\n TRACK 03 AUDIO\n  TITLE "Part two opener"\n  INDEX 01 00:00:00\n'
    )


async def _parsed_twice(session, monkeypatch, media: FileRecord, cue: str, **kwargs) -> tuple[FileRecord, ImportResult, ImportResult]:
    """Import ``cue`` under the oldest local parser, then re-import the same read as a parser upgrade does."""
    monkeypatch.setattr(local_parser, "PARSER_VERSION", "local-tracklist-v1")
    companion, older = await import_cue(session, media, cue, **kwargs)
    monkeypatch.undo()
    newer = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=older.raw_observation_id))
    assert newer.tracklist_observation_id not in {None, older.tracklist_observation_id}
    return companion, older, newer


async def _candidate(session, observation_id: uuid.UUID) -> ProviderRecordingCandidate:
    return (
        await session.scalars(
            select(ProviderRecordingCandidate)
            .where(ProviderRecordingCandidate.observation_id == observation_id)
            .execution_options(populate_existing=True)
        )
    ).one()


async def _events(session, media: FileRecord) -> list[ProviderSelectionEvent]:
    return list((await session.scalars(select(ProviderSelectionEvent).where(ProviderSelectionEvent.media_id == media.id))).all())


async def _queue(client) -> BeautifulSoup:
    response = await client.get("/pipeline/cue-conflicts")
    assert response.status_code == 200
    return BeautifulSoup(response.text, "html.parser")


def _card(soup: BeautifulSoup, media: FileRecord):
    card = soup.select_one(f'[data-cue-conflict="{media.id}"]')
    assert card is not None, f"{media.original_filename} has no conflict card"
    return card


def _winner(card, candidate_id: uuid.UUID) -> str:
    return card.select_one(f'[data-cue-conflict-candidate="{candidate_id}"] [data-cue-conflict-winner]')["value"]


async def _confirm(client, card, winner: str, **ordinals: str):
    data = {"media_id": card["data-cue-conflict"], "card_token": card.select_one('input[name="card_token"]')["value"], "winner": winner}
    data.update({f"ordinal.{candidate}": value for candidate, value in ordinals.items()})
    return await client.post("/pipeline/cue-conflicts/resolve", data=data, headers=ORIGIN)


def _offsets(selected) -> list:
    return [track.timestamp.offset.as_fraction() for track in selected.tracks if track.timestamp and track.timestamp.offset_usability == "qualified"]


async def test_identical_cue_files_preselect_the_strongest_link_and_one_confirm_selects_it_and_rejects_the_other(client, session):
    media = await media_row(session, filename="set.mp3")
    _, by_stem = await import_cue(session, media, ONE_FILE, method="stem", name="aaa.cue")
    _, by_reference = await import_cue(session, media, ONE_FILE, method="reference", name="zzz.cue")
    await session.commit()
    stem_candidate = await _candidate(session, by_stem.tracklist_observation_id)
    reference_candidate = await _candidate(session, by_reference.tracklist_observation_id)

    card = _card(await _queue(client), media)

    assert (card["data-reason"], card["data-identical"]) == ("multiple_cues", "true")
    (proposed,) = card.select("[data-cue-conflict-winner][checked]")
    assert proposed["value"] == _winner(card, reference_candidate.id), "the reference link beats the stem link, whatever the file names"
    assert await _events(session, media) == [], "showing the card decides nothing"

    response = await _confirm(client, card, proposed["value"])

    assert response.status_code == 200
    assert BeautifulSoup(response.text, "html.parser").select_one(f'[data-cue-conflict-resolved="{media.id}"]') is not None
    selected = await get_selected_recording_source(session, media.id)
    assert selected is not None and selected.observation_id == by_reference.tracklist_observation_id
    assert selected.target_mapping["cue_file_ordinal"] == 1
    assert _offsets(selected) == [0, 300, 630]
    assert (
        (await _candidate(session, reference_candidate.observation_id)).status,
        (await _candidate(session, stem_candidate.observation_id)).status,
    ) == (
        "accepted",
        "rejected",
    )
    events = {event.action: event for event in await _events(session, media)}
    assert set(events) == {"select", "reject"}, "one audited event per CUE file"
    assert events["reject"].observation_id == by_stem.tracklist_observation_id
    assert events["reject"].decision_payload["replaced_by_observation_id"] == str(by_reference.tracklist_observation_id)
    assert "replaced_by_observation_id" not in events["select"].decision_payload
    assert await load_cue_conflicts(session) == []


async def test_differing_cue_files_preselect_nothing_show_where_they_differ_and_the_operator_choice_wins(client, session):
    media = await media_row(session, filename="set.mp3")
    _, referenced = await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    _, by_stem = await import_cue(session, media, OTHER_CLOSER, method="stem", name="two.cue")
    await session.commit()
    stem_candidate = await _candidate(session, by_stem.tracklist_observation_id)

    card = _card(await _queue(client), media)

    assert card["data-identical"] == "false"
    assert card.select("[data-cue-conflict-winner][checked]") == [], "a judgement call is never preselected"
    assert "differ at track 3" in card.select_one("[data-cue-conflict-summary]").get_text()
    assert (
        "Track 3: Other closer" in card.select_one(f'[data-cue-conflict-candidate="{stem_candidate.id}"] [data-cue-conflict-differences]').get_text()
    )

    response = await _confirm(client, card, _winner(card, stem_candidate.id))

    assert response.status_code == 200
    selected = await get_selected_recording_source(session, media.id)
    assert selected is not None and selected.observation_id == by_stem.tracklist_observation_id, "the weaker link wins when the operator says so"
    assert (await _candidate(session, referenced.tracklist_observation_id)).status == "rejected"


async def test_an_undetermined_file_entry_is_chosen_from_a_dropdown_and_is_required(client, session):
    media = await media_row(session, filename="Whole set.mp3")
    _, imported = await import_cue(session, media, _two_files("Part A.wav", "Part B.wav"), method="reference")
    await session.commit()
    candidate = await _candidate(session, imported.tracklist_observation_id)

    card = _card(await _queue(client), media)

    assert card["data-reason"] == "file_ordinal"
    choice = card.select_one(f'select[name="ordinal.{candidate.id}"]')
    assert [option["value"] for option in choice.select("option")] == ["", "1", "2"]
    winner = _winner(card, candidate.id)

    refused = await _confirm(client, card, winner)

    assert refused.status_code == 422
    assert "Choose which FILE entry" in refused.text
    assert await get_selected_recording_source(session, media.id) is None
    assert await _events(session, media) == []

    response = await _confirm(client, card, winner, **{str(candidate.id): "2"})

    assert response.status_code == 200
    selected = await get_selected_recording_source(session, media.id)
    assert selected is not None and selected.target_mapping["cue_file_ordinal"] == 2
    assert [track.title for track in selected.tracks] == ["Part two opener"], "only FILE 2's tracks belong to this recording"
    (event,) = await _events(session, media)
    assert (event.action, event.target_mapping["cue_file_ordinal"]) == ("select", 2)


async def test_an_older_parse_of_the_same_cue_file_is_not_a_conflict_and_is_never_proposed_or_rejected(client, session, monkeypatch):
    media = await media_row(session, filename="set.mp3")
    _, older, newer = await _parsed_twice(session, monkeypatch, media, ONE_FILE, method="reference")
    await session.commit()

    review = await load_cue_link_review(session)

    (proposed,) = review.proposed
    assert proposed.observation_id == newer.tracklist_observation_id, "one CUE file is one CUE: its newest parse is proposed"
    (excluded,) = review.excluded
    assert (excluded.observation_id, excluded.reason, excluded.detail) == (older.tracklist_observation_id, "older_parse", "local-tracklist-v1")
    assert await load_cue_conflicts(session) == []

    # Rejecting the newer parse in bulk must not resurrect the older one as a proposal.
    page = BeautifulSoup((await client.get("/pipeline/cue-link-review")).text, "html.parser")
    token = page.select_one(f'[data-cue-link-row="{proposed.candidate_id}"] [data-cue-link-select]')["value"]
    rejected = await client.post("/pipeline/cue-link-review/decide", data={"action": "reject", "review_tokens": [token]}, headers=ORIGIN)
    assert rejected.status_code == 200

    after = await load_cue_link_review(session)
    assert after.proposed == []
    assert [(row.observation_id, row.reason) for row in after.excluded] == [(older.tracklist_observation_id, "older_parse")]
    assert (await _candidate(session, older.tracklist_observation_id)).status == "pending", "left for the supersession bead (phaze-og7sn)"


async def test_a_conflict_counts_cue_files_not_parses_and_leaves_older_parses_untouched(client, session, monkeypatch):
    media = await media_row(session, filename="set.mp3")
    _, older, newer = await _parsed_twice(session, monkeypatch, media, ONE_FILE, method="reference", name="one.cue")
    _, rival = await import_cue(session, media, OTHER_CLOSER, method="stem", name="two.cue")
    await session.commit()
    newer_candidate = await _candidate(session, newer.tracklist_observation_id)

    card = _card(await _queue(client), media)

    shown = {row["data-cue-conflict-candidate"] for row in card.select("[data-cue-conflict-candidate]")}
    assert shown == {str(newer_candidate.id), str((await _candidate(session, rival.tracklist_observation_id)).id)}, "two CUE files, two choices"

    response = await _confirm(client, card, _winner(card, newer_candidate.id))

    assert response.status_code == 200
    assert (await _candidate(session, rival.tracklist_observation_id)).status == "rejected"
    assert (await _candidate(session, older.tracklist_observation_id)).status == "pending", "an older parse is neither selected nor rejected here"
    assert {event.observation_id for event in await _events(session, media)} == {newer.tracklist_observation_id, rival.tracklist_observation_id}


async def test_a_card_whose_cue_files_changed_applies_nothing_and_is_redrawn(client, session):
    media = await media_row(session, filename="set.mp3")
    await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    await import_cue(session, media, ONE_FILE, method="stem", name="two.cue")
    await session.commit()
    card = _card(await _queue(client), media)
    proposed = card.select_one("[data-cue-conflict-winner][checked]")["value"]
    await import_cue(session, media, OTHER_CLOSER, method="folder", name="three.cue")
    await session.commit()

    response = await _confirm(client, card, proposed)

    assert response.status_code == 409
    assert await get_selected_recording_source(session, media.id) is None
    assert await _events(session, media) == []
    redrawn = BeautifulSoup(response.text, "html.parser")
    assert len(redrawn.select("[data-cue-conflict-candidate]")) == 3, "the card is redrawn as it is now"
    assert redrawn.select_one("[data-cue-conflict-message]") is not None


async def test_a_forged_winner_or_an_empty_choice_applies_nothing(client, session):
    media = await media_row(session, filename="set.mp3")
    await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    await import_cue(session, media, OTHER_CLOSER, method="stem", name="two.cue")
    await session.commit()
    card = _card(await _queue(client), media)
    real = card.select_one("[data-cue-conflict-winner]")["value"]

    forged = await _confirm(client, card, f"{real.partition('.')[0]}.{'0' * 32}")
    empty = await _confirm(client, card, "")

    assert (forged.status_code, empty.status_code) == (409, 422)
    assert "Check the CUE to use first" in empty.text
    assert await _events(session, media) == []


async def test_a_posted_file_entry_is_checked_against_the_cue(client, session):
    named = await media_row(session, filename="set.mp3")
    _, by_reference = await import_cue(session, named, ONE_FILE, method="reference", name="one.cue")
    await import_cue(session, named, ONE_FILE, method="stem", name="two.cue")
    whole = await media_row(session, filename="Whole set.mp3")
    _, undetermined = await import_cue(session, whole, _two_files("Part A.wav", "Part B.wav"), method="reference")
    await session.commit()
    reference_candidate = await _candidate(session, by_reference.tracklist_observation_id)
    undetermined_candidate = await _candidate(session, undetermined.tracklist_observation_id)
    soup = await _queue(client)
    named_card, whole_card = _card(soup, named), _card(soup, whole)

    overridden = await _confirm(client, named_card, _winner(named_card, reference_candidate.id), **{str(reference_candidate.id): "2"})
    not_a_number = await _confirm(client, whole_card, _winner(whole_card, undetermined_candidate.id), **{str(undetermined_candidate.id): "two"})

    assert (overridden.status_code, not_a_number.status_code) == (422, 422)
    assert "already names its FILE entry" in overridden.text, "a FILE entry the CUE determines cannot be overridden"
    assert "Choose which FILE entry" in not_a_number.text, "a non-numeric choice is no choice"
    assert await _events(session, named) == [] and await _events(session, whole) == []


async def test_a_conflicting_cue_can_be_confirmed_once_the_operator_acknowledges_the_conflict(client, session):
    """phaze-2al7q.3 finding 1: a CUE whose observation conflicts with retained source data is not a dead card.

    ``decide_local_source`` refuses to select a conflicting observation without ``accept_conflict``; the
    card used to never send it, so a recording whose chosen CUE conflicted could never be resolved. The
    card now marks the conflicting CUE and asks for the same explicit acknowledgement single review does.
    """
    media = await media_row(session, filename="set.mp3")
    _, by_reference = await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    _, by_stem = await import_cue(session, media, ONE_FILE, method="stem", name="two.cue")
    await session.execute(
        text("UPDATE provider_source_observations SET conflict_with_id = :other WHERE id = :id"),
        {"id": by_reference.tracklist_observation_id, "other": by_stem.tracklist_observation_id},
    )
    await session.commit()
    reference_candidate = await _candidate(session, by_reference.tracklist_observation_id)
    card = _card(await _queue(client), media)
    winner = _winner(card, reference_candidate.id)
    acknowledgement = card.select_one(f'[data-cue-conflict-candidate="{reference_candidate.id}"] [data-cue-conflict-accept]')
    assert acknowledgement is not None and acknowledgement["name"] == f"accept_conflict.{reference_candidate.id}"

    unacknowledged = await _confirm(client, card, winner)

    assert unacknowledged.status_code == 422
    assert "conflicts with other retained source data" in unacknowledged.text
    assert await _events(session, media) == [], "nothing is applied without the acknowledgement"

    card = _card(BeautifulSoup(unacknowledged.text, "html.parser"), media)
    data = {"media_id": str(media.id), "card_token": card.select_one('input[name="card_token"]')["value"], "winner": winner}
    acknowledged = await client.post("/pipeline/cue-conflicts/resolve", data={**data, acknowledgement["name"]: "true"}, headers=ORIGIN)

    assert acknowledged.status_code == 200
    selected = await get_selected_recording_source(session, media.id)
    assert selected is not None and selected.observation_id == by_reference.tracklist_observation_id
    events = {event.action: event for event in await _events(session, media)}
    assert events["select"].decision_payload["accept_conflict"] is True, "the acknowledgement is in the audit"
    assert events["reject"].observation_id == by_stem.tracklist_observation_id
    assert await load_cue_conflicts(session) == []


async def test_a_refused_rejection_rolls_back_the_selection_so_nothing_is_applied(session, monkeypatch):
    media = await media_row(session, filename="set.mp3")
    _, by_reference = await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    _, by_stem = await import_cue(session, media, ONE_FILE, method="stem", name="two.cue")
    await session.commit()
    (conflict,) = await load_cue_conflicts(session)
    proposed = next(candidate for candidate in conflict.candidates if candidate.candidate_id == conflict.proposed_id)
    real_decide = cue_conflict_review.decide_local_source

    async def refuse_rejections(session, command, *, actor):
        if command.action == "reject":
            raise ValueError("simulated refusal")
        return await real_decide(session, command, actor=actor)

    monkeypatch.setattr(cue_conflict_review, "decide_local_source", refuse_rejections)
    outcome = await resolve_cue_conflict(
        session, media_id=media.id, card_token=conflict.token, winner_token=proposed.token, cue_file_ordinal=None, actor="test"
    )

    assert (outcome.applied, outcome.error) == (False, "simulated refusal")
    assert await get_selected_recording_source(session, media.id) is None, "the winner's selection is rolled back with the refused rejection"
    assert await _events(session, media) == []
    assert {(await _candidate(session, imported.tracklist_observation_id)).status for imported in (by_reference, by_stem)} == {"pending"}

    monkeypatch.setattr(cue_conflict_review, "decide_local_source", real_decide)
    retried = await resolve_cue_conflict(
        session, media_id=media.id, card_token=conflict.token, winner_token=proposed.token, cue_file_ordinal=None, actor="test"
    )
    assert retried.applied, "the session is still usable and the same card applies once nothing refuses"


async def test_a_recording_that_already_has_a_selection_is_not_a_conflict(session):
    media = await media_row(session, filename="set.mp3")
    await select_cue(session, media, ONE_FILE)
    await import_cue(session, media, OTHER_CLOSER, method="reference", name="later.cue")
    await session.commit()

    assert await load_cue_conflicts(session) == []
    (row,) = (await load_cue_link_review(session)).excluded
    assert row.reason == "already_selected", "a decided recording is no longer reported as ambiguous"


async def test_a_replacement_is_only_recorded_on_a_rejection_naming_the_selected_source(session):
    media = await media_row(session, filename="set.mp3")
    companion, first = await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    _, second = await import_cue(session, media, OTHER_CLOSER, method="stem", name="two.cue")
    third_companion, third = await import_cue(session, media, ONE_FILE.replace("Opener", "Third opener"), method="folder", name="three.cue")
    await session.commit()
    base = {
        "media_id": media.id,
        "kind": "tracklist",
        "decision_id": uuid.uuid4(),
        "expected_revision": companion.sha256_hash,
        "expected_source_sha256": companion.sha256_hash,
        "expected_media_sha256": media.sha256_hash,
    }

    with pytest.raises(ValidationError, match="Only a rejection"):
        SourceDecision.model_validate(
            {**base, "observation_id": first.tracklist_observation_id, "replaced_by_observation_id": second.tracklist_observation_id}
        )
    with pytest.raises(ValidationError, match="never itself"):
        SourceDecision.model_validate(
            {
                **base,
                "observation_id": first.tracklist_observation_id,
                "action": "reject",
                "replaced_by_observation_id": first.tracklist_observation_id,
            }
        )
    with pytest.raises(ValueError, match="selected source"):
        await decide_local_source(
            session,
            SourceDecision.model_validate(
                {
                    **base,
                    "observation_id": first.tracklist_observation_id,
                    "action": "reject",
                    "replaced_by_observation_id": second.tracklist_observation_id,
                }
            ),
            actor="admin",
        )

    # With a selection in place, a rejection may only name THAT selection as its replacement.
    selection = uuid.uuid4()
    await decide_local_source(
        session,
        SourceDecision.model_validate({**base, "decision_id": selection, "observation_id": first.tracklist_observation_id, "cue_file_ordinal": 1}),
        actor="admin",
    )
    third_reject = {
        **base,
        "decision_id": uuid.uuid4(),
        "observation_id": third.tracklist_observation_id,
        "expected_revision": third_companion.sha256_hash,
        "expected_source_sha256": third_companion.sha256_hash,
        "action": "reject",
        "expected_selection_token": selection,
    }
    with pytest.raises(ValueError, match="selected source"):
        await decide_local_source(
            session, SourceDecision.model_validate({**third_reject, "replaced_by_observation_id": second.tracklist_observation_id}), actor="admin"
        )
    await decide_local_source(
        session, SourceDecision.model_validate({**third_reject, "replaced_by_observation_id": first.tracklist_observation_id}), actor="admin"
    )
    (event,) = [event for event in await _events(session, media) if event.action == "reject"]
    assert event.decision_payload["replaced_by_observation_id"] == str(first.tracklist_observation_id)


async def test_the_documented_count_query_agrees_with_the_queue(session, monkeypatch):
    identical = await media_row(session, filename="identical.mp3")
    await import_cue(session, identical, ONE_FILE, method="reference", name="one.cue")
    await import_cue(session, identical, ONE_FILE, method="stem", name="two.cue")
    differing = await media_row(session, filename="differing.mp3")
    await import_cue(session, differing, ONE_FILE, method="reference", name="one.cue")
    await import_cue(session, differing, OTHER_CLOSER, method="stem", name="two.cue")
    multi_file = await media_row(session, filename="Whole set.mp3")
    await import_cue(session, multi_file, _two_files("Part A.wav", "Part B.wav"), method="reference")
    parsed_twice = await media_row(session, filename="twice.mp3")
    await _parsed_twice(session, monkeypatch, parsed_twice, ONE_FILE, method="reference")
    single = await media_row(session, filename="single.mp3")
    await import_cue(session, single, ONE_FILE, method="reference")
    await session.commit()

    counts = dict((await session.execute(text(COUNT_SQL))).mappings().one())
    conflicts = await load_cue_conflicts(session)

    assert counts == {
        "media_with_a_current_cue": 5,
        "current_cue_files": 7,
        "current_cue_candidates": 8,
        "media_with_multiple_cue_files": 2,
        "of_which_identical_tracks": 1,
        "of_which_differing": 1,
        "media_with_a_multi_file_cue": 1,
        "superseded_parser_candidates": 1,
        "of_which_pending": 1,
    }
    multiple = [conflict for conflict in conflicts if conflict.reason == "multiple_cues"]
    assert len(multiple) == counts["media_with_multiple_cue_files"]
    assert sum(conflict.identical for conflict in multiple) == counts["of_which_identical_tracks"]
    assert [conflict.media_id for conflict in conflicts if conflict.reason == "file_ordinal"] == [multi_file.id]


async def test_a_cross_site_confirm_is_refused(client, session):
    media = await media_row(session, filename="set.mp3")
    await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    await import_cue(session, media, ONE_FILE, method="stem", name="two.cue")
    await session.commit()
    card = _card(await _queue(client), media)

    response = await client.post(
        "/pipeline/cue-conflicts/resolve",
        data={
            "media_id": str(media.id),
            "card_token": card.select_one('input[name="card_token"]')["value"],
            "winner": card.select_one("[data-cue-conflict-winner]")["value"],
        },
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert await _events(session, media) == []


async def test_the_cue_workspace_shows_the_conflicts_queue_and_the_bulk_page_points_to_it(client, session):
    media = await media_row(session, filename="set.mp3")
    await import_cue(session, media, ONE_FILE, method="reference", name="one.cue")
    await import_cue(session, media, OTHER_CLOSER, method="stem", name="two.cue")
    await session.commit()

    workspace = await client.get("/s/cue", headers={"HX-Request": "true"})
    bulk = BeautifulSoup((await client.get("/pipeline/cue-link-review")).text, "html.parser")
    queue = await _queue(client)

    assert 'hx-get="/pipeline/cue-conflicts"' in workspace.text
    assert bulk.select_one("[data-cue-link-conflicts-hint]") is not None
    assert {row["data-reason"] for row in bulk.select("[data-cue-link-excluded]")} == {"multiple_cues"}
    assert queue.select_one("[data-cue-conflicts-total]").get_text() == "1"
    assert queue.select_one('[data-cue-conflicts-count="multiple_cues"]') is not None


async def test_track_prints_are_read_for_the_conflict_set_only_never_for_the_whole_population(session):
    """phaze-2al7q.3 finding 4, PARTIAL mitigation: the per-track prints leave the population query.

    Both CUE pages still read and classify every candidate on every request (bounded work is phaze-m16w5),
    but the per-track fingerprint -- most of the query's cost at production scale -- is computed only for
    the candidates of recordings shown in the conflicts queue, and never by the bulk link review.
    """
    from sqlalchemy import event

    conflicted = await media_row(session, filename="set.mp3")
    _, one = await import_cue(session, conflicted, ONE_FILE, method="reference", name="one.cue")
    _, two = await import_cue(session, conflicted, OTHER_CLOSER, method="stem", name="two.cue")
    plain = await media_row(session, filename="plain.mp3")
    _, unambiguous = await import_cue(session, plain, ONE_FILE, method="reference", name="plain.cue")
    await session.commit()
    conflict_candidates = {(await _candidate(session, one.tracklist_observation_id)).id, (await _candidate(session, two.tracklist_observation_id)).id}
    unambiguous_candidate = (await _candidate(session, unambiguous.tracklist_observation_id)).id

    executed: list[tuple[str, object]] = []

    def _record(_conn, _cursor, statement, parameters, _context, _executemany) -> None:
        executed.append((statement, parameters))

    engine = session.sync_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        review = await load_cue_link_review(session)
        review_statements = list(executed)
        executed.clear()
        (conflict,) = await load_cue_conflicts(session)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert [row.candidate_id for row in review.proposed] == [unambiguous_candidate]
    assert not any("md5(" in statement for statement, _ in review_statements), "the bulk review computes no per-track print"
    print_queries = [(statement, parameters) for statement, parameters in executed if "md5(" in statement]
    assert len(print_queries) == 1
    bound = {value for value in (print_queries[0][1] or ()) if isinstance(value, uuid.UUID)}
    assert bound == conflict_candidates, "prints are read for the conflict set's candidates and nothing else"
    assert conflict.differing == (3,), "the comparison still sees the prints"
