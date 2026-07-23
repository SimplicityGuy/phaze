"""Jinja-render tests for the copy-pasted `>7-page` pagers (phaze-rv40 / phaze-7jbt / phaze-hb0a).

Each of ``pipeline/partials/_list_pager.html``, ``duplicates/partials/pagination.html`` and
``proposals/partials/pagination.html`` builds its "pages around current" window with
``range(max(current-1, 2), min(current+2, total_pages) + 1)`` and THEN unconditionally renders a
standalone "always show last page" button. Whenever ``current_page >= total_pages - 2`` the window's
own upper bound already reaches ``total_pages``, so the standalone button re-emits it: the last page
number renders twice, and on the actual final page BOTH copies carry the active-page highlight
(``aria-current="page"`` on the pipeline pager; the ``bg-blue-600`` class on all three).

Renders the templates directly (the same technique ``test_progress_partial.py`` uses) rather than
seeding 175+ DB rows to reach the `>7-page` branch through a real request -- the defect is pure
Jinja loop arithmetic, so exercising it with a synthetic ``Pagination`` is both faster and a more
direct regression test than an equivalent HTTP round trip.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from phaze.routers.view_state import ListViewState
from phaze.services.proposal_queries import Pagination


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "src" / "phaze" / "templates"

# Reuse the production-style ``Jinja2Templates`` wrapper so autoescape matches production exactly.
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fake_request() -> Request:
    """Minimal Starlette Request stub -- only needed because ``TemplateResponse`` injects it."""
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


def _render_pipeline_pager(*, page: int, page_size: int, total: int) -> str:
    """Render the v7 propose-workspace pager (phaze-rv40)."""
    pagination = Pagination(page=page, page_size=page_size, total=total)
    view = ListViewState(page=page, page_size=page_size)
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/_list_pager.html",
        context={
            "pagination": pagination,
            "view": view,
            "pager_url": "/s/propose",
            "pager_target": "#propose-workspace-list",
            "page_size_choices": (25, 50, 100),
        },
    )
    return response.body.decode()


# ---------------------------------------------------------------------------
# pipeline/partials/_list_pager.html -- phaze-rv40
# ---------------------------------------------------------------------------


def test_pipeline_pager_last_page_button_renders_once_on_the_final_page() -> None:
    """total_pages=8, current=8: the window's own last value is 8 -- must not be re-emitted."""
    html = _render_pipeline_pager(page=8, page_size=25, total=200)
    assert html.count(">8</button>") == 1, "the last page button must render exactly once"
    # On the final page every duplicate would carry aria-current="page"; must be exactly one.
    assert html.count('aria-current="page"') == 1
    assert html.count("bg-blue-600") == 1


def test_pipeline_pager_last_page_button_renders_once_near_the_end() -> None:
    """current=6 or 7 of 8 pages: the window overlaps the standalone last-page button too."""
    for page in (6, 7):
        html = _render_pipeline_pager(page=page, page_size=25, total=200)
        assert html.count(">8</button>") == 1, f"duplicate '8' button at current_page={page}"


def test_pipeline_pager_unaffected_when_current_page_is_not_near_the_end() -> None:
    """A window nowhere near the last page never touches this overlap -- sanity check."""
    html = _render_pipeline_pager(page=1, page_size=25, total=200)
    assert html.count(">8</button>") == 1
    assert html.count('aria-current="page"') == 1
