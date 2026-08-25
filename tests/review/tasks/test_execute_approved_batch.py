"""Contract tests for phaze.tasks.execution.execute_approved_batch (Phase 26 B2 Option A).

Four scenarios:
* Happy path: 3 proposals all succeed.
* Partial failure: middle proposal hits IO error, siblings succeed.
* Path traversal: proposed_path escapes scan_root.
* sha256 mismatch: declared hash differs from file contents.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

import pydantic
import pytest

from phaze.config import AgentSettings
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.execution_dispatch import get_approved_proposals_grouped_by_agent
from phaze.tasks.execution import execute_approved_batch


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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


def _item(orig: Path, dest: Path, root: Path, **kwargs: object) -> ExecuteBatchProposalItem:
    """Build an item whose proposed_path is the dest DIRECTORY relative to `root`.

    Mirrors the real dispatcher shape: proposed_path is a relative directory
    under the owning scan_root and proposed_filename is the target filename;
    the executor rebuilds the absolute destination as root/proposed_path/name.
    """
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        source_path=str(orig),
        proposed_path=str(dest.parent.relative_to(root)),
        proposed_filename=dest.name,
        **kwargs,  # type: ignore[arg-type]
    )


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
    proposals = [_item(o, p, tmp_path) for o, p in zip(orig_paths, proposed_paths, strict=True)]
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
    proposals = [_item(o, p, tmp_path) for o, p in zip(orig_paths, proposed_paths, strict=True)]
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
            source_path=str(orig),
            # relative-dir traversal that resolves OUTSIDE the scan_root -- T-26-11-S1
            proposed_path="../../../../../../../../etc",
            proposed_filename="passwd",
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
        _item(orig_paths[0], proposed_paths[0], tmp_path, sha256_hash=correct_hash),
        _item(orig_paths[1], proposed_paths[1], tmp_path, sha256_hash=wrong_hash),
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


async def test_execute_approved_batch_source_path_escape_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """source_path escapes scan_root -> proposal fails, no file op attempted (GAP-4 / T-26-11-S1-mirror).

    Mirrors test_execute_approved_batch_path_escape_rejected but flips which field carries the escape:
    here source_path="/etc/shadow" (outside scan_root) while proposed_path is valid.
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
            source_path="/etc/shadow",  # outside scan_root -- GAP-4 escape via the move source
            proposed_path="",  # in-place; irrelevant -- source_path is rejected first
            proposed_filename="dest.mp3",
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["error_count"] == 1, f"Expected error_count=1, got {result['error_count']}"
    assert result["status"] == "completed_with_errors"
    # Proposed destination must not have been created (no file op attempted)
    assert not proposed.exists(), "proposed destination was created despite source_path escape rejection"
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
            source_path=str(o),
            proposed_path="moved",
            proposed_filename="y.mp3",
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    with pytest.raises(RuntimeError, match="scan_roots"):
        await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))
    api.patch_proposal_state.assert_not_awaited()


async def test_execute_approved_batch_tolerates_post_execution_log_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: POST execution-log failure does NOT abort the file op (lines 105-108)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.post_execution_log = AsyncMock(side_effect=RuntimeError("audit log down"))

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [_item(orig_paths[0], proposed_paths[0], tmp_path)]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    # File op still ran and proposal still marked executed
    assert result["status"] == "completed"
    assert proposed_paths[0].exists()
    assert not orig_paths[0].exists()
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "executed"


async def test_execute_approved_batch_tolerates_patch_completed_log_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: PATCH completed log failure does NOT prevent SUCCESS report (lines 140-141)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.patch_execution_log = AsyncMock(side_effect=RuntimeError("patch died"))

    orig_paths, proposed_paths = _seed_files(tmp_path, 1)
    proposals = [_item(orig_paths[0], proposed_paths[0], tmp_path)]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    # patch_proposal_state still called with executed state
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "executed"


async def test_execute_approved_batch_tolerates_patch_failed_log_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort: PATCH failed log failure does NOT prevent FAILURE report (lines 173-174)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    # First PATCH (sets in_progress is via POST, second PATCH after file-op-failure goes to status=failed)
    # Make patch_execution_log raise on EVERY call so the "failed log" branch raises.
    api.patch_execution_log = AsyncMock(side_effect=RuntimeError("patch died"))

    # Force file-op failure via missing source.
    missing = tmp_path / "missing.mp3"
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(missing),
            proposed_path="new",
            proposed_filename="missing.mp3",
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["error_count"] == 1
    # Failure still reported via patch_proposal_state(failed)
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"


