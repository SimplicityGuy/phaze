"""Respx happy-path tests for the Phase 53 PhazeAgentClient upload callbacks (KSTAGE-02).

- report_upload_complete -> POST /api/internal/agent/s3/{file_id}/uploaded
- report_upload_failed   -> POST /api/internal/agent/s3/{file_id}/failed

Each test verifies URL construction (file_id on the path, AUTH-01), the serialized
request body, and the parsed response model type. Follows the fixture pattern in
tests/test_services/test_agent_client_endpoints.py.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from phaze.services.agent_client import PhazeAgentClient


_BASE_URL = "http://app.test"
_TOKEN = "phaze_agent_test-token-1234567890abcdef"


@pytest.fixture
async def client():  # type: ignore[no-untyped-def]
    """Fresh PhazeAgentClient; closes underlying AsyncClient on teardown."""
    c = PhazeAgentClient(base_url=_BASE_URL, token=_TOKEN, timeout=5.0)
    yield c
    await c.close()


@respx.mock
async def test_report_upload_complete_posts_parts_to_uploaded_url(client):  # type: ignore[no-untyped-def]
    """report_upload_complete -> POST /s3/{file_id}/uploaded with the ordered parts body."""
    from phaze.schemas.agent_s3 import UploadedPart, UploadedResponse

    file_id = uuid.uuid4()
    route = respx.post(f"{_BASE_URL}/api/internal/agent/s3/{file_id}/uploaded").mock(
        return_value=httpx.Response(200, json={"file_id": str(file_id), "status": "uploaded"}),
    )

    parts = [UploadedPart(part_number=1, etag="e1"), UploadedPart(part_number=2, etag="e2")]
    result = await client.report_upload_complete(file_id, parts)

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, UploadedResponse)
    assert result.file_id == file_id
    assert result.status == "uploaded"

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["parts"] == [{"part_number": 1, "etag": "e1"}, {"part_number": 2, "etag": "e2"}]
    # AUTH-01: identity rides the path, never the body.
    assert "file_id" not in sent_body
    assert "agent_id" not in sent_body


@respx.mock
async def test_report_upload_failed_posts_to_failed_url(client):  # type: ignore[no-untyped-def]
    """report_upload_failed -> POST /s3/{file_id}/failed, parses UploadFailedResponse."""
    from phaze.schemas.agent_s3 import UploadFailedResponse

    file_id = uuid.uuid4()
    route = respx.post(f"{_BASE_URL}/api/internal/agent/s3/{file_id}/failed").mock(
        return_value=httpx.Response(200, json={"file_id": str(file_id), "status": "failed", "cleared": False}),
    )

    result = await client.report_upload_failed(file_id, detail="part 2 returned 500")

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, UploadFailedResponse)
    assert result.file_id == file_id
    assert result.status == "failed"
    assert result.cleared is False

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["detail"] == "part 2 returned 500"
    assert "file_id" not in sent_body


@respx.mock
async def test_report_upload_failed_defaults_detail_none(client):  # type: ignore[no-untyped-def]
    """report_upload_failed with no detail sends a null detail body."""
    file_id = uuid.uuid4()
    route = respx.post(f"{_BASE_URL}/api/internal/agent/s3/{file_id}/failed").mock(
        return_value=httpx.Response(200, json={"file_id": str(file_id), "status": "failed", "cleared": True}),
    )

    result = await client.report_upload_failed(file_id)

    assert route.called
    assert result.cleared is True
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["detail"] is None
