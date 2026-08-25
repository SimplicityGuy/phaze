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

import pytest

from phaze.config import AgentSettings
from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.agent_client import AgentApiServerError
import phaze.tasks.execution as execmod
from phaze.tasks.execution import execute_approved_batch


if TYPE_CHECKING:
    from pathlib import Path


def _make_api_client_mock() -> AsyncMock:
    """Mock PhazeAgentClient with all 4 methods used by execute_approved_batch (Phase 28)."""
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    api.post_exec_batch_progress = AsyncMock(return_value=None)
    return api


def _make_job_mock(initial_meta: dict[str, str] | None = None) -> MagicMock:
    """Mock SAQ Job with a writeable ``meta`` dict and an async ``update`` method.

    phaze-ebb46: ``update`` now has a ``side_effect`` that actually applies ``meta=...`` onto
    ``job.meta``, mirroring real SAQ (``saq.queue.base.Queue.update`` does ``setattr(job, k, v)``
    for every kwarg before persisting). Without this the mock's ``job.meta`` stayed frozen at
    whatever ``initial_meta`` was, so code that reads ``job.meta`` again later in the SAME batch
    (as this bead's per-proposal "moved" flag does, right after `_load_or_seed_uuids` already
    wrote the UUID keys) would see a stale, incomplete dict instead of the real cumulative state.
    """
    job = MagicMock()
    job.meta = dict(initial_meta or {})

    async def _update(**kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(job, key, value)

    job.update = AsyncMock(side_effect=_update)
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


# ---------------------------------------------------------------------------
# D-16 — TELEMETRY progress POST failure logs WARNING and does not raise.
# phaze-j7u8 splits the rule: the sub_batch_terminal COMPLETION TOKEN re-raises instead
# (covered in the phaze-j7u8 section further down).
# ---------------------------------------------------------------------------


def _fail_only_telemetry_posts() -> AsyncMock:
    """A post_exec_batch_progress mock that fails ONLY the non-terminal (telemetry) POSTs.

    phaze-j7u8: a single-proposal batch makes every POST ``sub_batch_terminal=True``, so it can no
    longer exercise the D-16 swallow at all. Failing only the telemetry half keeps these tests
    asserting what they were written to assert.
    """

    async def _side_effect(_batch_id: uuid.UUID, body: object) -> None:
        if not getattr(body, "sub_batch_terminal", False):
            raise AgentApiServerError("progress endpoint down")

    return AsyncMock(side_effect=_side_effect)


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

    phaze-ebb46: the crash window this models is "the move committed AND this
    proposal's own ``moved:`` flag was durably persisted to job.meta, then the
    worker died before the completed/executed/progress PATCHes" -- so the flag is
    pre-seeded here alongside the UUIDs, exactly as a real first attempt would have
    left it. Without that corroboration the already-moved fast path is no longer
    conclusive (see the same-named test file's sibling
    ``test_crash_retry_already_moved_uncorroborated_fails_loudly_not_silently``),
    which is the whole point of this bead.
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
            f"moved:{proposal_id}": "1",
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
            source_path=str(original),
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

    phaze-ebb46: pre-seeds this proposal's own ``moved:`` corroboration flag,
    modeling a real first attempt that committed the move and persisted the flag
    before crashing -- see the docstring on
    ``test_crash_retry_already_moved_reports_completed_not_stale_failed``.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    original = orig_paths[0]
    proposed = proposed_paths[0]
    content_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    proposal_id = uuid.uuid4()
    job = _make_job_mock(initial_meta={f"moved:{proposal_id}": "1"})

    # Simulate the crash window (same as above): the move already committed.
    proposed.parent.mkdir(parents=True, exist_ok=True)
    original.replace(proposed)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            source_path=str(original),
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

    phaze-ebb46: pre-seeds the ``moved:`` corroboration flag so this exercises the
    hash-check verify branch specifically (a CORROBORATED replay that still fails on
    content mismatch), rather than being short-circuited earlier by the new
    uncorroborated-replay guard this bead adds -- see
    ``test_crash_retry_already_moved_uncorroborated_fails_loudly_not_silently`` for
    that separate, now-covered failure mode.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    original = orig_paths[0]
    proposed = proposed_paths[0]
    proposal_id = uuid.uuid4()
    job = _make_job_mock(initial_meta={f"moved:{proposal_id}": "1"})

    # Simulate the already-moved shape, but `proposed` does NOT match the
    # declared hash (as if an unrelated file occupies the destination).
    proposed.parent.mkdir(parents=True, exist_ok=True)
    original.replace(proposed)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            source_path=str(original),
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
# phaze-ebb46 — the already-moved heuristic must not trust content identity ALONE.
# A missing source with a byte-identical file already sitting at the destination
# (a DIFFERENT proposal's completed move, or simply no evidence at all) must fail
# loudly instead of being silently reported EXECUTED with a stolen current_path.
# ---------------------------------------------------------------------------


async def test_crash_retry_already_moved_uncorroborated_fails_loudly_not_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The already-moved SHAPE alone (original gone, proposed present, hash matches) is not
    conclusive replay evidence without per-proposal corroboration (phaze-ebb46).

    Unlike the phaze-ebpt regression tests above -- which pre-seed THIS proposal's own
    ``moved:`` flag to model the real "committed then crashed before the PATCHes" window --
    this test seeds neither the flag nor a cross-fs commit marker, modeling the case the
    old heuristic got wrong: content identity plus a missing source with zero evidence this
    proposal is the one that produced it. The fast path must refuse to trust it.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()  # no moved: flag seeded

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    original = orig_paths[0]
    proposed = proposed_paths[0]
    content_hash = hashlib.sha256(original.read_bytes()).hexdigest()

    # already-moved SHAPE: original gone, proposed present with MATCHING content -- but no
    # evidence that THIS proposal is the one that put it there.
    proposed.parent.mkdir(parents=True, exist_ok=True)
    original.replace(proposed)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
            sha256_hash=content_hash,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    # Uncorroborated -- must fail loudly, never silently "executed".
    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    state_patch = api.patch_proposal_state.await_args.args[1]
    assert state_patch.proposal_state == "failed"
    # The destination -- which this proposal never proved it authored -- is untouched.
    assert proposed.exists()


async def test_duplicate_missing_source_is_not_silently_executed_onto_another_records_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-ebb46 failure scenario, reproduced directly against the real code path.

    X and Y are byte-identical duplicates (same sha256; phaze's core use case). X's proposal
    already executed via the REAL ``_execute_one`` path -- so X's OWN job.meta correctly
    carries X's OWN ``moved:`` corroboration flag -- and landed X at a shared destination.
    Y's source then goes missing for an unrelated reason (operator deleted the redundant
    copy by hand, external tooling, a prior partial failure) before Y's proposal executes,
    on a DIFFERENT job that has never touched this destination.

    Pre-fix: ``original.exists()`` False, ``proposed.exists()`` True, and the hash matches
    (duplicates are identical) -- the heuristic "confirmed" already-moved on content identity
    alone and silently reported Y EXECUTED with ``current_path`` aliased onto X's file, the
    exact FileRecord corruption this bead closes.

    Post-fix: Y holds no corroboration of its own for this destination (only X's job ever
    wrote one, and only under X's proposal_id), so Y fails loudly instead.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    content = b"duplicate-audio-bytes" * 4096
    content_hash = hashlib.sha256(content).hexdigest()
    destination_name = "song.mp3"

    # X: a real first execution, landing X at the shared destination and persisting X's own
    # moved: flag under X's own job.
    original_x = tmp_path / "orig" / "x.mp3"
    original_x.parent.mkdir(parents=True, exist_ok=True)
    original_x.write_bytes(content)
    proposal_x = uuid.uuid4()
    job_x = _make_job_mock()
    proposals_x = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_x,
            file_id=uuid.uuid4(),
            source_path=str(original_x),
            proposed_path="new",
            proposed_filename=destination_name,
            sha256_hash=content_hash,
        ),
    ]
    payload_x = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals_x)
    result_x = await execute_approved_batch(
        {"api_client": _make_api_client_mock(), "job": job_x},
        **payload_x.model_dump(mode="json"),
    )
    destination = tmp_path / "new" / destination_name
    assert result_x["status"] == "completed"
    assert not original_x.exists()
    assert destination.read_bytes() == content
    # X's own job now carries X's flag -- Y's job (below) is a DIFFERENT job entirely and
    # never sees this.
    assert job_x.meta.get(f"moved:{proposal_x}") is not None

    # Y: a DIFFERENT proposal, DIFFERENT job, same content/hash (a real dedup pair). Y's
    # source has since gone missing for an unrelated reason -- simulated directly here, since
    # "the operator deleted it by hand" leaves no code path to reproduce.
    original_y = tmp_path / "orig" / "y.mp3"  # deliberately never created: Y's source is gone.
    proposal_y = uuid.uuid4()
    api_y = _make_api_client_mock()
    job_y = _make_job_mock()  # Y's own job has never touched this destination -- no flag, no marker.
    proposals_y = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_y,
            file_id=uuid.uuid4(),
            source_path=str(original_y),
            proposed_path="new",
            proposed_filename=destination_name,
            sha256_hash=content_hash,
        ),
    ]
    payload_y = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals_y)
    result_y = await execute_approved_batch({"api_client": api_y, "job": job_y}, **payload_y.model_dump(mode="json"))

    # Y must fail loudly -- NOT silently report executed with current_path stolen from X.
    assert result_y["status"] == "completed_with_errors"
    assert result_y["error_count"] == 1
    state_patch_y = api_y.patch_proposal_state.await_args.args[1]
    assert state_patch_y.proposal_state == "failed"
    # X's file at the shared destination is untouched by Y's failed attempt.
    assert destination.read_bytes() == content


