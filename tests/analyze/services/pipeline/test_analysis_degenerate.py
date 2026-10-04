"""Completed degenerate windows carry absent measurements, never plausible model guesses."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import wave

import numpy as np
import pytest

import phaze.services.analysis as analysis


@pytest.mark.parametrize("amplitude", [0.0, 1e-7, 1e-5])
def test_silent_fine_window_is_completed_with_null_measurements(amplitude: float) -> None:
    rhythm = MagicMock(return_value=(738.3, [], 4.69, [], []))
    key = MagicMock(return_value=("A", "minor", 0.8))
    decoded = {0: np.full(44100, amplitude, dtype=np.float32)}
    skipped = MagicMock()

    window = analysis._measure_fine_window(rhythm, key, (0, 0.0, 1.0), decoded, skipped)

    assert window is not None
    assert window.bpm is None
    assert window.musical_key is None
    assert window.as_payload_dict()["bpm"] is None
    assert decoded == {}
    rhythm.assert_not_called()
    key.assert_not_called()
    skipped.assert_not_called()


def test_signal_above_floor_keeps_reliable_measurements() -> None:
    rhythm = MagicMock(return_value=(120.0, [], 3.8, [], []))
    key = MagicMock(return_value=("C", "major", 0.8))
    decoded = {0: np.array([-2e-5, 2e-5], dtype=np.float32)}

    window = analysis._measure_fine_window(rhythm, key, (0, 0.0, 1.0), decoded, MagicMock())

    assert window is not None
    assert window.bpm == 120.0
    assert window.musical_key == "C major"
    rhythm.assert_called_once()
    key.assert_called_once()


def test_unreliable_extractors_return_null_for_individual_measurements() -> None:
    rhythm = MagicMock(return_value=(80.7, [], 0.0, [], []))
    key = MagicMock(return_value=("B", "minor", 0.0))
    window = analysis._measure_fine_window(rhythm, key, (0, 0.0, 3.0), {0: np.ones(44100)}, MagicMock())
    assert window is not None
    assert window.bpm is None
    assert window.musical_key is None


@pytest.mark.parametrize("amplitude", [0.0, 1e-7])
def test_silent_coarse_windows_have_no_classifier_guesses(amplitude: float) -> None:
    skipped = MagicMock()
    heartbeat = MagicMock()
    with patch.object(analysis, "_predict_single") as predict:
        features, failed = analysis._run_model_sets_over_windows([(0, np.full(16000, amplitude))], "/fake/models", skipped, heartbeat)
    window = analysis._derive_coarse_window((0, 0.0, 1.0), features, failed, skipped)

    assert window is not None
    assert (window.mood, window.style, window.danceability) == (None, None, None)
    assert failed == set()
    predict.assert_not_called()
    skipped.assert_not_called()
    assert heartbeat.call_count == sum(len(group.models) for group in analysis.MODEL_SETS) + 1


def test_empty_signal_and_classifier_evidence_are_absent() -> None:
    assert not analysis._has_analyzable_signal(np.array([], dtype=np.float32))
    assert analysis.derive_mood({}) is None
    assert analysis.derive_mood({"mood_happy": {"variant": []}}) is None
    assert analysis.derive_style({"predictions": []}) is None


def test_silent_coarse_window_does_not_remove_neighboring_audible_measurements() -> None:
    audible = np.array([-0.5, 0.5], dtype=np.float32)
    with (
        patch.object(analysis, "_predict_single", return_value=np.array([0.8, 0.2])) as predict,
        patch.object(analysis, "_get_labels", return_value=["Electronic---House", "Rock"]),
    ):
        features, failed = analysis._run_model_sets_over_windows([(0, np.zeros(2)), (1, audible)], "/fake/models", MagicMock())
    silent = analysis._derive_coarse_window((0, 0.0, 1.0), features, failed, MagicMock())
    neighbor = analysis._derive_coarse_window((1, 1.0, 2.0), features, failed, MagicMock())

    assert silent is not None and silent.style is None
    assert neighbor is not None
    assert neighbor.style == "Electronic/House"
    assert neighbor.mood is not None
    assert neighbor.danceability == pytest.approx(0.8)
    assert all(call.args[0] is audible for call in predict.call_args_list)


def test_real_silent_file_completes_every_window_with_null_measurements(tmp_path: Path) -> None:
    """Real decode plus both real tier loops; silence needs no classifier weights."""
    source = tmp_path / "silence.wav"
    with wave.open(str(source), "wb") as writer:
        writer.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        for _ in range(200):
            writer.writeframes(b"\x00\x00" * 44100)

    result = analysis.analyze_file(str(source), str(tmp_path / "no-models"))

    assert result["fine_windows_analyzed"] == result["fine_windows_total"] == 7
    assert result["coarse_windows_analyzed"] == result["coarse_windows_total"] == 2
    assert (result["bpm"], result["musical_key"], result["mood"], result["style"], result["danceability"]) == (None,) * 5
    assert len(result["windows"]) == 9
    for window in result["windows"]:
        assert all(window.get(field) is None for field in ("bpm", "musical_key", "mood", "style", "danceability"))
