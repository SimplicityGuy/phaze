"""phaze-x1qr3.6: the scraped tracklist as an index into the window projection.

The per-track values are asserted against medians, modes, argmaxes and means computed BY HAND
in each test rather than by calling the production helpers back -- a test that re-runs
``statistics.median`` over the same windows would agree with any window-selection bug the
implementation happened to have, which is the only interesting thing here to get wrong.

The window sets are synthetic and small on purpose: each one is chosen so the right answer and
the plausible wrong answer differ. A segment that collected one window too many, or that
selected by overlap instead of by midpoint, produces a different number in every case below.
"""

from __future__ import annotations

from dataclasses import fields
import uuid

import pytest

from phaze.models.analysis import AnalysisWindow
from phaze.models.tracklist import TracklistTrack
from phaze.services.analysis_timeline import MOOD_HUES, MOOD_LABELS, hue_for
from phaze.services.set_projection import key_name_for_camelot
from phaze.services.track_segments import TrackSegment, build_track_segments


def _track(position: int, timestamp: str | None) -> TracklistTrack:
    return TracklistTrack(version_id=uuid.uuid4(), position=position, timestamp=timestamp, title=f"Track {position}")


def _fine(index: int, start: float, end: float, *, bpm: float | None = None, camelot: str | None = None) -> AnalysisWindow:
    return AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=index, start_sec=start, end_sec=end, bpm=bpm, camelot=camelot)


def _coarse(index: int, start: float, end: float, *, energy: float | None = None, moods: dict[str, float] | None = None) -> AnalysisWindow:
    return AnalysisWindow(file_id=uuid.uuid4(), tier="coarse", window_index=index, start_sec=start, end_sec=end, energy=energy, mood_scores=moods)


def test_eighteen_timestamped_tracks_give_eighteen_segments_ending_at_the_duration() -> None:
    """Consecutive timestamps become consecutive half-open ranges; the last closes at duration."""
    tracks = [_track(position, f"{position * 5}:00") for position in range(1, 19)]

    segments = build_track_segments(tracks, [], 7200.0)

    assert len(segments) == 18
    assert [segment.position for segment in segments] == list(range(1, 19))
    assert segments[0].start_sec == 300.0
    # Every segment ends exactly where the next begins -- no overlap, no hole between tracks.
    assert all(segments[index].end_sec == segments[index + 1].start_sec for index in range(17))
    assert segments[-1].start_sec == 5400.0
    assert segments[-1].end_sec == 7200.0


def test_a_track_without_a_timestamp_yields_no_segment_and_the_next_one_absorbs_its_range() -> None:
    """Rule 1: an untimed track is never given a neighbour's range, so it has no segment at all.

    The range from 5:00 to 15:00 belongs to track 1, because track 2 never told us when it
    started. Track 2 gets em dashes rather than measurements taken from track 1's audio.
    """
    tracks = [_track(1, "5:00"), _track(2, None), _track(3, "15:00")]

    segments = build_track_segments(tracks, [], 1800.0)

    assert [segment.position for segment in segments] == [1, 3]
    assert segments[0].start_sec == 300.0
    assert segments[0].end_sec == 900.0  # track 3's start, not track 2's absent one
    assert not any(segment.position == 2 for segment in segments)


def test_an_unparseable_timestamp_is_the_same_as_an_absent_one_and_never_raises() -> None:
    """parse_timestamp_string is total (phaze-97u7): decorated and empty cells are absences."""
    tracks = [_track(1, "0:00"), _track(2, ""), _track(3, "~12:00"), _track(4, "[1:02:03]"), _track(5, "600")]

    segments = build_track_segments(tracks, [], 3600.0)

    assert [segment.position for segment in segments] == [1, 5]
    assert segments[1].start_sec == 600.0


def test_a_file_with_no_tracklist_yields_an_empty_list() -> None:
    assert build_track_segments([], [_fine(0, 0.0, 30.0, bpm=128.0)], 3600.0) == []
    assert build_track_segments([_track(1, None)], [_fine(0, 0.0, 30.0, bpm=128.0)], 3600.0) == []


def test_the_last_segment_stays_open_ended_when_the_duration_is_unknown_or_too_early() -> None:
    """No duration means the last track's extent is genuinely unknown -- not "the rest of the file".

    An open-ended segment intersects nothing, so its measurement cells read as em dashes. That
    is the honest answer; claiming the remainder would attach whatever came after to a track we
    cannot bound.
    """
    tracks = [_track(1, "0:00"), _track(2, "10:00")]
    windows = [_fine(0, 600.0, 630.0, bpm=140.0)]

    unknown = build_track_segments(tracks, windows, None)
    too_early = build_track_segments(tracks, windows, 60.0)

    assert unknown[-1].end_sec is None
    assert unknown[-1].bpm is None
    assert too_early[-1].end_sec is None


