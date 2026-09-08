"""phaze-x1qr3.5: the set panel's lanes -- energy area, mood river, key ribbons, ticks, chip.

Every assertion here is on the NUMBERS the template renders, not on prose about them: the
energy path's own coordinates, the river's own stacked boundaries, the polygon COUNT per
series either side of a coverage hole. That is the only way this file can catch the failure it
exists to catch, which is a lane that quietly draws across a gap and so shows an operator
continuous coverage the archive does not have.

The lanes read the stored projection columns (``energy``, ``camelot``, ``mood_scores``) and
never ``features`` -- ``phaze-x1qr3.3`` is what fills them, so a window constructed here
without them is exactly the "not yet projected" row the lanes must render as a hole.
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path
import re
import uuid

import pytest

from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.services.analysis_timeline import (
    LANE_H,
    MIN_MOOD_HUE_SEPARATION_DEG,
    MOOD_HUES,
    MOOD_LABELS,
    MOOD_NAMES,
    build_analysis_timeline_context,
    coverage_chip,
    energy_area,
    energy_peak,
    mood_legend,
    mood_river,
    mood_river_columns,
    mood_stack,
    tracklist_ticks,
)
from phaze.services.set_projection import MOOD_ORDER


_SERVICES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "services"
_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_TIMELINE_TEMPLATE = _TEMPLATES / "proposals" / "partials" / "analysis_timeline.html"
_TRACKLIST_TEMPLATE = _TEMPLATES / "record" / "partials" / "_tracklist_review_body.html"


def _coarse(index: int, start: float, end: float, *, energy: float | None = None, moods: dict[str, float] | None = None) -> AnalysisWindow:
    return AnalysisWindow(
        file_id=uuid.uuid4(),
        tier="coarse",
        window_index=index,
        start_sec=start,
        end_sec=end,
        energy=energy,
        mood_scores=moods,
    )


def _fine(index: int, start: float, end: float, *, bpm: float | None = None, key: str | None = None, camelot: str | None = None) -> AnalysisWindow:
    return AnalysisWindow(
        file_id=uuid.uuid4(),
        tier="fine",
        window_index=index,
        start_sec=start,
        end_sec=end,
        bpm=bpm,
        musical_key=key,
        camelot=camelot,
    )


def _even_moods(**overrides: float) -> dict[str, float]:
    """A full 7-mood score dict, uniform unless a name is overridden."""
    scores = dict.fromkeys(MOOD_NAMES, 0.1)
    scores.update(overrides)
    return scores


class _Boundary:
    """The two fields ``tracklist_ticks`` reads -- structurally phaze-x1qr3.6's ``TrackSegment``."""

    def __init__(self, position: int, start_sec: float) -> None:
        self.position = position
        self.start_sec = start_sec


# --- The palette: one source, and validated as categorical -------------------------------


def test_the_seven_mood_hues_are_one_module_level_source_no_surface_duplicates() -> None:
    """MOOD_HUES is the only place a mood's colour is decided, for all three surfaces.

    Discharges phaze-x1qr3.5's "the 7 mood hues are ... in one module-level constant used by
    the river, the legend and the tracklist mood dots (a test asserts one source)". The three
    consumers are checked directly: the river's series and the legend's entries must carry
    exactly this table's values, and NEITHER template may contain a literal ``hsl(<number>``
    for a mood -- the tracklist's dot has to receive its hue as data, like the other two.
    """
    assert set(MOOD_HUES) == set(MOOD_NAMES)
    assert len(MOOD_HUES) == 7

    windows = [_coarse(0, 0.0, 180.0, energy=0.5, moods=_even_moods()), _coarse(1, 180.0, 360.0, energy=0.6, moods=_even_moods())]
    river = mood_river(windows, 360.0, 600.0, LANE_H)
    assert {series["name"]: series["hue"] for series in river} == MOOD_HUES
    assert {entry["name"]: entry["hue"] for entry in mood_legend()} == MOOD_HUES

    # A mood hue may never be spelled as a literal in a template: every one arrives as data.
    literal_hsl = re.compile(r"hsl\(\s*\d")
    for template in (_TIMELINE_TEMPLATE, _TRACKLIST_TEMPLATE):
        assert not literal_hsl.search(template.read_text()), f"{template.name} hard-codes a hue; it must read one from MOOD_HUES"

    # ...and only one module in services/ may DEFINE the table.
    definitions = [path.name for path in _SERVICES.rglob("*.py") if re.search(r"^MOOD_HUES\b", path.read_text(), re.MULTILINE)]
    assert definitions == ["analysis_timeline.py"], f"MOOD_HUES is defined in more than one module: {definitions}"


