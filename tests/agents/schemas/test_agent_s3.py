"""Unit tests for phaze.schemas.agent_s3 (Phase 53 S3 upload leg, KSTAGE-02).

These ORM-free models back the agent-side upload task payload and the two
internal-API upload callbacks (`/uploaded` and `/failed`). file_id flows on the
URL path for callbacks (AUTH-01); the upload payload carries file_id because the
deterministic-key builder reads it. Every REQUEST model is `extra="forbid"`;
RESPONSE models are `extra="ignore"` (phaze-3ggc, Phase 25 convention) so an
additive control-plane field never hard-fails an older agent's `model_validate`.
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_s3 import (
    UploadedPart,
    UploadedRequest,
    UploadedResponse,
    UploadFailedRequest,
    UploadFailedResponse,
    UploadFileS3Payload,
)


def _valid_payload_kwargs() -> dict[str, object]:
    return {
        "file_id": uuid.uuid4(),
        "original_path": "/data/music/track.mp3",
        "part_urls": ["https://s3.test/bucket/key?partNumber=1", "https://s3.test/bucket/key?partNumber=2"],
        "part_size_bytes": 64 * 1024 * 1024,
        "agent_id": "fileserver-1",
    }


def test_upload_payload_requires_all_fields() -> None:
    """UploadFileS3Payload binds file_id + ordered part URLs + part_size_bytes + agent_id."""
    p = UploadFileS3Payload(**_valid_payload_kwargs())
    assert p.original_path == "/data/music/track.mp3"
    assert p.part_urls[0].endswith("partNumber=1")
    assert p.part_size_bytes == 64 * 1024 * 1024
    assert p.agent_id == "fileserver-1"


def test_upload_payload_rejects_extra() -> None:
    """extra='forbid' rejects any unexpected field."""
    kwargs = _valid_payload_kwargs()
    kwargs["unexpected"] = "x"
    with pytest.raises(pydantic.ValidationError) as exc_info:
        UploadFileS3Payload(**kwargs)
    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_upload_payload_rejects_relative_original_path() -> None:
    """original_path must be absolute (mirrors PushFilePayload)."""
    kwargs = _valid_payload_kwargs()
    kwargs["original_path"] = "relative/track.mp3"
    with pytest.raises(pydantic.ValidationError):
        UploadFileS3Payload(**kwargs)


def test_upload_payload_rejects_non_http_part_url() -> None:
    """A part URL that is not http(s) is rejected by a field validator (SSRF/scheme guard)."""
    kwargs = _valid_payload_kwargs()
    kwargs["part_urls"] = ["file:///etc/passwd"]
    with pytest.raises(pydantic.ValidationError):
        UploadFileS3Payload(**kwargs)


def test_upload_payload_accepts_http_part_url() -> None:
    """Plain http(s) part URLs are accepted."""
    kwargs = _valid_payload_kwargs()
    kwargs["part_urls"] = ["http://minio.local/bucket/key?partNumber=1"]
    p = UploadFileS3Payload(**kwargs)
    assert p.part_urls == ["http://minio.local/bucket/key?partNumber=1"]


def test_uploaded_part_part_number_ge_1() -> None:
    """UploadedPart.part_number must be >= 1 (S3 part numbers are 1-based)."""
    assert UploadedPart(part_number=1, etag="abc").part_number == 1
    with pytest.raises(pydantic.ValidationError):
        UploadedPart(part_number=0, etag="abc")


def test_uploaded_part_rejects_empty_etag() -> None:
    """UploadedPart.etag must be non-empty -- an empty ETag breaks CompleteMultipartUpload (WR-04)."""
    with pytest.raises(pydantic.ValidationError):
        UploadedPart(part_number=1, etag="")


def test_uploaded_request_carries_ordered_parts_no_identity() -> None:
    """UploadedRequest carries the ordered (part_number, etag) list and NO identity (AUTH-01)."""
    req = UploadedRequest(parts=[UploadedPart(part_number=1, etag="e1"), UploadedPart(part_number=2, etag="e2")])
    assert [p.part_number for p in req.parts] == [1, 2]


def test_uploaded_request_rejects_identity_in_body() -> None:
    """AUTH-01: extra='forbid' rejects any attempt to smuggle file_id/agent_id in the body."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        UploadedRequest.model_validate({"parts": [], "file_id": str(uuid.uuid4())})
    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_uploaded_response_echoes_file_id_and_status() -> None:
    """UploadedResponse echoes file_id with a fixed 'uploaded' status."""
    fid = uuid.uuid4()
    r = UploadedResponse(file_id=fid)
    assert r.file_id == fid
    assert r.status == "uploaded"
    with pytest.raises(pydantic.ValidationError):
        UploadedResponse.model_validate({"file_id": str(fid), "status": "nope"})


def test_uploaded_response_tolerates_unknown_field() -> None:
    """phaze-3ggc: a control-plane-first rolling deploy adding an additive field to the
    /uploaded echo must not raise on an older agent's model_validate."""
    fid = uuid.uuid4()
    r = UploadedResponse.model_validate({"file_id": str(fid), "status": "uploaded", "storage_class": "STANDARD"})
    assert r.file_id == fid
    assert r.status == "uploaded"


def test_upload_failed_request_detail_optional_and_bounded() -> None:
    """UploadFailedRequest carries only an optional bounded diagnostic detail."""
    assert UploadFailedRequest().detail is None
    assert UploadFailedRequest(detail="part 2 returned 500").detail == "part 2 returned 500"
    with pytest.raises(pydantic.ValidationError):
        UploadFailedRequest(detail="x" * 2001)


def test_upload_failed_response_echoes_file_id_status_cleared() -> None:
    """UploadFailedResponse echoes file_id + Literal 'failed' status + cleared flag."""
    fid = uuid.uuid4()
    r = UploadFailedResponse(file_id=fid, cleared=True)
    assert r.file_id == fid
    assert r.status == "failed"
    assert r.cleared is True
    with pytest.raises(pydantic.ValidationError):
        UploadFailedResponse.model_validate({"file_id": str(fid)})


def test_upload_failed_response_tolerates_unknown_field() -> None:
    """phaze-3ggc: same forward-compat guarantee for the /failed echo."""
    fid = uuid.uuid4()
    r = UploadFailedResponse.model_validate({"file_id": str(fid), "status": "failed", "cleared": False, "retry_after": 5})
    assert r.file_id == fid
    assert r.cleared is False
