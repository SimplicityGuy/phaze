"""The MixesDB adapter through its real consumers: the real registry, the real transport and its Postgres ledger,
and the real interpreter. Only the socket and the clock are fake.

Nothing opens a socket or contacts any host: ``ScriptedSocket`` answers every request in-process by route, and
records exactly what would have left. Every key, title, artist and identifier here is synthetic.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from phaze.config_remote_providers import RemoteProviderSettings
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderRecordingSelection, ProviderSourceObservation
from phaze.schemas.remote_lookup import LoadCommand, LookupCommand, RemoteSourceDecision
from phaze.services.local_source_import import selection_token
from phaze.services.remote_lookup import RemoteLookup, decide_remote_candidate
from phaze.services.remote_providers import REMOTE_PROVIDERS
from phaze.services.remote_transport import OutboundRequest, RemoteTransport
from phaze.tracklist_providers import ProviderRegistry
from phaze.tracklist_providers.domain import (
    DiscoveryBudget,
    Fact,
    LoadBudget,
    OutcomeStatus,
    ProviderCandidate,
    RecordingContext,
    SourceIdentity,
    SourceReference,
)
from phaze.tracklist_providers.mixesdb import PROFILE, MixesDbProvider, page_url


pytestmark = pytest.mark.integration

START = datetime(2026, 1, 1, tzinfo=UTC)
TIMESTAMP = "2026-01-01T00:00:00Z"
REST = "https://www.mixesdb.com/w/rest.php/v1/"
ROBOTS = b"User-agent: *\nCrawl-delay: 4\nDisallow: /w/index.php?\nDisallow: /w/Special:\nDisallow: /db/\n"
KEY = "2026-01-01_-_Example_Artist_@_Example_Festival"
WIKITEXT = "== Tracklist ==\n# [00] Example Artist - Opening Track\n# [07] Example Artist - Closing Track\n"
CONTEXT = RecordingContext(
    recording_id="recording-01",
    sources=(
        SourceReference(
            identity=SourceIdentity(provider_id="local", native_id="companion-01"),
            channel="companion",
            format="text",
            display_name="example-local-file-name.mp3",
        ),
    ),
    artist=Fact(value="Example Artist", certainty="inferred", evidence=("tag:artist",)),
    event=Fact(value="Example Festival", certainty="inferred"),
    date=Fact(value="2026", certainty="inferred", evidence=("tag:year",)),
    duration_seconds=3600.0,
)


class FakeClock:
    def __init__(self) -> None:
        self._wall = START
        self._mono = 1000.0

    def wall(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        self._wall += timedelta(seconds=seconds)
        self._mono += seconds
        await asyncio.sleep(0)


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "application/json"})


class FakeResponse:
    def __init__(self, reply: Reply) -> None:
        self.status = reply.status
        self.headers = reply.headers
        self._body = reply.body

    async def chunks(self) -> AsyncIterator[bytes]:
        yield self._body


class ScriptedSocket:
    """Answers by route: robots, search, or a page by its decoded key. Records every request and when it left."""

    def __init__(self, clock: FakeClock, *, search: Reply | None = None, pages: dict[str, Reply] | None = None) -> None:
        self.clock = clock
        self.search = search or Reply(body=search_body())
        self.pages = pages or {}
        self.requests: list[OutboundRequest] = []
        self.sent_at: list[datetime] = []

    @asynccontextmanager
    async def open(self, request: OutboundRequest) -> AsyncIterator[FakeResponse]:
        self.requests.append(request)
        self.sent_at.append(self.clock.wall())
        path = urlsplit(request.url).path
        if path == "/robots.txt":
            yield FakeResponse(Reply(body=ROBOTS, headers={"content-type": "text/plain"}))
        elif path == "/w/rest.php/v1/search/page":
            yield FakeResponse(self.search)
        else:
            yield FakeResponse(self.pages.get(path, Reply(status=404, body=b'{"errorKey": "rest-nonexistent-title"}')))

    def non_robots(self) -> list[OutboundRequest]:
        return [request for request in self.requests if not request.url.endswith("/robots.txt")]


def search_body(*entries: dict[str, object]) -> bytes:
    return json.dumps({"pages": list(entries)}).encode()


def entry(key: str, title: str | None = None, **extra: object) -> dict[str, object]:
    return {"id": 7, "key": key, "title": title or key.replace("_", " "), "excerpt": "", "matched_title": None, **extra}


def page(key: str = KEY, revision: int = 101, source: str = WIKITEXT) -> Reply:
    body = {"id": 7, "key": key, "title": key, "latest": {"id": revision, "timestamp": TIMESTAMP}, "content_model": "wikitext"}
    body |= {"license": {"url": "", "title": ""}, "source": source}
    return Reply(body=json.dumps(body).encode(), headers={"content-type": "application/json", "etag": '"0123abcd"'})


def page_path(key: str) -> str:
    return "/w/rest.php/v1/page/" + key.replace("@", "%40")


@dataclass
class Rig:
    registry: ProviderRegistry
    transport: RemoteTransport
    socket: ScriptedSocket
    clock: FakeClock


def rig(engine: AsyncEngine, **socket_kwargs: Any) -> Rig:
    clock = FakeClock()
    socket = ScriptedSocket(clock, **socket_kwargs)
    transport = RemoteTransport(profiles=(PROFILE,), engine=engine, socket=socket, clock=clock)
    return Rig(ProviderRegistry((MixesDbProvider(),), remote_profiles=(PROFILE,)), transport, socket, clock)


def query(request: OutboundRequest) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(request.url).query))


async def test_discovery_queries_use_only_recording_context_facts(committed_db: Any) -> None:
    engine, _ = committed_db
    built = rig(engine)
    outcome = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())
    assert outcome.status == OutcomeStatus.INCOMPLETE
    (search,) = built.socket.non_robots()
    assert search.url.startswith(REST + "search/page?")
    # Exactly the documented parameters, and only the artist, event and date facts in the query.
    assert query(search) == {"limit": "100", "q": "Example Artist Example Festival 2026"}
    for request in built.socket.requests:
        assert "example-local-file-name" not in request.url and "recording-01" not in request.url and "companion-01" not in request.url
        assert dict(request.headers) == {"User-Agent": PROFILE.user_agent, "Accept-Encoding": "identity"} and request.credential is None


async def test_result_at_the_cap_is_incomplete_with_no_cursor(committed_db: Any) -> None:
    engine, _ = committed_db
    built = rig(engine, search=Reply(body=search_body(*(entry(f"Example_Set_{index:03d}") for index in range(100)))))
    outcome = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(max_candidates=128), built.transport.run())
    assert (outcome.status, outcome.code, outcome.cursor) == (OutcomeStatus.INCOMPLETE, "search_at_cap", None)
    assert outcome.completeness is not None and outcome.completeness.state == "incomplete" and "limit of 100" in outcome.completeness.reason
    assert len(outcome.candidates) == 100 and "search_returned=100" in outcome.evidence
    assert len(built.socket.non_robots()) == 1


async def test_fewer_than_cap_without_exhaustion_evidence_is_still_incomplete(committed_db: Any) -> None:
    engine, _ = committed_db
    built = rig(engine, search=Reply(body=search_body(entry(KEY), entry("Example_Set_02"), entry("Example_Set_03"))))
    few = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())
    assert (few.status, few.code, few.cursor) == (OutcomeStatus.INCOMPLETE, "search_exhaustion_unknown", None)
    assert few.completeness is not None and few.completeness.state == "incomplete" and len(few.candidates) == 3
    built.socket.search = Reply(body=search_body())
    none = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())
    # Zero results is still not absence, of the recording or of any tracklist.
    assert (none.status, none.code, none.candidates, none.cursor) == (OutcomeStatus.INCOMPLETE, "search_exhaustion_unknown", (), None)


async def test_non_tracklist_namespaces_and_body_matches_are_evidence_not_identity(committed_db: Any) -> None:
    engine, _ = committed_db
    body_match = "2025-05-05_-_Other_Artist_@_Other_Event"
    results = search_body(
        entry("Category:Example_Artist"),
        entry("File:Example_Artist.jpg"),
        entry(body_match, excerpt='played <span class="searchmatch">Example</span> Artist at Example Festival 2026'),
        entry(KEY),
    )
    built = rig(engine, search=Reply(body=results), pages={page_path(body_match): page(body_match)})
    outcome = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())
    assert [candidate.identity.native_id for candidate in outcome.candidates] == [body_match, KEY]
    assert {"non_tracklist_namespace=Category:Example_Artist", "non_tracklist_namespace=File:Example_Artist.jpg"} <= set(outcome.evidence)
    other, intended = outcome.candidates
    assert {"title_artist=absent", "match_basis=body_or_fuzzy", "search_rank=3"} <= set(other.evidence)
    assert "search_excerpt:played Example Artist at Example Festival 2026" in other.evidence
    assert {"title_artist=present", "title_event=present", "title_date=present"} <= set(intended.evidence)
    assert "match_basis=body_or_fuzzy" not in intended.evidence
    # A candidate is a key to review: no source reference, and its title is a display name, never a fact.
    assert all(candidate.source is None for candidate in outcome.candidates)
    loaded = await built.registry.load(other, LoadBudget(), built.transport.run())
    assert loaded.snapshot is not None
    assert loaded.snapshot.artist.certainty == loaded.snapshot.event.certainty == loaded.snapshot.date.certainty == "unknown"
    assert loaded.snapshot.artist.value is None and loaded.snapshot.identity.native_id == body_match


async def test_404_on_a_slash_key_is_unavailable_never_absent(committed_db: Any) -> None:
    engine, _ = committed_db
    slash_key = "Example_Artist/Example_Sub_Page"
    # The socket would answer 404 to any page request, as the documented route did in the probe.
    built = rig(engine, search=Reply(body=search_body(entry(slash_key))))
    discovered = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())
    (candidate,) = discovered.candidates
    assert candidate.identity.native_id == slash_key and "slash_key_route_unproven" in candidate.evidence
    sent = len(built.socket.requests)
    loaded = await built.registry.load(candidate, LoadBudget(), built.transport.run())
    assert (loaded.status, loaded.code) == (OutcomeStatus.UNAVAILABLE, "slash_key_route_unproven")
    assert loaded.status != OutcomeStatus.ABSENT and loaded.scope == slash_key and loaded.snapshot is None
    assert f"native_key:{slash_key}" in loaded.evidence
    assert {"observed:percent_encoded_slash=http_404_edge_html", "observed:literal_slash=http_404_rest_no_match"} <= set(loaded.evidence)
    # No workaround and no wasted request: nothing more left the socket.
    assert len(built.socket.requests) == sent


async def test_404_on_an_ordinary_key_is_absent_for_that_page_only(committed_db: Any) -> None:
    engine, _ = committed_db
    built = rig(engine)
    loaded = await built.registry.load(
        ProviderCandidate(identity=SourceIdentity(provider_id="mixesdb", native_id=KEY)), LoadBudget(), built.transport.run()
    )
    assert (loaded.status, loaded.code, loaded.scope) == (OutcomeStatus.ABSENT, "http_404", KEY)


async def test_artist_only_fallback_is_not_a_default(committed_db: Any) -> None:
    engine, _ = committed_db
    built = rig(engine)
    outcome = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())
    # Zero results for the narrow query, and still exactly one search: no second, artist-only query follows.
    assert outcome.candidates == () and outcome.status == OutcomeStatus.INCOMPLETE
    assert [query(request)["q"] for request in built.socket.non_robots()] == ["Example Artist Example Festival 2026"]


async def test_no_action_api_call_other_than_bare_help_is_ever_built(committed_db: Any) -> None:
    engine, _ = committed_db
    built = rig(engine, search=Reply(body=search_body(entry(KEY, latest={"id": 101, "timestamp": TIMESTAMP}))), pages={page_path(KEY): page()})

    def assert_rest_only() -> None:
        assert built.socket.requests[0].url == "https://www.mixesdb.com/robots.txt"
        for request in built.socket.requests[1:]:
            assert request.url.startswith(REST) and "api.php" not in request.url and "action=" not in request.url
            # Not even a repeated load of the same page sends a conditional request (declined in this molecule).
            assert {name.casefold() for name, _ in request.headers}.isdisjoint({"if-none-match", "if-modified-since"})

    discovered = await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())
    assert_rest_only()
    for _ in range(2):
        loaded = await built.registry.load(discovered.candidates[0], LoadBudget(), built.transport.run())
        assert loaded.status == OutcomeStatus.FOUND
    assert_rest_only()
    assert len(built.socket.requests) == 4
    assert PROFILE.search is not None and PROFILE.fetch is not None
    assert PROFILE.search.path.startswith("/w/rest.php/v1/") and PROFILE.fetch.path_template.startswith("/w/rest.php/v1/")


async def test_revision_change_between_discovery_and_load_is_retry(committed_db: Any) -> None:
    engine, _ = committed_db
    results = search_body(entry(KEY, latest={"id": 101, "timestamp": TIMESTAMP}))
    built = rig(engine, search=Reply(body=results), pages={page_path(KEY): page(revision=102)})
    (candidate,) = (await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())).candidates
    assert candidate.observed_revision == f"101@{TIMESTAMP}"
    changed = await built.registry.load(candidate, LoadBudget(), built.transport.run())
    # Never a silent switch to the newer snapshot.
    assert (changed.status, changed.code, changed.snapshot) == (OutcomeStatus.RETRY, "revision_changed", None)
    built.socket.pages[page_path(KEY)] = page(revision=101)
    same = await built.registry.load(candidate, LoadBudget(), built.transport.run())
    assert same.status == OutcomeStatus.FOUND and same.snapshot is not None and same.snapshot.revision == f"101@{TIMESTAMP}"


async def test_requests_are_never_closer_than_the_profile_gap(committed_db: Any) -> None:
    engine, _ = committed_db
    built = rig(engine, search=Reply(body=search_body(entry(KEY))), pages={page_path(KEY): page()})
    (candidate,) = (await built.registry.discover("mixesdb", CONTEXT, DiscoveryBudget(), built.transport.run())).candidates
    loaded = await built.registry.load(candidate, LoadBudget(), built.transport.run())
    assert loaded.status == OutcomeStatus.FOUND and loaded.snapshot is not None and loaded.snapshot.url == page_url(KEY)
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(built.socket.sent_at, built.socket.sent_at[1:], strict=False)]
    # Robots asks for 4 s; the profile's own 6 s floor is kept, never loosened to it.
    assert len(gaps) == 2 and min(gaps) >= PROFILE.min_gap_seconds >= 6.0


# ============================================================================ end to end through the real orchestration
async def seed(sessions: async_sessionmaker[AsyncSession]) -> FileRecord:
    async with sessions() as session:
        if await session.get(Agent, "test-fileserver") is None:
            session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
            await session.flush()
        media_id = uuid.uuid4()
        path = f"/synthetic/{media_id}/example-local-file-name.mp3"
        media = FileRecord(
            id=media_id,
            agent_id="test-fileserver",
            original_path=path,
            current_path=path,
            original_filename="example-local-file-name.mp3",
            file_type="mp3",
            sha256_hash="a" * 64,
            file_size=1,
        )
        session.add(media)
        await session.flush()
        session.add(FileMetadata(file_id=media.id, artist="Example Artist", year=2026, duration=3600.0))
        await session.commit()
        return media


async def decide(sessions: async_sessionmaker[AsyncSession], media: FileRecord, observation_id: uuid.UUID, action: str = "select") -> Any:
    async with sessions() as session, session.begin():
        observation = await session.get(ProviderSourceObservation, observation_id)
        assert observation is not None
        current = await session.scalar(select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
        command = RemoteSourceDecision(
            media_id=media.id,
            observation_id=observation_id,
            decision_id=uuid.uuid4(),
            action=action,  # type: ignore[arg-type]
            expected_selection_token=selection_token(current) if current else None,
            expected_revision=observation.revision,
            expected_media_sha256=media.sha256_hash,
        )
        return await decide_remote_candidate(session, command, actor="operator")


async def count(sessions: async_sessionmaker[AsyncSession], model: type, *where: Any) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def test_end_to_end_through_orchestration_with_a_fake_transport(committed_db: Any) -> None:
    engine, sessions = committed_db
    clock = FakeClock()
    slash_key, rejected_key = "Example_Artist/Example_Sub_Page", "2026-02-02_-_Example_Artist_@_Other_Festival"
    at_cap = [entry(slash_key), entry(KEY), entry(rejected_key), *(entry(f"Example_Set_{index:03d}") for index in range(97))]
    socket = ScriptedSocket(clock, search=Reply(body=search_body(*at_cap)), pages={page_path(KEY): page()})
    # The shipped composition: the binding in REMOTE_PROVIDERS, enabled by settings (it ships disabled).
    (binding,) = (binding for binding in REMOTE_PROVIDERS if binding.profile.provider_id == "mixesdb")
    settings = {"mixesdb": RemoteProviderSettings(provider_id="mixesdb", enabled=True, min_interval_seconds=0.1)}
    lookup = RemoteLookup((binding,), settings=settings, engine=engine, sessions=sessions, socket=socket, clock=clock)
    try:
        media = await seed(sessions)

        # A search at the cap is held for review as incomplete, never complete and never absent.
        found = await lookup.lookup(LookupCommand(media_id=media.id, provider_id="mixesdb"))
        assert found.outcome.status == "candidates" and found.outcome.code == "search_at_cap"
        assert found.scope is not None and found.scope.state == "incomplete"
        by_key = {candidate.identity.native_id: candidate for candidate in found.candidates}
        assert len(found.candidates) == 64 and {slash_key, KEY, rejected_key} <= set(by_key)
        assert all("assessment:eligibility:review_required" in candidate.evidence for candidate in found.candidates)
        assert by_key[KEY].url == page_url(KEY)
        assert await count(sessions, ProviderRecordingSelection) == 0

        # A slash key loads as unavailable, with no request sent and nothing recorded as absent.
        sent = len(socket.requests)
        slashed = await lookup.load(LoadCommand(candidate_id=by_key[slash_key].candidate_id))
        assert (slashed.outcome.status, slashed.outcome.code, slashed.observation_id) == ("unavailable", "slash_key_route_unproven", None)
        assert len(socket.requests) == sent
        assert await count(sessions, ProviderSourceObservation, ProviderSourceObservation.status == "absent") == 0

        # An ordinary key loads and imports a PENDING version; nothing is selected until the operator decides.
        loaded = await lookup.load(LoadCommand(candidate_id=by_key[KEY].candidate_id))
        assert loaded.outcome.status == "loaded" and loaded.observation_id is not None and loaded.review_candidate_id is not None
        async with sessions() as session:
            version = await session.get(ProviderSourceObservation, loaded.observation_id)
            assert version is not None and version.status == "found" and version.revision == f"101@{TIMESTAMP}"
            review = await session.get(ProviderRecordingCandidate, loaded.review_candidate_id)
            assert review is not None and review.status == "pending"
        assert await count(sessions, ProviderRecordingSelection) == 0
        selected = await decide(sessions, media, loaded.observation_id)
        assert selected is not None and [track.title for track in selected.tracks] == ["Opening Track", "Closing Track"]

        # A rejected candidate is not proposed again, but it is listed.
        await decide(sessions, media, by_key[rejected_key].observation_id, action="reject")
        again = await lookup.lookup(LookupCommand(media_id=media.id, provider_id="mixesdb"))
        assert rejected_key not in {candidate.identity.native_id for candidate in again.candidates}
        assert [item.identity.native_id for item in again.previously_rejected] == [rejected_key]

        # A 429 stops the provider persistently: the next lookup is refused without contacting the host.
        socket.search = Reply(status=429, body=b"{}")
        limited = await lookup.lookup(LookupCommand(media_id=media.id, provider_id="mixesdb"))
        assert (limited.outcome.status, limited.outcome.code) == ("unavailable", "http_429")
        sent = len(socket.requests)
        stopped = await lookup.lookup(LookupCommand(media_id=media.id, provider_id="mixesdb"))
        assert (stopped.outcome.code, stopped.outcome.stop_code) == ("transport_stopped", "http_429")
        assert len(socket.requests) == sent

        # At the socket: robots first and once, the identifying User-Agent on every request, REST routes only, and
        # never closer than the profile's 6 s floor although settings asked for 0.1 s.
        urls = [request.url for request in socket.requests]
        assert urls[0] == "https://www.mixesdb.com/robots.txt" and urls.count(urls[0]) == 1
        assert all(url.startswith(REST) for url in urls[1:])
        assert all(dict(request.headers)["User-Agent"] == PROFILE.user_agent for request in socket.requests)
        assert [query(request)["q"] for request in socket.requests if "search/page" in request.url] == ["Example Artist 2026"] * 3
        gaps = [(later - earlier).total_seconds() for earlier, later in zip(socket.sent_at, socket.sent_at[1:], strict=False)]
        assert min(gaps) >= 6.0
    finally:
        await lookup.aclose()
