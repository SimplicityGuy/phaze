"""Tests for agent-side execute_approved_batch progress POSTs (Phase 28 D-03, D-15, D-16, L6/L22).

Covers:

* One ``api.post_exec_batch_progress`` per proposal at terminal state (D-03).
* Success path: ``terminal_step="deleted"`` with ``failed_at_step=None``.
* Failure paths: ``terminal_step="failed"`` with ``failed_at_step`` derived from
  the tracked ``current_step`` variable + ``_classify_failure_step`` helper:
  - path-traversal -> ``"copy"`` (path-resolve happens during current_step="copy").
  - sha256 mismatch -> ``"verify"`` (current_step="verify" before the hash check).
  - delete failure -> ``"delete"`` (current_step="delete" set before ``original.unlink()``).
* ``sub_batch_terminal=True`` only on the LAST item of the sub-batch (D-07).
* Progress POST failures after tenacity retries log WARNING and do NOT raise (D-16).
* Both ``execution_log_id`` AND ``progress_request_id`` UUIDs are persisted in
  ``ctx['job'].meta`` via ``await ctx['job'].update(meta=...)`` and re-used on
  SAQ retry (closes L6/L22, delivers D-15).
* Failed ``ExecutionLog.error_message`` uses the ``"<step>: <reason>"`` prefix
  convention (D-01 contract).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

from phaze.config import AgentSettings
from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.agent_client import AgentApiServerError
from phaze.tasks.execution import execute_approved_batch


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_api_client_mock() -> AsyncMock:
    """Mock PhazeAgentClient with all 4 methods used by execute_approved_batch (Phase 28)."""
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    api.post_exec_batch_progress = AsyncMock(return_value=None)
    return api


def _make_job_mock(initial_meta: dict[str, str] | None = None) -> MagicMock:
    """Mock SAQ Job with a writeable ``meta`` dict and an async ``update`` method."""
    job = MagicMock()
    job.meta = dict(initial_meta or {})
    job.update = AsyncMock(return_value=None)
    return job


def _seed_files(tmp_path: Path, count: int) -> tuple[list[Path], list[Path]]:
    """Create ``count`` orig files under ``tmp_path/orig`` and target paths under ``tmp_path/new``."""
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
    """Stub ``get_settings()`` to return an AgentSettings-shaped mock with given scan_roots."""
    fake_cfg = MagicMock(spec=AgentSettings)
    fake_cfg.scan_roots = scan_roots
    monkeypatch.setattr("phaze.tasks.execution.get_settings", lambda: fake_cfg)


def _payload_from_call(call: object) -> object:
    """Extract the ``ExecBatchProgressPayload`` second positional or kwarg from a mock call."""
    args = getattr(call, "args", ()) or ()
    kwargs = getattr(call, "kwargs", {}) or {}
    if len(args) >= 2:
        return args[1]
    if "payload" in kwargs:
        return kwargs["payload"]
    msg = f"could not extract ExecBatchProgressPayload from call {call!r}"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# 28-V-06 — success path: ONE progress POST with terminal_step="deleted"
# ---------------------------------------------------------------------------


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
            original_path=str(orig_paths[0]),
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


# ---------------------------------------------------------------------------
# 28-V-07 — failure path: terminal_step="failed" + failed_at_step derived from current_step
# ---------------------------------------------------------------------------


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
            original_path=str(orig),
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
            original_path=str(orig_paths[0]),
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
            original_path=str(orig_paths[0]),
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


# ---------------------------------------------------------------------------
# 28-V-08 — sub_batch_terminal True only on the LAST item
# ---------------------------------------------------------------------------


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
            original_path=str(o),
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


# ---------------------------------------------------------------------------
# D-16 — progress POST failure logs WARNING and does not raise
# ---------------------------------------------------------------------------


async def test_progress_post_failure_logs_warning_but_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-16: if the progress POST fails after retries, swallow + log WARNING."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = AsyncMock(side_effect=AgentApiServerError("progress endpoint down"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.WARNING, logger="phaze.tasks.execution"):
        result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # File op committed despite the progress POST failure.
    assert result["status"] == "completed"
    assert result["error_count"] == 0
    assert proposed_paths[0].exists()
    assert not orig_paths[0].exists()
    # WARNING was logged citing the progress POST.
    assert any("progress POST failed" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# L6/L22 + D-15 — SAQ-meta-backed UUIDs (execution_log_id + progress_request_id)
# ---------------------------------------------------------------------------


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
            original_path=str(o),
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
    """Pre-seeded job.meta -> UUIDs re-used; job.update NOT called; POST'd UUIDs match."""
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
            original_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # Both keys were already present -> NO update call (closes L6/L22).
    job.update.assert_not_awaited()

    # ExecutionLog POST re-used the preseeded log_id.
    assert api.post_execution_log.await_count == 1
    post_payload = api.post_execution_log.await_args.args[0]
    assert post_payload.id == preseeded_log_id

    # post_exec_batch_progress re-used the preseeded request_id.
    assert api.post_exec_batch_progress.await_count == 1
    progress_payload = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert progress_payload.request_id == preseeded_req_id


