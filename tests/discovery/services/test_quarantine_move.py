"""The quarantine move on a REAL filesystem: one successful move and every refusal (phaze-lwuf6).

Every case builds real files, directories and symlinks under ``tmp_path`` and calls the function the
agent task calls. A refusal is asserted by what is left on disk -- the source still in place, nothing
under the quarantine directory or outside the root -- not only by the exception.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

import pytest

from phaze.constants import QUARANTINE_DIRNAME
from phaze.services import quarantine_move
from phaze.services.quarantine_move import QuarantineOutcome, QuarantineRefused, quarantine_file


if TYPE_CHECKING:
    from pathlib import Path


_JUNK = b"Downloaded from www.example-release-site.test\r\n"


def _put(path: Path, payload: bytes = _JUNK) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _sha(payload: bytes = _JUNK) -> str:
    return hashlib.sha256(payload).hexdigest()


def _move(path: Path, root: Path, *, payload: bytes = _JUNK) -> QuarantineOutcome:
    return quarantine_file(str(path), [str(root)], sha256=_sha(payload), size=len(payload))


def _quarantined(root: Path) -> list[str]:
    """Every file under the root's quarantine directory, relative to it."""
    base = root / QUARANTINE_DIRNAME
    return sorted(str(path.relative_to(base)) for path in base.rglob("*") if not path.is_dir()) if base.exists() else []


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "music"
    path.mkdir()
    return path


def test_an_approved_file_moves_to_the_mirrored_quarantine_path(root: Path) -> None:
    source = _put(root / "Example Artist" / "2024 - Live" / "site.nfo")
    inode = source.stat().st_ino

    outcome = _move(source, root)

    destination = root / QUARANTINE_DIRNAME / "Example Artist" / "2024 - Live" / "site.nfo"
    assert outcome == QuarantineOutcome(source=source, destination=destination, replayed=False)
    assert not source.exists()
    assert destination.read_bytes() == _JUNK
    assert destination.stat().st_ino == inode  # one rename: the same inode, never a copy
    assert (root / "Example Artist" / "2024 - Live").is_dir()  # the folder it left stays


def test_a_file_at_the_root_moves_to_the_quarantine_directory_itself(root: Path) -> None:
    outcome = _move(_put(root / "empty.txt", b""), root, payload=b"")

    assert outcome.destination == root / QUARANTINE_DIRNAME / "empty.txt"
    assert _quarantined(root) == ["empty.txt"]


def test_a_replay_after_the_move_is_corroborated_by_the_quarantine_copy(root: Path) -> None:
    source = _put(root / "rel" / "site.nfo")
    first = _move(source, root)

    replay = _move(source, root)

    assert replay == QuarantineOutcome(source=source, destination=first.destination, replayed=True)
    assert _quarantined(root) == ["rel/site.nfo"]


def test_a_missing_source_with_no_quarantine_copy_is_refused(root: Path) -> None:
    with pytest.raises(QuarantineRefused, match="does not corroborate"):
        _move(root / "rel" / "gone.nfo", root)
    assert not (root / QUARANTINE_DIRNAME).exists()


def test_a_missing_source_whose_quarantine_copy_has_other_bytes_is_refused(root: Path) -> None:
    _put(root / QUARANTINE_DIRNAME / "rel" / "site.nfo", b"other bytes, same name\r\n")

    with pytest.raises(QuarantineRefused, match="does not corroborate"):
        _move(root / "rel" / "site.nfo", root)


def test_a_missing_source_whose_quarantine_copy_is_a_symlink_is_refused(root: Path, tmp_path: Path) -> None:
    real = _put(tmp_path / "elsewhere" / "site.nfo")
    (root / QUARANTINE_DIRNAME / "rel").mkdir(parents=True)
    (root / QUARANTINE_DIRNAME / "rel" / "site.nfo").symlink_to(real)

    with pytest.raises(QuarantineRefused, match="does not corroborate"):
        _move(root / "rel" / "site.nfo", root)


def test_a_path_outside_every_scan_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = _put(tmp_path / "elsewhere" / "site.nfo")

    with pytest.raises(QuarantineRefused, match="outside every scan root"):
        _move(outside, root)
    assert outside.read_bytes() == _JUNK
    assert not (root / QUARANTINE_DIRNAME).exists()


def test_a_traversal_out_of_the_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = _put(tmp_path / "elsewhere" / "site.nfo")

    with pytest.raises(QuarantineRefused, match="outside every scan root"):
        quarantine_file(f"{root}/../elsewhere/site.nfo", [str(root)], sha256=_sha(), size=len(_JUNK))
    assert outside.exists()


def test_no_configured_scan_root_moves_nothing(root: Path) -> None:
    source = _put(root / "site.nfo")

    with pytest.raises(QuarantineRefused, match="outside every scan root"):
        quarantine_file(str(source), [], sha256=_sha(), size=len(_JUNK))
    assert source.exists()


