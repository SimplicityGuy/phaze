"""Jinja-render tests for pipeline/partials/processing_card.html (Phase 34 Plan 03).

Targets VALIDATION 34-03-01 (busy renders bar + queued/active text; idle renders
empty) and 34-03-02 (the DB-derived percent drives the inline bar width; the
divide-by-zero guard renders empty without error).

Uses FastAPI's ``Jinja2Templates`` so the test renderer matches the production
autoescape configuration exactly (``.html``-suffix-driven autoescape default),
mirroring tests/test_template_helpers/test_progress_partial.py.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fake_request() -> Request:
    """Minimal Starlette Request stub; Jinja2Templates wraps every render with ``request``."""
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


def _render(
    *,
    agent_busy: int = 0,
    agent_queued: int = 0,
    agent_active: int = 0,
    controller_busy: int = 0,
    controller_queued: int = 0,
    controller_active: int = 0,
    queue_progress_percent: int = 0,
    stats: dict[str, int] | None = None,
    oob_counts: bool = False,
) -> str:
    """Render pipeline/partials/processing_card.html and return the decoded body."""
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/processing_card.html",
        context={
            "agent_busy": agent_busy,
            "agent_queued": agent_queued,
            "agent_active": agent_active,
            "controller_busy": controller_busy,
            "controller_queued": controller_queued,
            "controller_active": controller_active,
            "queue_progress_percent": queue_progress_percent,
            "stats": stats or {"analyzed": 0},
            "oob_counts": oob_counts,
        },
    )
    return response.body.decode()


# ---------------------------------------------------------------------------
# 34-03-01: busy renders bar + counts
# ---------------------------------------------------------------------------


def test_card_busy_shows_bar_and_counts() -> None:
    """agent_busy>0 -> bar at 75% width + '7 queued · 3 active' text (middle-dot)."""
    html = _render(
        agent_busy=10,
        agent_queued=7,
        agent_active=3,
        controller_busy=0,
        queue_progress_percent=75,
        stats={"analyzed": 30},
    )
    assert "7 queued" in html
    assert "3 active" in html
    # Middle-dot separator (autoescaped HTML entity for ·).
    assert "&middot;" in html or "·" in html
    # The bar's inline width style carries the passed percent.
    assert "width: 75%" in html


# ---------------------------------------------------------------------------
# 34-03-01: idle renders empty
# ---------------------------------------------------------------------------


def test_card_idle_renders_empty() -> None:
    """All-zero busy -> no progress-bar markup and no queued/active text."""
    html = _render(
        agent_busy=0,
        agent_queued=0,
        agent_active=0,
        controller_busy=0,
        queue_progress_percent=0,
        stats={"analyzed": 30},
    )
    # The outer #processing-card element is always present (stable swap target)...
    assert 'id="processing-card"' in html
    # ...but the visual block (bar + text) is absent when idle.
    assert "width:" not in html
    assert "queued" not in html
    assert "active" not in html
    assert "Processing" not in html


# ---------------------------------------------------------------------------
# 34-03-01: controller/proposals second line
# ---------------------------------------------------------------------------


def test_card_controller_line() -> None:
    """controller_busy>0 -> the second compact Proposals line is present."""
    html = _render(
        agent_busy=0,
        controller_busy=4,
        controller_queued=4,
        controller_active=0,
        queue_progress_percent=0,
    )
    assert "Proposals:" in html
    assert "4 queued" in html
    assert "0 active" in html


# ---------------------------------------------------------------------------
# 34-03-02: the rendered bar width matches the passed DB-derived percent
# ---------------------------------------------------------------------------


def test_card_percent_math_seventyfive() -> None:
    """analyzed=30 + agent_busy=10 -> queue_progress_percent=75 drives the bar width."""
    html = _render(
        agent_busy=10,
        agent_queued=10,
        agent_active=0,
        queue_progress_percent=75,
        stats={"analyzed": 30},
    )
    assert "width: 75%" in html
    assert 'aria-valuenow="75"' in html


# ---------------------------------------------------------------------------
# 34-03-02: zero-denominator guard renders empty, no exception
# ---------------------------------------------------------------------------


def test_card_zero_denominator_guard() -> None:
    """analyzed=0 + agent_busy=0 (percent guarded to 0 upstream) -> empty, no error."""
    html = _render(
        agent_busy=0,
        controller_busy=0,
        queue_progress_percent=0,
        stats={"analyzed": 0},
    )
    assert "width:" not in html
    assert "Processing" not in html
    # The stable outer element still renders (so the OOB swap target exists).
    assert 'id="processing-card"' in html


# ---------------------------------------------------------------------------
# OOB gating: hx-swap-oob only on the poll response
# ---------------------------------------------------------------------------


def test_card_oob_attribute_only_on_poll() -> None:
    """oob_counts gates hx-swap-oob: present on the poll, absent on the initial include."""
    poll = _render(agent_busy=5, agent_queued=5, queue_progress_percent=50, oob_counts=True)
    initial = _render(agent_busy=5, agent_queued=5, queue_progress_percent=50, oob_counts=False)
    assert 'hx-swap-oob="true"' in poll
    assert "hx-swap-oob" not in initial