def test_the_mood_palette_is_categorical_not_a_hash_of_the_names() -> None:
    """All 7 hues sit at least MIN_MOOD_HUE_SEPARATION_DEG apart around the circle.

    This is what "validated categorical palette" means operationally, and it is the property a
    character-sum hash does NOT have: ``hue_for`` puts ``mood_electronic`` and
    ``mood_aggressive`` 8 degrees apart, which is indistinguishable where two bands of a
    stacked river touch. Asserted rather than asserted-about, so moving any hue into a
    neighbour fails the build.
    """
    hues = sorted(MOOD_HUES.values())
    assert all(0 <= hue < 360 for hue in hues)
    gaps = [second - first for first, second in itertools.pairwise(hues)]
    gaps.append(360 - hues[-1] + hues[0])
    assert min(gaps) >= MIN_MOOD_HUE_SEPARATION_DEG, f"two moods are only {min(gaps)} degrees apart: {MOOD_HUES}"


def test_the_river_stacks_in_mood_orders_own_order_and_every_band_carries_a_label() -> None:
    """Stacking order is MOOD_ORDER's, and colour is never the only channel."""
    assert tuple(name for name in MOOD_ORDER if name.startswith("mood_")) == MOOD_NAMES
    assert [series["name"] for series in mood_river([], 0.0, 600.0, LANE_H)] == list(MOOD_NAMES)
    assert all(MOOD_LABELS[name] for name in MOOD_NAMES)
    assert [entry["label"] for entry in mood_legend()] == [MOOD_LABELS[name] for name in MOOD_NAMES]


# --- Energy lane -------------------------------------------------------------------------


def test_energy_path_points_are_the_projected_energies_at_window_midpoints() -> None:
    """The drawn coordinates equal the stored energies, positioned at window midpoints.

    Hand-computed: the file runs 0..360 s over a 600-unit canvas, so window 0's midpoint (90 s)
    is x=150 and window 1's (270 s) is x=450; energy 0.25 sits a quarter up a 56-unit lane
    (y=42) and 0.75 three quarters up (y=14).
    """
    windows = [_coarse(0, 0.0, 180.0, energy=0.25), _coarse(1, 180.0, 360.0, energy=0.75)]

    areas = energy_area(windows, 360.0, 600.0, 56.0)

    assert len(areas) == 1
    assert areas[0]["points"] == "150.00,42.00 450.00,14.00"
    # The filled area closes down to the baseline under the first and last points, and nowhere else.
    assert areas[0]["area_points"] == "150.00,42.00 450.00,14.00 450.00,56.00 150.00,56.00"


def test_the_energy_peak_is_marked_at_the_loudest_window_earliest_on_a_tie() -> None:
    windows = [_coarse(0, 0.0, 180.0, energy=0.4), _coarse(1, 180.0, 360.0, energy=0.9), _coarse(2, 360.0, 540.0, energy=0.9)]

    peak = energy_peak(windows, 540.0, 600.0, 56.0)

    assert peak is not None
    assert peak["energy"] == 0.9
    assert peak["sec"] == 270.0  # the EARLIER of the two 0.9 windows
    assert peak["label"] == "4:30"
    assert peak["y"] == pytest.approx(5.6)


