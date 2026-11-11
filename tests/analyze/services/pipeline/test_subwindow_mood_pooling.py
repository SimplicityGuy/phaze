"""Pooling a mood graph's per-patch output into sub-windows (phaze-x3gak).

No real graph here: matrices are built by hand, so every expected value is derived independently of
the code under test. The claims about REAL essentia output are in ``test_subwindow_mood_real_models.py``.

Acceptance criteria and the tests that discharge them:

* coarse ``features`` / ``mood_scores`` byte-identical to before
  -> ``test_predict_single_still_returns_the_exact_mean_it_always_did``,
     ``test_pooling_changes_neither_the_feature_dict_nor_the_sweep_return``, and the untouched
     ``test_analysis_output_equivalence.py`` golden tests (the result dict is unchanged unless
     ``include_grids`` is set: ``test_the_result_dict_is_unchanged_unless_grids_are_requested``);
* sub-window vectors averaged with patch-count weights reproduce the window's mean, and a misaligned
  pooling fails -> ``test_patch_count_weighted_sub_window_means_reproduce_the_coarse_mean`` and
  ``test_a_pooling_that_double_counts_overlapping_patches_fails_the_same_check``;
* ``ceil(window length / sub-window length)`` vectors per window, positional in ``MOOD_ORDER``
  -> ``test_each_coarse_window_yields_ceil_length_over_15_vectors``,
     ``test_vectors_are_positional_in_mood_order_with_the_positive_class_chosen_by_label``;
* patch data is held for the current chunk only
  -> ``test_what_survives_the_sweep_is_floats_per_slice_not_a_patch_matrix``.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from phaze.services import analysis as analysis_mod
from phaze.services.analysis_models import MODEL_SETS, ModelConfig
from phaze.services.analysis_subwindows import (
    PATCH_GEOMETRY,
    PatchMean,
    combine_variants,
    n_slices_for,
    patch_centres,
    pool_positive_class,
    positive_index,
    with_patches,
)
from phaze.services.analysis_windows import SUBWINDOW_SEC
from phaze.services.set_projection import MOOD_ORDER


def _matrix(n_patches: int, *, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).random((n_patches, 2)).astype(np.float32)


def _n_patches(seconds: float, classifier_type: str) -> int:
    """The closed form from ADR-0026 section 1, restated here so the test does not call the code under test."""
    samples = int(seconds * 16000)
    frame, frame_hop, patch, hop = (512, 256, 187, 93) if classifier_type == "musicnn" else (400, 160, 96, 93)
    frames = (samples - frame) // frame_hop + 1
    return (frames - patch) // hop + 1


@pytest.mark.parametrize(("kind", "expected"), [("musicnn", 119), ("vggish", 192)])
def test_the_geometry_table_reproduces_the_measured_patch_counts(kind: str, expected: int) -> None:
    # 119 / 192 patches at 179.5 s are the ADR's measured counts; the last centre must sit inside the buffer.
    assert _n_patches(179.5, kind) == expected
    centres = patch_centres(expected, kind)
    length, hop = PATCH_GEOMETRY[kind]
    assert centres[0] == pytest.approx(length / 2)
    assert centres[1] - centres[0] == pytest.approx(hop)
    assert centres[-1] < 179.5


def test_the_first_centres_are_the_adrs_stated_values() -> None:
    assert patch_centres(1, "musicnn")[0] == pytest.approx(1.496)
    assert patch_centres(1, "vggish")[0] == pytest.approx(0.48)
    assert patch_centres(3, "musicnn")[2] == pytest.approx(1.496 + 2 * 1.488)


# --- attribution --------------------------------------------------------------------------------


def test_a_patch_belongs_to_the_slice_holding_its_centre_half_open() -> None:
    # musicnn centres: 1.496 + 1.488 k. k=9 -> 14.888 (slice 0); k=10 -> 16.376 (slice 1).
    matrix = np.zeros((11, 2), dtype=np.float32)
    matrix[:, 0] = np.arange(11)
    sums, counts = pool_positive_class(matrix, ["positive", "negative"], "musicnn", n_slices=2)
    assert counts == [10, 1]
    assert sums == [float(sum(range(10))), 10.0]


def test_a_centre_exactly_on_a_boundary_goes_to_the_later_slice() -> None:
    # Half-open [start, end): a centre at exactly 15.0 s is slice 1. Hop 0.93, patch 0.96 -> centre 0.48 + 0.93 k;
    # no k lands exactly on 15, so assert the rule on the helper's arithmetic with a synthetic geometry.
    with patch.dict(PATCH_GEOMETRY, {"unit": (2.0, 1.0)}):
        # centres 1, 2, ..., so with a 2 s slice the centre 2.0 is the first of slice 1.
        sums, counts = pool_positive_class(np.ones((4, 2), dtype=np.float32), ["p", "q"], "unit", n_slices=3, sub_sec=2)
    assert counts == [1, 2, 1]
    assert sums == [1.0, 2.0, 1.0]


def test_an_overlong_centre_is_clipped_to_the_last_slice_never_indexed_past_it() -> None:
    _sums, counts = pool_positive_class(_matrix(200), ["p", "q"], "musicnn", n_slices=2)
    assert sum(counts) == 200
    assert counts[1] > 0


def test_a_slice_with_no_centre_is_none_not_an_invented_vector() -> None:
    # ADR-0026 section 2: a 16 s window's last musicnn centre is at 13.40 s, so the [15, 16) slice is empty.
    n = _n_patches(16.0, "musicnn")
    assert n == 9  # 1.496 + 1.488 * 8 = 13.4
    assert n_slices_for(0.0, 16.0) == 2
    pooled = {name: [pool_positive_class(_matrix(n, seed=i), ["p", "q"], "musicnn", 2)] for i, name in enumerate(MOOD_ORDER)}
    vectors = combine_variants(pooled, 2)
    assert vectors[0] is not None
    assert vectors[1] is None


def test_a_single_patch_buffer_is_pooled_as_one_patch() -> None:
    # essentia squeezes a short buffer's output to a bare [classes] vector; atleast_2d must make it one patch.
    sums, counts = pool_positive_class(np.array([0.7, 0.3], dtype=np.float32), ["p", "q"], "musicnn", n_slices=1)
    assert counts == [1]
    assert sums == [pytest.approx(0.7)]


# --- the discriminating test --------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["musicnn", "vggish"])
@pytest.mark.parametrize("seconds", [180.0, 179.5, 100.0, 37.2])
def test_patch_count_weighted_sub_window_means_reproduce_the_coarse_mean(kind: str, seconds: float) -> None:
    n = _n_patches(seconds, kind)
    matrix = _matrix(n, seed=n)
    labels = ["positive", "negative"]
    coarse_mean = float(np.mean(matrix, axis=0)[0])  # what _predict_single has always stored

    n_slices = n_slices_for(0.0, seconds)
    sums, counts = pool_positive_class(matrix, labels, kind, n_slices)
    sub_means = [s / c for s, c in zip(sums, counts, strict=True) if c]
    weighted = sum(m * c for m, c in zip(sub_means, [c for c in counts if c], strict=True)) / sum(counts)

    assert sum(counts) == n, "every patch is counted exactly once"
    assert weighted == pytest.approx(coarse_mean, abs=1e-6)


def test_a_pooling_that_double_counts_overlapping_patches_fails_the_same_check() -> None:
    # The misaligned pooling the ADR rejects: attribute a patch to EVERY slice its span overlaps. Overlapping
    # patches then count twice and the weighted mean drifts off the coarse mean. The honest check must see it.
    kind = "musicnn"
    n = _n_patches(180.0, kind)
    matrix = _matrix(n, seed=7)
    length, hop = PATCH_GEOMETRY[kind]
    n_slices = n_slices_for(0.0, 180.0)
    sums = [0.0] * n_slices
    counts = [0] * n_slices
    for k in range(n):
        start, end = k * hop, k * hop + length
        for slot in range(int(start // SUBWINDOW_SEC), min(int(end // SUBWINDOW_SEC), n_slices - 1) + 1):
            sums[slot] += float(matrix[k, 0])
            counts[slot] += 1
    assert sum(counts) > n  # patches were double counted
    weighted = sum(sums) / sum(counts)
    coarse_mean = float(np.mean(matrix, axis=0)[0])
    assert not math.isclose(weighted, coarse_mean, abs_tol=1e-6)
    # ...and the correct pooling, on the same matrix, passes.
    good_sums, good_counts = pool_positive_class(matrix, ["p", "q"], kind, n_slices)
    assert sum(good_sums) / sum(good_counts) == pytest.approx(coarse_mean, abs=1e-6)


def test_a_pooling_that_shifts_centres_by_a_hop_moves_values_between_slices() -> None:
    # Misalignment that keeps the TOTAL intact (so the weighted mean alone cannot see it) still changes which
    # slice holds which patch; the per-slice counts pin the half-open centre rule from outside.
    n = _n_patches(180.0, "musicnn")
    matrix = np.ones((n, 2), dtype=np.float32)
    _sums, counts = pool_positive_class(matrix, ["p", "q"], "musicnn", 12)
    expected = [0] * 12
    for k in range(n):
        expected[min(int((k * 1.488 + 1.496) // 15), 11)] += 1
    assert counts == expected
    assert counts[0] == 10
    assert counts[-1] == 9  # ADR: the last slice of a full 180 s window holds 9 musicnn centres


# --- slices, order, label selection --------------------------------------------------------------


@pytest.mark.parametrize("length", [0.5, 14.9, 15.0, 15.1, 90.0, 179.9, 180.0])
def test_each_coarse_window_yields_ceil_length_over_15_vectors(length: float) -> None:
    n_slices = math.ceil(length / SUBWINDOW_SEC)
    assert n_slices_for(40.0, 40.0 + length) == n_slices
    pooled = {name: [(([1.0] * n_slices), ([1] * n_slices))] for name in MOOD_ORDER}
    assert len(combine_variants(pooled, n_slices)) == n_slices


def test_vectors_are_positional_in_mood_order_with_the_positive_class_chosen_by_label() -> None:
    # mood_relaxed's classes are ['non_relaxed', 'relaxed']: the positive class is column 1, not 0.
    assert positive_index(["non_relaxed", "relaxed"]) == 1
    assert positive_index(["happy", "non_happy"]) == 0
    assert positive_index(["not_a", "not_b"]) == 0  # nothing qualifies: the first column, like the coarse path
    matrix = np.array([[0.9, 0.1], [0.7, 0.3]], dtype=np.float32)
    sums, counts = pool_positive_class(matrix, ["non_relaxed", "relaxed"], "vggish", 1)
    assert sums[0] / counts[0] == pytest.approx(0.2)  # mean of the 'relaxed' column, not of 'non_relaxed'

    # Each set gets a distinct value so a transposed order shows up as a wrong position.
    pooled = {name: [([float(i)], [1])] for i, name in enumerate(MOOD_ORDER)}
    (vector,) = combine_variants(pooled, 1)
    assert vector == tuple(float(i) for i in range(len(MOOD_ORDER)))


def test_variants_of_a_set_are_averaged_like_the_coarse_vector() -> None:
    pooled = {name: [([0.2], [1]), ([0.6], [1])] for name in MOOD_ORDER}
    (vector,) = combine_variants(pooled, 1)
    assert vector == pytest.approx((0.4,) * len(MOOD_ORDER))


def test_a_missing_set_makes_every_slice_a_gap() -> None:
    pooled = {name: [([0.5], [1])] for name in MOOD_ORDER[:-1]}
    assert combine_variants(pooled, 3) == (None, None, None)


# --- the coarse path is untouched ---------------------------------------------------------------------


class _FakeClassifier:
    def __init__(self, output: Any) -> None:
        self.output = output

    def __call__(self, _audio: Any) -> Any:
        return self.output


@pytest.mark.parametrize("output", [_matrix(119, seed=3), np.array([0.7, 0.3], dtype=np.float32), _matrix(1, seed=4)])
def test_predict_single_still_returns_the_exact_mean_it_always_did(output: Any) -> None:
    model = ModelConfig(name="mood_happy", variant="musicnn_msd", filename="m", classifier_type="musicnn")
    with patch.object(analysis_mod, "_get_classifier", return_value=_FakeClassifier(output)):
        result = analysis_mod._predict_single(np.zeros(16, dtype=np.float32), model, "/m")
    legacy = np.mean(np.atleast_2d(output), axis=0)  # the pre-change expression, verbatim
    assert isinstance(result, PatchMean)
    assert result.dtype == legacy.dtype
    assert result.tobytes() == legacy.tobytes()
    assert result.patches.shape == np.atleast_2d(output).shape
    assert [float(x) for x in result] == [float(x) for x in legacy]


def test_a_derived_value_does_not_keep_the_matrix_alive() -> None:
    result = with_patches(np.array([0.5, 0.5], dtype=np.float32), _matrix(119))
    assert (result * 2).patches is None
    assert result[:1].patches is None


def _stub_predict(_audio: Any, model: ModelConfig, _models_dir: str) -> Any:
    n = _n_patches(60.0, model.classifier_type)
    matrix = np.tile(np.array([0.7, 0.3], dtype=np.float32), (n, 1))
    return with_patches(np.mean(matrix, axis=0), matrix)


def _stub_labels(filename: str, _models_dir: str) -> list[str]:
    if "discogs" in filename:
        return [f"G{i}" for i in range(400)]
    return ["positive", "negative"]


def _sweep(buffers: list[tuple[int, Any]], **kwargs: Any) -> tuple[dict[int, dict[str, Any]], set[int]]:
    with (
        patch.object(analysis_mod, "_predict_single", _stub_predict),
        patch.object(analysis_mod, "_get_labels", _stub_labels),
        patch.object(analysis_mod, "_release_classifier_timed"),
    ):
        return analysis_mod._run_model_sets_over_windows(buffers, "/m", lambda _i: None, **kwargs)


def test_pooling_changes_neither_the_feature_dict_nor_the_sweep_return() -> None:
    audio = np.full(16000, 0.1, dtype=np.float32)
    plain_features, plain_failed = _sweep([(0, audio)])
    pooled: dict[int, dict[str, list[tuple[list[float], list[int]]]]] = {}
    pooled_features, pooled_failed = _sweep([(0, audio)], slices_by_window={0: 4}, pooled_out=pooled)
    assert pooled_features == plain_features
    assert pooled_failed == plain_failed == set()
    assert set(pooled[0]) == set(MOOD_ORDER)


def test_what_survives_the_sweep_is_floats_per_slice_not_a_patch_matrix() -> None:
    audio = np.full(16000, 0.1, dtype=np.float32)
    pooled: dict[int, dict[str, list[tuple[list[float], list[int]]]]] = {}
    _sweep([(0, audio)], slices_by_window={0: 4}, pooled_out=pooled)
    for per_variant in pooled[0].values():
        for sums, counts in per_variant:
            assert all(type(v) is float for v in sums)
            assert all(type(c) is int for c in counts)
            assert len(sums) == len(counts) == 4


def test_a_stub_that_returns_a_bare_vector_gets_no_sub_window_mood_and_no_error() -> None:
    audio = np.full(16000, 0.1, dtype=np.float32)
    pooled: dict[int, dict[str, list[tuple[list[float], list[int]]]]] = {}
    with (
        patch.object(analysis_mod, "_predict_single", lambda *_a: np.array([0.7, 0.3], dtype=np.float32)),
        patch.object(analysis_mod, "_get_labels", _stub_labels),
        patch.object(analysis_mod, "_release_classifier_timed"),
    ):
        features, failed = analysis_mod._run_model_sets_over_windows([(0, audio)], "/m", lambda _i: None, slices_by_window={0: 4}, pooled_out=pooled)
    assert failed == set()
    assert features[0]["mood_happy"]
    assert pooled == {}


def test_every_selected_mood_set_is_one_the_projection_orders() -> None:
    assert {s.name for s in MODEL_SETS} == set(MOOD_ORDER)


def test_the_result_dict_is_unchanged_unless_grids_are_requested() -> None:
    from phaze.services.analysis_windows import CoarseWindow, FineWindow

    fine = [FineWindow(0, 0.0, 30.0, 120.0, "A minor", 1.0, beats=(0.5, 1.0, 1.5))]
    moods = tuple((0.1,) * len(MOOD_ORDER) for _ in range(2))
    coarse = [CoarseWindow(0, 0.0, 30.0, "happy", "house", 0.5, {"danceability": {}}, subwindow_moods=moods)]
    default = analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 1)
    grids = analysis_mod._assemble_analysis_result("f", fine, 1, coarse, 1, include_grids=True)
    assert set(grids) - set(default) == {"subwindows", "beat_grid"}
    assert {k: v for k, v in grids.items() if k in default} == default
    assert [(r["sub_index"], r["start_sec"], r["end_sec"]) for r in grids["subwindows"]] == [(0, 0.0, 15.0), (1, 15.0, 30.0)]
    assert grids["subwindows"][0]["mood_scores"] == [pytest.approx(0.1)] * len(MOOD_ORDER)
    assert grids["beat_grid"]["beats"] == [0.5, 1.0, 1.5]
