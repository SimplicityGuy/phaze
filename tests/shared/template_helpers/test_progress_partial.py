"""Jinja-render tests for execution/partials/progress.html + agents_table.html (Phase 28 D-08, D-11).

Targets 28-V-21 — UI-SPEC §"Test Contract (UI side)" empty / single-agent /
multi-agent / completed-with-errors / pending / revoked-banner pluralization.

Uses FastAPI's ``Jinja2Templates`` (the safe wrapper Phaze uses in production)
so the test renderer matches the production autoescape configuration exactly,
including the ``.html``-suffix-driven autoescape default.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "src" / "phaze" / "templates"

# Reuse the production-style ``Jinja2Templates`` wrapper. Autoescape for ``.html``
# is enabled by default in this constructor (see FastAPI's docs).
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fake_request() -> Request:
    """Minimal Starlette Request stub for templates that reference ``request``.

    Only the ``url_for`` / dict-style access patterns matter; our partials
    don't use either, but Jinja2Templates wraps every render with a ``request``
    context key.
    """
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


def _render_agents_table(*, agents: list[dict[str, object]]) -> str:
    """Render the per-agent rollup table partial with the given agents list."""
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="execution/partials/agents_table.html",
        context={"agents": agents},
    )
    return response.body.decode()


def _render_progress(
    *,
    batch_id: str = "00000000-0000-0000-0000-000000000000",
    skipped_revoked: int = 0,
    revoked_agents: list[dict[str, object]] | None = None,
    total: int = 0,
    completed: int = 0,
    failed: int = 0,
    subjobs_expected: int = 0,
    agents: list[dict[str, object]] | None = None,
    status: str = "running",
) -> str:
    """Render the rewritten progress card partial with the given context."""
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="execution/partials/progress.html",
        context={
            "batch_id": batch_id,
            "skipped_revoked": skipped_revoked,
            "revoked_agents": revoked_agents or [],
            "total": total,
            "completed": completed,
            "failed": failed,
            "subjobs_expected": subjobs_expected,
            "agents": agents or [],
            "status": status,
        },
    )
    return response.body.decode()


# ---------------------------------------------------------------------------
# agents_table.html — Empty state
# ---------------------------------------------------------------------------


def test_empty_dispatch_summary_renders_italic_paragraph() -> None:
    """No agents -> the italic 'No active sub-jobs.' paragraph renders instead of an empty table."""
    html = _render_agents_table(agents=[])
    assert "No active sub-jobs." in html
    assert "italic" in html
    # Defensive: no <tbody> rows when empty.
    assert "<tr" not in html or "<tbody" not in html


# ---------------------------------------------------------------------------
# agents_table.html — Single-agent RUNNING
# ---------------------------------------------------------------------------


def test_single_agent_renders_one_row_with_running_pill() -> None:
    """One agent with completed=2, failed=0, total=5 -> RUNNING pill + 1 row."""
    html = _render_agents_table(
        agents=[
            {
                "agent_id": "agent-aaa",
                "name": "Agent Alpha",
                "completed": 2,
                "failed": 0,
                "total": 5,
            },
        ],
    )
    # One body row (the header <tr> + one body <tr> = 2 total).
    assert html.count("<tr") == 2
    # RUNNING pill with blue surface (UI-SPEC pill rules).
    assert "RUNNING" in html
    assert "bg-blue-100" in html
    # Two-line agent cell.
    assert "Agent Alpha" in html
    assert "agent-aaa" in html


# ---------------------------------------------------------------------------
# agents_table.html — Multi-agent ordering
# ---------------------------------------------------------------------------


def test_multi_agent_renders_rows_in_dispatch_order() -> None:
    """3 agents in [A, B, C] order -> 3 <tr> in that order (no reorder by sort key)."""
    html = _render_agents_table(
        agents=[
            {"agent_id": "agent-aaa", "name": "Alpha", "completed": 0, "failed": 0, "total": 3},
            {"agent_id": "agent-bbb", "name": "Beta", "completed": 1, "failed": 0, "total": 4},
            {"agent_id": "agent-ccc", "name": "Gamma", "completed": 5, "failed": 0, "total": 5},
        ],
    )
    # 1 header <tr> + 3 body <tr> = 4 total.
    assert html.count("<tr") == 4
    # Ordering: Alpha appears before Beta which appears before Gamma in the rendered html.
    pos_alpha = html.find("Alpha")
    pos_beta = html.find("Beta")
    pos_gamma = html.find("Gamma")
    assert 0 <= pos_alpha < pos_beta < pos_gamma


# ---------------------------------------------------------------------------
# agents_table.html — COMPLETE state
# ---------------------------------------------------------------------------


def test_all_complete_pill_green() -> None:
    """completed=5, failed=0, total=5 -> COMPLETE pill with bg-green-100."""
    html = _render_agents_table(
        agents=[
            {"agent_id": "agent-aaa", "name": "Alpha", "completed": 5, "failed": 0, "total": 5},
        ],
    )
    assert "COMPLETE" in html
    assert "bg-green-100" in html


# ---------------------------------------------------------------------------
# agents_table.html — ERRORS state + Failed cell coloring
# ---------------------------------------------------------------------------


def test_completed_with_errors_pill_red_classes() -> None:
    """completed=2, failed=3, total=5 -> ERRORS pill + Failed cell text-red-600 font-semibold."""
    html = _render_agents_table(
        agents=[
            {"agent_id": "agent-aaa", "name": "Alpha", "completed": 2, "failed": 3, "total": 5},
        ],
    )
    assert "ERRORS" in html
    assert "bg-red-100" in html
    # Failed cell coloring per UI-SPEC C2.
    assert "text-red-600" in html
    assert "font-semibold" in html


# ---------------------------------------------------------------------------
# agents_table.html — PENDING state
# ---------------------------------------------------------------------------


def test_pending_pill_when_no_progress() -> None:
    """completed=0, failed=0, total=5 -> PENDING pill bg-gray-100."""
    html = _render_agents_table(
        agents=[
            {"agent_id": "agent-aaa", "name": "Alpha", "completed": 0, "failed": 0, "total": 5},
        ],
    )
    assert "PENDING" in html
    assert "bg-gray-100" in html


# ---------------------------------------------------------------------------
# agents_table.html — Caption / accessibility
# ---------------------------------------------------------------------------


def test_agents_table_has_screen_reader_caption() -> None:
    """The table must carry the sr-only caption per UI-SPEC accessibility contract."""
    html = _render_agents_table(
        agents=[
            {"agent_id": "agent-aaa", "name": "Alpha", "completed": 1, "failed": 0, "total": 2},
        ],
    )
    assert "Per-agent execution progress" in html
    assert "sr-only" in html


# ---------------------------------------------------------------------------
# progress.html — Revoked-agents banner pluralization (1 vs N)
# ---------------------------------------------------------------------------


def test_revoked_agents_banner_pluralization_singular() -> None:
    """skipped_revoked=1 -> '1 approved proposal ... its agent has been revoked.'"""
    html = _render_progress(
        skipped_revoked=1,
        revoked_agents=[{"agent_id": "agent-zzz", "name": "Zulu", "count": 1}],
    )
    assert "1 approved proposal" in html
    # Singular pronoun set.
    assert "its agent has" in html
    # No plural pronoun in the body line.
    assert "their agents have" not in html
    # Banner heading.
    assert "Some proposals skipped" in html
    assert "bg-orange-50" in html
    assert 'role="alert"' in html


def test_revoked_agents_banner_pluralization_plural() -> None:
    """skipped_revoked=3 -> '3 approved proposals ... their agents have been revoked.'"""
    html = _render_progress(
        skipped_revoked=3,
        revoked_agents=[
            {"agent_id": "agent-yyy", "name": "Yankee", "count": 1},
            {"agent_id": "agent-zzz", "name": "Zulu", "count": 2},
        ],
    )
    assert "3 approved proposals" in html
    assert "their agents have" in html
    assert "its agent has" not in html


def test_no_revoked_banner_when_zero_skipped() -> None:
    """skipped_revoked=0 -> the orange-surface banner is NOT rendered."""
    html = _render_progress(skipped_revoked=0)
    assert "Some proposals skipped" not in html
    assert "bg-orange-50" not in html


# ---------------------------------------------------------------------------
# progress.html — Dual sse-close listeners + sse event slot wiring
# ---------------------------------------------------------------------------


def test_progress_has_dual_sse_close_listeners() -> None:
    """Both 'complete' and 'complete_with_errors' close the SSE per UI-SPEC C1 step 5."""
    # phaze-5zyv: the close listeners are gated on a connected stream (agents present), so render
    # with an agent -- the empty-state card has no stream and therefore no close listeners.
    html = _render_progress(
        total=10,
        subjobs_expected=2,
        agents=[{"agent_id": "agent-a", "name": "Alpha", "completed": 0, "failed": 0, "total": 10}],
    )
    assert 'sse-close="complete"' in html
    assert 'sse-close="complete_with_errors"' in html


def test_progress_has_agents_table_swap_slot() -> None:
    """The progress card must contain an sse-swap='agents_table' slot wrapping the table partial."""
    html = _render_progress(
        total=10,
        subjobs_expected=2,
        agents=[{"agent_id": "agent-a", "name": "Alpha", "completed": 0, "failed": 0, "total": 10}],
    )
    assert 'sse-swap="agents_table"' in html


def test_progress_has_dispatch_summary_swap_slot() -> None:
    """The dispatch summary heading is an sse-swap='dispatch_summary' target per UI-SPEC C1 step 2."""
    html = _render_progress(
        total=10,
        subjobs_expected=2,
        agents=[{"agent_id": "agent-a", "name": "Alpha", "completed": 0, "failed": 0, "total": 10}],
    )
    assert 'sse-swap="dispatch_summary"' in html


def test_progress_sse_connect_points_at_batch_id() -> None:
    """The outer container connects to /execution/progress/{batch_id} when there are agents to stream."""
    # phaze-5zyv: the sse-connect is now gated on a non-empty agents list, so exercise the
    # connect path with an agent present.
    html = _render_progress(
        batch_id="cafef00d-cafe-f00d-cafe-f00dcafef00d",
        agents=[{"agent_id": "agent-a", "name": "Alpha", "completed": 0, "failed": 0, "total": 10}],
    )
    assert "sse-connect=" in html
    assert "/execution/progress/cafef00d-cafe-f00d-cafe-f00dcafef00d" in html


def test_progress_empty_state_has_no_sse_connect() -> None:
    """phaze-5zyv: the empty-state card (no agents) opens NO SSE stream -- it would never terminate."""
    html = _render_progress(skipped_revoked=0, agents=[])
    assert "sse-connect" not in html
    assert "hx-ext" not in html


def test_progress_empty_state_when_no_agents() -> None:
    """skipped_revoked=0 and no agents -> 'No approved proposals to execute.' per UI-SPEC empty-state row."""
    html = _render_progress(skipped_revoked=0, agents=[])
    assert "No approved proposals to execute." in html


# ---------------------------------------------------------------------------
# PR4: scan_progress_card.html RUNNING-branch live activity affordance
# ---------------------------------------------------------------------------


def _render_scan_progress_card(
    *,
    status: str = "running",
    is_stalled: bool = False,
    seconds_since_progress: int = 5,
    elapsed_seconds: int | None = 0,
) -> str:
    """Render the pipeline scan_progress_card.html partial with the given context."""
    from types import SimpleNamespace

    batch = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000000",
        status=status,
        processed_files=3,
        total_files=10,
        scan_path="/data/music",
        error_message=None,
    )
    response = _templates.TemplateResponse(
        request=_fake_request(),
        name="pipeline/partials/scan_progress_card.html",
        context={
            "batch": batch,
            "agent_name": "Test Agent",
            "elapsed_seconds": elapsed_seconds,
            "is_stalled": is_stalled,
            "seconds_since_progress": seconds_since_progress,
        },
    )
    return response.body.decode()


def test_scan_card_running_renders_green_pulse_and_last_activity() -> None:
    """A progressing RUNNING card shows the green pulsing dot + 'last activity Ns ago'."""
    html = _render_scan_progress_card(status="running", is_stalled=False, seconds_since_progress=8)
    assert "animate-pulse" in html
    assert "bg-green-500" in html
    assert "last activity 8s ago" in html
    assert "stalled?" not in html
    # Pitfall 6: the RUNNING branch still carries the polling trigger.
    assert 'hx-trigger="every 2s"' in html


def test_scan_card_running_stalled_renders_amber_warning() -> None:
    """A stalled RUNNING card swaps to the amber dot + 'stalled?' treatment."""
    html = _render_scan_progress_card(status="running", is_stalled=True, seconds_since_progress=350)
    assert "bg-amber-500" in html
    assert "stalled?" in html
    assert "no activity for 350s" in html
    # The green pulse is replaced, not added alongside.
    assert "animate-pulse" not in html


def test_scan_card_terminal_branches_have_no_polling_trigger() -> None:
    """COMPLETED/FAILED branches OMIT hx-trigger so HTMX polling halts (Pitfall 6)."""
    for status in ("completed", "failed"):
        html = _render_scan_progress_card(status=status, elapsed_seconds=12)
        assert "hx-trigger" not in html, f"{status} branch must not poll"
        assert "animate-pulse" not in html, f"{status} branch must not show the live pulse"
