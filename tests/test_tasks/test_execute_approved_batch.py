"""Contract tests for phaze.tasks.execution.execute_approved_batch (Phase 26 B2 Option A).

Four scenarios:
* Happy path: 3 proposals all succeed.
* Partial failure: middle proposal hits IO error, siblings succeed.
* Path traversal: proposed_path escapes scan_root.
* sha256 mismatch: declared hash differs from file contents.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from phaze.config import AgentSettings
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.tasks.execution import execute_approved_batch


if TYPE_CHECKING:
    from pathlib import Path


def _make_api_client_mock() -> AsyncMock:
    """Mock PhazeAgentClient with all 3 methods used by execute_approved_batch."""
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    return api


def _seed_files(tmp_path: Path, count: int) -> tuple[list[Path], list[Path]]:
    """Create `count` orig files under tmp_path/orig and target paths under tmp_path/new."""
    orig_paths: list[Path] = []
    proposed_paths: list[Path] = []
    for i in range(count):
        o = tmp_path / "orig" / f"track{i}.mp3"
        o.parent.mkdir(parents=True, exist_ok=True)
        o.write_bytes(f"audio-content-{i}".encode())
        n = tmp_path / "new" / f"track{i}.mp3"
        orig_paths.append(o)
        proposed_paths.append(n)
    return orig_paths, proposed_paths


def _patch_settings(monkeypatch: pytest.MonkeyPatch, scan_roots: list[str]) -> None:
    """Stub get_settings() to return an AgentSettings-shaped mock with given scan_roots."""
    fake_cfg = MagicMock(spec=AgentSettings)
    fake_cfg.scan_roots = scan_roots
    monkeypatch.setattr("phaze.tasks.execution.get_settings", lambda: fake_cfg)


async def test_execute_approved_batch_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3 proposals all succeed -> 3 patch_proposal_state(executed) + 3 post/patch logs."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 3)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(o),
            proposed_path=str(p),
        )
        for o, p in zip(orig_paths, proposed_paths, strict=True)
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["processed_count"] == 3
    assert result["error_count"] == 0
    assert api.post_execution_log.await_count == 3
    assert api.patch_execution_log.await_count == 3
    assert api.patch_proposal_state.await_count == 3
    # All file ops happened: orig is gone, proposed exists
    for o, p in zip(orig_paths, proposed_paths, strict=True):
        assert not o.exists(), f"original still exists: {o}"
        assert p.exists(), f"proposed not created: {p}"
    # Every proposal_state call carries proposal_state='executed'
    states = [call.args[1].proposal_state for call in api.patch_proposal_state.await_args_list]
    assert states == ["executed", "executed", "executed"]


async def test_execute_approved_batch_partial_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3 proposals, middle IO-fails -> 1 failed + 2 executed; final status=completed_with_errors."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 3)
    # Delete the middle original to force a read failure
    orig_paths[1].unlink()
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(o),
            proposed_path=str(p),
        )
        for o, p in zip(orig_paths, proposed_paths, strict=True)
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["processed_count"] == 3
    assert result["error_count"] == 1
    # All 3 proposals reported state (1 failed + 2 executed)
    assert api.patch_proposal_state.await_count == 3
    states = [call.args[1].proposal_state for call in api.patch_proposal_state.await_args_list]
    assert states.count("executed") == 2
    assert states.count("failed") == 1


async def test_execute_approved_batch_path_escape_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proposed_path escapes scan_root -> proposal fails, no file op attempted (T-26-11-S1)."""
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    _patch_settings(monkeypatch, [str(allowed_root)])
    api = _make_api_client_mock()
    orig = allowed_root / "ok.mp3"
    orig.write_bytes(b"x")
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(orig),
            proposed_path="/etc/passwd",  # outside scan_root -- T-26-11-S1
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    # Original still exists -- no file op attempted
    assert orig.exists()
    # Failure reported
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"


async def test_execute_approved_batch_sha256_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sha256_hash supplied + content differs -> that proposal fails; others succeed."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 2)
    # First proposal: correct hash
    correct_hash = hashlib.sha256(orig_paths[0].read_bytes()).hexdigest()
    # Second proposal: wrong hash
    wrong_hash = "0" * 64
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(orig_paths[0]),
            proposed_path=str(proposed_paths[0]),
            sha256_hash=correct_hash,
        ),
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(orig_paths[1]),
            proposed_path=str(proposed_paths[1]),
            sha256_hash=wrong_hash,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["error_count"] == 1
    assert result["processed_count"] == 2
    # First file moved; second untouched
    assert proposed_paths[0].exists()
    assert not orig_paths[0].exists()
    assert orig_paths[1].exists()
    assert not proposed_paths[1].exists()


async def test_execute_approved_batch_original_path_escape_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """original_path escapes scan_root -> proposal fails, no file op attempted (GAP-4 / T-26-11-S1-mirror).

    Mirrors test_execute_approved_batch_path_escape_rejected but flips which field carries the escape:
    here original_path="/etc/shadow" (outside scan_root) while proposed_path is valid.
    Verifies that _resolve_and_check_containment is enforced on BOTH paths, not just proposed_path.
    """
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    _patch_settings(monkeypatch, [str(allowed_root)])
    api = _make_api_client_mock()
    proposed = allowed_root / "dest.mp3"
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path="/etc/shadow",  # outside scan_root -- GAP-4 escape via original_path
            proposed_path=str(proposed),
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["error_count"] == 1, f"Expected error_count=1, got {result['error_count']}"
    assert result["status"] == "completed_with_errors"
    # Proposed destination must not have been created (no file op attempted)
    assert not proposed.exists(), "proposed destination was created despite original_path escape rejection"
    # Failure reported via patch_proposal_state
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"


async def test_execute_approved_batch_requires_scan_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty scan_roots is a mis-deployment -> RuntimeError before any file ops."""
    _patch_settings(monkeypatch, [])
    api = _make_api_client_mock()
    o = tmp_path / "x.mp3"
    o.write_bytes(b"x")
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(o),
            proposed_path=str(tmp_path / "y.mp3"),
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    with pytest.raises(RuntimeError, match="scan_roots"):
        await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))
    api.patch_proposal_state.assert_not_awaited()