async def test_execute_approved_batch_tolerates_failure_report_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Last-line defense: even if patch_proposal_state(failed) raises, the batch returns cleanly (lines 189-192)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    api.patch_proposal_state = AsyncMock(side_effect=RuntimeError("state-machine API down"))

    # Force file-op failure -- this exercises the failed-PATCH-of-failure path.
    missing = tmp_path / "missing.mp3"
    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(missing),
            proposed_path="new",
            proposed_filename="missing.mp3",
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)

    # Should NOT raise -- the inner try/except wraps the failure report.
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["processed_count"] == 1
    assert result["error_count"] == 1
    # Handler reached patch_proposal_state (the side_effect fired) before swallowing.
    api.patch_proposal_state.assert_awaited_once()


# ---------------------------------------------------------------------------
# End-to-end regression: dispatcher -> executor with the REAL stored shape.
#
# The pre-fix bug survived because dispatcher tests and executor tests each used
# a shape the OTHER half never produces: the dispatcher stores proposed_path as a
# RELATIVE destination directory (+ a separate proposed_filename), but the
# executor treated proposed_path as an ABSOLUTE destination FILE. These tests
# wire the two halves together so the shapes must agree.
# ---------------------------------------------------------------------------


async def test_e2e_dispatcher_to_executor_relative_dir_moves_file(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL stored proposal (relative proposed_path + proposed_filename) executes end-to-end.

    Seeds Agent/FileRecord/RenameProposal exactly as production does (proposed_path
    is a relative directory), builds the wire payload via the dispatcher helper
    ``get_approved_proposals_grouped_by_agent``, then runs the executor and asserts
    the file landed at ``scan_root/proposed_path/proposed_filename``.
    """
    scan_root = tmp_path / "media"
    orig = scan_root / "incoming" / "raw-set.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"concert-audio-bytes"
    orig.write_bytes(content)
    real_sha = hashlib.sha256(content).hexdigest()

    agent_id = "agent-e2e"
    session.add(Agent(id=agent_id, name=agent_id, token_hash=None, scan_roots=[str(scan_root)], revoked_at=None))
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            sha256_hash=real_sha,
            original_path=str(orig),
            original_filename="raw-set.mp3",
            current_path=str(orig),
            file_type="music",
            file_size=len(content),
            agent_id=agent_id,
        ),
    )
    await session.flush()
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename="Disclosure - Live at Coachella.mp3",
            proposed_path="performances/artists/Disclosure",  # RELATIVE dir, as stored
            status=ProposalStatus.APPROVED,
            confidence=0.95,
        ),
    )
    await session.commit()

    groups = await get_approved_proposals_grouped_by_agent(session)
    items = groups[agent_id]
    assert len(items) == 1
    # The wire item carries the relative directory + filename (the fix).
    assert items[0].proposed_path == "performances/artists/Disclosure"
    assert items[0].proposed_filename == "Disclosure - Live at Coachella.mp3"

    _patch_settings(monkeypatch, [str(scan_root)])
    api = _make_api_client_mock()
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id=agent_id, proposals=items)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    expected_dest = scan_root / "performances/artists/Disclosure" / "Disclosure - Live at Coachella.mp3"
    assert expected_dest.exists(), f"file not moved to {expected_dest}"
    assert expected_dest.read_bytes() == content
    assert not orig.exists(), "original was not removed"
    # patch_proposal_state carried the resolved absolute destination as current_path.
    state = api.patch_proposal_state.await_args.args[1]
    assert state.proposal_state == "executed"
    assert state.current_path == str(expected_dest)


async def test_null_proposed_path_renames_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A null/empty proposed_path renames the file in place (same directory, new name).

    Pre-fix, a null proposed_path coerced to '' and resolved against /app -> escaped
    every scan_root -> ValueError. Now it means "keep the directory, apply the new
    filename".
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "library" / "messy name.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"x")

    proposals = [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            source_path=str(orig),
            proposed_path="",  # null -> '' on the wire: rename in place
            proposed_filename="Clean Name.mp3",
        ),
    ]
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=proposals)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    dest = orig.parent / "Clean Name.mp3"
    assert dest.exists(), "in-place rename did not produce the new filename"
    assert not orig.exists(), "original name still present after in-place rename"
    assert api.patch_proposal_state.await_args.args[1].current_path == str(dest)


# ---------------------------------------------------------------------------
# phaze-shzdj: dispatch must ship the file's CURRENT location as the move source.
#
# `FileRecord.original_path` is written once at ingest and never again (operator,
# 2026-08-24: "original_path should never change. it's the ORIGINAL location of the
# file. the current_path is where the file is now."). Dispatch used to ship it, so
# the SECOND execution of an already-renamed file was handed a source that no longer
# exists.
#
# The stale value fed THREE derivations, not one -- and only the first fails loudly:
#   1. the move SOURCE                     -> FileNotFoundError out of _sha256_of_file
#   2. `owning_root`                       -> destination built under the INGEST scan root
#   3. `original.parent` (rename in place) -> destination built in the INGEST directory
# 2 and 3 are silent-wrong-result defects: a fix that addressed only 1 would let the
# move succeed to the wrong place. So these tests assert WHERE THE FILE LANDED, never
# merely that nothing raised -- an "it no longer errors" assertion passes with the
# destination still wrong.
#
# BE PRECISE ABOUT WHAT THE PRE-FIX RED RUN PROVES. All three derivations read the SAME
# single value, `item.source_path` (named `item.original_path` until phaze-xzjrr renamed
# the wire field), so shipping `current_path` into it repairs all
# three at once -- 2 and 3 were never independent code defects, they were the same stale
# input flowing downstream. That also means 2 and 3 cannot be shown RED on their own
# through the real path: derivation 1's FileNotFoundError always fires first and masks
# them. The destination assertions below are therefore REGRESSION guards, not
# reproductions -- they exist so that a future partial fix (e.g. adding a separate
# `current_path` wire field that feeds only the source) fails here instead of shipping a
# correct-looking move to a stale directory.
#
# Every test below drives the REAL path dispatcher -> executor: a stored
# Agent/FileRecord/RenameProposal, the wire payload built by
# `get_approved_proposals_grouped_by_agent`, and `execute_approved_batch` against a
# real fixture on disk.
# ---------------------------------------------------------------------------


async def _seed_catalog_row(
    session: AsyncSession,
    *,
    agent_id: str,
    scan_roots: list[str],
    original_path: str,
    current_path: str,
    content: bytes,
    proposed_path: str,
    proposed_filename: str,
) -> None:
    """Seed Agent + FileRecord + APPROVED RenameProposal exactly as production stores them.

    ``original_path`` and ``current_path`` are set INDEPENDENTLY so a caller can
    reproduce an already-moved file: ingest location in one column, real on-disk
    location in the other. ``sha256_hash`` is the digest of ``content``, which is what
    the file at ``current_path`` holds -- a move does not change a file's bytes, so the
    ingest-time digest is still the correct one for the moved file.
    """
    session.add(Agent(id=agent_id, name=agent_id, token_hash=None, scan_roots=scan_roots, revoked_at=None))
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            sha256_hash=hashlib.sha256(content).hexdigest(),
            original_path=original_path,
            original_filename=original_path.rsplit("/", 1)[-1],
            current_path=current_path,
            file_type="music",
            file_size=len(content),
            agent_id=agent_id,
        ),
    )
    await session.flush()
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=proposed_filename,
            proposed_path=proposed_path,
            status=ProposalStatus.APPROVED,
            confidence=0.95,
        ),
    )
    await session.commit()


async def test_e2e_second_execution_moves_the_file_from_where_it_is_now(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-moved file's second execution reads AND writes relative to its CURRENT location.

    The fixture's ingest location and current location differ in BOTH their directory
    and their owning scan root, so this covers derivations 1 and 2 together: pre-fix the
    move source is the missing ingest path, and even if it were found the destination
    would be built under the INGEST scan root.

    Pre-fix this fails at `result["status"] == "completed"` -- `_verify_hash_or_raise`
    opens the ingest path, which is not on disk, and the proposal errors out.
    """
    root_ingest = tmp_path / "root-ingest"
    root_current = tmp_path / "root-current"
    ingest = root_ingest / "incoming" / "raw-set.mp3"
    current = root_current / "library" / "raw-set.mp3"
    # ONLY the current location exists on disk -- the ingest path was consumed by a
    # prior execution, which is exactly the state this bead is about.
    current.parent.mkdir(parents=True, exist_ok=True)
    root_ingest.mkdir(parents=True, exist_ok=True)
    content = b"concert-audio-bytes-already-moved"
    current.write_bytes(content)
    assert not ingest.exists()

    agent_id = "agent-shzdj-moved"
    scan_roots = [str(root_ingest), str(root_current)]
    await _seed_catalog_row(
        session,
        agent_id=agent_id,
        scan_roots=scan_roots,
        original_path=str(ingest),
        current_path=str(current),
        content=content,
        proposed_path="sorted/Disclosure",
        proposed_filename="Disclosure - Live at Coachella.mp3",
    )

    groups = await get_approved_proposals_grouped_by_agent(session)
    items = groups[agent_id]
    assert len(items) == 1

    _patch_settings(monkeypatch, scan_roots)
    api = _make_api_client_mock()
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id=agent_id, proposals=items)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0

    # Derivation 2: the destination is built under the CURRENT owning scan root.
    expected_dest = root_current / "sorted/Disclosure" / "Disclosure - Live at Coachella.mp3"
    assert expected_dest.exists(), f"file not moved to {expected_dest}"
    assert expected_dest.read_bytes() == content
    assert not current.exists(), "the file was not removed from its previous location"
    # ...and NOTHING was written under the ingest root. Asserting only that the move
    # succeeded would pass with the file landing here instead.
    stray = sorted(p for p in root_ingest.rglob("*") if p.is_file())
    assert stray == [], f"destination was built under the stale ingest root: {stray}"

    state = api.patch_proposal_state.await_args.args[1]
    assert state.proposal_state == "executed"
    assert state.current_path == str(expected_dest)

    # The seam itself, asserted LAST so the behavioural assertions above are what
    # characterise a pre-fix run instead of being short-circuited by this one.
    assert items[0].source_path == str(current)


