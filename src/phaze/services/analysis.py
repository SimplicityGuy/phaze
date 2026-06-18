"""Audio analysis service: model registry, essentia analysis, mood/style derivation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from statistics import mean, median
from typing import Any

import numpy as np


# Suppress TF C++ logging before any essentia/TF import
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import essentia
import essentia.standard as es


log = logging.getLogger(__name__)


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

_MOOD_SET_NAMES = frozenset(
    {
        "mood_acoustic",
        "mood_electronic",
        "mood_aggressive",
        "mood_relaxed",
        "mood_happy",
        "mood_sad",
        "mood_party",
    }
)


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


def derive_danceability(features: dict[str, Any]) -> float | None:
    """Derive a scalar danceability from the danceability model set.

    Averages the positive-class (first class = 'danceable') prediction across
    the 3 variants. Returns None if the danceability set is absent/empty.
    """
    set_data = features.get("danceability")
    if not set_data:
        return None

    scores: list[float] = []
    for _variant_name, predictions in set_data.items():
        if predictions:
            scores.append(float(predictions[0]["prediction"]))

    return sum(scores) / len(scores) if scores else None


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


# ---------------------------------------------------------------------------
# Main analysis function (synchronous, for ProcessPoolExecutor)
# ---------------------------------------------------------------------------

# Sample rates for the two analysis passes (locked by Plan 31-01 spike).
_FINE_SAMPLE_RATE = 44100
_COARSE_SAMPLE_RATE = 16000

# AgentSettings defaults (config.py). analyze_file accepts overrides so the
# agent worker can pass settings.analysis_* values; defaults mirror the config.
_DEFAULT_FINE_WINDOW_SEC = 30
_DEFAULT_COARSE_WINDOW_SEC = 180
_DEFAULT_FINE_MIN_SEC = 15

# Per-file cost caps (Phase 43): bound essentia work to a constant regardless of
# duration. A file whose natural window count exceeds the cap is strided evenly
# across the WHOLE file via ``_stride_to_cap`` rather than analyzed window-by-window.
_DEFAULT_FINE_CAP = 60
_DEFAULT_COARSE_CAP = 30


def _probe_duration_sec(file_path: str) -> float:
    """Return total audio duration in seconds WITHOUT materializing PCM.

    Uses ``es.MetadataReader`` (reads container/header metadata; it does NOT
    decode the full signal, unlike ``MonoLoader``). Output index 8 is the
    duration in seconds. A failure here is fatal (the file is unreadable) and
    propagates to the caller rather than being treated as a per-window skip.
    """
    metadata = es.MetadataReader(filename=file_path, filterMetadata=True)()
    return float(metadata[8])


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


def _stride_to_cap(windows: list[tuple[int, float, float]], cap: int) -> tuple[list[tuple[int, float, float]], bool]:
    """Even-stride ``windows`` down to ``<=cap`` entries, preserving original idx.

    Bounds per-file analysis cost to a constant regardless of duration: when a
    file's natural window count exceeds ``cap`` we sample evenly across the WHOLE
    file (first and last window always kept) instead of truncating to first-N.

    Returns ``(kept, sampled)``:
      * ``cap <= 0`` or ``len(windows) <= cap`` → ``(windows, False)`` unchanged.
      * otherwise → ``(kept, True)`` where ``kept`` retains each original tuple's
        idx (NO renumbering), is sorted ascending by idx, and never exceeds
        ``cap`` (rounding collisions dedup via a set, yielding ``<= cap``).

    Math: endpoint-inclusive even stride ``round(i * (n - 1) / (cap - 1))`` for
    ``i in 0..cap-1`` spans positions ``0 .. n-1`` so the first and last windows
    are always included (RESEARCH §Q2).
    """
    n = len(windows)
    if cap <= 0 or n <= cap:
        return windows, False
    picks = {round(i * (n - 1) / (cap - 1)) for i in range(cap)}  # set dedups rounding collisions
    kept = [windows[p] for p in sorted(picks)]
    return kept, True


def _run_model_sets(audio_16k: Any, models_dir: str) -> dict[str, Any]:
    """Run all 11 characteristic model sets + the genre model on one buffer.

    Identical prediction shape to the previous whole-file path, but fed a single
    coarse-window buffer instead of the whole file. Reuses the module-level
    ``_classifier_cache`` (inference-only; no per-window graph reload).
    """
    features: dict[str, Any] = {}
    for model_set in MODEL_SETS:
        set_data: dict[str, list[dict[str, Any]]] = {}
        for model in model_set.models:
            predictions = _predict_single(audio_16k, model, models_dir)
            labels = _get_labels(model.filename, models_dir)
            set_data[model.variant] = [{"label": label, "prediction": float(pred)} for label, pred in zip(labels, predictions, strict=False)]
        features[model_set.name] = set_data

    genre_predictions = _predict_single(audio_16k, GENRE_MODEL, models_dir)
    genre_labels = _get_labels(GENRE_MODEL.filename, models_dir)
    genre_pairs = list(zip(genre_labels, genre_predictions, strict=False))
    genre_pairs.sort(key=lambda pair: float(pair[1]), reverse=True)
    features["genre"] = {
        "predictions": [{"label": label, "confidence": float(conf)} for label, conf in genre_pairs[:10]],
    }
    return features


def _analyze_fine_windows(file_path: str, total_sec: float, win_sec: int, min_sec: int, cap: int) -> tuple[list[FineWindow], int, bool]:
    """FINE pass: BPM + key per ``win_sec`` window via segmented EasyLoader decode.

    Returns ``(windows, total, sampled)`` where ``total`` is the natural window
    count BEFORE striding and ``sampled`` is True when the cap forced an even
    stride. ``len(windows)`` (analyzed) counts successful appends; per-window
    failures are skipped, so it may be below the post-stride target.
    """
    natural = _iter_windows(total_sec, win_sec, min_sec, drop_short_trailing=True)
    kept, sampled = _stride_to_cap(natural, cap)
    fine_windows: list[FineWindow] = []
    for idx, start, end in kept:
        try:
            buf = es.EasyLoader(filename=file_path, sampleRate=_FINE_SAMPLE_RATE, startTime=start, endTime=end)()
            bpm, _beats, confidence, _, _beats_intervals = es.RhythmExtractor2013(method="multifeature")(buf)
            key, scale, _strength = es.KeyExtractor(profileType="edma")(buf)
            fine_windows.append(
                FineWindow(
                    window_index=idx,
                    start_sec=start,
                    end_sec=end,
                    bpm=round(float(bpm), 1),
                    musical_key=f"{key} {scale}",
                    confidence=float(confidence),
                )
            )
        except Exception:  # per-window failure isolation: skip, never fail the file
            log.warning("fine window %d [%.1f, %.1f) failed; skipping", idx, start, end, exc_info=True)
            continue
    return fine_windows, len(natural), sampled


def _analyze_coarse_windows(file_path: str, total_sec: float, win_sec: int, models_dir: str, cap: int) -> tuple[list[CoarseWindow], int, bool]:
    """COARSE pass: mood/style/danceability per ``win_sec`` window (no length floor).

    Returns ``(windows, total, sampled)`` mirroring ``_analyze_fine_windows``:
    ``total`` is the natural pre-stride count and ``sampled`` is True when the
    cap forced an even stride.
    """
    natural = _iter_windows(total_sec, win_sec, 0, drop_short_trailing=False)
    kept, sampled = _stride_to_cap(natural, cap)
    coarse_windows: list[CoarseWindow] = []
    for idx, start, end in kept:
        try:
            buf = es.EasyLoader(filename=file_path, sampleRate=_COARSE_SAMPLE_RATE, startTime=start, endTime=end)()
            features = _run_model_sets(buf, models_dir)
            coarse_windows.append(
                CoarseWindow(
                    window_index=idx,
                    start_sec=start,
                    end_sec=end,
                    mood=derive_mood(features),
                    style=derive_style(features["genre"]),
                    danceability=derive_danceability(features),
                    features=features,
                )
            )
        except Exception:  # per-window failure isolation: skip, never fail the file
            log.warning("coarse window %d [%.1f, %.1f) failed; skipping", idx, start, end, exc_info=True)
            continue
    return coarse_windows, len(natural), sampled


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


def analyze_file(
    file_path: str,
    models_dir: str,
    *,
    fine_window_sec: int = _DEFAULT_FINE_WINDOW_SEC,
    coarse_window_sec: int = _DEFAULT_COARSE_WINDOW_SEC,
    fine_min_sec: int = _DEFAULT_FINE_MIN_SEC,
    fine_cap: int = _DEFAULT_FINE_CAP,
    coarse_cap: int = _DEFAULT_COARSE_CAP,
) -> dict[str, Any]:
    """Analyze a single audio file via essentia as a two-tier time-series.

    The main synchronous function called from ``run_in_process_pool``. Instead of
    decoding the whole file into one buffer (the latent OOM) and feeding long
    audio to ``RhythmExtractor2013`` (the ``OnsetDetectionGlobal`` overflow), it
    decodes one short window at a time via segmented ``EasyLoader`` (Plan 31-01
    locked strategy) so no essentia algorithm ever sees more than one window.

    Two passes:
      * FINE (44.1 kHz): ``RhythmExtractor2013`` + ``KeyExtractor`` per
        ``fine_window_sec`` window; trailing windows shorter than
        ``fine_min_sec`` are dropped (except window 0).
      * COARSE (16 kHz): the 34 TF model sets per ``coarse_window_sec`` window;
        every window with audio is analyzed (no minimum-length floor).

    Per-window failures are logged and skipped — one bad window never fails the
    file. Window sizes default to the ``AgentSettings`` defaults (30/180/15) and
    may be overridden by the agent worker.

    To keep per-file cost constant regardless of duration, each pass is bounded
    by a cap (``fine_cap``/``coarse_cap``, defaults 60/30): a file whose natural
    window count exceeds the cap is strided EVENLY across the whole file instead
    of analyzed window-by-window (root cause of the 4h-timeout: cost was
    O(duration)). Under the cap, behavior is unchanged (every window analyzed).

    Returns a dict with the representative aggregates
    (``bpm``/``musical_key``/``mood``/``style``/``danceability``/``features``)
    PLUS ``windows``: a flat list of fine + coarse window dicts, each ready for
    ``AnalysisWindowPayload(**w)`` — PLUS a five-field coverage contract
    (``fine_windows_analyzed``/``fine_windows_total``/``coarse_windows_analyzed``/
    ``coarse_windows_total``/``sampled``) so a sampled file can be re-deepened
    later (Phase 44). ``*_total`` is the natural pre-stride window count;
    ``*_analyzed`` is the count actually analyzed (post-stride, minus per-window
    skips); ``sampled`` is True when either pass was strided.
    """
    _suppress_essentia_logging()

    total_sec = _probe_duration_sec(file_path)

    fine_windows, fine_total, fine_sampled = _analyze_fine_windows(file_path, total_sec, fine_window_sec, fine_min_sec, fine_cap)
    coarse_windows, coarse_total, coarse_sampled = _analyze_coarse_windows(file_path, total_sec, coarse_window_sec, models_dir, coarse_cap)

    windows: list[dict[str, Any]] = [w.as_payload_dict() for w in fine_windows]
    windows.extend(w.as_payload_dict() for w in coarse_windows)

    return {
        "bpm": aggregate_bpm(fine_windows),
        "musical_key": aggregate_key(fine_windows),
        "mood": aggregate_dominant(coarse_windows, "mood"),
        "style": aggregate_dominant(coarse_windows, "style"),
        "danceability": aggregate_danceability(coarse_windows),
        "features": _representative_features(coarse_windows),
        "windows": windows,
        "fine_windows_analyzed": len(fine_windows),
        "fine_windows_total": fine_total,
        "coarse_windows_analyzed": len(coarse_windows),
        "coarse_windows_total": coarse_total,
        "sampled": fine_sampled or coarse_sampled,
    }
