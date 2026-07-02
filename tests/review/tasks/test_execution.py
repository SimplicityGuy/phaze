"""Legacy aggregate-shape tests for execute_approved_batch (Phase 26 Plan 11).

The detailed contract tests live in tests/test_tasks/test_execute_approved_batch.py
(per Plan 11 acceptance criteria). This file preserves a couple of small smoke
tests for the high-level success / partial-failure aggregate counts so that
``uv run pytest tests/test_tasks/`` continues to cover the SAQ-entrypoint
contract in two places.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

from phaze.config import AgentSettings
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.tasks.execution import execute_approved_batch


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_api_client_mock() -> AsyncMock:
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    return api


def _patch_settings(monkeypatch: pytest.MonkeyPatch, scan_roots: list[str]) -> None:
    fake_cfg = MagicMock(spec=AgentSettings)
    fake_cfg.scan_roots = scan_roots
    monkeypatch.setattr("phaze.tasks.execution.get_settings", lambda: fake_cfg)


async def test_execute_approved_batch_smoke_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2 proposals, both succeed -> aggregate status='completed'."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    o1 = tmp_path / "a.mp3"
    o2 = tmp_path / "b.mp3"
    o1.write_bytes(b"a")
    o2.write_bytes(b"b")
    p1 = tmp_path / "moved" / "a.mp3"
    p2 = tmp_path / "moved" / "b.mp3"

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(o1),
            proposed_path=str(p1),
        ),
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(o2),
            proposed_path=str(p2),
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-1", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["processed_count"] == 2
    assert result["error_count"] == 0


async def test_execute_approved_batch_smoke_partial_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1 succeeds + 1 missing-source-fails -> status='completed_with_errors', error_count=1."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    o_ok = tmp_path / "a.mp3"
    o_ok.write_bytes(b"a")
    p_ok = tmp_path / "moved" / "a.mp3"

    o_missing = tmp_path / "missing.mp3"  # intentionally never created
    p_missing = tmp_path / "moved" / "missing.mp3"

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(o_ok),
            proposed_path=str(p_ok),
        ),
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(o_missing),
            proposed_path=str(p_missing),
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-1", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    assert result["processed_count"] == 2
