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

from itertools import pairwise
import struct
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable


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
