"""Focused render contracts for the shared server-rendered UI primitives."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.templating import Jinja2Templates


TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _render(source: str, **context: object) -> str:
    return _templates.env.from_string(source).render(**context)


def _contrast_against_white(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return 1.05 / (luminance + 0.05)


def test_page_header_is_focusable_fragment_safe_and_responsive() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{% set actions %}<button>Run</button>{% endset %}{{ ui.page_header("Files", "12 records", actions, "file-count") }}"""
    )

    assert "<html" not in html and "<head" not in html
    assert '<h1 tabindex="-1"' in html
    assert 'id="file-count"' in html
    assert "sm:flex-row" in html
    assert "Run" in html


def test_metric_strip_uses_semantic_markup_and_palette_tones() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{% call ui.metric_strip("Pipeline metrics") %}{{ ui.metric("Queued", 12, "ready", "accent") }}{{ ui.metric("Failed", 2, "needs attention", "danger") }}{% endcall %}"""
    )

    assert 'aria-label="Pipeline metrics"' in html
    assert "<dl" in html and html.count("<dt") == 2 and html.count("<dd") == 4
    assert "text-blue-700" in html and "text-red-700" in html
    assert "overflow-x-auto" in html


def test_status_badge_has_text_icon_and_reduced_motion_safe_activity() -> None:
    html = _render("""{% import "ui/primitives.html" as ui %}{{ ui.status_badge("Analyze", "in flight", "accent", "●", true) }}""")

    assert 'aria-label="Analyze: in flight"' in html
    assert "●" in html and "in flight" in html
    assert "motion-safe:animate-pulse" in html
    assert "dark:bg-blue-950" in html


def test_actionable_alert_has_redundant_semantics_and_action_slot() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{% call ui.actionable_alert("Queue blocked", "Review the lane configuration.") %}<button>Inspect</button>{% endcall %}"""
    )

    assert 'role="alert"' in html and "Queue blocked" in html
    assert "⚠" in html and "Inspect" in html
    assert "dark:bg-amber-950" in html


def test_filter_toolbar_preserves_htmx_and_narrow_screen_contracts() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{% call ui.filter_toolbar("filters", "/items", "#results", true) %}<input aria-label="Query">{% endcall %}"""
    )

    assert 'id="filters"' in html and 'aria-label="Filters"' in html
    assert 'hx-get="/items"' in html
    assert 'hx-target="#results"' in html
    assert 'hx-push-url="true"' in html
    assert "flex-col" in html and "sm:flex-row" in html


def test_data_table_labels_scrollable_content() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{% call ui.data_table("Files", "files-help") %}<tbody><tr><td>song.mp3</td></tr></tbody>{% endcall %}"""
    )

    assert 'aria-label="Files"' in html
    assert 'aria-describedby="files-help"' in html
    assert "overflow-x-auto" in html and "song.mp3" in html


def test_data_table_controls_have_focus_and_disabled_semantics() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{% call ui.data_table_controls("Files pagination") %}{{ ui.data_table_button("Previous", "/items?page=1", "#results") }}{{ ui.data_table_button("Next", disabled=true) }}{% endcall %}"""
    )

    assert 'aria-label="Files pagination"' in html
    assert 'hx-get="/items?page=1"' in html and 'aria-disabled="true"' in html
    assert "focus-visible:ring-2" in html


def test_empty_loading_and_error_states_have_distinct_live_semantics() -> None:
    empty = _render("""{% import "ui/primitives.html" as ui %}{{ ui.state("empty", "No files", "Discovered files appear here.") }}""")
    loading = _render("""{% import "ui/primitives.html" as ui %}{{ ui.state("loading", "Loading files", "Please wait.") }}""")
    error = _render("""{% import "ui/primitives.html" as ui %}{{ ui.state("error", "Files unavailable", "Retry.") }}""")

    assert 'role="status"' in empty and "No files" in empty
    assert 'role="status"' in loading and "motion-safe:animate-pulse" in loading
    assert 'role="alert"' in error and "text-red-600" in error


def test_refresh_status_exposes_exactly_one_message_per_request_state() -> None:
    html = _render("""{% import "ui/primitives.html" as ui %}{{ ui.refresh_status("Updated just now", "Refreshing", "refresh-state") }}""")
    css = (Path(__file__).resolve().parents[2] / "assets" / "src" / "app.css").read_text()

    assert 'id="refresh-state"' in html and 'role="status"' in html and 'aria-live="polite"' in html
    assert html.count("htmx-idle") == 1 and html.count("htmx-loading") == 1
    assert "htmx-indicator" not in html
    assert ".htmx-loading { display: none; }" in css
    assert ".htmx-request .htmx-idle { display: none; }" in css
    assert ".htmx-request .htmx-loading { display: inline-flex; }" in css


def test_confirmation_is_contrast_safe_and_dismissible_without_breaking_native_dialog_behavior() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{{ ui.confirmation("confirm-run", "Run analysis", "Analyze selected files?", "/analysis/run", target="#result") }}"""
    )
    danger = _render(
        """{% import "ui/primitives.html" as ui %}{{ ui.confirmation("confirm-delete", "Delete files", "Delete selected files?", "/files/delete", tone="danger") }}"""
    )

    assert 'id="confirm-run"' in html and 'aria-labelledby="confirm-run-title"' in html
    assert 'hx-post="/analysis/run"' in html and 'hx-target="#result"' in html
    assert "bg-amber-800 hover:bg-amber-700" in html
    assert "bg-amber-600" not in html
    assert "bg-red-700 hover:bg-red-600" in danger
    for color in ("92400e", "b45309", "b91c1c", "dc2626"):
        assert _contrast_against_white(color) >= 4.5
    assert 'onpointerdown="if (event.target === this) this.close()"' in html
    assert '<form method="dialog">' in html
    assert "hx-on::after-request" in html
    assert "focus-visible:ring-2" in html


def test_detail_drawer_has_pointer_backdrop_close_native_escape_and_focus_contracts() -> None:
    html = _render(
        """{% import "ui/primitives.html" as ui %}{% call ui.detail_drawer("file-detail", "File detail") %}<p>Detail body</p>{% endcall %}"""
    )

    assert 'id="file-detail"' in html and 'aria-labelledby="file-detail-title"' in html
    assert 'onpointerdown="if (event.target === this) this.close()"' in html
    assert '<form method="dialog">' in html
    assert 'aria-label="Close File detail"' in html
    assert 'id="file-detail-title" tabindex="-1"' in html
    assert "preventDefault" not in html
    assert "Detail body" in html


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
