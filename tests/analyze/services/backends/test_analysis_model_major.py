"""phaze-15sw: model-major coarse inference — residency, construction count, identity.

The coarse pass used to iterate WINDOW-major with every ``TensorflowPredict*`` graph
held in ``_classifier_cache`` for the process's lifetime. Spike ``phaze-esut`` measured
what that residency cost (+4.090 GiB macOS / +3.995 GiB Linux with zero inference
performed) and ``phaze-7i0k`` confirmed it on the burst node; the loop nesting is now
inverted so exactly one graph is resident at a time.

These tests are the automated half of that claim — the parts assertable without 3 GB of
``.pb`` files:

* **residency + construction count** — instrumentation, not reading: every model is
  built exactly once per file (the wall-clock optimization the cache existed to provide
  is preserved) and the cache never holds more than one graph.
* **output identity** — model-major features are byte-identical, as JSON, to what the
  window-major nesting produced on the same buffers. Key insertion order is output here
  (these dicts are JSON-serialized into ``features`` JSONB), so it is compared as bytes.
* **failure isolation** — an inference failure still kills exactly one window, and the
  exception is not retained (retaining it would retain its traceback, which holds
  ``_predict_single``'s frame, which holds the classifier — silently pinning the graph
  the sweep just released).

The measured peak-RSS half is not CI-feasible (it needs the real model set on a Linux
burst node) and lives in the bead's before/after table.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import phaze.services.analysis as analysis_mod
from phaze.services.analysis import GENRE_MODEL, MODEL_SETS, analyze_file


if TYPE_CHECKING:
    from collections.abc import Iterator


_ALL_MODELS = [m for ms in MODEL_SETS for m in ms.models] + [GENRE_MODEL]
_N_MODELS = 34
_DURATION_SEC = 900.0  # 5 coarse (180s) windows -- enough for window/model mix-ups to show


# ---------------------------------------------------------------------------
# A mock whose outputs are distinct per (model, window)
# ---------------------------------------------------------------------------
#
# The default mocks in test_analysis.py return one constant array for every model, which
# cannot distinguish "ran the right model on the right window" from "ran something". Here
# each window's buffer carries a marker value and each graph a filename-derived offset, so
# any transposition of the model-major loops changes the numbers.


def _marker(audio: np.ndarray) -> float:
    return float(audio[0])


def _n_classes(graph_filename: str) -> int:
    return 400 if "discogs" in graph_filename else 2


def _predictions_for(graph_filename: str, audio: np.ndarray) -> np.ndarray:
    """Deterministic (model, window) -> activations. 10 frames x n_classes."""
    seed = sum(graph_filename.encode()) % 997
    base = np.arange(_n_classes(graph_filename), dtype=np.float32)
    row = ((base * 7.0 + seed + _marker(audio) * 13.0) % 101.0) / 101.0
    return np.tile(row, (10, 1))


def _build_mock_es(duration_sec: float = _DURATION_SEC) -> MagicMock:
    mock_es = MagicMock()

    mock_metadata = MagicMock()
    mock_metadata.return_value = ("", "", "", "", "", "", "", MagicMock(), duration_sec, 128, 16000, 1)
    mock_es.MetadataReader.return_value = mock_metadata

    def _easyloader(*, filename: str, sampleRate: int, startTime: float, endTime: float) -> MagicMock:
        loader = MagicMock()
        # Marker == the window's start second, so a buffer identifies its window.
        loader.return_value = np.full(1024, startTime, dtype=np.float32)
        return loader

    mock_es.EasyLoader.side_effect = _easyloader

    mock_rhythm = MagicMock()
    mock_rhythm.return_value = (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5]))
    mock_es.RhythmExtractor2013.return_value = mock_rhythm

    mock_key = MagicMock()
    mock_key.return_value = ("C", "minor", 0.8)
    mock_es.KeyExtractor.return_value = mock_key

    for cls_name in ("TensorflowPredictMusiCNN", "TensorflowPredictVGGish", "TensorflowPredictEffnetDiscogs"):
        # batchSize is accepted and ignored: these mocks assert the model-major LOOP
        # SHAPE, and the batch parameter (phaze-0582) does not change which graph sees
        # which window. Its own contract is asserted in test_analysis_batch_size.py.
        def _ctor(*, graphFilename: str, batchSize: int) -> MagicMock:
            inst = MagicMock()
            inst.side_effect = lambda audio: _predictions_for(graphFilename, audio)
            return inst

        setattr(mock_es, cls_name, MagicMock(side_effect=_ctor))

    return mock_es


def _mock_labels(model_filename: str, _models_dir: str) -> list[str]:
    n = _n_classes(model_filename)
    return [f"Genre{i}" for i in range(n)] if n > 2 else ["positive_class", "non_positive_class"]


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    """The module caches are process-global; keep each test's accounting its own."""
    analysis_mod._classifier_cache.clear()
    analysis_mod._labels_cache.clear()
    yield
    analysis_mod._classifier_cache.clear()
    analysis_mod._labels_cache.clear()


