"""Safe, exhaustive presentation data for the windowed-analysis timeline.

The analysis pipeline owns what windows exist; this module only projects every stored row into
numeric SVG geometry, elapsed-time ticks, and an inspection payload. It must never sample, cap,
stride, or otherwise reinterpret coverage.
"""

from collections.abc import Callable, Sequence
import math
from typing import TYPE_CHECKING, Final, NamedTuple, Protocol, cast

from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.services.set_projection import GAP_TOLERANCE_SEC, MOOD_ORDER, flicker_filtered_key_runs, placeable_key_runs


if TYPE_CHECKING:
    from phaze.models.set_profile import SetProfile


TIMELINE_W = 640.0
TIMELINE_H = 120.0
WINDOW_WIDTH_PX = 56.0
# The energy area and the mood river are shorter than the BPM plot: they carry a 0..1 scale
# rather than a spread of real BPM values, so they need less vertical room to be readable.
LANE_H = 56.0


# The 7 binary MOOD classifiers, in ``MOOD_ORDER``'s own order -- DERIVED from that tuple by
# prefix rather than re-listed, so the river's stacking order can never drift from the order
# ``AnalysisWindow.mood_scores`` and ``SetProfile.mean_vector`` are keyed/positional in. The
# other 4 ``MOOD_ORDER`` names (danceability, gender, tonality, voice_instrumental) are not
# moods and are not stacked in the river.
MOOD_NAMES: Final[tuple[str, ...]] = tuple(name for name in MOOD_ORDER if name.startswith("mood_"))

# THE ONE SOURCE OF MOOD COLOUR. The river's bands, the river's legend and the tracklist's
# per-track mood dots all read this table and nothing else -- no surface derives, hashes or
# hard-codes a mood hue of its own, and no template carries an ``hsl(<number>`` literal for a
# mood. ``tests/shared/services/test_analysis_timeline_lanes.py`` fails the build on either.
#
# WHY A TABLE AND NOT :func:`hue_for`: ``hue_for`` is a character-sum hash, which is right for
# the OPEN vocabularies it serves (musical keys, essentia style labels -- unbounded sets where
# no hand-maintained table could be complete). The 7 moods are a CLOSED, fixed vocabulary
# rendered as 7 adjacent bands of one stacked lane, which is the case a hash is worst at:
# measured on these exact 7 names, ``hue_for`` puts ``mood_electronic`` at 150 and
# ``mood_aggressive`` at 158 -- 8 degrees apart, indistinguishable where they touch -- and
# ``mood_party`` at 6 against ``mood_happy`` at 352. A stacked categorical lane needs a
# categorical palette, so these hues are picked for separation and validated as such: the
# guard test asserts all 7 are at least ``MIN_MOOD_HUE_SEPARATION_DEG`` apart around the
# circle. Hue only -- saturation and lightness stay in the template so the bands can respond
# to the light/dark theme without a second colour table (ADR-0010, colour contrast tokens).
MOOD_HUES: Final[dict[str, int]] = {
    "mood_acoustic": 32,  # warm amber
    "mood_electronic": 268,  # violet
    "mood_aggressive": 0,  # red
    "mood_relaxed": 168,  # green-teal
    "mood_happy": 58,  # yellow
    "mood_sad": 212,  # blue
    "mood_party": 322,  # magenta
}

# The separation floor the palette above is validated against, in degrees around the hue
# circle. 25 is what distinguishes two touching bands at the lane's own size; it is asserted,
# not aspirational, so shifting any hue into a neighbour fails the build rather than shipping
# two moods that read as one colour.
MIN_MOOD_HUE_SEPARATION_DEG: Final[int] = 25

# Display text for a mood name. Colour is never the ONLY channel carrying a mood: the legend
# pairs every swatch with this label, and the tracklist's dot carries it as a title.
MOOD_LABELS: Final[dict[str, str]] = {name: name.removeprefix("mood_").capitalize() for name in MOOD_HUES}


class TrackBoundary(Protocol):
    """The two fields a tracklist boundary tick needs from ``phaze-x1qr3.6``'s ``TrackSegment``.

    Structural, so this module renders ticks without importing the track-segment service --
    the segments are built from the tracklist and handed IN, keeping the lane geometry here
    and the tracklist reading there. Read-only properties, so a frozen dataclass satisfies it.
    """

    @property
    def position(self) -> int: ...

    @property
    def start_sec(self) -> float: ...


