"""Analysis window geometry, per-window value containers, and aggregate reductions.

Extracted VERBATIM from ``services/analysis.py`` (phaze-bk9el.15) -- the window tuple
generator, the two frozen per-window records, and the pure reductions over them. No
essentia import, no I/O, no dependency on the analysis pipeline.

:func:`_iter_windows` is the exhaustiveness invariant's own function and moved UNCHANGED:
there is still no window cap, no even stride, no ``sampled`` flag and no ``deepen`` path
(D-07 / ADR-0007 (windowed analysis) section 7). What bounds memory is the CHUNKING in ``analysis.py``, which
did not move.

``services/analysis.py`` re-exports every name below, so ``phaze.services.analysis`` stays
the import site for dependents and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from statistics import mean, median
from typing import Any


# Windowed time-series: per-window value containers + aggregate reductions


@dataclass(frozen=True)
class FineWindow:
    """A single fine-tier (BPM/key) analysis window."""

    window_index: int
    start_sec: float
    end_sec: float
    bpm: float | None
    musical_key: str | None
    confidence: float = 0.0
    # Beat ticks in FILE-relative seconds (window ticks + start_sec); empty for a silent window.
    # Deliberately NOT in as_payload_dict: the per-window payload is unchanged, and the stitched
    # per-file grid travels separately (phaze-0296l).
    beats: tuple[float, ...] = ()

    def as_payload_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict ready for AnalysisWindowPayload(**w)."""
        return {
            "tier": "fine",
            "window_index": self.window_index,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "bpm": self.bpm,
            "musical_key": self.musical_key,
        }


