"""phaze-jvo7i: the mood river at sub-window resolution, and the beat ruler.

Every acceptance criterion of the bead maps to one test here (the file names them):

- the river draws one band STEP per sub-window when sub-window mood exists, and today's coarse
  river otherwise -- ``test_one_step_per_sub_window_*``, ``test_no_sub_window_rows_*`` and the
  rendered-markup tests at the bottom;
- the river keeps the shared ``MOOD_ORDER`` stacking and hue assignment --
  ``test_the_subwindow_river_keeps_mood_order_and_the_shared_hues``;
- the beat ruler never draws more ticks than its per-pixel budget --
  ``test_the_beat_ruler_never_exceeds_its_per_pixel_budget``;
- the beat ruler shows recorded gaps as gaps and draws no tick inside one --
  ``test_recorded_gaps_are_spans_and_no_tick_falls_inside_one``.

The geometry is asserted on the NUMBERS the template draws (the polygon corners), and the markup
tests render the shipped partial, so a renamed key cannot quietly empty the lane.
"""

from __future__ import annotations

import itertools
from pathlib import Path
import re
import uuid

from fastapi.templating import Jinja2Templates
import pytest

from phaze.models.analysis import AnalysisBeatGrid, AnalysisSubwindow, AnalysisWindow
from phaze.services.analysis_timeline import (
    BEAT_TICK_SPACING_PX,
    LANE_H,
    MOOD_HUES,
    MOOD_NAMES,
    beat_ruler,
    beat_tick_budget,
    build_analysis_timeline_context,
    mood_river,
    resolved_mood_river,
    subwindow_mood_river,
)
from phaze.services.set_projection import MOOD_ORDER


_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_PARTIAL = "proposals/partials/analysis_timeline.html"
_POINT = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def _sub(index: int, sub: int, start: float, end: float, **moods: float | None) -> AnalysisSubwindow:
    """One sub-window row, its mood vector positional in MOOD_ORDER (absent moods read 0.0).

    ``moods=None``-valued entries, or passing no moods at all, make the NULL slice the writer stores.
    """
    file_id = _FILE_ID
    scores = None if not moods else [moods.get(name, 0.0) if moods.get(name) is not None else None for name in MOOD_ORDER]
    return AnalysisSubwindow(file_id=file_id, window_index=index, sub_index=sub, start_sec=start, end_sec=end, mood_scores=scores)


_FILE_ID = uuid.uuid4()


def _even(**overrides: float) -> dict[str, float]:
    scores = dict.fromkeys(MOOD_NAMES, 0.1)
    scores.update(overrides)
    return scores


def _coarse(index: int, start: float, end: float, moods: dict[str, float] | None) -> AnalysisWindow:
    return AnalysisWindow(file_id=_FILE_ID, tier="coarse", window_index=index, start_sec=start, end_sec=end, mood_scores=moods)


def _points(polygon: str) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in _POINT.findall(polygon)]


def _four_slices() -> list[AnalysisSubwindow]:
    """A 60 s file: one coarse window of four 15 s slices, the first two dominated by ``mood_party``."""
    return [
        _sub(0, 0, 0.0, 15.0, mood_party=0.9, mood_sad=0.1),
        _sub(0, 1, 15.0, 30.0, mood_party=0.9, mood_sad=0.1),
        _sub(0, 2, 30.0, 45.0, mood_relaxed=1.0),
        _sub(0, 3, 45.0, 60.0, mood_relaxed=1.0),
    ]


# --- the river ----------------------------------------------------------------------------------


def test_one_step_per_sub_window_when_sub_window_mood_exists() -> None:
    """Each sub-window is one flat step: a 60 s file with four slices has four steps per series.

    A step polygon has 4 corners per step (two on its top edge, two on its bottom), so one polygon per
    series carries exactly 16 points, and the x-edges are the four slices' own start and end.
    """
    series = subwindow_mood_river(_four_slices(), 60.0, 600.0, LANE_H)

    assert len(series) == len(MOOD_NAMES)
    for entry in series:
        polygons = entry["polygons"]
        assert isinstance(polygons, list)
        assert len(polygons) == 1, f"{entry['name']} should be one continuous stepped band here"
        assert len(_points(polygons[0])) == 16

    party = next(entry for entry in series if entry["name"] == "mood_party")
    polygons = party["polygons"]
    assert isinstance(polygons, list)
    xs = sorted({x for x, _ in _points(polygons[0])})
    assert xs == pytest.approx([0.0, 150.0, 300.0, 450.0, 600.0])


