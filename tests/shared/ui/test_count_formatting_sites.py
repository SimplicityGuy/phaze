"""phaze-dwevc: counts render with thousands separators wherever the UI shows one, server-side and live.

Two renderers print a count -- the Jinja ``thousands`` filter (first paint, OOB swaps) and the Alpine ``x-text`` bindings
that keep a value live after a poll -- and they must agree, because an operator watching ``145,057`` tick to ``145058``
must not see the separator vanish. ``static/js/format_count.js`` is the JS twin; ``tests/browser/test_format_count_parity.py``
runs both over one case table in Chromium.

Exclusions -- things that are NOT counts and are deliberately left raw: years and timestamps, ids and hashes, ports,
version strings, codec bitrates, percentages and confidence, page numbers and page sizes, byte sizes, durations, BPM and
key positions, file paths and names, queue priorities, and the integer assignments inside ``x-init`` and ``hx-vals``
that seed the store (those are JS source, not text).
"""

from __future__ import annotations

from pathlib import Path
import re

from fastapi.templating import Jinja2Templates

from phaze.web.template_globals import register_page_name_globals


ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = ROOT / "src" / "phaze" / "templates"
STATIC_JS = ROOT / "src" / "phaze" / "static" / "js" / "format_count.js"

_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_page_name_globals(_templates.env)

_SKIP = re.compile(r"\{#.*?#\}|<!--.*?-->|<script\b.*?</script>", re.S)
_BINDING = re.compile(r"""(?<![\w-])(?:x-text|:title|:aria-label|x-bind:title|x-bind:aria-label)="((?:[^"\\]|\\.)*)\"""")
# Store keys that are not counts: readiness flags, the pause flag, and queue PRIORITY (a rank, not a tally).
_NOT_COUNTS = re.compile(r"\$store\.pipeline\.\w*(?:Known|Paused|Priority)\b")
_STORE_READ = re.compile(r"\$store\.pipeline\.\w+")
_FORMATTED = re.compile(r"formatCount\(\s*\$store\.pipeline\.\w+\s*\)")


def _template(relative: str) -> str:
    return (TEMPLATES_DIR / relative).read_text(encoding="utf-8")


def _render(relative: str, **context: object) -> str:
    return _templates.env.get_template(relative).render(**context)


# --- live bindings ---------------------------------------------------------------------------------------------


def test_every_store_count_in_a_text_binding_goes_through_the_js_formatter() -> None:
    offenders: list[str] = []
    bindings = 0
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = _SKIP.sub(lambda m: " " * len(m.group(0)), path.read_text(encoding="utf-8"))
        for match in _BINDING.finditer(text):
            expression = match.group(1)
            if "$store.pipeline." not in expression:
                continue
            bindings += 1
            remainder = _NOT_COUNTS.sub("", _FORMATTED.sub("", expression))
            if _STORE_READ.search(remainder):
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}:{text.count(chr(10), 0, match.start()) + 1}: {expression[:90]}")
    assert bindings > 20, f"the guard found only {bindings} store bindings, so it is not scanning what it thinks it is"
    assert not offenders, f"a live count is bound without formatCount(): {offenders}"


def test_the_guard_catches_a_raw_store_read() -> None:
    raw = "<span x-text=\"$store.pipeline.seedKnown ? $store.pipeline.discovered : '—'\"></span>"
    fixed = "<span x-text=\"$store.pipeline.seedKnown ? formatCount($store.pipeline.discovered) : '—'\"></span>"

    for source, expect_offender in ((raw, True), (fixed, False)):
        expression = _BINDING.search(source)
        assert expression is not None
        remainder = _NOT_COUNTS.sub("", _FORMATTED.sub("", expression.group(1)))
        assert bool(_STORE_READ.search(remainder)) is expect_offender


def test_the_formatter_is_loaded_before_alpine() -> None:
    shell = _template("shell/shell.html")

    formatter = shell.index("js/format_count.js")
    alpine = shell.index("alpinejs@3")
    assert formatter < alpine
    tag = re.search(r"<script[^>]*js/format_count.js[^>]*>", shell)
    assert tag is not None
    assert "defer" not in tag.group(0), "a deferred formatter could lose the race with Alpine's first x-text"


def test_the_js_formatter_is_registered_as_window_formatcount() -> None:
    source = STATIC_JS.read_text(encoding="utf-8")

    assert "window.formatCount = formatCount;" in source
    assert "toLocaleString" not in source, "grouping must not depend on the operator's locale"


# --- server-rendered sites -------------------------------------------------------------------------------------


def test_the_rail_counts_render_with_separators_on_first_paint() -> None:
    seed = {
        "discovered": 145057,
        "metadataStatusDone": 104156,
        "metadataStatusTotal": 145057,
        "metadataStatusKnown": True,
        "analyzeDone": 94772,
        "analyzeTotal": 103338,
        "tracklistDone": 1234,
        "proposalsDone": 5000,
        "proposalsTotal": 12000,
    }

    html = _render("shell/partials/rail.html", stage="summary", pipeline_seed=seed)

    for expected in ("145,057", "104,156", "94,772", "103,338", "1,234", "5,000", "12,000"):
        assert expected in html, expected
    assert ">145057<" not in html


def test_the_header_status_strip_renders_separators() -> None:
    html = _render("shell/partials/header.html", pipeline_seed={"agentOnline": 1200, "computeLanesActive": 3000}, force_local=False)

    assert ">1,200<" in html
    assert ">3,000<" in html


def test_metric_tiles_and_tab_chips_format_numbers_but_leave_text_alone() -> None:
    source = (
        '{% import "ui/primitives.html" as ui %}'
        "{{ ui.metric('Files', 145057) }}{{ ui.metric('Avg', '94%') }}{{ ui.metric('Latest', '2026-10-06') }}"
        "{{ ui.tab_count(103338) }}"
    )

    html = _templates.env.from_string(source).render()

    assert ">145,057</dd>" in html
    assert ">94%</dd>" in html
    assert ">2026-10-06</dd>" in html
    assert ">103,338</span>" in html


def test_the_drain_status_uses_the_shared_filter_not_its_own_format_spec() -> None:
    source = _SKIP.sub(" ", _template("pipeline/partials/_tracklist_drain_status.html"))
    row_detail = _SKIP.sub(" ", _template("proposals/partials/row_detail.html"))

    for text in (source, row_detail):
        assert "{:,}" not in text
        assert "| thousands" in text


def test_nothing_that_is_not_a_count_goes_through_the_filter() -> None:
    forbidden = re.compile(
        r"\{\{[^}]*(?:year|port\b|version|bitrate|kbps|path|sha256|hash|_id\b|\.page\b|(?<!low_)confidence|percent|bpm|byte)[^}]*\|\s*thousands"
    )
    offenders = [
        path.relative_to(TEMPLATES_DIR).as_posix()
        for path in sorted(TEMPLATES_DIR.rglob("*.html"))
        if forbidden.search(_SKIP.sub(" ", path.read_text(encoding="utf-8")))
    ]
    assert not offenders, f"a non-count is formatted with thousands separators: {offenders}"
