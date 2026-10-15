"""The recording page's look-up panel through its REAL consumers: the real templates, routes, RemoteLookup and Postgres.

Nothing here opens a socket or contacts a host. The provider is the fictional ``example_catalog`` of
docs/design/tracklist-provider-specification.md behind ``CatalogSocket`` (hosts are ``.invalid``) with a fake clock,
exactly as tests/integration/test_remote_lookup.py builds it; every identifier and track is synthetic. The panel is
rendered by the application's own Jinja environment and fetched through the application's own routes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import re
from typing import Any
import uuid

from bs4 import BeautifulSoup
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderRecordingSelection, ProviderSelectionEvent
from phaze.models.remote_transport import RemoteTransportAttempt
from phaze.routers.record import templates
from phaze.schemas.remote_lookup import ProviderStatus
from phaze.services.remote_lookup import _MESSAGES, _assessment_evidence, operator_outcome
from phaze.services.remote_lookup_view import (
    AssessmentView,
    CandidateGroup,
    RemotePanel,
    TrackView,
    VersionView,
    parse_assessment,
    safe_attribution_url,
)
from phaze.services.remote_providers import REMOTE_PROVIDERS, RemoteProviderBinding
from phaze.tracklist_providers.domain import Completeness, Fact, OutcomeStatus, ProviderTrack, RecordingContext, Snapshot, Timestamp
from phaze.tracklist_providers.matching import AssessedSubject, assess_recording_match
from tests.integration.test_remote_lookup import (
    EXAMPLE,
    EXAMPLE_PROFILE,
    SECOND_PROFILE,
    ExampleCatalog,
    ExampleUpstream,
    Harness,
    Reply,
    SecondCatalog,
    count,
    enabled,
    harness,
    seed,
)


pytestmark = pytest.mark.integration

ORIGIN = {"origin": "http://test"}
BOTH = (RemoteProviderBinding(ExampleCatalog(), EXAMPLE_PROFILE), RemoteProviderBinding(SecondCatalog(), SECOND_PROFILE))


@asynccontextmanager
async def client_for(h: Harness) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.state.remote_lookup = h.lookup

    async def override() -> AsyncIterator[AsyncSession]:
        async with h.sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def soup(response: Any) -> BeautifulSoup:
    assert response.status_code == 200, response.text[:400]
    return BeautifulSoup(response.text, "html.parser")


def form_fields(form: Any) -> dict[str, str]:
    """The fields a browser would submit from this native form (the clicked button's name/value included separately)."""
    return {
        item["name"]: item.get("value", "") for item in form.find_all("input") if item.get("name") and item.get("type") not in {"checkbox", "radio"}
    }


def page(media_id: uuid.UUID, path: str = "") -> str:
    return f"/files/{media_id}/remote-lookup{path}"


async def lookup_and_load(client: AsyncClient, media_id: uuid.UUID, provider: str = EXAMPLE) -> BeautifulSoup:
    looked = await client.post(page(media_id, "/lookup"), data={"provider_id": provider}, headers=ORIGIN)
    form = soup(looked).select_one("[data-load-form]")
    assert form is not None, looked.text[:600]
    return soup(await client.post(page(media_id, "/load"), data=form_fields(form), headers=ORIGIN))


# =============================================================================================== acceptance
async def test_lookup_action_lists_only_enabled_providers(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=BOTH, settings=enabled(EXAMPLE)) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            html = soup(await client.get(page(media.id)))
    assert html.select("[data-provider-list]") == [], "global inventory belongs in Settings"
    assert html.select("[data-clear-stop-form]") == []
    [button] = html.select("[data-lookup-form] button")
    assert "Fictional example catalog" in button.get_text()
    assert "Settings" in html.get_text()
    assert h.socket.requests == []


async def test_the_shipped_providers_are_all_listed_as_off_with_no_action_until_enabled(committed_db: Any) -> None:
    assert len(REMOTE_PROVIDERS) >= 2
    async with harness(committed_db, {}, bindings=tuple(REMOTE_PROVIDERS), settings={}) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            html = soup(await client.get("/admin/remote-providers"))
            recording = soup(await client.get(page(media.id)))
    assert {item["data-provider"] for item in html.select("[data-provider]")} == {binding.profile.provider_id for binding in REMOTE_PROVIDERS}
    assert "Disabled" in html.get_text()
    assert recording.select("[data-lookup-form]") == []
    assert h.socket.requests == []


async def test_no_lookup_is_issued_on_page_load(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), bindings=BOTH) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            record = await client.get(f"/files/{media.id}")
            assert record.status_code == 200
            slot = BeautifulSoup(record.text, "html.parser").select_one("[data-remote-lookup-slot]")
            assert slot is not None and slot["hx-get"] == page(media.id) and slot["hx-trigger"] == "load"
            assert "hx-post" not in str(slot), "the slot only READS; it carries no action"
            for _ in range(2):
                assert (await client.get(page(media.id))).status_code == 200
            # Also with a held candidate and a loaded version already stored: still only reads.
            await lookup_and_load(client, media.id)
            sent = len(h.socket.requests)
            assert sent == 2, "one search and one fetch, each from an explicit POST"
            for url in (f"/files/{media.id}", page(media.id), f"/record/{media.id}"):
                assert (await client.get(url)).status_code == 200
    assert len(h.socket.requests) == sent, "rendering the page or the panel issued a provider request"
    assert h.socket.probes == sent