class TrackSpan(TrackBoundary, Protocol):
    """A boundary plus the two fields the INSPECTION payload needs: where the track ends, and
    what it is called.

    Separate from :class:`TrackBoundary` because a tick genuinely needs only a position and a
    start -- a track's end is the next track's start, which the tick lane never has to know.
    The readout does: "you are inside track 7" is a claim about a RANGE, and a payload built
    from starts alone would either have to guess the last track's end or leave the cursor
    inside track 7 forever after the set finished.
    """

    @property
    def end_sec(self) -> float | None: ...

    @property
    def title(self) -> str | None: ...


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


def bpm_segments(
    windows: Sequence[AnalysisWindow],
    total_sec: float,
    width: float,
    height: float,
    lo: float | None,
    hi: float | None,
) -> list[dict[str, object]]:
    """Build one polyline per contiguous measured run so gaps are never bridged."""
    if lo is None or hi is None or hi <= lo or total_sec <= 0:
        return []
    result: list[dict[str, object]] = []
    run: list[tuple[float, float]] = []
    previous_end: float | None = None

    def flush() -> None:
        if not run:
            return
        result.append(
            {
                "points": " ".join(f"{x:.2f},{y:.2f}" for x, y in run),
                "single_x": run[0][0] if len(run) == 1 else None,
                "single_y": run[0][1] if len(run) == 1 else None,
            }
        )
        run.clear()

    for window in windows:
        bpm = _valid_bpm(window.bpm)
        if bpm is None or not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec):
            flush()
            previous_end = None
            continue
        if previous_end is not None and window.start_sec > previous_end + 1e-6:
            flush()
        midpoint = (window.start_sec + window.end_sec) / 2.0
        x = min(max(midpoint / total_sec * width, 0.0), width)
        y = height - ((bpm - lo) / (hi - lo)) * height
        run.append((x, y))
        previous_end = window.end_sec
    flush()
    return result


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


def top_mood(window: AnalysisWindow) -> dict[str, object] | None:
    """The strongest of the seven mood classifiers in one coarse window, with its share.

    Reads :func:`mood_stack`, so the number the readout says is the same normalised fraction
    the river draws as that band's thickness -- a share computed a second way here would let
    the sentence and the picture disagree about the same window. ``None`` wherever the stack
    is ``None``: a window with no projected mood scores has no top mood, and reporting the
    first of seven zeroes would manufacture one.

    Ties resolve to ``MOOD_NAMES`` order, which is stable across renders of identical data.
    """
    fractions = mood_stack(window)
    if fractions is None:
        return None
    # ``max`` returns the FIRST maximal element, so a tie resolves to MOOD_NAMES order without
    # a tie-break clause -- the same rule the river's stacking order already follows.
    share, name = max(zip(fractions, MOOD_NAMES, strict=True), key=lambda pair: pair[0])
    return {"name": name, "label": MOOD_LABELS.get(name, name), "share": round(share, 4)}


def inspection_windows(windows: Sequence[AnalysisWindow]) -> list[dict[str, object]]:
    """Serialize every natural window for safe client-side lookup at an arbitrary time.

    ``phaze-x1qr3.10`` added ``camelot``, ``energy`` and ``mood_top``: the inspection readout
    names the key by its Camelot code, the energy the area lane draws, and the strongest mood
    with its share -- and every one of those has to come from the SAME window row the lanes
    were drawn from, or the sentence under the cursor describes a different window than the
    picture at it. Absences stay explicit ``None`` rather than becoming a zero or an empty
    string; the client renders an em dash for them.
    """
    return [
        {
            "tier": window.tier,
            "index": window.window_index,
            "start": _finite_or_none(window.start_sec),
            "end": _finite_or_none(window.end_sec),
            "bpm": _valid_bpm(window.bpm),
            "key": window.musical_key,
            "camelot": window.camelot,
            "energy": _measured_energy(window),
            "mood": window.mood,
            "mood_top": top_mood(window),
            "style": window.style,
        }
        for window in windows
    ]


