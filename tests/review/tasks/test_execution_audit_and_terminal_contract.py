"""Audit reconstruction and terminal-token contracts for execution orchestration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.agent_client import AgentApiServerError
import phaze.tasks.execution as execmod
from phaze.tasks.execution import execute_approved_batch
from tests.review.tasks._execution_contract_scenarios import (
    _make_api_client_mock,
    _make_job_mock,
    _patch_settings,
    _seed_files,
)


if TYPE_CHECKING:
    from pathlib import Path


def _failing_then_succeeding_post_execution_log() -> AsyncMock:
    """post_execution_log that fails the FIRST call (the write-ahead row) and succeeds after.

    Models the real trigger: a transient hub failure window that outlasts the POST's tenacity
    budget but has closed again by the time the terminal report is made.
    """
    calls = {"n": 0}

    async def _side_effect(_body: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AgentApiServerError("hub shedding load: 503")
        return MagicMock(execution_log_id=uuid.uuid4())

    return AsyncMock(side_effect=_side_effect)


async def test_lost_completion_token_on_success_path_raises_for_saq_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed sub_batch_terminal POST must fail the JOB, not return a clean 'completed'.

    Under the old blanket D-16 swallow the task returned status='completed', so SAQ never retried
    and the only writer of ``subjobs_completed`` was lost forever: the batch spun at 'running'
    until its 24h TTL and every subsequent Execute Approved was refused for that whole window.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = AsyncMock(side_effect=AgentApiServerError("hub restarting behind the proxy"))
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

    with caplog.at_level(logging.ERROR), pytest.raises(execmod.ExecBatchTerminalReportError):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # The move itself still committed -- the replay is safe precisely because the work is done and
    # every downstream write on the replay path is idempotent.
    assert proposed_paths[0].exists()
    assert not orig_paths[0].exists()
    assert any("terminal completion event lost" in r.message for r in caplog.records)


async def test_lost_completion_token_does_not_mark_the_proposal_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-raise must bypass the generic per-proposal failure handler.

    The token POST fires from inside ``_execute_one``'s main try block, so a naive raise would be
    caught by the generic ``except Exception`` and reported as a per-proposal FAILURE -- flipping an
    executed proposal to FAILED and posting terminal_step='failed' for a file sitting correctly at
    its destination. The undeliverable token is a transport problem, not a file-op problem.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = AsyncMock(side_effect=AgentApiServerError("upstream 502"))
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

    with pytest.raises(execmod.ExecBatchTerminalReportError):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # The proposal was reported EXECUTED before the token POST, and never re-reported as failed.
    states = [c.args[1].proposal_state for c in api.patch_proposal_state.await_args_list]
    assert states == ["executed"]
    # The completed audit PATCH stands; no FAILED patch was issued.
    log_statuses = [c.args[1].status for c in api.patch_execution_log.await_args_list]
    assert ExecutionStatus.FAILED not in log_statuses


async def test_lost_completion_token_on_failure_path_also_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The twin site: a sub-batch whose LAST proposal legitimately FAILED carries the token too.

    Its loss strands the batch identically, so the failure-path POST gets the same treatment.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = AsyncMock(side_effect=AgentApiServerError("upstream 502"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
            sha256_hash="0" * 64,  # forces a verify failure -> the FAILURE-path token POST
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with pytest.raises(execmod.ExecBatchTerminalReportError):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # The genuine per-proposal failure was still reported before the token POST was attempted.
    states = [c.args[1].proposal_state for c in api.patch_proposal_state.await_args_list]
    assert states == ["failed"]


async def test_failed_start_log_is_recreated_before_the_completed_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed start POST must be re-POSTed before the terminal PATCH, not left to 404 (phaze-87ba)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_execution_log = _failing_then_succeeding_post_execution_log()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposal_id = uuid.uuid4()
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert proposed_paths[0].exists()
    # The row was re-created, so the PATCH has something to update.
    assert api.post_execution_log.await_count == 2
    # Both POSTs carry the SAME retry-stable id, which is what makes the re-POST a safe no-op
    # against the controller's ON CONFLICT (id) DO NOTHING insert.
    ids = {c.args[0].id for c in api.post_execution_log.await_args_list}
    assert len(ids) == 1
    # ...and the terminal PATCH targets that same id.
    assert api.patch_execution_log.await_args.args[0] == next(iter(ids))
    assert api.patch_execution_log.await_args.args[1].status == ExecutionStatus.COMPLETED


async def test_failed_start_log_is_recreated_before_the_failed_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FAILED audit row is lost the same way and gets the same heal (phaze-87ba)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_execution_log = _failing_then_succeeding_post_execution_log()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
            sha256_hash="0" * 64,  # forces a verify failure
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert api.post_execution_log.await_count == 2
    assert api.patch_execution_log.await_args.args[1].status == ExecutionStatus.FAILED


async def test_successful_start_log_is_not_re_posted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heal is conditional -- the ordinary path must stay at exactly one CREATE (phaze-87ba)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
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

    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert api.post_execution_log.await_count == 1


async def test_persistent_execution_log_outage_is_reported_at_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the re-POST also fails the move still completes, but the lost trail is logged at ERROR.

    The batch must not be failed over an audit-transport problem -- the file op is committed and
    correct -- but "this move has no audit trail" is not a WARNING-level fact.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_execution_log = AsyncMock(side_effect=AgentApiServerError("hub down for the whole batch"))
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

    with caplog.at_level(logging.ERROR, logger="phaze.tasks.execution"):
        result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert proposed_paths[0].exists()
    assert any("NO audit trail" in r.getMessage() for r in caplog.records)
