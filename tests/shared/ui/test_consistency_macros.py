"""phaze-gmh3f: the alert, banner, tab-count and disclosure patterns, and the one expand glyph.

Each of these used to be spelled differently per surface (three orphaned-work alert headings, a cyan eyebrow banner
against an amber bold-title one, ``All 0`` chips against ``All (0)``, disclosures with no marker, three expand glyphs,
Review nav items tinted amber at rest). The macros in ``ui/primitives.html`` are the one definition of each; these
tests render them and guard the templates that must use them.
"""

from __future__ import annotations

from pathlib import Path
import re

from fastapi.templating import Jinja2Templates
import pytest

from phaze.web.template_globals import register_page_name_globals


ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = ROOT / "src" / "phaze" / "templates"
APP_CSS = ROOT / "assets" / "src" / "app.css"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_page_name_globals(_templates.env)
_IMPORT = '{% import "ui/primitives.html" as ui %}'


def _render(source: str, **context: object) -> str:
    return _templates.env.from_string(_IMPORT + source).render(**context)


def _template(relative: str) -> str:
    return (TEMPLATES_DIR / relative).read_text(encoding="utf-8")


# --- alerts ------------------------------------------------------------------------------------------------------


def test_the_alert_heading_is_the_jura_eyebrow_with_a_tone_icon() -> None:
    html = _render("{{ ui.actionable_alert('Orphaned analysis work', 'two files', 'attention') }}")

    heading = re.search(r"<h2 class=\"([^\"]+)\">Orphaned analysis work</h2>", html)
    assert heading is not None
    assert "font-jura" in heading.group(1)
    assert "uppercase" in heading.group(1)
    assert "⚠" in html


@pytest.mark.parametrize(
    "template",
    ["pipeline/partials/discover_workspace.html", "pipeline/partials/analyze_workspace.html"],
)
def test_orphaned_work_alerts_render_through_the_shared_alert(template: str) -> None:
    source = _template(template)

    assert "ui.actionable_alert('Orphaned" in source


def test_summary_alert_items_wear_the_shared_alert_heading() -> None:
    source = _template("shell/partials/summary_overview.html")

    assert "ui.alert_heading_class" in source
    assert 'class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ item.title }}' not in source


def test_the_gate_copy_beside_a_recover_button_wraps_under_it_rather_than_floating_beside_it() -> None:
    for template, element in (
        ("pipeline/partials/discover_workspace.html", 'id="recover-gate-copy"'),
        ("pipeline/partials/analyze_workspace.html", 'id="analyze-recover-gate-copy"'),
    ):
        tag = re.search(rf"<span {re.escape(element)}[^\n]*", _template(template))
        assert tag is not None
        assert "basis-full" in tag.group(0), f"{template}: the gate copy must take its own line"


def test_trigger_scan_is_a_subheading_not_a_competing_h2() -> None:
    source = _template("pipeline/partials/trigger_scan_card.html")

    assert re.search(r'<h3 id="trigger-scan-heading" class="text-sm font-semibold', source)
    assert "text-lg" not in source


# --- banners -----------------------------------------------------------------------------------------------------


def test_the_banner_has_one_tone_and_an_eyebrow_title() -> None:
    html = _render("{{ ui.banner('Approval boundary', 'Nothing moves without review.') }}")

    assert "data-page-banner" in html
    assert "border-blue-200" in html
    assert re.search(r"font-jura[^\"]*uppercase[^\"]*\">Approval boundary</p>", html)


def test_propose_and_rename_render_through_the_one_banner() -> None:
    for template in ("pipeline/partials/propose_workspace.html", "pipeline/partials/changes_workspace.html"):
        source = _template(template)
        assert "ui.banner(" in source, template
        assert "bg-amber-50" not in source, f"{template} still spells its own amber banner"


# --- tab counts --------------------------------------------------------------------------------------------------


def test_a_tab_count_is_a_chip_never_parentheses() -> None:
    html = _render("{{ ui.tab_count(0) }}")

    assert 'data-not-status-pill="count chip"' in html
    assert ">0</span>" in html
    assert "(0)" not in html