# ---------------------------------------------------------------------------
# phaze-qx8z — cross-fs replay: a crash between the committed copy and the
# pending ``original.unlink()`` leaves BOTH `original` and a distinct-inode
# `proposed`. The replay must recognize the already-copied destination and
# complete the move forward (delete `original`, report executed), NOT misfire
# the phaze-yu2e clobber guard and flip the succeeded move to FAILED while
# leaving the file duplicated. A genuinely foreign file is still refused.
# ---------------------------------------------------------------------------


async def test_cross_fs_replay_committed_copy_completes_move_not_clobber_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for phaze-qx8z (no supplied hash).

    Simulates the OOM-kill window: a prior cross-fs attempt committed the copy
    (a byte-identical file sits at `proposed`) then died before
    ``original.unlink()``, so replay begins with BOTH files present on different
    filesystems. The recovery must delete `original` and report executed.

    phaze-i7jo: content identity alone is no longer sufficient corroboration (an
    archive full of exact duplicates means a byte-identical `proposed` could be a
    DIFFERENT proposal's completed move) -- this proposal's own commit marker
    (`_committed_copy_marker_path`) must ALSO be present, exactly as the real
    ``_atomic_cross_fs_copy`` call site writes it right after landing the copy.
    Seeding it here is what distinguishes "my own prior attempt" from the
    phaze-i7jo bug this test predates.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()

    original = tmp_path / "orig" / "concert.mkv"
    original.parent.mkdir(parents=True, exist_ok=True)
    content = b"concert-bytes" * 4096
    original.write_bytes(content)

    proposed = tmp_path / "new" / "concert.mkv"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    proposed.write_bytes(content)  # the prior attempt's committed, identical copy

    proposal_id = uuid.uuid4()
    execmod._committed_copy_marker_path(proposed, proposal_id).write_text(str(proposal_id))

    # Force the cross-filesystem branch: st_dev compare would say same-fs under one
    # tmp tree, but the crash residue only occurs across a mount boundary.
    monkeypatch.setattr("phaze.tasks.execution._same_filesystem", lambda _s, _d: False)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            source_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    state_patch = api.patch_proposal_state.await_args.args[1]
    assert state_patch.proposal_state == "executed"
    assert state_patch.file_state == "moved"
    assert state_patch.current_path == str(proposed)
    progress_post = _payload_from_call(api.post_exec_batch_progress.await_args)
    assert progress_post.terminal_step == "deleted"
    # The move is completed forward: original deleted, identical copy preserved.
    assert not original.exists()
    assert proposed.read_bytes() == content
    # phaze-i7jo: the corroborating marker is cleaned up once the move completes.
    assert not execmod._committed_copy_marker_path(proposed, proposal_id).exists()


async def test_cross_fs_replay_committed_copy_with_hash_completes_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-qx8z with a supplied sha256_hash: recovery verifies `proposed` against it.

    phaze-i7jo: also seeds this proposal's own commit marker -- see the docstring on
    ``test_cross_fs_replay_committed_copy_completes_move_not_clobber_fail`` above.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()

    original = tmp_path / "orig" / "set.mp3"
    original.parent.mkdir(parents=True, exist_ok=True)
    content = b"audio" * 8192
    original.write_bytes(content)
    content_hash = hashlib.sha256(content).hexdigest()

    proposed = tmp_path / "new" / "set.mp3"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    proposed.write_bytes(content)

    proposal_id = uuid.uuid4()
    execmod._committed_copy_marker_path(proposed, proposal_id).write_text(str(proposal_id))

    monkeypatch.setattr("phaze.tasks.execution._same_filesystem", lambda _s, _d: False)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            source_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
            sha256_hash=content_hash,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "executed"
    assert not original.exists()
    assert not execmod._committed_copy_marker_path(proposed, proposal_id).exists()


# ---------------------------------------------------------------------------
# phaze-v3b1e — a crash between the completed-forward `original.unlink()` and
# its OWN marker cleanup leaves the marker orphaned: the NEXT replay sees
# `original` gone + `proposed` present, takes the `already_moved` fast path
# entirely (corroborated by the still-present marker), and never reaches either
# of the two call sites that would otherwise unlink it -- both require
# `original` to still exist. Unlike the two tests above (which model the FIRST
# crash edge -- before `original.unlink()` -- and assert the marker is cleaned
# up when the move completes forward), this models the SECOND edge: the move
# already completed on a PRIOR call, and this call is the already-moved replay.
# ---------------------------------------------------------------------------


async def test_already_moved_replay_cleans_up_orphaned_commit_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for phaze-v3b1e.

    Simulates a worker crash strictly between the completed-forward
    ``original.unlink()`` and the marker's own ``unlink(missing_ok=True)`` (or,
    equivalently, between the fresh cross-fs move's ``original.unlink()`` and its
    marker cleanup): `original` is already gone, `proposed` already holds the
    final file, and the per-proposal commit marker is still sitting on disk. SAQ
    replays the job; `_execute_one` correctly detects `already_moved` (using the
    orphaned marker as its OWN corroboration, exactly as a genuine replay would),
    but pre-fix it never touched the marker on this path -- it survived forever,
    polluting the archive with `<dest>.phaze-committed.<uuid>`.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()

    proposed = tmp_path / "new" / "concert.mkv"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    content = b"concert-bytes" * 4096
    proposed.write_bytes(content)  # the move already fully completed

    original = tmp_path / "orig" / "concert.mkv"  # gone -- the crash happened after unlink()

    proposal_id = uuid.uuid4()
    marker = execmod._committed_copy_marker_path(proposed, proposal_id)
    marker.write_text(str(proposal_id))  # orphaned: never cleaned up by the crashed attempt
    assert marker.exists()

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            source_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    state_patch = api.patch_proposal_state.await_args.args[1]
    assert state_patch.proposal_state == "executed"
    assert state_patch.current_path == str(proposed)
    # The whole point: the orphaned marker is cleaned up by the already-moved replay,
    # not left behind forever.
    assert not marker.exists()
    assert proposed.read_bytes() == content


