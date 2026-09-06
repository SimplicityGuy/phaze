"""``services/set_projection`` -- vocabulary (phaze-x1qr3.1) + projection math (phaze-x1qr3.2).

phaze-x1qr3.1 declared :data:`MOOD_ORDER`; phaze-x1qr3.2 adds the Camelot table, the 11-d
positive-class vector, the energy scalar and the per-file profile to the same module. What
is asserted here is every acceptance criterion on phaze-x1qr3.2, each split into the
independent way it can break, plus the module's own "MOOD_ORDER" vocabulary tests it
inherited from phaze-x1qr3.1.
"""

from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisWindow
from phaze.services import set_projection
from phaze.services.analysis_models import MODEL_SETS
from phaze.services.set_projection import (
    ARC_POINTS,
    CAMELOT_TABLE,
    ENERGY_WEIGHTS,
    MOOD_ORDER,
    build_profile,
    camelot_code,
    energy,
    harmonic_discipline,
    positive_class_vector,
)
from tests.analyze._real_result import real_analysis_result


if TYPE_CHECKING:
    from collections.abc import Sequence


# ---------------------------------------------------------------------------
# phaze-x1qr3.1: MOOD_ORDER, the fixed archive-wide vocabulary
# ---------------------------------------------------------------------------


def test_mood_order_is_the_eleven_fixed_names_in_a_fixed_order() -> None:
    """The exact tuple is pinned, so a reorder or a rename is a deliberate edit to this test.

    Order is not cosmetic here. It is the stacking and hue assignment of the mood river, the order
    of its legend and of the tracklist's mood dots, and -- because ``SetProfile.mean_vector`` is
    positional -- it is baked into every stored projection row. Changing it re-colours every
    rendered set and invalidates every stored vector, so it is a ``projection_version`` bump and a
    re-backfill, never an edit in place.
    """
    assert MOOD_ORDER == (
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
    assert len(MOOD_ORDER) == 11
    assert len(set(MOOD_ORDER)) == len(MOOD_ORDER), "a duplicate name would silently shorten the vector"


def test_mood_order_names_exactly_the_model_sets_that_produce_them() -> None:
    """Every name is a real ``MODEL_SETS`` entry, and no model set is left out of the projection.

    This is the half a pinned literal cannot catch on its own. The names double as the keys those
    model sets occupy in ``analysis_window.features``, so a rename or an added/removed model set
    on the analysis side would leave the projection reading a key that is no longer written -- and
    a missing-key read yields no error, just a silently absent score. Compared as SETS, not
    sequences, precisely because the ORDER above must be free to differ from ``MODEL_SETS``'
    declaration order without failing.
    """
    assert set(MOOD_ORDER) == {model_set.name for model_set in MODEL_SETS}


def test_seven_of_the_eleven_are_the_binary_mood_classifiers() -> None:
    """The ``mood_`` prefix is kept, so the 7 moods stay distinguishable from the 4 non-mood sets.

    The mood river stacks the 7; the other 4 (danceability, gender, tonality, voice_instrumental)
    are facts and similarity terms, never river bands. Keeping the prefix in the stored key means
    that split needs no second list to fall out of sync with this one.
    """
    moods = [name for name in MOOD_ORDER if name.startswith("mood_")]
    assert len(moods) == 7
    assert len(MOOD_ORDER) - len(moods) == 4
    # The 7 lead, so the river's bands are a prefix of the vector rather than a scatter through it.
    assert MOOD_ORDER[:7] == tuple(moods)


# ---------------------------------------------------------------------------
# phaze-x1qr3.2: the Camelot table
# ---------------------------------------------------------------------------

# The standard 24-position Camelot wheel, spelled out independently of ``CAMELOT_TABLE`` so this
# test is a real cross-check rather than importing the same literal it is meant to verify.
_EXPECTED_WHEEL: dict[str, str] = {
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

# essentia's flat spellings for the 5 enharmonic-ambiguous pitch classes the bead names
# (G#/Ab, D#/Eb, A#/Bb, C#/Db, F#/Gb), one alias per pitch class per mode.
_ENHARMONIC_SPELLINGS: dict[str, str] = {
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


def test_camelot_table_covers_all_24_keys() -> None:
    """Every one of the 12 major + 12 minor keys resolves to its documented Camelot position."""
    assert len(CAMELOT_TABLE) == 24
    for musical_key, expected_code in _EXPECTED_WHEEL.items():
        assert camelot_code(musical_key) == expected_code


def test_camelot_table_accepts_every_enharmonic_spelling() -> None:
    """A flat spelling resolves to the exact same code as its sharp-spelled canonical form."""
    for flat_key, canonical_key in _ENHARMONIC_SPELLINGS.items():
        assert camelot_code(flat_key) == camelot_code(canonical_key) == _EXPECTED_WHEEL[canonical_key]


@pytest.mark.parametrize("musical_key", [None, "", "Q major", "A minor ", " A minor", "a minor", "C blorp", "12A"])
def test_camelot_code_returns_none_never_raises_for_unknown_keys(musical_key: str | None) -> None:
    assert camelot_code(musical_key) is None


# ---------------------------------------------------------------------------
# phaze-x1qr3.2: positive_class_vector, against REAL stored feature dicts
# ---------------------------------------------------------------------------


def _coarse_feature_dicts() -> list[dict[str, object]]:
    """Every coarse window's real ``features`` dict from the A7 real-payload fixture."""
    result = real_analysis_result()
    return [window["features"] for window in result["windows"] if window.get("tier") == "coarse"]


def _positive_by_label(predictions: list[dict[str, object]]) -> float:
    """An independent (test-local) by-label positive-class pick, so this does not just re-run

    the production selector against itself.
    """
    for entry in predictions:
        label = str(entry["label"])
        if not label.startswith(("non_", "not_")):
            return float(entry["prediction"])  # type: ignore[arg-type]
    return float(predictions[0]["prediction"])  # type: ignore[arg-type]


@pytest.mark.parametrize("mood_name", ["mood_relaxed", "mood_sad", "mood_party"])
def test_positive_class_vector_selects_by_label_never_by_index_zero(mood_name: str) -> None:
    """The three moods essentia orders negative-first, checked against REAL stored payloads.

    essentia orders a binary classifier's classes alphabetically, so ``predictions[0]`` is the
    NEGATIVE class for each of these three model sets (their negative label -- "non_relaxed",
    "non_sad", "non_party" -- sorts before the positive one). Averaging index 0 across variants
    therefore differs materially from averaging the POSITIVE class by label on every real coarse
    window in the fixture -- this test fails loudly if the production function ever regresses to
    a positional read.
    """
    index = MOOD_ORDER.index(mood_name)
    for features in _coarse_feature_dicts():
        vector = positive_class_vector(features)
        expected_positive = sum(_positive_by_label(preds) for preds in features[mood_name].values()) / len(features[mood_name])
        expected_index_zero = sum(preds[0]["prediction"] for preds in features[mood_name].values()) / len(features[mood_name])
        assert vector[index] == pytest.approx(expected_positive)
        assert vector[index] != pytest.approx(expected_index_zero), "regressed to a positional (index-0) read"


def test_positive_class_vector_is_positional_in_mood_order_and_full_on_real_data() -> None:
    """Every one of the 11 positions is populated (no unexpected gap) on a real coarse window."""
    features = _coarse_feature_dicts()[0]
    vector = positive_class_vector(features)
    assert len(vector) == len(MOOD_ORDER) == 11
    assert all(value is not None for value in vector)
    assert all(0.0 <= value <= 1.0 for value in vector)  # type: ignore[operator]


def test_positive_class_vector_reports_a_gap_for_an_absent_model_set() -> None:
    """A missing model set is ``None`` at its position, never a manufactured 0.0."""
    features = {k: v for k, v in _coarse_feature_dicts()[0].items() if k != "mood_sad"}
    vector = positive_class_vector(features)
    assert vector[MOOD_ORDER.index("mood_sad")] is None
    assert vector[MOOD_ORDER.index("mood_acoustic")] is not None


# ---------------------------------------------------------------------------
# phaze-x1qr3.2: the energy scalar
# ---------------------------------------------------------------------------


def test_energy_is_monotone_increasing_in_danceability_and_party() -> None:
    values = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    danceability_series = [energy({"danceability": v}, bpm_z=0.0) for v in values]
    party_series = [energy({"mood_party": v}, bpm_z=0.0) for v in values]
    assert danceability_series == sorted(danceability_series)
    assert party_series == sorted(party_series)
    assert danceability_series[0] < danceability_series[-1]
    assert party_series[0] < party_series[-1]


def test_energy_is_monotone_decreasing_in_relaxed_and_sad() -> None:
    values = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    relaxed_series = [energy({"danceability": 0.5, "mood_relaxed": v}, bpm_z=0.0) for v in values]
    sad_series = [energy({"danceability": 0.5, "mood_sad": v}, bpm_z=0.0) for v in values]
    assert relaxed_series == sorted(relaxed_series, reverse=True)
    assert sad_series == sorted(sad_series, reverse=True)
    assert relaxed_series[0] > relaxed_series[-1]
    assert sad_series[0] > sad_series[-1]


def test_energy_is_clamped_to_the_unit_interval() -> None:
    assert energy({"danceability": 1.0, "mood_party": 1.0, "mood_aggressive": 1.0}, bpm_z=1000.0) == 1.0
    assert energy({"mood_relaxed": 1.0, "mood_sad": 1.0}, bpm_z=-1000.0) == 0.0


def test_energy_treats_a_missing_score_as_no_contribution_rather_than_raising() -> None:
    assert energy({}, bpm_z=0.0) == 0.0
    assert energy({"danceability": None}, bpm_z=0.0) == 0.0


def test_energy_weights_live_in_exactly_one_place_in_the_module() -> None:
    """A test greps the module for literal weight floats living outside ``ENERGY_WEIGHTS``.

    Parses the module's own source with ``ast`` (a real structural check, not a text grep that a
    reformat could dodge) and asserts every float literal in the file is either inside the
    ``ENERGY_WEIGHTS`` assignment's line span or one of the two bare mathematical bounds
    (``0.0`` / ``1.0``) every clamp-to-``[0, 1]`` needs -- neither of which is a tuning weight.
    """
    source_path = Path(inspect.getfile(set_projection))
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))

    def _is_energy_weights_assignment(node: ast.AST) -> bool:
        if isinstance(node, ast.Assign):
            return any(isinstance(target, ast.Name) and target.id == "ENERGY_WEIGHTS" for target in node.targets)
        if isinstance(node, ast.AnnAssign):
            return isinstance(node.target, ast.Name) and node.target.id == "ENERGY_WEIGHTS"
        return False

    weights_node = next(node for node in ast.walk(tree) if _is_energy_weights_assignment(node))
    weights_lines = range(weights_node.lineno, (weights_node.end_lineno or weights_node.lineno) + 1)
    allowed_outside_table = {0.0, 1.0}

    offenders = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, float)
        and node.lineno not in weights_lines
        and node.value not in allowed_outside_table
    ]
    assert offenders == [], f"literal weight-shaped float(s) found outside ENERGY_WEIGHTS: {offenders}"

    # And the table itself is exactly the weights the energy() formula reads -- a name declared
    # in ENERGY_WEIGHTS but never consulted (or vice versa) would be a silent formula/table split.
    assert set(ENERGY_WEIGHTS) == {"danceability", "mood_party", "mood_aggressive", "mood_relaxed", "mood_sad", "bpm_z"}


