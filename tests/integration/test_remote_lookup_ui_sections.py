"""Selecting from a SECTIONED remote snapshot through the recording page's look-up panel.

A sectioned page (a heading per tracklist, as a wiki page has) is the real input class that selected zero tracks when
the decision carried no section mapping. The fake provider here returns such a snapshot through the real registry,
transport, persistence and templates; nothing opens a socket. ``RemoteSourceDecision`` carries
``text_section_ordinal`` / ``accept_text_warnings`` (the orchestration fix from the MixesDB bead).
"""

import json
from typing import Any
import uuid

import pytest

from phaze.services.local_source_import import get_selected_recording_source
from phaze.services.remote_providers import RemoteProviderBinding
from phaze.tracklist_providers.domain import (
    Completeness,
    Fact,
    LoadBudget,
    LoadOutcome,
    OutcomeStatus,
    ProviderCandidate,
    ProviderTrack,
    Snapshot,
    TrackSection,
)
from phaze.tracklist_providers.port import AuthorizedEffects
from tests.integration.test_remote_lookup import EXAMPLE_PROFILE, ExampleCatalog, ExampleUpstream, harness, seed
from tests.integration.test_remote_lookup_ui import ORIGIN, client_for, form_fields, lookup_and_load, page, soup


pytestmark = pytest.mark.integration


class SectionedCatalog(ExampleCatalog):
    """Loads a snapshot of several sections: tracks carry their section, and a section may carry warnings."""

    def __init__(self, sections: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]) -> None:
        super().__init__()
        self.sections = sections

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        read = await effects.read(effects.minted[0], budget)
        assert read.status == OutcomeStatus.FOUND
        data = json.loads(read.text or "{}")
        tracks: list[ProviderTrack] = []
        for ordinal, (_title, titles, _warnings) in enumerate(self.sections, start=1):
            for title in titles:
                tracks.append(ProviderTrack(position=len(tracks) + 1, section_ordinal=ordinal, artist="Example Artist", title=title))
        snapshot = Snapshot(
            identity=candidate.identity,
            revision=data["rev"],
            revision_scope="full",
            retrieved_at=read.retrieved_at,
            artist=Fact(value=data["artist"], certainty="known"),
            event=Fact(value=data["event"], certainty="known"),
            date=Fact(value=data["date"], certainty="known"),
            tracks=tuple(tracks),
            sections=tuple(
                TrackSection(ordinal=ordinal, title=title, completeness=Completeness(state="complete", reason="every row read"), warnings=warnings)
                for ordinal, (title, _titles, warnings) in enumerate(self.sections, start=1)
            ),
            section_scan_complete=True,
            source_format="example_set_json",
            parser_version="example-v1",
            completeness=Completeness(state="complete", reason="every section read"),
            rights_evidence=("rights:unknown",),
        )
        return LoadOutcome(status="found", code="loaded", scope=candidate.identity.native_id, snapshot=snapshot)


TWO = (("First hour", ("Alpha", "Bravo"), ()), ("Second hour", ("Charlie", "Delta", "Echo"), ()))
WARNED = (("First hour", ("Alpha", "Bravo"), ()), ("Second hour", ("Charlie", "Delta", "Echo"), ("row 3 had no separator",)))
ONE = (("Only hour", ("Alpha", "Bravo"), ()),)


def binding(sections: tuple[Any, ...]) -> tuple[RemoteProviderBinding, ...]:
    return (RemoteProviderBinding(SectionedCatalog(sections), EXAMPLE_PROFILE),)


async def loaded_form(h: Any, media_id: uuid.UUID) -> Any:
    async with client_for(h) as client:
        html = await lookup_and_load(client, media_id)
    return html