async def test_already_moved_replay_via_moved_flag_cleans_up_absent_marker_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The already-moved cleanup must not raise when there is no marker to clean up.

    A same-fs move is corroborated via the ``moved:`` job-meta flag (phaze-ebb46), not a
    cross-fs commit marker -- there is nothing on disk to unlink. `unlink(missing_ok=True)`
    must make this a no-op, not an error.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    original = orig_paths[0]
    proposed = proposed_paths[0]
    proposal_id = uuid.uuid4()
    job = _make_job_mock(initial_meta={f"moved:{proposal_id}": "1"})

    proposed.parent.mkdir(parents=True, exist_ok=True)
    original.replace(proposed)  # the prior same-fs attempt already committed

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=proposal_id,
            file_id=uuid.uuid4(),
            source_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    assert not execmod._committed_copy_marker_path(proposed, proposal_id).exists()


async def test_cross_fs_foreign_file_at_destination_still_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-qx8z guard: a NON-identical file at `proposed` (both present, cross-fs) is a genuine collision.

    The recovery must not blindly delete `original` when `proposed` holds an
    UNRELATED file -- that would destroy the source in favor of a foreign
    destination, the very thing phaze-yu2e prevents. It must still raise
    FileExistsError and leave BOTH files intact.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    job = _make_job_mock()

    original = tmp_path / "orig" / "set.mp3"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"THE-REAL-SOURCE")

    proposed = tmp_path / "new" / "set.mp3"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    proposed.write_bytes(b"AN-UNRELATED-FILE")  # foreign, NOT a copy of original

    monkeypatch.setattr("phaze.tasks.execution._same_filesystem", lambda _s, _d: False)

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(original),
            proposed_path="new",
            proposed_filename=proposed.name,
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api, "job": job}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"
    assert api.patch_execution_log.await_args.args[1].error_message.startswith("copy:")
    # Neither file destroyed.
    assert original.read_bytes() == b"THE-REAL-SOURCE"
    assert proposed.read_bytes() == b"AN-UNRELATED-FILE"


