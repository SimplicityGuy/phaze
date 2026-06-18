"""Respx happy-path tests for Phase-26-new PhazeAgentClient methods (GAP-2).

One test per new endpoint method introduced in Phase 26:
- create_tracklist   -> POST /api/internal/agent/tracklists
- patch_proposal_state -> PATCH /api/internal/agent/proposals/{id}/state
- post_execution_log -> POST /api/internal/agent/execution-log
- patch_execution_log -> PATCH /api/internal/agent/execution-log/{id}
- heartbeat          -> POST /api/internal/agent/heartbeat (204 No Content)

Each test verifies URL construction, serialized request body, and response model type.
Follows the fixture pattern in tests/test_services/test_agent_client.py.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from phaze.services.agent_client import AgentApiClientError, PhazeAgentClient


_BASE_URL = "http://app.test"
_TOKEN = "phaze_agent_test-token-1234567890abcdef"


@pytest.fixture
async def client():  # type: ignore[no-untyped-def]
    """Fresh PhazeAgentClient; closes underlying AsyncClient on teardown."""
    c = PhazeAgentClient(base_url=_BASE_URL, token=_TOKEN, timeout=5.0)
    yield c
    await c.close()


@respx.mock
async def test_create_tracklist_posts_to_correct_url_and_returns_response_model(client):  # type: ignore[no-untyped-def]
    """create_tracklist -> POST /api/internal/agent/tracklists, returns TracklistCreateResponse."""
    from phaze.schemas.agent_tracklists import TracklistCreatePayload, TracklistCreateResponse, TracklistTrackPayload

    tracklist_id = uuid.uuid4()
    file_id = uuid.uuid4()
    request_id = uuid.uuid4()

    route = respx.post(f"{_BASE_URL}/api/internal/agent/tracklists").mock(
        return_value=httpx.Response(
            200,
            json={
                "tracklist_id": str(tracklist_id),
                "version": 1,
                "track_count": 1,
            },
        ),
    )

    payload = TracklistCreatePayload(
        file_id=file_id,
        source="fingerprint",
        external_id="ext-001",
        tracks=[TracklistTrackPayload(position=0, title="Track One")],
        request_id=request_id,
    )

    result = await client.create_tracklist(payload)

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, TracklistCreateResponse), f"Expected TracklistCreateResponse, got {type(result)}"
    assert result.tracklist_id == tracklist_id
    assert result.track_count == 1

    sent_body = json.loads(route.calls.last.request.content)
    assert str(file_id) in json.dumps(sent_body), "file_id not serialized into request body"


@respx.mock
async def test_patch_proposal_state_uses_correct_url_and_exclude_unset(client):  # type: ignore[no-untyped-def]
    """patch_proposal_state -> PATCH /api/internal/agent/proposals/{id}/state, exclude_unset=True."""
    from phaze.schemas.agent_proposals import ProposalStatePatch, ProposalStateResponse

    proposal_id = uuid.uuid4()

    route = respx.patch(f"{_BASE_URL}/api/internal/agent/proposals/{proposal_id}/state").mock(
        return_value=httpx.Response(
            200,
            json={
                "proposal_id": str(proposal_id),
                "proposal_state": "executed",
                "file_state": "moved",
                "current_path": "/data/music/moved.mp3",
            },
        ),
    )

    # Use a partial patch (exclude_unset=True means only proposal_state + file_state + current_path sent)
    payload = ProposalStatePatch(
        proposal_state="executed",
        file_state="moved",
        current_path="/data/music/moved.mp3",
    )

    result = await client.patch_proposal_state(proposal_id, payload)

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, ProposalStateResponse), f"Expected ProposalStateResponse, got {type(result)}"
    assert result.proposal_id == proposal_id
    assert result.proposal_state == "executed"

    sent_body = json.loads(route.calls.last.request.content)
    # exclude_unset=True -- error_message was not set, so it must not appear in the body
    assert "error_message" not in sent_body, "error_message should be excluded (exclude_unset=True)"


@respx.mock
async def test_post_execution_log_posts_to_correct_url_and_returns_response_model(client):  # type: ignore[no-untyped-def]
    """post_execution_log -> POST /api/internal/agent/execution-log, returns ExecutionLogCreateResponse."""
    from phaze.enums.execution import ExecutionStatus
    from phaze.schemas.agent_execution import ExecutionLogCreate, ExecutionLogCreateResponse

    log_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    execution_log_id = uuid.uuid4()

    route = respx.post(f"{_BASE_URL}/api/internal/agent/execution-log").mock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": "agent-01",
                "execution_log_id": str(execution_log_id),
            },
        ),
    )

    payload = ExecutionLogCreate(
        id=log_id,
        proposal_id=proposal_id,
        operation="move",
        source_path="/data/orig.mp3",
        destination_path="/data/new.mp3",
        sha256_verified=True,
        status=ExecutionStatus.PENDING,
    )

    result = await client.post_execution_log(payload)

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, ExecutionLogCreateResponse), f"Expected ExecutionLogCreateResponse, got {type(result)}"
    assert result.execution_log_id == execution_log_id

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["operation"] == "move"
    assert sent_body["sha256_verified"] is True


@respx.mock
async def test_patch_execution_log_uses_correct_url_and_returns_response_model(client):  # type: ignore[no-untyped-def]
    """patch_execution_log -> PATCH /api/internal/agent/execution-log/{id}, returns ExecutionLogPatchResponse."""
    from phaze.enums.execution import ExecutionStatus
    from phaze.schemas.agent_execution import ExecutionLogPatch, ExecutionLogPatchResponse

    execution_log_id = uuid.uuid4()

    route = respx.patch(f"{_BASE_URL}/api/internal/agent/execution-log/{execution_log_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": "agent-01",
                "execution_log_id": str(execution_log_id),
                "status": "completed",
            },
        ),
    )

    payload = ExecutionLogPatch(status=ExecutionStatus.COMPLETED)

    result = await client.patch_execution_log(execution_log_id, payload)

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, ExecutionLogPatchResponse), f"Expected ExecutionLogPatchResponse, got {type(result)}"
    assert result.execution_log_id == execution_log_id
    assert result.status == ExecutionStatus.COMPLETED

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["status"] == "completed"
    # error_message not set -> excluded (exclude_unset=True used in patch_execution_log)
    assert "error_message" not in sent_body


@respx.mock
async def test_patch_scan_batch_uses_correct_url_and_exclude_unset(client):  # type: ignore[no-untyped-def]
    """patch_scan_batch -> PATCH /api/internal/agent/scan-batches/{id}, exclude_unset=True (Phase 27 D-10)."""
    from phaze.schemas.agent_scan_batches import ScanBatchPatch, ScanBatchPatchResponse

    batch_id = uuid.uuid4()

    route = respx.patch(f"{_BASE_URL}/api/internal/agent/scan-batches/{batch_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "batch_id": str(batch_id),
                "agent_id": "agent-01",
                "scan_path": "/data/music",
                "status": "completed",
                "total_files": 42,
                "processed_files": 42,
                "error_message": None,
            },
        ),
    )

    # Partial patch: only status + total_files set; processed_files + error_message
    # must NOT appear in the wire body (exclude_unset=True).
    payload = ScanBatchPatch(status="completed", total_files=42)

    result = await client.patch_scan_batch(batch_id, payload)

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, ScanBatchPatchResponse), f"Expected ScanBatchPatchResponse, got {type(result)}"
    assert result.batch_id == batch_id
    assert result.status == "completed"
    assert result.total_files == 42

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["status"] == "completed"
    assert sent_body["total_files"] == 42
    # exclude_unset=True -- processed_files + error_message were not set, so must not appear
    assert "processed_files" not in sent_body, "processed_files should be excluded (exclude_unset=True)"
    assert "error_message" not in sent_body, "error_message should be excluded (exclude_unset=True)"


@respx.mock
async def test_report_analysis_failed_posts_to_correct_url_and_returns_response_model(client):  # type: ignore[no-untyped-def]
    """report_analysis_failed -> POST /api/internal/agent/analysis/{file_id}/failed, returns AnalysisFailureResponse."""
    from phaze.schemas.agent_analysis import AnalysisFailurePayload, AnalysisFailureResponse

    file_id = uuid.uuid4()

    route = respx.post(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}/failed").mock(
        return_value=httpx.Response(
            200,
            json={"agent_id": "agent-01", "file_id": str(file_id)},
        ),
    )

    payload = AnalysisFailurePayload(reason="timeout", error="killed after 7200s")

    result = await client.report_analysis_failed(file_id, payload)

    assert route.called
    assert route.call_count == 1
    assert isinstance(result, AnalysisFailureResponse), f"Expected AnalysisFailureResponse, got {type(result)}"
    assert result.agent_id == "agent-01"
    assert result.file_id == file_id

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["reason"] == "timeout"
    assert sent_body["error"] == "killed after 7200s"


@respx.mock
async def test_report_analysis_failed_does_not_retry_on_4xx(client):  # type: ignore[no-untyped-def]
    """A 422 from the failure endpoint surfaces immediately as AgentApiClientError (no retry, D-11/D-12)."""
    from phaze.schemas.agent_analysis import AnalysisFailurePayload

    file_id = uuid.uuid4()

    route = respx.post(f"{_BASE_URL}/api/internal/agent/analysis/{file_id}/failed").mock(
        return_value=httpx.Response(422, json={"detail": "bad body"}),
    )

    payload = AnalysisFailurePayload(reason="error")

    with pytest.raises(AgentApiClientError):
        await client.report_analysis_failed(file_id, payload)

    # 4xx must NOT be retried -- exactly one HTTP call.
    assert route.call_count == 1, "4xx must surface immediately without retry"


@respx.mock
async def test_heartbeat_posts_to_correct_url_and_returns_none(client):  # type: ignore[no-untyped-def]
    """heartbeat -> POST /api/internal/agent/heartbeat with 204, no exception raised, None returned."""
    from phaze.schemas.agent_heartbeat import HeartbeatRequest

    route = respx.post(f"{_BASE_URL}/api/internal/agent/heartbeat").mock(
        return_value=httpx.Response(204),
    )

    payload = HeartbeatRequest(agent_version="1.0.0", worker_pid=12345, queue_depth=3)

    result = await client.heartbeat(payload)

    assert route.called
    assert route.call_count == 1
    assert result is None, f"heartbeat() should return None, got {result!r}"

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["agent_version"] == "1.0.0"
    assert sent_body["worker_pid"] == 12345
    assert sent_body["queue_depth"] == 3
