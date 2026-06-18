"""Route-introspection helpers that survive FastAPI's lazy router inclusion.

FastAPI 0.137 changed ``app.include_router(...)`` from eagerly flattening the
sub-router's ``APIRoute`` objects into ``app.routes`` to inserting a single lazy
``_IncludedRouter`` placeholder (``fastapi.routing._IncludedRouter``, a
``BaseRoute`` subclass that resolves its effective routes only at request-match
time). That placeholder has no ``.path`` attribute, so the pre-0.137 idiom

    paths = [route.path for route in app.routes]

now raises ``AttributeError`` (or silently misses every included route when
guarded with ``hasattr(route, "path")``). The production app is unaffected —
routing resolves fine at request time — but tests that introspect ``app.routes``
to assert a router is wired need to expand the placeholder.

``iter_effective_routes`` walks ``app.routes`` and descends into any include
placeholder via its public ``original_router.routes`` (the ``APIRouter`` that was
included), recursing through nested includes. Each included ``APIRoute`` already
carries its full path (its own router prefix is baked into ``APIRoute.path``), so
this is correct as long as routers are included WITHOUT an extra ``prefix=`` at
include time — which is how ``phaze.main.create_app`` wires every router (each
router declares its own prefix). The walker also works unchanged on older
FastAPI, where ``app.routes`` already holds flat ``APIRoute`` objects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


def iter_effective_routes(app_or_routes: Any) -> Iterator[Any]:
    """Yield every leaf route (objects exposing ``.path``) reachable from an app.

    Accepts either a ``FastAPI`` app or a list of routes. Lazy ``_IncludedRouter``
    placeholders (no ``.path``, but a ``.original_router`` with ``.routes``) are
    expanded recursively; everything else is yielded as-is.
    """
    routes = getattr(app_or_routes, "routes", app_or_routes)
    for route in routes:
        if hasattr(route, "path"):
            yield route
        else:
            included = getattr(route, "original_router", None)
            if included is not None and hasattr(included, "routes"):
                yield from iter_effective_routes(included.routes)


def effective_route_paths(app: FastAPI) -> set[str]:
    """Return the set of every registered route path, including lazily-included routers."""
    return {route.path for route in iter_effective_routes(app)}