@dataclass(frozen=True)
class CoarseWindow:
    """A single coarse-tier (mood/style/danceability) analysis window."""

    window_index: int
    start_sec: float
    end_sec: float
    mood: str | None
    style: str | None
    danceability: float | None
    features: dict[str, Any] = field(default_factory=dict)
    # One entry per sub-window slice of this window (see iter_subwindows): the pooled mood vector,
    # positional in MOOD_ORDER, or None for a slice holding no patch centre. Empty when the patch
    # matrices were unavailable. Not in as_payload_dict (phaze-x3gak).
    subwindow_moods: tuple[tuple[float, ...] | None, ...] = ()
    # This window's pooled Discogs-EffNet embedding, as analysis_embeddings.encode_embedding's base64 of
    # 1280 little-endian float16 values, or None: not asked for, silent, under one patch, or dropped.
    # Not in as_payload_dict; it travels only in the result's "embeddings" key, when asked for.
    embedding: str | None = None

    def as_payload_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict ready for AnalysisWindowPayload(**w)."""
        return {
            "tier": "coarse",
            "window_index": self.window_index,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "mood": self.mood,
            "style": self.style,
            "danceability": self.danceability,
            "features": self.features,
        }


def aggregate_bpm(fine: list[FineWindow]) -> float | None:
    """Representative BPM = median of fine-window BPMs (rounded to 0.1).

    Excludes windows with ``confidence == 0.0`` (unreliable BPM on short/silent
    audio per RESEARCH Pitfall 2) and windows with no BPM. Returns None if empty.
    """
    vals = [w.bpm for w in fine if w.bpm is not None and w.confidence != 0.0]
    return round(median(vals), 1) if vals else None


def _max_by_duration(weights: dict[str, float]) -> str | None:
    """Return the key with the greatest accumulated duration (stable on ties)."""
    if not weights:
        return None
    # max() is stable: on a tie it returns the first-inserted key.
    return max(weights, key=lambda k: weights[k])


def aggregate_key(fine: list[FineWindow]) -> str | None:
    """Representative key = duration-weighted modal key across fine windows."""
    weights: dict[str, float] = {}
    for w in fine:
        if w.musical_key:
            weights[w.musical_key] = weights.get(w.musical_key, 0.0) + (w.end_sec - w.start_sec)
    return _max_by_duration(weights)


def aggregate_dominant(coarse: list[CoarseWindow], attr: str) -> str | None:
    """Time-weighted dominant label (mood/style) across coarse windows."""
    weights: dict[str, float] = {}
    for w in coarse:
        label = getattr(w, attr)
        if label:
            weights[label] = weights.get(label, 0.0) + (w.end_sec - w.start_sec)
    return _max_by_duration(weights)


def aggregate_danceability(coarse: list[CoarseWindow]) -> float | None:
    """Representative danceability = mean across coarse windows; None if empty."""
    vals = [w.danceability for w in coarse if w.danceability is not None]
    return mean(vals) if vals else None


def _representative_features(coarse: list[CoarseWindow]) -> dict[str, Any]:
    """Pick a representative full-features dict for the aggregate ``analysis`` row.

    Returns the longest-duration coarse window's features (ties → first). Keeps
    the existing ``features`` JSONB structure (all model sets + genre) populated
    for downstream consumers; empty dict when there are no coarse windows.
    """
    if not coarse:
        return {}
    longest = max(coarse, key=lambda w: w.end_sec - w.start_sec)
    return longest.features


def _iter_windows(total_sec: float, win_sec: int, min_sec: int, *, drop_short_trailing: bool) -> list[tuple[int, float, float]]:
    """Yield ``(index, start_sec, end_sec)`` for fixed-duration windows over a file.

    When ``drop_short_trailing`` is True (FINE tier), a trailing window shorter
    than ``min_sec`` is dropped — EXCEPT window 0, so very short tracks still
    produce one window. When False (COARSE tier) every window with audio is
    emitted (no minimum-length floor; RESEARCH Open Q3 RESOLVED).
    """
    windows: list[tuple[int, float, float]] = []
    start = 0.0
    idx = 0
    while start < total_sec:
        end = min(start + win_sec, total_sec)
        if drop_short_trailing and (end - start) < min_sec and idx > 0:
            break
        windows.append((idx, start, end))
        start = end
        idx += 1
    return windows


# Sub-windows: the fixed slices of a COARSE window that sub-window mood is pooled into
# (phaze-f6eeb; docs/design/0026-subwindow-mood-and-beat-grid.md).

# 15 s divides both the 180 s coarse window and the 30 s fine window, so the default layout puts every
# sub-window boundary on a file-wide 15 s grid that also lines up with the fine-window boundaries. It
# divides NEITHER measured patch hop (musicnn 1.488 s, VGGish 0.93 s); patches are attributed to the
# sub-window holding their centre, which is why it does not need to (the ADR's section 2).
SUBWINDOW_SEC: int = 15


def iter_subwindows(start_sec: float, end_sec: float, sub_sec: int = SUBWINDOW_SEC) -> list[tuple[int, float, float]]:
    """``(sub_index, start_sec, end_sec)`` for the sub-windows of ONE coarse window, in file seconds.

    ``ceil((end_sec - start_sec) / sub_sec)`` slices laid from the coarse window's own start, the last
    one clipped to ``end_sec``, so a short trailing coarse window keeps a short trailing sub-window
    instead of losing its audio. Relative to the coarse window rather than to file time zero: the
    patch matrix a coarse window's inference returns starts at that window's ``start_sec``, so a
    sub-window can only ever hold patches of one coarse window. With the defaults (coarse windows tiled
    from 0 in 180 s strides) the two layouts coincide; with a ``coarse_window_sec`` that is not a
    multiple of ``sub_sec`` only this one is right.

    The bounds are stored on every ``analysis_subwindow`` row as well, so a reader never has to
    re-derive them and a later change to ``SUBWINDOW_SEC`` cannot silently re-time rows already
    written.
    """
    if sub_sec <= 0:
        msg = f"sub_sec must be positive, got {sub_sec}"
        raise ValueError(msg)
    span = end_sec - start_sec
    if span <= 0:
        return []
    count = math.ceil(span / sub_sec)
    return [(i, start_sec + i * sub_sec, min(start_sec + (i + 1) * sub_sec, end_sec)) for i in range(count)]