def test_each_step_carries_its_own_share_not_the_window_mean() -> None:
    """The first two slices are 90% party; the last two are 0% party. A mean would read ~45% everywhere."""
    series = subwindow_mood_river(_four_slices(), 60.0, 600.0, LANE_H)
    party = next(entry for entry in series if entry["name"] == "mood_party")
    polygons = party["polygons"]
    assert isinstance(polygons, list)
    points = _points(polygons[0])
    # Top edge: the first four corners are slice 0 (x 0..150), the next four slice 1 (150..300),
    # then slices 2 and 3 hold the band at zero thickness (top == bottom).
    # SVG y grows downwards: a band's thickness is its bottom edge minus its top edge.
    thickness = [points[-(i + 1)][1] - points[i][1] for i in range(0, 8, 2)]
    assert thickness[0] == pytest.approx(0.9 * LANE_H, abs=0.02)
    assert thickness[1] == pytest.approx(0.9 * LANE_H, abs=0.02)


def test_the_subwindow_river_keeps_mood_order_and_the_shared_hues() -> None:
    """Stacking follows MOOD_NAMES and every band takes its hue from MOOD_HUES, never a second table."""
    series = subwindow_mood_river(_four_slices(), 60.0, 600.0, LANE_H)

    assert [entry["name"] for entry in series] == list(MOOD_NAMES)
    for entry in series:
        assert entry["hue"] == MOOD_HUES[str(entry["name"])]


def test_a_null_slice_breaks_the_river_and_is_never_bridged() -> None:
    """A slice with no patch centre (NULL mood) is a recorded gap: two polygons, nothing across it."""
    rows = [
        _sub(0, 0, 0.0, 15.0, mood_party=1.0),
        _sub(0, 1, 15.0, 30.0),  # NULL: no moods at all
        _sub(0, 2, 30.0, 45.0, mood_party=1.0),
    ]
    series = subwindow_mood_river(rows, 45.0, 450.0, LANE_H)

    for entry in series:
        polygons = entry["polygons"]
        assert isinstance(polygons, list)
        assert len(polygons) == 2, f"{entry['name']} bridged the NULL slice"


def test_a_time_hole_between_sub_windows_breaks_the_river() -> None:
    """A silent coarse window has no sub-window rows at all: the hole in time breaks the run."""
    rows = [_sub(0, 0, 0.0, 15.0, mood_party=1.0), _sub(2, 0, 360.0, 375.0, mood_party=1.0)]
    series = subwindow_mood_river(rows, 375.0, 750.0, LANE_H)
    assert all(len(entry["polygons"]) == 2 for entry in series)  # type: ignore[arg-type]


def test_no_sub_window_rows_falls_back_to_the_coarse_river_exactly() -> None:
    """With no sub-window rows the river IS today's coarse river, polygon for polygon."""
    coarse = [_coarse(0, 0.0, 180.0, _even(mood_party=0.9)), _coarse(1, 180.0, 360.0, _even(mood_relaxed=0.7))]

    river, resolution = resolved_mood_river(coarse, [], 360.0, 640.0, LANE_H)

    assert resolution == "coarse"
    assert river == mood_river(coarse, 360.0, 640.0, LANE_H)


def test_sub_window_rows_with_no_usable_mood_also_fall_back_to_coarse() -> None:
    """Rows that exist but carry no mood at all are gaps, so the coarse river stays the answer."""
    coarse = [_coarse(0, 0.0, 180.0, _even())]
    rows = [_sub(0, 0, 0.0, 15.0), _sub(0, 1, 15.0, 30.0)]

    river, resolution = resolved_mood_river(coarse, rows, 180.0, 640.0, LANE_H)

    assert resolution == "coarse"
    assert river == mood_river(coarse, 180.0, 640.0, LANE_H)


def test_sub_window_rows_select_the_subwindow_river_and_say_so() -> None:
    coarse = [_coarse(0, 0.0, 60.0, _even())]
    _, resolution = resolved_mood_river(coarse, _four_slices(), 60.0, 600.0, LANE_H)
    assert resolution == "subwindow"


# --- the beat ruler -----------------------------------------------------------------------------


def _grid(beats: list[float], gaps: list[list[float]] | None = None) -> AnalysisBeatGrid:
    return AnalysisBeatGrid(file_id=_FILE_ID, beats=beats, beat_count=len(beats), gaps=gaps or [])


