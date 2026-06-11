"""Unit tests for phaze.schemas.agent_analysis (Phase 26 Plan 03 — D-26)."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_analysis import AnalysisWindowPayload, AnalysisWritePayload, AnalysisWriteResponse


def test_analysis_write_payload_accepts_empty_body() -> None:
    """All fields are optional — partial PUT must accept an empty body."""
    payload = AnalysisWritePayload()

    assert payload.bpm is None
    assert payload.musical_key is None
    assert payload.mood is None
    assert payload.style is None
    assert payload.danceability is None
    assert payload.energy is None


def test_analysis_write_payload_accepts_full_body() -> None:
    """Full essentia analysis body validates."""
    payload = AnalysisWritePayload(
        bpm=128.5,
        musical_key="A minor",
        mood={"happy": 0.8, "sad": 0.1},
        style={"electronic": 0.9, "rock": 0.0},
        danceability=0.7,
        energy=0.85,
    )

    assert payload.bpm == 128.5
    assert payload.mood == {"happy": 0.8, "sad": 0.1}
    assert payload.style == {"electronic": 0.9, "rock": 0.0}


def test_analysis_write_payload_rejects_unknown_field() -> None:
    """D-16/D-26 — extra='forbid' must reject unknown keys with a 422-friendly error."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        AnalysisWritePayload.model_validate({"bpm": 120.0, "extra": "x"})

    errors = exc_info.value.errors()
    assert any(err.get("type") == "extra_forbidden" for err in errors), errors


def test_analysis_write_payload_rejects_negative_bpm() -> None:
    """bpm has ge=0.0 constraint."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisWritePayload.model_validate({"bpm": -1.0})


def test_analysis_write_payload_rejects_out_of_range_danceability() -> None:
    """danceability is bounded to [0.0, 1.0]."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisWritePayload.model_validate({"danceability": 1.5})

    with pytest.raises(pydantic.ValidationError):
        AnalysisWritePayload.model_validate({"danceability": -0.1})


def test_analysis_write_payload_rejects_out_of_range_energy() -> None:
    """energy is bounded to [0.0, 1.0]."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisWritePayload.model_validate({"energy": 2.0})


def test_analysis_window_payload_round_trips() -> None:
    """Fine-tier window round-trips via model_dump()/model_validate()."""
    payload = AnalysisWindowPayload(
        tier="fine",
        window_index=0,
        start_sec=0.0,
        end_sec=30.0,
        bpm=120.0,
        musical_key="C major",
    )

    restored = AnalysisWindowPayload.model_validate(payload.model_dump())

    assert restored == payload
    assert restored.tier == "fine"
    assert restored.window_index == 0
    assert restored.bpm == 120.0
    assert restored.musical_key == "C major"


def test_analysis_window_payload_accepts_coarse_fields() -> None:
    """Coarse-tier window carries mood/style/danceability/features."""
    payload = AnalysisWindowPayload(
        tier="coarse",
        window_index=2,
        start_sec=180.0,
        end_sec=360.0,
        mood="happy",
        style="electronic",
        danceability=0.8,
        features={"valence": 0.6},
    )

    assert payload.tier == "coarse"
    assert payload.mood == "happy"
    assert payload.style == "electronic"
    assert payload.danceability == 0.8
    assert payload.features == {"valence": 0.6}


def test_analysis_window_payload_rejects_bad_tier() -> None:
    """tier outside {'fine','coarse'} raises ValidationError (Literal guard)."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisWindowPayload.model_validate({"tier": "medium", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0})


def test_analysis_window_payload_rejects_negative_window_index() -> None:
    """window_index < 0 raises ValidationError (ge=0)."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisWindowPayload.model_validate({"tier": "fine", "window_index": -1, "start_sec": 0.0, "end_sec": 30.0})


def test_analysis_window_payload_rejects_negative_start_sec() -> None:
    """start_sec < 0 raises ValidationError (ge=0.0)."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisWindowPayload.model_validate({"tier": "fine", "window_index": 0, "start_sec": -1.0, "end_sec": 30.0})


def test_analysis_window_payload_rejects_unknown_field() -> None:
    """extra='forbid' rejects unknown keys on a window."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        AnalysisWindowPayload.model_validate({"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0, "bogus": 1})

    errors = exc_info.value.errors()
    assert any(err.get("type") == "extra_forbidden" for err in errors), errors


def test_analysis_write_payload_windows_omitted_is_none() -> None:
    """windows omitted leaves windows None (partial-PUT contract)."""
    payload = AnalysisWritePayload(bpm=120.0)

    assert payload.windows is None


def test_analysis_write_payload_accepts_windows_list() -> None:
    """windows field accepts a list of AnalysisWindowPayload."""
    payload = AnalysisWritePayload(
        windows=[
            AnalysisWindowPayload(tier="fine", window_index=0, start_sec=0.0, end_sec=30.0, bpm=120.0),
            AnalysisWindowPayload(tier="coarse", window_index=0, start_sec=0.0, end_sec=180.0, mood="calm"),
        ]
    )

    assert payload.windows is not None
    assert len(payload.windows) == 2
    assert payload.windows[0].tier == "fine"
    assert payload.windows[1].tier == "coarse"


def test_analysis_write_payload_rejects_oversized_windows() -> None:
    """windows list is bounded (max_length) — DoS-via-huge-bulk-insert mitigation."""
    too_many = [{"tier": "fine", "window_index": i, "start_sec": 0.0, "end_sec": 30.0} for i in range(50001)]

    with pytest.raises(pydantic.ValidationError):
        AnalysisWritePayload.model_validate({"windows": too_many})


def test_analysis_write_response_shape() -> None:
    """Response carries agent_id + file_id only (minimal echo)."""
    file_id = uuid.uuid4()
    resp = AnalysisWriteResponse(agent_id="agent-a", file_id=file_id)

    assert resp.agent_id == "agent-a"
    assert resp.file_id == file_id