# ---------------------------------------------------------------------------
# phaze-i7jo — a byte-identical DUPLICATE's own already-completed move must not be
# mistaken for THIS proposal's residue. Reproduces the reported bug directly: two
# distinct proposals (A and B) share a sha256 hash (a real dedup group) and the same
# resolved destination; A's move already completed (no marker of B's own left behind
# for that destination) when B's proposal executes. Content identity alone used to be
# "proof enough" that `proposed` was B's own prior attempt -- it is not.
# ---------------------------------------------------------------------------


async def test_cross_fs_duplicates_own_already_completed_move_is_refused_not_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-i7jo: duplicate B must NOT delete its own original just because duplicate A already
    moved a byte-identical copy to the same destination.

    Before the fix: ``_destination_is_committed_copy`` sees `proposed` (A's completed move) is
    byte-identical to B's `original`, treats it as B's own resumable residue, deletes B's
    original, and reports B executed with `current_path` aliased onto A's file -- two
    ``FileRecord``s now share one on-disk file with no dedup bookkeeping.

    After the fix: B has no commit marker of its own for this destination (only A ever wrote
    one, and A's own marker was cleaned up on A's successful completion), so the corroboration
    check fails and B's proposal is refused via the phaze-yu2e ``FileExistsError`` -- loud,
    recoverable, and B's original survives on disk.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    monkeypatch.setattr("phaze.tasks.execution._same_filesystem", lambda _s, _d: False)

    content = b"duplicate-audio-bytes" * 4096
    content_hash = hashlib.sha256(content).hexdigest()

    # Duplicate A: a distinct source file, byte-identical to B, that already completed its
    # move to the shared destination via the REAL code path (so its marker was written and
    # then cleaned up exactly as production does).
    original_a = tmp_path / "orig" / "a.mp3"
    original_a.parent.mkdir(parents=True, exist_ok=True)
    original_a.write_bytes(content)
    proposed = tmp_path / "new" / "song.mp3"

    proposals_a = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(original_a),
            proposed_path="new",
            proposed_filename=proposed.name,
            sha256_hash=content_hash,
        ),
    ]
    payload_a = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals_a)
    result_a = await execute_approved_batch({"api_client": _make_api_client_mock(), "job": _make_job_mock()}, **payload_a.model_dump(mode="json"))
    assert result_a["status"] == "completed"
    assert not original_a.exists()
    assert proposed.read_bytes() == content

    # Duplicate B: a DIFFERENT source file with the SAME content/hash (a real dedup pair),
    # whose proposal independently resolves to the SAME destination -- already occupied by
    # A's completed move. B never attempted this destination before, so it holds no marker.
    original_b = tmp_path / "orig" / "b.mp3"
    original_b.write_bytes(content)

    api_b = _make_api_client_mock()
    proposals_b = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(original_b),
            proposed_path="new",
            proposed_filename=proposed.name,
            sha256_hash=content_hash,
        ),
    ]
    payload_b = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=proposals_b)
    result_b = await execute_approved_batch({"api_client": api_b, "job": _make_job_mock()}, **payload_b.model_dump(mode="json"))

    assert result_b["status"] == "completed_with_errors"
    assert result_b["error_count"] == 1
    assert api_b.patch_proposal_state.await_args.args[1].proposal_state == "failed"
    assert api_b.patch_execution_log.await_args.args[1].error_message.startswith("copy:")
    # B's original survives -- NOT deleted in favor of A's already-moved copy.
    assert original_b.exists()
    assert original_b.read_bytes() == content
    # A's file at the shared destination is untouched.
    assert proposed.read_bytes() == content


