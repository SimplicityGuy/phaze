"""Focused render contracts for the shared server-rendered UI primitives."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.templating import Jinja2Templates


TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _render(source: str, **context: object) -> str:
    return _templates.env.from_string(source).render(**context)


def test_primitives_render_the_visual_accessibility_and_fragment_contracts() -> None:
    html = _render(
        """
        {% import "ui/primitives.html" as ui %}
        {% set actions %}<button class="focus-visible:ring-2">Run</button>{% endset %}
        {{ ui.page_header("Files", "12 records", actions) }}
        {% call ui.metric_strip("Pipeline metrics") %}
          {{ ui.metric("Queued", 12, "ready", "accent") }}
          {{ ui.metric("Failed", 2, "needs attention", "danger") }}
        {% endcall %}
        {{ ui.status_badge("Analyze", "in flight", "accent", "●", true) }}
        {% call ui.actionable_alert("Queue blocked", "Review the lane configuration.") %}<button>Inspect</button>{% endcall %}
        {% call ui.filter_toolbar("filters", "/items", "#results", true) %}<input aria-label="Query">{% endcall %}
        {% call ui.data_table("Files") %}<tbody><tr><td>song.mp3</td></tr></tbody>{% endcall %}
        {% call ui.data_table_controls("Files pagination") %}{{ ui.data_table_button("Previous", "/items?page=1", "#results") }}{{ ui.data_table_button("Next", disabled=true) }}{% endcall %}
        {{ ui.state("empty", "No files", "Discovered files appear here.") }}
        {{ ui.state("loading", "Loading files", "The current view is being refreshed.") }}
        {{ ui.state("error", "Files unavailable", "Retry the request.") }}
        {{ ui.refresh_status("Updated just now", "Refreshing", "refresh-state") }}
        {{ ui.confirmation("confirm-run", "Run analysis", "Analyze selected files?", "/analysis/run", target="#result") }}
        {% call ui.detail_drawer("file-detail", "File detail") %}<p>Detail body</p>{% endcall %}
        """
    )

    assert "<html" not in html and "<head" not in html
    assert '<h1 tabindex="-1"' in html
    assert 'aria-label="Pipeline metrics"' in html
    assert 'aria-label="Analyze: in flight"' in html
    assert "●" in html and "in flight" in html
    assert 'role="alert"' in html and "Queue blocked" in html
    assert 'hx-get="/items"' in html
    assert 'hx-target="#results"' in html
    assert 'hx-push-url="true"' in html
    assert 'aria-label="Files"' in html
    assert 'aria-label="Files pagination"' in html
    assert 'hx-get="/items?page=1"' in html and 'aria-disabled="true"' in html
    assert html.count('role="status"') >= 3
    assert 'id="refresh-state"' in html and "htmx-indicator" in html
    assert 'id="confirm-run"' in html and 'hx-post="/analysis/run"' in html
    assert 'id="file-detail"' in html and "Detail body" in html
    assert html.count('aria-labelledby="') >= 2
    assert "focus-visible:ring-2" in html
    assert "dark:" in html
    assert "sm:flex-row" in html
    assert "overflow-x-auto" in html
    assert "motion-safe:animate-pulse" in html
    assert "hx-on::after-request" in html


def test_primitive_content_is_autoescaped() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{{ ui.status_badge(label, status) }}{{ ui.state("error", title, message) }}""",
        label='<img src=x onerror="alert(1)">',
        status="failed",
        title="<script>alert(1)</script>",
        message="<unsafe>",
    )

    assert "<script>" not in html and "<img" not in html and "<unsafe>" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html and "&lt;unsafe&gt;" in html


def test_files_workspace_adopts_primitives_without_changing_htmx_contracts() -> None:
    html = _render(
        """{% include "pipeline/partials/files_table_view.html" %}""",
        files_page=SimpleNamespace(rows=[], page=1, page_size=50, has_next=False),
        active_stage="analyze",
        active_bucket="failed",
        sort=None,
    )

    assert 'id="status-filter-bar"' in html
    assert 'aria-label="File status filters"' in html
    assert 'hx-get="/pipeline/files"' in html
    assert 'hx-target="#files-table-view"' in html
    assert 'hx-swap="innerHTML"' in html
    assert 'hx-push-url="true"' in html
    assert "No failed files in Analyze" in html
    assert 'role="status"' in html
    assert "sm:flex-row" in html
