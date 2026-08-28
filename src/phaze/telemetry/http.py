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

**How the template is recovered, verified against THIS app rather than assumed.** FastAPI's
router writes the matched ``APIRoute`` into ``scope["route"]``, and that object carries
``path_format`` -- the template. That is the primary and it is what phaze's real app uses.

The fallback exists because a **plain Starlette route or Mount does not set
``scope["route"]``** (checked against the installed Starlette 1.6.0:
``routing.py`` writes only ``{"endpoint": ..., "path_params": ...}`` into the child scope),
and this app mounts ``/static`` that way. For those, the matched ``endpoint`` is mapped back
to the path of the route that declared it, using a table built once from the app's routes.

**Both halves were earned.** A first version had only the table, was tested against a
hand-built Starlette app, and passed -- while recovering exactly FOUR templates from
phaze's real 36-route app, because this FastAPI version keeps included routers as lazy
``_IncludedRouter`` objects rather than flattening them into ``Route``s. Every request would
have reported ``__unmatched__`` in production. ``test_http_middleware.py`` now drives the
REAL ``create_app()``, which is the only version of this test that could have failed.

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


def _mount_table(routes: Any, prefix: str = "") -> dict[int, str]:
    """Map ``id(endpoint)`` -> path, for the routes that do NOT set ``scope["route"]``.

    Only a fallback: FastAPI's own routes are recovered from ``scope["route"]`` directly.
    This exists for plain Starlette ``Route``s and ``Mount``s -- ``/static`` here -- where the
    scope carries an ``endpoint`` and nothing else.

    Recurses through ``original_router`` as well as ``routes``, because this FastAPI version
    represents an included router as a lazy ``_IncludedRouter`` holding the original router
    rather than as flattened ``Route`` objects.
    """
    table: dict[int, str] = {}
    for route in routes:
        path_format = prefix + str(getattr(route, "path_format", getattr(route, "path", "")) or "")
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            table.setdefault(id(endpoint), path_format)
        mount_app = getattr(route, "app", None)
        if mount_app is not None and getattr(route, "routes", None) is not None:
            table.setdefault(id(mount_app), path_format)
        nested = getattr(route, "routes", None)
        if nested is None:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None)
        if nested:
            for child_id, child_path in _mount_table(nested, path_format).items():
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
        """The matched route TEMPLATE, or ``__unmatched__``. Never a raw path."""
        route = scope.get("route")
        if route is not None:
            template = getattr(route, "path_format", None) or getattr(route, "path", None)
            if template:
                return str(template)
        endpoint = scope.get("endpoint")
        if endpoint is None:
            return UNMATCHED_ROUTE
        if self._table is None:
            # Built lazily: at __init__ time this middleware wraps an app whose routers may
            # not all be included yet (phaze's factory adds routers after building the app),
            # so a table built then would be missing most of them.
            root = scope.get("app")
            self._table = _mount_table(getattr(getattr(root, "router", None), "routes", []) or [])
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