def resolve_inspection(windows: Sequence[AnalysisWindow], time_sec: float, total_sec: float) -> dict[str, object]:
    """Resolve the containing fine and coarse windows at one in-range elapsed time."""
    payload = inspection_windows(windows)

    def containing(tier: str) -> dict[str, object] | None:
        candidates = [
            window
            for window in payload
            if window["tier"] == tier and isinstance(window["start"], float) and isinstance(window["end"], float) and window["start"] <= time_sec
        ]
        if not candidates:
            return None
        candidate = max(candidates, key=lambda window: cast("float", window["start"]))
        candidate_end = cast("float", candidate["end"])
        includes_final_end = math.isclose(time_sec, total_sec) and math.isclose(time_sec, candidate_end)
        return candidate if time_sec < candidate_end or includes_final_end else None

    return {"time": time_sec, "fine": containing("fine"), "coarse": containing("coarse")}


def inspection_segments(segments: Sequence[TrackSpan], total_sec: float) -> list[dict[str, object]]:
    """Serialize each timestamped track as a half-open span the client can look a time up in.

    ``end`` is clamped into ``[start, total_sec]`` and is ``None`` only where the segment
    itself is open-ended -- the last track of a file whose duration is unknown. The client
    treats a ``None`` end as "runs to the end of what we know", which is the same reading
    ``TrackSegment`` documents; it does not invent a length.

    A non-monotonic scraped pair already arrives clamped to a zero-length segment, and one
    stays zero-length here: it then contains no time at all, so the cursor never reports being
    inside a track whose boundaries we do not trust.
    """
    if not math.isfinite(total_sec) or total_sec <= 0:
        return []
    result: list[dict[str, object]] = []
    for segment in segments:
        start = segment.start_sec
        if not math.isfinite(start) or start < 0 or start > total_sec:
            continue
        end = segment.end_sec
        bounded = min(max(end, start), total_sec) if end is not None and math.isfinite(end) else None
        result.append({"position": segment.position, "title": segment.title, "start": start, "end": bounded})
    return result


def inspection_key_runs(fine_windows: Sequence[AnalysisWindow]) -> list[dict[str, object]]:
    """The flicker-filtered key runs, numbered exactly as the harmonic wheel numbers its nodes.

    ``index`` is the position in :func:`~phaze.services.set_projection.placeable_key_runs`' output, which is
    what ``harmonic_journey._nodes`` enumerates to stamp ``data-node-index`` on each drawn
    node. That shared call is the whole point: the cursor ring is placed by looking a node up
    BY INDEX, so a payload that filtered runs even slightly differently would ring the wrong
    key from the first unplaceable code onward -- and would do it silently, because every
    index still resolves to a real node.

    Empty for a file with no usable ``camelot`` data, which is the same file the wheel draws
    with no nodes at all.
    """
    return [
        {"index": index, "code": run.code, "number": number, "start": run.start_sec, "end": run.end_sec}
        for index, (run, number) in enumerate(placeable_key_runs(flicker_filtered_key_runs(fine_windows)))
    ]


def hue_for(label: str) -> int:
    """Derive a stable integer HSL hue without putting label text into CSS syntax."""
    return sum(ord(character) for character in label) % 360


def ribbons(windows: Sequence[AnalysisWindow], attr: str, total_sec: float, *, code_attr: str | None = None) -> list[dict[str, object]]:
    """Build positioned ribbon descriptors while preserving honest gaps between windows.

    ``code_attr`` names an OPTIONAL second, already-projected column to show alongside the raw
    label -- the key lane passes ``"camelot"`` so a ribbon reads "A minor · 8A" rather than a
    bare key, which is the whole point of storing the Camelot code: the wheel position is what
    makes two adjacent keys legible as a harmonic move. ``label`` stays the raw attribute
    value (unchanged for every existing caller and its tests); ``display`` is what a surface
    renders, and ``code`` is the projected value on its own.
    """
    if not math.isfinite(total_sec) or total_sec <= 0:
        return []
    result: list[dict[str, object]] = []
    for window in windows:
        label = getattr(window, attr)
        if label is None or not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec):
            continue
        code = getattr(window, code_attr) if code_attr is not None else None
        start = min(max(window.start_sec, 0.0), total_sec)
        end = min(max(window.end_sec, start), total_sec)
        result.append(
            {
                "label": label,
                "code": code,
                "display": f"{label} \u00b7 {code}" if code else str(label),
                "left_pct": round(start / total_sec * 100.0, 6),
                "width_pct": round((end - start) / total_sec * 100.0, 6),
                # phaze-x1qr3.10: the ribbon's own clamped seconds, rendered as data attributes so
                # the inspector can outline the ribbon UNDER the cursor by reading the DOM rather
                # than by re-deriving a percentage and hoping it lands inside the same box. A
                # rounding difference of one part in 1e6 is invisible on screen and decides the
                # wrong ribbon at a boundary, which is exactly where the outline is being read.
                "start_sec": start,
                "end_sec": end,
                "hue": hue_for(str(label)),
            }
        )
    return result