async def test_several_sections_are_shown_and_a_choice_is_required_before_select(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=binding(TWO)) as h:
        media = await seed(h.sessions)
        html = await loaded_form(h, media.id)
    version = html.select_one("[data-loaded-version]")
    sections = version.select("[data-text-section]")
    assert [item["data-text-section"] for item in sections] == ["1", "2"]
    assert "First hour" in sections[0].get_text() and "2 tracks" in sections[0].get_text() and "complete" in sections[0].get_text()
    assert "Second hour" in sections[1].get_text() and "3 tracks" in sections[1].get_text()
    radios = version.select("input[type=radio][name=text_section_ordinal]")
    assert [radio["value"] for radio in radios] == ["1", "2"] and all(radio.has_attr("required") for radio in radios)
    assert version.select_one("input[name=accept_text_warnings]") is None, "no warnings, no acknowledgement"
    assert version.select_one('button[value="select"]') is not None
    assert version.select_one('button[value="reject"]').has_attr("formnovalidate"), "rejecting needs no section choice"
    assert "[section 2]" in version.select_one('[data-track="3"]').get_text()


async def test_a_section_with_warnings_shows_them_and_offers_an_acknowledgement(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=binding(WARNED)) as h:
        media = await seed(h.sessions)
        html = await loaded_form(h, media.id)
    version = html.select_one("[data-loaded-version]")
    assert "row 3 had no separator" in version.select_one("[data-section-warnings]").get_text()
    assert version.select_one("input[type=checkbox][name=accept_text_warnings]") is not None


async def test_one_clean_section_needs_no_extra_step(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=binding(ONE)) as h:
        media = await seed(h.sessions)
        html = await loaded_form(h, media.id)
    version = html.select_one("[data-loaded-version]")
    assert version.select_one("input[name=text_section_ordinal]") is None and version.select_one("input[name=accept_text_warnings]") is None
    assert version.select_one('button[value="select"]') is not None


async def test_selecting_a_chosen_section_projects_that_sections_tracks(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=binding(TWO)) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            html = await lookup_and_load(client, media.id)
            fields = form_fields(html.select_one("[data-version-decision]"))
            unchosen = soup(await client.post(page(media.id, "/decision"), data={**fields, "action": "select"}, headers=ORIGIN))
            assert unchosen.select_one("[data-remote-notice][role=alert]") is not None, "several sections: an explicit choice is required"
            chosen = soup(
                await client.post(page(media.id, "/decision"), data={**fields, "action": "select", "text_section_ordinal": "2"}, headers=ORIGIN)
            )
            assert chosen.select_one('[data-version-state="selected"]') is not None
        async with h.sessions() as session:
            selected = await get_selected_recording_source(session, media.id)
    assert selected is not None
    assert [track.title for track in selected.tracks] == ["Charlie", "Delta", "Echo"], "the chosen section's tracks, not none and not all"
    assert len(selected.tracks) == 3 and [track.position for track in selected.tracks] == [3, 4, 5]


async def test_a_warned_section_is_selected_only_with_the_acknowledgement(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=binding(WARNED)) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            html = await lookup_and_load(client, media.id)
            fields = {**form_fields(html.select_one("[data-version-decision]")), "action": "select", "text_section_ordinal": "2"}
            refused = soup(await client.post(page(media.id, "/decision"), data=fields, headers=ORIGIN))
            assert "acknowledgement" in refused.select_one("[data-remote-notice][role=alert]").get_text()
            accepted = soup(await client.post(page(media.id, "/decision"), data={**fields, "accept_text_warnings": "true"}, headers=ORIGIN))
            assert accepted.select_one('[data-version-state="selected"]') is not None
        async with h.sessions() as session:
            selected = await get_selected_recording_source(session, media.id)
    assert selected is not None and [track.title for track in selected.tracks] == ["Charlie", "Delta", "Echo"]


async def test_one_section_is_picked_automatically_and_its_tracks_are_shown(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=binding(ONE)) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            html = await lookup_and_load(client, media.id)
            fields = form_fields(html.select_one("[data-version-decision]"))
            chosen = soup(await client.post(page(media.id, "/decision"), data={**fields, "action": "select"}, headers=ORIGIN))
            assert chosen.select_one('[data-version-state="selected"]') is not None
        async with h.sessions() as session:
            selected = await get_selected_recording_source(session, media.id)
    assert selected is not None and [track.title for track in selected.tracks] == ["Alpha", "Bravo"]
