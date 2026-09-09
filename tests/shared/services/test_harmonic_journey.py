"""phaze-x1qr3.8: the harmonic-journey wheel's geometry, classification, and caption.

Pure-function tests over ``AnalysisWindow`` rows -- no DB, no client, no browser -- mirroring
``test_set_projection.py``'s idiom. The rendered-markup half of the contract (sector tints,
dashed amber jumps, the sidebar) lives in ``tests/shared/routers/test_record_page_layout.py``;
this module owns the numbers underneath it.

The single rule most of these exist to hold: the wheel and the ``harmonic_discipline``
percentage captioned beside it must be derived from ONE filtered sequence and ONE adjacency
predicate. Two of the tests below would pass with a second, drifting implementation of either
and then disagree in front of an operator, so they assert the agreement directly rather than
asserting a hand-counted literal on each side.
"""

from __future__ import annotations

import math
import uuid

import pytest

from phaze.models.analysis import AnalysisWindow
from phaze.services.harmonic_journey import (
    CANVAS,
    MAJOR_RING_R,
    MINOR_RING_R,
    NODE_R_MAX,
    NODE_R_MIN,
    build_harmonic_journey,
)
from phaze.services.set_glyph_colors import camelot_hue
from phaze.services.set_projection import flicker_filtered_key_runs, harmonic_discipline


_WINDOW_SEC = 30.0


def _fine(codes: list[str | None], window_sec: float = _WINDOW_SEC) -> list[AnalysisWindow]:
    """One fine window per code, contiguous and in order. ``None`` is a window with no key."""
    return [
        AnalysisWindow(
            file_id=uuid.uuid4(),
            tier="fine",
            window_index=index,
            start_sec=index * window_sec,
            end_sec=(index + 1) * window_sec,
            camelot=code,
        )
        for index, code in enumerate(codes)
    ]


def test_the_wheel_is_drawn_even_when_there_is_no_key_data() -> None:
    """A file with no ``camelot`` anywhere gets the frame of reference and nothing plotted on it."""
    journey = build_harmonic_journey(_fine([None, None, None]))

    assert journey.has_journey is False
    assert journey.nodes == []
    assert journey.edges == []
    assert journey.caption == ""
    assert journey.adjacent_share is None
    assert [sector.number for sector in journey.sectors] == list(range(1, 13))


def test_no_windows_at_all_is_the_same_empty_answer_and_raises_nothing() -> None:
    """The record renders for a file that was never analyzed; the wheel must not be the exception."""
    journey = build_harmonic_journey([])

    assert journey.has_journey is False
    assert len(journey.sectors) == 12


def test_every_sector_is_tinted_with_the_glyphs_own_camelot_hue() -> None:
    """The wheel shares ``camelot_hue`` with the set glyph, so one position is one colour."""
    journey = build_harmonic_journey([])

    assert [sector.hue for sector in journey.sectors] == [camelot_hue(number) for number in range(1, 13)]
    # 12 evenly spaced hues around the circle, none repeated -- the property the shared formula
    # provides and a per-surface re-derivation would be free to break.
    assert len({sector.hue for sector in journey.sectors}) == 12


def test_the_node_count_is_the_key_runs_after_the_flicker_filter() -> None:
    """A one-window blip is noise: it is neither a node nor a pair of transitions."""
    windows = _fine(["8A", "8A", "3A", "8A", "8A", "9A", "9A"])
    journey = build_harmonic_journey(windows)

    assert [node.code for node in journey.nodes] == ["8A", "9A"]
    assert [node.code for node in journey.nodes] == [run.code for run in flicker_filtered_key_runs(windows)]
    assert len(journey.edges) == 1


def test_the_wheels_adjacent_share_equals_the_stored_projections_discipline_figure() -> None:
    """One filter and one predicate: the picture and the number captioning it cannot disagree.

    This is the test that would fail if the wheel ever grew a flicker filter or an adjacency
    rule of its own, which is the whole reason both live in ``set_projection``.
    """
    windows = _fine(["8A", "8A", "9A", "9A", "3A", "9A", "9A", "5A", "5A", "10B", "10B", "5A", "5A"])
    journey = build_harmonic_journey(windows)

    assert journey.adjacent_share == harmonic_discipline(windows)
    assert journey.adjacent_share == pytest.approx(0.25)


def test_a_relative_major_minor_move_is_adjacent_and_a_tritone_is_a_jump() -> None:
    """Both adjacency shapes are honoured: a step around the wheel, and the radial pair."""
    step = build_harmonic_journey(_fine(["8A", "8A", "9A", "9A"]))
    relative = build_harmonic_journey(_fine(["8A", "8A", "8B", "8B"]))
    across = build_harmonic_journey(_fine(["8A", "8A", "2A", "2A"]))

    assert [edge.adjacent for edge in step.edges] == [True]
    assert [edge.adjacent for edge in relative.edges] == [True]
    assert [edge.adjacent for edge in across.edges] == [False]
    assert across.edges[0].kind == "jump"


def test_minor_keys_ride_the_inner_ring_and_majors_the_outer_one() -> None:
    """Camelot's own layout, so a relative-major move draws as a short radial hop."""
    journey = build_harmonic_journey(_fine(["8A", "8A", "8B", "8B"]))
    minor, major = journey.nodes

    centre = CANVAS / 2.0
    assert math.hypot(minor.cx - centre, minor.cy - centre) == pytest.approx(MINOR_RING_R, abs=0.05)
    assert math.hypot(major.cx - centre, major.cy - centre) == pytest.approx(MAJOR_RING_R, abs=0.05)