def test_a_coverage_gap_produces_two_separate_energy_paths_never_one_bridged_one() -> None:
    """A time hole and an unprojected window each END a run; neither is drawn across.

    The discriminating check is the SEGMENT COUNT plus the absence of any coordinate inside the
    hole -- a single bridged path would still contain every measured point and would pass a
    "the points are right" assertion on its own.
    """
    windows = [
        _coarse(0, 0.0, 180.0, energy=0.5),
        _coarse(1, 180.0, 360.0, energy=0.6),
        _coarse(2, 900.0, 1080.0, energy=0.7),  # a 540 s hole in stored coverage
    ]

    areas = energy_area(windows, 1080.0, 600.0, 56.0)

    assert len(areas) == 2
    assert areas[0]["points"] == "50.00,28.00 150.00,22.40"
    assert areas[1]["single_x"] == pytest.approx(550.0)
    joined = " ".join(str(area["points"]) for area in areas)
    assert not any(200.0 < float(pair.split(",")[0]) < 540.0 for pair in joined.split()), "a path crosses the unmeasured hole"


def test_an_unprojected_window_breaks_the_energy_lane_rather_than_reading_as_zero() -> None:
    """``energy`` NULL is "not yet projected", which is a hole -- never a measured 0.0."""
    windows = [_coarse(0, 0.0, 180.0, energy=0.5), _coarse(1, 180.0, 360.0, energy=None), _coarse(2, 360.0, 540.0, energy=0.5)]

    areas = energy_area(windows, 540.0, 600.0, 56.0)

    assert len(areas) == 2
    assert all("56.00" not in str(area["points"]) for area in areas), "a NULL energy was drawn on the baseline"


# --- Mood river --------------------------------------------------------------------------


def test_the_rivers_seven_series_sum_to_the_lane_height_at_every_window() -> None:
    """The 7 band thicknesses at each window add up to exactly the lane height.

    Asserted on ``mood_river_columns``' boundaries, which are the numbers ``mood_river`` builds
    its polygons from -- so this is an assertion about the geometry that is drawn, not about a
    parallel calculation. A window with an uneven mood mix is used deliberately: uniform scores
    would pass even a normalisation that divided by a constant instead of the window's own sum.
    """
    windows = [
        _coarse(0, 0.0, 180.0, moods=_even_moods(mood_party=0.9, mood_sad=0.02)),
        _coarse(1, 180.0, 360.0, moods=_even_moods(mood_relaxed=0.7)),
    ]

    runs = mood_river_columns(windows, 360.0, 600.0, 56.0)

    assert len(runs) == 1
    for column in runs[0]:
        boundaries = column["boundaries"]
        assert isinstance(boundaries, list)
        assert len(boundaries) == len(MOOD_NAMES) + 1
        assert boundaries[0] == 56.0  # bottom of the lane
        assert boundaries[-1] == 0.0  # exactly the top: no sliver of background above the stack
        thicknesses = [boundaries[index] - boundaries[index + 1] for index in range(len(MOOD_NAMES))]
        assert sum(thicknesses) == pytest.approx(56.0)
        assert all(thickness >= 0 for thickness in thicknesses)

    # And the band widths are each mood's OWN share of that window's total, not an equal split.
    first = runs[0][0]["boundaries"]
    assert isinstance(first, list)
    party = first[MOOD_NAMES.index("mood_party")] - first[MOOD_NAMES.index("mood_party") + 1]
    total = 0.9 + 0.02 + 0.1 * 5
    assert party == pytest.approx(0.9 / total * 56.0)


def test_a_coverage_gap_breaks_the_river_and_is_never_bridged() -> None:
    """Every one of the 7 series gets TWO polygons across a hole, and none spans it."""
    windows = [
        _coarse(0, 0.0, 180.0, moods=_even_moods()),
        _coarse(1, 180.0, 360.0, moods=_even_moods()),
        _coarse(2, 3600.0, 3780.0, moods=_even_moods()),
        _coarse(3, 3780.0, 3960.0, moods=_even_moods()),
    ]

    series = mood_river(windows, 3960.0, 600.0, 56.0)

    assert len(series) == 7
    for entry in series:
        polygons = entry["polygons"]
        assert isinstance(polygons, list)
        assert len(polygons) == 2, f"{entry['name']} was drawn as one continuous band across the hole"
        xs = [float(pair.split(",")[0]) for polygon in polygons for pair in polygon.split()]
        assert not any(60.0 < x < 540.0 for x in xs), "a band has geometry inside the unmeasured hole"


