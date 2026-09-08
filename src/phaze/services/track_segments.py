"""The scraped tracklist as an INDEX into the window projection (``phaze-x1qr3.6``).

A 1001Tracklists tracklist gives a set structure the audio alone does not: named tracks with
cue times. The windows give measurement the tracklist does not: BPM, key, mood, energy every
30 s and every 180 s. This module is the join -- consecutive scraped timestamps become half-open
time segments, and each segment collects the windows whose MIDPOINT falls inside it, so a
tracklist row can answer "what does track 7 actually sound like" from stored data.

Three rules the shape here exists to keep:

1. **A track without a parseable timestamp produces no segment.** It is not given the previous
   track's range, and it is not given a zero-length one at position zero -- either would attach
   measurements from audio that is not that track. It renders as em dashes, which is the honest
   reading of "we do not know when this track starts".
2. **Midpoint containment, not overlap.** A 180 s coarse window straddling a track boundary
   belongs to exactly one of the two tracks -- whichever contains its centre. Overlap would let
   one window's mood count toward two tracks and make the per-track means sum to more than the
   file.
3. **No manufactured values.** A segment that intersects no window of the right tier gets
   ``None`` for that metric, never a zero, a mean of nothing, or the file's own average.

I/O-free, like ``set_projection``: it takes rows and returns dataclasses, so the record page,
the two other surfaces that re-render the tracklist fragment, and the tests all share one
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import TYPE_CHECKING, Any

from phaze.services.analysis_timeline import MOOD_HUES, MOOD_LABELS, MOOD_NAMES, hue_for
from phaze.services.cue_generator import parse_timestamp_string
from phaze.services.set_projection import CAMELOT_TABLE, modal_camelot


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.models.analysis import AnalysisWindow
    from phaze.models.tracklist import TracklistTrack


# ``CAMELOT_TABLE`` is a bijection (24 distinct keys onto 24 distinct wheel positions), so it
# inverts without loss. Inverting it rather than reading the window's own ``musical_key`` is
# deliberate: the modal is computed over the stored ``camelot`` column, and essentia's flat
# spellings normalise onto one canonical key string on the way in, so the name shown beside a
# code is always that code's own canonical name and never one of its enharmonic twins.
_CAMELOT_TO_KEY: dict[str, str] = {code: key for key, code in CAMELOT_TABLE.items()}


@dataclass(frozen=True)
class TrackSegment:
    """One scraped track's time range, and what the windows inside it measured.

    ``end_sec`` is ``None`` only for the LAST track of a file whose duration is unknown -- there
    is no later timestamp to bound it and no duration to close it, so its extent is genuinely
    unknown and it collects no windows rather than claiming the rest of the file.

    ``position`` is the scraped track number, which is what joins a segment back to its row in
    the tracklist table and to its boundary tick on the timeline; the segment list is NOT
    positionally parallel to the track list, because untimed tracks are absent from it.
    """

    position: int
    start_sec: float
    end_sec: float | None
    bpm: float | None
    """Median of the intersecting FINE windows' BPM. Median, not mean: a single mis-detected
    half- or double-time window would drag a mean well off the track's real tempo."""
    camelot: str | None
    """Duration-weighted modal Camelot code of the intersecting FINE windows."""
    key: str | None
    """That code's canonical key name ("8A" -> "A minor"), for the operator who does not read
    the wheel."""
    key_hue: int | None
    """``analysis_timeline.hue_for`` of the key NAME -- the identical call the key ribbon on the
    timeline makes, so one key is one colour on both surfaces. An open vocabulary of key strings
    is exactly what that hash is for, and is why the moods use a table instead."""
    mood: str | None
    """The argmax of the seven per-mood MEANS across the intersecting COARSE windows."""
    mood_label: str | None
    mood_hue: int | None
    """From ``analysis_timeline.MOOD_HUES`` -- the same table the river and its legend read, so
    a mood is one colour wherever it appears. Carried on the segment so the four surfaces that
    render the tracklist fragment cannot each acquire a different palette."""
    energy: float | None
    """Mean of the intersecting COARSE windows' stored energy scalars."""


