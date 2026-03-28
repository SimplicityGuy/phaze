"""Audio analysis service: model registry, essentia analysis, mood/style derivation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import numpy as np


# Suppress TF C++ logging before any essentia/TF import
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import essentia  # type: ignore[import-untyped]
import essentia.standard as es  # type: ignore[import-untyped]


# ---------------------------------------------------------------------------
# Type definitions for model registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """Configuration for a single ML model file."""

    name: str  # e.g., "mood_acoustic"
    variant: str  # e.g., "musicnn_msd", "musicnn_mtt", "vggish"
    filename: str  # e.g., "mood_acoustic-musicnn-msd-2" (no extension)
    classifier_type: str  # "musicnn", "vggish", "effnet_discogs"


@dataclass(frozen=True)
class ModelSetConfig:
    """A set of model variants for one characteristic."""

    name: str
    models: tuple[ModelConfig, ...]


# ---------------------------------------------------------------------------
# Model registry: 11 characteristic model sets (33 models) per D-02
# ---------------------------------------------------------------------------


def _make_standard_set(name: str, filename_prefix: str) -> ModelSetConfig:
    """Create a model set with the standard 3 variants (musicnn_msd-2, musicnn_mtt-2, vggish-1)."""
    return ModelSetConfig(
        name=name,
        models=(
            ModelConfig(name=name, variant="musicnn_msd", filename=f"{filename_prefix}-musicnn-msd-2", classifier_type="musicnn"),
            ModelConfig(name=name, variant="musicnn_mtt", filename=f"{filename_prefix}-musicnn-mtt-2", classifier_type="musicnn"),
            ModelConfig(name=name, variant="vggish", filename=f"{filename_prefix}-vggish-audioset-1", classifier_type="vggish"),
        ),
    )


MODEL_SETS: tuple[ModelSetConfig, ...] = (
    _make_standard_set("mood_acoustic", "mood_acoustic"),
    _make_standard_set("mood_electronic", "mood_electronic"),
    _make_standard_set("mood_aggressive", "mood_aggressive"),
    _make_standard_set("mood_relaxed", "mood_relaxed"),
    _make_standard_set("mood_happy", "mood_happy"),
    _make_standard_set("mood_sad", "mood_sad"),
    _make_standard_set("mood_party", "mood_party"),
    _make_standard_set("danceability", "danceability"),
    _make_standard_set("gender", "gender"),
    _make_standard_set("tonality", "tonal_atonal"),
    # voice_instrumental uses musicnn-msd-1 (not -2), per prototype
    ModelSetConfig(
        name="voice_instrumental",
        models=(
            ModelConfig(name="voice_instrumental", variant="musicnn_msd", filename="voice_instrumental-musicnn-msd-1", classifier_type="musicnn"),
            ModelConfig(name="voice_instrumental", variant="musicnn_mtt", filename="voice_instrumental-musicnn-mtt-2", classifier_type="musicnn"),
            ModelConfig(name="voice_instrumental", variant="vggish", filename="voice_instrumental-vggish-audioset-1", classifier_type="vggish"),
        ),
    ),
)

GENRE_MODEL = ModelConfig(
    name="discogs_genre",
    variant="effnet",
    filename="discogs-effnet-bs64-1",
    classifier_type="effnet_discogs",
)


# ---------------------------------------------------------------------------
# Module-level caches for lazy loading in ProcessPoolExecutor workers
# ---------------------------------------------------------------------------

_classifier_cache: dict[str, Any] = {}
_labels_cache: dict[str, list[str]] = {}
_essentia_logging_suppressed = False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _suppress_essentia_logging() -> None:
    """Suppress essentia info/warning logging (safe to call multiple times)."""
    global _essentia_logging_suppressed
    if not _essentia_logging_suppressed:
        essentia.log.infoActive = False
        essentia.log.warningActive = False
        _essentia_logging_suppressed = True


def _get_classifier(model: ModelConfig, models_dir: str) -> Any:
    """Get or create a cached classifier instance for the given model."""
    if model.filename in _classifier_cache:
        return _classifier_cache[model.filename]

    graph_path = str(Path(models_dir) / (model.filename + ".pb"))

    if model.classifier_type == "musicnn":
        classifier = es.TensorflowPredictMusiCNN(graphFilename=graph_path)
    elif model.classifier_type == "vggish":
        classifier = es.TensorflowPredictVGGish(graphFilename=graph_path)
    elif model.classifier_type == "effnet_discogs":
        classifier = es.TensorflowPredictEffnetDiscogs(graphFilename=graph_path)
    else:
        msg = f"Unknown classifier type: {model.classifier_type}"
        raise ValueError(msg)

    _classifier_cache[model.filename] = classifier
    return classifier


def _get_labels(model_filename: str, models_dir: str) -> list[str]:
    """Get or load cached labels for the given model file."""
    if model_filename in _labels_cache:
        return _labels_cache[model_filename]

    json_path = Path(models_dir) / (model_filename + ".json")
    with json_path.open() as f:
        metadata = json.load(f)

    labels = [label.replace("---", "/") for label in metadata["classes"]]
    _labels_cache[model_filename] = labels
    return labels


def _predict_single(audio_16k: Any, model: ModelConfig, models_dir: str) -> Any:
    """Run a single model prediction and return mean activations."""
    classifier = _get_classifier(model, models_dir)
    activations = classifier(audio_16k)
    return np.mean(activations, axis=0)


# ---------------------------------------------------------------------------
# Mood / style derivation
# ---------------------------------------------------------------------------

_MOOD_SET_NAMES = frozenset({
    "mood_acoustic",
    "mood_electronic",
    "mood_aggressive",
    "mood_relaxed",
    "mood_happy",
    "mood_sad",
    "mood_party",
})


def derive_mood(features: dict[str, Any]) -> str:
    """Derive dominant mood from feature predictions.

    For each mood model set, average the positive-class prediction (first class)
    across the 3 variants. Return the mood name (without 'mood_' prefix) with
    the highest averaged confidence.
    """
    best_mood = ""
    best_score = -1.0

    for set_name in _MOOD_SET_NAMES:
        if set_name not in features:
            continue

        variant_scores: list[float] = []
        for _variant_name, predictions in features[set_name].items():
            if predictions:
                # First class = positive class (binary classifier)
                variant_scores.append(float(predictions[0]["prediction"]))

        if variant_scores:
            avg_score = sum(variant_scores) / len(variant_scores)
            if avg_score > best_score:
                best_score = avg_score
                best_mood = set_name

    # Strip "mood_" prefix
    return best_mood.removeprefix("mood_")


def derive_style(genre_features: dict[str, Any]) -> str:
    """Derive top style/genre from genre model predictions.

    Returns the label of the highest-confidence genre prediction.
    Defensively replaces '---' with '/' in labels.
    """
    predictions = genre_features.get("predictions", [])
    if not predictions:
        return "unknown"

    top = max(predictions, key=lambda p: p["confidence"])
    return str(top["label"]).replace("---", "/")


# ---------------------------------------------------------------------------
# Main analysis function (synchronous, for ProcessPoolExecutor)
# ---------------------------------------------------------------------------


def analyze_file(file_path: str, models_dir: str) -> dict[str, Any]:
    """Analyze a single audio file via essentia.

    This is the main synchronous function called from run_in_process_pool.
    It runs BPM/key detection at 44.1kHz and all TF model predictions at 16kHz.

    Returns a dict with: bpm, musical_key, mood, style, features (JSONB-ready).
    """
    _suppress_essentia_logging()

    # 1. Load audio at 44.1kHz for BPM and key detection
    audio_44k = es.MonoLoader(filename=file_path, sampleRate=44100)()

    # 2. Detect BPM
    rhythm = es.RhythmExtractor2013(method="multifeature")
    bpm, _beats, _beats_confidence, _, _beats_intervals = rhythm(audio_44k)

    # 3. Detect key
    key_ext = es.KeyExtractor(profileType="edma")
    key, scale, _strength = key_ext(audio_44k)
    musical_key = f"{key} {scale}"

    # 4. Load audio at 16kHz for TF model predictions
    audio_16k = es.MonoLoader(filename=file_path, sampleRate=16000)()

    # 5. Run all 11 model sets (33 models)
    features: dict[str, Any] = {}
    for model_set in MODEL_SETS:
        set_data: dict[str, list[dict[str, Any]]] = {}
        for model in model_set.models:
            predictions = _predict_single(audio_16k, model, models_dir)
            labels = _get_labels(model.filename, models_dir)
            set_data[model.variant] = [
                {"label": label, "prediction": float(pred)} for label, pred in zip(labels, predictions, strict=False)
            ]
        features[model_set.name] = set_data

    # 6. Run genre model
    genre_predictions = _predict_single(audio_16k, GENRE_MODEL, models_dir)
    genre_labels = _get_labels(GENRE_MODEL.filename, models_dir)
    genre_pairs = list(zip(genre_labels, genre_predictions, strict=False))
    genre_pairs.sort(key=lambda pair: float(pair[1]), reverse=True)
    features["genre"] = {
        "predictions": [{"label": label, "confidence": float(conf)} for label, conf in genre_pairs[:10]],
    }

    # 7. Derive mood and style
    mood = derive_mood(features)
    style = derive_style(features["genre"])

    return {
        "bpm": round(float(bpm), 1),
        "musical_key": musical_key,
        "mood": mood,
        "style": style,
        "features": features,
    }
