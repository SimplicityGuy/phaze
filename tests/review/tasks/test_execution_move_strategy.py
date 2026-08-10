"""Regression tests for the execute_approved_batch move strategy (bead phaze-uciu.5).

The executor must never load a whole file into RAM: the core use case is
multi-GB concert videos and ``execute_approved_batch`` runs on the 'meta' lane
(concurrency 2, no memory pin), so a whole-file read would MemoryError or get
the worker OOM-killed. The move therefore:

* prefers an ``os.link`` claim + source unlink (atomic, O(1), constant memory)
  when source and destination share a filesystem; and
* falls back to a bounded ``shutil.copyfileobj`` stream + fsync + unlink across
  a filesystem boundary.

phaze-s0wu: both branches publish through a NO-CLOBBER claim (``os.link`` fails
EEXIST), not an unconditional rename. The exists-check in ``_execute_one`` runs
once up front while the destructive act can be minutes later, so only an atomic
claim at publish time can refuse an occupant that arrived in between.

These tests cover BOTH branches plus the bounded-memory guarantee (the fallback
must not call ``Path.read_bytes``) and the helper units.
"""

from __future__ import annotations

import errno
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from phaze.config import AgentSettings
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
import phaze.tasks.execution as execmod
from phaze.tasks.execution import _same_filesystem, _streamed_copy, execute_approved_batch


if TYPE_CHECKING:
    from pathlib import Path


def _make_api_client_mock() -> AsyncMock:
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    api.post_exec_batch_progress = AsyncMock(return_value=None)
    return api


def _patch_settings(monkeypatch: pytest.MonkeyPatch, scan_roots: list[str]) -> None:
    fake_cfg = MagicMock(spec=AgentSettings)
    fake_cfg.scan_roots = scan_roots
    monkeypatch.setattr("phaze.tasks.execution.get_settings", lambda: fake_cfg)


def _item(orig: Path, proposed_path: str, filename: str) -> ExecuteBatchProposalItem:
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        original_path=str(orig),
        proposed_path=proposed_path,
        proposed_filename=filename,
    )


async def test_same_filesystem_move_is_o1_and_never_streams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-fs move is an O(1) constant-memory link claim -- it never streams or read_bytes.

    phaze-s0wu: this used to assert ``os.replace`` specifically. The move is now an ``os.link``
    claim plus a source unlink, which keeps every property the assertion was protecting (atomic,
    O(1), no bytes read) and adds the one it could not express: the destination create is
    no-clobber, so a file that appeared after the exists-check is refused rather than destroyed.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "concert.mp4"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"a" * (3 * 1024 * 1024)
    orig.write_bytes(content)

    calls = {"link": 0, "stream": 0}
    real_link = execmod.os.link

    def spy_link(src: object, dst: object) -> None:
        calls["link"] += 1
        real_link(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(execmod.os, "link", spy_link)
    monkeypatch.setattr(execmod, "_streamed_copy", lambda _s, _d: calls.__setitem__("stream", calls["stream"] + 1))

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "moved", "concert.mp4")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert calls["link"] == 1
    assert calls["stream"] == 0
    dest = tmp_path / "moved" / "concert.mp4"
    assert dest.exists()
    with dest.open("rb") as fh:
        assert fh.read() == content
    assert not orig.exists()


async def test_cross_filesystem_move_streams_with_bounded_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-fs move streams in bounded chunks and never slurps the whole file into RAM.

    We force the fallback branch and make ``Path.read_bytes`` fail loudly: if the
    move path ever reads the whole file into memory the test aborts.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "huge.mkv"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"z" * (5 * 1024 * 1024)
    orig.write_bytes(content)

    from pathlib import Path as _Path

    monkeypatch.setattr(execmod, "_same_filesystem", lambda _s, _d: False)

    def _forbid_read_bytes(_self: object) -> bytes:
        msg = "read_bytes() slurps the whole file -- the streamed move must not call it"
        raise AssertionError(msg)

    monkeypatch.setattr(_Path, "read_bytes", _forbid_read_bytes)

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "out", "huge.mkv")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    dest = tmp_path / "out" / "huge.mkv"
    assert dest.exists()
    with dest.open("rb") as fh:
        assert fh.read() == content
    # Cross-filesystem copy leaves the original in place, then unlinks it.
    assert not orig.exists()


