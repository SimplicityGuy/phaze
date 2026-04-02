"""Tests for the fingerprint SAQ task function."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from phaze.models.file import FileState


def _make_ctx() -> dict[str, Any]:
    """Create a minimal SAQ context dict with async_session factory and orchestrator."""
    mock_session = AsyncMock()
    mock_session_factory = MagicMock()
    mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

    mock_orchestrator = AsyncMock()

    return {
        "async_session": mock_session_factory,
        "_mock_session": mock_session,
        "fingerprint_orchestrator": mock_orchestrator,
    }


def _make_file_record(
    file_id: uuid.UUID | None = None,
    state: str = "metadata_extracted",
    current_path: str = "/music/track.mp3",
) -> MagicMock:
    """Create a mock FileRecord."""
    record = MagicMock()
    record.id = file_id or uuid.uuid4()
    record.state = state
    record.current_path = current_path
    return record


def _make_ingest_result(status: str = "success", error: str | None = None) -> MagicMock:
    """Create a mock IngestResult."""
    result = MagicMock()
    result.status = status
    result.error = error
    return result


@pytest.mark.asyncio
async def test_both_engines_success_transitions_to_fingerprinted() -> None:
    """fingerprint_file with both engines succeeding transitions file to FINGERPRINTED."""
    from phaze.tasks.fingerprint import fingerprint_file

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    # First execute call: file record lookup
    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    # Second and third execute: FingerprintResult lookups for each engine (none found)
    mock_result_fprint1 = MagicMock()
    mock_result_fprint1.scalar_one_or_none.return_value = None
    mock_result_fprint2 = MagicMock()
    mock_result_fprint2.scalar_one_or_none.return_value = None
    session.execute.side_effect = [mock_result_file, mock_result_fprint1, mock_result_fprint2]

    # Both engines succeed
    ctx["fingerprint_orchestrator"].ingest_all.return_value = {
        "audfprint": _make_ingest_result("success"),
        "panako": _make_ingest_result("success"),
    }

    result = await fingerprint_file(ctx, file_id=str(file_record.id))
    assert result["status"] == "fingerprinted"
    assert file_record.state == FileState.FINGERPRINTED
    session.commit.assert_awaited_once()
    # Two new FingerprintResult rows added
    assert session.add.call_count == 2


@pytest.mark.asyncio
async def test_one_engine_fails_no_transition() -> None:
    """fingerprint_file with one engine failing does NOT transition to FINGERPRINTED."""
    from phaze.tasks.fingerprint import fingerprint_file

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record(state="metadata_extracted")

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_fprint1 = MagicMock()
    mock_result_fprint1.scalar_one_or_none.return_value = None
    mock_result_fprint2 = MagicMock()
    mock_result_fprint2.scalar_one_or_none.return_value = None
    session.execute.side_effect = [mock_result_file, mock_result_fprint1, mock_result_fprint2]

    ctx["fingerprint_orchestrator"].ingest_all.return_value = {
        "audfprint": _make_ingest_result("success"),
        "panako": _make_ingest_result("failed", error="HTTP 500: Internal Server Error"),
    }

    result = await fingerprint_file(ctx, file_id=str(file_record.id))
    assert result["status"] == "partial"
    assert file_record.state != FileState.FINGERPRINTED


@pytest.mark.asyncio
async def test_nonexistent_file_returns_not_found() -> None:
    """fingerprint_file with non-existent file_id returns not_found."""
    from phaze.tasks.fingerprint import fingerprint_file

    ctx = _make_ctx()
    session = ctx["_mock_session"]

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    result = await fingerprint_file(ctx, file_id=str(uuid.uuid4()))
    assert result["status"] == "not_found"
    ctx["fingerprint_orchestrator"].ingest_all.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_updates_existing_results() -> None:
    """fingerprint_file is idempotent -- running twice updates existing FingerprintResult rows."""
    from phaze.tasks.fingerprint import fingerprint_file

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    file_record = _make_file_record()

    # Simulate existing FingerprintResult rows (already fingerprinted before)
    existing_fprint1 = MagicMock()
    existing_fprint2 = MagicMock()

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_fprint1 = MagicMock()
    mock_result_fprint1.scalar_one_or_none.return_value = existing_fprint1
    mock_result_fprint2 = MagicMock()
    mock_result_fprint2.scalar_one_or_none.return_value = existing_fprint2
    session.execute.side_effect = [mock_result_file, mock_result_fprint1, mock_result_fprint2]

    ctx["fingerprint_orchestrator"].ingest_all.return_value = {
        "audfprint": _make_ingest_result("success"),
        "panako": _make_ingest_result("success"),
    }

    result = await fingerprint_file(ctx, file_id=str(file_record.id))
    assert result["status"] == "fingerprinted"
    # No new rows added -- existing ones updated in place
    session.add.assert_not_called()
    # Existing rows should have status updated
    assert existing_fprint1.status == "success"
    assert existing_fprint2.status == "success"


@pytest.mark.asyncio
async def test_exception_propagates() -> None:
    """fingerprint_file propagates exceptions (SAQ handles retry with backoff)."""
    from phaze.tasks.fingerprint import fingerprint_file

    ctx = _make_ctx()
    session = ctx["_mock_session"]
    session.execute.side_effect = RuntimeError("DB connection lost")

    with pytest.raises(RuntimeError, match="DB connection lost"):
        await fingerprint_file(ctx, file_id=str(uuid.uuid4()))
