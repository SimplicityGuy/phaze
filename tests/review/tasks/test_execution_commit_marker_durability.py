"""Durability of the cross-filesystem commit marker (phaze-ihje6, seam D4).

The ``.phaze-committed.<proposal_id>`` marker is written between publishing a
cross-filesystem copy and unlinking its source.  Its whole purpose is to survive
a crash in that window, so the replay can reclaim the published destination as
this proposal's own rather than refusing it as a foreign occupant.

Two kinds of test, because no single test can exhibit the failure:

* The ordering test pins the syscall sequence -- copy fsync, publish link,
  marker fsync, destination-directory fsync, source unlink.  It is the only
  test here about POWER LOSS, and it proves the calls are made in order, not
  that the device honours them.
* The kill tests SIGKILL a real child process inside the window and run the
  real replay in the test process.  A SIGKILL leaves the page cache intact, so
  these pass with or without fsync; they prove the replay against an
  out-of-process crash residue, not the marker's durability.
"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from typing import TYPE_CHECKING
import uuid

import pytest
from structlog.testing import capture_logs

from phaze.schemas.agent_tasks import ExecuteBatchProposalItem
from phaze.tasks.execution_filesystem import (
    COMMIT_MARKER_SUFFIX,
    COPY_TMP_SUFFIX,
    FilesystemMoveRequest,
    LocalExecutionFilesystemEngine,
    LocalFilesystemPrimitives,
    MoveStep,
)


if TYPE_CHECKING:
    from collections.abc import Callable


_CONTENT = b"concert-set-bytes" * 4096


class _CrossFsPrimitives(LocalFilesystemPrimitives):
    """Force the cross-filesystem branch inside one tmp_path."""

    def same_filesystem(self, _src: Path, _dst_dir: Path) -> bool:
        return False


def _seed(tmp_path: Path) -> tuple[Path, Path, ExecuteBatchProposalItem]:
    original = tmp_path / "src" / "set.mkv"
    original.parent.mkdir(parents=True)
    original.write_bytes(_CONTENT)
    item = ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        source_path=str(original),
        proposed_path="dst",
        proposed_filename="Artist - Set.mkv",
        sha256_hash=hashlib.sha256(_CONTENT).hexdigest(),
    )
    return original, tmp_path / "dst" / "Artist - Set.mkv", item


async def test_marker_and_directory_are_fsynced_after_publish_and_before_source_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin copy fsync -> publish -> marker fsync -> dir fsync -> source unlink.

    Real-crash limit: this records the calls, it does not lose power.  It fails if
    the marker or its directory is not fsynced, or if either happens after the
    source unlink -- the cross-fs case where a power loss can keep the source
    unlink (one filesystem) and lose the published entry (the other).
    """
    original, proposed, item = _seed(tmp_path)
    events: list[tuple[str, object]] = []
    real_fsync: Callable[[int], None] = os.fsync
    real_link = os.link
    real_unlink = os.unlink

    def spy_fsync(fd: int) -> None:
        st = os.fstat(fd)
        events.append(("fsync-dir" if stat.S_ISDIR(st.st_mode) else "fsync-file", st.st_ino))
        real_fsync(fd)

    def spy_link(src: object, dst: object) -> None:
        real_link(src, dst)  # type: ignore[arg-type]
        events.append(("link", Path(str(dst))))

    def spy_unlink(path: object, *args: object, **kwargs: object) -> None:
        events.append(("unlink", Path(str(path))))
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "link", spy_link)
    monkeypatch.setattr(os, "unlink", spy_unlink)

    result = await LocalExecutionFilesystemEngine(_CrossFsPrimitives()).move(
        FilesystemMoveRequest(item=item, scan_roots=[str(tmp_path)]),
        MoveStep(),
    )

    assert result.committed_now is True
    assert proposed.read_bytes() == _CONTENT
    copy_ino = proposed.stat().st_ino
    dir_ino = proposed.parent.stat().st_ino

    def index(event: tuple[str, object]) -> int:
        assert event in events, f"missing {event} in {events}"
        return events.index(event)

    file_fsyncs = [ino for kind, ino in events if kind == "fsync-file"]
    assert len(file_fsyncs) == 2, events
    assert file_fsyncs[0] == copy_ino
    marker_ino = file_fsyncs[1]
    assert marker_ino != copy_ino

    copy_fsync = index(("fsync-file", copy_ino))
    publish = index(("link", proposed))
    marker_fsync = index(("fsync-file", marker_ino))
    dir_fsync = index(("fsync-dir", dir_ino))
    source_unlink = index(("unlink", original))
    assert copy_fsync < publish < marker_fsync < dir_fsync < source_unlink, events