async def test_e2e_second_execution_in_place_rename_targets_the_current_directory(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-place rename of an already-moved file renames it where it is, not where it was ingested.

    Derivation 3: with ``proposed_path == ""`` the destination directory is
    ``original.parent``. Pre-fix ``original`` is the ingest path, so the rename targets
    the INGEST directory -- a wrong destination that raises nothing once the source
    problem alone is fixed. This test is the one that pins it, because it asserts the
    landing directory rather than the absence of an error.
    """
    scan_root = tmp_path / "media"
    ingest = scan_root / "incoming" / "messy name.mp3"
    current = scan_root / "library" / "messy name.mp3"
    current.parent.mkdir(parents=True, exist_ok=True)
    ingest.parent.mkdir(parents=True, exist_ok=True)
    content = b"already-moved-in-place"
    current.write_bytes(content)
    assert not ingest.exists()

    agent_id = "agent-shzdj-inplace"
    await _seed_catalog_row(
        session,
        agent_id=agent_id,
        scan_roots=[str(scan_root)],
        original_path=str(ingest),
        current_path=str(current),
        content=content,
        proposed_path="",  # null -> '' on the wire: rename in place
        proposed_filename="Clean Name.mp3",
    )

    groups = await get_approved_proposals_grouped_by_agent(session)
    items = groups[agent_id]
    assert items[0].proposed_path == ""

    _patch_settings(monkeypatch, [str(scan_root)])
    api = _make_api_client_mock()
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id=agent_id, proposals=items)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0

    expected_dest = current.parent / "Clean Name.mp3"
    assert expected_dest.exists(), f"in-place rename did not land in the file's current directory ({expected_dest})"
    assert expected_dest.read_bytes() == content
    assert not current.exists(), "the file was not renamed away from its previous name"
    # The ingest directory must be untouched -- this is the assertion that a
    # source-only fix cannot satisfy.
    assert not (ingest.parent / "Clean Name.mp3").exists(), "in-place rename targeted the stale ingest directory"
    assert api.patch_proposal_state.await_args.args[1].current_path == str(expected_dest)
    assert items[0].source_path == str(current)


async def test_e2e_first_execution_is_unchanged_when_current_path_equals_original_path(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FIRST execution -- original_path == current_path == the on-disk location -- is unaffected.

    This is the blast-radius test (CLAUDE.md rule 4). The dispatch query has no
    already-moved predicate, so switching the shipped column changes the source path for
    EVERY executed proposal, not only second ones. For a file that has never been moved
    the two columns hold the same string, so the wire item and the destination must be
    byte-identical to what the pre-fix code produced.
    """
    scan_root = tmp_path / "media"
    orig = scan_root / "incoming" / "raw-set.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"never-moved-concert-audio"
    orig.write_bytes(content)

    agent_id = "agent-shzdj-first"
    await _seed_catalog_row(
        session,
        agent_id=agent_id,
        scan_roots=[str(scan_root)],
        original_path=str(orig),
        current_path=str(orig),  # never executed: the two columns agree
        content=content,
        proposed_path="performances/artists/Disclosure",
        proposed_filename="Disclosure - Live at Coachella.mp3",
    )

    groups = await get_approved_proposals_grouped_by_agent(session)
    items = groups[agent_id]

    _patch_settings(monkeypatch, [str(scan_root)])
    api = _make_api_client_mock()
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id=agent_id, proposals=items)
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    expected_dest = scan_root / "performances/artists/Disclosure" / "Disclosure - Live at Coachella.mp3"
    assert expected_dest.exists(), f"file not moved to {expected_dest}"
    assert expected_dest.read_bytes() == content
    assert not orig.exists(), "original was not removed"
    assert api.patch_proposal_state.await_args.args[1].current_path == str(expected_dest)
    assert items[0].source_path == str(orig)


