"""phaze-0582: the `TensorflowPredict*` ``batchSize`` phaze passes, and the one it cannot.

`TensorflowPredict*` batches patches before feeding the graph and its ``batchSize``
DEFAULTS to 64. phaze inherited that default silently until this change; spike
``phaze-mqq5`` measured it as the dominant remaining term in the analysis peak
(2.588 -> 1.715 GiB end to end, -33.7%, for +0.36% wall at the knee of 32).

What is assertable here is the CONTRACT, not the memory: that the parameter is actually
passed on every construction, that ``discogs-effnet-bs64-1`` is exempted (its graph
Placeholder is `[64, 128, 96]` — the batch is baked in, and overriding it is a
configuration error rather than a tuning choice), that the override is a deployment knob
rather than a literal, and that a fat-fingered override degrades to the default instead of
failing every analysis.

Two things these tests deliberately do NOT assert:

* **Peak RSS.** It needs the real 3 GB model set on a Linux burst node; it lives in the
  bead's before/after table, re-measured at submit time.
* **Byte-identical output.** Unlike ``phaze-15sw`` / ``phaze-rc1q``, this change moves the
  last float32 ulp — regrouping patches into different batches changes summation order.
  Measured: max aggregated |delta| 1.19e-7, 0/34 argmax flips, every categorical field
  identical, ``danceability`` moving ~3e-9. A sha256 equivalence test would fail for the
  right reason and be deleted for the wrong one; the bar is the tolerance.

The ``lastBatchMode`` assertion is the ``phaze-rc1q`` §3d interaction check: standard-mode
``TensorflowPredictEffnetDiscogs`` zero-pads the final batch and then erases exactly the
padded predictions under its default ``lastBatchMode="same"``. That default must stay
untouched, and the model it applies to must stay at 64, or the padding arithmetic changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import phaze.services.analysis as analysis_mod
from phaze.services.analysis import GENRE_MODEL, MODEL_SETS, analyze_file


if TYPE_CHECKING:
    from collections.abc import Iterator


_TUNABLE_MODELS = [m for ms in MODEL_SETS for m in ms.models]
_FIXED_BATCH_MODEL = GENRE_MODEL
_DURATION_SEC = 600.0  # 20 fine (30s) + 4 coarse (180s) windows


@pytest.fixture(autouse=True)
def _stub_duration_probe() -> Any:
    """phaze-l832u: ``_probe_duration_sec`` is a REAL ``ffprobe`` subprocess now, not essentia.

    Every test in this module hands ``analyze_file`` a synthetic path with a mocked essentia
    behind it. The duration used to come out of that mock (``es.MetadataReader``); since D-10 it
    comes from ffprobe, which would go looking for a file that does not exist. Stubbing the
    probe -- rather than the binary -- keeps these tests exactly as mocked as they were, and
    leaves the real probe to the tests that exercise it on real audio
    (``tests/analyze/services/pipeline/test_extraction_analysis_handoff.py``).
    """
    with patch("phaze.services.analysis._probe_duration_sec", return_value=_DURATION_SEC):
        yield


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    """The module caches are process-global; a cached graph would skip construction."""
    analysis_mod._classifier_cache.clear()
    analysis_mod._labels_cache.clear()
    yield
    analysis_mod._classifier_cache.clear()
    analysis_mod._labels_cache.clear()


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default arm: the deployment override is absent, so the shipped default applies."""
    monkeypatch.delenv(analysis_mod._TF_BATCH_SIZE_ENV, raising=False)


def _mock_labels(model_filename: str, _models_dir: str) -> list[str]:
    return [f"Genre{i}" for i in range(400)] if "discogs" in model_filename else ["positive_class", "non_positive_class"]


def _build_mock_es(duration_sec: float = _DURATION_SEC) -> MagicMock:
    """Mock essentia whose `TensorflowPredict*` constructors record their kwargs."""
    mock_es = MagicMock()

    mock_metadata = MagicMock()
    mock_metadata.return_value = ("", "", "", "", "", "", "", MagicMock(), duration_sec, 128, 16000, 1)
    mock_es.MetadataReader.return_value = mock_metadata

    loader = MagicMock()
    loader.return_value = np.zeros(16000, dtype=np.float32)
    mock_es.EasyLoader.return_value = loader
    mock_es.MonoLoader.return_value = loader

    mock_rhythm = MagicMock()
    mock_rhythm.return_value = (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5]))
    mock_es.RhythmExtractor2013.return_value = mock_rhythm

    mock_key = MagicMock()
    mock_key.return_value = ("C", "minor", 0.8)
    mock_es.KeyExtractor.return_value = mock_key

    for cls_name in ("TensorflowPredictMusiCNN", "TensorflowPredictVGGish", "TensorflowPredictEffnetDiscogs"):
        instance = MagicMock()
        instance.return_value = np.array([[0.7, 0.3]] * 10, dtype=np.float32)
        setattr(mock_es, cls_name, MagicMock(return_value=instance))

    return mock_es


def _ctor_kwargs(mock_es: MagicMock) -> dict[str, dict[str, Any]]:
    """graph filename (no dir, no extension) -> construction kwargs, across all 3 families."""
    out: dict[str, dict[str, Any]] = {}
    for cls_name in ("TensorflowPredictMusiCNN", "TensorflowPredictVGGish", "TensorflowPredictEffnetDiscogs"):
        for call in getattr(mock_es, cls_name).call_args_list:
            graph = call.kwargs["graphFilename"].rsplit("/", 1)[-1].removesuffix(".pb")
            out[graph] = dict(call.kwargs)
    return out