def _raise_on_directory_fsync(monkeypatch: pytest.MonkeyPatch, err: int) -> list[int]:
    """Make fsync fail with ``err`` for directory fds only; return the file-fsync inodes."""
    real_fsync: Callable[[int], None] = os.fsync
    file_fsyncs: list[int] = []

    def fsync(fd: int) -> None:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise OSError(err, os.strerror(err))
        file_fsyncs.append(st.st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return file_fsyncs


@pytest.mark.parametrize("err", [errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP])
async def test_unsupported_directory_fsync_is_best_effort_and_logged_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int) -> None:
    """A mount that cannot fsync a directory fd must not fail every cross-fs move onto it.

    The marker file is still written and fsynced, the move completes, and the
    warning is logged once per directory without the real path.
    """
    file_fsyncs = _raise_on_directory_fsync(monkeypatch, err)
    primitives = _CrossFsPrimitives()
    markers_seen: list[bool] = []
    real_write = primitives.write_commit_marker

    def write_and_observe(marker: Path, proposal_id: uuid.UUID) -> None:
        real_write(marker, proposal_id)
        markers_seen.append(marker.read_text() == str(proposal_id))

    monkeypatch.setattr(primitives, "write_commit_marker", write_and_observe)
    engine = LocalExecutionFilesystemEngine(primitives)

    original, proposed, item = _seed(tmp_path)
    second = original.with_name("second.mkv")
    second.write_bytes(_CONTENT)
    second_item = item.model_copy(update={"proposal_id": uuid.uuid4(), "source_path": str(second), "proposed_filename": "Artist - Set 2.mkv"})

    with capture_logs() as logs:
        for each in (item, second_item):
            result = await engine.move(FilesystemMoveRequest(item=each, scan_roots=[str(tmp_path)]), MoveStep())
            assert result.committed_now is True

    assert markers_seen == [True, True]
    assert len(file_fsyncs) == 4  # copy + marker, per move: the FILE fsync stays mandatory
    assert not original.exists()
    assert not second.exists()
    assert proposed.read_bytes() == _CONTENT
    warnings = [entry for entry in logs if entry["event"] == "directory fsync unsupported; continuing"]
    assert len(warnings) == 1
    assert warnings[0]["errno"] == err
    assert warnings[0]["path_role"] == "destination_dir"
    assert str(tmp_path) not in repr(warnings[0])


async def test_other_directory_fsync_errors_propagate_before_the_source_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only "unsupported" errnos are tolerated: an EIO fails the move and keeps the source."""
    _raise_on_directory_fsync(monkeypatch, errno.EIO)
    original, proposed, item = _seed(tmp_path)

    with pytest.raises(OSError, match=os.strerror(errno.EIO)) as excinfo:
        await LocalExecutionFilesystemEngine(_CrossFsPrimitives()).move(
            FilesystemMoveRequest(item=item, scan_roots=[str(tmp_path)]),
            MoveStep(),
        )

    assert excinfo.value.errno == errno.EIO
    assert original.read_bytes() == _CONTENT
    assert proposed.read_bytes() == _CONTENT


_CHILD = """
import asyncio, sys, time
from pathlib import Path
from phaze.schemas.agent_tasks import ExecuteBatchProposalItem
from phaze.tasks.execution_filesystem import FilesystemMoveRequest, LocalExecutionFilesystemEngine, LocalFilesystemPrimitives, MoveStep