# ---------------------------------------------------------------------------
# phaze-ebpt — already-moved replay detection: a SAQ retry after a crash between
# the committed file move and the success PATCHes must report COMPLETED (with a
# current_path pointing at `proposed`), NOT flip an already-executed proposal to
# FAILED with a stale current_path pointing at the now-deleted `original`.
# ---------------------------------------------------------------------------


async def test_crash_retry_already_moved_reports_completed_not_stale_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for phaze-ebpt.

    Simulates the exact crash window the bug report describes: the file op
    (``original.replace(proposed)``) already committed on a first attempt, the
    worker died before the completed/executed/progress PATCHes ran, and SAQ's
    sweep now re-dispatches the SAME job -- reusing the SAME
    ``execution_log_id``/``progress_request_id`` from ``job.meta`` (D-15),
    exactly as a genuine retry would.

    Pre-fix: ``_resolve_and_check_containment``'s non-strict resolve lets the
    missing ``original`` resolve without error, the move/verify code below then
    discovers ``proposed`` already occupied (``_is_same_file`` can't confirm a
    match because ``original.stat()`` raises OSError) and raises
    ``FileExistsError("destination already exists, refusing to overwrite")`` --
    caught by the generic handler, which PATCHes the execution log FAILED, flips
    the still-APPROVED proposal to FAILED, and reports ``current_path=None``
    (leaving ``FileRecord.current_path`` pointing at the deleted ``original``).

    Post-fix: ``_execute_one`` detects ``not original.exists() and
    proposed.exists()`` up front, skips the file op entirely, and falls through
    to the SAME success-reporting path a first-time success takes -- the
    proposal ends ``executed``/COMPLETED with ``current_path == str(proposed)``.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    original = orig_paths[0]
    proposed = proposed_paths[0]

    proposal_id = uuid.uuid4()
    preseeded_log_id = uuid.uuid4()
    preseeded_req_id = uuid.uuid4()
    job = _make_job_mock(
        initial_meta={
            f"log_id:{proposal_id}": str(preseeded_log_id),
            f"req_id:{proposal_id}": str(preseeded_req_id),
        },
    )

    # Simulate the crash window: the FIRST attempt already committed the move
    # (original.replace(proposed)) on disk before it crashed, so replay begins
    # with `original` gone and `proposed` present -- exactly what os.replace
    # leaves behind, and exactly what the code under test must detect.
    proposed.parent.mkdir(parents=True, exist_ok=True)
    original.replace(proposed)
    assert not original.exists()
    assert proposed.exists()

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            original_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # The batch as a whole must report success, not a failure.
    assert result["status"] == "completed"
    assert result["error_count"] == 0

    # The retry-stable execution_log_id/progress_request_id were re-used (no
    # fresh UUIDs seeded -- this really is the "same job" SAQ retry shape).
    job.update.assert_not_awaited()
    log_post = api.post_execution_log.await_args.args[0]
    assert log_post.id == preseeded_log_id

    # ExecutionLog PATCH must be COMPLETED, never FAILED.
    log_patch = api.patch_execution_log.await_args.args[1]
    assert log_patch.status == ExecutionStatus.COMPLETED

    # Proposal-state PATCH must report 'executed' with current_path pointing at
    # `proposed` -- NOT 'failed' with the stale (deleted) `original` path.
    state_patch = api.patch_proposal_state.await_args.args[1]
    assert state_patch.proposal_state == "executed"
    assert state_patch.file_state == "moved"
    assert state_patch.current_path == str(proposed)

    # Progress POST must report the success terminal_step, reusing the
    # preseeded (not-yet-consumed, since the first attempt crashed before
    # posting it) request_id.
    progress_post = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert progress_post.terminal_step == "deleted"
    assert progress_post.request_id == preseeded_req_id

    # The file itself is untouched by the replay: still exactly at `proposed`.
    assert proposed.exists()
    assert not original.exists()


async def test_crash_retry_already_moved_with_hash_verifies_against_proposed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-moved replay with a supplied sha256_hash verifies against `proposed`, not the gone `original`.

    Pre-fix, a hash-carrying retry of an already-moved proposal would hit
    ``_sha256_of_file(original)`` and raise FileNotFoundError (a distinct crash
    signature from the no-hash case, but still misreports the proposal FAILED).
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    original = orig_paths[0]
    proposed = proposed_paths[0]
    content_hash = hashlib.sha256(original.read_bytes()).hexdigest()

    # Simulate the crash window (same as above): the move already committed.
    proposed.parent.mkdir(parents=True, exist_ok=True)
    original.replace(proposed)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
            sha256_hash=content_hash,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    state_patch = api.patch_proposal_state.await_args.args[1]
    assert state_patch.proposal_state == "executed"
    assert state_patch.current_path == str(proposed)


