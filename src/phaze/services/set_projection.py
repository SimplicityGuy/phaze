"""The set projection: fixed vocabulary, Camelot table, energy scalar, per-file profile.

``phaze-x1qr3.1`` (the SCHEMA half) declared :data:`MOOD_ORDER` and the nullable per-window
columns on ``analysis_window`` plus the per-file ``set_profile`` table -- "one projection,
then everything rides it". ``phaze-x1qr3.2`` (this bead) is the MATH half: pure functions
from stored rows to the projection, with no I/O anywhere in this module. Everything here
takes plain values or ``AnalysisWindow`` rows and returns floats, strings or dataclasses, so
the live write path (``phaze-x1qr3.3``), the backfill, and the tests below all share one
implementation.

Four things live here, in the order a caller would reach for them:

1. :func:`camelot_code` -- ``musical_key`` string (essentia's ``"<Note> <mode>"`` form,
   sharp OR flat) to a Camelot wheel position, or ``None`` for anything unrecognised.
2. :func:`positive_class_vector` -- one coarse window's raw ``features`` JSONB to the 11-d
   positive-class vector, positional in :data:`MOOD_ORDER`.
3. :data:`ENERGY_WEIGHTS` / :func:`energy` -- a coarse window's positive-class scores plus a
   caller-supplied BPM z-score to one energy scalar in ``[0, 1]``.
4. :func:`build_profile` -- a whole file's ``AnalysisWindow`` rows, ALREADY carrying their
   per-window ``energy`` / ``camelot`` / ``mood_scores`` (the "stored rows" the module
   docstring above promises), to the per-file :class:`SetProfileProjection`.

``MOOD_ORDER`` is declared here rather than on the model because it is one order shared by
several surfaces that must not disagree: the JSONB key set of
``AnalysisWindow.mood_scores``, the element order of ``SetProfile.mean_vector``, the mood
river's stacking order and hue assignment, the legend, and the tracklist's mood dots. A
per-surface ordering would render a different picture per page from identical data.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import math
from typing import TYPE_CHECKING, Any, Final

from phaze.services.analysis_derive import _positive_class_prediction  # the by-LABEL selector; see its docstring


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from phaze.models.analysis import AnalysisWindow


# The 11 model sets whose POSITIVE-class prediction the projection stores, in the single
# fixed archive-wide order every surface reads.
#
# WHY THESE NAMES: each entry is the ``name`` of a ``ModelSetConfig`` in
# ``services/analysis_models.MODEL_SETS``, which is also the key that model set's
# predictions occupy in a coarse window's ``analysis_window.features`` JSONB (shape
# ``features[set_name][variant] -> [{label, prediction}]``). Keeping the projection's key
# names identical to the stored feature keys means the backfill in phaze-x1qr3.3 is a
# direct read with no translation table to drift -- and it keeps the ``mood_`` prefix,
# which distinguishes the 7 binary mood classifiers from the 4 non-mood ones below.
# ``tests/shared/services/test_set_projection.py`` fails the build if the name SET ever
# stops matching ``MODEL_SETS``.
#
# WHY THIS ORDER: it is ``MODEL_SETS``' own declaration order, pinned here as a literal
# rather than derived from it. The order is a DISPLAY contract (the mood river's stacking
# and hue assignment, the legend, the tracklist mood dots) and it is baked into stored
# data (``SetProfile.mean_vector`` is positional), so reordering ``MODEL_SETS`` -- a purely
# internal analysis concern -- must not silently re-colour every rendered set or invalidate
# every stored vector. Deriving it would make exactly that possible. Changing this tuple is
# a ``projection_version`` bump and a re-backfill, never an edit in place.
#
# WHY POSITIVE-CLASS, not raw predictions: see
# ``services/analysis_derive._positive_class_prediction`` -- essentia orders a binary
# classifier's classes ALPHABETICALLY, so ``predictions[0]`` is the NEGATIVE class for
# ``mood_relaxed`` / ``mood_sad`` / ``mood_party``. The projection stores the positive class
# selected BY LABEL; a positional read here would systematically invert three of the seven
# moods, which is the defect that function exists to prevent.
MOOD_ORDER: tuple[str, ...] = (
    "mood_acoustic",
    "mood_electronic",
    "mood_aggressive",
    "mood_relaxed",
    "mood_happy",
    "mood_sad",
    "mood_party",
    "danceability",
    "gender",
    "tonality",
    "voice_instrumental",
)


# ---------------------------------------------------------------------------
# Camelot wheel
# ---------------------------------------------------------------------------

# The 24-entry Camelot wheel, keyed by essentia's ``"<Note> <mode>"`` ``musical_key`` form
# (``services/analysis.py`` writes ``f"{key} {scale}"``; the 8-char examples in
# ``schemas/agent_analysis.py`` -- "C# minor" / "G# major" -- are this exact shape). Every
# key is spelled with a SHARP here (never a flat); :data:`_ENHARMONIC_ALIASES` below is the
# other half of "every enharmonic spelling" -- essentia's flat spellings normalise to one of
# these 24 canonical strings before the lookup, so this table itself never grows past 24.
CAMELOT_TABLE: Final[dict[str, str]] = {
    "A minor": "8A",
    "C major": "8B",
    "E minor": "9A",
    "G major": "9B",
    "B minor": "10A",
    "D major": "10B",
    "F# minor": "11A",
    "A major": "11B",
    "C# minor": "12A",
    "E major": "12B",
    "G# minor": "1A",
    "B major": "1B",
    "D# minor": "2A",
    "F# major": "2B",
    "A# minor": "3A",
    "C# major": "3B",
    "F minor": "4A",
    "G# major": "4B",
    "C minor": "5A",
    "D# major": "5B",
    "G minor": "6A",
    "A# major": "6B",
    "D minor": "7A",
    "F major": "7B",
}

# essentia's flat spellings for the 5 pitch classes that are ambiguous (the sharp/flat pairs
# named in the bead: G#/Ab, D#/Eb, A#/Bb, C#/Db, F#/Gb), one alias per pitch class per mode --
# 5 x 2 = 10 entries, each resolving to one of the 10 sharp-spelled keys above that use one of
# those 5 pitch classes. A key that needs no alias (e.g. "A minor") is already canonical.
_ENHARMONIC_ALIASES: Final[dict[str, str]] = {
    "Ab minor": "G# minor",
    "Ab major": "G# major",
    "Eb minor": "D# minor",
    "Eb major": "D# major",
    "Bb minor": "A# minor",
    "Bb major": "A# major",
    "Db minor": "C# minor",
    "Db major": "C# major",
    "Gb minor": "F# minor",
    "Gb major": "F# major",
}


def camelot_code(musical_key: str | None) -> str | None:
    """Map an essentia ``musical_key`` string to its Camelot wheel position, or ``None``.

    Never raises: an empty/``None`` input, or any string that is not one of the 24 canonical
    keys or one of the 10 enharmonic aliases, returns ``None`` -- exactly the same "not yet
    projected" gap every other nullable projection column renders, never an exception a
    caller has to guard against.
    """
    if not musical_key:
        return None
    canonical = _ENHARMONIC_ALIASES.get(musical_key, musical_key)
    return CAMELOT_TABLE.get(canonical)


# ``CAMELOT_TABLE`` is a bijection (24 distinct key names onto 24 distinct wheel positions), so
# it inverts without loss. Inverted ONCE here, at import time, rather than re-derived per call or
# per surface -- ``services/track_segments.py`` and the record page's facts list both read this.
_CAMELOT_TO_KEY: Final[dict[str, str]] = {code: key for key, code in CAMELOT_TABLE.items()}


def key_name_for_camelot(code: str | None) -> str | None:
    """The canonical key name for a Camelot code ("8A" -> "A minor"), or ``None``.

    The name a code maps back to is always that code's own SHARP-spelled canonical name, never
    one of its enharmonic twins: essentia's flat spellings normalise onto a canonical string on
    the way in (:func:`camelot_code`), so the inverse has exactly one answer per code.
    """
    if not code:
        return None
    return _CAMELOT_TO_KEY.get(code)


def camelot_number(code: str | None) -> int | None:
    """The numeric 1-12 half of a Camelot code (its wheel position, ignoring A/B mode)."""
    if not code or len(code) < 2:
        return None
    try:
        return int(code[:-1])
    except ValueError:
        return None


def wheel_adjacent(a: str, b: str) -> bool:
    """Two Camelot codes are wheel-adjacent for harmonic mixing purposes when either:

    - they share a letter (mode) and their numbers are a semitone step apart on the 12-hour
      wheel (``(na - nb) % 12`` is 1 or 11, i.e. +/-1 with wraparound); or
    - they share a number and differ in letter (the relative major/minor pair).

    Public since ``phaze-x1qr3.8``: the harmonic-journey wheel colours each EDGE by this same
    predicate that :func:`harmonic_discipline` counts with, so an edge drawn as a jump and the
    percentage in the caption beside it can never disagree about what "adjacent" means.

    A code either side cannot be parsed as ``<1-12><A|B>`` is not adjacent to anything -- the
    honest answer for a value outside the wheel, and never a raised ``ValueError`` on a render
    path.
    """
    na, nb = camelot_number(a), camelot_number(b)
    if na is None or nb is None:
        return False
    la, lb = a[-1], b[-1]
    if la == lb:
        return (na - nb) % 12 in (1, 11)
    return na == nb


# ---------------------------------------------------------------------------
# Positive-class vector
# ---------------------------------------------------------------------------


def positive_class_vector(features: Mapping[str, Any]) -> tuple[float | None, ...]:
    """One coarse window's raw ``features`` JSONB to the 11-d positive-class vector.

    For each name in :data:`MOOD_ORDER`, averages the POSITIVE-class prediction -- selected
    by label via :func:`phaze.services.analysis_derive._positive_class_prediction`, never by
    list position -- across whatever variants are present. A name absent from ``features``,
    or present with no variants, is ``None`` at its position rather than a manufactured 0.0:
    a missing model set is a gap, not a measurement of "no signal".

    Reusing ``_positive_class_prediction`` here rather than re-deriving the by-label
    selection is deliberate: that function is the single place the alphabetical-ordering
    trap (``mood_relaxed`` / ``mood_sad`` / ``mood_party`` put the negative class first) is
    already fixed and tested, and a second implementation is a second place it could drift
    back in.
    """
    values: list[float | None] = []
    for name in MOOD_ORDER:
        set_data = features.get(name)
        if not set_data:
            values.append(None)
            continue
        scores = [_positive_class_prediction(predictions) for predictions in set_data.values() if predictions]
        values.append(sum(scores) / len(scores) if scores else None)
    return tuple(values)


# ---------------------------------------------------------------------------
# Energy scalar
# ---------------------------------------------------------------------------

# THE single place the energy weights live (acceptance: a test greps this module for
# literal weight floats outside this table). ``bpm_z`` is not a ``MOOD_ORDER`` name -- it is
# the caller-supplied file-local BPM z-score, keyed here rather than passed as a second
# weight argument so the whole linear combination is one table to read, and one
# ``projection_version`` bump if it ever changes.
#
# Signs: danceability, party and aggressive raise energy; relaxed and sad lower it; a
# faster-than-the-file's-own-average BPM (positive z) raises it. Magnitudes here are the
# implementer's own pick, not yet reviewed by the operator -- ``phaze-x1qr3.12``'s blind
# check over 20 real sets is what settles that with the operator's own input and bumps
# ``projection_version`` if the values change; nothing above should be read as though that
# review already happened.
ENERGY_WEIGHTS: Final[dict[str, float]] = {
    "danceability": 0.30,
    "mood_party": 0.30,
    "mood_aggressive": 0.15,
    "mood_relaxed": -0.20,
    "mood_sad": -0.15,
    "bpm_z": 0.10,
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def energy(scores: Mapping[str, float | None], bpm_z: float) -> float:
    """The energy scalar for one coarse window, clamped to ``[0, 1]``.

    ``scores`` is a positive-class mapping keyed by :data:`MOOD_ORDER` names (typically an
    ``AnalysisWindow.mood_scores`` dict, or a :func:`positive_class_vector` result zipped
    back onto ``MOOD_ORDER``) -- a missing or ``None`` entry contributes 0.0 rather than
    raising, so a partial vector still yields a defined (if less informed) energy. ``bpm_z``
    is the file-local BPM z-score for this window's time range; a file with no fine BPM data
    passes 0.0 (see :func:`build_profile`'s ``sources`` field for how that is recorded).
    """
    total = 0.0
    for name, weight in ENERGY_WEIGHTS.items():
        if name == "bpm_z":
            continue
        value = scores.get(name)
        if value is not None:
            total += weight * value
    total += ENERGY_WEIGHTS["bpm_z"] * bpm_z
    return _clamp01(total)


# ---------------------------------------------------------------------------
# Per-file profile
# ---------------------------------------------------------------------------

ARC_POINTS: Final[int] = 64
# A genuine coverage hole is many multiples of a normal ~180s coarse-window step; anything
# past a 1-second slop between one window's end and the next's start is real missing
# coverage, not float rounding on adjacent windows. An int, not a float, so it needs no entry
# in ENERGY_WEIGHTS and adds no ambiguity to the weight-literal grep.
GAP_TOLERANCE_SEC: Final[int] = 1


@dataclass(frozen=True)
class SetProfileProjection:
    """The pure-function result :func:`build_profile` returns -- no ``file_id``, no session.

    Positional field-for-field with ``SetProfile`` (``mean_vector``, ``arc``, ``glyph``,
    ``camelot_modal``, ``harmonic_discipline``, ``peak_sec``) EXCEPT ``sources``, which has
    no column on that model: it is provenance for the writer/backfill to log or fold into
    its own bookkeeping, not persisted data. ``phaze-x1qr3.3`` maps every other field
    directly onto a ``SetProfile(file_id=..., projection_version=..., **the rest)``.

    ``glyph`` is a ``list``, not a ``dict`` -- one cell per COARSE window, in order (see
    :func:`_glyph_cells`) -- which JSONB stores as a JSON array with no Python-side "object vs
    array" distinction to violate. ``SetProfile.glyph``'s annotation is ``list | dict | None``
    (widened by phaze-x1qr3.3) precisely so this field's actual shape has somewhere honest to
    land under strict mypy, rather than a dict wrapper invented to force a type that never
    matched what this dataclass produces.
    """

    mean_vector: list[float] | None
    arc: list[float] | None
    glyph: list[dict[str, int | float | None]] | None
    camelot_modal: str | None
    harmonic_discipline: float | None
    peak_sec: float | None
    sources: dict[str, str]


def _mean_vector(coarse_windows: Sequence[AnalysisWindow]) -> list[float] | None:
    """Simple (unweighted) mean of already-stored ``mood_scores`` vectors, positional in MOOD_ORDER.

    ``None`` when no coarse window carries ``mood_scores`` at all (a fine-only file, or one
    not yet projected) -- the honest gap, never a manufactured zero vector. A ``MOOD_ORDER``
    position that no window supplied a value for is ``NaN`` at that position rather than
    silently dropped, the same "gap stays a gap" convention the arc uses.

    A window whose ``mood_scores`` is a NON-EMPTY dict with every value ``None`` (a coarse window
    the writer projected with no usable ``features``) counts as carrying NO data here, the same as
    an absent/empty dict -- ``bool({"k": None, ...})`` is ``True`` in Python, so the naive
    ``if not window.mood_scores`` truthiness check alone would flip ``seen_any`` for a window that
    contributed nothing, turning a file with NO real coarse data into an 11-NaN vector instead of
    the honest ``None`` (phaze-x1qr3.3 review finding 3: caught because the writer briefly produced
    exactly this all-``None`` dict for a featureless coarse window).
    """
    sums = [0.0] * len(MOOD_ORDER)
    counts = [0] * len(MOOD_ORDER)
    seen_any = False
    for window in coarse_windows:
        if not window.mood_scores:
            continue
        values = [window.mood_scores.get(name) for name in MOOD_ORDER]
        if all(value is None for value in values):
            continue
        seen_any = True
        for i, value in enumerate(values):
            if value is not None:
                sums[i] += value
                counts[i] += 1
    if not seen_any:
        return None
    return [sums[i] / counts[i] if counts[i] else math.nan for i in range(len(MOOD_ORDER))]


def _linear_interp(knots: Sequence[tuple[float, float]], t: float) -> float:
    """Piecewise-linear value at ``t`` over sorted ``(elapsed_sec, value)`` knots.

    Clamps (flat) before the first knot and after the last -- those are inside a window's own
    covered span, just past its midpoint, not a coverage gap. Callers only reach this for
    ``t`` already known to fall within one run's covered bounds.
    """
    if t <= knots[0][0]:
        return knots[0][1]
    if t >= knots[-1][0]:
        return knots[-1][1]
    for (t0, v0), (t1, v1) in itertools.pairwise(knots):
        if t0 <= t <= t1:
            if t1 == t0:
                return v0
            fraction = (t - t0) / (t1 - t0)
            return v0 + fraction * (v1 - v0)
    return knots[-1][1]  # pragma: no cover -- unreachable: the bounds checks above are exhaustive


def _resample_energy_arc(coarse_windows: Sequence[AnalysisWindow], total_sec: float, points: int = ARC_POINTS) -> list[float] | None:
    """Linear resample of already-stored coarse-window ``energy`` to a fixed ``points``-length series.

    Windows are grouped into maximal CONTIGUOUS runs (adjacent starts/ends within
    :data:`GAP_TOLERANCE_SEC`); each run is resampled by linear interpolation between its
    windows' midpoints, clamped flat at its own edges. A sample time that falls in a genuine
    gap BETWEEN runs -- missing coarse windows, not just missing ``energy`` values -- is
    ``NaN``, never bridged across the hole by interpolating between the runs on either side.
    ``None`` (not a list of NaN) only when there is no usable coarse energy at all.
    """
    usable = sorted(
        (window.start_sec, window.end_sec, window.energy)
        for window in coarse_windows
        if window.energy is not None and math.isfinite(window.start_sec) and math.isfinite(window.end_sec) and window.end_sec > window.start_sec
    )
    if not usable or not math.isfinite(total_sec) or total_sec <= 0:
        return None

    runs: list[list[tuple[float, float, float]]] = []
    for start, end, value in usable:
        if runs and start - runs[-1][-1][1] <= GAP_TOLERANCE_SEC:
            runs[-1].append((start, end, value))
        else:
            runs.append([(start, end, value)])
    run_bounds = [(run[0][0], run[-1][1], [((s + e) / 2, value) for s, e, value in run]) for run in runs]

    def value_at(t: float) -> float:
        for lo, hi, knots in run_bounds:
            if lo <= t <= hi:
                return _linear_interp(knots, t)
        return math.nan

    if points <= 1:
        return [value_at(0.0)]
    return [value_at((i / (points - 1)) * total_sec) for i in range(points)]


def _peak_sec(arc: Sequence[float] | None, total_sec: float, points: int = ARC_POINTS) -> float | None:
    """Elapsed seconds of the arc's maximum sample -- where the page rests with nothing hovered."""
    if not arc or not math.isfinite(total_sec) or total_sec <= 0:
        return None
    best_index: int | None = None
    best_value = -math.inf
    for i, value in enumerate(arc):
        if value is not None and math.isfinite(value) and value > best_value:
            best_value = value
            best_index = i
    if best_index is None:
        return None
    denominator = (points - 1) if points > 1 else 1
    return (best_index / denominator) * total_sec


def modal_camelot(windows: Sequence[AnalysisWindow]) -> str | None:
    """Duration-weighted modal ``camelot`` code across ``windows`` (stable on ties).

    Public because ``phaze-x1qr3.6`` asks the same question of a different SLICE of windows:
    the per-file profile weights every fine window of the file, a track segment weights the
    fine windows whose midpoint falls inside that track. One implementation, so the key a
    tracklist row shows and the key the file's profile stores can never disagree by method.
    """
    weights: dict[str, float] = {}
    for window in windows:
        if window.camelot:
            weights[window.camelot] = weights.get(window.camelot, 0.0) + max(0.0, window.end_sec - window.start_sec)
    if not weights:
        return None
    return max(weights, key=lambda code: weights[code])


def _glyph_cells(coarse_windows: Sequence[AnalysisWindow], fine_windows: Sequence[AnalysisWindow]) -> list[dict[str, int | float | None]] | None:
    """One ``{camelot_number, energy}`` cell per coarse window, ordered by ``window_index``.

    ``camelot`` lives only on FINE-tier rows (it is derived from ``musical_key``, a fine-tier
    field), so a coarse window's cell borrows the duration-weighted modal camelot of the fine
    windows whose time range overlaps it -- the same cross-tier join the epic's glyph macro
    needs to pair a colour (Camelot number) with a lightness (energy) per cell.
    """
    if not coarse_windows:
        return None
    cells: list[dict[str, int | float | None]] = []
    for window in sorted(coarse_windows, key=lambda w: (w.window_index, w.start_sec)):
        overlapping = [f for f in fine_windows if f.camelot and f.start_sec < window.end_sec and f.end_sec > window.start_sec]
        cells.append({"camelot_number": camelot_number(modal_camelot(overlapping)), "energy": window.energy})
    return cells


@dataclass(frozen=True)
class KeyRun:
    """One surviving key run of the flicker-filtered sequence, with the time it occupied.

    ``phaze-x1qr3.8``. :func:`harmonic_discipline` only ever needed the CODES; the harmonic
    journey wheel needs the same runs plus when each one happened and how long it lasted, so
    the filter now returns runs and the discipline figure is derived from them. One filter,
    two readers -- the wheel's node count and the discipline percentage it captions can never
    be computed from two different filtered sequences.

    ``window_count`` is the number of fine windows the run absorbed (including any blip
    windows dropped INSIDE it by the merge below, which are noise within a continuous run
    rather than coverage of their own). ``dwell_sec`` is the run's wall extent, which is what
    sizes its node -- a run of two 30 s windows and a run of forty read very differently and
    must not draw the same dot.
    """

    code: str
    start_sec: float
    end_sec: float
    window_count: int

    @property
    def dwell_sec(self) -> float:
        """Non-negative wall extent of the run. Zero (never negative) on degenerate bounds."""
        return max(0.0, self.end_sec - self.start_sec)


def placeable_key_runs(runs: Sequence[KeyRun]) -> list[tuple[KeyRun, int]]:
    """Pair each run with its wheel position, dropping any code the wheel cannot place.

    ``analysis_window.camelot`` is constrained to the 24 canonical codes, so in practice every
    run places; a value that somehow escaped that is dropped from the PICTURE rather than
    rendered at a made-up position, and the caption's counts are of what was drawn.

    It lives HERE, beside the filter that produces the runs, rather than in
    ``harmonic_journey`` where it was written (``phaze-x1qr3.8``), because two surfaces now
    number key runs and they must number them identically: ``harmonic_journey._nodes``
    enumerates this list to stamp ``data-node-index`` on each drawn node, and
    ``analysis_timeline.inspection_key_runs`` enumerates it to tell the client which node to
    ring. A re-implementation on either side would agree until the first unplaceable code and
    then silently shift every later index by one -- pointing the cursor ring at the wrong key
    for the rest of the set, with every index still resolving to a real node.
    ``analysis_timeline`` cannot import ``harmonic_journey`` (that module imports
    ``format_elapsed_time`` back out of it), so a shared home was required as well as tidier.
    """
    placed: list[tuple[KeyRun, int]] = []
    for run in runs:
        number = camelot_number(run.code)
        if number is not None and 1 <= number <= 12:
            placed.append((run, number))
    return placed


def flicker_filtered_key_runs(fine_windows: Sequence[AnalysisWindow]) -> list[KeyRun]:
    """Run-length encode ``camelot`` over ordered fine windows, drop 1-window runs, re-merge.

    "A key run shorter than two fine windows is not a transition": a single-window blip
    (``[..., "8A", "3A", "8A", ...]``) is dropped as noise rather than counted as two
    transitions, so the surrounding run reads as continuous. Dropping it can bring two equal
    survivors back together (``8A`` on both sides of the dropped ``3A``), which the final
    merge pass joins into ONE run spanning the blip -- or it can bring two DIFFERENT survivors
    together (``8A`` .. dropped .. ``9A``), which correctly becomes one direct transition.

    Windows with no ``camelot`` are absent from the sequence entirely; they neither break a
    run nor extend one, because "no key was resolved here" is a gap in measurement rather than
    a measured key change.
    """
    ordered = sorted((w for w in fine_windows if w.camelot), key=lambda w: (w.window_index, w.start_sec))
    encoded: list[list[Any]] = []
    for window in ordered:
        if encoded and encoded[-1][0] == window.camelot:
            encoded[-1][2] = max(float(encoded[-1][2]), window.end_sec)
            encoded[-1][3] = int(encoded[-1][3]) + 1
        else:
            encoded.append([window.camelot, window.start_sec, window.end_sec, 1])
    merged: list[list[Any]] = []
    for code, start, end, count in (run for run in encoded if int(run[3]) >= 2):
        if merged and merged[-1][0] == code:
            merged[-1][2] = max(float(merged[-1][2]), float(end))
            merged[-1][3] = int(merged[-1][3]) + int(count)
        else:
            merged.append([code, start, end, count])
    return [KeyRun(code=str(code), start_sec=float(start), end_sec=float(end), window_count=int(count)) for code, start, end, count in merged]


def harmonic_discipline(fine_windows: Sequence[AnalysisWindow]) -> float | None:
    """Share of key transitions that are wheel-adjacent, after the flicker filter.

    ``None`` when there is no usable ``camelot`` sequence at all. ``1.0`` when the filtered
    sequence never actually changes key (zero transitions is the trivial "fully disciplined"
    case -- there is nothing non-adjacent happening). Otherwise the fraction of the filtered
    transitions that are wheel-adjacent (see :func:`wheel_adjacent`).

    Derived from :func:`flicker_filtered_key_runs` rather than from a filter of its own, so
    this number and the harmonic-journey wheel that captions it always describe the same
    sequence of runs.
    """
    survivors = [run.code for run in flicker_filtered_key_runs(fine_windows)]
    if not survivors:
        return None
    transitions = list(itertools.pairwise(survivors))
    if not transitions:
        return 1.0
    adjacent = sum(1 for a, b in transitions if wheel_adjacent(a, b))
    return adjacent / len(transitions)


def _bpm_source(fine_windows: Sequence[AnalysisWindow]) -> str:
    """Return "fine" when any fine window carries a real BPM, else "none" (z-scores default to 0)."""
    return "fine" if any(w.bpm is not None for w in fine_windows) else "none"


def build_profile(windows: Sequence[AnalysisWindow]) -> SetProfileProjection:
    """A whole file's ``AnalysisWindow`` rows to the per-file :class:`SetProfileProjection`.

    Pure aggregation over STORED rows: every window is assumed to already carry its own
    per-window projection (``energy`` on coarse rows, ``camelot`` on fine rows,
    ``mood_scores`` on coarse rows) -- computed via :func:`camelot_code`,
    :func:`positive_class_vector` and :func:`energy` by the caller (the live write path or
    the backfill) before this runs. This function does no per-window computation of its own
    and touches no database.

    ``sources`` records file-level provenance the fields above cannot otherwise recover:
    today just ``{"bpm": "fine" | "none"}`` (see :func:`_bpm_source`), since a file with no
    fine BPM data at all fed ``bpm_z = 0`` into every window's already-computed ``energy``.
    """
    fine = [w for w in windows if w.tier == "fine"]
    coarse = [w for w in windows if w.tier == "coarse"]
    total_sec = max((w.end_sec for w in windows if math.isfinite(w.end_sec)), default=0.0)

    arc = _resample_energy_arc(coarse, total_sec)
    return SetProfileProjection(
        mean_vector=_mean_vector(coarse),
        arc=arc,
        glyph=_glyph_cells(coarse, fine),
        camelot_modal=modal_camelot(fine),
        harmonic_discipline=harmonic_discipline(fine),
        peak_sec=_peak_sec(arc, total_sec),
        sources={"bpm": _bpm_source(fine)},
    )


__all__ = [
    "ARC_POINTS",
    "CAMELOT_TABLE",
    "ENERGY_WEIGHTS",
    "GAP_TOLERANCE_SEC",
    "MOOD_ORDER",
    "KeyRun",
    "SetProfileProjection",
    "build_profile",
    "camelot_code",
    "camelot_number",
    "energy",
    "flicker_filtered_key_runs",
    "harmonic_discipline",
    "key_name_for_camelot",
    "modal_camelot",
    "placeable_key_runs",
    "positive_class_vector",
    "wheel_adjacent",
]
