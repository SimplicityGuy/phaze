"""A search route's page origin: logical pages stay 1-based, only the number on the wire moves.

Real transport, real ledger, FAKE socket and clock; hosts are ``.invalid``.
"""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from phaze.services.remote_transport import RemoteTransport
from phaze.tracklist_providers.domain import DiscoveryBudget, RemoteRequest, SearchTerm, SourceIdentity, SourceReference
from phaze.tracklist_providers.host_profile import RemoteHostProfile, RemoteSearchRoute
from tests.integration.test_remote_transport import FakeClock, FakeSocket


pytestmark = pytest.mark.integration


def profile(origin: int | None) -> RemoteHostProfile:
    extra: dict[str, Any] = {} if origin is None else {"page_origin": origin}
    return RemoteHostProfile(
        provider_id="example_catalog",
        host="catalog.example.invalid",
        user_agent="phaze-test/1",
        robots="not_applicable",
        search=RemoteSearchRoute(
            fields=frozenset({"artist"}),
            format="example_search_json",
            max_page=3,
            path="/api/search",
            term_parameters=(("artist", "q"),),
            page_parameter="page",
            **extra,
        ),
    )


def reference(page: int) -> SourceReference:
    return SourceReference(
        identity=SourceIdentity(provider_id="example_catalog", native_id="search:" + "a" * 64),
        channel="remote",
        format="example_search_json",
        request=RemoteRequest(kind="search", terms=(SearchTerm(field="artist", value="Example"),), page=page),
    )


async def wire_pages(engine: Any, origin: int | None) -> list[str]:
    clock = FakeClock()
    socket = FakeSocket(clock)
    run = RemoteTransport(profiles=(profile(origin),), engine=engine, socket=socket, clock=clock).run()
    for page in (1, 2, 3):
        await run.read(reference(page), DiscoveryBudget())
    return [parse_qs(urlsplit(request.url).query)["page"][0] for request in socket.requests]


async def test_the_default_origin_leaves_every_existing_routes_query_unchanged(committed_db: Any) -> None:
    engine, _ = committed_db
    assert await wire_pages(engine, None) == ["1", "2", "3"]
    search = profile(None).search
    assert search is not None and search.page_origin == 1


async def test_origin_zero_sends_the_first_logical_page_as_zero_and_never_a_negative_page(committed_db: Any) -> None:
    engine, _ = committed_db
    sent = await wire_pages(engine, 0)
    assert sent == ["0", "1", "2"] and all(int(page) >= 0 for page in sent)
