"""Standalone header health pill first-paint, count naming and disclosure contract."""

from __future__ import annotations

from pathlib import Path
import re

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from phaze.web.template_globals import register_format_filters


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_format_filters(_templates.env)


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


def test_health_pill_renders_without_seed_and_names_unknown_state() -> None:
    """Header remains independently renderable with only its routing override context."""
    html = _render_header()
    title = re.search(r':title="([^"\n]*attentionCount[^"\n]*)"', html)
    aria = re.search(r':aria-label="([^"\n]*attentionCount[^"\n]*)"', html)
    assert title and aria
    assert title.group(1) == aria.group(1)
    assert "attentionKnown" in title.group(1)
    assert "seedKnown" in title.group(1)
    assert "Health</span>" in html
    assert ">—</span>" in html
    assert 'aria-controls="attention-tray"' in html
    assert 'aria-expanded="false"' in html


def test_header_uses_one_lazy_health_pill_instead_of_inventory_strip() -> None:
    html = _render_header()
    assert 'hx-get="/shell/attention" hx-trigger="attention:load"' in html
    assert "formatCount($store.pipeline.attentionCount)" in html
    assert "Lanes&nbsp;" not in html
    assert "Clusters&nbsp;" not in html
    assert "Agents online" not in html
    assert 'role="dialog"' not in html
    assert "x-trap" not in html
    assert '@keydown.escape.window="closeAttention()"' in html
    assert "open = false" not in html
