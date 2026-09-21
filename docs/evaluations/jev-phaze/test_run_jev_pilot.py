from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from types import ModuleType


SCRIPT = Path(__file__).with_name("run_jev_pilot.py")


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_jev_pilot", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pilot() -> ModuleType:
    return _module()


@pytest.fixture
def track() -> dict[str, Any]:
    return {
        "file_id": "example",
        "artist": "Artist",
        "title": "Title",
        "album": "Album",
        "duration_seconds": 180.0,
        "bpm": 128.0,
        "musical_key": "A minor",
        "moods": {
            "acoustic": 0.1,
            "electronic": 0.9,
            "aggressive": 0.2,
            "relaxed": 0.3,
            "happy": 0.4,
            "sad": 0.5,
            "party": 0.8,
        },
    }


def test_jev_only_state_has_no_classifier_leakage(pilot: ModuleType, track: dict[str, Any]) -> None:
    state = pilot.build_state(track, "jev_only")
    assert "essentia_classifier_evidence" not in state
    rendered = repr(state)
    for score in track["moods"].values():
        assert str(score) not in rendered


def test_hybrid_adds_exactly_the_frozen_mood_vector(pilot: ModuleType, track: dict[str, Any]) -> None:
    jev_only = pilot.build_state(track, "jev_only")
    hybrid = pilot.build_state(track, "hybrid")
    classifier = hybrid.pop("essentia_classifier_evidence")
    assert hybrid == jev_only | {"evidence_contract": hybrid["evidence_contract"]}
    assert hybrid["evidence_contract"]["arm"] == "hybrid"
    assert classifier["mood_probabilities"] == track["moods"]


def test_seven_independent_nouls_share_one_request(pilot: ModuleType, track: dict[str, Any]) -> None:
    payload = pilot.build_payload(track, "jev_only")
    assert tuple(payload["questions"]) == pilot.MOODS
    assert {question["type"] for question in payload["questions"].values()} == {"noul"}


def test_corpus_rejects_wrong_size_or_mood_order(pilot: ModuleType, track: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="exactly 12"):
        pilot.validate_corpus({"mood_order": pilot.MOODS, "tracks": [track]})

    tracks = [dict(track, file_id=str(index)) for index in range(12)]
    with pytest.raises(ValueError, match="frozen seven-label order"):
        pilot.validate_corpus({"mood_order": tuple(reversed(pilot.MOODS)), "tracks": tracks})


def test_run_records_usage_latency_and_cost(pilot: ModuleType, track: dict[str, Any]) -> None:
    tracks = [dict(track, file_id=str(index)) for index in range(12)]

    def fake_caller(_payload: dict[str, Any], *, api_key: str) -> Any:
        assert api_key == "not-a-real-key"
        answers = {mood: {"type": "noul", "noul": 0.25} for mood in pilot.MOODS}
        return pilot.ApiResult(
            {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 20}},
            12.5,
            1,
            (),
        )

    result = pilot.run_pilot(
        {"mood_order": pilot.MOODS, "tracks": tracks},
        api_key="not-a-real-key",
        repeats=1,
        caller=fake_caller,
        progress=None,
    )
    assert result["summary"] == {
        "subjects": 12,
        "requests": 24,
        "successful_requests": 24,
        "failed_requests": 0,
        "input_tokens": 2400,
        "output_tokens": 480,
        "cost_usd_public_rate": pytest.approx(0.0001008),
    }
    assert all(record["latency_ms"] == 12.5 for record in result["records"])
