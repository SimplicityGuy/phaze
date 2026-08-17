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

``app_route_table`` (phaze-u2p8s) is that same walk narrowed to ``APIRoute`` and exploded
one row per HTTP method, which is the granularity a reachability check needs: ``DELETE
/pipeline/scans/{batch_id}`` and ``GET /pipeline/scans/{batch_id}`` share a path but are
two independently-reachable endpoints, and losing the UI caller of one says nothing about
the other. ``resolve_reference`` matches a URL harvested from a template (or from a router
that hands a URL to a template) against that table segment by segment, treating both a
route's ``{param}`` and a reference's unresolvable ``{{ ... }}`` hole as single-segment
wildcards.

Enumerating from the live app object is load-bearing rather than incidental: routers get
decomposed into packages and endpoints get re-homed (``/pipeline/files`` → ``/s/files``),
so any check that globs router modules or reads ``@router.get`` decorators measures the
source layout instead of the served URL space. The app is the only truthful source.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Any

from fastapi.routing import APIRoute


if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI


# Sentinel standing in for a path segment (or run of segments) whose value is only known at
# render time -- a ``{{ jinja }}`` hole in a template attribute, or an f-string
# ``{placeholder}`` in a router-built URL. It is deliberately a character that cannot occur
# in a URL, so it never collides with a literal segment.
WILDCARD = "\x00"


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


@dataclass(frozen=True, order=True)
class AppRoute:
    """One (path, method) endpoint of the live app -- the unit of reachability."""

    path: str
    method: str
    name: str = ""

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


def app_route_table(app: FastAPI) -> tuple[AppRoute, ...]:
    """Every application-owned endpoint as a sorted ``(path, method)`` table.

    Only ``APIRoute`` instances are returned. That excludes exactly two things, both of
    which the application does not own and neither of which a template is expected to
    call: the ``StaticFiles`` ``Mount`` at ``/static``, and FastAPI's own auto-generated
    ``/docs`` / ``/redoc`` / ``/openapi.json`` documentation endpoints, which Starlette
    registers as plain ``Route`` objects. Scoping structurally is stronger than
    allowlisting them by path -- there is no list to keep in sync when FastAPI adds or
    renames a built-in.
    """
    table = {
        AppRoute(path=route.path, method=method, name=getattr(route, "name", "") or "")
        for route in iter_effective_routes(app)
        if isinstance(route, APIRoute)
        for method in (route.methods or ())
    }
    return tuple(sorted(table))


def _segments(path: str) -> list[str]:
    """Split a URL path into segments, ignoring the leading/trailing slash.

    Trailing slashes are not significant here: Starlette's ``redirect_slashes`` resolves
    ``/cue`` onto ``/cue/`` in one hop, so a template linking either form reaches the route.
    """
    stripped = path.strip("/")
    return stripped.split("/") if stripped else []


def _segment_matches(ref: str, route: str) -> bool:
    if route.startswith("{") and route.endswith("}"):
        # A path parameter accepts any single segment, however the reference spells it.
        return True
    if WILDCARD not in ref:
        return ref == route
    if ref == WILDCARD:
        return True
    # A PARTIAL wildcard keeps its literal text as a constraint. `/pipeline/pending-files{{
    # _retry_qs }}` must match `/pipeline/pending-files` and NOT `/pipeline/stats`; treating
    # any wildcard-bearing segment as a free match silently marked unrelated siblings
    # reachable, which is precisely the orphan this check is for.
    pattern = "[^/]*".join(re.escape(literal) for literal in ref.split(WILDCARD))
    return re.fullmatch(pattern, route) is not None


def reference_matches(reference: str, route_path: str) -> bool:
    """Does a harvested URL reference resolve to ``route_path``?

    A segment matches when it is literally equal, when the route declares it as a
    ``{param}``, or when the reference resolves it only at render time -- wholly
    (``WILDCARD``, matching any segment) or partially (literal text around a ``WILDCARD``,
    which still has to match).

    The wildcard is permissive in one direction and that is a documented limit, not an
    oversight: a reference like ``/pipeline/{{ endpoints[stage] }}/retry`` marks *both*
    ``/pipeline/analysis-failed/retry`` and ``/pipeline/metadata-failed/retry`` as
    reachable, so if the lookup table stopped producing one of the two, the orphan check
    would not notice. Narrowing that would mean evaluating template expressions, which needs
    render-time context this check deliberately does not have.

    A wildcard never spans a ``/``: a reference and a route must have the same segment
    count. An expression that renders multiple segments therefore resolves to nothing and
    reads as dangling -- none exist today, and the loud failure is the right way to find out
    that one has appeared.
    """
    ref_parts, route_parts = _segments(reference), _segments(route_path)
    if len(ref_parts) != len(route_parts):
        return False
    return all(_segment_matches(ref, route) for ref, route in zip(ref_parts, route_parts, strict=True))


def resolve_reference(reference: str, method: str | None, table: tuple[AppRoute, ...]) -> tuple[AppRoute, ...]:
    """Every endpoint in ``table`` a reference can reach.

    ``method`` is ``None`` for references whose verb is not statically knowable -- a URL
    handed to a Jinja macro or built in a router, where the ``hx-*`` attribute that will
    carry it lives somewhere else. Those resolve against any method, which is the honest
    reading: the URL demonstrably names that path, and nothing more.
    """
    return tuple(route for route in table if reference_matches(reference, route.path) and (method is None or method == route.method))
