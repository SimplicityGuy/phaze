"""Analysis window geometry, per-window value containers, and aggregate reductions.

Extracted VERBATIM from ``services/analysis.py`` (phaze-bk9el.15) -- the window tuple
generator, the two frozen per-window records, and the pure reductions over them. No
essentia import, no I/O, no dependency on the analysis pipeline.

:func:`_iter_windows` is the exhaustiveness invariant's own function and moved UNCHANGED:
there is still no window cap, no even stride, no ``sampled`` flag and no ``deepen`` path
(D-07 / ADR-0007 section 7). What bounds memory is the CHUNKING in ``analysis.py``, which
did not move.

``services/analysis.py`` re-exports every name below, so ``phaze.services.analysis`` stays
the import site for dependents and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, median
from typing import Any


# ---------------------------------------------------------------------------
# Windowed time-series: per-window value containers + aggregate reductions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FineWindow:
    """A single fine-tier (BPM/key) analysis window."""

    window_index: int
    start_sec: float
    end_sec: float
    bpm: float | None
    musical_key: str | None
    confidence: float = 0.0

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
