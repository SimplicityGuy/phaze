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


def test_agents_numeral_has_matching_title_and_aria_label() -> None:
    """The 'Agents · N' numeral's :title and :aria-label are BOTH present, bound, and identical."""
    html = _render_header()

    title_match = re.search(r':title="([^"]*agentOnline[^"]*computeLanesActive[^"]*)"', html)
    aria_match = re.search(r':aria-label="([^"]*agentOnline[^"]*computeLanesActive[^"]*)"', html)
    assert title_match, "the header Agents numeral is missing a :title naming what it counts"
    assert aria_match, "the header Agents numeral is missing an :aria-label naming what it counts"
    assert title_match.group(1) == aria_match.group(1), (
        "the Agents numeral's :title and :aria-label disagree -- a title and an aria-label that say different things is worse than neither"
    )

    # Names BOTH store keys COMPUTE-02 sums into the total, not just a generic "agents" label.
    assert "$store.pipeline.agentOnline" in title_match.group(1)
    assert "$store.pipeline.computeLanesActive" in title_match.group(1)


def test_agents_numeral_title_and_aria_label_are_bound_not_static() -> None:
    """The :title / :aria-label must be Alpine-bound expressions, not a static string (must track live count)."""
    html = _render_header()

    numeral = re.search(r"<span\b[^>]*\bx-text=\"\$store\.pipeline\.agentOnline \+ \$store\.pipeline\.computeLanesActive\"[^>]*>", html, re.DOTALL)
    assert numeral, "expected the x-text numeral summing agentOnline + computeLanesActive"
    tag = numeral.group(0)
    assert ":title=" in tag, "the Agents numeral must bind :title (not a plain title=) so it tracks the live count"
    assert ":aria-label=" in tag, "the Agents numeral must bind :aria-label (not a plain aria-label=) so it tracks the live count"
