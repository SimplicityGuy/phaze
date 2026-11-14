"""The scraped tracklist as an INDEX into the window projection (``phaze-x1qr3.6``).

A stored tracklist gives a set structure the audio alone does not: named tracks with
cue times. The windows give measurement the tracklist does not: BPM, key, mood, energy every
30 s and every 180 s. This module is the join -- consecutive scraped timestamps become half-open
time segments, and each segment collects the windows whose MIDPOINT falls inside it, so a
tracklist row can answer "what does track 7 actually sound like" from stored data.

Three rules the shape here exists to keep:

1. **A track without a qualified recording-relative offset produces no segment.** It is not given the previous
   track's range, and it is not given a zero-length one at position zero -- either would attach
   measurements from audio that is not that track. It renders as em dashes, which is the honest
   reading of "we do not know when this track starts".
2. **Midpoint containment, not overlap.** A 180 s coarse window straddling a track boundary
   belongs to exactly one of the two tracks -- whichever contains its centre. Overlap would let
   one window's mood count toward two tracks and make the per-track means sum to more than the
   file.
3. **No manufactured values.** A segment that intersects no window of the right tier gets
   ``None`` for that metric, never a zero, a mean of nothing, or the file's own average.

Mood has a finer source when one exists (``phaze-bxp3m``, docs/design/0026-subwindow-mood-and-beat-grid.md section 3). A file's
``analysis_subwindow`` rows are 15 s mood slices; a segment takes its mood from the slices whose
MIDPOINT falls inside it, by the same rule 2 that places coarse windows. That is what lets a track
shorter than 180 s have a mood at all. A segment with no slice carrying a mood vector falls back to
the coarse windows exactly as before, so a file analysed before sub-window mood existed produces
the same segments it always did.

I/O-free, like ``set_projection``: it takes rows and returns dataclasses, so the record page,
the two other surfaces that re-render the tracklist fragment, and the tests all share one
implementation.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
import math
import statistics
from typing import TYPE_CHECKING, Any, Final, Protocol

from phaze.services.analysis_timeline import MOOD_HUES, MOOD_LABELS, MOOD_NAMES
from phaze.services.analysis_windows import iter_subwindows
from phaze.services.set_glyph_colors import camelot_hue
from phaze.services.set_projection import MOOD_ORDER, camelot_number, key_name_for_camelot, modal_camelot
from phaze.tracklist_providers.domain import ProviderTrack


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from phaze.models.analysis import AnalysisSubwindow, AnalysisWindow
    from phaze.models.tracklist import TracklistTrack


class _Spanned(Protocol):
    """Anything with a file-relative span: a coarse/fine window or a sub-window slice."""

    @property
    def start_sec(self) -> float: ...

    @property
    def end_sec(self) -> float: ...


# Where each mood sits in a sub-window's positional ``mood_scores`` vector (MOOD_ORDER), resolved
# once. The same names as ``MOOD_NAMES``; only the lookup differs from the coarse dict.
_MOOD_SLOTS: Final[tuple[tuple[str, int], ...]] = tuple((name, MOOD_ORDER.index(name)) for name in MOOD_NAMES)


# Naming a code rather than reading the window's own ``musical_key`` is deliberate: the modal is
# computed over each window's ``camelot`` (a read-time property of ``musical_key`` since
# migration 066, phaze-6r3eh -- previously a stored column, same value either way), and
# essentia's flat spellings normalise onto one canonical key string on the way in, so the name
# shown beside a code is always that code's own canonical name and never one of its enharmonic
# twins. ``phaze-x1qr3.8`` moved the inversion itself into ``set_projection.key_name_for_camelot``
# -- the record page's facts list names a code the same way, and one inversion means the two
# surfaces cannot drift.


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
    mood: str | None
    """The argmax of the seven per-mood MEANS across the intersecting COARSE windows."""
    energy: float | None
    """Mean of the intersecting COARSE windows' stored energy scalars."""
    title: str | None = None
    """The scraped track title, carried so the timeline's readout can name the track the cursor
    is inside without the client re-joining the tracklist table (``phaze-x1qr3.10``). Defaulted,
    so every existing construction of this dataclass keeps working unchanged; ``None`` is the
    honest answer for a scraped row that carried no title at all."""

    # The four PRESENTATION readings below were stored fields, computed by the one constructor
    # call in ``build_track_segments`` and then carried. Each is a pure function of ``camelot``
    # or ``mood`` alone, so as fields they made an inconsistent segment REPRESENTABLE: a
    # ``TrackSegment(camelot="8A", key="F# minor", ...)`` type-checks, constructs, and renders a
    # key name that contradicts the code beside it. As properties there is nothing to keep in
    # step -- the render path reads the same attribute names it always did, and the only writable
    # state left is what was actually measured.

    @property
    def key(self) -> str | None:
        """``camelot``'s canonical key name ("8A" -> "A minor"), for the operator who does not
        read the wheel."""
        return key_name_for_camelot(self.camelot) if self.camelot else None

    @property
    def key_hue(self) -> int | None:
        """The shared Camelot hue used by the timeline ribbon, glyph and harmonic journey."""
        return camelot_hue(camelot_number(self.camelot)) if self.camelot else None

    @property
    def mood_label(self) -> str | None:
        """``mood``'s display name from ``analysis_timeline.MOOD_LABELS``."""
        return MOOD_LABELS.get(self.mood) if self.mood else None

    @property
    def mood_hue(self) -> int | None:
        """From ``analysis_timeline.MOOD_HUES`` -- the same table the river and its legend read,
        so a mood is one colour wherever it appears. Read off the segment so the four surfaces
        that render the tracklist fragment cannot each acquire a different palette."""
        return MOOD_HUES.get(self.mood) if self.mood else None


