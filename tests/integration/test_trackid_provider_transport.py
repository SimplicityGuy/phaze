"""The TrackId provider over the real transport: real registry, real interpreter, real ledger, FAKE socket and clock.

Nothing here opens a socket or contacts any host. Every request is recorded by ``FakeSocket`` so the exact
URL, query and credential header the provider causes are asserted. Slugs, titles and ids are synthetic.
"""

from dataclasses import dataclass
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr
import pytest
from sqlalchemy import text

from phaze.services.remote_transport import RemoteTransport
from phaze.tracklist_providers.domain import DiscoveryBudget, Fact, LoadBudget, OutcomeStatus, ProviderCandidate, RecordingContext, SourceIdentity
from phaze.tracklist_providers.host_profile import RemoteHostProfile
from phaze.tracklist_providers.registry import ProviderRegistry
from phaze.tracklist_providers.trackid import TRACKID_PROFILE, TrackIdProvider
from tests.identify.services.trackid_fixtures import detail_document, list_page, process, row
from tests.integration.test_remote_transport import FakeClock, FakeSocket, Reply


pytestmark = pytest.mark.integration

SECRET = "synthetic-secret-8d41"
CLIENT = "tkid_synthetic01"
CONTEXT = RecordingContext(
    recording_id="recording-01",
    artist=Fact(value="Example Artist", certainty="known"),
    event=Fact(value="Example Event", certainty="inferred"),
    date=Fact(value="2026-01-01", certainty="inferred"),
)
GAP = TRACKID_PROFILE.min_gap_seconds


@dataclass(frozen=True)
class Wiring:
    registry: ProviderRegistry
    profiles: tuple[RemoteHostProfile, ...]
    credentials: dict[str, SecretStr]

    def __repr__(self) -> str:
        return f"Wiring(profiles={len(self.profiles)})"


def composition() -> Wiring:
    registry = ProviderRegistry((TrackIdProvider(),), remote_profiles=(TRACKID_PROFILE,))
    return Wiring(registry, (TRACKID_PROFILE,), {"trackid": SecretStr(f"{CLIENT}:{SECRET}")})


def reply(body: object, **extra: Any) -> Reply:
    return Reply(body=(body if isinstance(body, bytes) else json.dumps(body).encode()), **extra)


def transport(engine: Any, clock: FakeClock, socket: FakeSocket, wiring: Wiring) -> RemoteTransport:
    return RemoteTransport(profiles=wiring.profiles, engine=engine, socket=socket, clock=clock, credentials=wiring.credentials)


