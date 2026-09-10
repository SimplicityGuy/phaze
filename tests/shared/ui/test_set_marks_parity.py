"""The poster draws the SAME wheel and the same energy area as the record page.

WHAT THIS IS FOR. `record/poster.svg` used to re-implement the Camelot wheel's three loops and
the energy area's segment loop from `record/partials/_harmonic_wheel.html` and
`proposals/partials/analysis_timeline.html`, coordinate expression for coordinate expression,
with only its colour literals differing -- the page paints with Tailwind classes and
`currentColor`, the poster must be self-contained with no stylesheet to inherit from. Both now
render `ui/set_marks.html` (PR #556 review, finding 4).

That is a claim no existing test could see. `tests/shared/routers/test_poster.py` asserts the
poster is well-formed SVG with no script and no external reference; `test_record_page_layout.py`
counts the page's sectors, nodes and edges. Each is right about its own surface, and a poster
whose wheel had quietly drifted a degree from the page's passes both. So this renders BOTH
templates from ONE `build_harmonic_journey` result and compares the numbers they actually
emitted.

Rendered through a bare `Jinja2Templates` rather than the app, because the claim is about the
templates: no route, no session, and no DB row is involved in whether two files agree on a
coordinate.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
import uuid

from fastapi.templating import Jinja2Templates

from phaze.models.analysis import AnalysisWindow
from phaze.services.analysis_timeline import build_analysis_timeline_context
from phaze.services.harmonic_journey import build_harmonic_journey
from phaze.web.template_globals import register_set_glyph_globals


TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_set_glyph_globals(_templates.env)

# Every numeric attribute the two surfaces both emit for a mark. Colour is deliberately absent:
# it is the ONE thing the palettes are allowed to differ on, and asserting it would make this
# test fail for the intended difference instead of the unintended one.
_GEOMETRY = ("d", "x", "y", "cx", "cy", "r", "x1", "y1", "x2", "y2", "points")
# `[0-9]` in the NAME class is load-bearing: `x1`/`y2` are the edge endpoints, and a class of
# letters and hyphens alone silently matches nothing on them -- which drops every `<line>` from
# the comparison and leaves a parity test that never looks at an edge.
_ATTR = re.compile(r'(?P<name>[a-zA-Z0-9-]+)="(?P<value>[^"]*)"')
_TAG = re.compile(r"<(?P<tag>path|text|line|circle|polygon|polyline)\b(?P<attrs>[^>]*)>")


def _nested_svg(markup: str, viewbox: str) -> str:
    """The one nested ``<svg>`` whose ``viewBox`` is ``viewbox``, up to its closing tag.

    The poster is ONE document with the header text, both nested pictures and the tracklist in
    it, and every `<text>` in it carries an x and a y. Comparing the whole file against a
    partial would fail on the poster's own title line -- a difference that is the point of the
    poster, not a drift. Slicing to the nested viewBox compares the two pictures and nothing
    else."""
    start = markup.index(f'viewBox="{viewbox}"')
    opening = markup.rindex("<svg", 0, start)
    return markup[opening : markup.index("</svg>", start)]


def _geometry(markup: str) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    """Every drawn mark as ``(tag, sorted geometry attributes)``, in document order."""
    marks = []
    for tag in _TAG.finditer(markup):
        attrs = {match["name"]: match["value"] for match in _ATTR.finditer(tag["attrs"])}
        geometry = tuple(sorted((name, value) for name, value in attrs.items() if name in _GEOMETRY))
        if geometry:
            marks.append((tag["tag"], geometry))
    return marks


def _fine(index: int, start: float, end: float, camelot: str) -> AnalysisWindow:
    return AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=index, start_sec=start, end_sec=end, bpm=128.0, camelot=camelot)


def _coarse(index: int, start: float, end: float, energy: float) -> AnalysisWindow:
    return AnalysisWindow(file_id=uuid.uuid4(), tier="coarse", window_index=index, start_sec=start, end_sec=end, energy=energy)


# A journey with BOTH edge kinds: 8A -> 9A is a wheel-adjacent move, 9A -> 3A is a jump. The two
# render through different palette entries, so a parity check over one kind alone would miss a
# drift in the other.
_KEY_PATH = ("8A", "8A", "9A", "9A", "3A", "3A")
_FINE_WINDOWS = [_fine(index, index * 30.0, index * 30.0 + 30.0, code) for index, code in enumerate(_KEY_PATH)]
# Coarse coverage with a HOLE in it: two windows, a gap, then two more. That makes the lane draw
# two separate areas (plus their polylines) rather than one, so a poster that rendered only the
# first run -- the shape a re-inlined loop with a subtly different guard would produce -- differs
# from the page in the mark LIST and not merely in a coordinate.
_COARSE_WINDOWS = [
    _coarse(0, 0.0, 90.0, 0.3),
    _coarse(1, 90.0, 180.0, 0.9),
    _coarse(2, 900.0, 990.0, 0.4),
    _coarse(3, 990.0, 1080.0, 0.7),
]


def _poster_context() -> dict[str, object]:
    """The poster's non-wheel context, filled with values it only ever prints or positions by."""
    layout = SimpleNamespace(
        width=800.0,
        height=1000.0,
        margin=40.0,
        title_y=60.0,
        filename_y=80.0,
        arc_label_y=110.0,
        arc_y=120.0,
        arc_w=720.0,
        arc_h=160.0,
        arc_note_y=290.0,
        wheel_label_y=320.0,
        wheel_x=40.0,
        wheel_y=330.0,
        wheel_size=260.0,
        wheel_caption_y=610.0,
        tracklist_label_y=640.0,
        tracklist_y=660.0,
        track_row_h=16.0,
    )
    return {
        "poster": layout,
        "poster_title": "A Set",
        "file": SimpleNamespace(original_filename_repaired=None, original_filename="set.mp3"),
        "poster_track_rows": [],
        "tracklist_ticks": [],
        "energy_peak": None,
        "has_coarse_windows": True,
    }


