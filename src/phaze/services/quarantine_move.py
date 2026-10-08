"""The quarantine move: one approved junk companion into ``<root>/.phaze-quarantine/`` (phaze-lwuf6).

phaze's first archive mutator that removes a file from where the operator keeps it, so every check
runs on the AGENT, against the bytes and the path that actually get moved, and the move itself is a
single ``os.rename`` -- never a copy and an unlink. Operator decisions 3 ("Quarantine folder
(Recommended)") and 9 ("Hidden dir per scan root (Recommended)"), both 2026-10-07, recorded on epic
phaze-4x319: the file is moved, not deleted, to ``<owning root>/.phaze-quarantine/<its path relative to
that root>``, so a wrong approval is undone by moving it back.

Before the rename, :func:`quarantine_file` requires, and refuses with :class:`QuarantineRefused`
otherwise:

- **containment** of the exact path that is moved: the NFC/NFD twin is looked up first and the
  containment check runs on it (``resolve_contained_twin``, phaze-2tei9);
- **no symlink** anywhere below the scan root, the final component included (``lstat``), and a
  **regular file** -- never a directory, device or socket;
- an extension in ``INGESTIBLE_COMPANION_EXTENSIONS``, so media is refused by construction, and a
  source that is not already under the quarantine directory;
- the **approved size and SHA-256**, re-read through an ``O_NOFOLLOW`` descriptor;
- a destination that does not exist (**no clobber**), whose every directory below the root is a real
  directory and not a symlink, contained in the same root, and on the **same filesystem** as the
  source (``st_dev``), so the rename is atomic;
- a source unchanged since it was hashed: ``st_ino``, ``st_dev``, ``st_size`` and ``st_mtime_ns``
  are compared once after hashing on the open descriptor and again by ``lstat`` immediately before
  the rename.

The residual windows are named rather than hidden. Between the last ``lstat`` and ``rename(2)`` a
local process could still swap the source; the destination is ``lstat``-ed after the rename and a
different inode is reported as a failure (whatever was moved sits in quarantine, recoverable). And
the no-clobber check is a check-then-rename, because Python exposes no ``RENAME_NOREPLACE``: only
phaze writes under the quarantine directory, and the review table allows one live row per identity.

REPLAY. A SAQ retry after a rename whose report never arrived finds no source. That counts as done
only when the destination corroborates it -- a regular file, contained, with the approved size and
SHA-256 (:attr:`QuarantineOutcome.replayed`); otherwise it is refused as missing.

Stdlib-only apart from its stdlib-only siblings: the agent worker imports it, and the D-25 boundary
(``tests/shared/core/test_task_split.py``) bans ``phaze.database`` and SQLAlchemy there.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS, QUARANTINE_DIRNAME
from phaze.services.containment import resolve_contained_twin
from phaze.services.media_path_resolve import resolve_media_path


_HASH_CHUNK_SIZE = 65536


class QuarantineRefused(Exception):
    """Why the file is not in quarantine. Terminal: a retry would refuse again.

    Every refusal but one is raised before the rename, so nothing moved. The exception is the
    post-rename inode check, which reports that something OTHER than the hashed file was moved.
    """


@dataclass(frozen=True)
class QuarantineOutcome:
    """A file that is now in quarantine: where it is, and whether THIS attempt moved it."""

    source: Path
    """The resolved path the file was moved from."""
    destination: Path
    replayed: bool
    """True when an earlier attempt had already moved it (the source was gone, the destination corroborated it)."""


@dataclass(frozen=True)
class _Identity:
    """What a file looked like when it was checked; compared again before the rename."""

    ino: int
    dev: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, st: os.stat_result) -> _Identity:
        return cls(st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns)


def _contained_without_symlinks(candidate: str, scan_roots: list[str]) -> tuple[Path, Path]:
    """``(resolved, owning_root)`` for the on-disk twin of ``candidate``, refusing an escape or any symlink below the root.

    ``resolve_contained_twin`` follows symlinks before comparing, so a symlink that stays inside the
    roots would pass it and the TARGET would be moved. The twin's own path, lexically below a
    configured root, must therefore equal the resolved path below the resolved root: any symlinked
    component below the root -- directory or file -- makes the two differ. A root that is itself
    reached through a symlink is fine, because both sides are taken relative to it.
    """
    twin = os.path.abspath(resolve_media_path(candidate))  # noqa: PTH100 -- lexical on purpose: Path.absolute() keeps '..'
    try:
        resolved, owning_root = resolve_contained_twin(candidate, scan_roots)
    except ValueError as exc:
        raise QuarantineRefused(f"outside every scan root: {exc}") from exc
    relative = resolved.relative_to(owning_root)
    for root in scan_roots:
        lexical_root = os.path.abspath(root)  # noqa: PTH100 -- lexical on purpose
        if Path(lexical_root).resolve() != owning_root:
            continue
        try:
            lexical_relative = Path(twin).relative_to(lexical_root)
        except ValueError:
            continue
        if lexical_relative == relative:
            return resolved, owning_root
    raise QuarantineRefused(f"{twin} reaches {resolved} through a symlink; only a file at its real path is moved")


def _check_source_name(resolved: Path, owning_root: Path) -> Path:
    """The source's path relative to its root, after the extension and quarantine-directory refusals."""
    relative = resolved.relative_to(owning_root)
    if not relative.parts:
        raise QuarantineRefused("the source is a scan root itself")
    if QUARANTINE_DIRNAME in relative.parts:
        raise QuarantineRefused(f"{resolved} is already under {QUARANTINE_DIRNAME}")
    if resolved.suffix.lower() not in INGESTIBLE_COMPANION_EXTENSIONS:
        raise QuarantineRefused(f"{resolved.name!r} is not a companion file type; only companions are quarantined")
    return relative


