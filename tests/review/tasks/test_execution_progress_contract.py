"""Progress payload and failure-attribution contracts for execution orchestration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import uuid

from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
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


async def test_success_emits_one_deleted_progress_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """28-V-06: single-proposal success -> 1 post_exec_batch_progress with terminal_step='deleted' + sub_batch_terminal=True."""
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

    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "deleted"
    assert sent.failed_at_step is None
    assert sent.sub_batch_terminal is True
    assert sent.proposal_id == proposals[0].proposal_id
    assert sent.agent_id == "agent-a"
    assert sent.batch_id == payload.batch_id


async def test_failure_emits_failed_progress_post_with_failed_at_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """28-V-07: path-traversal happens during current_step='copy' -> failed_at_step='copy'."""
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    _patch_settings(monkeypatch, [str(allowed_root)])
    api = _make_api_client_mock()
    job = _make_job_mock()
    orig = allowed_root / "ok.mp3"
    orig.write_bytes(b"x")
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig),
            # relative-dir traversal resolving OUTSIDE the scan_root -> path-traversal ValueError
            proposed_path="../../../../../../../../etc",
            proposed_filename="passwd",
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "failed"
    assert sent.failed_at_step == "copy"
    assert sent.sub_batch_terminal is True


async def test_sha256_mismatch_maps_to_failed_at_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sha256 mismatch raised while current_step='verify' -> failed_at_step='verify'."""
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
            sha256_hash="0" * 64,  # wrong hash forces sha256 mismatch
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "failed"
    assert sent.failed_at_step == "verify"


async def test_delete_failure_maps_to_failed_at_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unlink() raises after a successful copy -> failed_at_step='delete'.

    The 'delete' step only exists on the cross-filesystem fallback (a same-fs
    os.replace moves + deletes atomically, so there is no separate unlink to
    fail). Force the streamed-copy fallback so the unlink failure is reachable.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    monkeypatch.setattr("phaze.tasks.execution._same_filesystem", lambda _s, _d: False)

    # Monkeypatch Path.unlink to raise OSError ONLY when the orig file path is targeted.
    from pathlib import Path as _Path

    real_unlink = _Path.unlink
    target = orig_paths[0].resolve()

    def fail_unlink(self: _Path, *args: object, **kwargs: object) -> None:
        if self == target:
            msg = "simulated delete failure"
            raise OSError(msg)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "unlink", fail_unlink)

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

    assert api.post_exec_batch_progress.await_count == 1
    sent = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert sent.terminal_step == "failed"
    assert sent.failed_at_step == "delete"


async def test_sub_batch_terminal_set_on_last_item_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """28-V-08: 3 proposals -> 3 POSTs; only the last has sub_batch_terminal=True."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 3)
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

    assert api.post_exec_batch_progress.await_count == 3
    terminal_flags = [_payload_from_call(c).sub_batch_terminal for c in api.post_exec_batch_progress.await_args_list]
    assert terminal_flags == [False, False, True]
    # Every POST should also carry terminal_step="deleted" on the happy path.
    steps = [_payload_from_call(c).terminal_step for c in api.post_exec_batch_progress.await_args_list]
    assert steps == ["deleted", "deleted", "deleted"]


async def test_progress_post_failure_logs_warning_but_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-16: if a TELEMETRY progress POST fails after retries, swallow + log WARNING."""
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

    with caplog.at_level(logging.WARNING, logger="phaze.tasks.execution"):
        result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # File ops committed despite the progress POST failure.
    assert result["status"] == "completed"
    assert result["error_count"] == 0
    assert all(p.exists() for p in proposed_paths)
    assert not any(p.exists() for p in orig_paths)
    # WARNING was logged citing the progress POST.
    assert any("progress POST failed" in record.getMessage() for record in caplog.records)


async def test_error_message_uses_step_reason_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-01: failed PATCH execution-log error_message starts with '<step>: '."""
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
            sha256_hash="0" * 64,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # patch_execution_log was called with status=FAILED + error_message starting with 'verify: '
    failed_patches = [c for c in api.patch_execution_log.await_args_list if c.args[1].error_message is not None]
    assert len(failed_patches) == 1
    err = failed_patches[0].args[1].error_message
    assert err.startswith("verify: "), f"expected 'verify: ' prefix, got: {err!r}"
