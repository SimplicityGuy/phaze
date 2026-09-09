"""Exhaustive, deterministic analysis-timeline presentation data."""

from __future__ import annotations

import math
import uuid

from phaze.models.analysis import AnalysisWindow
from phaze.models.set_profile import SetProfile
from phaze.services.analysis_timeline import (
    bpm_segments,
    bpm_spark,
    build_analysis_timeline_context,
    elapsed_time_ticks,
    format_elapsed_time,
    inspection_windows,
    resolve_inspection,
    rounded_bpm_bounds,
)


def _window(
    index: int,
    start: float,
    end: float,
    *,
    tier: str = "fine",
    bpm: float | None = None,
    key: str | None = None,
    mood: str | None = None,
    style: str | None = None,
    energy: float | None = None,
) -> AnalysisWindow:
    return AnalysisWindow(
        file_id=uuid.uuid4(),
        tier=tier,
        window_index=index,
        start_sec=start,
        end_sec=end,
        bpm=bpm,
        musical_key=key,
        mood=mood,
        style=style,
        energy=energy,
    )


def test_bpm_bounds_round_outward_and_drive_geometry() -> None:
    windows = [_window(0, 0.0, 30.0, bpm=76.0), _window(1, 30.0, 60.0, bpm=153.0)]

    spark = bpm_spark(windows, 60.0, 1000.0, 120.0)

    assert (spark.lo, spark.hi) == (70.0, 160.0)
    assert spark.points == "250.00,112.00 750.00,9.33"


def test_bpm_bounds_handle_decimals_exact_multiples_constants_and_invalid_values() -> None:
    assert rounded_bpm_bounds([76.1, 153.9]) == (70.0, 160.0)
    assert rounded_bpm_bounds([120.0, 130.0]) == (120.0, 130.0)
    assert rounded_bpm_bounds([120.0]) == (110.0, 130.0)
    assert rounded_bpm_bounds([123.4]) == (120.0, 130.0)
    assert rounded_bpm_bounds([-1.0, 0.0, math.nan, math.inf]) is None


def test_elapsed_ticks_span_zero_to_real_duration_and_format_long_files() -> None:
    ticks = elapsed_time_ticks(9_001.25)

    assert ticks[0] == {"sec": 0.0, "left_pct": 0.0, "label": "0:00"}
    assert ticks[-1]["sec"] == 9_001.25
    assert ticks[-1]["left_pct"] == 100.0
    assert ticks[-1]["label"] == "2:30:01"
    assert format_elapsed_time(360_061.0) == "100:01:01"


def test_inspection_payload_preserves_every_window_and_explicit_absences() -> None:
    windows = [
        _window(0, 0.0, 30.0, bpm=128.0, key="Am"),
        _window(1, 30.0, 60.0, bpm=None, key=None),
        _window(0, 0.0, 60.0, tier="coarse", mood="focused", style=None),
    ]

    payload = inspection_windows(windows)

    assert len(payload) == len(windows)
    assert payload[0] == {
        "tier": "fine",
        "index": 0,
        "start": 0.0,
        "end": 30.0,
        "bpm": 128.0,
        "key": "Am",
        # phaze-x1qr3.10 added these three. An unprojected window carries them as explicit
        # absences, never as a zero energy or an empty-string key -- the client renders an em
        # dash for `None` and would render "0.0" for a manufactured zero.
        "camelot": None,
        "energy": None,
        "mood": None,
        "mood_top": None,
        "style": None,
    }
    assert payload[1]["bpm"] is None
    assert payload[2]["style"] is None


def test_context_keeps_gaps_and_all_windows_for_long_files() -> None:
    windows = [
        _window(0, 0.0, 30.0, bpm=128.0, key="Am"),
        _window(1, 90.0, 120.0, bpm=130.0, key="C"),
        _window(0, 0.0, 120.0, tier="coarse", mood="focused", style="electronic"),
        _window(1, 120.0, 172_801.0, tier="coarse", mood=None, style=None),
    ]

    context = build_analysis_timeline_context(windows)

    assert context["total_sec"] == 172_801.0
    assert len(context["timeline_inspection"]["windows"]) == len(windows)  # type: ignore[index]
    key_ribbons = context["key_ribbons"]
    assert isinstance(key_ribbons, list)
    assert key_ribbons[1]["left_pct"] > key_ribbons[0]["width_pct"]