# ---------------------------------------------------------------------------
# phaze-x1qr3.2: build_profile -- the arc resample
# ---------------------------------------------------------------------------


def _window(
    index: int,
    start: float,
    end: float,
    *,
    tier: str = "coarse",
    energy_value: float | None = None,
    camelot: str | None = None,
    mood_scores: dict[str, float] | None = None,
    bpm: float | None = None,
) -> AnalysisWindow:
    return AnalysisWindow(
        file_id=uuid.uuid4(),
        tier=tier,
        window_index=index,
        start_sec=start,
        end_sec=end,
        bpm=bpm,
        energy=energy_value,
        camelot=camelot,
        mood_scores=mood_scores,
    )


def _contiguous_coarse_windows(count: int, *, step: float = 180.0) -> list[AnalysisWindow]:
    """``count`` back-to-back coarse windows with linearly increasing energy in ``[0, 1]``."""
    return [_window(i, i * step, (i + 1) * step, energy_value=(i / (count - 1) if count > 1 else 0.0)) for i in range(count)]


def test_arc_resample_produces_64_points_on_a_44_window_file() -> None:
    windows = _contiguous_coarse_windows(44)
    profile = build_profile(windows)
    assert profile.arc is not None
    assert len(profile.arc) == ARC_POINTS == 64
    assert all(math.isfinite(v) for v in profile.arc)
    # Energy rises monotonically across the file; the resample must preserve that shape.
    assert profile.arc == sorted(profile.arc)
    assert profile.arc[0] == pytest.approx(0.0, abs=1e-9)
    assert profile.arc[-1] == pytest.approx(1.0, abs=1e-9)