async def test_move_refuses_to_clobber_existing_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A destination that already exists is never overwritten: the proposal fails, both files survive (phaze-yu2e)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig = tmp_path / "orig" / "set.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"ORIGINAL-BYTES")

    dest = tmp_path / "moved" / "Artist - Track.mp3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"PREEXISTING-DO-NOT-DESTROY")

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "moved", "Artist - Track.mp3")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    # Neither the source nor the pre-existing destination was destroyed.
    assert orig.read_bytes() == b"ORIGINAL-BYTES"
    assert dest.read_bytes() == b"PREEXISTING-DO-NOT-DESTROY"
    # Reported as a failure at the 'copy' step.
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"
    assert api.patch_execution_log.await_args.args[1].error_message.startswith("copy:")


async def test_in_place_rename_to_same_file_is_not_a_clobber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename whose destination resolves to the file itself is a no-op, not a refused clobber (phaze-yu2e)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig = tmp_path / "set.mp3"
    orig.write_bytes(b"KEEP-ME")

    # Empty proposed_path == rename in place; same filename -> destination IS the original file.
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "", "set.mp3")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    assert orig.read_bytes() == b"KEEP-ME"


async def test_cross_fs_copy_failure_leaves_no_partial_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-fs copy that aborts mid-stream leaves NO file at `proposed` and no temp (phaze-k23z).

    Pre-fix, ``_streamed_copy`` wrote straight into the final destination, so an
    ENOSPC/EIO partway through left a truncated fragment at the exact proposed
    path -- misleading 'already moved' checks and wasting disk. The atomic
    temp-then-replace copy must leave the destination absent and the source
    intact.
    """
    import shutil as _shutil

    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "concert.mkv"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"v" * (4 * 1024 * 1024))

    monkeypatch.setattr(execmod, "_same_filesystem", lambda _s, _d: False)

    def _boom(_fsrc: object, _fdst: object, length: int = 0) -> None:
        # Simulate ENOSPC on a multi-GB video partway through the copy.
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(_shutil, "copyfileobj", _boom)

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "out", "concert.mkv")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    # No partial file at the real destination, and no leftover temp sibling.
    dest = tmp_path / "out" / "concert.mkv"
    assert not dest.exists()
    # phaze-otoqj: the tmp name is now unique per attempt (pid+uuid4 suffix), so glob for the
    # family of names rather than the single deterministic one.
    assert not list((tmp_path / "out").glob(f"concert.mkv{execmod._COPY_TMP_SUFFIX}*"))
    # The source is untouched -- no data loss.
    assert orig.read_bytes() == b"v" * (4 * 1024 * 1024)
    # Reported as a failure at the 'copy' step.
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"
    assert api.patch_execution_log.await_args.args[1].error_message.startswith("copy:")


def test_is_same_file_true_for_same_path(tmp_path: Path) -> None:
    from phaze.tasks.execution import _is_same_file

    f = tmp_path / "x.mp3"
    f.write_bytes(b"data")
    assert _is_same_file(f, f) is True


def test_is_same_file_false_for_distinct_files(tmp_path: Path) -> None:
    from phaze.tasks.execution import _is_same_file

    a = tmp_path / "a"
    a.write_bytes(b"1")
    b = tmp_path / "b"
    b.write_bytes(b"2")
    assert _is_same_file(a, b) is False


def test_is_same_file_false_when_destination_missing(tmp_path: Path) -> None:
    from phaze.tasks.execution import _is_same_file

    a = tmp_path / "a"
    a.write_bytes(b"1")
    assert _is_same_file(a, tmp_path / "nope") is False


def test_same_filesystem_helper_true_within_one_tree(tmp_path: Path) -> None:
    """Two paths under the same tmp dir share a filesystem."""
    a = tmp_path / "a"
    a.write_bytes(b"x")
    assert _same_filesystem(a, tmp_path) is True


# ---------------------------------------------------------------------------
# phaze-timy: the blocking streamed copy and sha256 hashing must run OFF the
# meta-lane worker's event loop (via asyncio.to_thread) so a multi-GB copy/hash
# cannot freeze the loop, starving the co-scheduled lane slot, SAQ's timers, and
# the Phase-46 heartbeat for minutes.
# ---------------------------------------------------------------------------


def _item_with_hash(orig: Path, proposed_path: str, filename: str, sha256_hash: str) -> ExecuteBatchProposalItem:
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        original_path=str(orig),
        proposed_path=proposed_path,
        proposed_filename=filename,
        sha256_hash=sha256_hash,
    )


async def test_verify_hash_runs_off_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sha256 verify of the original is dispatched via asyncio.to_thread."""
    import hashlib

    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "set.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"a" * (2 * 1024 * 1024)
    orig.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    real_to_thread = execmod.asyncio.to_thread
    offloaded: list[str] = []

    async def _spy(func: object, *args: object, **kwargs: object) -> object:
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execmod.asyncio, "to_thread", _spy)

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item_with_hash(orig, "moved", "set.mp3", digest)])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    # The verify hash of the original was offloaded, not run on the event loop.
    assert "_sha256_of_file" in offloaded