async def test_discovery_and_load_cause_exactly_the_documented_requests(committed_db: Any, caplog: pytest.LogCaptureFixture) -> None:
    engine, _ = committed_db
    clock, wiring = FakeClock(), composition()
    socket = FakeSocket(
        clock,
        reply(list_page(range(20), 25)),
        reply(list_page(range(20, 25), 25)),
        reply(detail_document([process(10, [row("00:00:05", title="First"), row("00:03:10", title="Second")])])),
    )
    live = transport(engine, clock, socket, wiring)
    run = live.run()
    caplog.set_level(logging.DEBUG)
    discovery = await wiring.registry.discover("trackid", CONTEXT, DiscoveryBudget(), run)
    assert (discovery.status, discovery.code, len(discovery.candidates)) == (OutcomeStatus.FOUND, "search_exhausted", 25)

    first, second = (urlsplit(request.url) for request in socket.requests[:2])
    assert (first.scheme, first.netloc, first.path) == ("https", "trackid.net", "/api/integration/audiostreams")
    # The first page is page 0 for this host, and only the documented filters plus the recording's facts are sent.
    assert parse_qs(first.query) == {"status": ["Ready"], "pageSize": ["20"], "keywords": ["Example Artist Example Event"], "currentPage": ["0"]}
    assert parse_qs(second.query)["currentPage"] == ["1"] and set(parse_qs(second.query)) == set(parse_qs(first.query))
    assert "2026" not in first.query  # the date fact is not sent

    candidate = discovery.candidates[3]
    loaded = await wiring.registry.load(candidate, LoadBudget(), run)
    assert (loaded.status, loaded.code) == (OutcomeStatus.FOUND, "parsed_trackid_detail")
    assert loaded.snapshot is not None and [t.title for t in loaded.snapshot.tracks] == ["First", "Second"]
    detail = urlsplit(socket.requests[2].url)
    assert (detail.netloc, detail.path, detail.query) == ("trackid.net", f"/api/integration/audiostreams/{candidate.identity.native_id}", "")

    # The one credential header carries Bearer <client_id>:<client_secret>, on every request, and nowhere else.
    for request in socket.requests:
        assert request.credential is not None and request.credential[0] == "Authorization"
        assert request.credential[1].get_secret_value() == f"Bearer {CLIENT}:{SECRET}"
        assert dict(request.headers).keys() == {"User-Agent", "Accept-Encoding"}
    gaps = [(b - a).total_seconds() for a, b in zip(socket.sent_at, socket.sent_at[1:], strict=False)]
    assert len(socket.requests) == 3 and min(gaps) >= GAP

    surfaces = [caplog.text, repr(live), repr(run), repr(wiring), discovery.model_dump_json(), loaded.model_dump_json()]
    surfaces += [repr(request) for request in socket.requests]
    async with engine.connect() as conn:
        surfaces.append(str((await conn.execute(text("SELECT * FROM remote_transport_attempts"))).all()))
    assert all(SECRET not in surface and CLIENT not in surface for surface in surfaces)


async def test_transport_outcomes_map_to_typed_results_none_of_them_a_tracklist(committed_db: Any) -> None:
    engine, _ = committed_db
    clock, wiring = FakeClock(), composition()
    oversized = b'{"pad": "' + b"x" * 262144 + b'"}'
    socket = FakeSocket(
        clock,
        Reply(status=404, body=b""),
        Reply(status=401, body=b""),
        Reply(status=200, body=oversized),
        reply(detail_document([process(10, [])])),
        reply(detail_document([process(10, [row()]), process(11, [], end_date=None)])),
        Reply(status=429, body=b""),
    )
    run = transport(engine, clock, socket, wiring).run()

    def candidate(slug: str) -> ProviderCandidate:
        return ProviderCandidate(identity=SourceIdentity(provider_id="trackid", native_id=slug))

    outcomes = [await wiring.registry.load(candidate(f"set-slug-0{n}"), LoadBudget(), run) for n in range(6)]
    absent, unauthorized, too_large, empty, in_flight, limited = outcomes
    assert (absent.status, absent.code, absent.scope) == (OutcomeStatus.ABSENT, "stream_not_found", "set-slug-00")
    assert (unauthorized.status, unauthorized.code) == (OutcomeStatus.UNAVAILABLE, "http_401")
    assert (too_large.status, too_large.code) == (
        OutcomeStatus.UNAVAILABLE,
        "response_too_large",
    ) and "nothing about the tracklist" in too_large.detail
    assert empty.status == OutcomeStatus.INCOMPLETE and empty.snapshot is not None and not empty.snapshot.tracks
    assert in_flight.status == OutcomeStatus.INCOMPLETE and "still running" in in_flight.detail
    assert (limited.status, limited.code) == (OutcomeStatus.RETRY, "http_429")
    # After the 429 the host is stopped: the next lookup is refused, never reported as "no tracklist".
    after = await wiring.registry.load(candidate("set-slug-09"), LoadBudget(), run)
    assert (after.status, after.code) == (OutcomeStatus.UNAVAILABLE, "transport_stopped") and after.snapshot is None
    assert len(socket.requests) == 6
    assert all(o.status != OutcomeStatus.FOUND for o in (*outcomes, after))