def test_position_one_sits_at_the_top_and_positions_ascend_clockwise() -> None:
    """The wheel reads like a clock, not like SVG's zero-at-three-o'clock convention."""
    journey = build_harmonic_journey(_fine(["1A", "1A", "4A", "4A", "7A", "7A", "10A", "10A"]))
    centre = CANVAS / 2.0
    at_one, at_four, at_seven, at_ten = journey.nodes

    assert at_one.cx == pytest.approx(centre, abs=0.05) and at_one.cy < centre  # 12 o'clock
    assert at_four.cy == pytest.approx(centre, abs=0.05) and at_four.cx > centre  # 3 o'clock
    assert at_seven.cx == pytest.approx(centre, abs=0.05) and at_seven.cy > centre  # 6 o'clock
    assert at_ten.cy == pytest.approx(centre, abs=0.05) and at_ten.cx < centre  # 9 o'clock


def test_node_area_not_radius_tracks_dwell_and_the_longest_run_is_the_largest() -> None:
    """A four-times-longer run draws a two-times-wider dot, not a four-times-wider one."""
    # "8A" spans 8 windows, "9A" spans 2 -- a 4x dwell ratio, so a 2x radius ratio above the
    # floor. The trailing "5A" run is only there to close "9A"'s run at a known boundary.
    journey = build_harmonic_journey(_fine(["8A"] * 8 + ["9A"] * 2 + ["5A"] * 2))
    long_run, short_run, _ = journey.nodes

    assert long_run.r == NODE_R_MAX
    assert short_run.r == pytest.approx(NODE_R_MIN + 0.5 * (NODE_R_MAX - NODE_R_MIN), abs=0.02)
    assert NODE_R_MIN <= short_run.r < long_run.r


def test_a_run_with_no_measurable_extent_still_places_at_the_minimum_radius() -> None:
    """Degenerate bounds are a data problem, never a division by zero on a render path."""
    windows = _fine(["8A", "8A"], window_sec=0.0)
    journey = build_harmonic_journey(windows)

    assert [node.r for node in journey.nodes] == [NODE_R_MIN]


def test_a_move_is_timed_at_the_moment_it_lands_not_when_the_previous_key_began() -> None:
    """The caption's time is where an operator scrubbing the timeline would hear the change."""
    journey = build_harmonic_journey(_fine(["8A", "8A", "5A", "5A"]))
    edge = journey.edges[0]

    assert edge.at_sec == pytest.approx(2 * _WINDOW_SEC)
    assert edge.at_label == "1:00"
    assert edge.label == "8A → 5A"


def test_the_caption_names_the_adjacent_share_and_every_jump_with_its_elapsed_time() -> None:
    """The text alternative carries the one thing in the picture an operator has to act on."""
    journey = build_harmonic_journey(_fine(["8A", "8A", "9A", "9A", "3A", "9A", "9A", "5A", "5A", "10B", "10B", "5A", "5A"]))

    assert journey.caption == ("5 key runs, 25% of 4 moves wheel-adjacent. Jumps: 9A → 5A at 3:30; 5A → 10B at 4:30; 10B → 5A at 5:30.")
    assert [edge.label for edge in journey.jumps] == ["9A → 5A", "5A → 10B", "10B → 5A"]


def test_a_fully_disciplined_set_says_so_rather_than_listing_nothing() -> None:
    """No jumps is a reading in its own right, and reads as one."""
    journey = build_harmonic_journey(_fine(["8A", "8A", "9A", "9A", "10A", "10A"]))

    assert journey.jumps == []
    assert journey.caption == "3 key runs, 100% of 2 moves wheel-adjacent. No jumps."


def test_a_set_that_never_changes_key_has_one_node_and_no_moves_to_report() -> None:
    """Zero transitions is the trivially disciplined case, and must not read as 0% adjacent."""
    journey = build_harmonic_journey(_fine(["8A"] * 6))

    assert len(journey.nodes) == 1
    assert journey.edges == []
    assert journey.adjacent_share == 1.0
    assert journey.caption == "1 key run, no key changes."


def test_a_code_the_wheel_cannot_place_is_dropped_from_the_picture_not_placed_at_random() -> None:
    """``camelot`` is constrained to 24 values; anything else is excluded, never guessed at."""
    journey = build_harmonic_journey(_fine(["8A", "8A", "99Z", "99Z", "9A", "9A"]))

    assert [node.code for node in journey.nodes] == ["8A", "9A"]
    assert [edge.adjacent for edge in journey.edges] == [True]


def test_every_coordinate_is_a_finite_number_inside_the_canvas() -> None:
    """Coordinates reach SVG attributes directly, so none may be NaN, infinite, or off-canvas."""
    journey = build_harmonic_journey(_fine([f"{number}{letter}" for number in range(1, 13) for letter in "AB" for _ in (0, 1)]))

    coordinates = [value for node in journey.nodes for value in (node.cx, node.cy)]
    coordinates += [value for edge in journey.edges for value in (edge.x1, edge.y1, edge.x2, edge.y2)]
    coordinates += [value for sector in journey.sectors for value in (sector.label_x, sector.label_y)]
    assert coordinates
    assert all(math.isfinite(value) and 0.0 <= value <= CANVAS for value in coordinates)


def test_every_sector_path_is_numeric_svg_with_no_interpolated_label_text() -> None:
    """The path grammar is arcs and numbers only -- the timeline's injection-safety convention."""
    allowed = set("MALZ0123456789.- ")

    for sector in build_harmonic_journey([]).sectors:
        assert set(sector.path) <= allowed, f"sector {sector.number} path carries non-numeric syntax"
        assert sector.path.startswith("M ") and sector.path.endswith(" Z")