async def test_cross_fs_copy_runs_off_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cross-filesystem streamed copy is dispatched via asyncio.to_thread."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "huge.mkv"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"z" * (5 * 1024 * 1024)
    orig.write_bytes(content)

    monkeypatch.setattr(execmod, "_same_filesystem", lambda _s, _d: False)

    real_to_thread = execmod.asyncio.to_thread
    offloaded: list[str] = []

    async def _spy(func: object, *args: object, **kwargs: object) -> object:
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execmod.asyncio, "to_thread", _spy)

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "out", "huge.mkv")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    dest = tmp_path / "out" / "huge.mkv"
    assert dest.exists()
    assert not orig.exists()
    # The atomic cross-fs copy primitive was offloaded, not run on the event loop.
    assert "_atomic_cross_fs_copy" in offloaded


def test_streamed_copy_preserves_content_and_mtime(tmp_path: Path) -> None:
    """The streamed copy reproduces bytes exactly and preserves mtime (copystat)."""
    src = tmp_path / "src.bin"
    payload = b"q" * (2 * 1024 * 1024 + 7)  # non-chunk-aligned size
    src.write_bytes(payload)
    import os as _os

    _os.utime(src, (1_600_000_000, 1_600_000_000))
    dst = tmp_path / "nested" / "dst.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)

    _streamed_copy(src, dst)

    with dst.open("rb") as fh:
        assert fh.read() == payload
    assert dst.stat().st_mtime == src.stat().st_mtime


# ---------------------------------------------------------------------------
# phaze-s0wu — the no-clobber guard must be ATOMIC, not check-then-act.
#
# The exists-check in _execute_one runs once, up front; the destructive act can
# be minutes later (a multi-GB cross-fs copy). Anything that lands at the
# destination inside that window was silently overwritten, and if the occupant
# was a completed same-fs move its source was already gone -- so its bytes were
# unrecoverable while BOTH proposals reported success.
# ---------------------------------------------------------------------------


async def test_cross_fs_copy_refuses_a_destination_occupied_during_the_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that appears at the destination DURING the copy must not be destroyed (phaze-s0wu).

    This is the data-loss shape: the exists-check passed long ago, so only the publish step can
    still catch the occupant. It needs no dispatch-time gate miss at all -- the occupant can be a
    file the operator's own tooling wrote into the archive while the copy ran.
    """
    src = tmp_path / "src" / "<track-01>.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"the file being moved")
    dst = tmp_path / "dst" / "<track-01>.mp3"
    dst.parent.mkdir(parents=True, exist_ok=True)

    real_streamed_copy = execmod._streamed_copy

    def _copy_then_someone_else_lands_at_dst(s: Path, d: Path) -> None:
        real_streamed_copy(s, d)
        # Simulate the concurrent actor publishing to `dst` mid-copy.
        dst.write_bytes(b"a DIFFERENT file's bytes, already committed elsewhere")

    monkeypatch.setattr(execmod, "_streamed_copy", _copy_then_someone_else_lands_at_dst)

    with pytest.raises(FileExistsError):
        execmod._atomic_cross_fs_copy(src, dst)

    # The occupant survives untouched -- that is the whole point.
    assert dst.read_bytes() == b"a DIFFERENT file's bytes, already committed elsewhere"
    # ...and this move lost nothing either: the source is intact and the temp file is cleaned up.
    assert src.read_bytes() == b"the file being moved"
    # phaze-otoqj: unique-per-attempt tmp name -- glob for the family, not one fixed path.
    assert not list((tmp_path / "dst").glob(f"<track-01>.mp3{execmod._COPY_TMP_SUFFIX}*"))


async def test_cross_fs_copy_publishes_to_a_free_destination(tmp_path: Path) -> None:
    """The happy path still lands the full, fsynced copy and orphans no temp file."""
    src = tmp_path / "src" / "<track-02>.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"payload" * 1000)
    dst = tmp_path / "dst" / "<track-02>.mp3"
    dst.parent.mkdir(parents=True, exist_ok=True)

    execmod._atomic_cross_fs_copy(src, dst)

    assert dst.read_bytes() == b"payload" * 1000
    # phaze-otoqj: unique-per-attempt tmp name -- glob for the family, not one fixed path.
    assert not list((tmp_path / "dst").glob(f"<track-02>.mp3{execmod._COPY_TMP_SUFFIX}*"))
    assert src.exists()  # the caller owns the unlink, not this primitive


async def test_cross_fs_copy_falls_back_when_the_filesystem_has_no_hard_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAT/SMB/FUSE mounts cannot link -- the move must still work, via the documented fallback."""
    src = tmp_path / "src" / "<track-03>.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"content")
    dst = tmp_path / "dst" / "<track-03>.mp3"
    dst.parent.mkdir(parents=True, exist_ok=True)

    def _no_links(_s: object, _d: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "operation not supported")

    monkeypatch.setattr(execmod.os, "link", _no_links)
    execmod._atomic_cross_fs_copy(src, dst)

    assert dst.read_bytes() == b"content"
    # phaze-otoqj: unique-per-attempt tmp name -- glob for the family, not one fixed path.
    assert not list((tmp_path / "dst").glob(f"<track-03>.mp3{execmod._COPY_TMP_SUFFIX}*"))


