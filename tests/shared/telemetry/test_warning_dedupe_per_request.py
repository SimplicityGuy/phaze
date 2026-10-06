"""A warning raised on every phaze-api request is emitted ONCE, with telemetry on (phaze-0gwqr).

Python shows a ``default``-action warning once per code location by recording it in the
caller module's ``__warningregistry__``. Any mutation of ``warnings.filters`` invalidates
EVERY module's registry, so the next occurrence is shown again as if it were new.

FastAPI 0.142's native telemetry calls ``trace.get_tracer("fastapi", ...)`` on every HTTP
request (``fastapi/telemetry/_asgi.py``), and opentelemetry-sdk 1.45's
``TracerProvider.get_tracer`` calls ``warnings.filterwarnings(...)`` on every call
(``opentelemetry/sdk/trace/__init__.py``) -- re-inserting the same filter, which still
counts as a mutation. Together they reset the dedupe once per request: the kr8s version
warning printed 3,201 times in phaze-api before phaze-lhunw bounded that one caller.

Everything here is the real thing: phaze's own ``configure_telemetry("api")`` installs the
provider exactly as production does, ``phaze.main.create_app()`` is the app, and a real
ASGI client drives it. The endpoint is the black hole the bootstrap tests use, so the
OTLP exporter never delivers and never blocks the request path.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
import warnings

from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
import pytest

from phaze.telemetry import _env, bootstrap, tracing
from tests.shared.telemetry.conftest import reset_otel_globals
from tests.shared.telemetry.test_telemetry_bootstrap import BLACK_HOLE


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.trace import TracerProvider

REQUESTS = 5
MESSAGE = "phaze-0gwqr probe: raised on every request from one location"


class RepeatedWarning(UserWarning):
    """Its own category, so nothing else in the process can be mistaken for it."""


def _warn_from_one_location() -> None:
    warnings.warn(MESSAGE, RepeatedWarning, stacklevel=1)


@pytest.fixture
def api_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    """phaze's own api-role provider, plus an in-memory processor proving FastAPI used it."""
    monkeypatch.setenv("PHAZE_ROLE", "api")
    monkeypatch.setenv(_env.ENDPOINT_ENV, BLACK_HOLE)
    bootstrap._reset_for_tests()
    reset_otel_globals()
    tracing._reset_for_tests()
    assert bootstrap.configure_telemetry("api") is True
    provider = trace.get_tracer_provider()
    assert provider is bootstrap._tracer_provider
    assert isinstance(provider, bootstrap._resolved_once_tracer_provider())
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider, exporter
    finally:
        bootstrap.shutdown_telemetry(200)
        bootstrap._reset_for_tests()
        reset_otel_globals()
        tracing._reset_for_tests()


async def _drive(paths: list[str]) -> list[int]:
    from phaze.main import create_app  # after the env and the provider are in place

    app = create_app()

    @app.get("/__phaze_0gwqr_warns")
    async def warns() -> dict[str, str]:
        _warn_from_one_location()
        return {"ok": "yes"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://phaze.test") as client:
        return [(await client.get(path)).status_code for path in paths]


def test_a_warning_raised_on_every_request_is_emitted_once(api_provider: tuple[TracerProvider, InMemorySpanExporter]) -> None:
    _, exporter = api_provider
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        statuses = asyncio.run(_drive(["/__phaze_0gwqr_warns"] * REQUESTS))

    assert statuses == [200] * REQUESTS
    # Not vacuous: FastAPI's native telemetry DID run on every request, against phaze's
    # provider. With telemetry off it never calls get_tracer, and this test would pass for
    # the wrong reason.
    server_spans = [span for span in exporter.get_finished_spans() if span.instrumentation_scope.name == "fastapi" and span.kind is SpanKind.SERVER]
    assert len(server_spans) == REQUESTS, [(span.instrumentation_scope.name, span.kind, span.name) for span in exporter.get_finished_spans()]

    emitted = [warning for warning in caught if issubclass(warning.category, RepeatedWarning)]
    assert len(emitted) == 1, f"a once-per-location warning was emitted {len(emitted)} times over {REQUESTS} requests"


def test_resolving_a_tracer_again_leaves_the_warning_filters_alone(api_provider: tuple[TracerProvider, InMemorySpanExporter]) -> None:
    """The mechanism, directly: a repeat ``get_tracer`` is a lookup, not a filter mutation."""
    provider, _ = api_provider
    first = provider.get_tracer("fastapi", "0.0", schema_url="https://opentelemetry.io/schemas/1.44.0")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        for _ in range(REQUESTS):
            again = provider.get_tracer("fastapi", "0.0", schema_url="https://opentelemetry.io/schemas/1.44.0")
            assert again is first
            _warn_from_one_location()
    assert len([warning for warning in caught if issubclass(warning.category, RepeatedWarning)]) == 1


def test_distinct_scopes_still_get_distinct_tracers() -> None:
    """Resolving once must not collapse scopes: each still reports under its own name and version."""
    provider = bootstrap._resolved_once_tracer_provider()(shutdown_on_exit=False)
    fastapi_tracer = provider.get_tracer("fastapi", "1")
    assert provider.get_tracer("fastapi", "2") is not fastapi_tracer
    assert provider.get_tracer("phaze") is not fastapi_tracer
    assert provider.get_tracer("fastapi", "1", schema_url="https://example.invalid/s") is not fastapi_tracer
    assert provider.get_tracer("fastapi", "1") is fastapi_tracer


def test_a_scope_with_attributes_is_resolved_by_the_sdk_itself() -> None:
    """An attribute mapping is unhashable, so it bypasses the cache -- and still gets the SDK's answer."""
    provider = bootstrap._resolved_once_tracer_provider()(shutdown_on_exit=False)
    tracer = provider.get_tracer("fastapi", "1", attributes={"k": "v"})
    assert tracer is provider.get_tracer("fastapi", "1", attributes={"k": "v"})
    assert tracer is not provider.get_tracer("fastapi", "1")
    assert provider._resolved.keys() == {("fastapi", "1", None)}
