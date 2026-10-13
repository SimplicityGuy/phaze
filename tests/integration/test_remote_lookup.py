"""The operator-triggered remote lookup through its REAL consumers: registry, transport, persistence, Postgres.

Nothing here opens a socket or contacts a host. Every exchange goes through ``CatalogSocket`` (hosts are
``.invalid``), time is ``FakeClock``, and every identifier and track is synthetic. The registry, the transport
(its ledger, pacing lock and status mapping), the persistence functions and the database are the real ones.
The two fake providers are the fictional ``example_catalog`` of docs/design/tracklist-provider-specification.md
and a second, differently shaped catalog that proves a new provider needs no orchestration change.
"""

import ast
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from phaze.config_remote_providers import RemoteProviderSettings
from phaze.database import get_session
from phaze.main import create_app
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.provider_source import (
    ProviderRecordingCandidate,
    ProviderRecordingSelection,
    ProviderSelectionEvent,
    ProviderSourceObject,
    ProviderSourceObservation,
)
from phaze.models.remote_transport import RemoteTransportAttempt, RemoteTransportLedger
from phaze.schemas.remote_lookup import LoadCommand, LookupCommand, RemoteSourceDecision
from phaze.services import remote_lookup as remote_lookup_module
from phaze.services.local_source_import import get_selected_recording_source, selection_token
from phaze.services.provider_persistence import source_availability
from phaze.services.remote_lookup import (
    RemoteLookup,
    build_remote_lookup,
    context_from_metadata,
    decide_remote_candidate,
    effective_profile,
    operator_outcome,
)
from phaze.services.remote_providers import RemoteProviderBinding
from phaze.services.remote_transport import OutboundRequest
from phaze.tracklist_providers.domain import (
    Capability,
    Completeness,
    DiscoveryBudget,
    DiscoveryOutcome,
    Fact,
    LoadBudget,
    LoadOutcome,
    OpaqueCursor,
    OutcomeStatus,
    ProviderCandidate,
    ProviderDescriptor,
    ProviderTrack,
    RecordingContext,
    Snapshot,
)
from phaze.tracklist_providers.host_profile import CredentialSlot, RemoteFetchRoute, RemoteHostProfile, RemoteSearchRoute
from phaze.tracklist_providers.port import AuthorizedEffects


pytestmark = pytest.mark.integration

START = datetime(2026, 1, 1, tzinfo=UTC)
GAP = 6.0
EXAMPLE = "example_catalog"
SECOND = "second_catalog"
SRC = Path(__file__).resolve().parents[2] / "src" / "phaze"


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
    raises: BaseException | None = None


class _Response:
    def __init__(self, reply: Reply) -> None:
        self.status = reply.status
        self.headers = reply.headers
        self._body = reply.body

    async def chunks(self) -> AsyncIterator[bytes]:
        yield self._body


class CatalogSocket:
    """Stands in for the network. Answers from per-path handlers and probes the database at every send.

    The probe records any backend of THIS test database (other than its own) that is inside a transaction
    while a request is in flight: that is a write transaction, row lock or xact advisory lock held across I/O.
    """

    def __init__(self, engine: AsyncEngine, clock: FakeClock, routes: dict[str, Callable[[OutboundRequest], Reply]]) -> None:
        self.engine = engine
        self.clock = clock
        self.routes = routes
        self.requests: list[OutboundRequest] = []
        self.sent_at: list[datetime] = []
        self.held_across_io: list[dict[str, Any]] = []
        self.probes = 0
        # Runs while a request is in flight, as another operator acting concurrently would.
        self.during_send: Callable[[], Awaitable[None]] | None = None

    async def _probe(self) -> None:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT a.pid, a.state, a.xact_start IS NOT NULL AS in_transaction, "
                        "(SELECT count(*) FROM pg_locks l WHERE l.pid = a.pid AND l.locktype = 'advisory') AS advisory "
                        "FROM pg_stat_activity a WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()"
                    )
                )
            ).mappings()
            for row in rows:
                if row["in_transaction"]:
                    self.held_across_io.append(dict(row))
            self.probes += 1

    @asynccontextmanager
    async def open(self, request: OutboundRequest) -> AsyncIterator[_Response]:
        await self._probe()
        if self.during_send is not None:
            await self.during_send()
        self.requests.append(request)
        self.sent_at.append(self.clock.wall())
        parts = urlsplit(request.url)
        handler = next((handler for prefix, handler in self.routes.items() if parts.path.startswith(prefix)), None)
        reply = handler(request) if handler else Reply(status=404)
        if reply.raises is not None:
            raise reply.raises
        yield _Response(reply)


def _json(value: object) -> Reply:
    return Reply(body=json.dumps(value).encode())


# --------------------------------------------------------------------------- the fictional example_catalog (JSON)
EXAMPLE_PROFILE = RemoteHostProfile(
    provider_id=EXAMPLE,
    host="catalog.example.invalid",
    user_agent="phaze-test/1 (+https://example.invalid)",
    search=RemoteSearchRoute(
        fields=frozenset({"artist", "date"}), format="example_search_json", path="/search", term_parameters=(("artist", "q"), ("date", "year"))
    ),
    fetch=RemoteFetchRoute(native_id_pattern=r"set-[0-9]{2}", format="example_set_json", path_template="/sets/{native_id}"),
    min_gap_seconds=GAP,
    robots="not_applicable",
)


