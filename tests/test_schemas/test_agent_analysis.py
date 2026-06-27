"""Unit tests for phaze.schemas.agent_analysis (Phase 26 Plan 03 — D-26)."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_analysis import (
    AnalysisFailurePayload,
    AnalysisFailureResponse,
    AnalysisWindowPayload,
    AnalysisWritePayload,
    AnalysisWriteResponse,
    PresignDownloadResponse,
)


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


def test_analysis_write_payload_accepts_coverage_fields() -> None:
    """Phase 43: the five windowed-analysis coverage fields validate and round-trip."""
    payload = AnalysisWritePayload(
        bpm=128.0,
        fine_windows_analyzed=10,
        fine_windows_total=40,
        coarse_windows_analyzed=2,
        coarse_windows_total=8,
        sampled=True,
    )

    assert payload.fine_windows_analyzed == 10
    assert payload.fine_windows_total == 40
    assert payload.coarse_windows_analyzed == 2
    assert payload.coarse_windows_total == 8
    assert payload.sampled is True


def test_analysis_write_payload_coverage_default_none() -> None:
    """Coverage fields are optional -- omitted leaves them None (partial-PUT contract)."""
    payload = AnalysisWritePayload(bpm=120.0)

    assert payload.fine_windows_analyzed is None
    assert payload.fine_windows_total is None
    assert payload.coarse_windows_analyzed is None
    assert payload.coarse_windows_total is None
    assert payload.sampled is None


def test_analysis_write_payload_rejects_negative_coverage_count() -> None:
    """Coverage counts are ge=0."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisWritePayload.model_validate({"fine_windows_analyzed": -1})


def test_analysis_failure_payload_accepts_valid_reasons() -> None:
    """reason validates against the Literal classifications; error is optional."""
    for reason in ("timeout", "crashed", "error"):
        payload = AnalysisFailurePayload(reason=reason)  # type: ignore[arg-type]
        assert payload.reason == reason
        assert payload.error is None


def test_analysis_failure_payload_accepts_error_detail() -> None:
    """error free-text detail round-trips."""
    payload = AnalysisFailurePayload(reason="timeout", error="killed after 7200s")

    assert payload.reason == "timeout"
    assert payload.error == "killed after 7200s"


def test_analysis_failure_payload_rejects_bad_reason() -> None:
    """reason outside the Literal set raises (input-validation control)."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisFailurePayload.model_validate({"reason": "kaboom"})


def test_analysis_failure_payload_rejects_oversized_error() -> None:
    """error max_length=2000 bounds the DoS-via-huge-string threat (T-43-06)."""
    with pytest.raises(pydantic.ValidationError):
        AnalysisFailurePayload.model_validate({"reason": "error", "error": "x" * 2001})


def test_analysis_failure_payload_rejects_unknown_field() -> None:
    """extra='forbid' rejects an attempt to smuggle agent_id/file_id in the body (AUTH-01)."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        AnalysisFailurePayload.model_validate({"reason": "error", "agent_id": "spoofed"})

    errors = exc_info.value.errors()
    assert any(err.get("type") == "extra_forbidden" for err in errors), errors


def test_analysis_failure_response_shape() -> None:
    """Failure response carries agent_id + file_id only (minimal echo)."""
    file_id = uuid.uuid4()
    resp = AnalysisFailureResponse(agent_id="agent-a", file_id=file_id)

    assert resp.agent_id == "agent-a"
    assert resp.file_id == file_id


def test_presign_download_response_accepts_valid_sha256() -> None:
    """A 64-char lowercase-hex digest validates (IN-02)."""
    resp = PresignDownloadResponse(download_url="https://s3.example/obj?sig=xyz", expected_sha256="a" * 64)

    assert resp.download_url == "https://s3.example/obj?sig=xyz"
    assert resp.expected_sha256 == "a" * 64


@pytest.mark.parametrize(
    "bad_sha",
    [
        "a" * 63,  # too short
        "a" * 65,  # too long
        "A" * 64,  # uppercase hex rejected (storage is lowercase)
        "g" * 64,  # non-hex character
        "",  # empty string no longer accepted
        " " + "a" * 63,  # leading whitespace
    ],
)
def test_presign_download_response_rejects_malformed_sha256(bad_sha: str) -> None:
    """expected_sha256 is pinned to ^[0-9a-f]{64}$ so format skew fails fast (IN-02)."""
    with pytest.raises(pydantic.ValidationError):
        PresignDownloadResponse.model_validate({"download_url": "https://s3.example/obj", "expected_sha256": bad_sha})