# ---------------------------------------------------------------------------
# Construction count + residency -- asserted by instrumentation, not by reading
# ---------------------------------------------------------------------------


def _instrumented_analyze(mock_es: MagicMock, **kwargs: Any) -> tuple[dict[str, Any], list[str], list[int]]:
    """Run analyze_file, recording every graph construction and the cache size at each."""
    built: list[str] = []
    resident_at_build: list[int] = []
    real_get_classifier = analysis_mod._get_classifier

    def counting(model: Any, models_dir: str) -> Any:
        was_cached = model.filename in analysis_mod._classifier_cache
        out = real_get_classifier(model, models_dir)
        if not was_cached:
            built.append(model.filename)
            # Cache size AFTER insertion: 1 means this graph is the only one resident.
            resident_at_build.append(len(analysis_mod._classifier_cache))
        return out

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels),
        patch.object(analysis_mod, "_get_classifier", side_effect=counting),
    ):
        result = analyze_file("/fake/audio.mp3", "/fake/models", **kwargs)
    return result, built, resident_at_build


def test_every_model_constructed_exactly_once_per_file() -> None:
    """34 graphs, 34 constructions -- the cache's wall-clock benefit is fully preserved."""
    mock_es = _build_mock_es()
    result, built, _ = _instrumented_analyze(mock_es)

    assert result["coarse_windows_analyzed"] == 5, "the fixture must run several coarse windows for this to mean anything"
    assert len(built) == _N_MODELS, f"expected exactly {_N_MODELS} constructions across the whole file, got {len(built)}"
    assert sorted(built) == sorted(m.filename for m in _ALL_MODELS)
    assert len(set(built)) == len(built), f"a model was rebuilt: {[f for f in set(built) if built.count(f) > 1]}"


def test_at_most_one_graph_is_resident() -> None:
    """The whole point: residency is 1, not 34, at every instant of the coarse pass."""
    mock_es = _build_mock_es()
    result, built, resident_at_build = _instrumented_analyze(mock_es)

    assert built, "no graphs were built -- the instrumentation is not wired up"
    assert max(resident_at_build) == 1, (
        f"more than one graph was co-resident (max cache size {max(resident_at_build)}); model-major iteration must release each graph before building the next"
    )
    assert analysis_mod._classifier_cache == {}, "the last model's graph was never released"
    assert result["coarse_windows_analyzed"] == 5


