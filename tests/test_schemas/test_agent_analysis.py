"""Unit tests for phaze.schemas.agent_analysis (Phase 26 Plan 03 — D-26)."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_analysis import AnalysisWritePayload, AnalysisWriteResponse


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


def test_analysis_write_response_shape() -> None:
    """Response carries agent_id + file_id only (minimal echo)."""
    file_id = uuid.uuid4()
    resp = AnalysisWriteResponse(agent_id="agent-a", file_id=file_id)

    assert resp.agent_id == "agent-a"
    assert resp.file_id == file_id