def test_non_monotonic_scraped_timestamps_clamp_to_a_zero_length_segment() -> None:
    """A backwards pair is a scrape defect; it must not become a negative-length range."""
    segments = build_track_segments([_track(1, "10:00"), _track(2, "5:00")], [], 3600.0)

    assert segments[0].start_sec == 600.0
    assert segments[0].end_sec == 600.0
    assert segments[0].bpm is None


def test_per_track_values_are_the_median_bpm_modal_key_argmax_mood_and_mean_energy() -> None:
    """One synthetic window set, all four expected values worked out by hand in this docstring.

    Track 1 runs [0, 600) and track 2 runs [600, 1200).

    FINE windows (30 s each, midpoints at start+15):
      t1: 120, 124, 132, 300  -> median of four is (124 + 132) / 2 = 128.0. The 300 is a
          double-time misdetection and is exactly why this is a median: the mean would be 169.
          Keys: 8A, 8A, 5A, 8A -> modal 8A ("A minor"), which is NOT the last or the first.
      t2: 140, 150            -> median 145.0. Keys 12B, 12B -> 12B ("E major").

    COARSE windows (300 s each, midpoints at start+150):
      t1: energy 0.4 and 0.6  -> mean 0.5. Moods: party {0.8, 0.2} mean 0.5; sad {0.1, 0.9}
          mean 0.5; happy {0.55, 0.55} mean 0.55 -> argmax is HAPPY, which is neither window's
          own strongest mood. A per-window argmax then a vote would answer party or sad.
      t2: energy 0.9          -> mean 0.9. Moods: party 0.7 -> argmax party.
    """
    tracks = [_track(1, "0:00"), _track(2, "10:00")]
    fine = [
        _fine(0, 0.0, 30.0, bpm=120.0, camelot="8A"),
        _fine(1, 30.0, 60.0, bpm=124.0, camelot="8A"),
        _fine(2, 60.0, 90.0, bpm=132.0, camelot="5A"),
        _fine(3, 90.0, 120.0, bpm=300.0, camelot="8A"),
        _fine(4, 600.0, 630.0, bpm=140.0, camelot="12B"),
        _fine(5, 630.0, 660.0, bpm=150.0, camelot="12B"),
    ]
    coarse = [
        _coarse(0, 0.0, 300.0, energy=0.4, moods={"mood_party": 0.8, "mood_sad": 0.1, "mood_happy": 0.55}),
        _coarse(1, 300.0, 600.0, energy=0.6, moods={"mood_party": 0.2, "mood_sad": 0.9, "mood_happy": 0.55}),
        _coarse(2, 600.0, 900.0, energy=0.9, moods={"mood_party": 0.7, "mood_sad": 0.1, "mood_happy": 0.2}),
    ]

    first, second = build_track_segments(tracks, [*fine, *coarse], 1200.0)

    assert first.bpm == 128.0
    assert first.camelot == "8A"
    assert first.key == "A minor"
    assert first.key_hue == hue_for("A minor")
    assert first.mood == "mood_happy"
    assert first.mood_label == MOOD_LABELS["mood_happy"]
    assert first.mood_hue == MOOD_HUES["mood_happy"]
    assert first.energy == 0.5

    assert second.bpm == 145.0
    assert second.camelot == "12B"
    assert second.key == "E major"
    assert second.mood == "mood_party"
    assert second.energy == 0.9


def test_a_window_belongs_to_the_track_containing_its_MIDPOINT_not_to_both_it_overlaps() -> None:
    """Midpoint containment, not overlap: one window counts toward exactly one track.

    The 300 s coarse window spans [150, 450) and straddles the 300 s boundary. Its midpoint is
    300.0, which is track 2's first instant, so it belongs to track 2 alone. Under overlap it
    would land in both and each track's mean energy would include it.
    """
    tracks = [_track(1, "0:00"), _track(2, "5:00")]
    straddling = _coarse(0, 150.0, 450.0, energy=0.8, moods={"mood_party": 0.9})

    first, second = build_track_segments(tracks, [straddling], 900.0)

    assert first.energy is None
    assert second.energy == 0.8


def test_a_segment_intersecting_no_window_reports_absence_never_a_zero_or_a_neighbour() -> None:
    """Rule 3: nothing measured means None, not 0.0 and not the adjacent track's value."""
    tracks = [_track(1, "0:00"), _track(2, "10:00")]
    windows = [_fine(0, 0.0, 30.0, bpm=128.0, camelot="8A"), _coarse(0, 0.0, 300.0, energy=0.7, moods={"mood_sad": 0.9})]

    _, second = build_track_segments(tracks, windows, 1200.0)

    assert (second.bpm, second.camelot, second.key, second.mood, second.energy) == (None, None, None, None, None)
    assert second.mood_hue is None