def test_construction_count_scales_with_chunks_not_with_windows() -> None:
    """34 constructions per COARSE CHUNK -- not per window, and not once per file (phaze-w55w1).

    This assertion changed with the chunked rework and the change is a real, deliberate cost,
    so it is stated exactly rather than loosened. Under the caps a file cost 34 constructions
    FULL STOP, because the whole tier was one model-major sweep. Chunking means one sweep per
    chunk, so the cost is ``34 x ceil(coarse_windows / _COARSE_CHUNK_WINDOWS)``: that is the
    price paid to keep PCM residency independent of duration (D-07).

    What did NOT change is the invariant that actually bought the memory (phaze-15sw): still one
    graph at a time, still no per-WINDOW reload. The 900 s file (5 coarse windows, 1 chunk) and
    the 7 200 s file (40 coarse windows, 2 chunks) differ 8x in windows and only 2x in
    constructions -- if graphs were rebuilt per window the second figure would be 34 x 40.
    """
    result_5, built_5, _ = _instrumented_analyze(_build_mock_es(900.0))
    result_40, built_40, resident_40 = _instrumented_analyze(_build_mock_es(7200.0))

    assert result_5["coarse_windows_analyzed"] == 5  # 1 chunk
    assert result_40["coarse_windows_analyzed"] == 40  # 2 chunks (30 + 10)
    assert len(built_5) == _N_MODELS
    assert len(built_40) == _N_MODELS * 2, "constructions must track CHUNKS; 34 x n_windows would mean a per-window reload"
    # The load-bearing half: chunking must not have reintroduced co-residency.
    assert max(resident_40) == 1


# ---------------------------------------------------------------------------
# Output identity against the window-major nesting
# ---------------------------------------------------------------------------


def _window_major_features(audio_16k: Any, models_dir: str) -> dict[str, Any]:
    """The pre-phaze-15sw ``_run_model_sets`` body, verbatim, as the identity reference."""
    features: dict[str, Any] = {}
    for model_set in MODEL_SETS:
        set_data: dict[str, list[dict[str, Any]]] = {}
        for model in model_set.models:
            predictions = analysis_mod._predict_single(audio_16k, model, models_dir)
            labels = analysis_mod._get_labels(model.filename, models_dir)
            set_data[model.variant] = [{"label": label, "prediction": float(pred)} for label, pred in zip(labels, predictions, strict=False)]
        features[model_set.name] = set_data

    genre_predictions = analysis_mod._predict_single(audio_16k, GENRE_MODEL, models_dir)
    genre_labels = analysis_mod._get_labels(GENRE_MODEL.filename, models_dir)
    genre_pairs = list(zip(genre_labels, genre_predictions, strict=False))
    genre_pairs.sort(key=lambda pair: float(pair[1]), reverse=True)
    features["genre"] = {"predictions": [{"label": label, "confidence": float(conf)} for label, conf in genre_pairs[:10]]}
    return features


def test_model_major_features_are_byte_identical_to_window_major() -> None:
    """Same buffers, both nestings, compared as JSON bytes -- values AND key order."""
    mock_es = _build_mock_es()
    buffers = [(i, np.full(1024, float(i * 180), dtype=np.float32)) for i in range(4)]

    with patch.object(analysis_mod, "es", mock_es), patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels):
        expected = {key: json.dumps(_window_major_features(buf, "/fake/models"), sort_keys=False) for key, buf in buffers}
        analysis_mod._classifier_cache.clear()
        actual_features, failed = analysis_mod._run_model_sets_over_windows(buffers, "/fake/models", lambda _i: None)

    assert failed == set()
    for key, _ in buffers:
        assert json.dumps(actual_features[key], sort_keys=False) == expected[key], f"window {key} differs from the window-major reference"


def test_features_key_order_is_model_sets_order_then_genre() -> None:
    """``features`` is serialized to JSONB downstream, so insertion order is output."""
    mock_es = _build_mock_es()
    result, _, _ = _instrumented_analyze(mock_es)

    coarse = [w for w in result["windows"] if w["tier"] == "coarse"]
    expected_keys = [ms.name for ms in MODEL_SETS] + ["genre"]
    for w in coarse:
        assert list(w["features"]) == expected_keys
        for ms in MODEL_SETS:
            assert list(w["features"][ms.name]) == [m.variant for m in ms.models]


def test_distinct_windows_get_distinct_features() -> None:
    """Guards the transposition bug the restructure could plausibly introduce."""
    mock_es = _build_mock_es()
    result, _, _ = _instrumented_analyze(mock_es)

    coarse = [w for w in result["windows"] if w["tier"] == "coarse"]
    serialized = {json.dumps(w["features"], sort_keys=True) for w in coarse}
    assert len(serialized) == len(coarse), "every window must carry ITS OWN activations, not a neighbour's"