def _timestamped(tracks: Sequence[TracklistTrack | ProviderTrack]) -> list[tuple[int, float]]:
    """Qualified rational boundaries, converted to finite floats only at the analysis boundary.

    Only a reviewed selected source's projected ``ProviderTrack`` can carry a qualified,
    recording-bound offset. A legacy ``TracklistTrack`` is display-only and never yields a
    boundary: its raw ``timestamp`` text carries no meaning or precision, and no revalidation
    path exists to qualify it (docs/design/0027-retire-legacy-timestamp-qualification.md).
    """
    result: list[tuple[int, float]] = []
    for track in tracks:
        if not isinstance(track, ProviderTrack):
            continue
        timestamp = track.timestamp
        if timestamp is None or timestamp.kind != "offset" or timestamp.offset_usability != "qualified" or timestamp.offset is None:
            continue
        try:
            start = float(timestamp.offset.as_fraction())
        except OverflowError:
            continue
        if not math.isfinite(start) or start < 0:
            continue
        result.append((track.position, start))
    return result


def _midpoint(window: _Spanned) -> float | None:
    """A window's centre, or ``None`` when either bound is non-finite (rule 2 cannot place it)."""
    if not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec):
        return None
    return (window.start_sec + window.end_sec) / 2.0


class _MidpointIndex[S: _Spanned]:
    """Windows sorted by MIDPOINT, so a segment's members are one slice rather than one scan.

    Rule 2 (module docstring) places a window in exactly one segment, by its centre, so
    "the windows of segment i" is the half-open midpoint interval ``[start, end)`` and two
    bisects name it. The scan it replaces was O(tracks x windows) per TIER -- a 40-track set of
    1,440 fine and 240 coarse windows is 67,200 predicate evaluations for 40 answers, and both
    tiers were scanned separately (PR #556 review, finding 6).

    Deliberately does NOT assume the segments themselves are ordered. A non-monotonic pair of
    scraped timestamps is a real historical provider defect this module already handles, so a moving
    cursor over the segments would be a new assumption bought for nothing; two bisects per
    segment cost the same and assume only what is actually true.

    Members come back in the caller's original order. ``_mean_energy`` and ``_argmax_mood`` both
    aggregate with float addition, which is not associative, so returning them in midpoint order
    would shift the last bits of a rendered value for no reason a reader could reconstruct.
    """

    __slots__ = ("_midpoints", "_rows")

    def __init__(self, windows: Sequence[S]) -> None:
        placeable = [(midpoint, arrival, window) for arrival, window in enumerate(windows) if (midpoint := _midpoint(window)) is not None]
        self._rows = sorted(placeable, key=lambda row: row[0])
        self._midpoints = [row[0] for row in self._rows]

    def within(self, start: float, end: float | None) -> list[S]:
        """The windows whose midpoint falls in ``[start, end)``, in the caller's order.

        ``end`` of ``None`` is the last track of a file whose duration is unknown: it collects
        nothing rather than claiming the rest of the file, which is what ``TrackSegment.end_sec``
        documents and what the scan it replaces did.
        """
        if end is None:
            return []
        low = bisect.bisect_left(self._midpoints, start)
        high = bisect.bisect_left(self._midpoints, end)
        if low >= high:
            return []
        return [window for _midpoint, _arrival, window in sorted(self._rows[low:high], key=lambda row: row[1])]


