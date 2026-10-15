"""The TrackId provider through the REAL operator-triggered lookup: registry, transport, persistence and Postgres.

Nothing here opens a socket or contacts a host: every exchange goes through the shared ``CatalogSocket`` fake and
time is ``FakeClock``. Slugs, titles, ids and the credential are synthetic.
"""

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr
import pytest
from sqlalchemy import select, text

from phaze.config_remote_providers import RemoteProviderSettings
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderRecordingSelection, ProviderSourceObservation
from phaze.schemas.remote_lookup import LoadCommand, LookupCommand
from phaze.services.remote_lookup import RemoteLookup, effective_profile
from phaze.services.remote_providers import REMOTE_PROVIDERS
from phaze.services.remote_transport import OutboundRequest
from phaze.tracklist_providers.trackid import RIGHTS_EVIDENCE, TRACKID_PROFILE
from tests.identify.services.trackid_fixtures import detail_document, list_page, process, row
from tests.integration.test_remote_lookup import CatalogSocket, Reply, decide, harness, seed


pytestmark = pytest.mark.integration

SECRET = "synthetic-secret-8d41"
CLIENT = "tkid_synthetic01"
CREDENTIAL = f"{CLIENT}:{SECRET}"
LIST_PATH = "/api/integration/audiostreams"
DETAIL = "/api/integration/audiostreams/"


def settings(**values: Any) -> dict[str, RemoteProviderSettings]:
    credentials = {"client_credentials": SecretStr(CREDENTIAL)}
    return {"trackid": RemoteProviderSettings(provider_id="trackid", enabled=True, credentials=credentials, **values)}


def reply(body: object, status: int = 200) -> Reply:
    return Reply(status=status, body=json.dumps(body).encode())


class Upstream:
    """25 Ready streams over two list pages; detail documents by slug."""

    def __init__(self, details: dict[str, Reply] | None = None, list_reply: Reply | None = None) -> None:
        self.details = details or {}
        self.list_reply = list_reply

    def routes(self) -> dict[str, Any]:
        return {DETAIL: self.detail, LIST_PATH: self.listing}

    def listing(self, request: OutboundRequest) -> Reply:
        if self.list_reply is not None:
            return self.list_reply
        page = int(parse_qs(urlsplit(request.url).query)["currentPage"][0])
        return reply(list_page(range(page * 20, min(25, page * 20 + 20)), 25))

    def detail(self, request: OutboundRequest) -> Reply:
        slug = urlsplit(request.url).path.removeprefix(DETAIL)
        return self.details.get(slug) or reply(detail_document([process(10, [row("00:00:05", title="First"), row("00:03:10", title="Second")])]))


async def everything_stored(h: Any) -> str:
    tables = (
        "provider_source_objects",
        "provider_source_observations",
        "provider_recording_candidates",
        "remote_transport_attempts",
        "remote_transport_ledgers",
    )
    stored = ""
    async with h.engine.connect() as conn:
        for table in tables:
            stored += str((await conn.execute(text(f"SELECT t::text FROM {table} t"))).all())  # noqa: S608
    return stored


async def test_end_to_end_through_orchestration_with_a_fake_transport(committed_db: Any, caplog: pytest.LogCaptureFixture) -> None:
    async with harness(committed_db, Upstream().routes(), bindings=REMOTE_PROVIDERS, settings=settings()) as h:
        media = await seed(h.sessions)
        caplog.set_level(logging.DEBUG)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="trackid"))
        assert found.outcome.status == "candidates" and len(found.candidates) == 25
        assert found.scope is not None and found.scope.state == "complete"
        lists = [parse_qs(urlsplit(request.url).query) for request in h.socket.requests]
        # The first page of this host is page 0; the recording's facts are the only variable part of the query.
        assert [query["currentPage"] for query in lists] == [["0"], ["1"]]
        assert lists[0]["keywords"] == ["Example Artist"] and lists[0]["status"] == ["Ready"] and "2024" not in str(lists[0])

        candidate = next(c for c in found.candidates if c.identity.native_id == "set-slug-03")
        loaded = await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert (loaded.outcome.status, loaded.outcome.code) == ("loaded", "parsed_trackid_detail")
        assert urlsplit(h.socket.requests[-1].url).path == f"{DETAIL}set-slug-03"
        for request in h.socket.requests:
            assert request.credential is not None and request.credential[0] == "Authorization"
            assert request.credential[1].get_secret_value() == f"Bearer {CREDENTIAL}"
        assert min((b - a).total_seconds() for a, b in zip(h.socket.sent_at, h.socket.sent_at[1:], strict=False)) >= 6.0

        assert loaded.observation_id is not None
        async with h.sessions() as session:
            version = await session.get(ProviderSourceObservation, loaded.observation_id)
            assert version is not None and version.status == "found"
            tracks = version.payload["tracks"]
            assert [t["title"] for t in tracks] == ["First", "Second"]
            assert [t["timestamp"]["offset_usability"] for t in tracks] == ["approximate", "approximate"]
            assert tuple(version.payload["rights_evidence"]) == RIGHTS_EVIDENCE
            review = await session.scalar(select(ProviderRecordingCandidate).where(ProviderRecordingCandidate.observation_id == version.id))
            assert review is not None and review.status == "pending"
            assert await session.scalar(select(ProviderRecordingSelection)) is None, "nothing is selected automatically"
        selected = await decide(h.sessions, media, loaded.observation_id)
        assert selected is not None and [t.title for t in selected.tracks] == ["First", "Second"]

        surfaces = [
            caplog.text,
            repr(h.lookup),
            repr(h.lookup.transport),
            found.model_dump_json(),
            loaded.model_dump_json(),
            await everything_stored(h),
        ]
        surfaces += [repr(request) for request in h.socket.requests]
        assert all(SECRET not in surface and CLIENT not in surface for surface in surfaces)


