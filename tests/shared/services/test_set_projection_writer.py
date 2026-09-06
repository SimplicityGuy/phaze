"""Unit-level tests for ``services/set_projection_writer.py`` (phaze-x1qr3.3) -- the I/O layer's
PURE half (``compute_window_projection`` and its helpers), exercised directly on plain dict rows
with no database. DB-backed round trips (the router path, the backfill path) live in
``tests/shared/tasks/test_analysis_persist_projection.py`` and
``tests/shared/cli/test_backfill_projection.py``; this module targets the BPM-z-score edge cases
those corpus-level tests do not naturally hit -- zero-variance fine BPMs, a coarse window with no
overlapping fine window, and ``annotate_window_rows``' atomic-on-failure contract.
"""

from __future__ import annotations

import pytest

from phaze.services.set_projection_writer import (
    _bpm_stats,
    _bpm_z_for_range,
    annotate_window_orm_objects,
    annotate_window_rows,
    compute_window_projection,
)


def test_bpm_stats_is_none_below_two_values() -> None:
    assert _bpm_stats([]) is None
    assert _bpm_stats([120.0]) is None


def test_bpm_stats_is_none_when_every_value_is_identical() -> None:
    """Zero variance -- a z-score against a zero-width distribution is undefined, not zero."""
    assert _bpm_stats([120.0, 120.0, 120.0]) is None


def test_bpm_stats_reports_mean_and_stdev_for_a_real_spread() -> None:
    stats = _bpm_stats([100.0, 120.0, 140.0])
    assert stats is not None
    mean, stdev = stats
    assert mean == 120.0
    assert stdev > 0.0


def test_bpm_z_for_range_is_zero_with_no_stats() -> None:
    assert _bpm_z_for_range([(0.0, 30.0, 120.0)], 0.0, 180.0, None) == 0.0


def test_bpm_z_for_range_is_zero_when_no_fine_window_overlaps() -> None:
    """Stats exist (the file has usable fine BPM elsewhere), but none of the fine windows fall
    inside THIS coarse window's time range -- z reads 0.0 rather than reaching outside the range."""
    stats = (120.0, 10.0)
    fine_ranges = [(0.0, 30.0, 100.0), (30.0, 60.0, 140.0)]
    assert _bpm_z_for_range(fine_ranges, 500.0, 680.0, stats) == 0.0


def test_bpm_z_for_range_scores_the_overlapping_windows_average() -> None:
    stats = (120.0, 10.0)
    fine_ranges = [(0.0, 30.0, 130.0), (30.0, 60.0, 130.0)]
    assert _bpm_z_for_range(fine_ranges, 0.0, 60.0, stats) == 1.0


def test_compute_window_projection_gives_a_fully_none_entry_for_an_unrecognised_tier() -> None:
    """A window whose ``tier`` is neither ``fine`` nor ``coarse`` (a malformed row this module has
    never been asked to handle) gets the same all-``None`` gap as any other "not yet knowable"
    field, never an exception -- ``compute_window_projection`` must stay total over its input."""
    windows = [{"tier": "unknown", "start_sec": 0.0, "end_sec": 30.0, "bpm": None, "musical_key": None, "features": None}]

    result = compute_window_projection(windows)

    assert result == [{"camelot": None, "energy": None, "mood_scores": None}]


def test_annotate_window_rows_leaves_rows_untouched_when_computation_raises() -> None:
    """A coarse row whose ``features`` is not a mapping (e.g. a raw string) makes
    ``positive_class_vector`` raise; ``annotate_window_rows`` must not partially merge onto the
    rows it was given -- the atomicity ``annotate_window_rows``' own docstring promises."""
    rows: list[dict[str, object]] = [
        {"tier": "fine", "start_sec": 0.0, "end_sec": 30.0, "bpm": 120.0, "musical_key": "A minor", "features": None},
        {"tier": "coarse", "start_sec": 30.0, "end_sec": 210.0, "bpm": None, "musical_key": None, "features": "not-a-mapping"},
    ]

    with pytest.raises(AttributeError):
        annotate_window_rows(rows)

    assert "camelot" not in rows[0]
    assert "energy" not in rows[1]
    assert "mood_scores" not in rows[1]


def test_annotate_window_orm_objects_sets_the_three_attributes_from_dict_like_rows() -> None:
    """``annotate_window_orm_objects`` reads via ``getattr`` (not ``.get``), so a lightweight
    stand-in object -- not a real ``AnalysisWindow`` -- exercises the non-Mapping branch of
    ``_extract`` directly, mirroring what the real ORM instances the backfill loads look like."""

    class _FakeWindow:
        def __init__(self, **kwargs: object) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    fine = _FakeWindow(tier="fine", start_sec=0.0, end_sec=30.0, bpm=120.0, musical_key="A minor", features=None)
    windows = [fine]

    annotate_window_orm_objects(windows)  # type: ignore[arg-type]

    assert fine.camelot == "8A"
    assert fine.energy is None
    assert fine.mood_scores is None