def test_the_beat_ruler_never_exceeds_its_per_pixel_budget() -> None:
    """30,000 beats over three hours on a 640 px canvas: the ruler draws at most the stated budget.

    The budget is ``floor(width / BEAT_TICK_SPACING_PX) + 1``; no two drawn ticks are closer than the
    spacing, which is what makes the count a property of the canvas rather than of the file.
    """
    total = 3 * 3600.0
    beats = [i * (total / 30_000) for i in range(30_000)]

    ruler = beat_ruler(_grid(beats), total, 640.0)

    assert ruler is not None
    ticks = ruler["ticks"]
    assert isinstance(ticks, list)
    assert len(ticks) <= beat_tick_budget(640.0)
    assert len(ticks) <= ruler["tick_budget"]  # type: ignore[operator]
    drawn_x = [tick["left_pct"] * 640.0 / 100 for tick in ticks]  # type: ignore[index]
    # left_pct is rounded to 4 dp for the markup (about 3e-4 px at this width), so the spacing is checked with that slack.
    assert all(later - earlier >= BEAT_TICK_SPACING_PX - 1e-2 for earlier, later in itertools.pairwise(drawn_x))
    assert ruler["beat_count"] == 30_000


def test_a_sparse_grid_keeps_every_beat_it_has() -> None:
    """Thinning only happens where ticks would crowd: a sparse grid loses nothing."""
    beats = [10.0, 20.0, 30.0, 40.0]
    ruler = beat_ruler(_grid(beats), 60.0, 640.0)
    assert ruler is not None
    assert [tick["sec"] for tick in ruler["ticks"]] == beats  # type: ignore[index]


def test_recorded_gaps_are_spans_and_no_tick_falls_inside_one() -> None:
    """A recorded gap is its own hatched span; no beat tick is drawn inside it, and the label says so."""
    beats = [float(sec) for sec in range(0, 30)] + [float(sec) for sec in range(60, 90)]
    ruler = beat_ruler(_grid(beats, gaps=[[30.0, 60.0]]), 90.0, 900.0)

    assert ruler is not None
    gaps = ruler["gaps"]
    assert isinstance(gaps, list)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["start_sec"] == 30.0
    assert gap["end_sec"] == 60.0
    assert gap["left_pct"] == pytest.approx(100 * 30 / 90, abs=1e-3)  # rounded to 4 dp for the markup
    assert gap["width_pct"] == pytest.approx(100 * 30 / 90, abs=1e-3)
    assert "No beats recorded" in str(gap["label"])
    ticks = ruler["ticks"]
    assert isinstance(ticks, list)
    assert not any(30.0 < tick["sec"] < 60.0 for tick in ticks)  # type: ignore[index]
    assert "1 recorded gap" in str(ruler["label"])


def test_a_file_without_a_beat_grid_row_gets_no_ruler_at_all() -> None:
    """``None`` is an absence: an empty ruler would claim the file has no beats."""
    assert beat_ruler(None, 60.0, 640.0) is None


def test_the_context_carries_the_river_resolution_and_the_ruler() -> None:
    windows = [_coarse(0, 0.0, 60.0, _even(mood_party=0.9))]
    subwindows = _four_slices()
    grid = _grid([5.0, 10.0, 15.0], gaps=[])

    context = build_analysis_timeline_context(windows, subwindows=subwindows, beat_grid=grid)

    assert context["mood_river_resolution"] == "subwindow"
    assert context["beat_ruler"] is not None
    assert build_analysis_timeline_context(windows)["mood_river_resolution"] == "coarse"
    assert build_analysis_timeline_context(windows)["beat_ruler"] is None


# --- the shipped partial, rendered --------------------------------------------------------------


def _render(context: dict[str, object]) -> str:
    return _TEMPLATES.env.get_template(_PARTIAL).render(file_id="f", **context)


def test_a_rendered_subwindow_river_names_its_resolution_and_draws_one_polygon_per_run() -> None:
    windows = [_coarse(0, 0.0, 60.0, _even(mood_party=0.9))]
    html = _render(build_analysis_timeline_context(windows, subwindows=_four_slices()))

    assert "stacked as each sub-window's share" in html
    assert html.count("<polygon") == len(MOOD_NAMES)


def test_a_rendered_coarse_river_keeps_todays_label_and_no_ruler() -> None:
    windows = [_coarse(0, 0.0, 180.0, _even(mood_party=0.9))]
    html = _render(build_analysis_timeline_context(windows))

    assert "stacked as each window's share" in html
    assert "sub-window's share" not in html
    assert 'data-timeline-lane="beat"' not in html


