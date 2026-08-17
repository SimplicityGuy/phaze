"""Jinja-render test for phaze-74ii (phaze-nebh residue).

phaze-nebh (fixgroup-15) labeled every rail numeral in ``shell/partials/rail.html`` with a
store-bound ``:title`` + ``:aria-label`` naming exactly what the number counts (see
``tests/shared/test_rail_priority_controls.py``), but the header's "Agents · N" numeral in
``shell/partials/header.html`` was outside that batch's surface and was left title/aria-label-less
-- a sighted operator hovering it gets no tooltip, and assistive tech announces a bare number with
no explanation of what it counts. This test proves the SAME treatment landed here: an explicit,
store-bound ``:title`` + ``:aria-label`` pair that agree with each other and name the two store keys
COMPUTE-02 sums (``agentOnline`` -- heartbeating agents -- and ``computeLanesActive`` -- active
compute lanes).

Renders ``shell/partials/header.html`` through FastAPI's ``Jinja2Templates`` (the SAME safe wrapper
Phaze uses in production, matching the idiom in ``test_rail_priority_controls.py``).
"""

from __future__ import annotations

from pathlib import Path
import re

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fake_request() -> Request:
    """Minimal Starlette Request stub -- header.html reads no path/query context of its own."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": None,
    }
    return Request(scope=scope)  # type: ignore[arg-type]


def _render_header() -> str:
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="shell/partials/header.html",
        # header.html includes _force_local_pill.html, which reads force_local directly.
        context={"force_local": False},
    )
    return response.body.decode()


def test_header_names_online_agents_and_active_lanes_separately() -> None:
    """The header labels both live counts instead of presenting an ambiguous total."""
    html = _render_header()
    aria_match = re.search(r':aria-label="([^"]*agentOnline[^"]*computeLanesActive[^"]*)"', html)
    assert aria_match
    assert "agents online" in aria_match.group(1)
    assert "compute lanes active" in aria_match.group(1)
    assert "Agents online" in html
    assert "Lanes active" in html


def test_header_counts_bind_to_their_distinct_store_values() -> None:
    html = _render_header()
    assert html.count('x-text="$store.pipeline.agentOnline"') == 1
    assert html.count('x-text="$store.pipeline.computeLanesActive"') == 1
    assert 'x-text="$store.pipeline.agentOnline + $store.pipeline.computeLanesActive"' not in html
