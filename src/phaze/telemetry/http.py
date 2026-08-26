"""HTTP server instrumentation -- a pure-ASGI middleware phaze owns, on purpose.

``opentelemetry-instrumentation-fastapi`` exists and is deliberately NOT used. Its
``http.route`` attribute falls back to the RAW PATH when no route matched, so the first
404 scan against this deployment would mint one Prometheus series per probed path, and
``/record/<a-real-file-uuid>`` -- a real, matched route in this app -- is exactly the shape
that puts an archive identifier into a label. phaze does not own the Prometheus that would
store it (phaze-m1drf.3 acceptance 2), so the label set is not something to inherit from a
library's defaults.

This middleware reports the route TEMPLATE (``/record/{file_id}``) and, for anything that
matched no route at all, the literal ``__unmatched__``. Those are the only two shapes it
can emit; there is no path through it that reaches a raw URL.

**How the template is recovered, verified in THIS environment rather than assumed.**
Starlette 1.6.0's router writes only ``endpoint`` and ``path_params`` into the ASGI scope
-- it does NOT write ``scope["route"]`` (checked against the installed
``starlette/routing.py``: ``child_scope = {"endpoint": self.endpoint, "path_params": ...}``).
So the template is recovered by mapping the matched ``endpoint`` callable back to the
``path_format`` of the route that declared it, using a table built once from ``app.routes``.
Two routes sharing one endpoint function collapse onto whichever was declared first; that
is a bounded, documented imprecision and never an unbounded label.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from phaze.telemetry.instruments import add, record
from phaze.telemetry.tracing import span


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: What a request that matched no route reports. A literal, so a scanner hammering random
#: paths adds observations to ONE series instead of one series per path.
UNMATCHED_ROUTE = "__unmatched__"

_STANDARD_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _status_class(status: int | None) -> str:
    """Round a status to its class; ``error`` when the app raised before responding."""
    if status is None:
        return "error"
    if 100 <= status < 600:
        return f"{status // 100}xx"
    return "error"


def _route_table(routes: Any, prefix: str = "") -> dict[int, str]:
    """Map ``id(endpoint callable)`` -> route template, recursing through mounts.

    Keyed on ``id`` rather than the callable itself because a Starlette endpoint may be an
    unhashable partial or a bound method whose identity is recreated per access; the table
    is built once from objects the app holds for its whole life, so the ids stay valid.
    """
    table: dict[int, str] = {}
    for route in routes:
        path_format = prefix + str(getattr(route, "path_format", getattr(route, "path", "")))
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            table.setdefault(id(endpoint), path_format)
        mounted = getattr(route, "routes", None)
        if mounted:
            mount_app = getattr(route, "app", None)
            if mount_app is not None:
                # A Mount reports ITSELF as the endpoint for everything below it, so the
                # mount's own prefix is the only template its children can be attributed to
                # without re-implementing Starlette's matching.
                table.setdefault(id(mount_app), path_format)
            for child_id, child_path in _route_table(mounted, path_format).items():
                table.setdefault(child_id, child_path)
    return table


class TelemetryMiddleware:
    """Pure-ASGI middleware: request duration, in-flight count, and a server span.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because the latter runs the downstream
    app in a task group and buffers the response body through a stream -- overhead paid on
    every request, on a surface whose heaviest partial is already a 534 ms poll fired every
    five seconds by every open admin tab (phaze-zaf2l section 4a).

    Non-HTTP scopes (``lifespan``, ``websocket``) pass straight through untouched.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app
        self._table: dict[int, str] | None = None

    def _template_for(self, scope: dict[str, Any]) -> str:
        endpoint = scope.get("endpoint")
        if endpoint is None:
            return UNMATCHED_ROUTE
        if self._table is None:
            # Built lazily: at __init__ time this middleware wraps an app whose routers
            # may not all be included yet (phaze's factory adds routers after building the
            # app), so a table built then would be missing most of them.
            root = scope.get("app")
            self._table = _route_table(getattr(getattr(root, "router", None), "routes", []) or [])
        return self._table.get(id(endpoint), UNMATCHED_ROUTE)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_method = str(scope.get("method", "")).upper()
        method = raw_method if raw_method in _STANDARD_METHODS else "OTHER"
        status: int | None = None

        async def _send(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 0))
            await send(message)

        started = time.perf_counter()
        add("phaze.http.server.active_requests", 1)
        # The span name is the METHOD only at this point -- the route is not known until
        # the router has matched, which happens inside `self.app`. It is renamed below.
        with span("http.server.request", {"http.request.method": method}) as current:
            try:
                await self.app(scope, receive, _send)
            finally:
                add("phaze.http.server.active_requests", -1)
                route = self._template_for(scope)
                record(
                    "phaze.http.server.request.duration",
                    time.perf_counter() - started,
                    http_method=method,
                    http_route=route,
                    http_status_class=_status_class(status),
                )
                if current.is_recording():
                    current.update_name(f"{method} {route}")
                    current.set_attributes({"http.route": route, "http.response.status_code": status or 0})