async def test_a_get_issues_nothing_even_for_a_fresh_database(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            await client.get(f"/files/{media.id}")
            await client.get(page(media.id))
        assert h.socket.requests == [] and h.socket.probes == 0
        assert await count(h.sessions, RemoteTransportAttempt) == 0, "not even a budgeted attempt was recorded"
        assert await count(h.sessions, ProviderRecordingCandidate) == 0


async def test_candidate_review_shows_evidence_conflicts_and_incomplete_scope(committed_db: Any) -> None:
    async with harness(
        committed_db, ExampleUpstream().routes(), bindings=(RemoteProviderBinding(ExampleCatalog(partial=True), EXAMPLE_PROFILE),)
    ) as h:
        media = await seed(h.sessions)
        async with h.sessions() as session:
            # The recording's known artist tag differs from the provider's artist, so the matcher records a conflict.
            from phaze.models.metadata import FileMetadata

            await session.execute(update(FileMetadata).where(FileMetadata.file_id == media.id).values(artist="Different Artist"))
            await session.commit()
        async with client_for(h) as client:
            looked = soup(await client.post(page(media.id, "/lookup"), data={"provider_id": EXAMPLE}, headers=ORIGIN))
            result = looked.select_one("[data-remote-notice]")
            assert result is not None and "candidate(s) held for review" in result.get_text()
            assert looked.select_one('[data-lookup-scope="complete"]') is not None
            group = looked.select_one("[data-candidate-group]")
            assert group is not None
            assert "Example Event (set-01)" in group.get_text() and "Fictional example catalog" in group.get_text()
            assert "Rights: unknown" in group.select_one("[data-rights]").get_text()
            assert group.select_one("[data-evidence-band]") is not None
            loaded = await client.post(page(media.id, "/load"), data=form_fields(group.select_one("[data-load-form]")), headers=ORIGIN)
            html = soup(loaded)
    version = html.select_one("[data-loaded-version]")
    assert version is not None
    # Per-field evidence, with the CONFLICT shown as one.
    conflicts = version.select('[data-evidence-relation="contradictory"]')
    assert conflicts and any("CONFLICTS" in item.get_text() for item in conflicts)
    assert version.select_one('[data-evidence-band="conflicting"]') is not None
    # The snapshot is partial: its scope is stated, it is not selectable, and it never says "no tracklist".
    scope = version.select_one("[data-incomplete-scope]")
    assert scope is not None and "not evidence that no tracklist exists" in scope.get_text()
    assert version.select_one('button[value="select"]') is None, "an incomplete snapshot cannot be selected"
    # Rights are the provider's recorded text, shown as given.
    assert "rights:unknown" in version.select_one("[data-rights]").get_text()


async def test_ambiguous_date_alternatives_come_from_the_real_matcher_and_are_shown(committed_db: Any) -> None:
    context = RecordingContext(
        recording_id="r", artist=Fact(value="Example Artist", certainty="known"), date=Fact(value="03/04/2024", certainty="known")
    )
    snapshot = Snapshot(
        identity={"provider_id": EXAMPLE, "native_id": "set-01"},
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        artist=Fact(value="Example Artist", certainty="known"),
        date=Fact(value="2024-04-03", certainty="known"),
        source_format="x",
        parser_version="x-1",
        completeness=Completeness(state="complete", reason="x"),
    )
    evidence = _assessment_evidence(assess_recording_match(context, AssessedSubject(source=snapshot), scope=snapshot.completeness))
    view = parse_assessment(evidence)
    assert view is not None and view.date_alternatives, "the matcher keeps every reading of an ambiguous recording date"
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            await client.post(page(media.id, "/lookup"), data={"provider_id": EXAMPLE}, headers=ORIGIN)
            async with h.sessions() as session:
                await session.execute(update(ProviderRecordingCandidate).values(evidence=list(evidence)))
                await session.commit()
            html = soup(await client.get(page(media.id)))
    shown = html.select_one("[data-date-alternatives]")
    assert shown is not None and all(value in shown.get_text() for value in view.date_alternatives)


async def test_selecting_a_candidate_requires_an_explicit_operator_action(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            html = await lookup_and_load(client, media.id)
            # Looking up and loading selected nothing, and the page shows a PENDING version waiting on the operator.
            assert await count(h.sessions, ProviderRecordingSelection) == 0
            assert html.select_one('[data-version-state="pending"]') is not None
            form = html.select_one("[data-version-decision]")
            assert form is not None
            fields = form_fields(form)
            # Compare-and-set values are carried in the form; the actor is NOT a field.
            assert set(fields) >= {"observation_id", "decision_id", "expected_selection_token", "expected_revision", "expected_media_sha256"}
            assert fields["expected_media_sha256"] == media.sha256_hash and fields["expected_revision"] == "rev-a"
            assert "actor" not in fields and form.select_one('button[value="select"]') is not None
            again = soup(await client.get(page(media.id))).select_one("[data-version-decision]")
            assert form_fields(again)["decision_id"] != fields["decision_id"], "decision_id is minted per render, server-side"

            # A POST that is not the operator's explicit select (no action) is refused and selects nothing.
            refused = soup(await client.post(page(media.id, "/decision"), data={**fields, "action": ""}, headers=ORIGIN))
            assert refused.select_one("[data-remote-notice][role=alert]") is not None
            assert await count(h.sessions, ProviderRecordingSelection) == 0
            # Missing the media hash: the compare-and-set value is required, not defaulted.
            incomplete = {key: value for key, value in fields.items() if key != "expected_media_sha256"}
            assert soup(await client.post(page(media.id, "/decision"), data={**incomplete, "action": "select"}, headers=ORIGIN)).select_one(
                "[data-remote-notice][role=alert]"
            )
            assert await count(h.sessions, ProviderRecordingSelection) == 0

            chosen = soup(await client.post(page(media.id, "/decision"), data={**fields, "action": "select"}, headers=ORIGIN))
            assert "Tracklist selected" in chosen.select_one("[data-remote-notice]").get_text()
            assert chosen.select_one('[data-version-state="selected"]') is not None
            assert await count(h.sessions, ProviderRecordingSelection) == 1
            async with h.sessions() as session:
                event = await session.scalar(select(ProviderSelectionEvent).where(ProviderSelectionEvent.action == "select"))
            assert event is not None and event.actor == "admin"

            # A stale form (the selection token it carried is now outdated) is refused, not silently applied.
            stale = soup(
                await client.post(page(media.id, "/decision"), data={**fields, "decision_id": str(uuid.uuid4()), "action": "select"}, headers=ORIGIN)
            )
            assert "Selected source changed" in stale.select_one("[data-remote-notice][role=alert]").get_text()


async def test_reject_then_reopen_requires_a_reason_and_the_rejection_is_listed(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            html = await lookup_and_load(client, media.id)
            fields = form_fields(html.select_one("[data-version-decision]"))
            rejected = soup(await client.post(page(media.id, "/decision"), data={**fields, "action": "reject"}, headers=ORIGIN))
            section = rejected.select_one("[data-rejected-section]")
            assert section is not None and section.select_one("[data-previously-rejected]") is not None
            assert rejected.select_one("[data-candidate-list]") is None, "a rejected candidate is no longer proposed"
            reopen = section.select_one("[data-reopen-form]")
            assert reopen is not None and reopen.select_one("textarea[name=reason][required]") is not None

            # A repeat lookup lists the rejected candidate as not proposed again, and still does not re-propose it.
            repeat = soup(await client.post(page(media.id, "/lookup"), data={"provider_id": EXAMPLE}, headers=ORIGIN))
            assert repeat.select_one("[data-previously-rejected-result]") is not None
            assert repeat.select_one("[data-load-form]") is None

            reopen_fields = form_fields(repeat.select_one("[data-reopen-form]"))
            assert reopen_fields["action"] == "reopen"
            for reason in (None, "", "   "):
                data = dict(reopen_fields) if reason is None else {**reopen_fields, "reason": reason}
                blank = soup(await client.post(page(media.id, "/decision"), data=data, headers=ORIGIN))
                assert "requires a reason" in blank.select_one("[data-remote-notice][role=alert]").get_text()
                assert blank.select_one("[data-rejected-section]") is not None, "still rejected"
            reopened = soup(await client.post(page(media.id, "/decision"), data={**reopen_fields, "reason": "Rejected by mistake"}, headers=ORIGIN))
            assert "reopened" in reopened.select_one("[data-remote-notice]").get_text()
            assert reopened.select_one("[data-rejected-section]") is None and reopened.select_one("[data-candidate-list]") is not None
            async with h.sessions() as session:
                event = await session.scalar(select(ProviderSelectionEvent).where(ProviderSelectionEvent.action == "reopen"))
            assert event is not None and event.decision_payload["reason"] == "Rejected by mistake"


async def test_a_stopped_provider_shows_its_stop_code_and_an_operator_clears_it_with_a_reason(committed_db: Any) -> None:
    upstream = ExampleUpstream(search_reply=Reply(status=403))
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            first = soup(await client.post(page(media.id, "/lookup"), data={"provider_id": EXAMPLE}, headers=ORIGIN))
            assert first.select_one("[data-outcome-code]")["data-outcome-code"] == "http_403"
            assert first.select("[data-clear-stop-form]") == [], "global clear action moved to Settings"
            settings = soup(await client.get("/admin/remote-providers"))
            assert "Stopped: http_403" in settings.get_text()
            response = await client.post(
                f"/admin/remote-providers/{EXAMPLE}/clear-stop", data={"reason": "Synthetic test: access restored"}, headers=ORIGIN
            )
            assert "Stop cleared" in soup(response).get_text()
            status = (await h.lookup.provider_status())[0]
            assert not status.stopped and status.cleared_by == "admin"
            assert status.clear_reason == "Synthetic test: access restored"
            assert len(h.socket.requests) == 1


async def test_attribution_links_open_in_a_new_tab_and_unsafe_urls_never_become_links(committed_db: Any) -> None:
    assert safe_attribution_url("https://catalog.example.invalid/sets/set-01") is not None
    assert safe_attribution_url("http://catalog.example.invalid/x") is not None
    for bad in ("javascript:alert(1)", "data:text/html,x", "//catalog.example.invalid/x", "ftp://catalog.example.invalid/x", "", None, "https://"):
        assert safe_attribution_url(bad) is None, bad
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            await client.post(page(media.id, "/lookup"), data={"provider_id": EXAMPLE}, headers=ORIGIN)
            async with h.sessions() as session:
                from phaze.models.provider_source import ProviderSourceObservation

                await session.execute(update(ProviderSourceObservation).values(source_url="https://catalog.example.invalid/sets/set-01"))
                await session.commit()
            link = soup(await client.get(page(media.id))).select_one("[data-attribution-link]")
            assert link is not None and link["href"] == "https://catalog.example.invalid/sets/set-01"
            assert link["target"] == "_blank" and set(link["rel"]) == {"noopener", "noreferrer"}
            async with h.sessions() as session:
                await session.execute(update(ProviderSourceObservation).values(source_url="javascript:alert(1)"))
                await session.commit()
            html = soup(await client.get(page(media.id)))
            assert html.select_one("[data-attribution-link]") is None and "javascript:" not in str(html)
        assert len(h.socket.requests) == 1, "phaze never fetches an attribution link"


async def test_a_cross_origin_or_unmarked_post_sends_nothing(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        async with client_for(h) as client:
            for path, data in (
                ("/lookup", {"provider_id": EXAMPLE}),
                ("/load", {"candidate_id": str(uuid.uuid4())}),
                ("/decision", {"action": "reject"}),
            ):
                for headers in ({"origin": "https://elsewhere.invalid", "sec-fetch-site": "cross-site"}, {}):
                    assert (await client.post(page(media.id, path), data=data, headers=headers)).status_code == 403, (path, headers)
        assert h.socket.requests == [] and await count(h.sessions, ProviderRecordingCandidate) == 0


async def test_a_candidate_of_another_recording_cannot_be_loaded_from_this_page(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media, other = await seed(h.sessions), await seed(h.sessions)
        async with client_for(h) as client:
            looked = soup(await client.post(page(media.id, "/lookup"), data={"provider_id": EXAMPLE}, headers=ORIGIN))
            fields = form_fields(looked.select_one("[data-load-form]"))
            sent = len(h.socket.requests)
            refused = soup(await client.post(page(other.id, "/load"), data=fields, headers=ORIGIN))
            assert "held for this recording" in refused.select_one("[data-remote-notice][role=alert]").get_text()
        assert len(h.socket.requests) == sent


async def test_the_panel_without_a_service_or_for_a_missing_recording(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        app = create_app()

        async def override() -> AsyncIterator[AsyncSession]:
            async with h.sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert "not available in this deployment" in soup(await client.get(page(media.id))).get_text()
            unconfigured = soup(await client.post(page(media.id, "/lookup"), data={"provider_id": EXAMPLE}, headers=ORIGIN))
            assert "not configured" in unconfigured.select_one("[data-remote-notice][role=alert]").get_text()
            assert (await client.get(page(uuid.uuid4()))).status_code == 404


# =============================================================================================== outcomes and rendering
@pytest.mark.parametrize("code", sorted(_MESSAGES))
def test_every_outcome_is_rendered_as_given_and_none_as_no_tracklist(code: str) -> None:
    outcome = operator_outcome(OutcomeStatus.UNAVAILABLE, code, evidence=("stop=http_403", "retry_after_seconds=30"))
    html = templates.get_template("record/remote/_notice.html").render(
        notice=type("N", (), {"outcome": outcome, "message": None, "failed": False, "provider_name": None, "lookup": None})()
    )
    parsed = BeautifulSoup(html, "html.parser")
    line = parsed.select_one("[data-outcome-code]")
    assert line is not None and line["data-outcome-code"] == code and line["data-outcome-status"] == outcome.status
    assert outcome.message in line.get_text(), "the message is rendered as given, never empty"
    text = parsed.get_text()
    assert code in text
    # "no tracklist" may appear only inside the disclaimer that a result is NOT evidence of one.
    assert not re.search(r"(?<!not evidence that )no tracklist (exists|was found)", text)


def test_the_assessment_parser_reads_what_the_matcher_wrote_and_nothing_else() -> None:
    assert parse_assessment(()) is None and parse_assessment(("kind:tracklist", "discovered:x")) is None
    view = parse_assessment(
        (
            "assessment:eligibility:review_required",
            "assessment:band:conflicting",
            "assessment:artist:exact",
            "assessment:event:missing",
            "assessment:date:contradictory:day",
            "assessment:date_alternative:2024-03-04",
            "assessment:date_alternative:2024-04-03",
            "assessment:scope:incomplete:one page only",
            "assessment:tied_with:2",
            "assessment:reason:date disagrees",
        )
    )
    assert isinstance(view, AssessmentView) and view.conflicting and view.band == "conflicting"
    assert [(field.label, field.conflicting) for field in view.fields] == [("Artist", False), ("Event", False), ("Date", True)]
    assert view.date_alternatives == ("2024-03-04", "2024-04-03") and view.tied_with == 2
    assert (view.scope_state, view.scope_reason) == ("incomplete", "one page only") and view.reasons == ("date disagrees",)


def test_tracks_show_approximate_offsets_as_approximate_and_unresolved_rows_as_unresolved() -> None:
    from phaze.services.remote_lookup_view import _track_view

    def stamp(usability: str, precision: str = "second") -> Timestamp:
        return Timestamp.model_validate(
            {
                "original": "00:10:00",
                "kind": "offset",
                "precision": precision,
                "origin": "start",
                "offset": {"numerator": 600, "denominator": 1},
                "offset_usability": usability,
                "evidence": ("x",),
            }
        )

    approximate = _track_view(ProviderTrack(position=1, artist="A", title="T", timestamp=stamp("approximate")))
    assert approximate.timing == "00:10:00" and approximate.timing_approximate
    assert _track_view(ProviderTrack(position=1, artist="A", title="T", timestamp=stamp("qualified"))).timing_approximate is False
    unresolved = _track_view(ProviderTrack(position=2))
    assert unresolved.unresolved and unresolved.timing is None
    unusable = _track_view(ProviderTrack(position=3, title="T", timestamp=Timestamp(original="~10m")))
    assert unusable.timing is not None and "no usable offset" in unusable.timing and not unusable.unresolved

    version = VersionView(
        observation_id=uuid.uuid4(), candidate_id=uuid.uuid4(), candidate_status="pending", status="found", revision="r", retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        url=None, completeness_state="complete", completeness_reason="all", tracks=(approximate, unresolved, unusable), track_count=3, rights=(), assessment=None,
        conflicting_source=True, selected=False, can_select=True, can_reject=True, unreadable=False, decision_id=uuid.uuid4(),
    )  # fmt: skip
    group = CandidateGroup(
        provider_id="p", provider_name="Fictional", native_id="n", display_name="Name", url=None, discovered_observation_id=None, discovered_candidate_id=None,
        discovered_revision=None, assessment=None, versions=(version,), rejected=False, rejected_at=None, reopen_observation_id=None, reopen_revision=None,
        can_load=False, can_reject_discovered=False, decision_id=uuid.uuid4(),
    )  # fmt: skip
    status = ProviderStatus(provider_id="p", display_name="Fictional", enabled=True, stopped=False)
    panel = RemotePanel(media_id=uuid.uuid4(), media_sha256="a" * 64, expected_selection_token="", providers=(status,), groups=(group,), rejected=())
    html = BeautifulSoup(templates.get_template("record/remote/_group.html").render(group=group, panel=panel, file_id=panel.media_id), "html.parser")
    assert "approximate offset (not exact): 00:10:00" in html.select_one('[data-track="1"]').get_text()
    assert html.select_one('[data-track="2"]').has_attr("data-track-unresolved") and "Unresolved" in html.select_one('[data-track="2"]').get_text()
    assert html.select_one("[data-source-conflict]") is not None and html.select_one('input[name="accept_conflict"]') is not None
    assert html.select_one("[data-rights]") is not None and "unknown" in html.select_one("[data-loaded-version] [data-rights]").get_text()
    assert TrackView  # the dataclass the template reads