def test_arc_resample_produces_64_points_on_a_3_window_file_without_crashing() -> None:
    windows = _contiguous_coarse_windows(3)
    profile = build_profile(windows)
    assert profile.arc is not None
    assert len(profile.arc) == 64
    assert all(math.isfinite(v) for v in profile.arc)


def test_arc_resample_leaves_a_coverage_gap_as_nan_rather_than_bridging_it() -> None:
    """Two contiguous runs with a large real hole between them: the hole samples stay NaN.

    A run from 0-360s (2 windows) and a second run from 1000-1360s (2 windows), with nothing
    projected for the ~640s between them -- unlike the trailing-window-length variation that is
    normal (D-07), this is a genuine missing-coverage hole. Samples whose elapsed time falls in
    that hole must read NaN, never a value interpolated straight across it from the two runs on
    either side.
    """
    first_run = [_window(0, 0.0, 180.0, energy_value=0.2), _window(1, 180.0, 360.0, energy_value=0.3)]
    second_run = [_window(2, 1000.0, 1180.0, energy_value=0.8), _window(3, 1180.0, 1360.0, energy_value=0.9)]
    windows = [*first_run, *second_run]

    profile = build_profile(windows)

    assert profile.arc is not None
    assert len(profile.arc) == 64
    total_sec = 1360.0
    nan_indices = [i for i, v in enumerate(profile.arc) if math.isnan(v)]
    assert nan_indices, "expected at least one NaN sample inside the coverage gap"
    for i in nan_indices:
        t = (i / 63) * total_sec
        assert 360.0 < t < 1000.0, f"NaN sample at t={t}s falls outside the expected gap"
    # The samples right at the start and end of the file remain covered and finite.
    assert math.isfinite(profile.arc[0])
    assert math.isfinite(profile.arc[-1])
    # A naive bridge would land near the midpoint of 0.3 and 0.8 (~0.55); the gap must not read that.
    bridged_guess = pytest.approx(0.55, abs=0.05)
    assert not any(profile.arc[i] == bridged_guess for i in nan_indices)


