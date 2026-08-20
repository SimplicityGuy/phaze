"""Safe, exhaustive presentation data for the windowed-analysis timeline.

The analysis pipeline owns what windows exist; this module only projects every stored row into
numeric SVG geometry, elapsed-time ticks, and an inspection payload. It must never sample, cap,
stride, or otherwise reinterpret coverage.
"""

from collections.abc import Sequence
import math
from typing import NamedTuple

from phaze.models.analysis import AnalysisWindow


TIMELINE_W = 1000.0
TIMELINE_H = 120.0


class BpmSpark(NamedTuple):
    """Numeric SVG points and the outward-rounded scale that contains them."""

    points: str
    lo: float | None
    hi: float | None
    window_count: int


def _valid_bpm(value: float | None) -> float | None:
    """Return a finite, physically meaningful BPM or an explicit absence."""
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def rounded_bpm_bounds(values: Sequence[float]) -> tuple[float, float] | None:
    """Round finite positive BPM extrema outward to containing multiples of ten.

    A lone value on an exact multiple needs an explicit interval on both sides. Otherwise the
    ordinary floor/ceiling pair already provides a deterministic, non-zero containing range.
    """
    valid = [value for raw in values if (value := _valid_bpm(raw)) is not None]
    if not valid:
        return None
    lo = math.floor(min(valid) / 10.0) * 10.0
    hi = math.ceil(max(valid) / 10.0) * 10.0
    if lo == hi:
        lo -= 10.0
        hi += 10.0
    return lo, hi


def bpm_spark(windows: Sequence[AnalysisWindow], total_sec: float, width: float, height: float) -> BpmSpark:
    """Map valid fine-window BPM values onto injection-safe numeric SVG coordinates."""
    pairs = [
        (window.start_sec, window.end_sec, bpm)
        for window in windows
        if (bpm := _valid_bpm(window.bpm)) is not None and math.isfinite(window.start_sec) and math.isfinite(window.end_sec)
    ]
    if not pairs or not math.isfinite(total_sec) or total_sec <= 0:
        return BpmSpark("", None, None, 0)
    bounds = rounded_bpm_bounds([bpm for _, _, bpm in pairs])
    if bounds is None:
        return BpmSpark("", None, None, 0)
    lo, hi = bounds
    span = hi - lo
    coords: list[str] = []
    for start_sec, end_sec, bpm in pairs:
        midpoint = (start_sec + end_sec) / 2.0
        x = min(max(midpoint / total_sec * width, 0.0), width)
        # Higher BPM sits higher on the chart (smaller y in SVG's top-left origin).
        y = height - ((bpm - lo) / span) * height
        coords.append(f"{x:.2f},{y:.2f}")
    return BpmSpark(" ".join(coords), lo, hi, len(pairs))


def format_elapsed_time(seconds: float) -> str:
    """Format an elapsed-time axis value without implying a wall-clock time."""
    whole = max(0, round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def elapsed_time_ticks(total_sec: float, *, target_intervals: int = 6) -> list[dict[str, float | str]]:
    """Build duration-appropriate ticks from zero through the analyzed duration."""
    if not math.isfinite(total_sec) or total_sec <= 0:
        return []
    desired_step = total_sec / max(target_intervals, 1)
    candidates = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400)
    step = next((candidate for candidate in candidates if candidate >= desired_step), None)
    if step is None:
        magnitude = 10 ** math.floor(math.log10(desired_step))
        step = next(factor * magnitude for factor in (1, 2, 5, 10) if factor * magnitude >= desired_step)

    values = [float(index * step) for index in range(math.floor(total_sec / step) + 1)]
    if not math.isclose(values[-1], total_sec, abs_tol=1e-6):
        values.append(total_sec)
    return [
        {
            "sec": value,
            "left_pct": round(value / total_sec * 100.0, 6),
            "label": format_elapsed_time(value),
        }
        for value in values
    ]


def _finite_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def inspection_windows(windows: Sequence[AnalysisWindow]) -> list[dict[str, object]]:
    """Serialize every natural window for safe client-side lookup at an arbitrary time."""
    return [
        {
            "tier": window.tier,
            "index": window.window_index,
            "start": _finite_or_none(window.start_sec),
            "end": _finite_or_none(window.end_sec),
            "bpm": _valid_bpm(window.bpm),
            "key": window.musical_key,
            "mood": window.mood,
            "style": window.style,
        }
        for window in windows
    ]


def hue_for(label: str) -> int:
    """Derive a stable integer HSL hue without putting label text into CSS syntax."""
    return sum(ord(character) for character in label) % 360


def ribbons(windows: Sequence[AnalysisWindow], attr: str, total_sec: float) -> list[dict[str, object]]:
    """Build positioned ribbon descriptors while preserving honest gaps between windows."""
    if not math.isfinite(total_sec) or total_sec <= 0:
        return []
    result: list[dict[str, object]] = []
    for window in windows:
        label = getattr(window, attr)
        if label is None or not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec):
            continue
        start = min(max(window.start_sec, 0.0), total_sec)
        end = min(max(window.end_sec, start), total_sec)
        result.append(
            {
                "label": label,
                "left_pct": round(start / total_sec * 100.0, 6),
                "width_pct": round((end - start) / total_sec * 100.0, 6),
                "hue": hue_for(str(label)),
            }
        )
    return result


def build_analysis_timeline_context(windows: Sequence[AnalysisWindow]) -> dict[str, object]:
    """Build the shared exhaustive timeline context for record and proposal presentations."""
    ordered = sorted(windows, key=lambda window: (window.tier, window.window_index))
    finite_ends = [window.end_sec for window in ordered if math.isfinite(window.end_sec) and window.end_sec >= 0]
    total_sec = max(finite_ends, default=0.0)
    fine = [window for window in ordered if window.tier == "fine"]
    coarse = [window for window in ordered if window.tier == "coarse"]
    spark = bpm_spark(fine, total_sec, TIMELINE_W, TIMELINE_H)
    return {
        "has_windows": bool(ordered),
        "total_sec": total_sec,
        "timeline_w": TIMELINE_W,
        "timeline_h": TIMELINE_H,
        "bpm_points": spark.points,
        "bpm_lo": spark.lo,
        "bpm_hi": spark.hi,
        "time_ticks": elapsed_time_ticks(total_sec),
        "timeline_inspection": {"duration": total_sec, "windows": inspection_windows(ordered)},
        "key_ribbons": ribbons(fine, "musical_key", total_sec),
        "mood_ribbons": ribbons(coarse, "mood", total_sec),
        "style_ribbons": ribbons(coarse, "style", total_sec),
    }
