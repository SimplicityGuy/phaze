"""Tests for the audio analysis service."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from phaze.services.analysis import (
    GENRE_MODEL,
    MODEL_SETS,
    CoarseWindow,
    FineWindow,
    ModelConfig,
    ModelSetConfig,
    _stride_to_cap,
    aggregate_bpm,
    aggregate_danceability,
    aggregate_dominant,
    aggregate_key,
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


_MOCK_DURATION_SEC = 600.0  # 10 min -> 20 fine (30s) + 4 coarse (180s) windows


def _build_mock_essentia(duration_sec: float = _MOCK_DURATION_SEC) -> MagicMock:
    """Build a mock essentia.standard module with all required classes."""
    mock_es = MagicMock()

    # MetadataReader()() returns a 12-tuple; index 8 is duration in seconds.
    mock_metadata = MagicMock()
    mock_metadata.return_value = ("", "", "", "", "", "", "", MagicMock(), duration_sec, 128, 16000, 1)
    mock_es.MetadataReader.return_value = mock_metadata

    # MonoLoader / EasyLoader return a callable that returns a numpy array.
    mock_loader_instance = MagicMock()
    mock_loader_instance.return_value = np.zeros(16000, dtype=np.float32)
    mock_es.MonoLoader.return_value = mock_loader_instance
    mock_es.EasyLoader.return_value = mock_loader_instance

    # RhythmExtractor2013 returns (bpm, beats, confidence, _, intervals)
    mock_rhythm = MagicMock()
    mock_rhythm.return_value = (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5]))
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
    """analyze_file propagates (does not swallow) a fatal duration-probe failure.

    A whole-file-unreadable error surfaces at the ``_probe_duration_sec`` stage
    (es.MetadataReader) and is fatal — unlike per-window decode failures, which
    are isolated and skipped.
    """
    mock_es.MetadataReader.side_effect = RuntimeError("Corrupt audio file")

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


# ---------------------------------------------------------------------------
# Phase 43: _stride_to_cap even-stride downsampler (pure-Python, NO essentia)
# ---------------------------------------------------------------------------


def _win(idx: int) -> tuple[int, float, float]:
    """A synthetic (idx, start, end) window tuple with idx-derived bounds."""
    return (idx, float(idx) * 30.0, float(idx) * 30.0 + 30.0)


def test_stride_under_cap_is_noop() -> None:
    """len(windows) <= cap returns the windows unchanged with sampled False."""
    windows = [_win(i) for i in range(3)]
    kept, sampled = _stride_to_cap(windows, 5)
    assert kept == windows
    assert sampled is False


def test_stride_equal_to_cap_is_noop() -> None:
    """len(windows) == cap is the boundary: no striding, sampled False."""
    windows = [_win(i) for i in range(5)]
    kept, sampled = _stride_to_cap(windows, 5)
    assert kept == windows
    assert sampled is False


def test_stride_cap_le_zero_is_noop() -> None:
    """cap <= 0 returns the windows unchanged with sampled False (guard)."""
    windows = [_win(i) for i in range(10)]
    kept, sampled = _stride_to_cap(windows, 0)
    assert kept == windows
    assert sampled is False
    kept_neg, sampled_neg = _stride_to_cap(windows, -3)
    assert kept_neg == windows
    assert sampled_neg is False


def test_stride_over_cap_bounds_count_and_sets_sampled() -> None:
    """len(windows) > cap yields len(kept) <= cap and sampled True."""
    windows = [_win(i) for i in range(100)]
    kept, sampled = _stride_to_cap(windows, 60)
    assert sampled is True
    assert len(kept) <= 60


def test_stride_keeps_first_and_last() -> None:
    """The first and last original windows are always retained (whole-file span)."""
    windows = [_win(i) for i in range(100)]
    kept, _sampled = _stride_to_cap(windows, 60)
    assert kept[0] == windows[0]
    assert kept[-1] == windows[-1]


def test_stride_preserves_original_index_no_renumber() -> None:
    """Kept tuples retain their ORIGINAL idx; nothing is renumbered to 0..k-1."""
    windows = [_win(i) for i in range(100)]
    kept, _sampled = _stride_to_cap(windows, 60)
    # Each kept tuple must be identical to the original window at its idx.
    for idx, start, end in kept:
        assert (idx, start, end) == windows[idx]
    # The kept indices are a strict subset that is NOT a contiguous 0..k-1 range.
    kept_indices = [w[0] for w in kept]
    assert kept_indices != list(range(len(kept)))


def test_stride_sorted_ascending_by_index() -> None:
    """Kept windows are sorted ascending by original idx."""
    windows = [_win(i) for i in range(57)]
    kept, _sampled = _stride_to_cap(windows, 30)
    kept_indices = [w[0] for w in kept]
    assert kept_indices == sorted(kept_indices)


def test_stride_evenly_spaced() -> None:
    """Picks are approximately evenly spaced across the whole file."""
    windows = [_win(i) for i in range(101)]  # n=101, cap=11 -> step 10
    kept, sampled = _stride_to_cap(windows, 11)
    assert sampled is True
    assert [w[0] for w in kept] == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


# ---------------------------------------------------------------------------
# Phase 31: aggregate-reduction unit tests (pure-Python, NO essentia mock)
# ---------------------------------------------------------------------------


def _fine(idx: int, bpm: float | None, key: str | None, *, start: float = 0.0, end: float = 30.0, confidence: float = 3.8) -> FineWindow:
    return FineWindow(window_index=idx, start_sec=start, end_sec=end, bpm=bpm, musical_key=key, confidence=confidence)


def _coarse(
    idx: int,
    mood: str | None,
    style: str | None,
    dance: float | None,
    *,
    start: float = 0.0,
    end: float = 180.0,
) -> CoarseWindow:
    return CoarseWindow(window_index=idx, start_sec=start, end_sec=end, mood=mood, style=style, danceability=dance, features={})


def test_aggregate_bpm_median() -> None:
    """aggregate_bpm returns the median of fine-window BPMs rounded to 0.1."""
    fine = [_fine(0, 120.0, "C major"), _fine(1, 124.0, "C major"), _fine(2, 121.0, "C major")]
    assert aggregate_bpm(fine) == 121.0


def test_aggregate_bpm_empty_returns_none() -> None:
    """aggregate_bpm on no usable windows returns None."""
    assert aggregate_bpm([]) is None


def test_aggregate_bpm_excludes_zero_confidence() -> None:
    """A confidence==0.0 window (unreliable BPM, Pitfall 2) is excluded from the median."""
    fine = [_fine(0, 120.0, "C major"), _fine(1, 999.0, "C major", confidence=0.0)]
    assert aggregate_bpm(fine) == 120.0


def test_aggregate_bpm_excludes_none_bpm() -> None:
    """Windows with bpm=None are excluded from the median."""
    fine = [_fine(0, 120.0, "C major"), _fine(1, None, "C major")]
    assert aggregate_bpm(fine) == 120.0


def test_aggregate_key_duration_weighted() -> None:
    """aggregate_key returns the key with the most total window duration."""
    # 'A minor' covers 60s across two windows; 'C major' covers 30s.
    fine = [
        _fine(0, 120.0, "C major", start=0.0, end=30.0),
        _fine(1, 120.0, "A minor", start=30.0, end=60.0),
        _fine(2, 120.0, "A minor", start=60.0, end=90.0),
    ]
    assert aggregate_key(fine) == "A minor"


def test_aggregate_key_empty_returns_none() -> None:
    """aggregate_key on empty input returns None."""
    assert aggregate_key([]) is None


def test_aggregate_dominant_time_weighted() -> None:
    """aggregate_dominant returns the time-weighted dominant label for the attr."""
    coarse = [
        _coarse(0, "happy", "house", 1.0, start=0.0, end=180.0),
        _coarse(1, "sad", "techno", 1.0, start=180.0, end=360.0),
        _coarse(2, "sad", "techno", 1.0, start=360.0, end=540.0),
    ]
    assert aggregate_dominant(coarse, "mood") == "sad"
    assert aggregate_dominant(coarse, "style") == "techno"


def test_aggregate_dominant_empty_returns_none() -> None:
    """aggregate_dominant on empty input returns None."""
    assert aggregate_dominant([], "mood") is None


def test_aggregate_danceability_mean() -> None:
    """aggregate_danceability returns the mean of coarse-window danceability values."""
    coarse = [_coarse(0, "happy", "house", 0.2), _coarse(1, "happy", "house", 0.4)]
    assert aggregate_danceability(coarse) == pytest.approx(0.3)


def test_aggregate_danceability_empty_returns_none() -> None:
    """aggregate_danceability on empty input returns None."""
    assert aggregate_danceability([]) is None


# ---------------------------------------------------------------------------
# Phase 31: per-window analyze_file behavior (mocked essentia)
# ---------------------------------------------------------------------------


def _fine_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in result["windows"] if w["tier"] == "fine"]


def _coarse_dicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [w for w in result["windows"] if w["tier"] == "coarse"]


@patch("phaze.services.analysis._probe_duration_sec", return_value=65.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_window_boundaries(mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """Fine windows tile at [0,30),[30,60); the 5s trailing window is dropped. Coarse is one [0,65)."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    fine = _fine_dicts(result)
    coarse = _coarse_dicts(result)
    assert [(w["start_sec"], w["end_sec"]) for w in fine] == [(0.0, 30.0), (30.0, 60.0)]
    assert [(w["start_sec"], w["end_sec"]) for w in coarse] == [(0.0, 65.0)]
    # No whole-file MonoLoader decode; segmented EasyLoader is used instead.
    mock_es.MonoLoader.assert_not_called()
    assert mock_es.EasyLoader.called


