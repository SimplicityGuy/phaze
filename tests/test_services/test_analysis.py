"""Tests for the audio analysis service."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from phaze.services.analysis import (
    GENRE_MODEL,
    MODEL_SETS,
    ModelConfig,
    ModelSetConfig,
    analyze_file,
    derive_mood,
    derive_style,
)


# --- Model registry tests ---


def test_model_sets_count() -> None:
    """MODEL_SETS has exactly 11 entries."""
    assert len(MODEL_SETS) == 11


def test_model_sets_have_three_variants() -> None:
    """Each ModelSetConfig in MODEL_SETS has exactly 3 ModelConfig entries."""
    for model_set in MODEL_SETS:
        assert isinstance(model_set, ModelSetConfig), f"{model_set.name} is not ModelSetConfig"
        assert len(model_set.models) == 3, f"{model_set.name} has {len(model_set.models)} models, expected 3"
        for model in model_set.models:
            assert isinstance(model, ModelConfig), f"{model} is not ModelConfig"


def test_genre_model_exists() -> None:
    """GENRE_MODEL is a ModelConfig with name 'discogs_genre' and classifier_type 'effnet_discogs'."""
    assert isinstance(GENRE_MODEL, ModelConfig)
    assert GENRE_MODEL.name == "discogs_genre"
    assert GENRE_MODEL.classifier_type == "effnet_discogs"


# --- derive_mood tests ---


def test_derive_mood() -> None:
    """Given mood model set predictions where 'happy' has highest averaged positive-class confidence, derive_mood returns 'happy'."""
    features: dict[str, Any] = {
        "mood_acoustic": {
            "musicnn_msd": [{"label": "acoustic", "prediction": 0.2}, {"label": "not_acoustic", "prediction": 0.8}],
            "musicnn_mtt": [{"label": "acoustic", "prediction": 0.3}, {"label": "not_acoustic", "prediction": 0.7}],
            "vggish": [{"label": "acoustic", "prediction": 0.1}, {"label": "not_acoustic", "prediction": 0.9}],
        },
        "mood_electronic": {
            "musicnn_msd": [{"label": "electronic", "prediction": 0.4}, {"label": "not_electronic", "prediction": 0.6}],
            "musicnn_mtt": [{"label": "electronic", "prediction": 0.3}, {"label": "not_electronic", "prediction": 0.7}],
            "vggish": [{"label": "electronic", "prediction": 0.5}, {"label": "not_electronic", "prediction": 0.5}],
        },
        "mood_aggressive": {
            "musicnn_msd": [{"label": "aggressive", "prediction": 0.1}, {"label": "not_aggressive", "prediction": 0.9}],
            "musicnn_mtt": [{"label": "aggressive", "prediction": 0.2}, {"label": "not_aggressive", "prediction": 0.8}],
            "vggish": [{"label": "aggressive", "prediction": 0.1}, {"label": "not_aggressive", "prediction": 0.9}],
        },
        "mood_relaxed": {
            "musicnn_msd": [{"label": "relaxed", "prediction": 0.3}, {"label": "not_relaxed", "prediction": 0.7}],
            "musicnn_mtt": [{"label": "relaxed", "prediction": 0.4}, {"label": "not_relaxed", "prediction": 0.6}],
            "vggish": [{"label": "relaxed", "prediction": 0.2}, {"label": "not_relaxed", "prediction": 0.8}],
        },
        "mood_happy": {
            "musicnn_msd": [{"label": "happy", "prediction": 0.9}, {"label": "not_happy", "prediction": 0.1}],
            "musicnn_mtt": [{"label": "happy", "prediction": 0.8}, {"label": "not_happy", "prediction": 0.2}],
            "vggish": [{"label": "happy", "prediction": 0.85}, {"label": "not_happy", "prediction": 0.15}],
        },
        "mood_sad": {
            "musicnn_msd": [{"label": "sad", "prediction": 0.1}, {"label": "not_sad", "prediction": 0.9}],
            "musicnn_mtt": [{"label": "sad", "prediction": 0.2}, {"label": "not_sad", "prediction": 0.8}],
            "vggish": [{"label": "sad", "prediction": 0.15}, {"label": "not_sad", "prediction": 0.85}],
        },
        "mood_party": {
            "musicnn_msd": [{"label": "party", "prediction": 0.5}, {"label": "not_party", "prediction": 0.5}],
            "musicnn_mtt": [{"label": "party", "prediction": 0.6}, {"label": "not_party", "prediction": 0.4}],
            "vggish": [{"label": "party", "prediction": 0.4}, {"label": "not_party", "prediction": 0.6}],
        },
    }
    result = derive_mood(features)
    assert result == "happy"


# --- derive_style tests ---


def test_derive_style() -> None:
    """Given genre predictions where top label is 'Electronic---House', derive_style returns 'Electronic/House'."""
    genre_features: dict[str, Any] = {
        "predictions": [
            {"label": "Electronic/House", "confidence": 0.85},
            {"label": "Electronic/Techno", "confidence": 0.10},
            {"label": "Pop/Dance", "confidence": 0.05},
        ]
    }
    result = derive_style(genre_features)
    assert result == "Electronic/House"


def test_derive_style_replaces_triple_dash() -> None:
    """derive_style defensively replaces '---' with '/' in labels."""
    genre_features: dict[str, Any] = {
        "predictions": [
            {"label": "Electronic---House", "confidence": 0.9},
        ]
    }
    result = derive_style(genre_features)
    assert result == "Electronic/House"


# --- analyze_file tests (mocked essentia) ---


def _build_mock_essentia() -> MagicMock:
    """Build a mock essentia.standard module with all required classes."""
    mock_es = MagicMock()

    # MonoLoader returns a callable that returns a numpy array
    mock_loader_instance = MagicMock()
    mock_loader_instance.return_value = np.zeros(16000, dtype=np.float32)
    mock_es.MonoLoader.return_value = mock_loader_instance

    # RhythmExtractor2013 returns (bpm, beats, confidence, _, intervals)
    mock_rhythm = MagicMock()
    mock_rhythm.return_value = (128.0, np.array([0.5]), np.array([0.9]), np.array([]), np.array([0.5]))
    mock_es.RhythmExtractor2013.return_value = mock_rhythm

    # KeyExtractor returns (key, scale, strength)
    mock_key = MagicMock()
    mock_key.return_value = ("C", "minor", 0.8)
    mock_es.KeyExtractor.return_value = mock_key

    # TensorflowPredict* returns activations array (2D: frames x classes)
    for cls_name in ("TensorflowPredictMusiCNN", "TensorflowPredictVGGish", "TensorflowPredictEffnetDiscogs"):
        mock_cls = MagicMock()
        mock_instance = MagicMock()
        # Return a 2D array: 10 frames, 2 classes for characteristic models
        mock_instance.return_value = np.array([[0.7, 0.3]] * 10, dtype=np.float32)
        mock_cls.return_value = mock_instance
        setattr(mock_es, cls_name, mock_cls)

    return mock_es


def _mock_labels_file(model_filename: str, _models_dir: str) -> list[str]:
    """Return mock labels for any model file."""
    if "discogs" in model_filename:
        return [f"Genre{i}" for i in range(400)]
    return ["positive_class", "negative_class"]


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_returns_complete_result(_mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """analyze_file returns dict with keys bpm, musical_key, mood, style, features."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    assert "bpm" in result
    assert "musical_key" in result
    assert "mood" in result
    assert "style" in result
    assert "features" in result
    assert result["bpm"] == 128.0
    assert result["musical_key"] == "C minor"


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_features_has_all_model_sets(_mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """The features dict returned by analyze_file contains entries for all 11 model sets plus genre."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    features = result["features"]
    expected_set_names = {ms.name for ms in MODEL_SETS}
    expected_set_names.add("genre")
    for name in expected_set_names:
        assert name in features, f"Missing feature set: {name}"


@patch("phaze.services.analysis.es")
def test_analyze_file_raises_on_corrupt_file(mock_es: MagicMock) -> None:
    """analyze_file raises an exception (not swallowed) when essentia fails to load audio."""
    mock_loader_instance = MagicMock()
    mock_loader_instance.side_effect = RuntimeError("Corrupt audio file")
    mock_es.MonoLoader.return_value = mock_loader_instance

    with pytest.raises(RuntimeError, match="Corrupt audio file"):
        analyze_file("/fake/corrupt.mp3", "/fake/models")


# ---------------------------------------------------------------------------
# VALIDATION.md named tests — ANL-01 and ANL-02 behavioral coverage
# ---------------------------------------------------------------------------


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_detect_bpm(_mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """ANL-01: analyze_file detects BPM and returns it as a float in the result dict."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    assert "bpm" in result
    assert isinstance(result["bpm"], float)
    assert result["bpm"] == 128.0


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_bpm_stored(_mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """ANL-01: analyze_file returns bpm value that can be stored in AnalysisResult.bpm (Float column)."""
    from phaze.models.analysis import AnalysisResult

    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    # Construct an AnalysisResult with the returned bpm — verify it accepts the value
    import uuid

    ar = AnalysisResult(file_id=uuid.uuid4(), bpm=result["bpm"], musical_key=result["musical_key"])
    assert ar.bpm == 128.0
    assert ar.musical_key == "C minor"


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_classify_mood(_mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """ANL-02: analyze_file classifies mood using all 7 mood model sets and returns non-empty string."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    assert "mood" in result
    assert isinstance(result["mood"], str)
    assert len(result["mood"]) > 0


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_classify_style(_mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """ANL-02: analyze_file derives style from the discogs-effnet genre model and returns non-empty string."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    assert "style" in result
    assert isinstance(result["style"], str)
    assert len(result["style"]) > 0


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analysis_result_stored(_mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """ANL-02: analyze_file returns mood, style, features that can be stored in AnalysisResult (JSONB)."""
    from phaze.models.analysis import AnalysisResult

    mock_get_labels.side_effect = _mock_labels_file

    import uuid

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    ar = AnalysisResult(
        file_id=uuid.uuid4(),
        mood=result["mood"],
        style=result["style"],
        features=result["features"],
    )
    assert isinstance(ar.mood, str) and len(ar.mood) > 0
    assert isinstance(ar.style, str) and len(ar.style) > 0
    # features is a dict with all 11 model set names plus genre
    assert isinstance(ar.features, dict)
    assert "genre" in ar.features
    for model_set in MODEL_SETS:
        assert model_set.name in ar.features