# ---------------------------------------------------------------------------
# Failure isolation under model-major iteration
# ---------------------------------------------------------------------------


def test_inference_failure_kills_exactly_one_window() -> None:
    """One window failing mid-sweep is dropped; the others come back complete."""
    mock_es = _build_mock_es()
    victim_start = 360.0

    real_predict = analysis_mod._predict_single

    def flaky(audio: Any, model: Any, models_dir: str) -> Any:
        if model.filename == "mood_happy-vggish-audioset-1" and _marker(audio) == victim_start:
            msg = "simulated inference failure"
            raise RuntimeError(msg)
        return real_predict(audio, model, models_dir)

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels),
        patch.object(analysis_mod, "_predict_single", side_effect=flaky),
    ):
        result = analyze_file("/fake/audio.mp3", "/fake/models")

    coarse = [w for w in result["windows"] if w["tier"] == "coarse"]
    assert len(coarse) == 4, "exactly the one failing window must be dropped"
    assert victim_start not in [w["start_sec"] for w in coarse]
    for w in coarse:
        assert list(w["features"]) == [ms.name for ms in MODEL_SETS] + ["genre"]
    assert result["mood"] is not None


def test_failed_window_is_not_retried_by_later_models() -> None:
    """A killed window is skipped by every subsequent model, not re-attempted 30 more times."""
    calls: list[float] = []
    mock_es = _build_mock_es()
    real_predict = analysis_mod._predict_single

    def flaky(audio: Any, model: Any, models_dir: str) -> Any:
        calls.append(_marker(audio))
        if model.filename == "mood_acoustic-musicnn-msd-2" and _marker(audio) == 0.0:
            msg = "simulated inference failure on the very first model"
            raise RuntimeError(msg)
        return real_predict(audio, model, models_dir)

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels),
        patch.object(analysis_mod, "_predict_single", side_effect=flaky),
    ):
        analyze_file("/fake/audio.mp3", "/fake/models")

    assert calls.count(0.0) == 1, "window 0 failed on the first of 34 models and must never be fed to another"


def test_failures_are_reported_without_retaining_the_exception() -> None:
    """The kill list is indices, not exceptions.

    Retaining the exception retains its traceback, which holds ``_predict_single``'s frame,
    which holds ``classifier`` -- pinning the graph past ``_release_classifier`` and quietly
    rebuilding the co-residency this restructure removes.
    """
    mock_es = _build_mock_es()
    buffers = [(i, np.full(1024, float(i * 180), dtype=np.float32)) for i in range(3)]
    reported: list[int] = []

    def flaky(audio: Any, model: Any, models_dir: str) -> Any:
        msg = "everything fails"
        raise RuntimeError(msg)

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels),
        patch.object(analysis_mod, "_predict_single", side_effect=flaky),
    ):
        _features, failed = analysis_mod._run_model_sets_over_windows(buffers, "/fake/models", reported.append)

    assert failed == {0, 1, 2}
    assert all(isinstance(x, int) for x in failed), "the kill list must carry indices, never exception objects"
    assert sorted(reported) == [0, 1, 2], "each window must be reported exactly once, in-handler"
    assert analysis_mod._classifier_cache == {}, "the failing sweep must still release its graph (the finally)"


def test_single_buffer_wrapper_still_propagates() -> None:
    """``_run_model_sets`` keeps its pre-phaze-15sw contract: one window, exception propagates."""
    mock_es = _build_mock_es()

    def boom(audio: Any, model: Any, models_dir: str) -> Any:
        msg = "graph is broken"
        raise RuntimeError(msg)

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels),
        patch.object(analysis_mod, "_predict_single", side_effect=boom),
        pytest.raises(RuntimeError, match="graph is broken"),
    ):
        analysis_mod._run_model_sets(np.zeros(1024, dtype=np.float32), "/fake/models")