def test_a_symlink_is_refused_and_its_target_is_untouched(root: Path) -> None:
    target = _put(root / "real" / "site.nfo")
    link = root / "rel" / "site.nfo"
    link.parent.mkdir()
    link.symlink_to(target)

    with pytest.raises(QuarantineRefused, match="through a symlink"):
        _move(link, root)
    assert link.is_symlink()
    assert target.read_bytes() == _JUNK
    assert not (root / QUARANTINE_DIRNAME).exists()


def test_a_symlink_escaping_the_root_is_refused(root: Path, tmp_path: Path) -> None:
    target = _put(tmp_path / "elsewhere" / "site.nfo")
    link = root / "site.nfo"
    link.symlink_to(target)

    with pytest.raises(QuarantineRefused, match="outside every scan root"):
        _move(link, root)
    assert target.exists()


def test_a_file_reached_through_a_symlinked_directory_is_refused(root: Path) -> None:
    target = _put(root / "real" / "site.nfo")
    (root / "alias").symlink_to(root / "real", target_is_directory=True)

    with pytest.raises(QuarantineRefused, match="through a symlink"):
        _move(root / "alias" / "site.nfo", root)
    assert target.exists()
    assert not (root / QUARANTINE_DIRNAME).exists()


def test_a_root_configured_through_a_symlink_still_moves(tmp_path: Path) -> None:
    """Only symlinks BELOW the root are refused: the root itself may be reached through one."""
    real_root = tmp_path / "volume"
    source = _put(real_root / "rel" / "site.nfo")
    alias_root = tmp_path / "music-alias"
    alias_root.symlink_to(real_root, target_is_directory=True)

    outcome = quarantine_file(str(alias_root / "rel" / "site.nfo"), [str(alias_root)], sha256=_sha(), size=len(_JUNK))

    assert outcome.destination == real_root.resolve() / QUARANTINE_DIRNAME / "rel" / "site.nfo"
    assert not source.exists()


def test_a_directory_is_refused(root: Path) -> None:
    directory = root / "rel" / "folder.nfo"
    directory.mkdir(parents=True)

    with pytest.raises(QuarantineRefused, match="not a regular file"):
        _move(directory, root)
    assert directory.is_dir()
    assert not (root / QUARANTINE_DIRNAME).exists()


def test_a_fifo_is_refused_without_blocking(root: Path) -> None:
    fifo = root / "rel" / "pipe.nfo"
    fifo.parent.mkdir()
    os.mkfifo(fifo)

    with pytest.raises(QuarantineRefused):
        quarantine_file(str(fifo), [str(root)], sha256=_sha(), size=0)
    assert fifo.exists()


@pytest.mark.parametrize("name", ["set.mp3", "set.mkv", "cover.jpg", "notes"])
def test_a_non_companion_extension_is_refused(root: Path, name: str) -> None:
    source = _put(root / "rel" / name)

    with pytest.raises(QuarantineRefused, match="not a companion file type"):
        _move(source, root)
    assert source.exists()


def test_a_file_already_in_quarantine_is_refused(root: Path) -> None:
    source = _put(root / QUARANTINE_DIRNAME / "rel" / "site.nfo")

    with pytest.raises(QuarantineRefused, match=f"already under {QUARANTINE_DIRNAME}"):
        _move(source, root)
    assert source.exists()


def test_a_size_other_than_the_approved_one_is_refused(root: Path) -> None:
    source = _put(root / "site.nfo")

    with pytest.raises(QuarantineRefused, match="the approved file was"):
        quarantine_file(str(source), [str(root)], sha256=_sha(), size=len(_JUNK) + 1)
    assert source.exists()


def test_bytes_other_than_the_approved_ones_are_refused(root: Path) -> None:
    """Same size, different content: only the hash can tell."""
    changed = bytes(reversed(_JUNK))
    source = _put(root / "site.nfo", changed)

    with pytest.raises(QuarantineRefused, match="SHA-256"):
        _move(source, root)
    assert source.read_bytes() == changed
    assert not (root / QUARANTINE_DIRNAME).exists()


def test_an_existing_destination_is_never_overwritten(root: Path) -> None:
    source = _put(root / "rel" / "site.nfo")
    existing = _put(root / QUARANTINE_DIRNAME / "rel" / "site.nfo", b"an earlier quarantined copy\r\n")

    with pytest.raises(QuarantineRefused, match="already exists"):
        _move(source, root)
    assert source.read_bytes() == _JUNK
    assert existing.read_bytes() == b"an earlier quarantined copy\r\n"


def test_a_dangling_symlink_at_the_destination_is_never_replaced(root: Path, tmp_path: Path) -> None:
    source = _put(root / "rel" / "site.nfo")
    (root / QUARANTINE_DIRNAME / "rel").mkdir(parents=True)
    (root / QUARANTINE_DIRNAME / "rel" / "site.nfo").symlink_to(tmp_path / "nowhere")

    with pytest.raises(QuarantineRefused, match="already exists"):
        _move(source, root)
    assert source.exists()


