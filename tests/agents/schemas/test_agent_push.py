"""Unit tests for phaze.schemas.agent_push (Phase 50 push callbacks).

These ORM-free request/response models back the two internal-API push callbacks
(`/pushed` and `/mismatch`). file_id flows on the URL path (AUTH-01); request
bodies carry only optional diagnostics. Every REQUEST model is `extra="forbid"`;
RESPONSE models are `extra="ignore"` (phaze-3ggc, Phase 25 convention) so an
additive control-plane field never hard-fails an older agent's `model_validate`.
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_push import PushedResponse, PushMismatchRequest, PushMismatchResponse


def test_pushed_response_default_status() -> None:
    """PushedResponse echoes file_id with a fixed 'pushed' status."""
    fid = uuid.uuid4()
    r = PushedResponse(file_id=fid)
    assert r.file_id == fid
    assert r.status == "pushed"


def test_pushed_response_rejects_other_status() -> None:
    """The status Literal only admits 'pushed'."""
    with pytest.raises(pydantic.ValidationError):
        PushedResponse.model_validate({"file_id": str(uuid.uuid4()), "status": "nope"})


def test_push_mismatch_request_detail_optional() -> None:
    """PushMismatchRequest carries only an optional diagnostic detail."""
    assert PushMismatchRequest().detail is None
    assert PushMismatchRequest(detail="sha256 differed").detail == "sha256 differed"


def test_push_mismatch_request_rejects_identity_in_body() -> None:
    """AUTH-01: extra='forbid' rejects any attempt to smuggle file_id in the body."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        PushMismatchRequest.model_validate({"file_id": str(uuid.uuid4())})
    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_push_mismatch_request_detail_max_length() -> None:
    """detail is bounded to cap the huge-string DoS surface."""
    with pytest.raises(pydantic.ValidationError):
        PushMismatchRequest(detail="x" * 2001)


def test_push_mismatch_response_cleared_required() -> None:
    """PushMismatchResponse requires the `cleared` disposition flag."""
    fid = uuid.uuid4()
    r = PushMismatchResponse(file_id=fid, cleared=True)
    assert r.file_id == fid
    assert r.status == "mismatch"
    assert r.cleared is True
    with pytest.raises(pydantic.ValidationError):
        PushMismatchResponse.model_validate({"file_id": str(fid)})


def test_pushed_response_tolerates_unknown_field() -> None:
    """phaze-3ggc: a control-plane-first rolling deploy adding an additive field to the
    /pushed echo must not raise on an older agent's model_validate."""
    fid = uuid.uuid4()
    r = PushedResponse.model_validate({"file_id": str(fid), "status": "pushed", "enqueued": True})
    assert r.file_id == fid
    assert r.status == "pushed"


def test_push_mismatch_response_tolerates_unknown_field() -> None:
    """phaze-3ggc: same forward-compat guarantee for the /mismatch echo."""
    fid = uuid.uuid4()
    r = PushMismatchResponse.model_validate({"file_id": str(fid), "status": "mismatch", "cleared": False, "retry_after": 5})
    assert r.file_id == fid
    assert r.cleared is False