def test_build_profile_reports_no_arc_or_mean_vector_for_a_fine_only_file() -> None:
    """661 real files today have fine windows only (phaze-hia9z) -- a gap, never manufactured zeros."""
    windows = [
        AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=0, start_sec=0.0, end_sec=30.0, bpm=120.0, camelot="8A"),
        AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=1, start_sec=30.0, end_sec=60.0, bpm=121.0, camelot="8A"),
    ]
    profile = build_profile(windows)
    assert profile.arc is None
    assert profile.mean_vector is None
    assert profile.glyph is None
    assert profile.peak_sec is None
    assert profile.camelot_modal == "8A"
    assert profile.sources == {"bpm": "fine"}


def test_build_profile_reports_no_bpm_source_when_no_fine_bpm_exists() -> None:
    windows = _contiguous_coarse_windows(2)
    profile = build_profile(windows)
    assert profile.sources == {"bpm": "none"}


def test_build_profile_mean_vector_averages_stored_mood_scores_positionally() -> None:
    vector_a = dict.fromkeys(MOOD_ORDER, 0.2)
    vector_b = dict.fromkeys(MOOD_ORDER, 0.6)
    windows = [
        _window(0, 0.0, 180.0, energy_value=0.5, mood_scores=vector_a),
        _window(1, 180.0, 360.0, energy_value=0.5, mood_scores=vector_b),
    ]
    profile = build_profile(windows)
    assert profile.mean_vector is not None
    assert profile.mean_vector == pytest.approx([0.4] * len(MOOD_ORDER))