def _median_bpm(windows: Sequence[AnalysisWindow]) -> float | None:
    """Median BPM of the fine windows inside ONE track segment -- a different quantity from the
    sidebar's file-wide Median BPM (``phaze-duyyw``).

    ``services/record_facts.py`` used to compute a file-wide window median with this same
    ``> 0`` filter, and that duplication was the defect ``phaze-duyyw`` fixed: the sidebar now
    reads ``AnalysisResult.bpm`` (the analyze stage's own confidence-gated aggregate) directly
    instead. This function is not that duplication -- it has no per-file equivalent to read
    instead, because ``AnalysisResult`` is 1:1 with the file, not with a track inside it. A
    track's own median is a genuinely narrower quantity (the fine windows whose midpoint falls
    inside THIS track's scraped time range, per ``build_track_segments``' rule 2), and no
    per-track aggregate is written at analysis time for it to read. Kept intentionally separate
    rather than sharing a helper with the module ``AnalysisResult.bpm`` is read from.
    """
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
            if _is_score(raw):
                sums[name] += float(raw)
                counts[name] += 1
    return _argmax_of_means(sums, counts)


def _is_score(raw: Any) -> bool:
    return isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw)


def _argmax_mood_vector(
    vectors: Sequence[Sequence[float | None] | None],
    uncovered: Sequence[AnalysisWindow] = (),
) -> str | None:
    """:func:`_argmax_mood` for sub-window slices, whose mood is a POSITIONAL vector in ``MOOD_ORDER``.

    Same arithmetic and same tie rule: each mood's mean over the slices that carry a finite score
    for it, the highest mean wins, ties go to the earlier name in ``MOOD_NAMES``. ``None`` vectors
    (slices no patch centre fell in) and short vectors contribute nothing.

    ``uncovered`` are the segment's coarse windows that no slice with a mood covers (phaze-7hcgn.5 finding F): a
    window whose sub-window mood was dropped, or one analysed before sub-windows existed. Each stands in for the
    slices it would have produced -- its name-keyed scores, weighted by its slice count -- so a segment's mood is the
    mean over ALL of its measured time, not over only the part that happens to have slices.
    """
    sums = dict.fromkeys(MOOD_NAMES, 0.0)
    counts = dict.fromkeys(MOOD_NAMES, 0.0)
    for vector in vectors:
        if vector is None:
            continue
        for name, slot in _MOOD_SLOTS:
            raw: Any = vector[slot] if slot < len(vector) else None
            if _is_score(raw):
                sums[name] += float(raw)
                counts[name] += 1
    if not any(counts.values()):
        return None  # no slice carries a mood: the caller's coarse path, unchanged
    for window in uncovered:
        scores = window.mood_scores
        weight = len(iter_subwindows(window.start_sec, window.end_sec))
        if not isinstance(scores, dict) or not weight:
            continue
        for name in MOOD_NAMES:
            raw = scores.get(name)
            if _is_score(raw):
                sums[name] += float(raw) * weight
                counts[name] += weight
    return _argmax_of_means(sums, counts)


