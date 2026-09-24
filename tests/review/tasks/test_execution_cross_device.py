"""Cross-filesystem execution on a GENUINELY different device (phaze-x4kvv, seam D1).

Every other cross-filesystem test monkeypatches ``same_filesystem`` to ``False``
inside one ``tmp_path``, so ``os.link`` / ``os.replace`` always succeed there and
``copystat`` always lands on the same filesystem it read from.  These tests patch
nothing: the destination is a second mounted device, so the kernel itself answers
EXDEV, a filesystem without hard links answers the link claim for real, and a
deliberately tiny device answers ENOSPC.

Where the second device comes from:

* **Darwin** -- a few-MiB ``hdiutil`` disk image, attached unprivileged under
  ``tmp_path`` and detached (and deleted) at teardown.  HFS+ for EXDEV and
  metadata, exFAT for the no-hard-link fallback, a 4 MiB HFS+ for ENOSPC.
* **Linux** (CI) -- ``/dev/shm`` when it is a tmpfs on a different device from
  ``tmp_path``.  It covers EXDEV and metadata only: an unprivileged process can
  mount neither a size-limited filesystem nor one without hard links, so the
  ENOSPC and no-hard-link tests SKIP there, each naming why.

Every skip carries its reason (``pytest -rs``, and the junit report), never a
silent pass.

Measured on Darwin 25.6 when this was written (phaze-x4kvv), and the reason the
metadata assertions are shaped as they are:

* ``os.rename`` and ``os.link`` from the data volume onto HFS+, MS-DOS and exFAT
  images all raise EXDEV.
* ``os.link`` WITHIN an exFAT or MS-DOS volume raises ENOTSUP (45), which on
  Darwin is not EOPNOTSUPP (102), so the fallback did not fire there until
  ``_LINK_UNSUPPORTED_ERRNOS`` gained it.
* ``copystat`` onto HFS+ keeps the mode exactly and truncates mtime to whole
  seconds; onto exFAT it silently leaves the mode at 0o700 and keeps mtime to
  10 ms.  Source mtimes here are whole, even seconds so every filesystem above
  represents them exactly; exFAT's mode is not asserted because the filesystem
  cannot hold one.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING
import uuid

import pytest

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
    from collections.abc import Iterator


_MTIME = 1_600_000_000  # whole and even: exact on HFS+, exFAT, MS-DOS and tmpfs
_HDIUTIL_TIMEOUT_SEC = 60


@contextlib.contextmanager
def _darwin_disk_image(base: Path, fs: str, size_mb: int) -> Iterator[Path]:
    """Attach a fresh ``size_mb`` MiB ``fs`` image under ``base``; always detach and delete it."""
    hdiutil = shutil.which("hdiutil")
    if hdiutil is None:
        pytest.skip("no second device: hdiutil not on PATH")
    base.mkdir(parents=True, exist_ok=True)
    image = base / f"{fs.lower().replace('+', 'plus').replace('-', '')}.dmg"
    mountpoint = base / "mnt"
    mountpoint.mkdir()
    argv_create = [hdiutil, "create", "-quiet", "-size", f"{size_mb}m", "-fs", fs, "-volname", "PHAZED1", "-layout", "NONE", str(image)]
    argv_attach = [hdiutil, "attach", "-quiet", "-nobrowse", "-noautoopen", "-mountpoint", str(mountpoint), str(image)]
    attached = False
    try:
        for argv in (argv_create, argv_attach):
            result = subprocess.run(  # noqa: S603 - resolved executable; argv is a test literal
                argv, capture_output=True, text=True, timeout=_HDIUTIL_TIMEOUT_SEC, check=False
            )
            if result.returncode != 0:
                pytest.skip(f"no second device: `hdiutil {argv[1]}` for a {fs} image failed: {result.stderr.strip()}")
        attached = True
        yield mountpoint
    finally:
        if attached:
            subprocess.run(  # noqa: S603 - resolved executable; argv is a test literal
                [hdiutil, "detach", "-quiet", "-force", str(mountpoint)], capture_output=True, timeout=_HDIUTIL_TIMEOUT_SEC, check=False
            )
        image.unlink(missing_ok=True)


@contextlib.contextmanager
def _linux_shm_dir(tmp_path: Path) -> Iterator[Path]:
    shm = Path("/dev/shm")  # noqa: S108 - a private mkdtemp under it, removed at teardown; the tmpfs IS the device under test
    if not shm.is_dir() or not os.access(shm, os.W_OK):
        pytest.skip("no second device: /dev/shm is absent or not writable")
    directory = Path(tempfile.mkdtemp(prefix="phaze-x4kvv-", dir=shm))
    try:
        if directory.stat().st_dev == tmp_path.stat().st_dev:
            pytest.skip("no second device: /dev/shm is on the same device as tmp_path")
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def other_device(tmp_path: Path) -> Iterator[Path]:
    """A writable directory on a different device from ``tmp_path``, with hard links and ample space."""
    if sys.platform == "darwin":
        with _darwin_disk_image(tmp_path / "device", "HFS+", 8) as mount:
            yield mount
    elif sys.platform.startswith("linux"):
        with _linux_shm_dir(tmp_path) as directory:
            yield directory
    else:
        pytest.skip(f"no second device: no unprivileged way to make one on {sys.platform}")


@pytest.fixture
def linkless_device(tmp_path: Path) -> Iterator[Path]:
    """A different device whose filesystem has no hard links (exFAT, the archive's own mount type)."""
    if sys.platform != "darwin":
        pytest.skip(f"no hard-link-less device: an unprivileged process cannot mount exFAT/vfat on {sys.platform}")
    with _darwin_disk_image(tmp_path / "device", "ExFAT", 8) as mount:
        yield mount


@pytest.fixture
def tiny_device(tmp_path: Path) -> Iterator[Path]:
    """A different device with only a few MiB free, so a larger copy hits ENOSPC."""
    if sys.platform != "darwin":
        pytest.skip(f"no size-limited device: an unprivileged process cannot mount a size-limited filesystem on {sys.platform}")
    with _darwin_disk_image(tmp_path / "device", "HFS+", 4) as mount:
        yield mount


def _source(tmp_path: Path, name: str, payload: bytes) -> tuple[Path, Path]:
    """Write a 0o640, fixed-mtime source under a scan root on ``tmp_path``'s device."""
    root = tmp_path / "archive"
    src = root / "incoming" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(payload)
    src.chmod(0o640)
    os.utime(src, (_MTIME, _MTIME))
    return root, src


def _request(root: Path, device: Path, src: Path, name: str) -> FilesystemMoveRequest:
    """Route the move onto ``device`` the way a scan root spanning a mount does."""
    (root / "other").symlink_to(device, target_is_directory=True)
    item = ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        source_path=str(src),
        proposed_path="other/sorted",
        proposed_filename=name,
        sha256_hash=hashlib.sha256(src.read_bytes()).hexdigest(),
    )
    return FilesystemMoveRequest(item=item, scan_roots=[str(root), str(device)])


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if COPY_TMP_SUFFIX in p.name or COMMIT_MARKER_SUFFIX in p.name)


