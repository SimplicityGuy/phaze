"""HTTP instrumentation: the route TEMPLATE, and never a raw path (phaze-m1drf.3 §2).

The interesting assertions here are the negative ones. This app serves
``/record/{file_id}`` against an 11,428-file archive and answers 404s to anything else, so
the two ways an HTTP metric explodes are a matched route reported by its concrete path and
an unmatched request reported by whatever the client asked for. Both are tested against a
real Starlette app driven by a real client, because both are properties of the
router/middleware interaction rather than of a function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from phaze.telemetry.http import UNMATCHED_ROUTE, TelemetryMiddleware


if TYPE_CHECKING:
    from tests.shared.telemetry.conftest import TelemetrySink

METRIC = "phaze.http.server.request.duration"


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware)

    @app.get("/record/{file_id}")
    async def record(file_id: str) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/boom")
    async def boom() -> None:
        msg = "deliberate"
        raise RuntimeError(msg)

    app.router.routes.append(Mount("/static", routes=[Route("/x", lambda _request: PlainTextResponse("x"))]))
    return app


def test_a_parameterised_route_reports_its_template_not_the_uuid(telemetry_sink: TelemetrySink) -> None:
    """THE cardinality assertion. One series for every file in the archive, not 11,428."""
    client = TestClient(_app())
    for file_id in ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"):
        assert client.get(f"/record/{file_id}").status_code == 200

    attribute_sets = telemetry_sink.attribute_sets(METRIC)
    routes = {attrs["http_route"] for attrs in attribute_sets}
    assert routes == {"/record/{file_id}"}
    assert telemetry_sink.count(METRIC) == 2, "two requests, one series"
    rendered = repr(attribute_sets)
    assert "1111-1111" not in rendered and "2222-2222" not in rendered


def test_an_unmatched_path_collapses_onto_one_literal(telemetry_sink: TelemetrySink) -> None:
    """A scanner probing random URLs must add observations to ONE series, not mint one per
    probe. This is the specific behaviour that ruled out the off-the-shelf instrumentation."""
    client = TestClient(_app())
    for path in ("/wp-admin.php", "/.env", "/etc/passwd", "/../../secret"):
        client.get(path)

    routes = {attrs["http_route"] for attrs in telemetry_sink.attribute_sets(METRIC)}
    assert routes == {UNMATCHED_ROUTE}


def test_the_status_is_recorded_as_a_class(telemetry_sink: TelemetrySink) -> None:
    client = TestClient(_app())
    client.get("/health")
    client.get("/nope")
    classes = {attrs["http_status_class"] for attrs in telemetry_sink.attribute_sets(METRIC)}
    assert classes == {"2xx", "4xx"}


def test_a_handler_that_raises_is_still_measured(telemetry_sink: TelemetrySink) -> None:
    """A 500 is the most interesting request on the surface; dropping its observation would
    make the failure look free and leave the duration histogram describing only successes."""
    client = TestClient(_app(), raise_server_exceptions=False)
    client.post("/boom")
    attribute_sets = telemetry_sink.attribute_sets(METRIC)
    assert attribute_sets, "the raising request produced no observation"
    assert {attrs["http_route"] for attrs in attribute_sets} == {"/boom"}


def test_a_nonstandard_method_folds_into_other(telemetry_sink: TelemetrySink) -> None:
    client = TestClient(_app())
    client.request("PROPFIND", "/health")
    assert {attrs["http_method"] for attrs in telemetry_sink.attribute_sets(METRIC)} == {"OTHER"}


def test_active_requests_returns_to_zero(telemetry_sink: TelemetrySink) -> None:
    """An up-down counter that only ever goes up is a leak that reads as saturation."""
    client = TestClient(_app())
    client.get("/health")
    client.get("/health")
    assert telemetry_sink.total("phaze.http.server.active_requests") == 0


def test_the_span_is_renamed_to_method_and_template(telemetry_sink: TelemetrySink) -> None:
    """The route is not known until the router has matched, i.e. INSIDE the middleware's
    call to the app -- so the span opens on the method alone and is renamed afterwards."""
    client = TestClient(_app())
    client.get("/record/33333333-3333-3333-3333-333333333333")
    assert "GET /record/{file_id}" in telemetry_sink.span_names()


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_pass_straight_through(scope_type: str) -> None:
    """The middleware wraps the whole app, so it sees the lifespan scope too."""
    import asyncio

    seen: list[str] = []

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    asyncio.run(TelemetryMiddleware(app)({"type": scope_type}, None, None))
    assert seen == [scope_type]
