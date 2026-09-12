"""Stable identity and retry contracts for execution orchestration."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
import uuid

from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.tasks.execution import execute_approved_batch
from tests.review.tasks._execution_contract_scenarios import (
    _make_api_client_mock,
    _make_job_mock,
    _patch_settings,
    _payload_from_call,
    _seed_files,
)


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_uuids_persisted_in_job_meta_on_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run with empty job.meta -> job.update called with all 4 UUID keys."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 2)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(o),
            proposed_path="new",
            proposed_filename=p.name,
        )
        for o, p in zip(orig_paths, proposed_paths, strict=True)
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # job.update was called -- at least once, with the merged meta dict.
    assert job.update.await_count >= 1
    last_meta = job.update.await_args.kwargs["meta"]
    for item in proposals:
        assert f"log_id:{item.proposal_id}" in last_meta
        assert f"req_id:{item.proposal_id}" in last_meta
        # Stored as strings (so SAQ can serialize via json).
        assert isinstance(last_meta[f"log_id:{item.proposal_id}"], str)
        assert isinstance(last_meta[f"req_id:{item.proposal_id}"], str)
        # Strings are valid UUIDs.
        uuid.UUID(last_meta[f"log_id:{item.proposal_id}"])
        uuid.UUID(last_meta[f"req_id:{item.proposal_id}"])


async def test_uuids_reused_from_job_meta_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-seeded job.meta -> UUIDs re-used (no re-seed update); POST'd UUIDs match.

    phaze-ebb46: this is a genuinely FRESH move (the destination doesn't exist yet), so
    ``job.update`` IS still called once -- not to re-seed the already-present UUIDs (closes
    L6/L22, unchanged), but to persist this bead's per-proposal "moved" corroboration flag
    immediately after the move commits.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)

    proposal_id = uuid.uuid4()
    preseeded_log_id = uuid.uuid4()
    preseeded_req_id = uuid.uuid4()
    job = _make_job_mock(
        initial_meta={
            f"log_id:{proposal_id}": str(preseeded_log_id),
            f"req_id:{proposal_id}": str(preseeded_req_id),
        },
    )

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
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # The UUID keys were already present -> the only job.update call is this bead's
    # per-proposal "moved" flag (phaze-ebb46), not a fresh UUID seed.
    assert job.update.await_count == 1
    updated_meta = job.update.await_args.kwargs["meta"]
    assert updated_meta[f"log_id:{proposal_id}"] == str(preseeded_log_id)
    assert updated_meta[f"req_id:{proposal_id}"] == str(preseeded_req_id)
    assert updated_meta[f"moved:{proposal_id}"] is not None

    # ExecutionLog POST re-used the preseeded log_id.
    assert api.post_execution_log.await_count == 1
    post_payload = api.post_execution_log.await_args.args[0]
    assert post_payload.id == preseeded_log_id

    # post_exec_batch_progress re-used the preseeded request_id.
    assert api.post_exec_batch_progress.await_count == 1
    progress_payload = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert progress_payload.request_id == preseeded_req_id


async def test_execution_log_and_progress_use_distinct_uuids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log_id (passed to post_execution_log) is distinct from request_id (passed to progress POST)."""
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

    log_post = api.post_execution_log.await_args.args[0]
    progress_post = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert log_post.id != progress_post.request_id


async def test_legacy_ctx_without_job_does_not_break(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ctx without 'job' -> still executes; UUIDs are freshly generated; no AttributeError."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
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
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    # Progress POST still fires (uses freshly-generated request_id).
    assert api.post_exec_batch_progress.await_count == 1


async def test_correct_sha256_still_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the correct sha256 supplied, verify passes and terminal_step is 'deleted'."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    correct_hash = hashlib.sha256(orig_paths[0].read_bytes()).hexdigest()
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
            sha256_hash=correct_hash,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "deleted"
    assert sent.failed_at_step is None


async def test_empty_scan_roots_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent mis-deployed with empty scan_roots -> RuntimeError BEFORE any file op."""
    _patch_settings(monkeypatch, [])
    api = _make_api_client_mock()
    job = _make_job_mock()
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path="/music/x.mp3",
            proposed_path="renamed",
            proposed_filename="y.mp3",
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="agent has no scan_roots configured"):
        await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    api.patch_execution_log.assert_not_called()
    api.post_exec_batch_progress.assert_not_called()