def test_the_second_device_really_is_one(tmp_path: Path, other_device: Path) -> None:
    """The premise every other test here rests on: the kernel refuses a rename or link onto it with EXDEV."""
    _, src = _source(tmp_path, "track-01.mp3", b"premise")
    primitives = LocalFilesystemPrimitives()

    assert primitives.same_filesystem(src, other_device) is False
    for cross in (os.rename, os.link):
        with pytest.raises(OSError, match=r".") as excinfo:
            cross(src, other_device / "track-01.mp3")
        assert excinfo.value.errno == errno.EXDEV, cross.__name__
    assert src.read_bytes() == b"premise"


async def test_cross_device_move_publishes_bytes_mode_and_mtime(tmp_path: Path, other_device: Path) -> None:
    """The real engine, unpatched, moves across devices: exact bytes, mode and mtime; no staging residue."""
    payload = os.urandom(3 * 1024 * 1024 + 11)  # spans a partial chunk
    root, src = _source(tmp_path, "set-01.mkv", payload)
    request = _request(root, other_device, src, "set-01.mkv")

    result = await LocalExecutionFilesystemEngine().move(request, MoveStep())

    dest = other_device / "sorted" / "set-01.mkv"
    assert result.committed_now is True
    assert result.proposed == dest.resolve()
    assert not src.exists()
    assert dest.stat().st_dev != tmp_path.stat().st_dev
    assert dest.read_bytes() == payload
    assert dest.stat().st_mode == 0o100640
    assert dest.stat().st_mtime == _MTIME
    assert _leftovers(dest.parent) == []


async def test_cross_device_move_onto_a_filesystem_without_hard_links(tmp_path: Path, linkless_device: Path) -> None:
    """exFAT refuses the no-clobber link claim; the move must take the fallback and still publish."""
    probe = linkless_device / "probe"
    probe.write_bytes(b"x")
    assert LocalFilesystemPrimitives().claim_destination_by_link(probe, linkless_device / "probe-link") is False
    assert not (linkless_device / "probe-link").exists()
    probe.unlink()

    payload = os.urandom(2 * 1024 * 1024 + 3)
    root, src = _source(tmp_path, "track-02.m4a", payload)
    request = _request(root, linkless_device, src, "track-02.m4a")

    result = await LocalExecutionFilesystemEngine().move(request, MoveStep())

    dest = linkless_device / "sorted" / "track-02.m4a"
    assert result.committed_now is True
    assert not src.exists()
    assert dest.read_bytes() == payload
    assert dest.stat().st_mtime == _MTIME
    # No st_mode assertion: exFAT stores no permission bits and copystat's chmod silently
    # no-ops (measured 0o700 whatever the source) -- a property of the device, not the engine.
    assert _leftovers(dest.parent) == []


async def test_enospc_on_the_destination_device_keeps_the_source_and_publishes_nothing(tmp_path: Path, tiny_device: Path) -> None:
    """A copy that runs the destination device out of space fails at 'copy' with the source intact and no destination."""
    stat = os.statvfs(tiny_device)
    free = stat.f_bavail * stat.f_frsize
    payload = os.urandom(free + 1024 * 1024)  # a few MiB: cannot fit
    root, src = _source(tmp_path, "set-02.mkv", payload)
    request = _request(root, tiny_device, src, "set-02.mkv")
    step = MoveStep()

    with pytest.raises(OSError, match=r".") as excinfo:
        await LocalExecutionFilesystemEngine().move(request, step)

    assert excinfo.value.errno == errno.ENOSPC
    assert step.current == "copy"
    assert src.read_bytes() == payload
    dest_dir = tiny_device / "sorted"
    assert not (dest_dir / "set-02.mkv").exists()
    assert _leftovers(dest_dir) == []
