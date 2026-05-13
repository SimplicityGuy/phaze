"""Unit tests for phaze.schemas.agent_tracklists (Phase 26 Plan 03 — D-27, T-26-07-DoS)."""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_tracklists import (
    TracklistCreatePayload,
    TracklistCreateResponse,
    TracklistTrackPayload,
)


def _valid_track(position: int = 0) -> TracklistTrackPayload:
    return TracklistTrackPayload(position=position, artist="Artist", title="Title")


def test_tracklist_track_payload_minimal() -> None:
    """Only `position` is required; everything else is optional."""
    track = TracklistTrackPayload(position=0)
    assert track.position == 0
    assert track.artist is None


def test_tracklist_track_payload_rejects_unknown_field() -> None:
    """Nested item schemas must also enforce extra='forbid' (per-class ConfigDict)."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        TracklistTrackPayload.model_validate({"position": 0, "rogue": "x"})

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_tracklist_track_payload_rejects_negative_position() -> None:
    """Field(ge=0) on position — adversary can't send negative."""
    with pytest.raises(pydantic.ValidationError):
        TracklistTrackPayload.model_validate({"position": -1})


def test_tracklist_create_payload_valid() -> None:
    """A real-shaped POST /tracklists body validates."""
    payload = TracklistCreatePayload(
        file_id=uuid.uuid4(),
        source="fingerprint",
        external_id="ext-1",
        tracks=[_valid_track(0), _valid_track(1)],
        request_id=uuid.uuid4(),
    )

    assert payload.source == "fingerprint"
    assert len(payload.tracks) == 2


def test_tracklist_create_payload_rejects_unknown_field() -> None:
    """extra='forbid' on the parent."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        TracklistCreatePayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "source": "fingerprint",
                "external_id": "x",
                "tracks": [{"position": 0}],
                "request_id": str(uuid.uuid4()),
                "rogue": "no",
            },
        )

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_tracklist_create_payload_rejects_empty_tracks() -> None:
    """min_length=1 — at least one track required."""
    with pytest.raises(pydantic.ValidationError):
        TracklistCreatePayload(
            file_id=uuid.uuid4(),
            source="fingerprint",
            external_id="x",
            tracks=[],
            request_id=uuid.uuid4(),
        )


def test_tracklist_create_payload_rejects_too_many_tracks() -> None:
    """T-26-07-DoS — max_length=2000 cap."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        TracklistCreatePayload(
            file_id=uuid.uuid4(),
            source="fingerprint",
            external_id="x",
            tracks=[_valid_track(i) for i in range(2001)],
            request_id=uuid.uuid4(),
        )

    # error type for max_length violation
    assert any("too_long" in str(e.get("type", "")) for e in exc_info.value.errors())


def test_tracklist_create_payload_accepts_max_length_boundary() -> None:
    """Exactly 2000 tracks must pass (boundary)."""
    payload = TracklistCreatePayload(
        file_id=uuid.uuid4(),
        source="fingerprint",
        external_id="x",
        tracks=[_valid_track(i) for i in range(2000)],
        request_id=uuid.uuid4(),
    )

    assert len(payload.tracks) == 2000


def test_tracklist_create_payload_rejects_invalid_source() -> None:
    """Literal['fingerprint'] — any other source value is rejected."""
    with pytest.raises(pydantic.ValidationError):
        TracklistCreatePayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "source": "manual",  # not in Literal
                "external_id": "x",
                "tracks": [{"position": 0}],
                "request_id": str(uuid.uuid4()),
            },
        )


def test_tracklist_create_payload_rejects_invalid_request_id() -> None:
    """request_id must be a valid UUID (typed validation prevents Redis-key injection)."""
    with pytest.raises(pydantic.ValidationError):
        TracklistCreatePayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "source": "fingerprint",
                "external_id": "x",
                "tracks": [{"position": 0}],
                "request_id": "../../etc/passwd",
            },
        )


def test_tracklist_create_response_shape() -> None:
    """Response shape: tracklist_id, version, track_count."""
    tl_id = uuid.uuid4()
    resp = TracklistCreateResponse(tracklist_id=tl_id, version=1, track_count=42)

    assert resp.tracklist_id == tl_id
    assert resp.version == 1
    assert resp.track_count == 42