def test_build_profile_peak_sec_is_the_elapsed_time_of_the_arcs_maximum() -> None:
    windows = [
        _window(0, 0.0, 180.0, energy_value=0.1),
        _window(1, 180.0, 360.0, energy_value=0.9),
        _window(2, 360.0, 540.0, energy_value=0.2),
    ]
    profile = build_profile(windows)
    assert profile.peak_sec is not None
    # The peak coarse window is windows[1], centred at 270s of a 540s file.
    assert profile.peak_sec == pytest.approx(270.0, abs=15.0)


def test_build_profile_glyph_cells_pair_coarse_energy_with_the_overlapping_fine_camelot() -> None:
    coarse = [_window(0, 0.0, 180.0, tier="coarse", energy_value=0.4)]
    fine = [
        AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=0, start_sec=0.0, end_sec=30.0, camelot="8A"),
        AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=1, start_sec=30.0, end_sec=60.0, camelot="8A"),
    ]
    profile = build_profile([*coarse, *fine])
    assert profile.glyph == [{"camelot_number": 8, "energy": 0.4}]


# ---------------------------------------------------------------------------
# phaze-x1qr3.2: harmonic_discipline -- the flicker filter
# ---------------------------------------------------------------------------


def _fine_camelot_windows(codes: Sequence[str]) -> list[AnalysisWindow]:
    return [
        AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=i, start_sec=i * 30.0, end_sec=(i + 1) * 30.0, camelot=code)
        for i, code in enumerate(codes)
    ]


def test_harmonic_discipline_ignores_a_one_window_flicker() -> None:
    """A single-window blip to a non-adjacent key does not count as a transition at all.

    Without the flicker filter this would register two transitions (8A->3A, 3A->8A), neither
    wheel-adjacent, for a discipline of 0.0. Filtered, the sequence never really changes key.
    """
    windows = _fine_camelot_windows(["8A", "8A", "8A", "3A", "8A", "8A"])
    assert harmonic_discipline(windows) == 1.0


def test_harmonic_discipline_counts_a_real_two_window_change() -> None:
    """A key run sustained for >= 2 fine windows is a real transition and IS counted.

    "8A" -> "3A" is not wheel-adjacent (distance 5 on the 12-position wheel, same letter), so a
    real, counted transition here drives discipline down from the flicker case's 1.0.
    """
    windows = _fine_camelot_windows(["8A", "8A", "3A", "3A", "8A", "8A"])
    assert harmonic_discipline(windows) == 0.0


def test_harmonic_discipline_counts_a_real_wheel_adjacent_change_as_fully_disciplined() -> None:
    """A sustained but wheel-adjacent change (8A -> 9A, distance 1) is a counted, disciplined mix."""
    windows = _fine_camelot_windows(["8A", "8A", "9A", "9A"])
    assert harmonic_discipline(windows) == 1.0


def test_harmonic_discipline_is_none_without_any_usable_camelot_data() -> None:
    windows = _fine_camelot_windows([])
    assert harmonic_discipline(windows) is None
