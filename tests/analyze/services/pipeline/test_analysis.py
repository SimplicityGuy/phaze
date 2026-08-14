"""Tests for the audio analysis service."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import phaze.services.analysis as analysis_mod
from phaze.services.analysis import (
    GENRE_MODEL,
    MODEL_SETS,
    AnalysisDecodeError,
    AnalysisProbeError,
    CoarseWindow,
    FineWindow,
    ModelConfig,
    ModelSetConfig,
    _chunk_stop_sec,
    _chunked,
    _get_classifier,
    _get_labels,
    _peak_rss_gib,
    _positive_class_prediction,
    _resolve_malloc_trim,
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
    """A quiet/relaxed track resolves to 'relaxed' when the positive class is picked by LABEL.

    phaze-c8zi: the class lists here mirror the REAL essentia metadata order, which is
    ALPHABETICAL — so mood_relaxed/mood_sad/mood_party list their NEGATIVE class FIRST
    (['non_relaxed', 'relaxed'] etc.) while acoustic/electronic/aggressive/happy list
    the positive class first. This fixture is a strong regression: the old
    ``predictions[0]`` code would score P(non_party)=0.9 as 'party' and wrongly return
    'party'; label-based selection correctly returns 'relaxed' (its positive prob 0.9).
    """
    features: dict[str, Any] = {
        # Positive class FIRST (matches real metadata order for these sets).
        "mood_acoustic": {
            "musicnn_msd": [{"label": "acoustic", "prediction": 0.6}, {"label": "non_acoustic", "prediction": 0.4}],
            "musicnn_mtt": [{"label": "acoustic", "prediction": 0.6}, {"label": "non_acoustic", "prediction": 0.4}],
            "vggish": [{"label": "acoustic", "prediction": 0.6}, {"label": "non_acoustic", "prediction": 0.4}],
        },
        "mood_electronic": {
            "musicnn_msd": [{"label": "electronic", "prediction": 0.1}, {"label": "non_electronic", "prediction": 0.9}],
            "musicnn_mtt": [{"label": "electronic", "prediction": 0.1}, {"label": "non_electronic", "prediction": 0.9}],
            "vggish": [{"label": "electronic", "prediction": 0.1}, {"label": "non_electronic", "prediction": 0.9}],
        },
        "mood_aggressive": {
            "musicnn_msd": [{"label": "aggressive", "prediction": 0.05}, {"label": "not_aggressive", "prediction": 0.95}],
            "musicnn_mtt": [{"label": "aggressive", "prediction": 0.05}, {"label": "not_aggressive", "prediction": 0.95}],
            "vggish": [{"label": "aggressive", "prediction": 0.05}, {"label": "not_aggressive", "prediction": 0.95}],
        },
        "mood_happy": {
            "musicnn_msd": [{"label": "happy", "prediction": 0.2}, {"label": "non_happy", "prediction": 0.8}],
            "musicnn_mtt": [{"label": "happy", "prediction": 0.2}, {"label": "non_happy", "prediction": 0.8}],
            "vggish": [{"label": "happy", "prediction": 0.2}, {"label": "non_happy", "prediction": 0.8}],
        },
        # NEGATIVE class FIRST (real alphabetical metadata order for these sets).
        "mood_relaxed": {
            "musicnn_msd": [{"label": "non_relaxed", "prediction": 0.1}, {"label": "relaxed", "prediction": 0.9}],
            "musicnn_mtt": [{"label": "non_relaxed", "prediction": 0.1}, {"label": "relaxed", "prediction": 0.9}],
            "vggish": [{"label": "non_relaxed", "prediction": 0.1}, {"label": "relaxed", "prediction": 0.9}],
        },
        "mood_sad": {
            "musicnn_msd": [{"label": "non_sad", "prediction": 0.7}, {"label": "sad", "prediction": 0.3}],
            "musicnn_mtt": [{"label": "non_sad", "prediction": 0.7}, {"label": "sad", "prediction": 0.3}],
            "vggish": [{"label": "non_sad", "prediction": 0.7}, {"label": "sad", "prediction": 0.3}],
        },
        "mood_party": {
            "musicnn_msd": [{"label": "non_party", "prediction": 0.9}, {"label": "party", "prediction": 0.1}],
            "musicnn_mtt": [{"label": "non_party", "prediction": 0.9}, {"label": "party", "prediction": 0.1}],
            "vggish": [{"label": "non_party", "prediction": 0.9}, {"label": "party", "prediction": 0.1}],
        },
    }
    result = derive_mood(features)
    assert result == "relaxed", f"expected positive-class winner 'relaxed'; got {result!r} (inverted index would pick 'party')"


def test_derive_mood_scores_each_mood_by_its_positive_class() -> None:
    """phaze-c8zi: every mood is scored by its POSITIVE class, regardless of list position.

    For each mood set in isolation, a HIGH positive-class probability must make that
    mood the winner — even when the positive class is listed SECOND (relaxed/sad/party).
    Under the old ``predictions[0]`` code, relaxed/sad/party would score their negative
    class and never win on their own positive signal.
    """
    negative_first = {"mood_relaxed": "relaxed", "mood_sad": "sad", "mood_party": "party"}
    positive_first = {"mood_acoustic": "acoustic", "mood_electronic": "electronic", "mood_aggressive": "aggressive", "mood_happy": "happy"}

    for set_name, mood in {**negative_first, **positive_first}.items():
        positive_label = mood
        # Build the class list in the REAL order: negative-first sets list non_/not_ first.
        neg_label = f"non_{mood}" if set_name in negative_first else (f"not_{mood}" if mood == "aggressive" else f"non_{mood}")
        if set_name in negative_first:
            pair = [{"label": neg_label, "prediction": 0.05}, {"label": positive_label, "prediction": 0.95}]
        else:
            pair = [{"label": positive_label, "prediction": 0.95}, {"label": neg_label, "prediction": 0.05}]
        features: dict[str, Any] = {set_name: {"musicnn_msd": pair, "musicnn_mtt": pair, "vggish": pair}}
        assert derive_mood(features) == mood, f"{set_name}: positive class {positive_label!r} (0.95) must win"


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


@pytest.fixture(autouse=True)
def _stub_duration_probe() -> Any:
    """phaze-l832u: ``_probe_duration_sec`` is a REAL ``ffprobe`` subprocess now, not essentia.

    Every test in this module hands ``analyze_file`` a synthetic path with a mocked essentia
    behind it. The duration used to come out of that mock (``es.MetadataReader``); since D-10 it
    comes from ffprobe, which would go looking for a file that does not exist. Stubbing the
    probe -- rather than the binary -- keeps these tests exactly as mocked as they were, and
    leaves the real probe to the tests that actually exercise it on real audio
    (``test_extraction_analysis_handoff.py``). A test that needs a different duration still
    overrides this with its own ``@patch(..., return_value=...)``, which shadows this fixture.
    """
    with patch("phaze.services.analysis._probe_duration_sec", return_value=_MOCK_DURATION_SEC):
        yield


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
def test_analyze_file_hoists_fine_extractors_out_of_the_window_loop(mock_es: MagicMock, mock_get_labels: MagicMock) -> None:
    """phaze-ap8y: RhythmExtractor2013/KeyExtractor are constructed ONCE per file, not per window.

    ``_MOCK_DURATION_SEC`` (600s) yields 20 fine windows (VALIDATION.md's own comment on
    ``_build_mock_essentia``), so a per-window construction would show 20 calls to each
    constructor; a per-file hoist shows exactly 1. This is the regression guard for the
    hoist phaze-i93a measured at -24.0% of the fine tier.
    """
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    assert result["fine_windows_analyzed"] == 20, "expected 20 fine windows from _MOCK_DURATION_SEC=600s"
    assert mock_es.RhythmExtractor2013.call_count == 1, (
        f"RhythmExtractor2013 constructed {mock_es.RhythmExtractor2013.call_count} times; expected 1 (once per file, not per window)"
    )
    assert mock_es.KeyExtractor.call_count == 1, (
        f"KeyExtractor constructed {mock_es.KeyExtractor.call_count} times; expected 1 (once per file, not per window)"
    )
    # The single shared instance must still be CALLED once per analyzed window.
    assert mock_es.RhythmExtractor2013.return_value.call_count == 20
    assert mock_es.KeyExtractor.return_value.call_count == 20


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
def test_analyze_file_raises_on_corrupt_file(_mock_es: MagicMock, _stub_duration_probe: Any) -> None:
    """analyze_file propagates (does not swallow) a fatal duration-probe failure.

    A whole-file-unreadable error surfaces at the ``_probe_duration_sec`` stage and is fatal —
    unlike per-window decode failures, which are isolated and skipped. phaze-l832u moved the
    probe from ``es.MetadataReader`` to ``ffprobe`` (D-10) and gave the failure its own type;
    the CONTRACT under test is unchanged, which is the point of keeping this test.
    """
    with (
        patch("phaze.services.analysis._probe_duration_sec", side_effect=AnalysisProbeError("ffprobe failed: Invalid data found")),
        pytest.raises(AnalysisProbeError, match="Invalid data found"),
    ):
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
# phaze-w55w1: window CHUNKING (pure-Python, NO essentia). The Phase 43
# `_stride_to_cap` even-stride downsampler these tests replace is gone with the
# caps -- analysis is exhaustive, so the property under test flipped from "bound
# the window count" to "never drop a window while bounding what is held at once".
# ---------------------------------------------------------------------------


def _win(idx: int) -> tuple[int, float, float]:
    """A synthetic (idx, start, end) window tuple with idx-derived bounds."""
    return (idx, float(idx) * 30.0, float(idx) * 30.0 + 30.0)


def test_chunked_partitions_without_losing_or_reordering_windows() -> None:
    """The concatenated chunks are the input, exactly: no window dropped, none reordered."""
    windows = [_win(i) for i in range(101)]

    chunks = _chunked(windows, 30)

    assert [w for chunk in chunks for w in chunk] == windows


def test_chunked_bounds_every_chunk_by_the_chunk_size() -> None:
    """Every chunk is at most `size` long -- the property the memory envelope rests on."""
    windows = [_win(i) for i in range(101)]

    chunks = _chunked(windows, 30)

    assert [len(c) for c in chunks] == [30, 30, 30, 11]
    assert all(len(c) <= 30 for c in chunks)


def test_chunked_exact_multiple_emits_no_empty_trailing_chunk() -> None:
    """An exact multiple yields full chunks only -- an empty trailer would decode nothing, twice."""
    windows = [_win(i) for i in range(60)]

    assert [len(c) for c in _chunked(windows, 30)] == [30, 30]


def test_chunked_empty_input_yields_no_chunks() -> None:
    """A zero-window tier must do NO decode work at all (a 0-duration probe)."""
    assert _chunked([], 30) == []


def test_chunked_shorter_than_size_is_one_chunk() -> None:
    """The common case -- an ordinary track -- is a single chunk, i.e. the pre-chunking shape."""
    windows = [_win(i) for i in range(7)]

    assert _chunked(windows, 30) == [windows]


def test_chunk_stop_sec_gates_every_chunk_but_the_last() -> None:
    """Non-final chunks stop the decode at their own end; the final one runs to EOF (None).

    The last chunk is deliberately ungated: it reaches EOF anyway, so a gate could only risk
    clipping the file's final window for no saving.
    """
    chunks = _chunked([_win(i) for i in range(65)], 30)

    assert _chunk_stop_sec(chunks, 0) == chunks[0][-1][2]
    assert _chunk_stop_sec(chunks, 1) == chunks[1][-1][2]
    assert _chunk_stop_sec(chunks, 2) is None


def test_chunk_stop_sec_single_chunk_is_never_gated() -> None:
    """One chunk == the whole file: gating it would be pure risk for zero saving."""
    chunks = _chunked([_win(i) for i in range(5)], 30)

    assert _chunk_stop_sec(chunks, 0) is None


def test_chunk_sizes_match_the_documented_memory_envelope() -> None:
    """The D-07 chunk sizes are pinned: they ARE the pre-removal per-tier residency figures.

    317 MB fine / 345 MB coarse is the envelope ADR-0005's memory limits were sized against,
    and the D-07 record's whole argument is that chunking reproduces it rather than changing
    it. A silent bump here would invalidate that reasoning without anyone noticing, so the
    arithmetic is asserted, not just the constants.
    """
    fine_bytes = analysis_mod._FINE_CHUNK_WINDOWS * 30 * 44100 * 4
    coarse_bytes = analysis_mod._COARSE_CHUNK_WINDOWS * 180 * 16000 * 4

    assert analysis_mod._FINE_CHUNK_WINDOWS == 60
    assert analysis_mod._COARSE_CHUNK_WINDOWS == 30
    # +/- 1 MB: the docstrings' 317 / 345 are the truncated figures the pre-removal code
    # carried, kept verbatim so the docs and the constants can be diffed against each other.
    assert abs(fine_bytes / 1_000_000 - 317) <= 1.0
    assert abs(coarse_bytes / 1_000_000 - 345) <= 1.0


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


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_raises_when_every_window_fails(mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """phaze-zibn: total decode failure must raise, not return an empty success.

    A truncated file whose header still probes a duration fails EasyLoader on every
    window in BOTH passes. Per-window isolation previously returned a normal result
    dict (all-None aggregates, 0 analyzed windows), the completion PUT stamped
    analysis_completed_at, and the undecodable file was permanently recorded as
    successfully analyzed. The all-windows-failed floor raises instead.
    """
    mock_get_labels.side_effect = _mock_labels_file
    mock_es.EasyLoader.side_effect = RuntimeError("no decodable frames")

    with pytest.raises(AnalysisDecodeError, match="all analysis windows failed to decode"):
        analyze_file("/fake/truncated.mp3", "/fake/models")


@patch("phaze.services.analysis._probe_duration_sec", return_value=0.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_zero_natural_windows_does_not_trip_the_decode_floor(
    _mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock
) -> None:
    """A zero-duration probe yields 0 natural windows in both passes: 0/0 coverage is not
    'every window failed', so the phaze-zibn floor must not fire (the 0/0 shape stays the
    caller's concern -- job_runner already floors it)."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/empty.mp3", "/fake/models")

    assert result["fine_windows_total"] == 0
    assert result["coarse_windows_total"] == 0
    assert result["windows"] == []


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
# phaze-w55w1: EXHAUSTIVE coverage emit (mocked essentia)
# ---------------------------------------------------------------------------

_COVERAGE_KEYS = ("fine_windows_analyzed", "fine_windows_total", "coarse_windows_analyzed", "coarse_windows_total")


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_coverage_is_complete(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """Every natural window is analyzed: analyzed == total in both tiers."""
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    for key in _COVERAGE_KEYS:
        assert key in result, f"missing progress-count key: {key}"
    # 600s -> 20 fine (30s) + 4 coarse (180/180/180/60).
    assert result["fine_windows_total"] == 20
    assert result["fine_windows_analyzed"] == 20
    assert result["coarse_windows_total"] == 4
    assert result["coarse_windows_analyzed"] == 4
    # analyzed counts equal the emitted window lists.
    assert result["fine_windows_analyzed"] == len(_fine_dicts(result))
    assert result["coarse_windows_analyzed"] == len(_coarse_dicts(result))


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_no_longer_emits_sampled(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """The `sampled` flag is GONE from the result contract (ADR-0007 §7).

    Asserted rather than assumed because every downstream consumer -- the write payload, the
    job_runner log line, the (removed) badge -- read it by key off this dict; a stray reappearance
    would be silently accepted by `.get()` everywhere and mean nothing.
    """
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/audio.mp3", "/fake/models")

    assert "sampled" not in result


@patch("phaze.services.analysis._probe_duration_sec", return_value=54000.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_long_file_analyzes_every_window_across_many_chunks(
    _mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock
) -> None:
    """A 15-hour set -- far past every removed cap -- is analyzed window for window.

    This is the acceptance criterion of the whole change: 54 000 s is 1 800 fine windows (30
    chunks of 60) and 300 coarse windows (10 chunks of 30). Before phaze-w55w1 the same file
    produced 60 fine / 30 coarse and a `sampled` flag; the indices below prove nothing is
    dropped AT a chunk boundary either, which is the one way chunking could regress coverage.
    """
    mock_get_labels.side_effect = _mock_labels_file

    result = analyze_file("/fake/<set-01>", "/fake/models")

    assert result["fine_windows_total"] == 1800
    assert result["fine_windows_analyzed"] == 1800
    assert result["coarse_windows_total"] == 300
    assert result["coarse_windows_analyzed"] == 300
    # Contiguous 0..N-1 indices: no gap at any of the 30 fine / 10 coarse chunk seams.
    assert [w["window_index"] for w in _fine_dicts(result)] == list(range(1800))
    assert [w["window_index"] for w in _coarse_dicts(result)] == list(range(300))


@patch("phaze.services.analysis._probe_duration_sec", return_value=5400.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_decodes_each_tier_in_bounded_chunks(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """No single decode call is ever handed more than one chunk of windows.

    THIS is the memory invariant, stated where it is actually enforceable: peak PCM is a
    function of the largest window list that reaches `_decode_windows` at once, and that list
    is what this asserts is bounded. 5 400 s = 180 fine windows (3 chunks) + 30 coarse (1).
    """
    mock_get_labels.side_effect = _mock_labels_file
    seen: list[tuple[int, int, float | None]] = []
    real_decode = analysis_mod._decode_windows

    def _spy(file_path: str, sample_rate: int, windows: Any, on_skip: Any, **kwargs: Any) -> Any:
        seen.append((sample_rate, len(windows), kwargs.get("stop_at_sec")))
        return real_decode(file_path, sample_rate, windows, on_skip, **kwargs)

    with patch.object(analysis_mod, "_decode_windows", _spy):
        analyze_file("/fake/<set-02>", "/fake/models")

    fine_calls = [c for c in seen if c[0] == 44100]
    coarse_calls = [c for c in seen if c[0] == 16000]
    assert [c[1] for c in fine_calls] == [60, 60, 60]
    assert [c[1] for c in coarse_calls] == [30]
    # Every non-final fine chunk carries an early-stop gate; the last one runs to EOF.
    assert [c[2] is None for c in fine_calls] == [False, False, True]
    assert coarse_calls[0][2] is None


# Phase 57.1 (PROG-01): the analyze_file progress_cb seam.


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_progress_cb_start_then_bumps(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """progress_cb fires START(0, natural_total) then non-decreasing bumps up to len(fine).

    600s @ 30s fine windows = 20 natural windows; under the default cap none are strided,
    so the START carries (0, 20) and the final bump carries (20, 20). Every call's
    denominator is the natural pre-stride count — identical to the completion PUT's
    fine_windows_total (the denominator invariant the in-flight bar depends on).
    """
    mock_get_labels.side_effect = _mock_labels_file
    calls: list[tuple[int, int]] = []

    result = analyze_file("/fake/audio.mp3", "/fake/models", progress_cb=lambda a, t: calls.append((a, t)))

    natural_total = result["fine_windows_total"]
    assert calls, "progress_cb must be invoked at least for the START signal"
    # START signal first.
    assert calls[0] == (0, natural_total)
    # Every call carries the same denominator == the completion PUT's fine_windows_total.
    assert all(total == natural_total for _a, total in calls)
    # analyzed is non-decreasing and never exceeds the analyzed count.
    analyzed_seq = [a for a, _t in calls]
    assert analyzed_seq == sorted(analyzed_seq)
    assert max(analyzed_seq) == result["fine_windows_analyzed"]
    # A bump landed after the START (mid-flight progress was emitted, not just START).
    assert len(calls) >= 2
    assert calls[-1] == (result["fine_windows_analyzed"], natural_total)


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_progress_cb_default_none_is_inert(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """progress_cb=None (default) leaves the result shape byte-identical (no emission, no error)."""
    mock_get_labels.side_effect = _mock_labels_file

    with_none = analyze_file("/fake/audio.mp3", "/fake/models")
    with_cb = analyze_file("/fake/audio.mp3", "/fake/models", progress_cb=lambda _a, _t: None)

    # Same progress-count contract regardless of whether a callback was supplied.
    for key in _COVERAGE_KEYS:
        assert with_none[key] == with_cb[key]


def test_analysis_progress_interval_sec_config_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The parent-side throttle knob exists on AgentSettings with a ~5s default (Phase 57.1 D-04)."""
    from phaze.config import AgentSettings

    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", str(tmp_path))
    # phaze-27myl: fileserver-kind (the default) AgentSettings now fail-fasts on the default
    # (docker-service-name) queue_url.
    monkeypatch.setenv("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@app-server.example:5432/phaze")
    cfg = AgentSettings()
    assert cfg.analysis_progress_interval_sec == 5.0


def test_analysis_module_has_no_http_client_import() -> None:
    """analysis.py stays HTTP/pickle-free: no httpx / agent_client import crosses the compute seam."""
    src = Path(__file__).resolve().parents[4] / "src" / "phaze" / "services" / "analysis.py"
    text = src.read_text(encoding="utf-8")
    assert "import httpx" not in text
    assert "agent_client" not in text


def test_aggregates_are_order_and_gap_independent_reductions() -> None:
    """aggregate_* over an arbitrary subset equals the direct reduction over that subset.

    Kept from the Phase 43 era (where the subset was a stride) because the property still
    matters and now has a DIFFERENT source: per-window decode/inference failures punch gaps in
    an otherwise exhaustive window set, and chunking means those gaps can cluster at a chunk.
    No aggregation needs contiguous windows.
    """
    from statistics import median

    fine = [_fine(i, 120.0 + i, "C major", start=float(i) * 30.0, end=float(i) * 30.0 + 30.0) for i in range(100)]
    subset = [w for w in fine if w.window_index % 7 == 0]

    expected = round(median([w.bpm for w in subset if w.bpm is not None]), 1)
    assert aggregate_bpm(subset) == expected


# ---------------------------------------------------------------------------
# phaze-7qfd -- job-peak-RSS observability: platform unit handling + the log line
# ---------------------------------------------------------------------------


def test_peak_rss_gib_linux_reads_vmhwm_in_kb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Linux: VmHWM is read from /proc/self/status, which proc(5) documents as ALWAYS kB.

    A value read as bytes instead of kB would be off by 1024x -- this pins the divisor.
    """
    fake_status = tmp_path / "status"
    fake_status.write_text("VmData:\t     123 kB\nVmHWM:\t    8824 kB\nVmRSS:\t    8000 kB\n")
    real_path = analysis_mod.Path
    monkeypatch.setattr(analysis_mod, "Path", lambda arg: fake_status if arg == "/proc/self/status" else real_path(arg))
    monkeypatch.setattr(analysis_mod.platform, "system", lambda: "Linux")

    result = _peak_rss_gib()

    assert result == pytest.approx(8824 / (1024 * 1024))


def test_peak_rss_gib_linux_falls_back_to_ru_maxrss_in_kib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux, /proc unreadable: falls back to ru_maxrss, which IS KiB on Linux (unlike Darwin)."""

    class _Unreadable:
        def open(self) -> Any:
            msg = "no /proc here"
            raise OSError(msg)

    monkeypatch.setattr(analysis_mod, "Path", lambda _arg: _Unreadable())
    monkeypatch.setattr(analysis_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(analysis_mod.resource, "getrusage", lambda _who: MagicMock(ru_maxrss=1024 * 1024 * 3))  # 3 GiB in KiB

    result = _peak_rss_gib()

    assert result == pytest.approx(3.0)


def test_peak_rss_gib_darwin_uses_bytes_not_kib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Darwin: ru_maxrss is BYTES. Using the Linux (KiB) divisor here would misreport by 1024x."""
    monkeypatch.setattr(analysis_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(analysis_mod.resource, "getrusage", lambda _who: MagicMock(ru_maxrss=2 * 1024**3))  # 2 GiB in bytes

    result = _peak_rss_gib()

    assert result == pytest.approx(2.0)


def test_peak_rss_gib_unknown_platform_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform whose RSS unit was never verified returns None rather than guess one."""
    monkeypatch.setattr(analysis_mod.platform, "system", lambda: "Windows")

    assert _peak_rss_gib() is None


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_logs_peak_rss_once_at_info(
    _mock_es: MagicMock, mock_get_labels: MagicMock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyze_file logs the job's peak RSS exactly once, at INFO, after analysis completes."""
    mock_get_labels.side_effect = _mock_labels_file
    monkeypatch.setattr(analysis_mod, "_peak_rss_gib", lambda: 2.482)

    with caplog.at_level(logging.INFO, logger="phaze.services.analysis"):
        analyze_file("/fake/audio.mp3", "/fake/models")

    peak_records = [r for r in caplog.records if "peak RSS" in r.getMessage()]
    assert len(peak_records) == 1, f"expected exactly one peak-RSS log line, got {len(peak_records)}"
    assert peak_records[0].levelno == logging.INFO
    assert "2.482" in peak_records[0].getMessage()


@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_analyze_file_skips_peak_rss_log_when_unreadable(
    _mock_es: MagicMock, mock_get_labels: MagicMock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unverified/unreadable platform logs nothing rather than a guessed number."""
    mock_get_labels.side_effect = _mock_labels_file
    monkeypatch.setattr(analysis_mod, "_peak_rss_gib", lambda: None)

    with caplog.at_level(logging.INFO, logger="phaze.services.analysis"):
        analyze_file("/fake/audio.mp3", "/fake/models")

    assert not [r for r in caplog.records if "peak RSS" in r.getMessage()]


# --- _get_classifier / _get_labels / _positive_class_prediction / _resolve_malloc_trim ---


def test_get_classifier_rejects_an_unknown_classifier_type() -> None:
    """A ``ModelConfig`` whose ``classifier_type`` matches none of the three known kinds raises."""
    bogus = ModelConfig(name="bogus", variant="bogus_variant", filename="bogus-model-1", classifier_type="not_a_real_type")

    with pytest.raises(ValueError, match="Unknown classifier type: not_a_real_type"):
        _get_classifier(bogus, "/fake/models")


def test_get_labels_reads_json_and_normalizes_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_get_labels`` reads ``<models_dir>/<filename>.json``, maps '---' -> '/' in class names, and caches by filename.

    The cache is asserted directly: after priming it, the JSON file is deleted, so a second call
    that still returns the same labels proves the cache -- not a lucky re-read -- served it.
    """
    monkeypatch.setattr(analysis_mod, "_labels_cache", {})
    models_dir = tmp_path
    json_path = models_dir / "genre-model-1.json"
    json_path.write_text('{"classes": ["Electronic---House", "Pop---Dance", "Rock"]}')

    labels = _get_labels("genre-model-1", str(models_dir))

    assert labels == ["Electronic/House", "Pop/Dance", "Rock"]

    json_path.unlink()  # prove the second call is served from the cache, not a re-read
    cached_labels = _get_labels("genre-model-1", str(models_dir))

    assert cached_labels == labels


def test_positive_class_prediction_falls_back_to_first_entry_when_no_label_qualifies() -> None:
    """If every entry's label starts with a negation prefix, the defensive fallback picks entry 0.

    Real essentia binary-classifier metadata always has exactly one non-negated label, so this
    only fires on an unexpected label shape -- and must not raise when it does.
    """
    predictions = [
        {"label": "non_relaxed", "prediction": 0.2},
        {"label": "not_relaxed_either", "prediction": 0.8},
    ]

    result = _positive_class_prediction(predictions)

    assert result == pytest.approx(0.2)  # entry 0, the defensive fallback -- not the higher score


def test_derive_style_returns_unknown_when_no_predictions() -> None:
    """An empty/absent genre predictions list derives the sentinel 'unknown' style, not a crash."""
    assert derive_style({"predictions": []}) == "unknown"
    assert derive_style({}) == "unknown"


def test_resolve_malloc_trim_returns_none_off_glibc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-glibc libc (macOS, musl) short-circuits to None without ever touching ctypes."""
    monkeypatch.setattr(analysis_mod.platform, "libc_ver", lambda: ("", ""))

    assert _resolve_malloc_trim() is None


def test_resolve_malloc_trim_binds_the_symbol_on_glibc(monkeypatch: pytest.MonkeyPatch) -> None:
    """On glibc, the symbol is resolved via ``ctypes.CDLL(None)`` and given size_t/int signature/return types."""
    import ctypes

    trim_symbol = MagicMock(name="malloc_trim")
    fake_libc = MagicMock()
    fake_libc.malloc_trim = trim_symbol
    monkeypatch.setattr(analysis_mod.platform, "libc_ver", lambda: ("glibc", "2.35"))
    monkeypatch.setattr(analysis_mod.ctypes, "CDLL", lambda _arg: fake_libc)

    trim = _resolve_malloc_trim()

    assert trim is trim_symbol
    assert trim_symbol.argtypes == [ctypes.c_size_t]
    assert trim_symbol.restype == ctypes.c_int


def test_resolve_malloc_trim_returns_none_when_symbol_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """glibc reported, but the running image cannot ``dlopen``/resolve the symbol: None, never raises."""

    def _boom(_arg: object) -> None:
        msg = "cannot load shared object"
        raise OSError(msg)

    monkeypatch.setattr(analysis_mod.platform, "libc_ver", lambda: ("glibc", "2.35"))
    monkeypatch.setattr(analysis_mod.ctypes, "CDLL", _boom)

    assert _resolve_malloc_trim() is None


@patch("phaze.services.analysis._probe_duration_sec", return_value=600.0)
@patch("phaze.services.analysis._get_labels")
@patch("phaze.services.analysis.es", new_callable=_build_mock_essentia)
def test_a_coarse_window_that_fails_derivation_is_dropped_not_fatal(_mock_es: MagicMock, mock_get_labels: MagicMock, _mock_dur: MagicMock) -> None:
    """Per-window isolation survives chunking: a bad window is dropped, its chunk-mates are not.

    Derivation runs at chunk ASSEMBLY, after the model sweep, so a raise there is the one
    failure mode that could plausibly take a whole chunk with it rather than one window. 600 s
    is 4 coarse windows in a single chunk, and exactly one of them is made to fail.
    """
    mock_get_labels.side_effect = _mock_labels_file
    real_derive = analysis_mod.derive_mood
    seen: list[int] = []

    def _fail_second_call(features: dict[str, Any]) -> str:
        seen.append(len(seen))
        if len(seen) == 2:
            msg = "derivation exploded on this window"
            raise RuntimeError(msg)
        return real_derive(features)

    with patch.object(analysis_mod, "derive_mood", _fail_second_call):
        result = analyze_file("/fake/audio.mp3", "/fake/models")

    # 4 natural coarse windows, 1 dropped at assembly -> 3 analyzed, and the file still succeeds.
    assert result["coarse_windows_total"] == 4
    assert result["coarse_windows_analyzed"] == 3
    assert [w["window_index"] for w in _coarse_dicts(result)] == [0, 2, 3]
    # The fine tier is untouched by a coarse-side failure.
    assert result["fine_windows_analyzed"] == 20