class ExampleCatalog:
    """Interprets JSON. Reads only what the registry minted, and reports a failed read as its own outcome."""

    descriptor = ProviderDescriptor(
        id=EXAMPLE,
        display_name="Fictional example catalog",
        capabilities=frozenset({Capability.LOAD, Capability.DISCOVERY, Capability.REMOTE_TRANSPORT}),
    )

    def __init__(self, *, swallow_failures: bool = False, partial: bool = False) -> None:
        self.swallow_failures = swallow_failures
        self.partial = partial

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        del cursor
        read = await effects.read(effects.minted[0], budget)
        if read.status != OutcomeStatus.FOUND:
            if self.swallow_failures:
                # A careless provider: turns a failed read into an empty, incomplete search.
                return DiscoveryOutcome(
                    status="incomplete", code="nothing", scope=context.recording_id, completeness=Completeness(state="incomplete", reason="?")
                )
            return DiscoveryOutcome(status=read.status, code=read.code, scope=context.recording_id, evidence=read.evidence)
        rows = json.loads(read.text or "{}")
        candidates = tuple(
            ProviderCandidate.model_validate(
                {
                    "identity": {"provider_id": EXAMPLE, "native_id": row["id"]},
                    "observed_revision": row["rev"],
                    "display_name": row["title"],
                    "evidence": ("query:artist",),
                }
            )
            for row in rows["sets"]
        )
        return DiscoveryOutcome(
            status="found",
            code="search_exhausted",
            scope=context.recording_id,
            candidates=candidates,
            completeness=Completeness(state="complete", reason="all pages read"),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        read = await effects.read(effects.minted[0], budget)
        if read.status != OutcomeStatus.FOUND:
            return LoadOutcome(status=read.status, code=read.code, scope=candidate.identity.native_id, evidence=read.evidence)
        data = json.loads(read.text or "{}")
        snapshot = Snapshot(
            identity=candidate.identity,
            revision=data["rev"],
            revision_scope="full",
            retrieved_at=read.retrieved_at,
            artist=Fact(value=data["artist"], certainty="known"),
            event=Fact(value=data["event"], certainty="known"),
            date=Fact(value=data["date"], certainty="known"),
            tracks=tuple(ProviderTrack(position=index, artist=artist, title=title) for index, (artist, title) in enumerate(data["tracks"], start=1)),
            source_format="example_set_json",
            parser_version="example-v1",
            completeness=Completeness(state="incomplete" if self.partial else "complete", reason="every row read"),
            rights_evidence=("rights:unknown",),
        )
        return LoadOutcome(status="incomplete" if self.partial else "found", code="loaded", scope=candidate.identity.native_id, snapshot=snapshot)


@dataclass
class ExampleUpstream:
    """The fictional catalog's data. ``revision`` moves to simulate an upstream edit."""

    revision: str = "rev-a"
    fetch_revision: str | None = None
    sets: tuple[str, ...] = ("set-01",)
    search_reply: Reply | None = None
    fetch_reply: Reply | None = None
    tracks: tuple[tuple[str | None, str | None], ...] = (("Example Artist", "Opening"), (None, None))

    def routes(self) -> dict[str, Callable[[OutboundRequest], Reply]]:
        def search(request: OutboundRequest) -> Reply:
            assert "q" in parse_qs(urlsplit(request.url).query)
            if self.search_reply is not None:
                return self.search_reply
            return _json({"sets": [{"id": native, "rev": self.revision, "title": f"Example Event ({native})"} for native in self.sets]})

        def fetch(request: OutboundRequest) -> Reply:
            del request
            if self.fetch_reply is not None:
                return self.fetch_reply
            return _json(
                {
                    "rev": self.fetch_revision or self.revision,
                    "artist": "Example Artist",
                    "event": "Example Event",
                    "date": "2024-06-15",
                    "tracks": [list(track) for track in self.tracks],
                }
            )

        return {"/search": search, "/sets/": fetch}


# --------------------------------------------------------------------------- a second, differently shaped catalog
SECOND_PROFILE = RemoteHostProfile(
    provider_id=SECOND,
    host="second.example.invalid",
    user_agent="phaze-test/1 (+https://example.invalid)",
    search=RemoteSearchRoute(fields=frozenset({"artist"}), format="second_lines", path="/find", term_parameters=(("artist", "who"),)),
    fetch=RemoteFetchRoute(native_id_pattern=r"[0-9]{4}", format="second_text", path_template="/items/{native_id}"),
    min_gap_seconds=10.0,
    robots="not_applicable",
)


class SecondCatalog:
    """Interprets pipe-separated lines: a different wire shape, the same contract."""

    descriptor = ProviderDescriptor(
        id=SECOND,
        display_name="Fictional second catalog",
        capabilities=frozenset({Capability.LOAD, Capability.DISCOVERY, Capability.REMOTE_TRANSPORT}),
    )

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        del cursor
        read = await effects.read(effects.minted[0], budget)
        if read.status != OutcomeStatus.FOUND:
            return DiscoveryOutcome(status=read.status, code=read.code, scope=context.recording_id)
        candidates = []
        for line in (read.text or "").splitlines():
            native, revision = line.split("|")
            candidates.append(ProviderCandidate(identity={"provider_id": SECOND, "native_id": native}, observed_revision=revision))
        return DiscoveryOutcome(
            status="incomplete",
            code="first_page",
            scope=context.recording_id,
            candidates=tuple(candidates),
            completeness=Completeness(state="incomplete", reason="one page only"),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        read = await effects.read(effects.minted[0], budget)
        if read.status != OutcomeStatus.FOUND:
            return LoadOutcome(status=read.status, code=read.code, scope=candidate.identity.native_id)
        header, *rows = (read.text or "").splitlines()
        snapshot = Snapshot(
            identity=candidate.identity,
            revision=header,
            retrieved_at=read.retrieved_at,
            artist=Fact(value="Example Artist", certainty="inferred"),
            tracks=tuple(ProviderTrack(position=index, title=row) for index, row in enumerate(rows, start=1)),
            source_format="second_text",
            parser_version="second-v1",
            completeness=Completeness(state="complete", reason="end marker"),
        )
        return LoadOutcome(status="found", code="ok", scope=candidate.identity.native_id, snapshot=snapshot)


def second_routes() -> dict[str, Callable[[OutboundRequest], Reply]]:
    return {
        "/find": lambda _request: Reply(body=b"0001|r1\n0002|r1", headers={"content-type": "text/plain"}),
        "/items/": lambda _request: Reply(body=b"r1\nFirst\nSecond\nThird", headers={"content-type": "text/plain"}),
    }


# --------------------------------------------------------------------------- composition and seeding
def enabled(*provider_ids: str, **overrides: Any) -> dict[str, RemoteProviderSettings]:
    return {provider_id: RemoteProviderSettings(provider_id=provider_id, enabled=True, **overrides) for provider_id in provider_ids}


@dataclass
class Harness:
    lookup: RemoteLookup
    socket: CatalogSocket
    clock: FakeClock
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine


@asynccontextmanager
async def harness(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    routes: dict[str, Callable[[OutboundRequest], Reply]],
    *,
    bindings: tuple[RemoteProviderBinding, ...] = (RemoteProviderBinding(ExampleCatalog(), EXAMPLE_PROFILE),),
    settings: dict[str, RemoteProviderSettings] | None = None,
) -> AsyncIterator[Harness]:
    engine, sessions = committed_db
    clock = FakeClock()
    socket = CatalogSocket(engine, clock, routes)
    lookup = RemoteLookup(
        bindings,
        settings=enabled(*(binding.profile.provider_id for binding in bindings)) if settings is None else settings,
        engine=engine,
        sessions=sessions,
        socket=socket,
        clock=clock,
    )
    try:
        yield Harness(lookup, socket, clock, sessions, engine)
    finally:
        await lookup.aclose()


async def seed(sessions: async_sessionmaker[AsyncSession]) -> FileRecord:
    async with sessions() as session:
        if await session.get(Agent, "test-fileserver") is None:
            session.add(Agent(id="test-fileserver", name="test-fileserver", kind="fileserver", scan_roots=[]))
            await session.flush()
        media_id = uuid.uuid4()
        media = FileRecord(
            id=media_id,
            agent_id="test-fileserver",
            original_path=f"/synthetic/{media_id}/recording.mp3",
            current_path=f"/synthetic/{media_id}/recording.mp3",
            original_filename="recording.mp3",
            file_type="mp3",
            sha256_hash="a" * 64,
            file_size=1,
        )
        session.add(media)
        await session.flush()
        session.add(FileMetadata(file_id=media.id, artist="Example Artist", year=2024, duration=3600.0))
        await session.commit()
        return media


async def count(sessions: async_sessionmaker[AsyncSession], model: type, *where: Any) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def decide(
    sessions: async_sessionmaker[AsyncSession], media: FileRecord, observation_id: uuid.UUID, action: str = "select", reason: str | None = None
) -> Any:
    async with sessions() as session, session.begin():
        observation = await session.get(ProviderSourceObservation, observation_id)
        assert observation is not None
        current = await session.scalar(select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
        command = RemoteSourceDecision(
            media_id=media.id,
            observation_id=observation_id,
            decision_id=uuid.uuid4(),
            action=action,
            reason=reason,
            expected_selection_token=selection_token(current) if current else None,
            expected_revision=observation.revision,
            expected_media_sha256=media.sha256_hash,
        )
        return await decide_remote_candidate(session, command, actor="operator")


# =============================================================================================== acceptance
async def test_fake_provider_end_to_end_through_the_real_orchestration(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert found.outcome.status == "candidates"
        assert found.scope is not None and found.scope.state == "complete"
        [candidate] = found.candidates
        assert candidate.identity.native_id == "set-01"
        assert "assessment:eligibility:review_required" in candidate.evidence
        assert any(item.startswith("assessment:band:") for item in candidate.evidence)
        assert [urlsplit(request.url).netloc for request in h.socket.requests] == ["catalog.example.invalid"]
        async with h.sessions() as session:
            row = await session.get(ProviderRecordingCandidate, candidate.candidate_id)
            assert row is not None and row.status == "pending"
            discovered = await session.get(ProviderSourceObservation, candidate.observation_id)
            assert discovered is not None and discovered.status == "discovered"
            source = await session.get(ProviderSourceObject, discovered.object_id)
            assert source is not None and source.channel == "remote" and source.source_file_id is None
        assert await count(h.sessions, ProviderRecordingSelection) == 0, "discovery selects nothing"

        loaded = await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert loaded.outcome.status == "loaded", loaded.outcome
        assert loaded.observation_id is not None and loaded.review_candidate_id is not None
        async with h.sessions() as session:
            version = await session.get(ProviderSourceObservation, loaded.observation_id)
            assert version is not None and version.status == "found" and version.revision == "rev-a"
            assert version.parent_id == candidate.observation_id
            review = await session.get(ProviderRecordingCandidate, loaded.review_candidate_id)
            assert review is not None and review.status == "pending"
            assert "assessment:eligibility:review_required" in review.evidence
        assert await count(h.sessions, ProviderRecordingSelection) == 0, "importing a version selects nothing"

        selected = await decide(h.sessions, media, loaded.observation_id)
        assert selected is not None and selected.observation_id == loaded.observation_id
        assert selected.availability == "current"
        assert [track.title for track in selected.tracks] == ["Opening", None]
        assert await count(h.sessions, ProviderSelectionEvent, ProviderSelectionEvent.action == "select") == 1


@pytest.mark.parametrize("provider_id", [EXAMPLE, SECOND])
async def test_no_provider_specific_branch_exists(committed_db: Any, provider_id: str) -> None:
    upstream = ExampleUpstream()
    bindings = (RemoteProviderBinding(ExampleCatalog(), EXAMPLE_PROFILE), RemoteProviderBinding(SecondCatalog(), SECOND_PROFILE))
    async with harness(committed_db, {**upstream.routes(), **second_routes()}, bindings=bindings) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=provider_id))
        assert found.outcome.status == "candidates"
        assert {candidate.identity.provider_id for candidate in found.candidates} == {provider_id}
        loaded = await h.lookup.load(LoadCommand(candidate_id=found.candidates[0].candidate_id))
        assert loaded.outcome.status == "loaded"
        assert loaded.observation_id is not None
        assert (await decide(h.sessions, media, loaded.observation_id)).availability == "current"
        assert {urlsplit(request.url).netloc for request in h.socket.requests} == {
            EXAMPLE_PROFILE.host if provider_id == EXAMPLE else SECOND_PROFILE.host
        }

    # And the code says so: no provider id appears as a literal in the orchestration or its router.
    for path in (SRC / "services" / "remote_lookup.py", SRC / "routers" / "remote_lookups.py"):
        literals = {node.value for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        assert not literals & {EXAMPLE, SECOND, "local", "trackid", "mixesdb", "track_id", "mixes_db"}, path


async def test_nothing_is_awaited_while_a_write_lock_is_held(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        await h.lookup.load(LoadCommand(candidate_id=found.candidates[0].candidate_id))
        assert h.socket.probes == 2, "the probe ran at the search send and at the fetch send"
        assert h.socket.held_across_io == [], f"a transaction was open on this database while a request was in flight: {h.socket.held_across_io}"


async def test_changed_revision_between_discovery_and_load_is_retry_not_a_silent_switch(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        upstream.fetch_revision = "rev-b"  # edited upstream after discovery
        loaded = await h.lookup.load(LoadCommand(candidate_id=found.candidates[0].candidate_id))
        assert loaded.outcome.status == "changed_source"
        assert loaded.outcome.code == "revision_changed"
        assert loaded.observation_id is None and loaded.review_candidate_id is None
        assert await count(h.sessions, ProviderSourceObservation, ProviderSourceObservation.status == "found") == 0, "neither revision was imported"
        assert await count(h.sessions, ProviderRecordingCandidate) == 1


async def test_manual_or_approved_selection_is_never_overwritten_by_a_newer_arrival(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        first = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        loaded_a = await h.lookup.load(LoadCommand(candidate_id=first.candidates[0].candidate_id))
        assert loaded_a.observation_id is not None
        await decide(h.sessions, media, loaded_a.observation_id)
        async with h.sessions() as session:
            before = await session.scalar(select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
            assert before is not None
            pinned = (before.observation_id, before.actor, before.selected_at, before.selection_token, before.target_mapping)

        upstream.revision = "rev-b"  # a newer revision arrives
        second = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        [newer] = second.candidates
        loaded_b = await h.lookup.load(LoadCommand(candidate_id=newer.candidate_id))
        assert loaded_b.outcome.status == "loaded" and loaded_b.observation_id not in {None, loaded_a.observation_id}

        async with h.sessions() as session:
            after = await session.scalar(select(ProviderRecordingSelection).where(ProviderRecordingSelection.media_id == media.id))
            assert after is not None
            assert (after.observation_id, after.actor, after.selected_at, after.selection_token, after.target_mapping) == pinned
            review = await session.get(ProviderRecordingCandidate, loaded_b.review_candidate_id)
            assert review is not None and review.status == "pending"
            selected = await get_selected_recording_source(session, media.id)
            assert selected is not None and selected.observation_id == loaded_a.observation_id and selected.snapshot.revision == "rev-a"


async def test_rejected_candidate_is_not_reproposed(committed_db: Any) -> None:
    upstream = ExampleUpstream(sets=("set-01", "set-02"))
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        first = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        rejected = next(candidate for candidate in first.candidates if candidate.identity.native_id == "set-01")
        await decide(h.sessions, media, rejected.observation_id, action="reject")

        upstream.revision = "rev-b"  # even a new revision of the rejected object stays rejected
        again = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert [candidate.identity.native_id for candidate in again.candidates] == ["set-02"]
        [shown] = again.previously_rejected
        assert shown.identity.native_id == "set-01" and shown.rejected_at is not None, "a suppressed candidate is listed, never dropped"
        with pytest.raises(ValueError, match="rejected"):
            await h.lookup.load(LoadCommand(candidate_id=rejected.candidate_id))

        upstream.sets = ("set-01",)
        only_rejected = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert only_rejected.candidates == ()
        assert only_rejected.outcome.status == "no_candidates_in_scope"
        assert "not proposed again" in only_rejected.outcome.message
        assert [item.identity.native_id for item in only_rejected.previously_rejected] == ["set-01"]


async def test_a_rejection_is_reversed_only_by_an_explicit_reopen_with_a_reason(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        first = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        [candidate] = first.candidates
        with pytest.raises(ValueError, match="Only a rejected candidate can be reopened"):
            await decide(h.sessions, media, candidate.observation_id, action="reopen", reason="synthetic")
        version = await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert version.observation_id is not None
        # Two rejected rows of ONE object: the loaded version and the discovered candidate.
        await decide(h.sessions, media, version.observation_id, action="reject")
        await decide(h.sessions, media, candidate.observation_id, action="reject")
        assert (await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))).candidates == ()
        with pytest.raises(ValidationError, match="requires a reason"):
            await decide(h.sessions, media, candidate.observation_id, action="reopen")

        await decide(h.sessions, media, candidate.observation_id, action="reopen", reason="a later edit added the missing tracks")
        async with h.sessions() as session:
            event = await session.scalar(select(ProviderSelectionEvent).where(ProviderSelectionEvent.action == "reopen"))
            assert event is not None and event.actor == "operator"
            assert event.decision_payload is not None and event.decision_payload["reason"] == "a later edit added the missing tracks"
        reopened = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert [item.identity.native_id for item in reopened.candidates] == ["set-01"] and reopened.previously_rejected == ()
        loaded = await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert loaded.outcome.status == "loaded", "reopening one row reopens the object, so its other rejected row no longer blocks it"
        assert await count(h.sessions, ProviderRecordingCandidate, ProviderRecordingCandidate.status == "rejected") == 0


async def test_two_concurrent_lookups_for_one_recording_spend_once_and_write_one_set_of_candidates(committed_db: Any) -> None:
    upstream = ExampleUpstream(sets=("set-01", "set-02"))
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        command = LookupCommand(media_id=media.id, provider_id=EXAMPLE)
        first, second = await asyncio.gather(h.lookup.lookup(command), h.lookup.lookup(command))
        assert first == second
        assert len(h.socket.requests) == 1, "the duplicate awaited the first lookup instead of spending budget again"
        assert await count(h.sessions, RemoteTransportAttempt) == 1
        assert await count(h.sessions, ProviderRecordingCandidate) == 2
        assert await count(h.sessions, ProviderSourceObservation) == 2

        again = await h.lookup.lookup(command)  # sequential repeat: spends one request, writes nothing new
        assert {item.candidate_id for item in again.candidates} == {item.candidate_id for item in first.candidates}
        assert await count(h.sessions, ProviderRecordingCandidate) == 2
        assert await count(h.sessions, ProviderSourceObservation) == 2

        load = LoadCommand(candidate_id=first.candidates[0].candidate_id)
        one, two = await asyncio.gather(h.lookup.load(load), h.lookup.load(load))
        assert one == two and len(h.socket.requests) == 3


async def test_disabled_provider_lookup_returns_unavailable_without_any_effect(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes(), settings={}) as h:
        media = await seed(h.sessions)
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert result.outcome.status == "unavailable" and result.outcome.code == "disabled_provider"
        assert result.candidates == ()
        assert h.socket.requests == [] and h.socket.probes == 0
        for model in (RemoteTransportLedger, RemoteTransportAttempt, ProviderSourceObject, ProviderSourceObservation, ProviderRecordingCandidate):
            assert await count(h.sessions, model) == 0, model
        [status] = await h.lookup.provider_status()
        assert status.provider_id == EXAMPLE and status.enabled is False and status.stopped is False


async def test_remote_object_availability_semantics_are_defined_and_tested(committed_db: Any) -> None:
    """A remote object has no inventory: availability is what core STORED about it, never a live probe.

    current  = a stored snapshot with no later stored 'absent' read of the same object;
    transient outcomes (retry, pacing, budget, stop) never change it;
    absent   = the host later reported THAT object gone (404): selection is refused, the stored copy stays;
    discovered = a candidate not yet loaded, never selectable.
    """
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        candidate = found.candidates[0]
        loaded = await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert loaded.observation_id is not None

        async def availability(observation_id: uuid.UUID) -> str:
            async with h.sessions() as session:
                observation = await session.get(ProviderSourceObservation, observation_id)
                assert observation is not None
                return await source_availability(session, observation, media.id)

        assert await availability(candidate.observation_id) == "discovered"
        assert await availability(loaded.observation_id) == "current"

        upstream.fetch_reply = Reply(status=503)
        transient = await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert transient.outcome.status == "retry"
        assert await availability(loaded.observation_id) == "current", "a transient failure says nothing about the object"

        upstream.fetch_reply = Reply(status=404)
        gone = await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert gone.outcome.status == "absent" and gone.outcome.code == "http_404"
        assert await availability(loaded.observation_id) == "absent"
        with pytest.raises(ValueError, match="not currently available"):
            await decide(h.sessions, media, loaded.observation_id)
        assert await count(h.sessions, ProviderSourceObservation, ProviderSourceObservation.status == "found") == 1, "the stored copy is retained"


# =============================================================================================== outcomes and stops
TRANSPORT_CODES = (
    "run_budget_exhausted",
    "durable_budget_exhausted",
    "paced",
    "transport_stopped",
    "clock_discontinuity",
    "robots_disallow",
    "redirect_not_followed",
    "http_304_not_requested",
    "response_too_large",
    "undecodable_response",
    "challenge",
    "ledger_corrupt",
    "timeout",
    "transport_error",
    "http_400",
    "http_401",
    "http_403",
    "http_404",
    "http_408",
    "http_429",
    "http_500",
    "http_503",
    "robots_timeout",
    "robots_http_500",
)


@pytest.mark.parametrize("code", TRANSPORT_CODES)
def test_every_transport_outcome_maps_to_an_operator_result(code: str) -> None:
    status = (
        OutcomeStatus.ABSENT
        if code == "http_404"
        else OutcomeStatus.RETRY
        if code in {"paced", "durable_budget_exhausted"}
        else OutcomeStatus.UNAVAILABLE
    )
    outcome = operator_outcome(status, code, evidence=("retry_after_seconds=42", "stop=http_403"))
    assert outcome.status not in {"candidates", "no_candidates_in_scope", "loaded"}, "a transport outcome is never 'nothing found'"
    assert f"({code})" not in outcome.message, f"{code} fell through to the generic message"
    if code in {"paced", "durable_budget_exhausted"}:
        assert outcome.status == "retry" and outcome.retry_after_seconds == 42 and "42 s" in outcome.message
    if code == "transport_stopped":
        assert outcome.stop_code == "http_403" and "http_403" in outcome.message
    if code == "http_404":
        assert outcome.status == "absent" and "this one object only" in outcome.message


def test_both_forms_of_the_registry_refusal_are_the_same_refusal() -> None:
    swallowed = operator_outcome(OutcomeStatus.CONTRACT_ERROR, "unauthorized_read")
    propagated = operator_outcome(OutcomeStatus.CONTRACT_ERROR, "invalid_output", detail="unauthorized_read")
    assert swallowed == propagated
    assert swallowed.status == "refused"
    assert operator_outcome(OutcomeStatus.CONTRACT_ERROR, "invalid_output", detail="bad json").status == "contract_error"


@pytest.mark.parametrize(
    ("reply", "status", "code"),
    [
        (Reply(status=429), "unavailable", "http_429"),
        (Reply(status=500), "retry", "http_500"),
        (Reply(status=404), "absent", "http_404"),
        (Reply(status=302, headers={"location": "https://elsewhere.invalid/"}), "unavailable", "redirect_not_followed"),
        (Reply(status=304), "unavailable", "http_304_not_requested"),
        (Reply(body=b"x" * 300_000), "unavailable", "response_too_large"),
        (Reply(body=b"\xff\xfe\xfa", headers={"content-type": "text/plain; charset=utf-8"}), "unsupported", "undecodable_response"),
        (Reply(status=503, headers={"cf-mitigated": "challenge"}), "unavailable", "challenge"),
        (Reply(raises=TimeoutError("timed out")), "retry", "timeout"),
    ],
)
async def test_a_failed_read_is_never_reported_as_an_empty_search(committed_db: Any, reply: Reply, status: str, code: str) -> None:
    upstream = ExampleUpstream(search_reply=reply)
    bindings = (RemoteProviderBinding(ExampleCatalog(swallow_failures=True), EXAMPLE_PROFILE),)
    async with harness(committed_db, upstream.routes(), bindings=bindings) as h:
        media = await seed(h.sessions)
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert (result.outcome.status, result.outcome.code) == (status, code)
        assert result.candidates == ()


async def test_stopped_provider_lookup_returns_transport_stopped_and_an_operator_clears_it(committed_db: Any) -> None:
    upstream = ExampleUpstream(search_reply=Reply(status=403))
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        first = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert (first.outcome.status, first.outcome.code) == ("unavailable", "http_403")

        upstream.search_reply = None
        stopped = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert (stopped.outcome.status, stopped.outcome.code, stopped.outcome.stop_code) == ("unavailable", "transport_stopped", "http_403")
        assert stopped.candidates == () and stopped.outcome.status != "no_candidates_in_scope"
        assert len(h.socket.requests) == 1, "a stopped provider is not contacted"
        [status] = await h.lookup.provider_status()
        assert status.stopped and status.stop_code == "http_403"

        app = create_app()
        app.state.remote_lookup = h.lookup
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.get("/api/remote-lookups/providers")
            assert listed.status_code == 200 and listed.json()[0]["stop_code"] == "http_403"
            missing_reason = await client.post(f"/api/remote-lookups/providers/{EXAMPLE}/clear-stop", json={})
            assert missing_reason.status_code == 422
            unknown = await client.post("/api/remote-lookups/providers/nobody/clear-stop", json={"reason": "synthetic"})
            assert unknown.status_code == 404
            cleared = await client.post(f"/api/remote-lookups/providers/{EXAMPLE}/clear-stop", json={"reason": "403 investigated"})
            assert cleared.status_code == 200
            body = cleared.json()
            assert body["stopped"] is False and body["cleared_by"] == "admin" and body["clear_reason"] == "403 investigated"

        resumed = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert resumed.outcome.status == "candidates"


async def test_lookup_load_and_decision_are_reachable_through_the_router(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        app = create_app()
        app.state.remote_lookup = h.lookup

        async def override() -> AsyncIterator[AsyncSession]:
            async with h.sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            found = await client.post("/api/remote-lookups/lookup", json={"media_id": str(media.id), "provider_id": EXAMPLE})
            assert found.status_code == 200 and found.json()["outcome"]["status"] == "candidates"
            candidate_id = found.json()["candidates"][0]["candidate_id"]
            loaded = await client.post("/api/remote-lookups/load", json={"candidate_id": candidate_id})
            assert loaded.status_code == 200 and loaded.json()["outcome"]["status"] == "loaded"
            observation_id = loaded.json()["observation_id"]
            unknown = await client.post("/api/remote-lookups/load", json={"candidate_id": str(uuid.uuid4())})
            assert unknown.status_code == 409
            decision = {
                "media_id": str(media.id),
                "observation_id": observation_id,
                "decision_id": str(uuid.uuid4()),
                "expected_revision": "rev-a",
                "expected_media_sha256": media.sha256_hash,
            }
            selected = await client.post("/api/remote-lookups/decision", json=decision)
            assert selected.status_code == 200 and selected.json()["availability"] == "current"
            stale = await client.post("/api/remote-lookups/decision", json={**decision, "decision_id": str(uuid.uuid4())})
            assert stale.status_code == 409, "a decision against an outdated selection token is refused"
            missing = await client.post("/api/remote-lookups/lookup", json={"media_id": str(uuid.uuid4()), "provider_id": EXAMPLE})
            assert missing.status_code == 409


# =============================================================================================== settings and composition
def test_settings_override_never_lowers_the_pacing_floor() -> None:
    def settings(**values: Any) -> RemoteProviderSettings:
        return RemoteProviderSettings(provider_id=EXAMPLE, enabled=True, **values)

    assert effective_profile(EXAMPLE_PROFILE, settings(min_interval_seconds=0.1)).min_gap_seconds == GAP
    assert effective_profile(EXAMPLE_PROFILE, settings(min_interval_seconds=30.0)).min_gap_seconds == 30.0
    assert effective_profile(EXAMPLE_PROFILE, settings(max_requests_per_run=1000)).per_run_budget == EXAMPLE_PROFILE.per_run_budget
    assert effective_profile(EXAMPLE_PROFILE, settings(max_requests_per_run=2)).per_run_budget == 2
    assert effective_profile(EXAMPLE_PROFILE, None) == EXAMPLE_PROFILE


async def test_an_override_of_a_tenth_of_a_second_still_waits_the_profile_gap(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes(), settings=enabled(EXAMPLE, min_interval_seconds=0.1)) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        await h.lookup.load(LoadCommand(candidate_id=found.candidates[0].candidate_id))
        first, second = h.socket.sent_at
        assert (second - first).total_seconds() >= GAP


def test_context_uses_only_bounded_tag_facts_and_never_guesses_an_event() -> None:
    media_id = uuid.uuid4()
    metadata = FileMetadata(file_id=media_id, artist="  Example Artist ", album="Example Event 2024", year=2024, duration=3600.0)
    context = context_from_metadata(media_id, metadata)
    assert context.recording_id == str(media_id)
    assert (context.artist.value, context.artist.certainty) == ("Example Artist", "known")
    assert (context.date.value, context.date.certainty) == ("2024", "inferred")
    assert context.event == Fact(), "an album tag is not an event"
    assert context.duration_seconds == 3600.0
    assert context_from_metadata(media_id, None) == RecordingContext(recording_id=str(media_id))
    assert context_from_metadata(media_id, FileMetadata(file_id=media_id, artist="   ", year=0)).artist == Fact()
    failed = FileMetadata(file_id=media_id, artist="Example Artist", failed_at=START)
    assert context_from_metadata(media_id, failed).artist == Fact()


async def test_the_composition_root_builds_once_and_closes_cleanly() -> None:
    sessions = async_sessionmaker(class_=AsyncSession)
    lookup = build_remote_lookup("postgresql+asyncpg://synthetic:synthetic@localhost:5433/synthetic_test", sessions)
    assert remote_lookup_module.REMOTE_PROVIDERS == ()
    assert await lookup.provider_status() == ()
    await lookup.aclose()


# =============================================================================================== decisions and edges
class RefusingCatalog(ExampleCatalog):
    """Answers without reading: its own outcome, with no failed read behind it."""

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        del budget, cursor, effects
        return DiscoveryOutcome(status="unsupported", code="format_unknown", scope=context.recording_id)

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        del budget, effects
        return LoadOutcome(status="unsupported", code="format_unknown", scope=candidate.identity.native_id)


async def _decision(sessions: async_sessionmaker[AsyncSession], command: RemoteSourceDecision, actor: str = "operator") -> Any:
    async with sessions() as session, session.begin():
        return await decide_remote_candidate(session, command, actor=actor)


async def test_decisions_are_explicit_compare_and_set_and_refuse_what_cannot_be_selected(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        discovered = found.candidates[0]
        loaded = await h.lookup.load(LoadCommand(candidate_id=discovered.candidate_id))
        assert loaded.observation_id is not None

        def command(observation_id: uuid.UUID, **values: Any) -> RemoteSourceDecision:
            base: dict[str, Any] = {
                "media_id": media.id,
                "observation_id": observation_id,
                "decision_id": uuid.uuid4(),
                "expected_revision": "rev-a",
                "expected_media_sha256": media.sha256_hash,
            }
            return RemoteSourceDecision(**{**base, **values})

        refusals = [
            (command(loaded.observation_id), "", "bounded actor"),
            (command(uuid.uuid4()), "operator", "retained remote source"),
            (command(loaded.observation_id, media_id=uuid.uuid4()), "operator", "Recording is unavailable"),
            (command(loaded.observation_id, expected_media_sha256="b" * 64), "operator", "Recording changed"),
            (command(loaded.observation_id, expected_revision="rev-z"), "operator", "revision changed"),
            (command(discovered.observation_id), "operator", "complete loaded snapshot"),
        ]
        for refused, actor, message in refusals:
            with pytest.raises(ValueError, match=message):
                await _decision(h.sessions, refused, actor)

        select = command(loaded.observation_id)
        first = await _decision(h.sessions, select)
        assert first is not None and first.observation_id == loaded.observation_id
        replay = await _decision(h.sessions, select)
        assert replay is not None and replay.selection_token == first.selection_token, "an identical replay is idempotent"
        with pytest.raises(ValueError, match="different intent"):
            await _decision(h.sessions, select.model_copy(update={"accept_conflict": True}))
        with pytest.raises(ValueError, match="Selected source changed"):
            await _decision(h.sessions, command(loaded.observation_id))
        with pytest.raises(ValueError, match="requires choosing a replacement"):
            await _decision(h.sessions, command(loaded.observation_id, action="reject", expected_selection_token=first.selection_token))

        # The same revision with different content is a conflict that must be acknowledged explicitly.
        upstream.tracks = (("Example Artist", "Different"),)
        conflicting = await h.lookup.load(LoadCommand(candidate_id=discovered.candidate_id))
        assert conflicting.observation_id is not None
        async with h.sessions() as session:
            row = await session.get(ProviderSourceObservation, conflicting.observation_id)
            assert row is not None and row.conflict_with_id is not None
        token = first.selection_token
        with pytest.raises(ValueError, match="explicit acknowledgement"):
            await _decision(h.sessions, command(conflicting.observation_id, expected_selection_token=token))
        acknowledged = await _decision(h.sessions, command(conflicting.observation_id, expected_selection_token=token, accept_conflict=True))
        assert acknowledged is not None and acknowledged.observation_id == conflicting.observation_id

        # A rejected loaded candidate can no longer be selected, and replaying the rejection is idempotent.
        reject = command(loaded.observation_id, action="reject", expected_selection_token=acknowledged.selection_token)
        await _decision(h.sessions, reject)
        assert await _decision(h.sessions, reject) is not None
        with pytest.raises(ValueError, match="rejected candidate cannot be selected"):
            await _decision(
                h.sessions, command(conflicting.observation_id, expected_selection_token=acknowledged.selection_token, accept_conflict=True)
            )
        with pytest.raises(ValueError, match="no longer current"):
            await _decision(h.sessions, select)
        with pytest.raises(ValueError, match="not a candidate"):
            other = await seed(h.sessions)
            await _decision(h.sessions, command(loaded.observation_id, media_id=other.id))


async def test_a_partial_snapshot_is_held_for_review_but_never_selectable(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    bindings = (RemoteProviderBinding(ExampleCatalog(partial=True), EXAMPLE_PROFILE),)
    async with harness(committed_db, upstream.routes(), bindings=bindings) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        loaded = await h.lookup.load(LoadCommand(candidate_id=found.candidates[0].candidate_id))
        assert loaded.outcome.status == "loaded" and "partial snapshot" in loaded.outcome.message
        assert loaded.observation_id is not None
        with pytest.raises(ValueError, match="complete loaded snapshot"):
            await decide(h.sessions, media, loaded.observation_id)


async def test_a_rejection_made_while_a_load_is_in_flight_wins(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        candidate = found.candidates[0]

        async def reject_concurrently() -> None:
            h.socket.during_send = None
            await decide(h.sessions, media, candidate.observation_id, action="reject")

        h.socket.during_send = reject_concurrently
        with pytest.raises(ValueError, match="while it loaded"):
            await h.lookup.load(LoadCommand(candidate_id=candidate.candidate_id))
        assert await count(h.sessions, ProviderSourceObservation, ProviderSourceObservation.status == "found") == 0


async def test_provider_outcomes_without_a_failed_read_and_unregistered_or_disabled_providers(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes(), bindings=(RemoteProviderBinding(RefusingCatalog(), EXAMPLE_PROFILE),)) as h:
        media = await seed(h.sessions)
        unsupported = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert (unsupported.outcome.status, unsupported.outcome.code) == ("unsupported", "format_unknown")
        unknown = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id="nobody"))
        assert (unknown.outcome.status, unknown.outcome.code) == ("contract_error", "unknown_provider")
        assert "example_catalog" in repr(h.lookup)

    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        candidate_id = found.candidates[0].candidate_id
    async with harness(committed_db, upstream.routes(), bindings=(RemoteProviderBinding(RefusingCatalog(), EXAMPLE_PROFILE),)) as h:
        refused = await h.lookup.load(LoadCommand(candidate_id=candidate_id))
        assert (refused.outcome.status, refused.outcome.code) == ("unsupported", "format_unknown")
    async with harness(committed_db, upstream.routes(), settings={}) as h:
        disabled = await h.lookup.load(LoadCommand(candidate_id=candidate_id))
        assert (disabled.outcome.status, disabled.outcome.code) == ("unavailable", "disabled_provider")
        assert h.socket.requests == []


async def test_an_incomplete_empty_search_is_never_absence(committed_db: Any) -> None:
    async with harness(
        committed_db,
        {"/find": lambda _request: Reply(body=b"", headers={"content-type": "text/plain"})},
        bindings=(RemoteProviderBinding(SecondCatalog(), SECOND_PROFILE),),
    ) as h:
        media = await seed(h.sessions)
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=SECOND))
        assert result.outcome.status == "no_candidates_in_scope"
        assert "incomplete" in result.outcome.message and "not evidence that no tracklist exists" in result.outcome.message


async def test_a_credential_reaches_only_its_own_slot_from_settings(committed_db: Any) -> None:
    profile = EXAMPLE_PROFILE.model_copy(update={"credential": CredentialSlot(header="X-Api-Key")})
    binding = RemoteProviderBinding(ExampleCatalog(), profile, credential_name="api_key")
    assert binding.spec.credential_names == ("api_key",)
    settings = enabled(EXAMPLE, credentials={"api_key": SecretStr("synthetic-secret-value")})
    async with harness(committed_db, ExampleUpstream().routes(), bindings=(binding,), settings=settings) as h:
        media = await seed(h.sessions)
        await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        [request] = h.socket.requests
        assert request.credential is not None and request.credential[0] == "X-Api-Key"
        assert request.credential[1].get_secret_value() == "synthetic-secret-value"
        assert "synthetic-secret-value" not in repr(h.lookup)


def test_unmapped_codes_fall_back_to_their_status_and_bad_evidence_is_ignored() -> None:
    outcome = operator_outcome(OutcomeStatus.UNAVAILABLE, "provider_specific", evidence=("retry_after_seconds=soon",))
    assert outcome.status == "unavailable" and outcome.retry_after_seconds is None
    assert "(provider_specific)" in outcome.message


async def test_a_recording_with_no_usable_fact_sends_nothing_and_says_so(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        async with h.sessions() as session, session.begin():
            metadata = await session.scalar(select(FileMetadata).where(FileMetadata.file_id == media.id))
            assert metadata is not None
            metadata.artist, metadata.year = None, None
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert (result.outcome.status, result.outcome.code) == ("unavailable", "no_usable_hint")
        assert "not evidence that no tracklist exists" in result.outcome.message
        assert h.socket.requests == []


async def test_discovered_and_remote_rows_never_crash_or_misrender_an_observation_reader(committed_db: Any) -> None:
    """Every reader of provider observations, through its route, with a discovered row (payload: a candidate,
    not a Snapshot), a loaded remote snapshot and a selection of it. Readers found by grep over src/phaze for
    ProviderSourceObservation / ProviderRecordingCandidate: companion_details (+ companion_view), source_workspace,
    local_source_import's selected source, and the selected-only consumers (tags, Discogs, proposals), which a
    discovered row can never reach because it cannot be selected. Pipeline, stage, search and scan_deletion do not
    read observations at all.
    """
    upstream = ExampleUpstream(sets=("set-01", "set-02"))
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        found = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        discovered = {candidate.identity.native_id: candidate for candidate in found.candidates}
        loaded = await h.lookup.load(LoadCommand(candidate_id=discovered["set-01"].candidate_id))
        assert loaded.observation_id is not None
        await decide(h.sessions, media, loaded.observation_id)
        pending = discovered["set-02"].observation_id
        async with h.sessions() as session:
            pending_row = await session.get(ProviderSourceObservation, pending)
            assert pending_row is not None
            remote_source = pending_row.object_id

        app = create_app()
        app.state.remote_lookup = h.lookup

        async def override() -> AsyncIterator[AsyncSession]:
            async with h.sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override
        html = {"accept": "text/html", "HX-Request": "true"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

            async def get(url: str, headers: dict[str, str] | None = None) -> Any:
                response = await client.get(url, headers=headers or {})
                assert response.status_code == 200, (url, response.status_code, response.text[:500])
                return response

            for url in (f"/record/{media.id}", f"/files/{media.id}", f"/files/{media.id}/companion-details"):
                page = (await get(url, html)).text
                assert "No readable text received" not in page and "This version contains no text" not in page, url
                assert "example_catalog: set-01" in page, url
            await get(f"/files/{media.id}/poster.svg")

            details = (await get(f"/files/{media.id}/companion-details")).json()
            assert all(source["channel"] != "remote" for source in details["sources"]), "remote objects are not companion files"
            [authority] = details["selected"]
            assert authority["observation_id"] == str(loaded.observation_id) and authority["availability"] == "current"

            history = (await get(f"/files/{media.id}/companion-sources/{remote_source}/observations")).json()
            [row] = history["observations"]
            assert (row["status"], row["availability"], row["track_count"], row["completeness_state"]) == ("discovered", "discovered", 0, None)
            await get(f"/files/{media.id}/companion-sources/{remote_source}/observations", html)

            stored = (await get(f"/files/{media.id}/companion-observations/{pending}")).json()
            assert stored["tracks"] == [] and stored["summary"]["availability"] == "discovered"
            await get(f"/files/{media.id}/companion-observations/{pending}", html)
            assert (await get(f"/files/{media.id}/companion-observations/{pending}/text")).json()["text"] is None
            version = (await get(f"/files/{media.id}/companion-observations/{loaded.observation_id}")).json()
            assert version["summary"]["availability"] == "current" and version["selected_availability"] == "current"
            assert [track["title"] for track in version["tracks"]] == ["Opening", None]
            await get(f"/files/{media.id}/companion-observations/{loaded.observation_id}", html)

            for kind in ("tracklist", "release_metadata", "cue"):
                listing = (await get(f"/pipeline/local-source-sets?kind={kind}", html)).text
                assert "discovered" not in listing and " missing " not in listing, kind
            selected = (await get(f"/api/local-sources/recordings/{media.id}/selected")).json()
            assert selected["availability"] == "current" and selected["channel"] == "remote"


async def test_the_router_is_origin_guarded_records_the_server_owned_actor_and_refuses_a_disabled_provider(committed_db: Any) -> None:
    upstream = ExampleUpstream()
    async with harness(committed_db, upstream.routes()) as h:
        media = await seed(h.sessions)
        app = create_app()
        app.state.remote_lookup = h.lookup

        async def override() -> AsyncIterator[AsyncSession]:
            async with h.sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override
        body = {"media_id": str(media.id), "provider_id": EXAMPLE}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for path, payload in (
                ("/api/remote-lookups/lookup", body),
                ("/api/remote-lookups/load", {"candidate_id": str(uuid.uuid4())}),
                (f"/api/remote-lookups/providers/{EXAMPLE}/clear-stop", {"reason": "synthetic"}),
            ):
                foreign = await client.post(path, json=payload, headers={"origin": "https://elsewhere.invalid", "sec-fetch-site": "cross-site"})
                assert foreign.status_code == 403, path
            assert h.socket.requests == [], "a refused cross-origin request sends nothing"

            found = (await client.post("/api/remote-lookups/lookup", json=body)).json()
            loaded = (await client.post("/api/remote-lookups/load", json={"candidate_id": found["candidates"][0]["candidate_id"]})).json()
            decision = {
                "media_id": str(media.id),
                "observation_id": loaded["observation_id"],
                "decision_id": str(uuid.uuid4()),
                "expected_revision": "rev-a",
                "expected_media_sha256": media.sha256_hash,
            }
            assert (await client.post("/api/remote-lookups/decision", json={**decision, "actor": "someone"})).status_code == 422
            assert (await client.post("/api/remote-lookups/decision", json=decision)).status_code == 200
        async with h.sessions() as session:
            event = await session.scalar(select(ProviderSelectionEvent).where(ProviderSelectionEvent.action == "select"))
            assert event is not None and event.actor == "admin"

    async with harness(committed_db, upstream.routes(), settings={}) as h:
        media = await seed(h.sessions)
        app = create_app()
        app.state.remote_lookup = h.lookup
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post("/api/remote-lookups/lookup", json={"media_id": str(media.id), "provider_id": EXAMPLE})
            assert refused.status_code == 200 and refused.json()["outcome"]["code"] == "disabled_provider"
            [listed] = (await client.get("/api/remote-lookups/providers")).json()
            assert listed["enabled"] is False
        assert h.socket.requests == []