def test_the_active_tab_is_underlined_and_the_rest_are_muted() -> None:
    active = _render("{{ ui.tab_class(true) }}")
    rest = _render("{{ ui.tab_class(false) }}")

    assert "border-action-brand" in active
    assert "font-semibold" in active
    assert "text-muted" in rest
    assert "border-b-2" not in rest


@pytest.mark.parametrize(
    "template",
    [
        "proposals/partials/filter_tabs.html",
        "pipeline/partials/_changes_list.html",
        "execution/partials/filter_tabs.html",
    ],
)
def test_every_tab_strip_uses_the_shared_tab_macros(template: str) -> None:
    source = _template(template)

    assert "ui.tab_nav_class" in source
    assert "ui.tab_class(" in source
    assert "ui.tab_count(" in source
    assert ") }}" in source
    assert not re.search(r"\(\{\{ count \}\}\)", source), f"{template} prints the count in parentheses"


# --- disclosures and the one expand glyph ------------------------------------------------------------------------


def test_the_disclosure_macro_renders_a_summary_and_carries_its_hooks() -> None:
    html = _render('{% call ui.disclosure("History", attrs={"id": "history", "data-history": ""}) %}<p>body</p>{% endcall %}')

    assert '<details id="history" data-history=""' in html
    assert re.search(r"<summary class=\"[^\"]*flex[^\"]*\">History</summary>", html)
    assert "<p>body</p>" in html
    assert " open" not in html.split("class=", 1)[0]


def test_the_disclosure_macro_can_ship_open() -> None:
    html = _render('{% call ui.disclosure("Stage eligibility", open=true) %}x{% endcall %}')

    assert html.lstrip().startswith("<details open")


def test_every_record_fold_is_a_disclosure() -> None:
    source = _template("record/_record_content.html")

    assert "<details" not in source
    assert source.count("ui.disclosure(") == 4


def test_the_one_expand_glyph_is_drawn_by_a_single_css_rule() -> None:
    css = APP_CSS.read_text(encoding="utf-8")

    assert "details > summary::before" in css
    assert 'content: "\\25B6\\FE0E"' in css
    assert "details[open] > summary::before { transform: rotate(90deg); }" in css
    assert css.count('content: "\\25B6') == 1


def test_no_template_draws_a_second_expand_glyph() -> None:
    offenders: list[str] = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "group-open:rotate" in line or "\u2304" in line or ("\u203a" in line and "<span" in line and "aria-hidden" in line):
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{number}")
    assert not offenders, f"a template draws its own expand glyph instead of the shared summary marker: {offenders}"


def test_the_set_panel_body_is_not_double_padded() -> None:
    timeline = _template("proposals/partials/analysis_timeline.html")
    record = _template("record/_record_content.html")

    assert "timeline_embedded" in timeline
    assert "with timeline_embedded = true" in record


# --- sidebar -----------------------------------------------------------------------------------------------------


def test_review_and_operations_nav_items_share_the_pipeline_rest_and_active_treatment() -> None:
    def classes_for(tone: str) -> str:
        html = _templates.env.from_string(
            '{% import "shell/partials/rail.html" as rail %}{{ rail.nav_link("audit", "Audit log", "M0", "' + tone + '") }}'
        ).render()
        match = re.search(r'<a [^>]*class="([^"]+)"', html)
        assert match is not None
        return match.group(1)

    pipeline = classes_for("pipeline")

    assert classes_for("review") == pipeline
    assert classes_for("operations") == pipeline
    assert "text-warn" not in pipeline, "Review items must not be tinted amber at rest"
    assert "text-muted" not in pipeline.split("aria-[current=page]")[0], "Operations items must not read as disabled at rest"
    assert "aria-[current=page]:shadow-[inset_3px_0_0_var(--color-blue-500)]" in pipeline
    assert "aria-[current=page]:text-info" in pipeline
