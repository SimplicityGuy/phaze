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


# ------------------------------------------------------------------------------------------------
# phaze-btlu: width caps matching the mapped column (wire_bounds rules 1 and 2).
# ------------------------------------------------------------------------------------------------
def test_track_timestamp_accepts_the_column_width_boundary() -> None:
    """20 chars is exactly tracklist_tracks.timestamp varchar(20) -- must be ACCEPTED, not rejected."""
    track = TracklistTrackPayload.model_validate({"position": 0, "timestamp": "x" * 20})

    assert track.timestamp == "x" * 20


def test_track_timestamp_rejects_over_width() -> None:
    """21 chars would raise StringDataRightTruncation in Postgres -- reject at the boundary as 422."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        TracklistTrackPayload.model_validate({"position": 0, "timestamp": "x" * 21})

    assert any(e.get("type") == "string_too_long" for e in exc_info.value.errors())


def test_track_artist_and_title_stay_uncapped() -> None:
    """artist/title map to Text columns -- unbounded, so a long value must still validate (rule 2)."""
    track = TracklistTrackPayload.model_validate({"position": 0, "artist": "a" * 5000, "title": "t" * 5000})

    assert len(track.artist or "") == 5000
    assert len(track.title or "") == 5000


def test_external_id_accepts_the_column_width_boundary() -> None:
    """50 chars is exactly tracklists.external_id varchar(50) -- must be ACCEPTED."""
    payload = TracklistCreatePayload(
        file_id=uuid.uuid4(),
        source="fingerprint",
        external_id="e" * 50,
        tracks=[_valid_track()],
        request_id=uuid.uuid4(),
    )

    assert payload.external_id == "e" * 50


def test_external_id_rejects_over_width() -> None:
    """51 chars overflows the ON CONFLICT idempotency key column -- reject before the lock is taken."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        TracklistCreatePayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "source": "fingerprint",
                "external_id": "e" * 51,
                "tracks": [{"position": 0}],
                "request_id": str(uuid.uuid4()),
            }
        )

    assert any(e.get("type") == "string_too_long" for e in exc_info.value.errors())


def test_external_id_rejects_empty_string() -> None:
    """NOT NULL still admits '' -- an empty idempotency key would collide across files, so min_length=1."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        TracklistCreatePayload.model_validate(
            {
                "file_id": str(uuid.uuid4()),
                "source": "fingerprint",
                "external_id": "",
                "tracks": [{"position": 0}],
                "request_id": str(uuid.uuid4()),
            }
        )

    assert any(e.get("type") == "string_too_short" for e in exc_info.value.errors())