async def test_a_stream_with_a_reprocess_in_flight_is_held_as_incomplete_evidence_and_never_selectable(committed_db: Any) -> None:
    flight = reply(detail_document([process(10, [row()]), process(11, [], end_date=None)]))
    async with harness(committed_db, Upstream({"set-slug-00": flight}).routes(), bindings=REMOTE_PROVIDERS, settings=settings()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="trackid"))
        loaded = await h.lookup.load(LoadCommand(candidate_id=found.candidates[0].candidate_id))
        assert loaded.outcome.status == "loaded" and "partial snapshot" in loaded.outcome.message
        assert loaded.observation_id is not None
        async with h.sessions() as session:
            version = await session.get(ProviderSourceObservation, loaded.observation_id)
            assert version is not None and version.status == "incomplete"
            assert "still in flight" in version.payload["completeness"]["reason"]
        with pytest.raises(ValueError, match="complete loaded snapshot"):
            await decide(h.sessions, media, loaded.observation_id)


async def test_a_stopped_provider_is_not_contacted_and_is_never_reported_as_no_tracklist(committed_db: Any) -> None:
    async with harness(committed_db, Upstream(list_reply=Reply(status=403)).routes(), bindings=REMOTE_PROVIDERS, settings=settings()) as h:
        media = await seed(h.sessions)
        first = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="trackid"))
        assert (first.outcome.status, first.outcome.code) == ("unavailable", "http_403")
        second = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="trackid"))
        assert (second.outcome.code, second.outcome.stop_code) == ("transport_stopped", "http_403")
        assert second.candidates == () and second.outcome.status != "no_candidates_in_scope" and len(h.socket.requests) == 1


async def test_an_override_of_a_tenth_of_a_second_leaves_the_profile_floor_through_the_orchestration(committed_db: Any) -> None:
    fast = settings(min_interval_seconds=0.1, max_requests_per_run=1000)["trackid"]
    floor = effective_profile(TRACKID_PROFILE, fast)
    assert (floor.min_gap_seconds, floor.per_run_budget, floor.durable_budget, floor.durable_window_seconds, floor.max_response_bytes) == (
        6.0,
        16,
        60,
        3600,
        262144,
    )
    tight = effective_profile(TRACKID_PROFILE, settings(min_interval_seconds=30.0, max_requests_per_run=2)["trackid"])
    assert (tight.min_gap_seconds, tight.per_run_budget) == (30.0, 2)
    async with harness(committed_db, Upstream().routes(), bindings=REMOTE_PROVIDERS, settings=settings(min_interval_seconds=0.1)) as h:
        media = await seed(h.sessions)
        await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="trackid"))
        first, second = h.socket.sent_at
        assert (second - first).total_seconds() >= 6.0


async def test_a_recording_with_only_a_year_sends_nothing(committed_db: Any) -> None:
    async with harness(committed_db, Upstream().routes(), bindings=REMOTE_PROVIDERS, settings=settings()) as h:
        media = await seed(h.sessions)
        async with h.sessions() as session, session.begin():
            metadata = await session.scalar(select(FileMetadata).where(FileMetadata.file_id == media.id))
            assert metadata is not None
            metadata.artist = None
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="trackid"))
        assert (result.outcome.status, result.outcome.code) == ("unavailable", "no_search_hint")
        assert "nothing was sent" in result.outcome.message and h.socket.requests == []


async def test_enabled_without_its_credential_is_unavailable_and_a_malformed_one_stops_startup_without_echoing_it(committed_db: Any) -> None:
    missing = {"trackid": RemoteProviderSettings(provider_id="trackid", enabled=True)}
    async with harness(committed_db, Upstream().routes(), bindings=REMOTE_PROVIDERS, settings=missing) as h:
        media = await seed(h.sessions)
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="trackid"))
        assert result.outcome.code == "credential_missing" and result.outcome.status == "unavailable" and h.socket.requests == []
        status = next(status for status in await h.lookup.provider_status() if status.provider_id == "trackid")
        assert status.enabled and not status.available and status.unavailable_reason == "credential_missing"
    bad = {"trackid": RemoteProviderSettings(provider_id="trackid", enabled=True, credentials={"client_credentials": SecretStr("a:b:c-leaky-value")})}
    engine, sessions = committed_db
    with pytest.raises(ValueError, match="PHAZE_PROVIDER_TRACKID_CLIENT_CREDENTIALS") as raised:
        RemoteLookup(REMOTE_PROVIDERS, settings=bad, engine=engine, sessions=sessions, socket=CatalogSocket(engine, None, {}))  # type: ignore[arg-type]
    assert "leaky" not in str(raised.value)