# Set-panel lanes (phaze-x1qr3.5): energy area, mood river, tracklist ticks, coverage chip

# A coarse step is ~180 s, so a second of slop between one window's end and the next's start
# is float noise, not missing coverage. IMPORTED from ``set_projection.GAP_TOLERANCE_SEC``, not
# restated: the lane geometry drawn here and the coverage the stored profile records are answers
# to the same question about the same windows, so they have to move together. The comment that
# stood here claimed a restatement kept the two from disagreeing, which is backwards -- two
# literals are exactly how they come to disagree, and nothing would have caught it.
LANE_GAP_TOLERANCE_SEC: Final[float] = GAP_TOLERANCE_SEC

# Half-width, in canvas units, of the column drawn for a run of exactly ONE measured window.
# A run of one has no second point to draw a polygon between, and dropping it would render a
# measured window as a gap -- the one thing these lanes must never do.
SINGLE_COLUMN_HALF_W: Final[float] = 6.0


def _midpoint_x(window: AnalysisWindow, total_sec: float, width: float) -> float:
    """Canvas x of a window's midpoint, clamped into the canvas."""
    midpoint = (window.start_sec + window.end_sec) / 2.0
    return min(max(midpoint / total_sec * width, 0.0), width)


def _contiguous_runs(windows: Sequence[AnalysisWindow], usable: Callable[[AnalysisWindow], bool]) -> list[list[AnalysisWindow]]:
    """Split ``windows`` into runs of adjacent, usable coverage -- a gap ENDS a run.

    Two things end a run and they are not the same fact: a window whose own value is absent
    (never projected, or projected as an honest all-``None``), and a time hole between one
    window's end and the next's start. Both must break the drawing, because a line or a band
    continued across either would assert coverage the file does not have.
    """
    runs: list[list[AnalysisWindow]] = []
    current: list[AnalysisWindow] = []
    previous_end: float | None = None
    for window in windows:
        if not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec) or not usable(window):
            if current:
                runs.append(current)
                current = []
            previous_end = None
            continue
        if previous_end is not None and window.start_sec > previous_end + LANE_GAP_TOLERANCE_SEC and current:
            runs.append(current)
            current = []
        current.append(window)
        previous_end = window.end_sec
    if current:
        runs.append(current)
    return runs


def _measured_energy(window: AnalysisWindow) -> float | None:
    """A window's stored energy scalar, or an explicit absence (never a manufactured zero)."""
    value = window.energy
    if value is None or not math.isfinite(value):
        return None
    return min(max(float(value), 0.0), 1.0)


def energy_area(coarse_windows: Sequence[AnalysisWindow], total_sec: float, width: float, height: float) -> list[dict[str, object]]:
    """One filled area per contiguous run of measured energy, so a gap is drawn as a gap.

    Reads ``analysis_window.energy`` -- the stored projection column -- and never ``features``.
    Points sit at window MIDPOINTS (the same convention as the BPM spark), so the two lanes
    share one horizontal reading of the same file.
    """
    if not math.isfinite(total_sec) or total_sec <= 0 or width <= 0 or height <= 0:
        return []
    result: list[dict[str, object]] = []
    for run in _contiguous_runs(coarse_windows, lambda w: _measured_energy(w) is not None):
        points = [(_midpoint_x(window, total_sec, width), height - cast("float", _measured_energy(window)) * height) for window in run]
        if len(points) == 1:
            x, y = points[0]
            left, right = max(0.0, x - SINGLE_COLUMN_HALF_W), min(width, x + SINGLE_COLUMN_HALF_W)
            area = [(left, y), (right, y), (right, height), (left, height)]
        else:
            area = [*points, (points[-1][0], height), (points[0][0], height)]
        result.append(
            {
                "points": " ".join(f"{x:.2f},{y:.2f}" for x, y in points),
                "area_points": " ".join(f"{x:.2f},{y:.2f}" for x, y in area),
                "single_x": points[0][0] if len(points) == 1 else None,
                "single_y": points[0][1] if len(points) == 1 else None,
            }
        )
    return result