def _timestamped(tracks: Sequence[TracklistTrack]) -> list[tuple[int, float]]:
    """``(position, start_sec)`` for every track with a parseable, non-negative timestamp.

    ``parse_timestamp_string`` is total by contract (``phaze-97u7``): empty cells, bracketed or
    decorated times and non-numeric segments all come back ``None`` rather than raising, which
    is exactly the "no segment for this track" signal rule 1 needs.
    """
    result: list[tuple[int, float]] = []
    for track in tracks:
        start = parse_timestamp_string(track.timestamp)
        if start is None or not math.isfinite(start) or start < 0:
            continue
        result.append((track.position, float(start)))
    return result


def _midpoint_within(window: AnalysisWindow, start: float, end: float | None) -> bool:
    if end is None or not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec):
        return False
    midpoint = (window.start_sec + window.end_sec) / 2.0
    return start <= midpoint < end


def _median_bpm(windows: Sequence[AnalysisWindow]) -> float | None:
    values = [float(window.bpm) for window in windows if window.bpm is not None and math.isfinite(window.bpm) and window.bpm > 0]
    return round(statistics.median(values), 1) if values else None


def _mean_energy(windows: Sequence[AnalysisWindow]) -> float | None:
    values = [float(window.energy) for window in windows if window.energy is not None and math.isfinite(window.energy)]
    return round(sum(values) / len(values), 3) if values else None


def _argmax_mood(windows: Sequence[AnalysisWindow]) -> str | None:
    """The mood whose MEAN score is highest across ``windows``, or ``None`` if none scored.

    Each mood is averaged over the windows that actually carry it, so a window missing one of
    the seven does not silently pull that mood's mean toward zero. Ties resolve to the earlier
    name in ``MOOD_NAMES`` (``MOOD_ORDER``'s own order), which makes the answer stable rather
    than dependent on dict iteration.
    """
    sums = dict.fromkeys(MOOD_NAMES, 0.0)
    counts = dict.fromkeys(MOOD_NAMES, 0)
    for window in windows:
        scores = window.mood_scores
        if not isinstance(scores, dict):
            continue
        for name in MOOD_NAMES:
            raw: Any = scores.get(name)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
                sums[name] += float(raw)
                counts[name] += 1
    means = {name: sums[name] / counts[name] for name in MOOD_NAMES if counts[name]}
    if not means:
        return None
    best = max(means.values())
    return next(name for name in MOOD_NAMES if name in means and means[name] == best)


def build_track_segments(
    tracks: Sequence[TracklistTrack],
    windows: Sequence[AnalysisWindow],
    duration_sec: float | None,
) -> list[TrackSegment]:
    """Join the scraped tracklist to the window projection, one segment per timestamped track.

    Segments are half-open ``[start, end)`` and consecutive: track *i* ends where the next
    TIMESTAMPED track begins, and the last ends at ``duration_sec``. A non-monotonic pair of
    scraped timestamps (a real 1001Tracklists defect, not a hypothetical) is clamped to a
    zero-length segment rather than a negative one -- it then intersects nothing and reads as
    em dashes, which is the correct answer for a boundary we cannot trust.

    ``duration_sec`` is the file's own duration. ``None``, non-finite, or a value at or before
    the last track's start leaves the final segment open-ended; see ``TrackSegment.end_sec``.
    """
    bounds = _timestamped(tracks)
    if not bounds:
        return []
    fine = [window for window in windows if window.tier == "fine"]
    coarse = [window for window in windows if window.tier == "coarse"]
    closing = duration_sec if duration_sec is not None and math.isfinite(duration_sec) and duration_sec > bounds[-1][1] else None

    segments: list[TrackSegment] = []
    for index, (position, start) in enumerate(bounds):
        end = max(start, bounds[index + 1][1]) if index + 1 < len(bounds) else closing
        in_fine = [window for window in fine if _midpoint_within(window, start, end)]
        in_coarse = [window for window in coarse if _midpoint_within(window, start, end)]
        camelot = modal_camelot(in_fine)
        mood = _argmax_mood(in_coarse)
        segments.append(
            TrackSegment(
                position=position,
                start_sec=start,
                end_sec=end,
                bpm=_median_bpm(in_fine),
                camelot=camelot,
                key=_CAMELOT_TO_KEY.get(camelot) if camelot else None,
                key_hue=hue_for(_CAMELOT_TO_KEY.get(camelot, camelot)) if camelot else None,
                mood=mood,
                mood_label=MOOD_LABELS.get(mood) if mood else None,
                mood_hue=MOOD_HUES.get(mood) if mood else None,
                energy=_mean_energy(in_coarse),
            )
        )
    return segments


__all__ = ["TrackSegment", "build_track_segments"]
