"""Tests for arq batch execution task with Redis progress tracking."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(proposal_id: uuid.UUID | None = None) -> MagicMock:
    proposal = MagicMock()
    proposal.id = proposal_id or uuid.uuid4()
    proposal.file = MagicMock()
    return proposal


def _make_ctx(redis: Any = None) -> dict[str, Any]:
    """Create a minimal arq context dict with Redis mock."""
    if redis is None:
        redis = AsyncMock()
    return {"redis": redis}


# ---------------------------------------------------------------------------
# Batch success
# ---------------------------------------------------------------------------


@patch("phaze.tasks.execution.execute_single_file", new_callable=AsyncMock)
@patch("phaze.tasks.execution.get_approved_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.execution._get_session", new_callable=AsyncMock)
async def test_execute_approved_batch_success(
    mock_get_session: AsyncMock,
    mock_get_proposals: AsyncMock,
    mock_execute: AsyncMock,
) -> None:
    """Batch with 2 approved proposals, both succeed. Returns completed=2, failed=0."""
    from phaze.tasks.execution import execute_approved_batch

    proposals = [_make_proposal(), _make_proposal()]
    mock_get_proposals.return_value = proposals
    mock_execute.return_value = True

    mock_session = AsyncMock()
    mock_get_session.return_value = mock_session

    redis = AsyncMock()
    ctx = _make_ctx(redis)

    result = await execute_approved_batch(ctx)

    assert result["completed"] == 2
    assert result["failed"] == 0
    assert result["total"] == 2
    assert "batch_id" in result
    assert mock_execute.call_count == 2
    mock_session.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------


@patch("phaze.tasks.execution.execute_single_file", new_callable=AsyncMock)
@patch("phaze.tasks.execution.get_approved_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.execution._get_session", new_callable=AsyncMock)
async def test_execute_approved_batch_partial_failure(
    mock_get_session: AsyncMock,
    mock_get_proposals: AsyncMock,
    mock_execute: AsyncMock,
) -> None:
    """Batch with 3 proposals, 1 fails. completed=2, failed=1. Processing continues (D-07)."""
    from phaze.tasks.execution import execute_approved_batch

    proposals = [_make_proposal(), _make_proposal(), _make_proposal()]
    mock_get_proposals.return_value = proposals
    mock_execute.side_effect = [True, False, True]

    mock_session = AsyncMock()
    mock_get_session.return_value = mock_session

    redis = AsyncMock()
    ctx = _make_ctx(redis)

    result = await execute_approved_batch(ctx)

    assert result["completed"] == 2
    assert result["failed"] == 1
    assert result["total"] == 3
    # All 3 were processed (D-07 continue on failure)
    assert mock_execute.call_count == 3


# ---------------------------------------------------------------------------
# Empty batch
# ---------------------------------------------------------------------------


@patch("phaze.tasks.execution.get_approved_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.execution._get_session", new_callable=AsyncMock)
async def test_execute_approved_batch_empty(
    mock_get_session: AsyncMock,
    mock_get_proposals: AsyncMock,
) -> None:
    """No approved proposals. Returns completed=0, failed=0 immediately."""
    from phaze.tasks.execution import execute_approved_batch

    mock_get_proposals.return_value = []

    mock_session = AsyncMock()
    mock_get_session.return_value = mock_session

    redis = AsyncMock()
    ctx = _make_ctx(redis)

    result = await execute_approved_batch(ctx)

    assert result["completed"] == 0
    assert result["failed"] == 0
    assert result["total"] == 0
    mock_session.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Redis progress updates
# ---------------------------------------------------------------------------


@patch("phaze.tasks.execution.execute_single_file", new_callable=AsyncMock)
@patch("phaze.tasks.execution.get_approved_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.execution._get_session", new_callable=AsyncMock)
async def test_redis_progress_updates(
    mock_get_session: AsyncMock,
    mock_get_proposals: AsyncMock,
    mock_execute: AsyncMock,
) -> None:
    """Redis hash updated after each file. Status transitions from running to complete."""
    from phaze.tasks.execution import execute_approved_batch

    proposals = [_make_proposal(), _make_proposal()]
    mock_get_proposals.return_value = proposals
    mock_execute.return_value = True

    mock_session = AsyncMock()
    mock_get_session.return_value = mock_session

    redis = AsyncMock()
    ctx = _make_ctx(redis)

    result = await execute_approved_batch(ctx, batch_id="test-batch-123")

    # Check Redis hset calls
    hset_calls = redis.hset.call_args_list
    assert len(hset_calls) >= 3  # initial + 2 updates + final status

    # Initial call sets total and status=running
    initial_call = hset_calls[0]
    assert initial_call[0][0] == "exec:test-batch-123"
    initial_mapping = initial_call[1]["mapping"]
    assert initial_mapping["total"] == 2
    assert initial_mapping["status"] == "running"

    # Final status should be "complete"
    # The last hset call should set status to complete
    last_call = hset_calls[-1]
    last_mapping = last_call[1]["mapping"]
    assert last_mapping["status"] == "complete"

    # TTL set on key
    redis.expire.assert_awaited()

    assert result["batch_id"] == "test-batch-123"


# ---------------------------------------------------------------------------
# Batch generates UUID id
# ---------------------------------------------------------------------------


@patch("phaze.tasks.execution.execute_single_file", new_callable=AsyncMock)
@patch("phaze.tasks.execution.get_approved_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.execution._get_session", new_callable=AsyncMock)
async def test_batch_generates_uuid_id(
    mock_get_session: AsyncMock,
    mock_get_proposals: AsyncMock,
    _mock_execute: AsyncMock,
) -> None:
    """execute_approved_batch generates a UUID batch_id if not provided."""
    from phaze.tasks.execution import execute_approved_batch

    mock_get_proposals.return_value = []
    mock_session = AsyncMock()
    mock_get_session.return_value = mock_session

    redis = AsyncMock()
    ctx = _make_ctx(redis)

    result = await execute_approved_batch(ctx)

    batch_id = result["batch_id"]
    # Should be a valid hex UUID (32 hex chars)
    assert len(batch_id) == 32
    uuid.UUID(batch_id)  # Validates it's a UUID hex string