item_json, ready, kill_point, root = sys.argv[1:5]
item = ExecuteBatchProposalItem.model_validate_json(item_json)

def park():
    Path(ready).write_text("parked")
    time.sleep(600)

class P(LocalFilesystemPrimitives):
    def same_filesystem(self, src, dst_dir):
        return False

    def write_commit_marker(self, marker, proposal_id):
        if kill_point == "before_marker":
            park()
        super().write_commit_marker(marker, proposal_id)
        park()

asyncio.run(LocalExecutionFilesystemEngine(P()).move(FilesystemMoveRequest(item=item, scan_roots=[root]), MoveStep()))
"""


def _run_child_and_sigkill(tmp_path: Path, item: ExecuteBatchProposalItem, kill_point: str) -> None:
    ready = tmp_path / "child-parked"
    child = subprocess.Popen(  # noqa: S603  # trusted input: sys.executable + literal -c script
        [sys.executable, "-c", _CHILD, item.model_dump_json(), str(ready), kill_point, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            if child.poll() is not None:
                _, err = child.communicate()
                pytest.fail(f"child exited {child.returncode} before parking: {err.decode()}")
            if time.monotonic() > deadline:
                pytest.fail("child never reached the kill point")
            time.sleep(0.05)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
    assert child.returncode == -signal.SIGKILL


async def test_replay_after_sigkill_between_marker_and_unlink_reclaims_without_data_loss(tmp_path: Path) -> None:
    """A real child is SIGKILLed after the marker write, before the source unlink.

    The replay, in a different process, must recognise the published copy as its own through
    the marker, finish the move and leave exactly one intact copy.  Real-crash
    limit: SIGKILL keeps the page cache, so this passes with or without fsync --
    durability is pinned by the ordering test above, not here.
    """
    original, proposed, item = _seed(tmp_path)
    _run_child_and_sigkill(tmp_path, item, "after_marker")

    marker = proposed.with_name(f"{proposed.name}{COMMIT_MARKER_SUFFIX}.{item.proposal_id}")
    assert original.read_bytes() == _CONTENT  # the kill landed before the unlink
    assert proposed.read_bytes() == _CONTENT
    assert marker.exists()

    result = await LocalExecutionFilesystemEngine(_CrossFsPrimitives()).move(
        FilesystemMoveRequest(item=item, scan_roots=[str(tmp_path)]),
        MoveStep(),
    )

    assert result.committed_now is True
    assert result.proposed == proposed
    assert not original.exists()
    assert proposed.read_bytes() == _CONTENT
    assert not marker.exists()
    assert not list(proposed.parent.glob(f"*{COPY_TMP_SUFFIX}*"))


async def test_replay_after_sigkill_before_marker_refuses_and_keeps_both_copies(tmp_path: Path) -> None:
    """Control: killed after publish but before the marker, the replay refuses.

    Without the marker the published copy is indistinguishable from a foreign
    occupant, so the replay must fail loudly and destroy nothing.  This is the
    outcome a lost (un-fsynced) marker produces after a power loss: stuck, not
    data loss -- the data-loss case is the lost destination entry the directory
    fsync prevents.
    """
    original, proposed, item = _seed(tmp_path)
    _run_child_and_sigkill(tmp_path, item, "before_marker")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        await LocalExecutionFilesystemEngine(_CrossFsPrimitives()).move(
            FilesystemMoveRequest(item=item, scan_roots=[str(tmp_path)]),
            MoveStep(),
        )

    assert original.read_bytes() == _CONTENT
    assert proposed.read_bytes() == _CONTENT