def test_a_window_with_no_mood_scores_is_a_hole_not_a_flat_band() -> None:
    """``mood_scores`` NULL, or the fully-``None`` entry phaze-x1qr3.3 writes, is absence."""
    assert mood_stack(_coarse(0, 0.0, 180.0, moods=None)) is None
    assert mood_stack(_coarse(0, 0.0, 180.0, moods=dict.fromkeys(MOOD_NAMES, None))) is None  # type: ignore[arg-type]
    assert mood_stack(_coarse(0, 0.0, 180.0, moods=dict.fromkeys(MOOD_NAMES, 0.0))) is None

    windows = [_coarse(0, 0.0, 180.0, moods=_even_moods()), _coarse(1, 180.0, 360.0, moods=None), _coarse(2, 360.0, 540.0, moods=_even_moods())]
    assert all(len(entry["polygons"]) == 2 for entry in mood_river(windows, 540.0, 600.0, 56.0))  # type: ignore[arg-type]


def test_a_window_carrying_only_some_of_the_seven_normalises_over_what_it_has() -> None:
    """Partial data is partial data, not a hole -- discarding it would overstate the gap."""
    partial = {"mood_party": 0.6, "mood_sad": 0.2}
    stack = mood_stack(_coarse(0, 0.0, 180.0, moods=partial))

    assert stack is not None
    assert sum(stack) == pytest.approx(1.0)
    assert stack[MOOD_NAMES.index("mood_party")] == pytest.approx(0.75)
    assert stack[MOOD_NAMES.index("mood_acoustic")] == 0.0


# --- Key ribbons, ticks and the coverage chip --------------------------------------------


def test_key_ribbons_are_labelled_with_the_key_and_its_camelot_code() -> None:
    """ "A minor · 8A" -- the raw label stays available, the DISPLAY carries the wheel position."""
    windows = [_fine(0, 0.0, 30.0, key="A minor", camelot="8A"), _fine(1, 30.0, 60.0, key="C major", camelot=None)]

    context = build_analysis_timeline_context(windows)
    ribbons = context["key_ribbons"]

    assert isinstance(ribbons, list)
    assert ribbons[0]["label"] == "A minor"
    assert ribbons[0]["code"] == "8A"
    assert ribbons[0]["display"] == "A minor · 8A"
    # An unprojected window still shows its key: the Camelot half is additive, never a filter.
    assert ribbons[1]["display"] == "C major"


def test_one_tick_per_timestamped_track_and_none_for_a_track_without_one() -> None:
    """Ticks come from phaze-x1qr3.6's segments, which exist only for timestamped tracks."""
    ticks = tracklist_ticks([_Boundary(1, 0.0), _Boundary(3, 1800.0)], 3600.0)

    assert [tick["position"] for tick in ticks] == [1, 3]
    assert [tick["left_pct"] for tick in ticks] == [0.0, 50.0]
    assert [tick["time"] for tick in ticks] == ["0:00", "30:00"]
    # Track 2 carried no timestamp, produced no segment, and so contributes no tick at all.
    assert not any(tick["position"] == 2 for tick in ticks)


def test_a_file_without_a_tracklist_renders_no_tick_row_and_raises_nothing() -> None:
    assert tracklist_ticks([], 3600.0) == []
    assert build_analysis_timeline_context([_fine(0, 0.0, 30.0, bpm=128.0)])["tracklist_ticks"] == []


def test_the_coverage_chip_reads_the_analysis_rows_own_planned_versus_analyzed_counters() -> None:
    """ "gaps" the moment analyzed < total on either tier; "no gaps" only when both are complete."""
    complete = coverage_chip(
        AnalysisResult(file_id=uuid.uuid4(), fine_windows_analyzed=18, fine_windows_total=18, coarse_windows_analyzed=4, coarse_windows_total=4)
    )
    partial = coverage_chip(
        AnalysisResult(file_id=uuid.uuid4(), fine_windows_analyzed=17, fine_windows_total=18, coarse_windows_analyzed=4, coarse_windows_total=4)
    )
    coarse_short = coverage_chip(
        AnalysisResult(file_id=uuid.uuid4(), fine_windows_analyzed=18, fine_windows_total=18, coarse_windows_analyzed=3, coarse_windows_total=4)
    )

    assert complete is not None
    assert complete["text"] == "4 coarse · 18 fine · no gaps"
    assert complete["has_gaps"] is False
    assert partial is not None
    assert partial["text"] == "4 coarse · 17 fine · gaps"
    assert coarse_short is not None
    assert coarse_short["has_gaps"] is True