def test_a_coarse_window_with_no_mood_scores_contributes_nothing_rather_than_zeroes() -> None:
    tracks = [_track(1, "0:00")]
    windows = [
        _coarse(0, 0.0, 300.0, energy=0.5, moods=None),
        _coarse(1, 300.0, 600.0, energy=0.5, moods=dict.fromkeys(MOOD_HUES, None)),  # type: ignore[arg-type]
        _coarse(2, 600.0, 900.0, energy=0.5, moods={"mood_relaxed": 0.3}),
    ]

    segment = build_track_segments(tracks, windows, 1200.0)[0]

    assert segment.mood == "mood_relaxed"
    assert segment.energy == 0.5


# Read off the segment by the render path (`_tracklist_review_body.html`, `services/poster.py`)
# and by `phaze-x1qr3.10`'s readout, so they are part of the exposed shape whether they are
# stored or derived.
_DERIVED_READINGS = ("key", "key_hue", "mood_label", "mood_hue")


def test_the_derived_readings_cannot_contradict_the_measurements_they_come_from() -> None:
    """A segment's key name, key hue, mood label and mood hue are FUNCTIONS of `camelot`/`mood`.

    As stored fields they were set by the one constructor call in `build_track_segments` and
    then carried, which made `TrackSegment(camelot="8A", key="F# minor", ...)` a value the type
    checker, the constructor and every renderer accept -- a key name contradicting the code
    printed beside it. This asserts the two halves agree by CONSTRUCTION: setting them is not
    possible, and reading them re-derives from the measurement.
    """
    segment = build_track_segments([_track(1, "0:00")], [_fine(0, 0.0, 30.0, bpm=128.0, camelot="8A")], 600.0)[0]

    assert segment.camelot == "8A"
    assert segment.key == key_name_for_camelot("8A")
    assert segment.key_hue == hue_for(key_name_for_camelot("8A") or "8A")
    with pytest.raises((AttributeError, TypeError)):
        TrackSegment(position=1, start_sec=0.0, end_sec=1.0, bpm=None, camelot="8A", mood=None, energy=None, key="F# minor")  # type: ignore[call-arg]

    moody = build_track_segments(
        [_track(1, "0:00")],
        [_coarse(0, 0.0, 180.0, moods={"mood_happy": 0.9})],
        600.0,
    )[0]
    assert moody.mood == "mood_happy"
    assert moody.mood_label == MOOD_LABELS["mood_happy"]
    assert moody.mood_hue == MOOD_HUES["mood_happy"]


def test_the_segment_shape_is_what_the_inspection_and_poster_beads_will_read() -> None:
    """The record context's ``track_segments`` entries carry exactly these fields, in this order.

    Pinned because two later beads in this epic consume the list; a silently renamed or dropped
    field would leave their columns empty rather than erroring.
    """
    assert [field.name for field in fields(TrackSegment)] == [
        "position",
        "start_sec",
        "end_sec",
        "bpm",
        "camelot",
        "mood",
        "energy",
        # phaze-x1qr3.10 appended `title`, defaulted, so the timeline readout can say "track 7
        # <title>" without re-joining the tracklist table client-side. Appended and defaulted on
        # purpose: every existing construction of this frozen dataclass keeps working.
        "title",
    ]
    # `key`, `key_hue`, `mood_label` and `mood_hue` LEFT this list without leaving the segment:
    # each is a pure function of `camelot` or `mood`, so they are properties now (PR #556 review,
    # finding 8) and an inconsistent segment is no longer representable. The consumers read the
    # same attribute names, which is what the pin above is really protecting, so both halves are
    # asserted together.
    assert [name for name in _DERIVED_READINGS if not isinstance(getattr(TrackSegment, name, None), property)] == []

    segment = build_track_segments([_track(1, "0:00")], [_fine(0, 0.0, 30.0, bpm=128.0, camelot="8A")], 600.0)[0]
    assert isinstance(segment, TrackSegment)
    assert segment.position == 1
    assert segment.start_sec == 0.0
    assert segment.end_sec == 600.0
    assert segment.title == _track(1, "0:00").title
    with pytest.raises(AttributeError):
        segment.position = 2  # type: ignore[misc]


def test_the_mood_dot_reads_the_same_palette_as_the_river_and_never_its_own() -> None:
    """The tracklist's third consumer of MOOD_HUES: the hue arrives ON the segment."""
    tracks = [_track(1, "0:00")]
    windows = [_coarse(0, 0.0, 300.0, moods={"mood_electronic": 0.9})]

    segment = build_track_segments(tracks, windows, 600.0)[0]

    assert segment.mood_hue == MOOD_HUES["mood_electronic"]
    assert segment.mood_label == "Electronic"