def test_a_symlinked_quarantine_directory_is_refused(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / QUARANTINE_DIRNAME).symlink_to(elsewhere, target_is_directory=True)
    source = _put(root / "rel" / "site.nfo")

    with pytest.raises(QuarantineRefused, match="not a real directory"):
        _move(source, root)
    assert source.exists()
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_directory_inside_the_quarantine_path_is_refused(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / QUARANTINE_DIRNAME).mkdir()
    (root / QUARANTINE_DIRNAME / "rel").symlink_to(elsewhere, target_is_directory=True)
    source = _put(root / "rel" / "deep" / "site.nfo")

    with pytest.raises(QuarantineRefused, match="not a real directory"):
        _move(source, root)
    assert source.exists()
    assert list(elsewhere.iterdir()) == []


def test_a_file_in_place_of_a_quarantine_directory_is_refused(root: Path) -> None:
    _put(root / QUARANTINE_DIRNAME / "rel", b"a file, not a directory")
    source = _put(root / "rel" / "site.nfo")

    with pytest.raises(QuarantineRefused, match="not a real directory"):
        _move(source, root)
    assert source.exists()


def test_a_destination_on_another_filesystem_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tmp_path is one filesystem, so the other device is reported by the destination directory's lstat."""
    source = _put(root / "rel" / "site.nfo")
    destination_dir = root / QUARANTINE_DIRNAME / "rel"
    real_lstat = os.lstat

    def _lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        result = real_lstat(path)
        if os.fspath(path) == str(destination_dir):
            values = list(result)
            values[2] = result.st_dev + 1  # st_dev
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "lstat", _lstat)

    with pytest.raises(QuarantineRefused, match="another filesystem"):
        _move(source, root)
    assert source.exists()


def test_a_source_changed_after_hashing_is_not_moved(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TOCTOU: the bytes change between the hash and the rename (here, while the destination is prepared)."""
    source = _put(root / "rel" / "site.nfo")
    real = quarantine_move._make_destination_dirs

    def _then_modify(owning_root: Path, relative: Path) -> Path:
        destination = real(owning_root, relative)
        with source.open("ab") as handle:
            handle.write(b"appended after the hash")
        return destination

    monkeypatch.setattr(quarantine_move, "_make_destination_dirs", _then_modify)

    with pytest.raises(QuarantineRefused, match="changed after it was hashed"):
        _move(source, root)
    assert source.exists()
    assert _quarantined(root) == []


def test_a_source_replaced_after_hashing_is_not_moved(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same bytes, same size, but a different inode: the path no longer names the file that was hashed."""
    source = _put(root / "rel" / "site.nfo")
    real = quarantine_move._make_destination_dirs

    def _then_replace(owning_root: Path, relative: Path) -> Path:
        destination = real(owning_root, relative)
        replacement = _put(root / "rel" / "replacement.tmp")
        replacement.replace(source)
        return destination

    monkeypatch.setattr(quarantine_move, "_make_destination_dirs", _then_replace)

    with pytest.raises(QuarantineRefused, match="changed after it was hashed"):
        _move(source, root)
    assert source.exists()
    assert _quarantined(root) == []


def test_a_source_swapped_into_place_during_the_rename_is_reported(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last window: after the final lstat. The moved inode is checked and the swap reported, not hidden."""
    source = _put(root / "rel" / "site.nfo")
    real = quarantine_move._prepare

    def _then_swap(candidate: str, scan_roots: list[str], sha256: str, size: int) -> tuple[Path, Path, object]:
        prepared = real(candidate, scan_roots, sha256, size)
        _put(root / "rel" / "swap.tmp").replace(source)
        return prepared

    monkeypatch.setattr(quarantine_move, "_prepare", _then_swap)

    with pytest.raises(QuarantineRefused, match="not the file that was hashed"):
        _move(source, root)


def test_a_scan_root_itself_is_refused(root: Path) -> None:
    with pytest.raises(QuarantineRefused, match="scan root itself"):
        _move(root, root)
    assert root.is_dir()
    assert not (root / QUARANTINE_DIRNAME).exists()


def test_the_owning_root_is_found_among_several_and_aliased_roots(root: Path, tmp_path: Path) -> None:
    """The file's own root is picked even after an unrelated root and an alias of the same root that does not spell its path."""
    other = tmp_path / "video"
    other.mkdir()
    alias = tmp_path / "music-alias"
    alias.symlink_to(root, target_is_directory=True)
    source = _put(root / "rel" / "site.nfo")

    outcome = quarantine_file(str(source), [str(other), str(alias), str(root)], sha256=_sha(), size=len(_JUNK))

    assert outcome.destination == root.resolve() / QUARANTINE_DIRNAME / "rel" / "site.nfo"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_an_unreadable_source_is_refused(root: Path) -> None:
    source = _put(root / "rel" / "site.nfo")
    source.chmod(0o000)
    try:
        with pytest.raises(QuarantineRefused, match="cannot open"):
            _move(source, root)
        assert source.exists()
    finally:
        source.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_quarantine_directory_that_cannot_be_created_is_refused(root: Path) -> None:
    source = _put(root / "rel" / "site.nfo")
    root.chmod(0o555)
    try:
        with pytest.raises(QuarantineRefused, match="cannot create"):
            _move(source, root)
        assert source.exists()
    finally:
        root.chmod(0o755)
