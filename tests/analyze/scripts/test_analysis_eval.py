"""Tests of the offline evaluation boundary, not of Essentia's predictions."""

from __future__ import annotations

import hashlib
import json
import sys
from typing import TYPE_CHECKING

import pytest

from scripts.analysis_eval import _run_one, normalize_current, normalize_neutral, verify_staged_audio
from scripts.analysis_eval_score import key_score, score_one, tempo_scores
from scripts.analysis_eval_stage import _scratch_path
from tests.analyze._real_result import real_analysis_result


if TYPE_CHECKING:
    from pathlib import Path


def _neutral_result(duration: float = 30.0) -> dict:
    scores = {
        "mood_acoustic": 0.7,
        "mood_electronic": 0.1,
        "mood_aggressive": 0.1,
        "mood_relaxed": 0.8,
        "mood_happy": 0.6,
        "mood_sad": 0.2,
        "mood_party": 0.3,
        "danceability": 0.4,
        "gender": 0.5,
        "tonality": 0.9,
        "voice_instrumental": 0.1,
    }
    return {
        "bpm": 120.0,
        "musical_key": "A minor",
        "mood": {name.removeprefix("mood_"): value for name, value in scores.items() if name.startswith("mood_")},
        "style": [{"name": "Electronic/House", "score": 0.8}],
        "dominant_style": "Electronic/House",
        "danceability": 0.4,
        "fine_windows_total": 1,
        "fine_windows_analyzed": 1,
        "coarse_windows_total": 1,
        "coarse_windows_analyzed": 1,
        "windows": [
            {"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": duration, "bpm": 120.0, "musical_key": "A minor", "camelot": "8A"},
            {
                "tier": "coarse",
                "window_index": 0,
                "start_sec": 0.0,
                "end_sec": duration,
                "mood": "relaxed",
                "style": "Electronic/House",
                "danceability": 0.4,
                "mood_scores": scores,
                "energy": 0.4,
            },
        ],
    }


def test_neutral_contract_allows_semantic_scores_without_essentia_variant_arrays() -> None:
    result, problems = normalize_neutral(_neutral_result(), 30.0)
    assert problems == []
    assert result["windows"][1]["mood_scores"]["tonality"] == 0.9


def test_neutral_contract_catches_missing_time_coverage_and_score_dimension() -> None:
    result = _neutral_result(30.0)
    result["windows"][1]["end_sec"] = 10.0
    del result["windows"][1]["mood_scores"]["voice_instrumental"]
    _, problems = normalize_neutral(result, 30.0)
    assert "coarse: incomplete file coverage" in problems
    assert "coarse: incomplete 11-score vector" in problems


def test_production_adapter_accepts_a_recorded_real_model_result() -> None:
    normalized, problems = normalize_current(real_analysis_result(), 546.0)
    assert problems == []
    assert normalized["coarse_windows_analyzed"] == 4
    assert len(normalized["characteristics"]) == 11


def test_child_protocol_measurement_stays_in_scratch(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    contents = b"synthetic staged bytes"
    sha = hashlib.sha256(contents).hexdigest()
    (audio_dir / f"{sha}.mp3").write_bytes(contents)
    item = {"file_id": "00000000-0000-0000-0000-000000000001", "sha256": sha, "format": "mp3", "band": "short", "duration_sec": 30.0}
    verify_staged_audio([item], audio_dir)
    result = _neutral_result()
    command = [sys.executable, "-c", f"import json; print(json.dumps({json.dumps({'type': 'result', 'result': result})}))", "{audio}"]
    run = _run_one(item, audio_dir, tmp_path, tmp_path, command, "cold", 1, "neutral")
    assert run["exit_code"] == 0
    assert run["contract_problems"] == []
    assert run["wall_sec"] > 0
    assert (tmp_path / f"{sha}-cold-1" / "prediction.json").exists()
    assert str(audio_dir) not in json.dumps(run)


def test_key_and_tempo_scoring_distinguish_exact_octave_and_related_keys() -> None:
    assert key_score("C major", "G major") == 0.5
    assert key_score("C major", "A minor") == 0.3
    assert key_score("C major", "C minor") == 0.2
    assert tempo_scores(120.0, 60.0) == (False, True)
    row = score_one({"sha256": "a" * 64, "bpm": 120, "key": "A minor"}, _neutral_result())
    assert row["tempo"]["strict_2pct"] is True
    assert row["key"]["exact"] is True


def test_staging_rejects_paths_outside_scratch() -> None:
    with pytest.raises(ValueError, match="/scratch"):
        _scratch_path("/data/music")
    assert _scratch_path("/scratch/phaze-eval") == "/scratch/phaze-eval"