def energy_peak(coarse_windows: Sequence[AnalysisWindow], total_sec: float, width: float, height: float) -> dict[str, object] | None:
    """The loudest-reading moment of the set: the highest measured energy window, marked.

    Ties resolve to the EARLIEST window, so the mark is stable across re-renders of identical
    data. Returns ``None`` when no coarse window carries a measured energy -- the caller then
    renders no mark rather than a mark at zero.
    """
    if not math.isfinite(total_sec) or total_sec <= 0 or width <= 0 or height <= 0:
        return None
    best: tuple[float, AnalysisWindow] | None = None
    for window in coarse_windows:
        value = _measured_energy(window)
        if value is None or not math.isfinite(window.start_sec) or not math.isfinite(window.end_sec):
            continue
        if best is None or value > best[0]:
            best = (value, window)
    if best is None:
        return None
    value, window = best
    midpoint = (window.start_sec + window.end_sec) / 2.0
    return {
        "energy": round(value, 4),
        "sec": midpoint,
        "x": _midpoint_x(window, total_sec, width),
        "y": height - value * height,
        "label": format_elapsed_time(midpoint),
    }


def mood_stack(window: AnalysisWindow) -> tuple[float, ...] | None:
    """The 7 mood scores of one coarse window as fractions summing to 1.0, or ``None``.

    ``None`` is the honest gap: a window never projected, or projected as the fully-``None``
    entry ``phaze-x1qr3.3``'s writer stores for a coarse window with no features. A window
    carrying SOME of the seven normalises over what it has rather than being discarded --
    dropping it would turn partial data into a hole, which overstates the gap.
    """
    scores = window.mood_scores
    if not isinstance(scores, dict):
        return None
    values: list[float] = []
    for name in MOOD_NAMES:
        raw = scores.get(name)
        values.append(float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and raw > 0 else 0.0)
    total = sum(values)
    if total <= 0:
        return None
    return tuple(value / total for value in values)


def mood_river_columns(coarse_windows: Sequence[AnalysisWindow], total_sec: float, width: float, height: float) -> list[list[dict[str, object]]]:
    """Per-window stacked boundaries, grouped into contiguous runs -- the river's raw geometry.

    Each column carries ``x`` and ``boundaries``: 8 y values, bottom (``height``) first and top
    (``0.0``) last, one more than there are moods. Band *i* is bounded by ``boundaries[i]``
    below and ``boundaries[i + 1]`` above, so the 7 thicknesses sum to exactly ``height`` by
    construction. :func:`mood_river` builds its polygons from these numbers and nothing else,
    which is what makes asserting on them an assertion about what is drawn.
    """
    if not math.isfinite(total_sec) or total_sec <= 0 or width <= 0 or height <= 0:
        return []
    runs: list[list[dict[str, object]]] = []
    for run in _contiguous_runs(coarse_windows, lambda w: mood_stack(w) is not None):
        columns: list[dict[str, object]] = []
        for window in run:
            fractions = cast("tuple[float, ...]", mood_stack(window))
            boundaries = [height]
            for fraction in fractions:
                boundaries.append(boundaries[-1] - fraction * height)
            # Pin the top edge: floating-point accumulation over 7 divisions must not leave the
            # stack a hair short of the lane, or the topmost band renders a sliver of background.
            boundaries[-1] = 0.0
            columns.append({"x": _midpoint_x(window, total_sec, width), "boundaries": boundaries})
        runs.append(columns)
    return runs


def mood_river(coarse_windows: Sequence[AnalysisWindow], total_sec: float, width: float, height: float) -> list[dict[str, object]]:
    """The stacked mood river: one series per mood, one polygon per contiguous run.

    A coverage gap yields a series with MORE THAN ONE polygon and no geometry spanning the
    hole -- the river breaks exactly where the file stops being measured. Hues come from
    :data:`MOOD_HUES` and from nowhere else.
    """
    runs = mood_river_columns(coarse_windows, total_sec, width, height)
    series: list[dict[str, object]] = []
    for index, name in enumerate(MOOD_NAMES):
        polygons: list[str] = []
        for columns in runs:
            xs = [cast("float", column["x"]) for column in columns]
            tops = [cast("list[float]", column["boundaries"])[index + 1] for column in columns]
            bottoms = [cast("list[float]", column["boundaries"])[index] for column in columns]
            if len(columns) == 1:
                left, right = max(0.0, xs[0] - SINGLE_COLUMN_HALF_W), min(width, xs[0] + SINGLE_COLUMN_HALF_W)
                corners = [(left, tops[0]), (right, tops[0]), (right, bottoms[0]), (left, bottoms[0])]
            else:
                corners = list(zip(xs, tops, strict=True)) + list(zip(reversed(xs), reversed(bottoms), strict=True))
            polygons.append(" ".join(f"{x:.2f},{y:.2f}" for x, y in corners))
        series.append({"name": name, "label": MOOD_LABELS[name], "hue": MOOD_HUES[name], "polygons": polygons})
    return series


