"""The remote transport effect against a real Postgres ledger, a fake clock and a fake socket.

Nothing here opens a socket or contacts any host: every exchange goes through ``FakeSocket``, and time
is ``FakeClock``. The ledger is real (``committed_db``), because durability across restarts and
workers is the property under test. Hosts are ``.invalid`` and every identifier is synthetic.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from pydantic import SecretStr
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from phaze.services.remote_transport import OutboundRequest, RemoteTransport, clear_stop
from phaze.tracklist_providers import ProviderRegistry
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
    RemoteRequest,
    SearchTerm,
    Snapshot,
    SourceIdentity,
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.host_profile import CredentialSlot, RemoteFetchRoute, RemoteHostProfile, RemoteSearchRoute
from phaze.tracklist_providers.port import AuthorizedEffects
from tests._lock_barrier import POLL_INTERVAL_SEC, WEDGED_CONTENDER_CEILING_SEC


pytestmark = pytest.mark.integration

PROVIDER = "example_catalog"
GAP = 6.0
START = datetime(2026, 1, 1, tzinfo=UTC)
SECRET = "synthetic-secret-7f3a9c"  # a synthetic value whose absence is asserted
PROFILE = RemoteHostProfile(
    provider_id=PROVIDER,
    host="catalog.example.invalid",
    user_agent="phaze-test/1 (+https://example.invalid)",
    search=RemoteSearchRoute(
        fields=frozenset({"artist", "event", "date"}),
        format="example_search_json",
        max_page=3,
        path="/api/search",
        term_parameters=(("artist", "q"), ("event", "q"), ("date", "year")),
        page_parameter="page",
        fixed_parameters=(("limit", "100"),),
    ),
    fetch=RemoteFetchRoute(native_id_pattern=r"[A-Za-z0-9 _/.-]{1,64}", format="example_set_json", path_template="/api/sets/{native_id}"),
    min_gap_seconds=GAP,
    robots="not_applicable",
)
CONTEXT = RecordingContext(
    recording_id="recording-01",
    artist=Fact(value="Example Artist", certainty="known"),
    event=Fact(value="Example Event", certainty="inferred"),
)


class FakeClock:
    """Wall and monotonic time advance together on sleep; ``jump`` moves only the wall clock."""

    def __init__(self) -> None:
        self._wall = START
        self._mono = 1000.0

    def wall(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self._wall += timedelta(seconds=seconds)
        self._mono += seconds

    def jump(self, seconds: float) -> None:
        self._wall += timedelta(seconds=seconds)


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "application/json"})
    body: bytes = b'{"rows": []}'
    chunk: int = 4096
    raises: BaseException | None = None
    endless: bool = False


class FakeResponse:
    def __init__(self, reply: Reply, socket: "FakeSocket") -> None:
        self._reply = reply
        self._socket = socket
        self.status = reply.status
        self.headers = reply.headers

    async def chunks(self) -> AsyncIterator[bytes]:
        body = self._reply.body
        while True:
            for start in range(0, len(body), self._reply.chunk):
                self._socket.chunks_consumed += 1
                yield body[start : start + self._reply.chunk]
            if not self._reply.endless:
                return


class FakeSocket:
    """Stands in for the network. Records every request and when it left; replies from a script."""

    def __init__(self, clock: FakeClock, *replies: Reply) -> None:
        self.clock = clock
        self.replies = list(replies)
        self.requests: list[OutboundRequest] = []
        self.sent_at: list[datetime] = []
        self.chunks_consumed = 0

    @asynccontextmanager
    async def open(self, request: OutboundRequest) -> AsyncIterator[FakeResponse]:
        self.requests.append(request)
        self.sent_at.append(self.clock.wall())
        reply = self.replies.pop(0) if self.replies else Reply()
        if reply.raises is not None:
            raise reply.raises
        yield FakeResponse(reply, self)


class ProcessKilled(BaseException):
    """A worker dying mid-send: nothing after the send runs, exactly like a kill."""


def search_reference(page: int = 1) -> SourceReference:
    return SourceReference(
        identity=SourceIdentity(provider_id=PROVIDER, native_id="search:" + "a" * 64),
        channel="remote",
        format="example_search_json",
        request=RemoteRequest(kind="search", terms=(SearchTerm(field="artist", value="Example Artist"),), page=page),
    )


def fetch_reference(native_id: str = "set-01") -> SourceReference:
    return SourceReference(
        identity=SourceIdentity(provider_id=PROVIDER, native_id=native_id),
        channel="remote",
        format="example_set_json",
        request=RemoteRequest(kind="fetch"),
    )


def worker(engine: AsyncEngine, clock: FakeClock, socket: FakeSocket, profile: RemoteHostProfile = PROFILE, **kwargs: Any) -> RemoteTransport:
    return RemoteTransport(profiles=(profile,), engine=engine, socket=socket, clock=clock, **kwargs)


async def ledger(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM remote_transport_ledgers WHERE provider_id = :p"), {"p": PROVIDER})).mappings().one()
        attempts = (await conn.execute(text("SELECT * FROM remote_transport_attempts ORDER BY ordinal"))).mappings().all()
    return {**row, "attempts": attempts}


async def test_requests_are_serial_and_paced_across_restarts(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    first, reloaded = worker(engine, clock, socket), worker(engine, clock, socket)
    budget = DiscoveryBudget()
    first_run = first.run()
    assert (await first_run.read(fetch_reference(), budget)).status == OutcomeStatus.FOUND
    assert (await first_run.read(fetch_reference("set-02"), budget)).status == OutcomeStatus.FOUND
    # A reloaded worker, already past its own startup quarantine, still waits out the durable gap.
    assert (await reloaded.run().read(search_reference(), budget)).status == OutcomeStatus.FOUND
    # A worker started after all of that waits a full gap of its own before its first send.
    clock.advance(60)
    fresh = worker(engine, clock, socket)
    started = clock.wall()
    assert (await fresh.run().read(fetch_reference(), budget)).status == OutcomeStatus.FOUND
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(socket.sent_at, socket.sent_at[1:], strict=False)]
    assert len(socket.sent_at) == 4
    assert min(gaps) >= GAP
    assert (socket.sent_at[-1] - started).total_seconds() >= GAP
    state = await ledger(engine)
    assert state["ordinal"] == 4 and [row["completed_at"] is not None for row in state["attempts"]] == [True] * 4


async def test_concurrent_workers_never_overlap_requests(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    in_flight: list[int] = []
    peak = 0

    class SlowSocket(FakeSocket):
        @asynccontextmanager
        async def open(self, request: OutboundRequest) -> AsyncIterator[FakeResponse]:
            nonlocal peak
            in_flight.append(1)
            peak = max(peak, len(in_flight))
            try:
                await asyncio.sleep(0.01)
                async with super().open(request) as response:
                    yield response
            finally:
                in_flight.pop()

    socket = SlowSocket(clock)
    workers = [worker(engine, clock, socket) for _ in range(3)]
    reads = await asyncio.gather(*(each.run().read(fetch_reference(f"set-0{index}"), DiscoveryBudget()) for index, each in enumerate(workers)))
    assert [read.status for read in reads] == [OutcomeStatus.FOUND] * 3
    assert peak == 1
    assert min((b - a).total_seconds() for a, b in zip(socket.sent_at, socket.sent_at[1:], strict=False)) >= GAP


async def test_reservation_is_durable_before_send(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    profile = PROFILE.model_copy(update={"durable_budget": 1})
    dying = FakeSocket(clock, Reply(raises=ProcessKilled()))
    with pytest.raises(ProcessKilled):
        await worker(engine, clock, dying, profile).run().read(fetch_reference(), DiscoveryBudget())
    assert len(dying.requests) == 1
    state = await ledger(engine)
    assert state["ordinal"] == 1 and len(state["attempts"]) == 1
    clock.advance(GAP * 2)
    survivor = FakeSocket(clock)
    read = await worker(engine, clock, survivor, profile).run().read(fetch_reference(), DiscoveryBudget())
    assert (read.status, read.code) == (OutcomeStatus.RETRY, "durable_budget_exhausted")
    assert survivor.requests == []


async def test_budget_is_shared_across_workers(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    profile = PROFILE.model_copy(update={"durable_budget": 3})
    socket = FakeSocket(clock)
    workers = [worker(engine, clock, socket, profile) for _ in range(4)]
    attempts = [each.run().read(fetch_reference(f"set-{index}{turn}"), DiscoveryBudget()) for index, each in enumerate(workers) for turn in range(2)]
    reads = await asyncio.gather(*attempts)
    assert sum(read.status == OutcomeStatus.FOUND for read in reads) == 3
    assert {read.code for read in reads if read.status != OutcomeStatus.FOUND} == {"durable_budget_exhausted"}
    assert len(socket.requests) == 3
    assert (await ledger(engine))["ordinal"] == 3
    # The window rolls: once the oldest reservation leaves it, the budget is available again.
    clock.advance(PROFILE.durable_window_seconds)
    assert (await workers[0].run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND


async def test_per_run_budget_counts_every_request(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock, Reply(status=503), Reply(status=503))
    transport = worker(engine, clock, socket, PROFILE.model_copy(update={"per_run_budget": 2}))
    run = transport.run()
    assert [(await run.read(fetch_reference(), DiscoveryBudget())).code for _ in range(3)] == ["http_503", "http_503", "run_budget_exhausted"]
    assert len(socket.requests) == 2
    assert (await transport.run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE remote_transport_ledgers SET ordinal = 0, last_reserved_at = NULL",
        "DELETE FROM remote_transport_attempts WHERE ordinal = 2",
        "UPDATE remote_transport_ledgers SET last_reserved_at = last_reserved_at - interval '1 hour'",
        "UPDATE remote_transport_attempts SET ordinal = 7 WHERE ordinal = 2",
        "DELETE FROM remote_transport_attempts; DELETE FROM remote_transport_ledgers",
    ],
    ids=["counter_reset", "attempt_deleted", "anchor_moved", "ordinal_gap", "history_wiped_but_ledger_recreated"],
)
async def test_corrupt_ledger_fails_closed(committed_db: Any, tamper: str) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    transport = worker(engine, clock, socket)
    for native in ("set-01", "set-02"):
        assert (await transport.run().read(fetch_reference(native), DiscoveryBudget())).status == OutcomeStatus.FOUND
    async with engine.begin() as conn:
        for statement in tamper.split("; "):
            await conn.execute(text(statement))
        if tamper.startswith("DELETE FROM remote_transport_attempts; "):
            # A ledger row recreated by hand with no history behind it is just as corrupt as a reset one.
            await conn.execute(
                text("INSERT INTO remote_transport_ledgers(provider_id, ordinal, last_reserved_at) VALUES (:p, 2, now())"), {"p": PROVIDER}
            )
    async with engine.connect() as conn:
        before = (await conn.execute(text("SELECT ordinal, last_reserved_at FROM remote_transport_ledgers"))).all()
    clock.advance(GAP * 10)
    read = await transport.run().read(fetch_reference(), DiscoveryBudget())
    assert (read.status, read.code) == (OutcomeStatus.UNAVAILABLE, "ledger_corrupt")
    assert len(socket.requests) == 2
    async with engine.connect() as conn:
        after = (await conn.execute(text("SELECT ordinal, last_reserved_at FROM remote_transport_ledgers"))).all()
    assert after == before, "a corrupt ledger is refused, never repaired or reset"


async def test_forward_clock_jump_fails_closed(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    transport = worker(engine, clock, socket)
    assert (await transport.run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND
    clock.advance(1)
    clock.jump(3600)
    read = await transport.run().read(fetch_reference(), DiscoveryBudget())
    assert (read.status, read.code) == (OutcomeStatus.UNAVAILABLE, "clock_discontinuity")
    assert len(socket.requests) == 1
    # After the refusal the worker re-quarantines: the next send waits a full gap of real (monotonic) time.
    before = clock.monotonic()
    assert (await transport.run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND
    assert clock.monotonic() - before >= GAP
    # A jump within the tolerance is ordinary scheduling noise and does not refuse.
    clock.jump(PROFILE.clock_tolerance_seconds / 2)
    clock.advance(GAP)
    assert (await transport.run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND


async def test_jump_during_the_pacing_wait_fails_closed(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()

    class JumpingClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            await super().sleep(seconds)
            self.jump(600)

    jumping = JumpingClock()
    socket = FakeSocket(clock)
    read = await worker(engine, jumping, socket).run().read(fetch_reference(), DiscoveryBudget())
    assert (read.status, read.code) == (OutcomeStatus.UNAVAILABLE, "clock_discontinuity")
    assert socket.requests == []


def stop_reply(kind: str) -> Reply:
    html = {"content-type": "text/html; charset=utf-8"}
    return {
        "403": Reply(status=403, headers=html, body=b"<html>Forbidden</html>"),
        "429": Reply(status=429, headers={**html, "retry-after": "120"}, body=b"slow down"),
        "challenge_header": Reply(status=503, headers={**html, "cf-mitigated": "challenge"}, body=b"<html>interstitial</html>"),
        "challenge_body": Reply(
            status=200,
            headers=html,
            body=b"<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={cType:'managed'};</script></body></html>",
        ),
    }[kind]


@pytest.mark.parametrize("kind", ["403", "429", "challenge_header", "challenge_body"])
async def test_stop_on_403_429_and_real_interstitial_persists(committed_db: Any, kind: str) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock, stop_reply(kind))
    read = await worker(engine, clock, socket).run().read(fetch_reference(), DiscoveryBudget())
    assert read.status != OutcomeStatus.FOUND and read.text is None
    clock.advance(GAP * 100)
    restarted = FakeSocket(clock)
    refused = await worker(engine, clock, restarted).run().read(fetch_reference(), DiscoveryBudget())
    assert (refused.status, refused.code) == (OutcomeStatus.UNAVAILABLE, "transport_stopped")
    assert restarted.requests == []
    assert await clear_stop(engine, PROVIDER, operator="operator", reason="synthetic test reset")
    state = await ledger(engine)
    assert state["stop_code"] is None and state["cleared_by"] == "operator"
    assert (await worker(engine, clock, restarted).run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND
    assert not await clear_stop(engine, PROVIDER, operator="operator", reason="nothing to clear")
    with pytest.raises(ValueError, match="names the operator"):
        await clear_stop(engine, PROVIDER, operator=" ", reason="")


async def test_readable_page_with_challenge_script_reference_is_not_a_stop(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    page = (
        b"<html><head><title>Example policy</title></head><body><h1>Legal</h1><p>Readable policy content.</p>"
        b"<script>(function(){window.__CF$cv$params={r:'0',t:'0'};var a=document.createElement('script');"
        b"a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';document.head.appendChild(a);})();</script></body></html>"
    )
    socket = FakeSocket(clock, Reply(status=200, headers={"content-type": "text/html; charset=utf-8"}, body=page))
    transport = worker(engine, clock, socket)
    read = await transport.run().read(fetch_reference(), DiscoveryBudget())
    assert (read.status, read.code) == (OutcomeStatus.FOUND, "http_200")
    assert read.text is not None and "Readable policy content." in read.text
    assert (await ledger(engine))["stop_code"] is None
    assert (await transport.run().read(fetch_reference("set-02"), DiscoveryBudget())).status == OutcomeStatus.FOUND
    assert len(socket.requests) == 2


async def test_host_and_route_allowlist_refuses_everything_else(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    search_only = PROFILE.model_copy(update={"fetch": None})
    transport = worker(engine, clock, socket, search_only)
    other_host = fetch_reference().model_copy(update={"identity": SourceIdentity(provider_id="another_catalog", native_id="set-01")})
    forged_page = search_reference().model_copy(update={"request": RemoteRequest(kind="search", terms=search_reference().request.terms, page=9)})  # type: ignore[union-attr]
    unaccepted_field = SourceReference(
        identity=search_reference().identity,
        channel="remote",
        format="example_search_json",
        request=RemoteRequest(kind="search", terms=(SearchTerm(field="artist", value="x"),)),
    )
    narrow = worker(
        engine,
        clock,
        socket,
        PROFILE.model_copy(
            update={
                "search": RemoteSearchRoute(
                    fields=frozenset({"event"}), format="example_search_json", path="/api/search", term_parameters=(("event", "q"),)
                )
            }
        ),
    )
    local = SourceReference(identity=SourceIdentity(provider_id=PROVIDER, native_id="set-01"), channel="companion", format="cue")
    cases = [
        (transport, other_host, "unknown_host_profile"),
        (transport, fetch_reference(), "route_not_allowed"),
        (transport, forged_page, "search_not_allowed"),
        (narrow, unaccepted_field, "search_not_allowed"),
        (transport, search_reference().model_copy(update={"format": "html"}), "format_mismatch"),
        (transport, local, "not_remote"),
        (
            transport,
            {"identity": {"provider_id": PROVIDER, "native_id": "x"}, "channel": "remote", "format": "x", "url": "https://elsewhere.invalid/"},
            "invalid_reference",
        ),
        (worker(engine, clock, socket), fetch_reference("set:01"), "unauthorized_identity"),
        (worker(engine, clock, socket), fetch_reference(".."), "unauthorized_identity"),
        (worker(engine, clock, socket), fetch_reference("set-01").model_copy(update={"format": "x"}), "format_mismatch"),
        (worker(engine, clock, socket, PROFILE.model_copy(update={"search": None})), search_reference(), "route_not_allowed"),
    ]
    for transport_under_test, reference, code in cases:
        read = await transport_under_test.run().read(reference, DiscoveryBudget())  # type: ignore[arg-type]
        assert (read.status, read.code) == (OutcomeStatus.CONTRACT_ERROR, code), code
    assert socket.requests == []
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM remote_transport_attempts")) == 0
    # What is allowed is built from the profile alone: one host, one route, the native ID as one segment.
    allowed = worker(engine, clock, socket)
    await allowed.run().read(fetch_reference("a/b c"), DiscoveryBudget())
    clock.advance(GAP)
    await allowed.run().read(search_reference(page=2).model_copy(), DiscoveryBudget())
    assert [request.url for request in socket.requests] == [
        "https://catalog.example.invalid/api/sets/a%2Fb%20c",
        "https://catalog.example.invalid/api/search?limit=100&q=Example%20Artist&page=2",
    ]
    assert {request.host for request in socket.requests} == {"catalog.example.invalid"}


async def test_secret_never_appears_in_log_repr_or_error(committed_db: Any, caplog: pytest.LogCaptureFixture) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    profile = PROFILE.model_copy(update={"credential": CredentialSlot(header="Authorization", prefix="Bearer ")})
    replies = [
        Reply(),
        Reply(status=401),
        Reply(status=400),
        Reply(status=500),
        Reply(raises=TimeoutError(SECRET)),
        Reply(raises=ConnectionError(SECRET)),
        Reply(status=403),
    ]
    socket = FakeSocket(clock, *replies)
    transport = worker(engine, clock, socket, profile, credentials={PROVIDER: SecretStr(SECRET)})
    run = transport.run()
    caplog.set_level(logging.DEBUG)
    reads = []
    for _ in replies:
        reads.append(await run.read(fetch_reference(), DiscoveryBudget()))
        clock.advance(GAP)
    reads.append(await run.read(fetch_reference(), DiscoveryBudget()))
    sent = socket.requests[0].credential
    assert sent is not None and sent[0] == "Authorization" and sent[1].get_secret_value() == f"Bearer {SECRET}", "the secret is really sent"
    surfaces = [
        caplog.text,
        repr(transport),
        repr(run),
        repr(profile),
        *(repr(request) for request in socket.requests),
        *(str(request) for request in socket.requests),
    ]
    surfaces += [read.model_dump_json() for read in reads]
    async with engine.connect() as conn:
        surfaces.append(str((await conn.execute(text("SELECT * FROM remote_transport_attempts"))).all()))
        surfaces.append(str((await conn.execute(text("SELECT * FROM remote_transport_ledgers"))).all()))
    assert all(SECRET not in surface for surface in surfaces)
    with pytest.raises(ValueError, match="without a credential slot") as raised:
        worker(engine, clock, socket, credentials={PROVIDER: SecretStr(SECRET)})
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize(
    ("reply", "status", "code"),
    [
        (Reply(status=400), OutcomeStatus.CONTRACT_ERROR, "http_400"),
        (Reply(status=401), OutcomeStatus.UNAVAILABLE, "http_401"),
        (Reply(status=403), OutcomeStatus.UNAVAILABLE, "http_403"),
        (Reply(status=404), OutcomeStatus.ABSENT, "http_404"),
        (Reply(status=429), OutcomeStatus.RETRY, "http_429"),
        (Reply(status=500), OutcomeStatus.RETRY, "http_500"),
        (Reply(raises=TimeoutError()), OutcomeStatus.RETRY, "timeout"),
        (Reply(raises=ConnectionError()), OutcomeStatus.RETRY, "transport_error"),
        (Reply(status=304), OutcomeStatus.UNAVAILABLE, "http_304_not_requested"),
        (Reply(status=302, headers={"location": "https://elsewhere.invalid/"}), OutcomeStatus.UNAVAILABLE, "redirect_not_followed"),
        (Reply(status=204, body=b""), OutcomeStatus.UNSUPPORTED, "http_204"),
        (Reply(status=410), OutcomeStatus.UNAVAILABLE, "http_410"),
        (Reply(status=101), OutcomeStatus.UNSUPPORTED, "http_101"),
        (Reply(headers={"content-type": "application/json; charset=nonexistent"}), OutcomeStatus.UNSUPPORTED, "undecodable_response"),
        (Reply(body=b"\xff\xfe\xfa"), OutcomeStatus.UNSUPPORTED, "undecodable_response"),
    ],
)
async def test_status_codes_map_to_typed_outcomes(committed_db: Any, reply: Reply, status: OutcomeStatus, code: str) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock, reply)
    read = await worker(engine, clock, socket).run().read(fetch_reference("set-07"), DiscoveryBudget())
    assert (read.status, read.code) == (status, code)
    assert read.status != OutcomeStatus.FOUND and read.text is None
    assert read.scope == "set-07", "an outcome, absence included, is scoped to the one object read"
    assert len(socket.requests) == 1
    state = await ledger(engine)
    assert state["ordinal"] == 1 and state["attempts"][0]["outcome"] == code, "every request is charged, whatever came back"


async def test_found_read_carries_validators_and_a_declared_content_revision(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    headers = {"content-type": "application/json; charset=utf-8", "etag": '"abc123"', "last-modified": "Thu, 01 Jan 2026 00:00:00 GMT"}
    declared = PROFILE.model_copy(update={"fetch": PROFILE.fetch.model_copy(update={"etag_is_content_validator": True})})  # type: ignore[union-attr]
    socket = FakeSocket(clock, Reply(headers=headers), Reply(headers={**headers, "etag": 'W/"weak"'}), Reply(headers=headers))
    read = await worker(engine, clock, socket, declared).run().read(fetch_reference(), DiscoveryBudget())
    assert (read.revision, read.revision_scope, read.encoding) == ('"abc123"', "full", "utf-8")
    assert read.validators is not None and read.validators.last_modified == "Thu, 01 Jan 2026 00:00:00 GMT"
    clock.advance(GAP)
    weak = await worker(engine, clock, socket, declared).run().read(fetch_reference(), DiscoveryBudget())
    assert (weak.revision, weak.validators.etag if weak.validators else None) == (None, 'W/"weak"')
    clock.advance(GAP * 2)
    undeclared = await worker(engine, clock, socket).run().read(fetch_reference(), DiscoveryBudget())
    assert undeclared.revision is None and undeclared.validators is not None and undeclared.bytes_read == len(b'{"rows": []}')


async def test_retry_after_is_honoured_durably(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock, Reply(status=503, headers={"retry-after": "60"}), Reply(status=503, headers={"retry-after": "not a date"}))
    read = await worker(engine, clock, socket).run().read(fetch_reference(), DiscoveryBudget())
    assert read.status == OutcomeStatus.RETRY and "retry_after_seconds=60" in read.evidence
    restarted = worker(engine, clock, socket)
    assert (await restarted.run().read(fetch_reference(), DiscoveryBudget(deadline_seconds=30))).code == "paced"
    assert len(socket.requests) == 1, "a wait longer than the deadline returns retry without reserving"
    await restarted.run().read(fetch_reference(), DiscoveryBudget(deadline_seconds=120))
    assert (socket.sent_at[1] - socket.sent_at[0]).total_seconds() >= 60
    later = (socket.sent_at[1] + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    socket.replies.append(Reply(status=429, headers={"retry-after": later}))
    clock.advance(GAP)
    stopped = await restarted.run().read(fetch_reference(), DiscoveryBudget())
    assert stopped.code == "http_429" and any(item.startswith("retry_after_seconds=") for item in stopped.evidence)


async def test_oversized_response_is_rejected_without_buffering_it_all(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    endless = Reply(body=b"x" * 65536, chunk=65536, endless=True)
    socket = FakeSocket(clock, endless)
    budget = DiscoveryBudget(max_bytes=131072)
    read = await worker(engine, clock, socket).run().read(fetch_reference(), budget)
    assert (read.status, read.code, read.truncated, read.text) == (OutcomeStatus.UNAVAILABLE, "response_too_large", True, None)
    assert read.bytes_read == budget.max_bytes
    assert socket.chunks_consumed == 3, "reading stops one chunk past the limit of an endless body"
    clock.advance(GAP)
    socket.chunks_consumed = 0
    socket.replies.append(Reply(headers={"content-type": "application/json", "content-length": str(10**9)}, endless=True))
    declared = await worker(engine, clock, socket).run().read(fetch_reference(), budget)
    assert (declared.code, declared.bytes_read) == ("response_too_large", 0)
    assert socket.chunks_consumed == 0, "a declared oversize is refused before the body is read"


async def test_robots_is_paced_charged_cached_and_enforced(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    robots = Reply(headers={"content-type": "text/plain"}, body=b"User-agent: *\nCrawl-delay: 20\nDisallow: /api/search\n")
    socket = FakeSocket(clock, robots)
    profile = PROFILE.model_copy(update={"robots": "respect"})
    transport = worker(engine, clock, socket, profile)
    assert (await transport.run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND
    assert [request.url for request in socket.requests] == [
        "https://catalog.example.invalid/robots.txt",
        "https://catalog.example.invalid/api/sets/set-01",
    ]
    assert (socket.sent_at[1] - socket.sent_at[0]).total_seconds() >= 20, "Crawl-delay widens the gap"
    assert [row["request_kind"] for row in (await ledger(engine))["attempts"]] == ["robots", "fetch"]
    refused = await transport.run().read(search_reference(), DiscoveryBudget())
    assert (refused.status, refused.code) == (OutcomeStatus.UNAVAILABLE, "robots_disallow")
    assert len(socket.requests) == 2, "the cached robots answer is reused, and a disallowed route is never sent"
    assert (await ledger(engine))["stop_code"] == "robots_disallow"


@pytest.mark.parametrize(
    ("reply", "outcome"),
    [
        (Reply(status=404), OutcomeStatus.FOUND),
        (Reply(status=503), "robots_http_503"),
        (Reply(raises=TimeoutError()), "robots_timeout"),
        (Reply(status=403), "robots_http_403"),
    ],
)
async def test_unavailable_robots_allows_and_unreachable_robots_refuses(committed_db: Any, reply: Reply, outcome: object) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock, reply)
    read = await worker(engine, clock, socket, PROFILE.model_copy(update={"robots": "respect"})).run().read(fetch_reference(), DiscoveryBudget())
    if outcome == OutcomeStatus.FOUND:
        assert read.status == OutcomeStatus.FOUND and len(socket.requests) == 2
    else:
        assert read.code == outcome and read.status != OutcomeStatus.FOUND and len(socket.requests) == 1


class RemoteCatalog:
    """A fictional remote provider that reads only what the registry minted for it."""

    descriptor = ProviderDescriptor(
        id=PROVIDER,
        display_name="Fictional remote catalog",
        capabilities=frozenset({Capability.LOAD, Capability.DISCOVERY, Capability.REMOTE_TRANSPORT}),
    )

    def __init__(self, forge: bool = False) -> None:
        self.forge = forge
        self.seen: list[SourceRead] = []

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        del cursor
        reference = search_reference() if self.forge else effects.minted[0]
        self.seen.append(await effects.read(reference, budget))
        return DiscoveryOutcome(
            status="incomplete",
            code="search_capped",
            scope=context.recording_id,
            candidates=(ProviderCandidate(identity=SourceIdentity(provider_id=PROVIDER, native_id="set-01")),),
            completeness=Completeness(state="incomplete", reason="Search is capped and may be incomplete"),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:
        self.seen.append(await effects.read(effects.minted[0], budget))
        snapshot = Snapshot(
            identity=candidate.identity,
            retrieved_at=self.seen[-1].retrieved_at,
            source_format="example_set_json",
            parser_version="example-v1",
            tracks=(ProviderTrack(position=1, artist="Example Artist", title="Opening"),),
            completeness=Completeness(state="complete", reason="rows exhausted"),
        )
        return LoadOutcome(status="found", code="loaded", scope=candidate.identity.native_id, snapshot=snapshot)


async def test_remote_reads_run_end_to_end_through_the_real_registry(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    transport = worker(engine, clock, socket)
    provider = RemoteCatalog()
    registry = ProviderRegistry((provider,), remote_profiles=(PROFILE,))
    run = transport.run()
    discovered = await registry.discover(PROVIDER, CONTEXT, DiscoveryBudget(), run)
    assert (discovered.status, discovered.code) == (OutcomeStatus.INCOMPLETE, "search_capped")
    loaded = await registry.load(discovered.candidates[0], LoadBudget(), run)
    assert loaded.status == OutcomeStatus.FOUND
    assert [read.status for read in provider.seen] == [OutcomeStatus.FOUND] * 2
    assert [request.url for request in socket.requests] == [
        "https://catalog.example.invalid/api/search?limit=100&q=Example%20Artist%20Example%20Event&page=1",
        "https://catalog.example.invalid/api/sets/set-01",
    ]
    assert (socket.sent_at[1] - socket.sent_at[0]).total_seconds() >= GAP
    # An unminted reference is refused by the registry before the transport, so no socket ever opens.
    forging = ProviderRegistry((RemoteCatalog(forge=True),), remote_profiles=(PROFILE,))
    refused = await forging.discover(PROVIDER, CONTEXT, DiscoveryBudget(), transport.run())
    assert (refused.status, refused.code, refused.detail) == (OutcomeStatus.CONTRACT_ERROR, "invalid_output", "unauthorized_read")
    assert len(socket.requests) == 2
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM remote_transport_attempts")) == 2


async def test_state_changed_by_another_worker_during_the_wait_is_honoured(committed_db: Any) -> None:
    engine, _ = committed_db

    class TamperingClock(FakeClock):
        statement = ""

        async def sleep(self, seconds: float) -> None:
            await super().sleep(seconds)
            async with engine.begin() as conn:
                await conn.execute(text(self.statement))

    clock = TamperingClock()
    socket = FakeSocket(clock)
    transport = worker(engine, clock, socket)
    clock.statement = "UPDATE remote_transport_ledgers SET stopped_at = now(), stop_code = 'robots_disallow'"
    stopped = await transport.run().read(fetch_reference(), DiscoveryBudget())
    assert (stopped.code, stopped.evidence) == ("transport_stopped", ("stop=robots_disallow",))
    assert await clear_stop(engine, PROVIDER, operator="operator", reason="synthetic")
    clock.statement = "SELECT 1"
    assert (await transport.run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND
    clock.statement = "UPDATE remote_transport_ledgers SET ordinal = 5"
    corrupt = await transport.run().read(fetch_reference(), DiscoveryBudget())
    assert corrupt.code == "ledger_corrupt"
    assert len(socket.requests) == 1


async def test_concurrent_reads_in_one_run_share_its_budget(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    run = worker(engine, clock, socket, PROFILE.model_copy(update={"per_run_budget": 1})).run()
    reads = await asyncio.gather(run.read(fetch_reference("set-01"), DiscoveryBudget()), run.read(fetch_reference("set-02"), DiscoveryBudget()))
    assert sorted(read.code for read in reads) == ["http_200", "run_budget_exhausted"]
    assert len(socket.requests) == 1


async def test_a_ledger_row_missing_behind_its_history_fails_closed(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    transport = worker(engine, clock, socket)
    assert (await transport.run().read(fetch_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND
    async with engine.begin() as conn:
        # The foreign key forbids this; replica mode stands in for any route around it.
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(text("DELETE FROM remote_transport_ledgers"))
    clock.advance(GAP)
    read = await transport.run().read(fetch_reference(), DiscoveryBudget())
    assert (read.code, read.evidence) == ("ledger_corrupt", ("ledger_missing",))
    assert len(socket.requests) == 1


async def test_a_lost_connection_never_strands_the_pacing_lock(committed_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = FakeSocket(clock)
    transport = worker(engine, clock, socket)
    original = RemoteTransport._locked_exchange

    async def drop_connection(self: RemoteTransport, conn: Any, *args: Any) -> Any:
        result = await original(self, conn, *args)
        raw = await conn.get_raw_connection()
        await raw.driver_connection.close()
        return result

    monkeypatch.setattr(RemoteTransport, "_locked_exchange", drop_connection)
    with pytest.raises(Exception):  # noqa: B017 - whatever the driver raises for a closed connection
        await transport.run().read(fetch_reference(), DiscoveryBudget())
    monkeypatch.setattr(RemoteTransport, "_locked_exchange", original)
    clock.advance(GAP)
    assert (await asyncio.wait_for(worker(engine, clock, socket).run().read(fetch_reference(), DiscoveryBudget()), 10)).status == OutcomeStatus.FOUND


async def test_profiles_are_unique_and_robots_without_crawl_delay_keeps_the_gap(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    with pytest.raises(ValueError, match="Duplicate remote host profile"):
        RemoteTransport(profiles=(PROFILE, PROFILE), engine=engine, socket=FakeSocket(clock), clock=clock)
    pageless = PROFILE.model_copy(
        update={
            "robots": "respect",
            "search": RemoteSearchRoute(
                fields=frozenset({"artist"}), format="example_search_json", path="/api/search", term_parameters=(("artist", "q"),)
            ),
        }
    )
    socket = FakeSocket(clock, Reply(headers={"content-type": "text/plain"}, body=b"User-agent: *\nDisallow: /private\n"))
    read = await worker(engine, clock, socket, pageless).run().read(search_reference(), DiscoveryBudget())
    assert read.status == OutcomeStatus.FOUND
    assert socket.requests[-1].url == "https://catalog.example.invalid/api/search?q=Example%20Artist"
    assert (socket.sent_at[1] - socket.sent_at[0]).total_seconds() == GAP


HELD_SQL = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) AND pid = ANY(:pids)"
)
OWNED_BACKENDS_SQL = text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid = ANY(:pids)")


class HangingSocket(FakeSocket):
    """Never answers for the hosts in ``hang``: a send that outlives its caller."""

    def __init__(self, clock: FakeClock, hang: set[str]) -> None:
        super().__init__(clock)
        self.hang = hang

    @asynccontextmanager
    async def open(self, request: OutboundRequest) -> AsyncIterator[FakeResponse]:
        if request.host in self.hang:
            self.requests.append(request)
            await asyncio.Event().wait()
        async with super().open(request) as response:
            yield response


@asynccontextmanager
async def transport_backend_probe(engine: AsyncEngine, *transports: RemoteTransport) -> AsyncIterator[tuple[AsyncConnection, set[int]]]:
    """Observe only connections opened by this test, through one untracked live-catalogue probe.

    NullPool's client-side close can finish before Postgres removes the backend row. Counting all
    database backends makes a previous fixture/observer close look like a leaked connection. Record
    identities from each transport's dedicated engine after opening the observer instead. Neither
    the session guard nor the fixture/observer engine is registered with the listener.
    AUTOCOMMIT refreshes pg_stat_activity's transaction-scoped snapshot on every poll.
    """
    async with engine.connect() as raw:
        probe = await raw.execution_options(isolation_level="AUTOCOMMIT")
        pids: set[int] = set()

        def record_backend(connection: Any, _record: object) -> None:
            pids.add(connection.driver_connection.get_server_pid())

        targets = [transport._engine.sync_engine for transport in transports]
        for target in targets:
            event.listen(target, "connect", record_backend)
        try:
            yield probe, pids
        finally:
            for target in targets:
                event.remove(target, "connect", record_backend)


async def held_locks_and_backends(probe: AsyncConnection, pids: set[int]) -> tuple[int, int]:
    params = {"pids": list(pids)}
    return int(await probe.scalar(HELD_SQL, params) or 0), int(await probe.scalar(OWNED_BACKENDS_SQL, params) or 0)


async def assert_transport_connections_released(probe: AsyncConnection, pids: set[int]) -> None:
    """Wait for actual server-side termination, with the shared finite hang-protection ceiling."""
    assert pids, "the probe must have observed a real transport connection"
    held: tuple[int, int] | None = None
    try:
        async with asyncio.timeout(WEDGED_CONTENDER_CEILING_SEC):
            while (held := await held_locks_and_backends(probe, pids)) != (0, 0):
                await asyncio.sleep(POLL_INTERVAL_SEC)
    except TimeoutError:
        pytest.fail(f"transport connections did not terminate: last (advisory locks, backends) observation was {held}")


async def test_backend_probe_ignores_an_unrelated_connection_closing(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    transport = worker(engine, clock, FakeSocket(clock))
    async with engine.connect() as unrelated, transport_backend_probe(engine, transport) as (probe, pids):
        async with transport._engine.connect() as owned:
            await owned.execute(text("SELECT pg_advisory_lock(2147483001)"))
            assert await held_locks_and_backends(probe, pids) == (1, 1)
            await unrelated.close()
            assert await held_locks_and_backends(probe, pids) == (1, 1)
        await assert_transport_connections_released(probe, pids)


async def test_cancellation_and_timeout_release_the_lock_and_its_connection(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    socket = HangingSocket(clock, {PROFILE.host})
    transport = worker(engine, clock, socket)
    impatient = worker(engine, clock, socket, PROFILE.model_copy(update={"request_timeout_seconds": 0.2}))
    succeeding = worker(engine, clock, socket)
    async with transport_backend_probe(engine, transport, impatient, succeeding) as (probe, pids):
        # Cancelled by its caller mid-send: the reservation stands, the lock and the connection do not.
        request = asyncio.create_task(transport.run().read(fetch_reference(), DiscoveryBudget()))
        try:
            async with asyncio.timeout(WEDGED_CONTENDER_CEILING_SEC):
                while not socket.requests:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
            assert await held_locks_and_backends(probe, pids) == (1, 1)
        finally:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert len(socket.requests) == 1
        await assert_transport_connections_released(probe, pids)
        assert [row["outcome"] for row in (await ledger(engine))["attempts"]] == ["interrupted"]
        # Timed out by the profile: a typed retry, and again nothing is left held.
        clock.advance(GAP)
        read = await impatient.run().read(fetch_reference(), DiscoveryBudget())
        assert (read.status, read.code) == (OutcomeStatus.RETRY, "timeout")
        await assert_transport_connections_released(probe, pids)
        socket.hang.clear()
        clock.advance(GAP)
        assert (await asyncio.wait_for(succeeding.run().read(fetch_reference(), DiscoveryBudget()), 10)).status == OutcomeStatus.FOUND
        await transport.aclose()


async def test_two_providers_never_wait_on_each_other(committed_db: Any) -> None:
    engine, _ = committed_db
    clock = FakeClock()
    other = PROFILE.model_copy(update={"provider_id": "other_catalog", "host": "other.example.invalid"})
    socket = HangingSocket(clock, {PROFILE.host})
    transport = RemoteTransport(profiles=(PROFILE, other), engine=engine, socket=socket, clock=clock)
    async with transport_backend_probe(engine, transport) as (probe, pids):
        stuck = asyncio.create_task(transport.run().read(fetch_reference(), DiscoveryBudget()))
        try:
            async with asyncio.timeout(WEDGED_CONTENDER_CEILING_SEC):
                while not socket.requests:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
            other_reference = fetch_reference().model_copy(update={"identity": SourceIdentity(provider_id="other_catalog", native_id="set-01")})
            read = await asyncio.wait_for(transport.run().read(other_reference, DiscoveryBudget()), 10)
            assert read.status == OutcomeStatus.FOUND and socket.requests[-1].host == "other.example.invalid"
            assert (await held_locks_and_backends(probe, pids))[0] == 1, "only the stuck provider's lock is held"
        finally:
            stuck.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stuck
        await assert_transport_connections_released(probe, pids)