@patch("phaze.services.analysis._probe_duration_sec", return_value=190.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_trailing_policy_is_asymmetric(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """A sub-15s trailing FINE window is dropped, but the COARSE tier keeps its short trailing window."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    fine = _fine_dicts(result)
    coarse = _coarse_dicts(result)
    # Fine: [180,190) is 10s < 15 -> dropped; last fine window ends at 180.
    assert max(w["end_sec"] for w in fine) == 180.0
    # Coarse: no floor -> [0,180) + [180,190) both kept; last coarse window ends at 190.
    assert len(coarse) == 2
    assert max(w["end_sec"] for w in coarse) == 190.0


@patch("phaze.services.analysis._probe_duration_sec", return_value=10.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_keeps_short_window_zero(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """A very short track (10s < fine_min_sec) still yields one fine window (window 0 exception)."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    fine = _fine_dicts(result)
    assert len(fine) == 1
    assert (fine[0]["start_sec"], fine[0]["end_sec"]) == (0.0, 10.0)


@patch("phaze.services.analysis._probe_duration_sec", return_value=90.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_failure_isolation(mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """A window whose RhythmExtractor2013 raises is skipped; remaining windows + aggregates still return."""
    mock_get_labels.side_effect = _mock_labels_file
    # 90s -> 3 fine windows; make the 2nd raise (e.g. OnsetDetectionGlobal overflow).
    mock_es.RhythmExtractor2013.return_value.side_effect = [
        (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5])),
        RuntimeError("OnsetDetectionGlobal overflow"),
        (128.0, np.array([0.5]), 3.8, np.array([]), np.array([0.5])),
    ]

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    fine = _fine_dicts(result)
    assert len(fine) == 2, "the failed middle window must be skipped, not crash the file"
    # Aggregates are still produced from the surviving windows.
    assert result["bpm"] == 128.0


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_coarse_failure_isolation(mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """All coarse windows failing (decode error) is isolated: fine windows + aggregates still return."""
    mock_get_labels.side_effect = _mock_labels_file

    fine_loader = MagicMock()
    fine_loader.return_value = np.zeros(16000, dtype=np.float32)

    def _easyloader(*, filename: str, sampleRate: int, startTime: float, endTime: float) -> MagicMock:
        if sampleRate == 16000:  # coarse pass
            raise RuntimeError("coarse decode failed")
        return fine_loader

    mock_es.EasyLoader.side_effect = _easyloader

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    # Every coarse window was skipped; the fine tier + its aggregates survive.
    assert _coarse_dicts(result) == []
    assert len(_fine_dicts(result)) == 20
    assert result["bpm"] == 128.0
    # Empty coarse tier -> None mood/style/danceability and empty features blob.
    assert result["mood"] is None
    assert result["style"] is None
    assert result["danceability"] is None
    assert result["features"] == {}


def test_derive_danceability_returns_none_when_absent() -> None:
    """derive_danceability returns None when the danceability model set is absent."""
    from phaze.services.analysis import derive_danceability

    assert derive_danceability({}) is None


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_return_shape_has_windows(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """The return dict carries all aggregate keys PLUS a flat fine+coarse windows list."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    for key in ("bpm", "musical_key", "mood", "style", "danceability", "features", "windows"):
        assert key in result, f"missing aggregate/return key: {key}"

    # 600s -> 20 fine (30s) + 4 coarse (180/180/180/60) windows.
    assert len(_fine_dicts(result)) == 20
    assert len(_coarse_dicts(result)) == 4
    # Every window dict is ready for AnalysisWindowPayload(**w).
    for w in result["windows"]:
        assert {"tier", "window_index", "start_sec", "end_sec"} <= set(w)
        if w["tier"] == "fine":
            assert {"bpm", "musical_key"} <= set(w)
        else:
            assert {"mood", "style", "danceability", "features"} <= set(w)


# ---------------------------------------------------------------------------
# Phase 43: cap-bounded coverage emit (mocked essentia)
# ---------------------------------------------------------------------------

_COVERAGE_KEYS = ("fine_windows_analyzed", "fine_windows_total", "coarse_windows_analyzed", "coarse_windows_total", "sampled")


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_coverage_under_cap(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """Under the cap, every window is analyzed: analyzed == total and sampled is False."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    for key in _COVERAGE_KEYS:
        assert key in result, f"missing coverage key: {key}"
    # 600s -> 20 fine (<=60) + 4 coarse (<=30); nothing strided.
    assert result["fine_windows_total"] == 20
    assert result["fine_windows_analyzed"] == 20
    assert result["coarse_windows_total"] == 4
    assert result["coarse_windows_analyzed"] == 4
    assert result["sampled"] is False
    # analyzed counts equal the emitted window lists.
    assert result["fine_windows_analyzed"] == len(_fine_dicts(result))
    assert result["coarse_windows_analyzed"] == len(_coarse_dicts(result))


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_coverage_over_cap_strides(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """Over the cap, analyzed <= cap, total is the natural pre-stride count, sampled is True."""
    mock_get_labels.side_effect = _mock_labels_file

    # 600s -> 20 fine / 4 coarse naturally; force striding via small caps.
    result = analyze_file("/fake/audio.mp3", "/fake/models", fine_cap=5, coarse_cap=2)

    assert result["fine_windows_total"] == 20  # natural count, BEFORE stride
    assert result["fine_windows_analyzed"] <= 5
    assert len(_fine_dicts(result)) <= 5
    assert result["coarse_windows_total"] == 4
    assert result["coarse_windows_analyzed"] <= 2
    assert len(_coarse_dicts(result)) <= 2
    assert result["sampled"] is True
    # Whole-file span: first and last natural window indices are retained.
    fine_indices = [w["window_index"] for w in _fine_dicts(result)]
    assert 0 in fine_indices
    assert 19 in fine_indices


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_sampled_true_if_either_pass_strided(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """sampled is the OR of the two passes: striding only the fine pass still flips it True."""
    mock_get_labels.side_effect = _mock_labels_file

    # fine over cap (5 < 20), coarse under cap (30 > 4).
    result = analyze_file("/fake/audio.mp3", "/fake/models", fine_cap=5)

    assert result["fine_windows_analyzed"] <= 5
    assert result["coarse_windows_analyzed"] == 4
    assert result["sampled"] is True


def test_aggregates_valid_over_strided_subset() -> None:
    """Aggregations over a strided subset equal the direct reduction over that subset.

    No algorithm needs contiguous windows: aggregate_* are order-independent
    reductions, so sampling the windows keeps the aggregate well-defined.
    """
    from statistics import median

    fine = [_fine(i, 120.0 + i, "C major", start=float(i) * 30.0, end=float(i) * 30.0 + 30.0) for i in range(100)]
    tuples = [(w.window_index, w.start_sec, w.end_sec) for w in fine]
    kept_tuples, sampled = _stride_to_cap(tuples, 60)
    assert sampled is True
    kept_idx = {t[0] for t in kept_tuples}
    subset = [w for w in fine if w.window_index in kept_idx]

    expected = round(median([w.bpm for w in subset if w.bpm is not None]), 1)
    assert aggregate_bpm(subset) == expected