def test_a_rendered_ruler_draws_ticks_and_gap_spans_with_a_text_alternative() -> None:
    windows = [_coarse(0, 0.0, 90.0, _even(mood_party=0.9))]
    grid = _grid([float(s) for s in range(0, 30)] + [float(s) for s in range(60, 90)], gaps=[[30.0, 60.0]])
    html = _render(build_analysis_timeline_context(windows, beat_grid=grid))

    assert 'data-timeline-lane="beat"' in html
    assert html.count("analysis-timeline-beat-gap") == 1
    assert 'aria-label="Beat ruler: 60 beats, 1 recorded gap with no beats"' in html
    assert html.count("analysis-timeline-beat-tick") <= beat_tick_budget(640.0)


# --- phaze-7hcgn.4: bounded work per render (findings 3 and 9) ------------------------------------


def test_the_resolved_river_builds_the_sub_window_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.services import analysis_timeline

    coarse = [_coarse(0, 0.0, 60.0, _even())]
    expected = subwindow_mood_river(_four_slices(), 60.0, 600.0, LANE_H)
    real = analysis_timeline.subwindow_mood_river_runs
    calls: list[int] = []

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(analysis_timeline, "subwindow_mood_river_runs", counting)
    river, resolution = resolved_mood_river(coarse, _four_slices(), 60.0, 600.0, LANE_H)
    assert (river, resolution) == (expected, "subwindow")
    assert len(calls) == 1


class _CountingBeats:
    """A sorted beat sequence that counts element reads and refuses a full walk."""

    def __init__(self, beats: list[float]) -> None:
        self._beats = beats
        self.reads = 0

    def __len__(self) -> int:
        return len(self._beats)

    def __getitem__(self, index: int) -> float:
        self.reads += 1
        return self._beats[index]

    def __iter__(self) -> object:
        raise AssertionError("the ruler walked every beat")


def test_the_beat_ruler_reads_a_bounded_number_of_beats_not_all_of_them() -> None:
    total = 12 * 3600.0
    beats = [i * 0.5 for i in range(int(total / 0.5))]  # 86,400 beats: a 12 h set at 120 BPM
    counting = _CountingBeats(beats)
    grid = _grid([])
    grid.beats = counting  # type: ignore[assignment]
    grid.beat_count = len(beats)

    ruler = beat_ruler(grid, total, 640.0)

    assert ruler is not None
    reference = beat_ruler(_grid(beats), total, 640.0)
    assert reference is not None
    assert ruler["ticks"] == reference["ticks"]
    budget = beat_tick_budget(640.0)
    assert len(ruler["ticks"]) <= budget  # type: ignore[arg-type]
    assert counting.reads <= budget * 20  # a binary search per tick (log2 86,400 < 17), never all 86,400


def test_recorded_gaps_are_bounded_by_the_canvas_and_none_is_lost_from_the_count() -> None:
    # 50,000 recorded gaps (the wire cap) on a 3 h file: drawing each would be 50,000 spans on 640 px.
    total = 3 * 3600.0
    gaps = [[i * 0.2, i * 0.2 + 0.1] for i in range(50_000)]
    ruler = beat_ruler(_grid([0.0, total - 1.0], gaps=gaps), total, 640.0)

    assert ruler is not None
    drawn = ruler["gaps"]
    assert isinstance(drawn, list)
    assert len(drawn) <= beat_tick_budget(640.0)
    assert sum(int(span["gap_count"]) for span in drawn) == 50_000  # every recorded gap is in exactly one span
    assert "50000 recorded gaps" in str(ruler["label"])
    assert all("recorded gaps" in str(span["label"]) for span in drawn if span["gap_count"] > 1)  # type: ignore[operator]


def test_gaps_far_apart_on_the_canvas_stay_separate_spans() -> None:
    ruler = beat_ruler(_grid([0.0, 10.0, 50.0, 89.0], gaps=[[10.0, 30.0], [50.0, 70.0]]), 90.0, 900.0)
    assert ruler is not None
    assert [(g["start_sec"], g["end_sec"], g["gap_count"]) for g in ruler["gaps"]] == [(10.0, 30.0, 1), (50.0, 70.0, 1)]  # type: ignore[index, union-attr]


def test_the_record_and_proposal_pages_load_the_grids_through_one_helper() -> None:
    # phaze-7hcgn.4 finding 8: the two routers had copies of the sub-window + beat-grid read; one helper now, so a
    # proposal row's river and ruler cannot drift from the record page's for the same file.
    import inspect

    from phaze.routers import proposals, record
    from phaze.services import analysis_timeline

    assert proposals.load_timeline_grids is analysis_timeline.load_timeline_grids
    assert record.load_timeline_grids is analysis_timeline.load_timeline_grids
    for module in (proposals, record):
        source = inspect.getsource(module)
        assert "select(AnalysisSubwindow)" not in source
        assert "session.get(AnalysisBeatGrid" not in source
