"""Best-effort reporting contracts for execution orchestration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.agent_client import AgentApiServerError
from phaze.tasks.execution import execute_approved_batch
from tests.review.tasks._execution_contract_scenarios import (
    _fail_only_telemetry_posts,
    _make_api_client_mock,
    _make_job_mock,
    _patch_settings,
    _payload_from_call,
    _seed_files,
)


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_post_execution_log_failure_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Start-of-op audit log POST raises -> WARNING logged, file op still attempted."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_execution_log = AsyncMock(side_effect=AgentApiServerError("upstream 503"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.WARNING):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert any("could not record start log" in r.message for r in caplog.records)
    assert proposed_paths[0].exists()
    assert not orig_paths[0].exists()
    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "deleted"


async def test_patch_completed_log_failure_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """patch_execution_log raising on the success path still produces a 'deleted' progress POST."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.patch_execution_log = AsyncMock(side_effect=AgentApiServerError("upstream 503"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.WARNING):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert any("could not patch completed log" in r.message for r in caplog.records)
    api.patch_proposal_state.assert_awaited()
    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "deleted"


async def test_patch_failed_log_failure_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """patch_execution_log raising on the FAILED path still produces a 'failed' progress POST."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.patch_execution_log = AsyncMock(side_effect=AgentApiServerError("upstream 503"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
            sha256_hash="0" * 64,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.WARNING):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert any("could not patch failed log" in r.message for r in caplog.records)
    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "failed"
    assert sent.failed_at_step == "verify"


async def test_patch_proposal_state_failed_report_failure_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """patch_proposal_state raising on the FAILED report still produces a 'failed' progress POST."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.patch_proposal_state = AsyncMock(side_effect=AgentApiServerError("upstream 503"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
            sha256_hash="0" * 64,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.ERROR):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert any("failed to report failure" in r.message for r in caplog.records)
    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "failed"


async def test_progress_post_failure_on_success_path_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TELEMETRY progress POST raising on the SUCCESS path -> WARNING logged, batch still completes."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = _fail_only_telemetry_posts()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 2)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[i]),
            proposed_path="new",
            proposed_filename=proposed_paths[i].name,
        )
        for i in range(2)
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.WARNING):
        result = await execute_approved_batch(
            {"api_client": api, "job": job},
            **payload.model_dump(mode="json"),
        )

    assert any("progress POST failed" in r.message for r in caplog.records)
    assert all(p.exists() for p in proposed_paths)
    assert not any(p.exists() for p in orig_paths)
    assert result["status"] == "completed"


async def test_progress_post_failure_on_failure_path_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TELEMETRY progress POST raising on the FAILED path -> WARNING logged, batch still completes."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = _fail_only_telemetry_posts()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 2)
    # The FAILING proposal (bad hash) is first, so its progress POST is the telemetry half; the
    # clean second proposal carries the completion token.
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
            sha256_hash="0" * 64,
        ),
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[1]),
            proposed_path="new",
            proposed_filename=proposed_paths[1].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.WARNING):
        result = await execute_approved_batch(
            {"api_client": api, "job": job},
            **payload.model_dump(mode="json"),
        )

    assert any("progress POST failed" in r.message for r in caplog.records)
    # One failed proposal -> batch result is "completed_with_errors", not "completed".
    assert result["status"] == "completed_with_errors"


async def test_executed_state_patch_5xx_does_not_fail_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 503 on the executed-state PATCH (after a successful move) is swallowed.

    Before the fix the un-guarded success PATCH landed in the generic handler:
    proposal APPROVED->FAILED, failed_at_step misreported as 'delete', and
    FileRecord.current_path left pointing at the deleted original. Now the move
    is committed first, so the report failure is logged and the proposal still
    counts as executed.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)

    async def _raise_only_on_executed(_proposal_id: object, patch: object) -> None:
        # 503 ONLY on the success report; a 'failed' report (which must never be
        # reached in this scenario) would pass through.
        if getattr(patch, "proposal_state", None) == "executed":
            raise AgentApiServerError("503 reporting executed state")

    api.patch_proposal_state = AsyncMock(side_effect=_raise_only_on_executed)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.ERROR):
        result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # The move committed: file relocated, original gone.
    assert proposed_paths[0].exists()
    assert not orig_paths[0].exists()
    # Proposal is NOT marked failed.
    assert result["status"] == "completed"
    assert result["error_count"] == 0
    # No second (failed) report was attempted -- only the executed one fired.
    assert api.patch_proposal_state.await_count == 1
    reported_states = [c.args[1].proposal_state for c in api.patch_proposal_state.await_args_list]
    assert "failed" not in reported_states
    # The terminal progress POST reports SUCCESS ('deleted'), never 'failed'/'delete'.
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "deleted"
    assert sent.failed_at_step is None
    # The swallow was logged at ERROR (move committed, report failed).
    assert any("reporting executed state failed" in r.message for r in caplog.records)
