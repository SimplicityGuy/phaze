"""Host profile validation and the production socket, without a database or any network.

``HttpxSocket`` is exercised through ``httpx.MockTransport``, which answers in-process: no socket opens
and no host is contacted. Hosts are ``.invalid`` and every value is synthetic.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr, ValidationError
import pytest

from phaze.services.remote_transport import HttpxSocket, OutboundRequest, SystemClock, _http_outcome, _retry_after
from phaze.tracklist_providers.domain import OutcomeStatus
from phaze.tracklist_providers.host_profile import (
    DEFAULT_DURABLE_BUDGET,
    DEFAULT_MIN_GAP_SECONDS,
    CredentialSlot,
    RemoteFetchRoute,
    RemoteHostProfile,
    RemoteSearchRoute,
)


SEARCH = RemoteSearchRoute(fields=frozenset({"artist"}), format="json", path="/api/search", term_parameters=(("artist", "q"),))
FETCH = RemoteFetchRoute(native_id_pattern=r"set-[0-9]+", format="json", path_template="/api/sets/{native_id}")


def profile(**overrides: object) -> RemoteHostProfile:
    values: dict[str, object] = {"provider_id": "example_catalog", "host": "catalog.example.invalid", "user_agent": "phaze-test/1", "fetch": FETCH}
    return RemoteHostProfile.model_validate({**values, **overrides})


def test_defaults_are_conservative() -> None:
    built = profile()
    assert DEFAULT_MIN_GAP_SECONDS > 5.1 and built.min_gap_seconds == DEFAULT_MIN_GAP_SECONDS
    assert built.durable_budget == DEFAULT_DURABLE_BUDGET and built.durable_window_seconds == 3600
    assert built.robots == "respect" and built.stop_statuses == frozenset({403, 429}) and built.credential is None


@pytest.mark.parametrize(
    "host",
    [
        "https://catalog.example.invalid",
        "catalog.example.invalid:8443",
        "user@catalog.example.invalid",
        "catalog.example.invalid/path",
        "Catalog.Example.Invalid",
        "192.0.2.1",
        "[2001:db8::1]",
        "localhost",
        "catalog..invalid",
        "",
    ],
)
def test_a_host_is_one_lowercase_dns_name(host: str) -> None:
    with pytest.raises(ValidationError):
        profile(host=host)


@pytest.mark.parametrize(
    "template",
    [
        "https://elsewhere.invalid/{native_id}",
        "//elsewhere.invalid/{native_id}",
        "/api/{native_id}?x=1",
        "/api/../{native_id}",
        "/api/./{native_id}",
        "/api/set-{native_id}",
        "/api/{native_id}/{native_id}",
        "/api/sets",
        "api/{native_id}",
        "/api/{native_id}#frag",
        "/api//{native_id}",
    ],
)
def test_a_fetch_template_has_one_whole_segment_placeholder_and_no_host_or_query(template: str) -> None:
    with pytest.raises(ValidationError):
        RemoteFetchRoute(native_id_pattern="x", format="json", path_template=template)


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "https://elsewhere.invalid/search"},
        {"path": "/search?q=1"},
        {"term_parameters": ()},
        {"term_parameters": (("event", "q"),)},
        {"term_parameters": (("artist", "q"), ("artist", "r"))},
        {"term_parameters": (("artist", "q&x=1"),)},
        {"max_page": 2},
        {"page_parameter": "q"},
        {"fixed_parameters": (("q", "1"),)},
        {"fixed_parameters": (("limit", "1"), ("limit", "2"))},
        {"page_parameter": "page", "fixed_parameters": (("page", "1"),)},
    ],
)
def test_a_search_route_maps_each_field_to_one_unique_parameter(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RemoteSearchRoute.model_validate({**SEARCH.model_dump(), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_statuses": frozenset({403})},
        {"stop_statuses": frozenset({403, 429, 302})},
        {"user_agent": "phaze\r\nX-Injected: 1"},
        {"user_agent": ""},
        {"credential": {"header": "Authorization\r\n", "prefix": ""}},
        {"credential": {"header": "Authorization", "prefix": "Bearer\n"}},
        {"min_gap_seconds": 0.5},
        {"max_response_bytes": 262145},
        {"fetch": None, "search": None},
    ],
)
def test_profile_rejects_unsafe_or_weakening_settings(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        profile(**changes)


def test_a_profile_may_add_stop_statuses_and_a_credential_slot() -> None:
    built = profile(stop_statuses=frozenset({403, 429, 503}), credential=CredentialSlot(header="X-Api-Key"), search=SEARCH)
    assert 503 in built.stop_statuses and built.credential is not None and built.credential.header == "X-Api-Key"


@pytest.mark.parametrize(
    ("status", "outcome", "code"),
    [
        (200, OutcomeStatus.FOUND, "http_200"),
        (408, OutcomeStatus.RETRY, "http_408"),
        (503, OutcomeStatus.RETRY, "http_503"),
        (418, OutcomeStatus.UNAVAILABLE, "http_418"),
        (600, OutcomeStatus.UNSUPPORTED, "http_600"),
    ],
)
def test_http_outcome_table(status: int, outcome: OutcomeStatus, code: str) -> None:
    assert _http_outcome(status) == (outcome, code)


def test_retry_after_parses_seconds_and_dates_and_caps_them() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _retry_after(None, now) is None
    assert _retry_after("120", now) == 120
    assert _retry_after("999999", now) == 3600
    assert _retry_after("Thu, 01 Jan 2026 00:01:00 GMT", now) == 60
    assert _retry_after("Wed, 31 Dec 2025 00:00:00 GMT", now) == 0
    assert _retry_after("2026-01-01T00:01:00", now) is None
    assert _retry_after("Thu, 01 Jan 2026 00:01:00", now) == 60


async def test_system_clock() -> None:
    clock = SystemClock()
    before = clock.monotonic()
    await clock.sleep(0)
    assert clock.wall().tzinfo is not None and clock.monotonic() >= before


class Streamed(httpx.AsyncByteStream):
    """An unread body, as from a real connection; a bytes body would arrive already consumed."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._body