# ---------------------------------------------------------------------------
# The parameter is passed -- on the tunable graphs, at the measured knee
# ---------------------------------------------------------------------------


def test_default_batch_size_is_the_measured_knee() -> None:
    """32, not essentia's 64: 16/8 are flat on memory and 1 costs +55.7% wall."""
    assert analysis_mod._DEFAULT_TF_BATCH_SIZE == 32


@pytest.mark.parametrize("model", _TUNABLE_MODELS, ids=lambda m: m.filename)
def test_tunable_models_get_the_default_batch_size(model: Any) -> None:
    """Every musicnn/vggish graph resolves to the default rather than inheriting 64."""
    assert analysis_mod._resolve_tf_batch_size(model) == analysis_mod._DEFAULT_TF_BATCH_SIZE


def test_genre_model_is_pinned_at_64() -> None:
    """`discogs-effnet-bs64-1`'s Placeholder is `[64, 128, 96]` -- the batch is in the graph."""
    assert _FIXED_BATCH_MODEL.filename == "discogs-effnet-bs64-1"
    assert analysis_mod._resolve_tf_batch_size(_FIXED_BATCH_MODEL) == 64


@patch("phaze.services.analysis._get_labels", side_effect=_mock_labels)
@patch("phaze.services.analysis.es", new_callable=_build_mock_es)
def test_every_construction_passes_an_explicit_batch_size(mock_es: MagicMock, _labels: MagicMock) -> None:
    """A full `analyze_file` run: all 34 graphs built, 33 at 32 and the genre one at 64.

    Asserted at the essentia boundary rather than at `_get_classifier`, because passing
    NOTHING is exactly the pre-phaze-0582 bug and only the boundary can see the difference
    between "batchSize=64 was passed" and "batchSize was omitted and defaulted to 64".
    """
    analyze_file("/fake/audio.mp3", "/fake/models")

    kwargs_by_graph = _ctor_kwargs(mock_es)
    assert len(kwargs_by_graph) == 34

    for model in _TUNABLE_MODELS:
        assert kwargs_by_graph[model.filename]["batchSize"] == 32, model.filename
    assert kwargs_by_graph[_FIXED_BATCH_MODEL.filename]["batchSize"] == 64


@patch("phaze.services.analysis._get_labels", side_effect=_mock_labels)
@patch("phaze.services.analysis.es", new_callable=_build_mock_es)
def test_last_batch_mode_is_never_passed(mock_es: MagicMock, _labels: MagicMock) -> None:
    """phaze-rc1q §3d: effnet's default `lastBatchMode="same"` must stay in force.

    Standard-mode `TensorflowPredictEffnetDiscogs` zero-pads the signal to fill the final
    batch and then erases exactly `paddingPatches` predictions off the end, so the returned
    patch count is unchanged by the padding. That correction is what makes the padding
    invisible, and it only happens under `lastBatchMode` "same". phaze must keep inheriting
    that default -- and the model it applies to keeps `batchSize=64`, so the padding
    arithmetic this change could have perturbed is not perturbed at all.
    """
    analyze_file("/fake/audio.mp3", "/fake/models")

    for kwargs in _ctor_kwargs(mock_es).values():
        assert "lastBatchMode" not in kwargs
        assert set(kwargs) == {"graphFilename", "batchSize"}


# ---------------------------------------------------------------------------
# Configurable, not a literal -- and unbreakable by a typo
# ---------------------------------------------------------------------------


def test_env_override_applies_to_tunable_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knob is a deployment env var; phaze-rvcn will make it host-derived here."""
    monkeypatch.setenv(analysis_mod._TF_BATCH_SIZE_ENV, "16")

    assert analysis_mod._resolve_tf_batch_size(_TUNABLE_MODELS[0]) == 16


def test_env_override_cannot_move_the_genre_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overriding a graph-fixed batch is a configuration error, so the override is ignored."""
    monkeypatch.setenv(analysis_mod._TF_BATCH_SIZE_ENV, "16")

    assert analysis_mod._resolve_tf_batch_size(_FIXED_BATCH_MODEL) == 64


@pytest.mark.parametrize("raw", ["", "   ", "thirty-two", "32.0", "0", "-8"])
def test_malformed_override_falls_back_to_the_default(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in a deployment env var must not turn every analysis into a hard failure."""
    monkeypatch.setenv(analysis_mod._TF_BATCH_SIZE_ENV, raw)

    assert analysis_mod._resolve_tf_batch_size(_TUNABLE_MODELS[0]) == analysis_mod._DEFAULT_TF_BATCH_SIZE


@patch("phaze.services.analysis._get_labels", side_effect=_mock_labels)
@patch("phaze.services.analysis.es", new_callable=_build_mock_es)
def test_env_override_reaches_the_essentia_constructor(mock_es: MagicMock, _labels: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: the env var actually lands on the construction, not just on the resolver."""
    monkeypatch.setenv(analysis_mod._TF_BATCH_SIZE_ENV, "8")

    analyze_file("/fake/audio.mp3", "/fake/models")

    kwargs_by_graph = _ctor_kwargs(mock_es)
    assert kwargs_by_graph[_TUNABLE_MODELS[0].filename]["batchSize"] == 8
    assert kwargs_by_graph[_FIXED_BATCH_MODEL.filename]["batchSize"] == 64
