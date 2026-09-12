"""Crash, replay, and collision-recovery contracts for execution."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
import uuid

from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
import phaze.tasks.execution as execmod
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