# ---------------------------------------------------------------------------
# phaze-xzjrr: the producer/consumer wire seam, pinned by NAME and by VALUE.
#
# phaze-shzdj and phaze-2zeu0 both came from the same gap: the dispatcher and the
# executor share only a FIELD, and nothing asserted that the thing one puts in it is
# the thing the other takes out. The tests above close the VALUE half for the
# already-moved case. The test below closes the NAME half, on the wire.
#
# WHY THE EXISTING TESTS DO NOT ALREADY COVER IT. Every test in this file hands
# `ExecuteApprovedBatchPayload` a list of Python `ExecuteBatchProposalItem` objects, so
# the JSON KEY is never named in an assertion anywhere. That leaves a specific, cheap
# mistake uncaught: a `pydantic.Field(alias=...)` (or `serialization_alias`) renaming
# only the PYTHON attribute while the wire key stays put -- design option (c) on this
# bead, refused precisely because the wire shape is what an agent implementor reads.
# Under an alias the producer, the executor, mypy and every test in this file agree,
# and the JSON key still says something false. Asserting on `model_dump(mode="json")`
# is what sees it.
# ---------------------------------------------------------------------------

#: The JSON key carrying the move SOURCE in an `execute_approved_batch` payload.
#:
#: This literal IS the agent-facing contract: it is what
#: `services/execution_dispatch.py` emits, what
#: `ExecuteApprovedBatchPayload.model_validate` accepts at the top of
#: `tasks/execution.py::execute_approved_batch`, and what an out-of-tree agent
#: implementor codes against. `extra="forbid"` means a change here breaks BOTH skew
#: directions loudly (see `ExecuteBatchProposalItem`'s docstring), so change it only
#: together with the schema field, the producer, and all four consumer sites -- and
#: only after re-measuring `saq_jobs` and `scheduling_ledger` for serialized payloads.
MOVE_SOURCE_WIRE_KEY = "source_path"