async def test_crash_retry_hash_mismatch_at_proposed_is_still_a_genuine_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """already-moved shape (original gone, proposed present) + WRONG hash -> still fails.

    Guards against the fix over-trusting the already-moved heuristic: if the
    file sitting at `proposed` does not match the declared sha256, that is not
    the proposal's own replayed move (e.g. an unrelated file landed at the
    destination) and must be reported as a genuine verify failure, not silently
    swallowed into a false 'completed'.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    original = orig_paths[0]
    proposed = proposed_paths[0]

    # Simulate the already-moved shape, but `proposed` does NOT match the
    # declared hash (as if an unrelated file occupies the destination).
    proposed.parent.mkdir(parents=True, exist_ok=True)
    original.replace(proposed)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
            sha256_hash="0" * 64,  # deliberately wrong
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    state_patch = api.patch_proposal_state.await_args.args[1]
    assert state_patch.proposal_state == "failed"
    progress_post = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert progress_post.failed_at_step == "verify"


# ---------------------------------------------------------------------------
# D-01 — error_message uses the "<step>: <reason>" prefix
# ---------------------------------------------------------------------------


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
            original_path=str(orig_paths[0]),
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


# ---------------------------------------------------------------------------
# Sanity: progress request_id used on a single proposal matches what the POST sent
# (covers the "ExecutionLog POST and progress POST use SEPARATE UUIDs" invariant).
# ---------------------------------------------------------------------------


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
            original_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    log_post = api.post_execution_log.await_args.args[0]
    progress_post = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert log_post.id != progress_post.request_id


# ---------------------------------------------------------------------------
# Sanity: legacy ctx (no 'job' key) still works -- backward-compat with Phase 26 tests.
# This guarantees the regression test surface (test_execute_approved_batch.py) keeps
# passing even though it predates the SAQ-meta lift.
# ---------------------------------------------------------------------------


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
            original_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    # Progress POST still fires (uses freshly-generated request_id).
    assert api.post_exec_batch_progress.await_count == 1


# ---------------------------------------------------------------------------
# Sanity check: the helper file actually rebuilt the file successfully.
# ---------------------------------------------------------------------------


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
            original_path=str(orig_paths[0]),
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


# ---------------------------------------------------------------------------
# Failure-resilience coverage (Phase 28 patch-coverage fill)
#
# These tests assert the WARN-and-continue contract of each best-effort
# audit/PATCH/progress call inside ``_execute_one`` and the outer batch
# scan_roots precondition. They round out coverage of the lines that
# Codecov flagged as missing in PR #62.
# ---------------------------------------------------------------------------


async def test_empty_scan_roots_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent mis-deployed with empty scan_roots -> RuntimeError BEFORE any file op."""
    _patch_settings(monkeypatch, [])
    api = _make_api_client_mock()
    job = _make_job_mock()
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path="/music/x.mp3",
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
            original_path=str(orig_paths[0]),
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
            original_path=str(orig_paths[0]),
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
            original_path=str(orig_paths[0]),
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
            original_path=str(orig_paths[0]),
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
    """post_exec_batch_progress raising on the SUCCESS path -> WARNING logged, batch still completes."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = AsyncMock(side_effect=AgentApiServerError("upstream 503"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)

    with caplog.at_level(logging.WARNING):
        result = await execute_approved_batch(
            {"api_client": api, "job": job},
            **payload.model_dump(mode="json"),
        )

    assert any("progress POST failed" in r.message for r in caplog.records)
    assert proposed_paths[0].exists()
    assert not orig_paths[0].exists()
    assert result["status"] == "completed"


async def test_progress_post_failure_on_failure_path_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """post_exec_batch_progress raising on the FAILED path -> WARNING logged, batch still completes."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_exec_batch_progress = AsyncMock(side_effect=AgentApiServerError("upstream 503"))
    job = _make_job_mock()
    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=str(orig_paths[0]),
            proposed_path="new",
            proposed_filename=proposed_paths[0].name,
            sha256_hash="0" * 64,
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


# ---------------------------------------------------------------------------
# bead phaze-uciu.6 — the success-path 'report' PATCH is guarded so a 5xx after
# a committed move cannot flip the proposal to FAILED / misreport failed_at_step.
# ---------------------------------------------------------------------------


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
            original_path=str(orig_paths[0]),
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
