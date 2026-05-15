"""Jinja-render tests for _partials/cross_fs_fingerprint_notice.html (Phase 28 D-14 / TASK-04).

Targets 28-V-24 -- dismissible Alpine.js info banner partial on the Duplicate
Resolution page disclosing the v4.0 per-file-server fingerprint-index
limitation (XAGENT-01 deferred). The banner is per-session dismissible only
(no ``localStorage``) so the disclosure re-appears on every page load.

Uses FastAPI's ``Jinja2Templates`` so the test renderer matches production
autoescape configuration (default-on for ``.html`` templates).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fake_request() -> Request:
    """Minimal Starlette Request stub for Jinja2Templates render contract."""
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


def _render_banner() -> str:
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="_partials/cross_fs_fingerprint_notice.html",
        context={},
    )
    return response.body.decode()


# ---------------------------------------------------------------------------
# Banner Alpine.js dismissal state
# ---------------------------------------------------------------------------


def test_banner_renders_with_alpine_x_data() -> None:
    """The banner container carries ``x-data="{ open: true }"`` and ``x-show="open"`` per UI-SPEC C3."""
    html = _render_banner()
    assert 'x-data="{ open: true }"' in html
    assert 'x-show="open"' in html


def test_banner_has_role_status_not_alert() -> None:
    """UI-SPEC C3: ``role="status"`` (informational) -- NOT ``role="alert"`` (urgent).

    The limitation is by-design, not a problem, so screen readers must
    announce it as a polite update, not as an interruption.
    """
    html = _render_banner()
    assert 'role="status"' in html
    assert 'role="alert"' not in html


def test_banner_uses_info_glyph_not_warning_glyph() -> None:
    """UI-SPEC C3 + PATTERNS S7: info glyph ``&#9432;`` -- NOT the warning glyph ``&#9888;``."""
    html = _render_banner()
    assert "&#9432;" in html
    assert "&#9888;" not in html


def test_banner_has_dismiss_button_with_aria_label() -> None:
    """UI-SPEC C3 dismiss button: ``aria-label="Dismiss notice"`` + Alpine ``@click="open = false"``."""
    html = _render_banner()
    assert 'aria-label="Dismiss notice"' in html
    assert '@click="open = false"' in html


def test_banner_has_no_localstorage_reference() -> None:
    """CONTEXT.md D-14 is explicit: no ``localStorage`` anywhere in the partial source.

    Read the source file directly -- the file content is the contract; a
    server-rendered HTML check would miss a localStorage write hidden in an
    Alpine ``x-init`` attribute (or any sibling attribute) that produces no
    visible content.
    """
    partial = TEMPLATES_DIR / "_partials" / "cross_fs_fingerprint_notice.html"
    source = partial.read_text(encoding="utf-8")
    assert "localstorage" not in source.lower()


# ---------------------------------------------------------------------------
# Banner copy (UI-SPEC Copywriting Contract)
# ---------------------------------------------------------------------------


def test_banner_heading_copy() -> None:
    """UI-SPEC heading: ``Fingerprint matches are file-server-scoped``."""
    html = _render_banner()
    assert "Fingerprint matches are file-server-scoped" in html


def test_banner_xagent_disclosure_copy() -> None:
    """UI-SPEC body paragraph names the v4.0 limitation: ``not supported in v4.0``."""
    html = _render_banner()
    assert "not supported in v4.0" in html


# ---------------------------------------------------------------------------
# Inclusion contract: duplicates/list.html includes the partial
# ---------------------------------------------------------------------------


def test_duplicates_list_includes_banner() -> None:
    """``duplicates/list.html`` must include the banner partial above its ``<h1>``."""
    duplicates_list = TEMPLATES_DIR / "duplicates" / "list.html"
    source = duplicates_list.read_text(encoding="utf-8")
    assert "_partials/cross_fs_fingerprint_notice.html" in source