def test_single_buffer_wrapper_matches_window_major() -> None:
    """The retained single-window entry point produces the window-major result exactly."""
    mock_es = _build_mock_es()
    buf = np.full(1024, 42.0, dtype=np.float32)

    with patch.object(analysis_mod, "es", mock_es), patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels):
        expected = json.dumps(_window_major_features(buf, "/fake/models"), sort_keys=False)
        analysis_mod._classifier_cache.clear()
        actual = json.dumps(analysis_mod._run_model_sets(buf, "/fake/models"), sort_keys=False)

    assert actual == expected


# ---------------------------------------------------------------------------
# phaze-ouz0y: single-frame ("squeezed") activations must not collapse to a scalar
# ---------------------------------------------------------------------------
#
# Production crash (agent_analysis, 2026-08-10, two distinct files within 24h):
# ``analysis child failed (exit 1): TypeError: 'numpy.float64' object is not iterable``.
#
# ``classifier(audio_16k)`` is documented -- and normally observed, see ``_predictions_for``
# above -- to return a 2D ``[patches, classes]`` array. But on a short-enough buffer (the
# trailing coarse/fine window of a short file is the reproducing shape) essentia squeezes the
# patch axis away and returns a bare 1D ``[classes]`` vector instead. Before the fix,
# ``np.mean(activations, axis=0)`` on that 1D vector has no batch axis left to reduce over,
# so it collapses the WHOLE vector to a single 0-d ``numpy.float64`` scalar; that scalar then
# reaches ``zip(labels, predictions)`` in ``_run_model_sets_over_windows`` two lines later and
# ``zip`` raises exactly the production message.


def _predictions_for_squeezed(graph_filename: str, audio: np.ndarray) -> np.ndarray:
    """The same deterministic values as ``_predictions_for``, squeezed to 1D -- the
    single-frame essentia output shape that collapses under ``np.mean(axis=0)`` pre-fix."""
    return _predictions_for(graph_filename, audio)[0]


def _build_mock_es_single_frame(duration_sec: float = _DURATION_SEC) -> MagicMock:
    """Same registry as ``_build_mock_es``, but every ``TensorflowPredict*`` classifier
    returns the squeezed single-frame shape essentia yields on a short-enough buffer."""
    mock_es = _build_mock_es(duration_sec)

    for cls_name in ("TensorflowPredictMusiCNN", "TensorflowPredictVGGish", "TensorflowPredictEffnetDiscogs"):

        def _ctor(*, graphFilename: str, batchSize: int) -> MagicMock:
            inst = MagicMock()
            inst.side_effect = lambda audio: _predictions_for_squeezed(graphFilename, audio)
            return inst

        setattr(mock_es, cls_name, MagicMock(side_effect=_ctor))

    return mock_es


def test_predict_single_normalizes_single_frame_activations_to_a_vector() -> None:
    """A classifier call returning a bare ``[classes]`` vector (essentia's single-frame
    squeeze) must still yield a per-class vector, not a 0-d scalar, from ``_predict_single``."""
    model = MODEL_SETS[0].models[0]
    squeezed = np.array([0.2, 0.8], dtype=np.float32)
    classifier = MagicMock(return_value=squeezed)

    with patch.object(analysis_mod, "_get_classifier", return_value=classifier):
        result = analysis_mod._predict_single(np.zeros(1024, dtype=np.float32), model, "/fake/models")

    assert np.ndim(result) == 1, f"expected a 1D per-class vector, got a {np.ndim(result)}-d result (scalar collapse reproduces phaze-ouz0y)"
    np.testing.assert_allclose(result, squeezed)


