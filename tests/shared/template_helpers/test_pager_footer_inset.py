"""Render tests for the pending-files / tracklist-sets / analyze-files pager footer inset (phaze-rkikk).

The footer sat flush against the card border ("Page 1" on the left edge, Previous/Next on the right
and bottom edges) because it carried no horizontal padding and a bare ``mt-6``. It now carries the
same ``px-6`` as the shared ``_file_table.html`` cell padding, plus ``pb-4`` below. Padding utilities
are theme independent, so one class set covers light and dark.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

from fastapi.templating import Jinja2Templates
import pytest
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_INSET = ("px-6", "pb-4")


def _request() -> Request:
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


def _render(name: str, **context: Any) -> str:
    return _templates.TemplateResponse(request=_request(), name=name, context=context).body.decode()  # type: ignore[attr-defined]


def _footer_nav(html: str, label: str) -> str:
    match = re.search(rf'<nav aria-label="{re.escape(label)}"[^>]*>', html)
    assert match, f"no pager <nav> labelled {label!r} rendered"
    return match.group(0)


def _assert_inset(nav: str) -> None:
    classes = re.search(r'class="([^"]*)"', nav)
    assert classes
    tokens = classes.group(1).split()
    for token in _INSET:
        assert token in tokens, f"pager footer lacks {token}: {nav}"
    assert "mt-6" not in tokens


def _page(**extra: Any) -> SimpleNamespace:
    base = {"rows": [], "page": 2, "page_size": 50, "has_prev": True, "has_next": True, "show_pager": True, "available": True}
    base.update(extra)
    return SimpleNamespace(**base)


def test_metadata_pending_pager_footer_is_inset() -> None:
    html = _render("pipeline/partials/_pending_files.html", pending_page=_page(), stage="metadata", host_id="host", sort=None)
    _assert_inset(_footer_nav(html, "metadata files pagination"))


def test_tracklist_sets_pager_footer_is_inset() -> None:
    html = _render("pipeline/partials/_tracklist_sets.html", sets_page=_page(), host_id="host", sort=None)
    _assert_inset(_footer_nav(html, "Tracklist sets pagination"))


@pytest.mark.parametrize("template", ["_analyze_files.html", "_pending_files.html", "_tracklist_sets.html"])
def test_pager_footer_source_carries_inset_not_bare_margin(template: str) -> None:
    """The analyze pager needs a full route context to render, so pin the footer markup at the source too."""
    source = (TEMPLATES_DIR / "pipeline" / "partials" / template).read_text()
    navs = re.findall(r'<nav aria-label="[^"]*pagination" class="([^"]*)"', source)
    assert navs, template
    for classes in navs:
        for token in _INSET:
            assert token in classes.split(), (template, classes)