def test_inspection_resolves_tiers_and_leaves_gaps_absent() -> None:
    windows = [
        _window(0, 0.0, 30.0, bpm=128.0, key="Am"),
        _window(1, 90.0, 120.0, bpm=130.0, key="C"),
        _window(0, 0.0, 120.0, tier="coarse", mood="focused", style=None),
    ]

    measured = resolve_inspection(windows, 15.0, 120.0)
    gap = resolve_inspection(windows, 60.0, 120.0)
    final = resolve_inspection(windows, 120.0, 120.0)

    assert measured["fine"]["bpm"] == 128.0  # type: ignore[index]
    assert measured["coarse"]["mood"] == "focused"  # type: ignore[index]
    assert gap["fine"] is None
    assert gap["coarse"] is not None
    assert final["fine"]["key"] == "C"  # type: ignore[index]


def test_bpm_segments_do_not_draw_across_unmeasured_gaps() -> None:
    windows = [
        _window(0, 0.0, 30.0, bpm=120.0),
        _window(1, 30.0, 60.0, bpm=125.0),
        _window(2, 60.0, 90.0, bpm=None),
        _window(3, 120.0, 150.0, bpm=130.0),
    ]

    segments = bpm_segments(windows, 150.0, 600.0, 120.0, 120.0, 130.0)

    assert len(segments) == 2
    assert segments[0]["points"] == "60.00,120.00 180.00,60.00"
    assert segments[1]["single_x"] == 540.0


def test_canvas_width_grows_without_capping_or_dropping_windows() -> None:
    windows = [_window(index, index * 30.0, (index + 1) * 30.0, bpm=120.0 + index) for index in range(24)]

    context = build_analysis_timeline_context(windows)

    assert context["timeline_w"] == 1_344.0
    assert len(context["timeline_inspection"]["windows"]) == 24  # type: ignore[index]


# phaze-0zx26: `set_profile.peak_sec` and the live `energy_peak(...)["sec"]` used to be TWO
# DIFFERENT DEFINITIONS of the same word -- the stored value argmaxes the 64-point resampled
# `arc` while the live one argmaxes the raw coarse windows, which can land on a different window
# for a short spike. These pin the rest position (`timeline_inspection["peak_sec"]`) to the
# stored figure whenever one exists, and to the live computation only as the documented fallback
# for a file with no profile row yet.
def test_the_rest_position_reads_the_stored_peak_even_when_it_disagrees_with_the_live_argmax() -> None:
    # The live raw-window argmax sits at window 0's midpoint (30.0 s) -- the highest single
    # measured energy. A stored peak elsewhere (90.0 s) stands in for the resampled arc landing
    # on a different window than the raw argmax did.
    windows = [
        _window(0, 0.0, 60.0, tier="coarse", energy=0.9),
        _window(1, 60.0, 120.0, tier="coarse", energy=0.4),
    ]
    set_profile = SetProfile(file_id=uuid.uuid4(), peak_sec=90.0)

    context = build_analysis_timeline_context(windows, set_profile=set_profile)

    assert context["timeline_inspection"]["peak_sec"] == 90.0  # type: ignore[index]
    # The visual "Peak X at Y" mark is a different, complementary feature (an actual measured
    # window's own energy reading) and is deliberately UNCHANGED by this bead -- it still
    # anchors to the live raw-window argmax.
    assert context["energy_peak"]["sec"] == 30.0  # type: ignore[index]


def test_the_rest_position_falls_back_to_the_live_argmax_with_no_set_profile_row() -> None:
    windows = [
        _window(0, 0.0, 60.0, tier="coarse", energy=0.9),
        _window(1, 60.0, 120.0, tier="coarse", energy=0.4),
    ]

    context = build_analysis_timeline_context(windows, set_profile=None)

    assert context["timeline_inspection"]["peak_sec"] == 30.0  # type: ignore[index]


def test_the_rest_position_falls_back_to_the_live_argmax_when_the_stored_peak_is_null() -> None:
    # A profile row can exist with `peak_sec is None` -- no usable coarse energy at projection
    # time -- distinct from no row at all; both fall back identically.
    windows = [
        _window(0, 0.0, 60.0, tier="coarse", energy=0.9),
        _window(1, 60.0, 120.0, tier="coarse", energy=0.4),
    ]
    set_profile = SetProfile(file_id=uuid.uuid4(), peak_sec=None)

    context = build_analysis_timeline_context(windows, set_profile=set_profile)

    assert context["timeline_inspection"]["peak_sec"] == 30.0  # type: ignore[index]