def mood_legend() -> list[dict[str, object]]:
    """Swatch, label and hue per mood, in stacking order -- the river's key, from one table.

    Colour is never the only channel: every entry pairs its hue with :data:`MOOD_LABELS` text,
    so the river remains readable without colour perception.
    """
    return [{"name": name, "label": MOOD_LABELS[name], "hue": MOOD_HUES[name]} for name in MOOD_NAMES]


def tracklist_ticks(boundaries: Sequence[TrackBoundary], total_sec: float) -> list[dict[str, object]]:
    """One positioned tick per track boundary, drawn across every lane.

    ``boundaries`` is ``phaze-x1qr3.6``'s segment list, which contains ONLY tracks that carried
    a parseable timestamp -- a track without one contributes no tick here because it produced
    no segment there, never because this function filtered it. A file with no tracklist hands
    in an empty sequence and gets an empty list, which the template renders as no tick row.
    """
    if not math.isfinite(total_sec) or total_sec <= 0:
        return []
    ticks: list[dict[str, object]] = []
    for boundary in boundaries:
        start = boundary.start_sec
        if not math.isfinite(start) or start < 0 or start > total_sec:
            continue
        ticks.append(
            {
                "position": boundary.position,
                "sec": start,
                "left_pct": round(start / total_sec * 100.0, 6),
                "label": str(boundary.position),
                "time": format_elapsed_time(start),
            }
        )
    return ticks


def coverage_chip(analysis: AnalysisResult | None) -> dict[str, object] | None:
    """The "N coarse · M fine · no gaps" chip, read from the ANALYSIS ROW's own counters.

    ``fine_windows_analyzed`` / ``fine_windows_total`` (and the coarse pair) are what the
    analysis child itself recorded, so they are the only place that knows a window was
    PLANNED and not analyzed. Counting the stored ``AnalysisWindow`` rows instead could only
    ever report what exists, which is precisely the number that cannot reveal a gap.

    ``None`` when there is no analysis row or it carries no counters at all -- an absent chip,
    never a chip claiming zero. A tier whose OWN counter is ``NULL`` (a fine-only progress
    upsert, ``routers/agent_analysis.py``'s ``put_analysis_progress``, leaves the coarse pair
    untouched -- both columns together, never a lone one) is a third state, distinct from both
    "complete" and "short": UNKNOWN, not zero. It renders as ``"?"`` rather than folding into the
    ``0`` a bare ``coarse_done or 0`` would print, and -- because "no gaps" is a claim this
    function can only make when it has actually seen every tier's count -- it sets the gap state
    just as a genuine short tier would, rather than leaving it False by omission.
    """
    if analysis is None:
        return None
    fine_done, fine_total = analysis.fine_windows_analyzed, analysis.fine_windows_total
    coarse_done, coarse_total = analysis.coarse_windows_analyzed, analysis.coarse_windows_total
    if fine_done is None and coarse_done is None:
        return None
    tier_unknown = fine_done is None or coarse_done is None
    has_gaps = (
        tier_unknown
        or (fine_total is not None and fine_done is not None and fine_done < fine_total)
        or (coarse_total is not None and coarse_done is not None and coarse_done < coarse_total)
    )
    coarse_display = coarse_done if coarse_done is not None else "?"
    fine_display = fine_done if fine_done is not None else "?"
    return {
        "fine": fine_done,
        "fine_total": fine_total,
        "coarse": coarse_done,
        "coarse_total": coarse_total,
        "has_gaps": has_gaps,
        "text": f"{coarse_display} coarse \u00b7 {fine_display} fine \u00b7 {'gaps' if has_gaps else 'no gaps'}",
        "detail": f"{coarse_display} of {coarse_total if coarse_total is not None else '?'} coarse windows and "
        f"{fine_display} of {fine_total if fine_total is not None else '?'} fine windows were analyzed.",
    }