def test_single_frame_activations_do_not_crash_the_sweep() -> None:
    """Reproduces the exact production crash end to end through the model-major sweep.

    Pre-fix this raises ``TypeError: 'numpy.float64' object is not iterable`` out of
    ``zip(labels, predictions)`` in ``_run_model_sets_over_windows`` -- the analysis child's
    terminal error line, verbatim.
    """
    mock_es = _build_mock_es_single_frame()
    buffers = [(0, np.full(1024, 0.0, dtype=np.float32))]

    with patch.object(analysis_mod, "es", mock_es), patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels):
        features, failed = analysis_mod._run_model_sets_over_windows(buffers, "/fake/models", lambda _i: None)

    assert failed == set(), "a squeezed single-frame prediction must not be treated as an inference failure"
    mood_predictions = features[0]["mood_acoustic"]["musicnn_msd"]
    assert len(mood_predictions) == 2, "predictions must be zipped one-per-class, not collapsed to a single scalar"
    assert {p["label"] for p in mood_predictions} == {"positive_class", "non_positive_class"}

    genre_predictions = features[0]["genre"]["predictions"]
    assert len(genre_predictions) == 10, "genre predictions must survive the squeeze too"


def _assert_features_approximately_equal(actual: Any, expected: Any, *, path: str = "$") -> None:
    """Structural equality with float tolerance for the leaves.

    Both sides are built from the SAME deterministic ``row`` (``_predictions_for`` tiles one
    row 10x), so a squeezed single-frame vector and a 10-frame ``np.mean`` should carry the
    same value -- but float32 summation-then-division over 10 tiled copies does not round to
    the *exact* same bits as returning the row untouched (~1e-7, ~1 float32 ulp), the same
    class of harmless drift the batch-size docstring above measures and tolerates. Structure
    (keys, labels, list lengths, ordering) is still compared exactly.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual).__name__}"
        assert list(actual) == list(expected), f"{path}: key order differs"
        for key in expected:
            _assert_features_approximately_equal(actual[key], expected[key], path=f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list, got {type(actual).__name__}"
        assert len(actual) == len(expected), f"{path}: length differs ({len(actual)} vs {len(expected)})"
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            _assert_features_approximately_equal(a, e, path=f"{path}[{i}]")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, abs=1e-6), f"{path}: {actual} !~= {expected}"
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_single_frame_activations_match_the_normal_multi_frame_mean() -> None:
    """The squeezed single-frame vector and the tiled 10-frame mean are the SAME activation
    row in this fixture (``_predictions_for`` tiles one row 10x), so the fix must produce
    matching features for both shapes (float tolerance for ``np.mean``'s summation order) --
    proof the normalization is a true no-op for real multi-frame windows, not a
    value-changing workaround."""
    buf = np.full(1024, 42.0, dtype=np.float32)

    with patch.object(analysis_mod, "es", _build_mock_es_single_frame()), patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels):
        analysis_mod._classifier_cache.clear()
        squeezed = _window_major_features(buf, "/fake/models")

    with patch.object(analysis_mod, "es", _build_mock_es()), patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels):
        analysis_mod._classifier_cache.clear()
        multi_frame = _window_major_features(buf, "/fake/models")

    _assert_features_approximately_equal(squeezed, multi_frame)


# ---------------------------------------------------------------------------
# _release_classifier
# ---------------------------------------------------------------------------


def test_release_classifier_evicts_and_is_idempotent() -> None:
    analysis_mod._classifier_cache["some-model"] = object()
    analysis_mod._release_classifier("some-model")
    assert "some-model" not in analysis_mod._classifier_cache
    analysis_mod._release_classifier("some-model")  # safe in a finally, even when absent
    analysis_mod._release_classifier("never-cached")


def test_sweep_releases_the_graph_even_when_the_sweep_raises() -> None:
    """The ``finally`` is what bounds residency to one graph on the error path too."""
    mock_es = _build_mock_es()
    model = MODEL_SETS[0].models[0]

    def boom(audio: Any, m: Any, models_dir: str) -> Any:
        msg = "kaboom"
        raise MemoryError(msg)

    def reraise(_idx: int) -> None:
        raise  # mirrors _run_model_sets' propagating callback

    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_get_labels", side_effect=_mock_labels),
        patch.object(analysis_mod, "_predict_single", side_effect=boom),
        pytest.raises(MemoryError),
    ):
        analysis_mod._sweep_one_model(model, [(0, np.zeros(4, dtype=np.float32))], "/fake/models", set(), reraise)

    assert analysis_mod._classifier_cache == {}