def _hash_regular_file(path: Path, sha256: str, size: int) -> _Identity:
    """Re-read ``path`` without following a symlink; refuse unless it is a regular file with the approved size and SHA-256.

    Returns the descriptor's identity, read before hashing and confirmed unchanged after it.
    """
    try:
        # O_NONBLOCK: opening a FIFO for reading would otherwise wait for a writer; it is a no-op on a regular file.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise QuarantineRefused(f"cannot open {path} without following a symlink: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise QuarantineRefused(f"{path} is not a regular file")
        if before.st_size != size:
            raise QuarantineRefused(f"{path} is {before.st_size} bytes; the approved file was {size}")
        digest = hashlib.sha256()
        while chunk := os.read(fd, _HASH_CHUNK_SIZE):
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _Identity.of(after) != _Identity.of(before):
        raise QuarantineRefused(f"{path} changed while it was being hashed")
    if digest.hexdigest() != sha256:
        raise QuarantineRefused(f"{path} has SHA-256 {digest.hexdigest()}; the approved file was {sha256}")
    return _Identity.of(before)


def _make_destination_dirs(owning_root: Path, relative: Path) -> Path:
    """Create ``<root>/.phaze-quarantine/<relative parent>`` one level at a time; refuse a symlink or non-directory at any level.

    Returns the destination path. Each level is created with ``mkdir`` (never ``makedirs``, which
    follows a symlinked level) and then ``lstat``-ed, so a level planted as a symlink, before or
    during the walk, is refused rather than followed. The resolved parent is finally required to equal
    its lexical form and to sit below the root.
    """
    level = owning_root
    for part in (QUARANTINE_DIRNAME, *relative.parent.parts):
        level = level / part
        try:
            level.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise QuarantineRefused(f"cannot create {level}: {exc}") from exc
        mode = level.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise QuarantineRefused(f"{level} is not a real directory")
    parent = level.resolve()
    if parent != level or not parent.is_relative_to(owning_root):
        raise QuarantineRefused(f"the destination directory {level} resolves to {parent}")
    return level / relative.name


def _corroborated_replay(candidate: str, scan_roots: list[str], sha256: str, size: int) -> QuarantineOutcome:
    """The source is gone: done only if the destination already holds the approved bytes, else refused."""
    try:
        missing, owning_root = resolve_contained_twin(candidate, scan_roots)
    except ValueError as exc:
        raise QuarantineRefused(f"outside every scan root: {exc}") from exc
    relative = _check_source_name(missing, owning_root)
    expected = owning_root / QUARANTINE_DIRNAME / relative
    try:
        destination, _root = _contained_without_symlinks(str(expected), [str(owning_root)])
        _hash_regular_file(destination, sha256, size)
    except QuarantineRefused as exc:
        raise QuarantineRefused(f"the source is missing and the quarantine copy does not corroborate a move: {exc}") from exc
    return QuarantineOutcome(source=missing, destination=destination, replayed=True)


def _prepare(candidate: str, scan_roots: list[str], sha256: str, size: int) -> tuple[Path, Path, _Identity]:
    """Every pre-rename check, in order; returns ``(source, destination, hashed identity)``."""
    resolved, owning_root = _contained_without_symlinks(candidate, scan_roots)
    relative = _check_source_name(resolved, owning_root)
    hashed = _hash_regular_file(resolved, sha256, size)

    destination = _make_destination_dirs(owning_root, relative)
    if os.path.lexists(destination):
        raise QuarantineRefused(f"{destination} already exists; nothing is overwritten")
    if destination.parent.lstat().st_dev != hashed.dev:
        raise QuarantineRefused(f"{destination.parent} is on another filesystem; the move must be a single rename")

    # TOCTOU: the path must still name the inode that was hashed, unchanged, right before the rename.
    current = resolved.lstat()
    if stat.S_ISLNK(current.st_mode) or _Identity.of(current) != hashed:
        raise QuarantineRefused(f"{resolved} changed after it was hashed")
    return resolved, destination, hashed


def quarantine_file(candidate: str, scan_roots: list[str], *, sha256: str, size: int) -> QuarantineOutcome:
    """Move ``candidate`` into its root's quarantine directory, or raise :class:`QuarantineRefused`.

    See the module docstring for every check and its order. Blocking; the task runs it in a thread.
    An ``OSError`` before the rename is a refusal (nothing moved). One after it -- the destination's
    own ``lstat`` -- propagates, so the job is retried and the retry's replay path reports the move.
    """
    try:
        try:
            os.lstat(resolve_media_path(candidate))
        except FileNotFoundError:
            return _corroborated_replay(candidate, scan_roots, sha256, size)
        source, destination, hashed = _prepare(candidate, scan_roots, sha256, size)
        source.rename(destination)
    except OSError as exc:
        raise QuarantineRefused(f"not moved: {exc}") from exc

    moved = destination.lstat()
    if stat.S_ISLNK(moved.st_mode) or (moved.st_ino, moved.st_dev) != (hashed.ino, hashed.dev):
        raise QuarantineRefused(f"{destination} is not the file that was hashed; the source was swapped during the move")
    return QuarantineOutcome(source=source, destination=destination, replayed=False)
