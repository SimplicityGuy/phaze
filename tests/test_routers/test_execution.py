"""Integration tests for execution endpoints -- execute trigger, SSE progress, audit log."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_test_execution_log(
    session: AsyncSession,
    *,
    operation: str = "copy",
    source_path: str = "/music/old.mp3",
    destination_path: str = "/music/new.mp3",
    sha256_verified: bool = True,
    status: str = ExecutionStatus.COMPLETED,
    error_message: str | None = None,
) -> ExecutionLog:
    """Create an ExecutionLog entry for testing."""
    # Create prerequisite file and proposal
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/test.mp3",
        original_filename="test.mp3",
        current_path=source_path,
        file_type="music",
        file_size=1_000_000,
        state=FileState.EXECUTED,
    )
    session.add(file_record)
    await session.flush()

    proposal_id = uuid.uuid4()
    proposal = RenameProposal(
        id=proposal_id,
        file_id=file_id,
        proposed_filename="new.mp3",
        confidence=0.9,
        status=ProposalStatus.APPROVED,
        context_used={"artist": "Test"},
        reason="Test",
    )
    session.add(proposal)
    await session.flush()

    log_entry = ExecutionLog(
        id=uuid.uuid4(),
        proposal_id=proposal_id,
        operation=operation,
        source_path=source_path,
        destination_path=destination_path,
        sha256_verified=sha256_verified,
        status=status,
        error_message=error_message,
        executed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(log_entry)
    await session.commit()
    return log_entry


@pytest.mark.asyncio
async def test_audit_log_page(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/ returns 200 with HTML containing Audit Log heading."""
    await create_test_execution_log(session)
    response = await client.get("/audit/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Audit Log" in response.text


@pytest.mark.asyncio
async def test_audit_log_page_htmx(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/ with HX-Request header returns partial (audit_table only)."""
    await create_test_execution_log(session)
    response = await client.get("/audit/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text.lower()
    assert "audit-table-container" in response.text


@pytest.mark.asyncio
async def test_audit_log_filter(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/?status=completed returns filtered results."""
    await create_test_execution_log(session, status=ExecutionStatus.COMPLETED, source_path="/music/completed.mp3")
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, source_path="/music/failed.mp3", error_message="Hash mismatch")
    response = await client.get("/audit/?status=completed")
    assert response.status_code == 200
    assert "/music/completed.mp3" in response.text
    assert "/music/failed.mp3" not in response.text


@pytest.mark.asyncio
async def test_audit_log_empty_state(client: AsyncClient) -> None:
    """GET /audit/ with no logs returns empty state message."""
    response = await client.get("/audit/")
    assert response.status_code == 200
    assert "No operations recorded" in response.text


@pytest.mark.asyncio
async def test_execute_approved(client: AsyncClient) -> None:
    """POST /execution/start returns HTML with SSE progress container."""
    # Mock queue on the app
    mock_queue = AsyncMock()
    mock_queue.enqueue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    response = await client.post("/execution/start")
    assert response.status_code == 200
    assert "sse-connect" in response.text
    assert "execution/progress/" in response.text
    mock_queue.enqueue.assert_called_once()
    call_args = mock_queue.enqueue.call_args
    assert call_args.args[0] == "execute_approved_batch"


@pytest.mark.asyncio
async def test_sse_progress(client: AsyncClient) -> None:
    """GET /execution/progress/{batch_id} returns text/event-stream content type."""
    batch_id = uuid.uuid4().hex

    # Mock queue with Redis hgetall that returns progress data
    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(
        return_value={
            b"total": b"10",
            b"completed": b"5",
            b"failed": b"0",
            b"status": b"complete",
        }
    )
    mock_queue = MagicMock()
    mock_queue.redis = mock_redis
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    response = await client.get(f"/execution/progress/{batch_id}")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_execute_button_disabled(client: AsyncClient) -> None:
    """Proposals page renders disabled execute button when no approved proposals."""
    response = await client.get("/proposals/")
    assert response.status_code == 200
    # The button should be disabled (no approved proposals)
    assert "Execute Approved" in response.text
    assert "cursor-not-allowed" in response.text


@pytest.mark.asyncio
async def test_audit_log_stats_in_filter_tabs(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/ shows correct counts in filter tabs."""
    await create_test_execution_log(session, status=ExecutionStatus.COMPLETED, source_path="/a.mp3")
    await create_test_execution_log(session, status=ExecutionStatus.COMPLETED, source_path="/b.mp3")
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, source_path="/c.mp3", error_message="err")
    response = await client.get("/audit/")
    assert response.status_code == 200
    # Should show total of 3 and 2 completed
    assert "All (3)" in response.text
    assert "Completed (2)" in response.text
    assert "Failed (1)" in response.text


@pytest.mark.asyncio
async def test_collision_gate_blocks_execution(client: AsyncClient) -> None:
    """POST /execution/start returns collision block HTML when collisions exist."""
    mock_queue = AsyncMock()
    mock_queue.enqueue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    with patch("phaze.routers.execution.detect_collisions", new_callable=AsyncMock) as mock_detect:
        mock_detect.return_value = [("performances/artists/Disclosure/file.mp3", 2)]
        response = await client.post("/execution/start")

    assert response.status_code == 200
    assert "Path collisions detected" in response.text
    assert "performances/artists/Disclosure/file.mp3" in response.text
    mock_queue.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_no_collision_proceeds_normally(client: AsyncClient) -> None:
    """POST /execution/start proceeds with execution when no collisions detected."""
    mock_queue = AsyncMock()
    mock_queue.enqueue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    with patch("phaze.routers.execution.detect_collisions", new_callable=AsyncMock) as mock_detect:
        mock_detect.return_value = []
        response = await client.post("/execution/start")

    assert response.status_code == 200
    assert "sse-connect" in response.text
    mock_queue.enqueue.assert_called_once()
