"""The stored precision of a file's beat grid (phaze-f6eeb; docs/design/0026-subwindow-mood-and-beat-grid.md).

``analysis_beat_grid.beats`` is a ``real[]`` (float4): file-relative seconds, strictly ascending. This
module is the one place the writer turns computed positions into that form, so the beat an agent
sends and the beat a read returns are the same float, bit for bit. No essentia, no numpy, no I/O --
the agent, the control plane and the tests all import it.

Why float32 is enough, measured rather than assumed (essentia 2.1-beta6-dev, macOS arm64):
``RhythmExtractor2013(method="multifeature")`` returns its ticks as float32, quantized to its
512-sample hop at 44.1 kHz (11.61 ms). The float32 spacing at the longest analyzed file (12.07 h) is
3.9 ms, so rounding a file-relative position to float32 moves it by at most 1.95 ms -- under a sixth
of the resolution the ticks carry in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from statistics import median
import struct
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


_FLOAT32 = struct.Struct("<f")


def to_stored_precision(seconds: float) -> float:
    """Round ``seconds`` to the float32 value a ``real`` column stores, as a Python float."""
    return float(_FLOAT32.unpack(_FLOAT32.pack(seconds))[0])


def stored_beat_grid(beats: Iterable[float]) -> list[float]:
    """Round every beat to stored precision and refuse a grid that is not strictly ascending.

    A duplicate or a backwards step -- including two beats that only collapse onto one float32 value
    after rounding -- is a stitching defect upstream, and storing it would hand every reader a grid
    it has to re-validate.
    """
    stored = [to_stored_precision(beat) for beat in beats]
    for previous, current in pairwise(stored):
        if current <= previous:
            msg = f"beat grid must be strictly ascending at float32 precision: {previous!r} then {current!r}"
            raise ValueError(msg)
    return stored


# Stitching: one file-relative grid out of per-fine-window beat ticks (phaze-0296l)

# RhythmExtractor2013's own scale (essentia docs): confidence in [0, 5.32], below 1 is "low". A window
# below it still reports a tempo (bpm is unchanged) but its ticks are not trusted: measured on a window
# holding only a few edge clicks the extractor returned 39 confident-looking beats at confidence 0.19,
# against 3.94 on the clean windows of the same file. Such a window contributes no beats and so leaves a
# recorded gap, exactly as a silent one does. The implementer's choice (phaze-0296l), open to review.
MIN_BEAT_CONFIDENCE = 1.0
# A beat closer than this fraction of the local median interval to the previous accepted beat is the
# SAME beat seen from two windows (or a spurious double tick), and is dropped.
DUPLICATE_FRACTION = 0.5
# A stretch between two accepted beats wider than this multiple of the local median interval is a gap:
# recorded, never filled.
GAP_FACTOR = 1.5
# How many trailing intervals define the "local" median at a seam.
_LOCAL_INTERVALS = 16


@dataclass(frozen=True)
class StitchedBeatGrid:
    """The stitched grid: strictly ascending file-relative ``beats``, and the ``gaps`` left unfilled."""

    beats: list[float] = field(default_factory=list)
    gaps: list[tuple[float, float]] = field(default_factory=list)


def _local_median_interval(accepted: Sequence[float], window_beats: Sequence[float]) -> float | None:
    """Median beat interval near a seam: the accepted tail's, else the incoming window's own."""
    tail = accepted[-(_LOCAL_INTERVALS + 1) :]
    intervals = [b - a for a, b in pairwise(tail) if b > a]
    if not intervals:
        intervals = [b - a for a, b in pairwise(window_beats) if b > a]
    return float(median(intervals)) if intervals else None


def stitch_beat_grid(windows: Iterable[Sequence[float]]) -> StitchedBeatGrid:
    """Stitch per-window beat positions (ALREADY file-relative seconds) into one continuous grid.

    ``windows`` yields each analyzed fine window's beats in window order. A silent or failed window
    contributes an empty sequence, or is simply absent: either way it adds no beats, and the span it
    leaves between two accepted beats is what the gap rule below records.

    At every step the next candidate beat is compared with the last ACCEPTED beat, against the
    LOCAL median interval (the trailing accepted intervals, falling back to the incoming window's own
    when fewer than two beats exist yet):

    - closer than :data:`DUPLICATE_FRACTION` x median (or not strictly later): a duplicate -- dropped;
    - wider than :data:`GAP_FACTOR` x median: a gap -- ``(last, candidate)`` is RECORDED and the
      candidate is kept. Nothing is interpolated: a gap holds no invented beats.

    Where no median can be formed (fewer than two beats on either side of the seam) no gap can be
    judged, so none is recorded; the beats are simply concatenated. Silence before the first beat or
    after the last is not a gap between beats and is not recorded either.
    """
    beats: list[float] = []
    gaps: list[tuple[float, float]] = []
    for window_beats in windows:
        for beat in window_beats:
            if not beats:
                beats.append(beat)
                continue
            last = beats[-1]
            if beat <= last:
                continue
            local = _local_median_interval(beats, window_beats)
            delta = beat - last
            if local is not None:
                if delta < DUPLICATE_FRACTION * local:
                    continue
                if delta > GAP_FACTOR * local:
                    gaps.append((last, beat))
            beats.append(beat)
    return StitchedBeatGrid(beats=beats, gaps=gaps)