def test_no_analysis_row_and_no_counters_render_no_chip_rather_than_a_zero_claim() -> None:
    assert coverage_chip(None) is None
    assert coverage_chip(AnalysisResult(file_id=uuid.uuid4())) is None
    assert build_analysis_timeline_context([_fine(0, 0.0, 30.0, bpm=128.0)])["coverage_chip"] is None


# --- The fine-only file, and the lanes' text alternatives ---------------------------------


def test_a_fine_only_file_keeps_the_bpm_lane_and_says_so_in_the_energy_and_mood_lanes() -> None:
    """No coarse windows means no energy and no river -- said in words, never drawn as zero."""
    windows = [_fine(index, index * 30.0, (index + 1) * 30.0, bpm=124.0 + index) for index in range(6)]

    context = build_analysis_timeline_context(windows)

    assert context["bpm_segments"], "the BPM lane must still render for a fine-only file"
    assert context["has_coarse_windows"] is False
    assert context["energy_area"] == []
    assert context["energy_peak"] is None
    assert all(entry["polygons"] == [] for entry in context["mood_river"])  # type: ignore[union-attr]
    # ...and the template turns exactly that state into the explicit sentence.
    assert "No coarse windows for this file." in _TIMELINE_TEMPLATE.read_text()


def test_every_lane_carries_a_text_alternative_not_only_colour() -> None:
    """Each new lane is reachable without seeing it: an aria-label, a legend, or a text list."""
    html = _TIMELINE_TEMPLATE.read_text()

    assert 'data-timeline-lane="energy"' in html
    assert 'aria-label="Energy from 0.0 to 1.0 over elapsed time' in html
    assert 'data-timeline-lane="mood-river"' in html
    assert 'aria-label="Mood river:' in html
    # The river's bands are named twice over: a per-band <title> and the legend's labels.
    assert "<title>{{ series.label }}</title>" in html
    assert 'aria-label="Mood river colours"' in html
    # The boundary rules are decorative; the readable form is the sentence below the timeline.
    assert 'data-timeline-boundaries aria-hidden="true"' in html
    assert "data-timeline-boundary-text" in html
    assert "Track boundaries:" in html


def test_the_lane_height_constant_and_the_stylesheet_agree() -> None:
    """LANE_H drives the SVG viewBox; the CSS sets the box the SVG is drawn into.

    They are two halves of one number in two languages, so a change to either alone renders
    the geometry at the wrong scale -- silently, since neither errors.
    """
    css = (Path(__file__).resolve().parents[3] / "assets" / "src" / "app.css").read_text()
    assert f"height: {LANE_H / 16:g}rem" in css.split(".analysis-timeline-lane-plot {")[1].split("}")[0] + "}"


def test_the_context_exposes_every_lane_the_template_reads() -> None:
    """A renamed context key would render an EMPTY lane, not an error -- so pin the names."""
    windows = [
        _fine(0, 0.0, 30.0, bpm=128.0, key="A minor", camelot="8A"),
        _coarse(0, 0.0, 180.0, energy=0.5, moods=_even_moods()),
    ]

    context = build_analysis_timeline_context(windows, track_segments=[_Boundary(1, 0.0)])

    for key in ("energy_area", "energy_peak", "mood_river", "mood_legend", "tracklist_ticks", "coverage_chip", "lane_h", "has_coarse_windows"):
        assert key in context, f"the template reads {key} and the context no longer provides it"
    assert context["lane_h"] == LANE_H
    assert math.isclose(float(str(context["total_sec"])), 180.0)
