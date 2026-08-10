"""Unit tests for phaze.schemas.agent_scratch (phaze-5cvbz).

``ScratchLivenessRequest`` is a REQUEST model (extra="forbid", Phase 25 D-16 discipline);
``ScratchLivenessResponse`` is control-TRUSTED and stays loose (extra="ignore").
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_scratch import ScratchLivenessRequest, ScratchLivenessResponse


def test_request_defaults_to_empty_file_ids() -> None:
    req = ScratchLivenessRequest()
    assert req.file_ids == []


def test_request_accepts_explicit_file_ids() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    req = ScratchLivenessRequest(file_ids=ids)
    assert req.file_ids == ids


def test_request_rejects_extra_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        ScratchLivenessRequest.model_validate({"file_ids": [], "unexpected": "value"})


def test_request_rejects_more_than_max_length_ids() -> None:
    with pytest.raises(pydantic.ValidationError):
        ScratchLivenessRequest(file_ids=[uuid.uuid4() for _ in range(501)])


def test_request_accepts_exactly_max_length_ids() -> None:
    req = ScratchLivenessRequest(file_ids=[uuid.uuid4() for _ in range(500)])
    assert len(req.file_ids) == 500


def test_response_defaults() -> None:
    resp = ScratchLivenessResponse()
    assert resp.live_file_ids == []
    assert resp.push_file_active is False


def test_response_tolerates_additive_unknown_fields() -> None:
    """Forward-compat: a newer control plane adding a field must not break an older agent image."""
    resp = ScratchLivenessResponse.model_validate({"live_file_ids": [], "push_file_active": True, "future_field": "ignored"})
    assert resp.push_file_active is True