async def test_same_fs_move_refuses_an_occupant_rather_than_clobbering_it(tmp_path: Path) -> None:
    """The same-fs move is a no-clobber claim too -- an external writer cannot be overwritten."""
    src = tmp_path / "src" / "<track-04>.mp3"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"mine")
    dst = tmp_path / "dst" / "<track-04>.mp3"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"someone else's")

    with pytest.raises(FileExistsError):
        execmod._atomic_same_fs_move(src, dst)

    assert dst.read_bytes() == b"someone else's"
    assert src.read_bytes() == b"mine"


async def test_same_fs_move_completes_a_crashed_link_claim_rather_than_no_op_renaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between the link claim and the source unlink must complete forward (phaze-s0wu).

    Both paths then name ONE inode. ``Path.replace`` between two links of the same inode is a
    POSIX no-op, so a replay that fell through to the plain rename would leave the source in place
    forever while reporting success. The extra link is the evidence that distinguishes this
    residue from a genuine case-only rename.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "<track-05>.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"already claimed")
    dest_dir = tmp_path / "moved"
    dest_dir.mkdir(parents=True, exist_ok=True)
    # The residue of a crashed claim: dst is a hard link to the still-present source.
    execmod.os.link(orig, dest_dir / "<track-05>.mp3")
    assert orig.stat().st_nlink == 2

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "moved", "<track-05>.mp3")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert (dest_dir / "<track-05>.mp3").read_bytes() == b"already claimed"
    assert not orig.exists(), "the crashed claim must be completed forward, not left dangling"


# ---------------------------------------------------------------------------
# phaze-kxnnd -- st_nlink>1 alone is NOT sufficient evidence that the extra link
# sits AT `proposed`: a case-only rename on a case-insensitive filesystem also
# makes `original != proposed` true while both name the SAME single directory
# entry, and an unrelated pre-existing hard link elsewhere in the archive
# inflates nlink with nothing to do with `proposed`. The fix rules out the
# case-only-same-entry shape (a pure path comparison) before ever trusting nlink.
# ---------------------------------------------------------------------------


def test_is_case_only_same_entry_true_for_case_variants_in_the_same_directory(tmp_path: Path) -> None:
    from phaze.tasks.execution import _is_case_only_same_entry

    original = tmp_path / "dir" / "Track.MP3"
    proposed = tmp_path / "dir" / "track.mp3"
    assert _is_case_only_same_entry(original, proposed) is True


def test_is_case_only_same_entry_false_across_different_directories(tmp_path: Path) -> None:
    """A same-name (or same-case-fold-name) file in a DIFFERENT directory is a real distinct entry."""
    from phaze.tasks.execution import _is_case_only_same_entry

    original = tmp_path / "orig" / "Track.MP3"
    proposed = tmp_path / "moved" / "track.mp3"
    assert _is_case_only_same_entry(original, proposed) is False


