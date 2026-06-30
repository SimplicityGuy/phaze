"""SHELL-05 -- legacy bookmark resolution into the v7.0 shell.

Every legacy canonical (trailing-slash) route must resolve in ≤1 hop to a live shell
(200) with the matching rail node, and an in-page HX filter on a legacy route must NOT
be hijacked by the redirect (D-01 -- the app stays fully usable through cutover).

The conditional-redirect contract (Plan 57-04): a plain (non-HX) GET to a legacy
render-in-shell route 302-redirects to its canonical ``/s/<stage>``; ``/pipeline/`` and
``/search/`` are true renames (→ ``/`` and ``/?palette=1``). The existing
``HX-Request == "true"`` filter branch is left intact, so a filter keystroke returns the
filter partial, not a redirect.

≤1-hop caveat (Pitfall 4): ``redirect_slashes=True`` (Starlette default). We assert the
CANONICAL trailing-slash forms (``/proposals/``) -- they resolve in a single hop. The
no-slash form is a framework-level 2-hop that still terminates.

Routes are enumerated via ``tests/_route_introspection`` (never ``app.routes`` directly --
FastAPI 0.138 lazy-includes routers behind ``_IncludedRouter`` placeholders).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.main import create_app
from tests._route_introspection import effective_route_paths


if TYPE_CHECKING:
    from httpx import AsyncClient


# Legacy canonical (trailing-slash) route -> expected shell target (D-03).
# The first 6 are render-in-shell redirects to /s/<stage>; the last 2 are true renames.
CANONICAL: dict[str, str] = {
    "/proposals/": "/s/propose",
    "/tracklists/": "/s/tracklist",
    "/tags/": "/s/tagwrite",
    "/cue/": "/s/cue",
    "/duplicates/": "/s/dedupe",
    "/preview/": "/s/move",
    "/pipeline/": "/",
    "/search/": "/",  # → /?palette=1 (the ⌘K auto-open hook); target after stripping the query
}


def test_legacy_routes_registered() -> None:
    """Every legacy canonical route this plan redirects is actually wired into the app.

    Uses ``effective_route_paths`` (the lazy-include-aware introspector), NEVER
    ``app.routes`` directly -- so a removed/renamed legacy route would surface here rather
    than as a silent 404 in the redirect assertions below.
    """
    paths = effective_route_paths(create_app())
    for legacy in CANONICAL:
        assert legacy in paths, f"legacy route {legacy} is no longer registered"


@pytest.mark.asyncio
@pytest.mark.parametrize(("legacy", "target"), CANONICAL.items())
async def test_legacy_route_redirects_one_hop(client: AsyncClient, legacy: str, target: str) -> None:
    """SHELL-05 -- a plain (non-HX) GET to a legacy route reaches the shell in ≤1 hop (200)."""
    # follow_redirects=False to count hops: a single 302/307 straight to the canonical target.
    first = await client.get(legacy, follow_redirects=False)
    assert first.status_code in (302, 307), f"{legacy} did not redirect (got {first.status_code})"
    assert first.headers["location"].split("?")[0] == target, f"{legacy} redirected to {first.headers['location']!r}, expected {target}"

    # follow_redirects=True: the bookmark lands on a live shell (the matching rail node
    # pre-selected -- e.g. the workspace data-stage / aria-current the shell route renders).
    final = await client.get(legacy, follow_redirects=True)
    assert final.status_code == 200, f"{legacy} did not resolve to a 200 shell page"


@pytest.mark.asyncio
async def test_hx_filter_not_redirected(client: AsyncClient) -> None:
    """SHELL-05 / D-01 -- an in-page HX filter on a legacy route is NOT hijacked by the redirect.

    A filter keystroke arrives with ``HX-Request: true`` and must keep returning the
    existing filter partial (200, content-only) so the app stays fully usable through
    cutover -- the redirect fires ONLY when ``HX-Request`` is absent.
    """
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    # NOT a redirect -- the conditional guard let the existing HX-filter branch run.
    assert response.status_code not in (302, 307), "an HX filter request must not be redirected (D-01)"
    assert response.status_code == 200
    # The filter partial is content-only: no full-document wrapper (the shell chrome persists).
    assert "<html" not in response.text
