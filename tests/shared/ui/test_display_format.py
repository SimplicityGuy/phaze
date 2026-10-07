"""phaze-nwmsu: ONE duration format, registered on every template environment, used at every site.

Durations rendered four ways before this bead: ``54:01`` (record), ``119:32`` (Analyze: minutes
past 60), ``33757s`` (Discover elapsed) and ``1744.2 sec`` (Dedupe). ``format_duration`` is the one
definition (``h:mm:ss`` from an hour up, ``m:ss`` under it, ``—`` for no value), exposed as the
``duration`` Jinja filter.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.templating import Jinja2Templates
import pytest

from phaze.services.analysis_timeline import format_elapsed_time
from phaze.utils.humanize import NO_DATA, format_count, format_duration
from phaze.web.template_globals import register_page_name_globals
from tests.shared.ui.test_template_globals_reach_every_environment import _modules_building_a_template_environment


TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0:00"),
        (5, "0:05"),
        (59.4, "0:59"),
        (59.6, "1:00"),
        (60, "1:00"),
        (3241, "54:01"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (7172, "1:59:32"),  # Analyze used to print this as 119:32
        (10812, "3:00:12"),  # ... and this as 180:12
        (33757, "9:22:37"),  # Discover used to print this as 33757s
        (1744.2, "29:04"),  # Dedupe used to print this as 1744.2 sec
        (86399, "23:59:59"),
        (86400, "24:00:00"),  # hours are never rolled into days
        (90061, "25:01:01"),
        (400000, "111:06:40"),
        ("3241", "54:01"),
    ],
)
def test_format_duration(seconds: float | str, expected: str) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize("value", [None, "", "soon", float("nan"), float("inf"), True])
def test_a_duration_with_no_data_is_an_em_dash(value: object) -> None:
    assert format_duration(value) == NO_DATA == "—"  # type: ignore[arg-type]


def test_a_negative_duration_clamps_rather_than_printing_a_minus() -> None:
    assert format_duration(-5) == "0:00"


def test_the_timeline_axis_uses_the_same_definition() -> None:
    for seconds in (0, 59, 3600, 7172, 90061):
        assert format_elapsed_time(seconds) == format_duration(seconds)


@pytest.mark.parametrize("module_name", _modules_building_a_template_environment())
def test_every_template_environment_carries_the_duration_filter(module_name: str) -> None:
    environment = importlib.import_module(module_name).templates.env

    assert environment.filters["duration"] is format_duration, f"{module_name} has no `duration` filter"


def test_the_filter_renders_through_a_real_environment() -> None:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    register_page_name_globals(templates.env)

    rendered = templates.env.from_string("{{ a | duration }}|{{ b | duration }}|{{ c | duration }}").render(a=7172, b=33757, c=None)

    assert rendered == "1:59:32|9:22:37|—"


@pytest.mark.parametrize(
    ("template", "retired"),
    [
        ("pipeline/partials/_analyze_files.html", "%02d"),
        ("pipeline/partials/recent_scans_table.html", "_elapsed_seconds }}s"),
        ("pipeline/partials/scan_progress_card.html", "elapsed_seconds }}s"),
        ("pipeline/partials/_dupe_group.html", '"%.1f" | format(f.duration)'),
        ("pipeline/partials/_dupe_group.html", "sec</span>"),
    ],
)
def test_the_four_duration_sites_no_longer_hand_roll_a_format(template: str, retired: str) -> None:
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")

    assert retired not in source
    assert "| duration" in source


# --- phaze-dwevc: counts ------------------------------------------------------------------------------------------

# Shared with tests/browser/test_format_count_parity.py, which runs the SAME table through the shipped JS formatter.
COUNT_CASES: list[tuple[object, str]] = [
    (0, "0"),
    (7, "7"),
    (999, "999"),
    (1000, "1,000"),
    (12345, "12,345"),
    (145057, "145,057"),
    (1234567, "1,234,567"),
    (-1234, "-1,234"),
    (1000.0, "1,000"),
    ("1000", "1,000"),
    (" 94772 ", "94,772"),
    ("12+", "12+"),
    ("soon", "soon"),
    (12.5, "12.5"),
    (None, "\u2014"),
    (float("nan"), "\u2014"),
    (float("inf"), "\u2014"),
]


@pytest.mark.parametrize(("value", "expected"), COUNT_CASES)
def test_format_count(value: object, expected: str) -> None:
    assert format_count(value) == expected


def test_a_count_with_no_data_is_an_em_dash_never_zero() -> None:
    assert format_count(None) == "\u2014"
    assert format_count(0) == "0"
    assert format_count(True) == "\u2014"


@pytest.mark.parametrize("module_name", _modules_building_a_template_environment())
def test_every_template_environment_carries_the_thousands_filter(module_name: str) -> None:
    environment = importlib.import_module(module_name).templates.env

    assert environment.filters["thousands"] is format_count, f"{module_name} has no `thousands` filter"


def test_the_thousands_filter_renders_the_acceptance_values() -> None:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    register_page_name_globals(templates.env)

    rendered = templates.env.from_string(
        "{{ a | thousands }}|{{ b | thousands }}|{{ c | thousands }}|{{ d | thousands }}|{{ e | thousands }}"
    ).render(a=0, b=999, c=1000, d=145057, e=None)

    assert rendered == "0|999|1,000|145,057|\u2014"


def test_jinjas_own_count_filter_is_untouched() -> None:
    """``count`` is a builtin alias of ``length``; the formatter is deliberately named ``thousands``."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    register_page_name_globals(templates.env)

    assert templates.env.from_string("{{ items | count }}").render(items=[1, 2, 3]) == "3"