# ---------------------------------------------------------------------------
# phaze-q2lg — a live ``original.unlink()`` failure AFTER a committed cross-fs
# copy must leave a coherent, recoverable state: the copy at `proposed` is
# complete (not a partial), the failure is reported distinctly at
# failed_at_step="delete" (moved-but-source-not-removed), and a subsequent retry
# completes the move WITHOUT re-copying the whole (multi-GB) file.
# ---------------------------------------------------------------------------


async def test_cross_fs_unlink_failure_leaves_complete_copy_and_retry_does_not_recopy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for phaze-q2lg.

    Attempt 1: cross-fs copy commits, then ``original.unlink()`` raises (e.g. a
    read-only source mount). The proposal is FAILED at step 'delete', but the
    file at `proposed` is a COMPLETE copy (atomic temp+replace, phaze-k23z) and
    the source is untouched -- no partial, no lost data.

    Attempt 2 (retry, same paths): the executor recognizes the already-committed
    identical copy (phaze-qx8z recovery) and completes the move by deleting the
    original, WITHOUT re-streaming the whole file.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    monkeypatch.setattr("phaze.tasks.execution._same_filesystem", lambda _s, _d: False)

    original = tmp_path / "orig" / "concert.mkv"
    original.parent.mkdir(parents=True, exist_ok=True)
    content = b"concert-video-bytes" * 8192
    original.write_bytes(content)
    proposed = tmp_path / "new" / "concert.mkv"

    proposal_id = uuid.uuid4()

    def _make_proposals() -> list[ExecuteBatchProposalItem]:
        return [
            ExecuteBatchProposalItem(
                proposal_id=proposal_id,
                file_id=uuid.uuid4(),
                source_path=str(original),
                proposed_path="new",
                proposed_filename="concert.mkv",
            ),
        ]

    # --- Attempt 1: force the post-copy unlink to fail (read-only source mount).
    from pathlib import Path as _Path

    real_unlink = _Path.unlink

    def _failing_unlink(self: _Path, *, missing_ok: bool = False) -> None:
        if self == original:
            raise OSError(30, "Read-only file system")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(_Path, "unlink", _failing_unlink)

    api1 = _make_api_client_mock()
    payload1 = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=_make_proposals())
    result1 = await execute_approved_batch({"api_client": api1, "job": _make_job_mock()}, **payload1.model_dump(mode="json"))

    assert result1["status"] == "completed_with_errors"
    assert result1["error_count"] == 1
    # Reported distinctly as a delete-step failure (moved-but-source-not-removed),
    # NOT a copy failure.
    assert api1.patch_proposal_state.await_args.args[1].proposal_state == "failed"
    assert api1.patch_execution_log.await_args.args[1].error_message.startswith("delete:")
    progress1 = _payload_from_call(api1.post_exec_batch_progress.await_args)
    assert progress1.failed_at_step == "delete"
    # The copy at `proposed` is COMPLETE (byte-identical), and the source survives.
    assert proposed.read_bytes() == content
    assert original.read_bytes() == content

    # --- Attempt 2: unlink works again; the retry must NOT re-copy the file.
    monkeypatch.setattr(_Path, "unlink", real_unlink)
    recopy_calls = {"n": 0}
    real_atomic = execmod._atomic_cross_fs_copy

    def _spy_atomic(src: _Path, dst: _Path) -> None:
        recopy_calls["n"] += 1
        real_atomic(src, dst)

    monkeypatch.setattr(execmod, "_atomic_cross_fs_copy", _spy_atomic)

    api2 = _make_api_client_mock()
    payload2 = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="agent-a", proposals=_make_proposals())
    result2 = await execute_approved_batch({"api_client": api2, "job": _make_job_mock()}, **payload2.model_dump(mode="json"))

    assert result2["status"] == "completed"
    assert result2["error_count"] == 0
    assert recopy_calls["n"] == 0  # no whole-file re-copy on retry
    assert api2.patch_proposal_state.await_args.args[1].proposal_state == "executed"
    assert api2.patch_proposal_state.await_args.args[1].current_path == str(proposed)
    # Move completed forward: original gone, complete copy preserved.
    assert not original.exists()
    assert proposed.read_bytes() == content


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


# ---------------------------------------------------------------------------
# phaze-j7u8 — the sub_batch_terminal COMPLETION TOKEN is NOT telemetry. Losing it
# strands the batch at 'running' and holds exec:active for 24h, so it must re-raise
# and let SAQ replay the job rather than be swallowed under the D-16 rule.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# phaze-87ba — a failed write-ahead ExecutionLog POST must not erase the audit
# record of a move that actually happened. CREATE is idempotent (D-13) but PATCH
# is a monotonic-ladder UPDATE (D-15) that 404s on a missing row -- and a 404 is
# a 4xx, so it is never retried and is swallowed.
# ---------------------------------------------------------------------------


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