def test_is_case_only_same_entry_false_for_genuinely_different_names(tmp_path: Path) -> None:
    """Names that differ by more than case are never mistaken for a case-only alias."""
    from phaze.tasks.execution import _is_case_only_same_entry

    original = tmp_path / "dir" / "concert.mp4"
    proposed = tmp_path / "dir" / "renamed-concert.mp4"
    assert _is_case_only_same_entry(original, proposed) is False


async def test_same_fs_case_only_rename_with_unrelated_hardlink_does_not_delete_the_only_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for phaze-kxnnd.

    A case-only rename (``Track.MP3`` -> ``track.mp3``) of a file that ALSO carries an
    unrelated pre-existing hard link elsewhere in the archive (``st_nlink == 2``, but
    neither link is at ``proposed``) must complete the rename via ``original.replace``,
    never via ``original.unlink()``. Pre-fix, ``st_nlink > 1`` alone was trusted as proof
    of a crashed same-fs link claim and the executor deleted `original` outright -- the
    rename to `proposed` never happened, yet the proposal still reported success with a
    `current_path` that resolved to nothing.

    ``_is_same_file`` is forced to report a match here (this is the one thing a genuinely
    case-insensitive filesystem gives for free and this repo's test filesystem may not) --
    this isolates the fix under test (``_is_case_only_same_entry``) from the host
    filesystem's actual case-sensitivity, which is otherwise unrelated to it.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    monkeypatch.setattr(execmod, "_is_same_file", lambda _a, _b: True)

    orig = tmp_path / "dir" / "Track.MP3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"the only copy in this directory")
    # The unrelated pre-existing hard link elsewhere in the archive (not at `proposed`).
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    execmod.os.link(orig, backup_dir / "Track.MP3")
    assert orig.stat().st_nlink == 2

    proposed = tmp_path / "dir" / "track.mp3"
    # Empty proposed_path == rename in place, so `proposed` lands in the SAME directory
    # as `original` with only the filename's case changed.
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "", "track.mp3")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    # The whole point: the rename actually happened, unlike the pre-fix delete-only outcome.
    assert proposed.exists()
    assert proposed.read_bytes() == b"the only copy in this directory"
    # The unrelated backup hard link is untouched by this proposal's move.
    assert (backup_dir / "Track.MP3").read_bytes() == b"the only copy in this directory"
    state_patch = api.patch_proposal_state.await_args.args[1]
    assert state_patch.proposal_state == "executed"
    assert state_patch.current_path == str(proposed)


# ---------------------------------------------------------------------------
# phaze-otoqj -- the cross-fs staging file must be unique per attempt AND
# opened O_EXCL, and the destination is re-verified before the source is
# unlinked. The old deterministic tmp name let two genuinely concurrent
# attempts at the same destination share one truncatable inode.
# ---------------------------------------------------------------------------


def test_unique_tmp_path_differs_across_attempts_at_the_same_destination(tmp_path: Path) -> None:
    """`_unique_tmp_path` must not be derivable from `dst` alone (the phaze-otoqj root cause)."""
    dst = tmp_path / "out" / "dest.bin"

    first = execmod._unique_tmp_path(dst)
    second = execmod._unique_tmp_path(dst)

    assert first != second
    assert first.name.startswith(f"dest.bin{execmod._COPY_TMP_SUFFIX}")
    assert second.name.startswith(f"dest.bin{execmod._COPY_TMP_SUFFIX}")
    assert first.parent == dst.parent == second.parent


def test_atomic_cross_fs_copy_stages_into_a_fresh_tmp_name_each_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive `_atomic_cross_fs_copy` calls to the same `dst` never reuse a tmp path."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 1024)
    dst = tmp_path / "out" / "dest.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)

    seen: list[Path] = []
    real_streamed_copy = execmod._streamed_copy

    def _record(s: Path, d: Path) -> None:
        seen.append(d)
        real_streamed_copy(s, d)

    monkeypatch.setattr(execmod, "_streamed_copy", _record)

    execmod._atomic_cross_fs_copy(src, dst)
    dst.unlink()
    execmod._atomic_cross_fs_copy(src, dst)

    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_streamed_copy_refuses_to_write_into_an_existing_path(tmp_path: Path) -> None:
    """`_streamed_copy` opens O_EXCL -- it must never silently share/truncate an existing inode.

    phaze-otoqj: this is the belt half of "belt and suspenders" -- even if two attempts
    somehow computed the same staging name, O_EXCL turns that into a loud FileExistsError
    instead of a silent shared-inode write.
    """
    src = tmp_path / "src.bin"
    src.write_bytes(b"new content")
    dst = tmp_path / "dst.bin"
    dst.write_bytes(b"pre-existing, must survive")

    with pytest.raises(FileExistsError):
        _streamed_copy(src, dst)

    assert dst.read_bytes() == b"pre-existing, must survive"