def request(**overrides: object) -> OutboundRequest:
    values: dict[str, object] = {
        "url": "https://catalog.example.invalid/api/sets/set-01",
        "host": "catalog.example.invalid",
        "headers": (("User-Agent", "phaze-test/1"), ("Accept-Encoding", "identity")),
        "timeout_seconds": 5.0,
    }
    return OutboundRequest(**{**values, **overrides})  # type: ignore[arg-type]


async def test_httpx_socket_sends_the_credential_and_never_follows_a_redirect() -> None:
    seen: list[httpx.Request] = []

    def handler(outgoing: httpx.Request) -> httpx.Response:
        seen.append(outgoing)
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/"}, stream=Streamed(b"moved"))

    socket = HttpxSocket(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    secret = SecretStr("Bearer synthetic-secret")
    async with socket.open(request(credential=("Authorization", secret))) as response:
        assert response.status == 302 and response.headers["location"] == "https://elsewhere.invalid/"
        assert b"".join([chunk async for chunk in response.chunks()]) == b"moved"
    assert len(seen) == 1 and seen[0].headers["authorization"] == "Bearer synthetic-secret"
    assert seen[0].headers["user-agent"] == "phaze-test/1" and seen[0].url.host == "catalog.example.invalid"
    assert "synthetic-secret" not in repr(socket) + repr(request(credential=("Authorization", secret)))
    await socket.aclose()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("slow"), TimeoutError),
        (httpx.ConnectError("refused"), ConnectionError),
        (httpx.RemoteProtocolError("bad"), ConnectionError),
    ],
)
async def test_httpx_socket_maps_errors_without_their_messages(error: Exception, expected: type[Exception]) -> None:
    def handler(outgoing: httpx.Request) -> httpx.Response:
        raise error

    socket = HttpxSocket(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(expected) as raised:
        async with socket.open(request()):
            pass  # pragma: no cover - the open itself raises
    assert str(raised.value) == type(error).__name__ and raised.value.__cause__ is None
    await socket.aclose()


async def test_httpx_socket_refuses_a_response_from_another_host() -> None:
    socket = HttpxSocket(httpx.AsyncClient(transport=httpx.MockTransport(lambda _outgoing: httpx.Response(200))))
    with pytest.raises(ConnectionError, match="response_host_mismatch"):
        async with socket.open(request(host="other.example.invalid")):
            pass  # pragma: no cover - the open itself raises
    await socket.aclose()


def test_default_httpx_socket_disables_redirects_and_environment() -> None:
    socket = HttpxSocket()
    client = socket._client
    assert client.follow_redirects is False and client._trust_env is False
    asyncio.run(socket.aclose())


def test_a_page_origin_other_than_one_needs_a_page_parameter_and_defaults_to_one() -> None:
    assert SEARCH.page_origin == 1
    with pytest.raises(ValidationError):
        RemoteSearchRoute(fields=frozenset({"artist"}), format="json", path="/api/search", term_parameters=(("artist", "q"),), page_origin=0)
    zero = RemoteSearchRoute(
        fields=frozenset({"artist"}), format="json", path="/api/search", term_parameters=(("artist", "q"),), page_parameter="p", page_origin=0
    )
    assert zero.page_origin == 0
    with pytest.raises(ValidationError):
        RemoteSearchRoute(
            fields=frozenset({"artist"}), format="json", path="/api/search", term_parameters=(("artist", "q"),), page_parameter="p", page_origin=2
        )  # type: ignore[arg-type]