def _argmax_of_means(sums: Mapping[str, float], counts: Mapping[str, float]) -> str | None:
    means = {name: sums[name] / counts[name] for name in MOOD_NAMES if counts[name]}
    if not means:
        return None
    best = max(means.values())
    return next(name for name in MOOD_NAMES if name in means and means[name] == best)


def build_track_segments(
    tracks: Sequence[TracklistTrack | ProviderTrack],
    windows: Sequence[AnalysisWindow],
    duration_sec: float | None,
    *,
    subwindows: Sequence[AnalysisSubwindow] | None = None,
) -> list[TrackSegment]:
    """Join the scraped tracklist to the window projection, one segment per timestamped track.

    ``subwindows`` is the file's ``analysis_subwindow`` rows, if it has any. A segment's ``mood`` is
    then the argmax of the sub-window slices whose midpoint falls in it, together with any of its coarse
    windows that no such slice covers, each weighted as the slices it would have had (phaze-7hcgn.5); a
    segment none of whose slices carries a mood vector keeps the coarse answer. ``None`` or empty is exactly the coarse
    path, so a file analysed before sub-window mood existed is unchanged.

    Segments are half-open ``[start, end)`` and consecutive: track *i* ends where the next
    TIMESTAMPED track begins, and the last ends at ``duration_sec``. A non-monotonic pair of
    scraped timestamps (a real historical provider defect, not a hypothetical) is clamped to a
    zero-length segment rather than a negative one -- it then intersects nothing and reads as
    em dashes, which is the correct answer for a boundary we cannot trust.

    ``duration_sec`` is the file's own duration. ``None``, non-finite, or a value at or before
    the last track's start leaves the final segment open-ended; see ``TrackSegment.end_sec``.
    """
    bounds = _timestamped(tracks)
    if not bounds:
        return []
    # Keyed by position rather than zipped with ``bounds``: ``_timestamped`` drops untimed
    # tracks, so the two lists are not positionally parallel and a zip would attach one
    # track's title to the next track's segment.
    titles = {track.position: track.title for track in tracks}
    fine = _MidpointIndex([window for window in windows if window.tier == "fine"])
    coarse = _MidpointIndex([window for window in windows if window.tier == "coarse"])
    slices = _MidpointIndex(list(subwindows or ()))
    closing = duration_sec if duration_sec is not None and math.isfinite(duration_sec) and duration_sec > bounds[-1][1] else None

    segments: list[TrackSegment] = []
    for index, (position, start) in enumerate(bounds):
        end = max(start, bounds[index + 1][1]) if index + 1 < len(bounds) else closing
        in_fine = fine.within(start, end)
        in_coarse = coarse.within(start, end)
        in_slices = slices.within(start, end)
        camelot = modal_camelot(in_fine)
        uncovered = [
            window for window in in_coarse if not any(row.mood_scores is not None for row in slices.within(window.start_sec, window.end_sec))
        ]
        mood = _argmax_mood_vector([row.mood_scores for row in in_slices], uncovered) if in_slices else None
        if mood is None:
            mood = _argmax_mood(in_coarse)
        segments.append(
            TrackSegment(
                position=position,
                start_sec=start,
                end_sec=end,
                bpm=_median_bpm(in_fine),
                camelot=camelot,
                mood=mood,
                energy=_mean_energy(in_coarse),
                title=titles.get(position),
            )
        )
    return segments


__all__ = ["TrackSegment", "build_track_segments"]