def build_analysis_timeline_context(
    windows: Sequence[AnalysisWindow],
    *,
    analysis: AnalysisResult | None = None,
    track_segments: Sequence[TrackSpan] = (),
    set_profile: "SetProfile | None" = None,
) -> dict[str, object]:
    """Build the shared exhaustive timeline context for record and proposal presentations.

    ``analysis`` supplies the coverage chip's counters and ``track_segments`` the tracklist
    boundary ticks; both are OPTIONAL and their absence renders nothing rather than a claim.
    A caller that has neither (a proposal row with no tracklist) gets exactly the lanes the
    stored windows justify, which is the same honesty rule the rest of this module follows.

    ``set_profile`` is likewise OPTIONAL and governs only ``timeline_inspection["peak_sec"]``
    (see the comment at that assignment, phaze-0zx26) -- every other field here is unaffected.
    """
    ordered = sorted(windows, key=lambda window: (window.tier, window.window_index))
    finite_ends = [window.end_sec for window in ordered if math.isfinite(window.end_sec) and window.end_sec >= 0]
    total_sec = max(finite_ends, default=0.0)
    fine = [window for window in ordered if window.tier == "fine"]
    coarse = [window for window in ordered if window.tier == "coarse"]
    timeline_w = max(TIMELINE_W, max(len(fine), len(coarse), 1) * WINDOW_WIDTH_PX)
    spark = bpm_spark(fine, total_sec, timeline_w, TIMELINE_H)
    peak = energy_peak(coarse, total_sec, timeline_w, LANE_H)
    stored_peak_sec = set_profile.peak_sec if set_profile is not None else None
    return {
        "has_windows": bool(ordered),
        "has_coarse_windows": bool(coarse),
        "total_sec": total_sec,
        "timeline_w": timeline_w,
        "timeline_h": TIMELINE_H,
        "lane_h": LANE_H,
        "bpm_points": spark.points,
        "bpm_lo": spark.lo,
        "bpm_hi": spark.hi,
        "bpm_segments": bpm_segments(fine, total_sec, timeline_w, TIMELINE_H, spark.lo, spark.hi),
        "time_ticks": elapsed_time_ticks(total_sec),
        # phaze-x1qr3.10: ONE payload drives every inspection target. `peak_sec` is where the
        # page rests with nothing hovered -- the energy peak, because "where does this set peak"
        # is the question the lane exists to answer, and `None` (no measured energy anywhere) is
        # an honest absence the client renders as a rest at zero rather than as a peak claim at
        # zero. `segments` and `key_runs` are the two spans the readout, the shaded track and
        # the wheel ring are looked up in; both are empty for a file that has neither, and every
        # target that reads an empty list simply does not light.
        #
        # phaze-0zx26: THE STORED `set_profile.peak_sec` IS THE DEFINITION, not this function's
        # own `energy_peak(...)["sec"]`. The two used to disagree -- `energy_peak` argmaxes the
        # raw coarse windows while the stored value argmaxes the 64-point resampled arc
        # (`set_projection._peak_sec`), which averages neighbours and can land on a different
        # window for a short spike -- and the operator's phaze-x1qr3.12 blind check judges the
        # peak the STORED arc shows, so the rest position has to agree with it. `set_profile` is
        # `None` only for a file never analyzed to completion or predating the projection
        # backfill (record.py's own comment on the `session.get`); the live `energy_peak` argmax
        # is the fallback for exactly that case, never a silent second definition of the same
        # thing.
        "timeline_inspection": {
            "duration": total_sec,
            "peak_sec": stored_peak_sec if stored_peak_sec is not None else (peak["sec"] if peak else None),
            "windows": inspection_windows(ordered),
            "segments": inspection_segments(track_segments, total_sec),
            "key_runs": inspection_key_runs(fine),
        },
        "key_ribbons": ribbons(fine, "musical_key", total_sec, code_attr="camelot"),
        "mood_ribbons": ribbons(coarse, "mood", total_sec),
        "style_ribbons": ribbons(coarse, "style", total_sec),
        "energy_area": energy_area(coarse, total_sec, timeline_w, LANE_H),
        "energy_peak": peak,
        "mood_river": mood_river(coarse, total_sec, timeline_w, LANE_H),
        "mood_legend": mood_legend(),
        "tracklist_ticks": tracklist_ticks(track_segments, total_sec),
        "coverage_chip": coverage_chip(analysis),
    }