def test_the_poster_and_the_page_draw_the_same_wheel_for_one_journey() -> None:
    """Sector paths, sector label positions, edge endpoints and node centres/radii, all identical.

    Compared as an ordered list of marks, so a dropped node or a re-ordered group fails as
    loudly as a moved one. The page emits the inspection ring on top of this list -- a
    `<circle>` at radius 0 that the poster has no use for -- so it is excluded by
    `data-journey-cursor-ring` before the comparison rather than by trimming a count.
    """
    journey = build_harmonic_journey(_FINE_WINDOWS)
    assert journey.has_journey, "the fixture must produce a real path, or this test compares two empty wheels"
    assert any(not edge.adjacent for edge in journey.edges), "the fixture must contain a JUMP, which renders through its own palette entry"

    viewbox = f"0 0 {journey.canvas} {journey.canvas}"
    page = _templates.get_template("record/partials/_harmonic_wheel.html").render(harmonic_journey=journey)
    poster = _templates.get_template("record/poster.svg").render(harmonic_journey=journey, energy_area=[], **_poster_context())

    page_wheel = "\n".join(line for line in _nested_svg(page, viewbox).splitlines() if "data-journey-cursor-ring" not in line)
    page_marks = _geometry(page_wheel)

    assert page_marks == _geometry(_nested_svg(poster, viewbox))
    # 12 sectors x (path + label) plus the journey's own edges and nodes: a slicing bug that
    # produced two empty lists would otherwise pass this test without comparing anything.
    assert len(page_marks) == 24 + len(journey.edges) + len(journey.nodes)


def test_the_poster_and_the_page_draw_the_same_energy_area_for_one_set() -> None:
    """The lane's polygons, its single-window dots and its polylines, at the same coordinates.

    Both surfaces are handed ONE `build_analysis_timeline_context` result -- the same builder the
    live record page and proposal row render from -- so the lane's native coordinate space
    (`timeline_w` x `lane_h`) is the same on both and the marks inside it are directly
    comparable numbers. The page's lane carries a peak marker the poster's does not; the
    comparison is sliced to the lane's own nested `<svg>` and `energy_peak` is dropped from both
    contexts, because that difference is deliberate and would otherwise fail this test for the
    wrong reason.
    """
    context = build_analysis_timeline_context([*_FINE_WINDOWS, *_COARSE_WINDOWS])
    assert context["energy_area"], "the fixture must produce at least one drawn area"
    lane_viewbox = f"0 0 {context['timeline_w']} {context['lane_h']}"

    page = _templates.get_template("proposals/partials/analysis_timeline.html").render({**context, "file_id": uuid.uuid4(), "energy_peak": None})
    poster = _templates.get_template("record/poster.svg").render(
        harmonic_journey=build_harmonic_journey(_FINE_WINDOWS),
        energy_area=context["energy_area"],
        timeline_w=context["timeline_w"],
        lane_h=context["lane_h"],
        **_poster_context(),
    )

    page_marks = _geometry(_nested_svg(page, lane_viewbox))
    assert page_marks == _geometry(_nested_svg(poster, lane_viewbox))
    assert len(page_marks) >= 2, "a fixture producing no marks would make this pass vacuously"