async def test_the_wire_key_the_dispatcher_produces_is_the_one_the_executor_moves_from(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Producer -> JSON -> schema -> executor, asserted on ONE key by name and by value.

    Renaming either half alone fails here:

    * rename the schema field or the producer kwarg and the assertion on
      ``wire[MOVE_SOURCE_WIRE_KEY]`` raises ``KeyError`` -- the key the dispatcher
      emits is no longer the key this contract names;
    * rename the executor's reads (``item.source_path``) alone and the file is never
      moved, so the landing assertions fail;
    * add an alias so the attribute and the wire key diverge and the ``model_dump``
      key-set assertion fails, which no other test in this file can see.

    The fixture is an ALREADY-MOVED file -- ingest path absent from disk, real bytes at
    the current path -- so the value half is not vacuous: only the correct column
    reaching the correct key names a file that exists, and a wrong-but-consistent
    plumbing job still fails.
    """
    root = tmp_path / "media"
    ingest = root / "incoming" / "raw-set.mp3"
    current = root / "library" / "raw-set.mp3"
    current.parent.mkdir(parents=True, exist_ok=True)
    content = b"wire-seam-bytes"
    current.write_bytes(content)
    assert not ingest.exists()

    agent_id = "agent-xzjrr-seam"
    await _seed_catalog_row(
        session,
        agent_id=agent_id,
        scan_roots=[str(root)],
        original_path=str(ingest),
        current_path=str(current),
        content=content,
        proposed_path="sorted",
        proposed_filename="Renamed.mp3",
    )

    # 1. The REAL producer.
    items = (await get_approved_proposals_grouped_by_agent(session))[agent_id]
    assert len(items) == 1

    # 2. Serialize exactly as the enqueue does. THIS DICT IS THE WIRE -- an agent
    #    receives these bytes, not a Python object, so this is the only place the
    #    contract is observable.
    wire = items[0].model_dump(mode="json")
    assert MOVE_SOURCE_WIRE_KEY in wire, (
        f"the dispatcher no longer emits {MOVE_SOURCE_WIRE_KEY!r}; it emits {sorted(wire)}. "
        "The schema field, the producer and the executor must be renamed together."
    )
    # An alias would leave the ATTRIBUTE and the KEY disagreeing; require both.
    assert MOVE_SOURCE_WIRE_KEY in ExecuteBatchProposalItem.model_fields, (
        f"{MOVE_SOURCE_WIRE_KEY!r} is on the wire but is not a declared field name -- "
        "an alias hides the rename from the executor's readers and from mypy."
    )
    assert wire[MOVE_SOURCE_WIRE_KEY] == str(current), "the dispatcher did not ship the file's current location"

    # 3. Re-validate FROM the wire dict, the way the agent worker does.
    payload = ExecuteApprovedBatchPayload.model_validate(
        {"batch_id": str(uuid.uuid4()), "agent_id": agent_id, "proposals": [wire]},
    )

    # 4. The REAL executor.
    _patch_settings(monkeypatch, [str(root)])
    api = _make_api_client_mock()
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    # The consumer half: the file the executor moved is the one named by that key.
    assert not Path(wire[MOVE_SOURCE_WIRE_KEY]).exists(), "the executor did not move the file named by the wire key"
    dest = root / "sorted" / "Renamed.mp3"
    assert dest.exists() and dest.read_bytes() == content

    # The audit trail closes the loop: ExecutionLogCreate.source_path is populated from
    # this same wire field (tasks/execution.py), and after phaze-xzjrr the two names
    # agree -- so a divergence shows up as a mismatch here rather than only in prose.
    assert api.post_execution_log.await_args.args[0].source_path == wire[MOVE_SOURCE_WIRE_KEY]


async def test_the_old_wire_key_is_refused_before_any_file_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-phaze-xzjrr payload (``original_path``) dead-letters; it never half-moves a file.

    This is the stated skew behaviour, asserted rather than assumed (acceptance criterion 2).
    ``extra="forbid"`` gives BOTH errors -- unexpected ``original_path`` AND missing
    ``source_path`` -- and it fires inside ``ExecuteApprovedBatchPayload.model_validate``
    at the top of ``execute_approved_batch``, before any path is resolved. So a skewed
    deploy costs availability (the batch must be re-dispatched), never integrity.
    """
    root = tmp_path / "media"
    src = root / "incoming" / "raw-set.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"pre-rename-payload")

    legacy_wire = {
        "proposal_id": str(uuid.uuid4()),
        "file_id": str(uuid.uuid4()),
        "original_path": str(src),  # the pre-phaze-xzjrr key
        "proposed_path": "sorted",
        "proposed_filename": "Renamed.mp3",
        "sha256_hash": None,
    }

    _patch_settings(monkeypatch, [str(root)])
    api = _make_api_client_mock()
    with pytest.raises(pydantic.ValidationError) as exc_info:
        await execute_approved_batch(
            {"api_client": api},
            batch_id=str(uuid.uuid4()),
            agent_id="agent-xzjrr-skew",
            proposals=[legacy_wire],
        )

    kinds = {(e["type"], e["loc"][-1]) for e in exc_info.value.errors()}
    assert ("extra_forbidden", "original_path") in kinds
    assert ("missing", "source_path") in kinds
    # Nothing was touched: no file op, and no audit row claiming one.
    assert src.exists(), "the source was moved despite the payload never validating"
    assert not (root / "sorted" / "Renamed.mp3").exists()
    api.post_execution_log.assert_not_awaited()