def test_concurrent_cross_fs_copies_to_one_destination_never_share_one_staging_inode(tmp_path: Path) -> None:
    """Two genuinely concurrent `_atomic_cross_fs_copy` calls at the same `dst` must not interleave.

    phaze-otoqj failure scenario, reproduced directly against the primitive: two real OS
    threads (matching the ``asyncio.to_thread`` offload `_execute_one` uses) race to publish
    to the same destination. Pre-fix, both staged into ONE shared, deterministically-named
    inode and could publish a half-A-half-B interleave. Post-fix, each gets its own staging
    inode, so exactly one wins the no-clobber publish and the other is refused outright --
    never a corrupted destination, never both reporting success.
    """
    import threading

    src_a = tmp_path / "a.bin"
    src_b = tmp_path / "b.bin"
    content_a = b"A" * (2 * 1024 * 1024)
    content_b = b"B" * (2 * 1024 * 1024)
    src_a.write_bytes(content_a)
    src_b.write_bytes(content_b)
    dst = tmp_path / "out" / "dest.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)

    barrier = threading.Barrier(2, timeout=5)
    real_streamed_copy = execmod._streamed_copy

    def _synced_streamed_copy(src: Path, tmp: Path) -> None:
        # Force genuine temporal overlap between the two threads' writes -- each writes into
        # its OWN unique tmp file, so this proves the fix rather than defeating it.
        barrier.wait()
        real_streamed_copy(src, tmp)

    results: dict[str, str] = {}

    def _run(name: str, src: Path) -> None:
        try:
            execmod._atomic_cross_fs_copy(src, dst)
            results[name] = "ok"
        except FileExistsError:
            results[name] = "refused"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(execmod, "_streamed_copy", _synced_streamed_copy)
        t_a = threading.Thread(target=_run, args=("a", src_a))
        t_b = threading.Thread(target=_run, args=("b", src_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

    # Exactly one attempt wins the publish; the other is refused -- never both "succeeding"
    # with a corrupted/interleaved destination.
    assert sorted(results.values()) == ["ok", "refused"]
    # The winner's bytes are fully intact, not an interleave of A and B.
    winner_content = dst.read_bytes()
    assert winner_content in (content_a, content_b)
    # Both sources are untouched -- this primitive never unlinks the source itself.
    assert src_a.read_bytes() == content_a
    assert src_b.read_bytes() == content_b
    # No orphaned temp files left behind by either attempt.
    assert not list((tmp_path / "out").glob(f"dest.bin{execmod._COPY_TMP_SUFFIX}*"))


async def test_cross_fs_copy_post_publish_hash_mismatch_fails_loudly_without_deleting_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted publish must fail loudly at 'verify' and leave `original` untouched (phaze-otoqj).

    Simulates the exact failure this bead closes: bytes land at `proposed` that do not match
    the caller-supplied hash (interleaved/corrupt bytes from a shared-inode race, or any other
    copy-path corruption). The re-verify must catch it BEFORE `original.unlink()` runs.
    """
    import hashlib

    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    monkeypatch.setattr(execmod, "_same_filesystem", lambda _s, _d: False)

    orig = tmp_path / "orig" / "set.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"good bytes, the real file"
    orig.write_bytes(content)
    expected_hash = hashlib.sha256(content).hexdigest()

    real_copy = execmod._atomic_cross_fs_copy

    def _corrupting_copy(src: Path, dst: Path) -> None:
        real_copy(src, dst)
        dst.write_bytes(b"CORRUPTED BYTES, NOT WHAT WAS COPIED")

    monkeypatch.setattr(execmod, "_atomic_cross_fs_copy", _corrupting_copy)

    payload = ExecuteApprovedBatchPayload(
        batch_id=uuid.uuid4(),
        agent_id="a",
        proposals=[_item_with_hash(orig, "out", "set.mp3", expected_hash)],
    )
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    # The whole point: the source must survive a detected post-publish corruption.
    assert orig.exists()
    assert orig.read_bytes() == content
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"
    assert api.patch_execution_log.await_args.args[1].error_message.startswith("verify:")
