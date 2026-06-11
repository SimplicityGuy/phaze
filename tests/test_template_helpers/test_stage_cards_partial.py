"""Jinja-render tests for pipeline/partials/stage_cards.html (Phase 34 Plan 04).

Targets VALIDATION 34-04-01 — all four pipeline-action buttons render, the three
agent-task buttons (Run Analysis / Fingerprint / Extract Metadata) gate on
``$store.pipeline.agentBusy`` and Generate Proposals on ``$store.pipeline.controllerBusy``,
and the in-place busy-seed anchors carry the initial-load store writes.

Mirrors test_progress_partial.py: renders via FastAPI's ``Jinja2Templates`` (the safe
wrapper Phaze uses in production) so autoescape matches production exactly, then asserts
on the rendered attribute text (the :disabled bindings are static strings).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fake_request() -> Request:
    """Minimal Starlette Request stub for templates that reference ``request``."""
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


def _render_stage_cards(
    *,
    discovered: int = 3,
    analyzed: int = 2,
    metadata_extracted: int = 5,
    agent_busy: int = 0,
    controller_busy: int = 0,
    settings_batch_size: int = 10,
) -> str:
    """Render the stage_cards.html partial with the given dashboard context."""
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/stage_cards.html",
        context={
            "stats": {
                "discovered": discovered,
                "analyzed": analyzed,
                "metadata_extracted": metadata_extracted,
            },
            "agent_busy": agent_busy,
            "controller_busy": controller_busy,
            "settings_batch_size": settings_batch_size,
        },
    )
    return response.body.decode()


def _disabled_for(html: str, hx_post: str) -> str:
    """Return the :disabled binding text for the button posting to ``hx_post``.

    Slices from the ``hx-post`` attribute up to the closing ``">`` of the button
    open tag so each button's disable expression is asserted in isolation.
    """
    start = html.index(f'hx-post="{hx_post}"')
    end = html.index('">', start)
    return html[start:end]


# ---------------------------------------------------------------------------
# 34-04-01 (a) — all four buttons render
# ---------------------------------------------------------------------------


def test_stage_cards_four_buttons_render() -> None:
    """All four pipeline-action endpoints appear as hx-post targets."""
    html = _render_stage_cards()
    assert 'hx-post="/pipeline/analyze"' in html
    assert 'hx-post="/pipeline/fingerprint"' in html
    assert 'hx-post="/pipeline/extract-metadata"' in html
    assert 'hx-post="/pipeline/proposals"' in html


# ---------------------------------------------------------------------------
# 34-04-01 (b) — the three agent buttons gate on agentBusy
# ---------------------------------------------------------------------------


def test_stage_cards_agent_buttons_gated_on_agent_busy() -> None:
    """Run Analysis, Fingerprint, and Extract Metadata each gate on agentBusy > 0."""
    html = _render_stage_cards()
    for endpoint in ("/pipeline/analyze", "/pipeline/fingerprint", "/pipeline/extract-metadata"):
        binding = _disabled_for(html, endpoint)
        assert "$store.pipeline.agentBusy > 0" in binding, endpoint
    # Run Analysis keeps its own ready-count condition alongside the agent gate.
    assert "$store.pipeline.discovered === 0" in _disabled_for(html, "/pipeline/analyze")
    # Fingerprint keeps the metadataExtracted ready-count condition.
    assert "$store.pipeline.metadataExtracted === 0" in _disabled_for(html, "/pipeline/fingerprint")


# ---------------------------------------------------------------------------
# 34-04-01 (c) — Generate Proposals gates on controllerBusy
# ---------------------------------------------------------------------------


def test_stage_cards_proposals_gated_on_controller_busy() -> None:
    """Generate Proposals gates on controllerBusy > 0 (NOT agentBusy)."""
    binding = _disabled_for(_render_stage_cards(), "/pipeline/proposals")
    assert "$store.pipeline.controllerBusy > 0" in binding
    assert "$store.pipeline.analyzed === 0" in binding
    assert "agentBusy" not in binding


# ---------------------------------------------------------------------------
# 34-04-01 (d) — initial busy seed anchors carry the store-writes
# ---------------------------------------------------------------------------


def test_stage_cards_initial_busy_seed_present() -> None:
    """The in-place seed anchors render x-init store-writes with the server counts."""
    html = _render_stage_cards(agent_busy=5, controller_busy=2)
    assert 'id="agent-busy-seed"' in html
    assert "$store.pipeline.agentBusy = 5" in html
    assert 'id="controller-busy-seed"' in html
    assert "$store.pipeline.controllerBusy = 2" in html
