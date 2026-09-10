"""The indexed cross-tier joins return EXACTLY what the scans they replaced returned.

PR #556 review, finding 6: three joins scanned every window per question --
``set_projection._glyph_cells`` and ``set_projection_writer._bpm_z_for_range`` over the fine
windows per coarse window, ``track_segments.build_track_segments`` over every window per track.
On a 12-hour set that is 1,440 fine and 240 coarse windows, so 345,600 predicate evaluations to
answer 240 questions whose answers involve about six windows each.

WHY THE ORACLE IS TRANSCRIBED RATHER THAN CITED. The scans are gone, so "the new output equals
the old" needs the old algorithm to still exist somewhere; these tests carry it, written the way
it stood, and assert EQUALITY -- not closeness. Every one of these values is persisted
(``analysis_window.energy``, ``set_profile.glyph``) or rendered as a number an operator reads, so
"within a tolerance" would be a weaker claim than the change actually makes: the members are the
same members in the same order, and ``statistics.fmean`` sums with ``math.fsum``, which is
exactly rounded.

The 12-hour fixture is the point of the sizes. A three-window fixture agrees under any
implementation of anything; the failure a bisect can have is an off-by-one at a boundary, and
1,440 windows against 240 windows is 240 chances to find one.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Any
import uuid

from phaze.models.analysis import AnalysisWindow
from phaze.services.set_projection import OverlapIndex, _glyph_cells, camelot_number, modal_camelot
from phaze.services.set_projection_writer import _bpm_stats, _bpm_z_for_range, _extract, _fine_bpm_ranges
from phaze.services.track_segments import build_track_segments


if TYPE_CHECKING:
    from collections.abc import Sequence


# A 12-hour set at the deployed tier steps: 30 s fine, 180 s coarse. These are the numbers the
# analysis pipeline actually produces for a full festival set, not a scaled-down stand-in.
FINE_SEC = 30.0
COARSE_SEC = 180.0
DURATION_SEC = 12 * 3600.0
FINE_COUNT = int(DURATION_SEC / FINE_SEC)
COARSE_COUNT = int(DURATION_SEC / COARSE_SEC)

_CODES = ("8A", "8A", "9A", "3A", "9A", "10A", "4B", "8A")


def _fine_windows() -> list[AnalysisWindow]:
    """1,440 contiguous fine windows, cycling through eight Camelot codes with a few gaps.

    Every 37th window carries no ``camelot`` and no ``bpm`` -- a window analysed but not
    projected, which both joins must skip rather than count. A fixture where every window is
    usable cannot tell a correct filter from a missing one.
    """
    windows = []
    for index in range(FINE_COUNT):
        blank = index % 37 == 0
        windows.append(
            AnalysisWindow(
                file_id=uuid.uuid4(),
                tier="fine",
                window_index=index,
                start_sec=index * FINE_SEC,
                end_sec=(index + 1) * FINE_SEC,
                bpm=None if blank else 118.0 + (index % 23),
                camelot=None if blank else _CODES[index % len(_CODES)],
            )
        )
    return windows


def _coarse_windows() -> list[AnalysisWindow]:
    return [
        AnalysisWindow(
            file_id=uuid.uuid4(),
            tier="coarse",
            window_index=index,
            start_sec=index * COARSE_SEC,
            end_sec=(index + 1) * COARSE_SEC,
            energy=round((index % 17) / 17.0, 4),
            mood_scores={"mood_happy": (index % 11) / 11.0, "mood_sad": (index % 7) / 7.0},
        )
        for index in range(COARSE_COUNT)
    ]


FINE = _fine_windows()
COARSE = _coarse_windows()


# --- The oracles: the scans exactly as they stood before the index --------------------------


def _scan_glyph_cells(coarse: Sequence[AnalysisWindow], fine: Sequence[AnalysisWindow]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for window in sorted(coarse, key=lambda w: (w.window_index, w.start_sec)):
        overlapping = [f for f in fine if f.camelot and f.start_sec < window.end_sec and f.end_sec > window.start_sec]
        cells.append({"camelot_number": camelot_number(modal_camelot(overlapping)), "energy": window.energy})
    return cells


def _scan_bpm_z(fine_ranges: Sequence[tuple[float, float, float]], start_sec: float, end_sec: float, stats: tuple[float, float] | None) -> float:
    if stats is None:
        return 0.0
    mean, stdev = stats
    overlapping = [bpm for f_start, f_end, bpm in fine_ranges if f_start < end_sec and f_end > start_sec]
    if not overlapping:
        return 0.0
    return (statistics.fmean(overlapping) - mean) / stdev


# --- The equivalences ------------------------------------------------------------------------


def test_the_glyph_cells_of_a_twelve_hour_set_are_identical_to_the_scan() -> None:
    """240 cells, each a cross-tier modal over its own ~6 overlapping fine windows."""
    indexed = _glyph_cells(COARSE, FINE)

    assert indexed == _scan_glyph_cells(COARSE, FINE)
    assert indexed is not None
    assert len(indexed) == COARSE_COUNT
    # A fixture whose cells were all `None` would make the comparison above vacuous.
    assert sum(1 for cell in indexed if cell["camelot_number"] is not None) == COARSE_COUNT


def test_every_coarse_windows_bpm_z_is_identical_to_the_scan() -> None:
    """Compared per window rather than in aggregate: a boundary error at one coarse window is
    what this is looking for, and a mean over 240 of them would hide it."""
    facts = [_extract(window) for window in [*FINE, *COARSE]]
    ranges = _fine_bpm_ranges(facts)
    stats = _bpm_stats([bpm for _start, _end, bpm in ranges])
    assert stats is not None, "the fixture must have a usable reference distribution"
    index = OverlapIndex(ranges)

    indexed = [_bpm_z_for_range(index, window.start_sec, window.end_sec, stats) for window in COARSE]

    assert indexed == [_scan_bpm_z(ranges, window.start_sec, window.end_sec, stats) for window in COARSE]
    assert len({round(value, 9) for value in indexed}) > 1, "a fixture with one z-score everywhere would not exercise the join"


def test_the_indexed_join_agrees_with_the_scan_on_every_boundary_case() -> None:
    """Coarse windows placed to land exactly ON a fine boundary, inside one, and past the end.

    Half-open on both sides: a fine window ENDING exactly where a coarse window starts does not
    overlap it, and one STARTING exactly where it ends does not either. Those are the two cases a
    `bisect_left`/`bisect_right` mix-up gets wrong while every interior window stays right.
    """
    ranges = [(0.0, 30.0, 120.0), (30.0, 60.0, 130.0), (60.0, 90.0, 140.0)]
    index = OverlapIndex(ranges)
    stats = (125.0, 5.0)

    for start, end in [(0.0, 30.0), (30.0, 60.0), (0.0, 90.0), (29.999, 30.001), (90.0, 180.0), (-10.0, 0.0), (15.0, 45.0)]:
        assert _bpm_z_for_range(index, start, end, stats) == _scan_bpm_z(ranges, start, end, stats), f"[{start}, {end})"


def test_the_index_falls_back_to_a_scan_when_the_ends_are_not_sorted() -> None:
    """A long range nested among short ones breaks the "ends ascend with starts" property.

    The bisect on ends is exact only while they are non-decreasing. Nothing guarantees that --
    windows are fixed-width in practice, not by construction -- so the index checks and falls
    back. This is the branch that would otherwise silently return a SHORT slice: the long range
    starts early and ends late, and a bisect on unsorted ends would cut it out of the middle.
    """
    ranges = [(0.0, 600.0, 100.0), (10.0, 40.0, 120.0), (40.0, 70.0, 140.0)]
    index = OverlapIndex(ranges)
    stats = (120.0, 10.0)

    for start, end in [(0.0, 20.0), (20.0, 50.0), (500.0, 700.0), (300.0, 400.0)]:
        assert _bpm_z_for_range(index, start, end, stats) == _scan_bpm_z(ranges, start, end, stats), f"[{start}, {end})"
    # The nested long range is the whole point: a slice-based answer would drop it here.
    assert _bpm_z_for_range(index, 300.0, 400.0, stats) == (100.0 - 120.0) / 10.0


def test_the_index_returns_members_in_the_callers_own_order() -> None:
    """Not in start order. ``modal_camelot`` accumulates duration weights with float addition and
    ``_argmax_mood`` sums mood scores the same way; neither is associative, so a re-ordered
    result would move the last bits of a stored value with nothing to explain the change."""
    index = OverlapIndex([(60.0, 90.0, "third"), (0.0, 30.0, "first"), (30.0, 60.0, "second")])

    assert index.overlapping(0.0, 90.0) == ["third", "first", "second"]


def test_the_track_segment_join_of_a_twelve_hour_set_is_identical_to_the_scan() -> None:
    """40 tracks over 1,680 windows: the third scan finding 6 named, and the one whose result is
    read straight onto the screen rather than into a column."""
    from types import SimpleNamespace

    tracks = [
        SimpleNamespace(position=index + 1, timestamp=f"{(index * 18) // 60}:{(index * 18) % 60:02d}:00", title=f"Track {index + 1}")
        for index in range(40)
    ]
    windows = [*FINE, *COARSE]

    segments = build_track_segments(tracks, windows, DURATION_SEC)

    assert len(segments) == 40
    for segment in segments:
        end = segment.end_sec
        in_fine = [w for w in windows if w.tier == "fine" and end is not None and segment.start_sec <= (w.start_sec + w.end_sec) / 2.0 < end]
        in_coarse = [w for w in windows if w.tier == "coarse" and end is not None and segment.start_sec <= (w.start_sec + w.end_sec) / 2.0 < end]
        assert segment.camelot == modal_camelot(in_fine)
        assert segment.energy == (round(sum(float(w.energy) for w in in_coarse) / len(in_coarse), 3) if in_coarse else None)
    # A run where every segment came back empty would satisfy the loop above without joining.
    assert sum(1 for segment in segments if segment.camelot is not None) >= 30
