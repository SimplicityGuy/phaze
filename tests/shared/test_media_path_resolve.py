"""DB-free unit tests for `resolve_media_path` (phaze-9pg11).

Bucket: ``shared``. Stdlib-only module (mirrors ``test_containment.py``), no Postgres needed.

Covers the NFC/NFD on-disk mismatch this function exists to paper over: an agent task is handed a
STORED path -- always NFC-normalized, per ``routers/agent_files.py::upsert_files`` and the agent's
own ``scan.py`` / ``agent_watcher/poster.py`` -- but the real directory entry (the file, or ANY
ancestor directory, e.g. an artist folder minted from a yt-dlp uploader name) can be
NFD-decomposed (routine for names that started life on macOS, or titles minted by some web
sources/yt-dlp). Linux filenames are byte-exact, so a plain ``open()``/``stat()`` on the stored
path never matches such an entry; this resolver is the shared agent-side fallback every
filesystem-touching task routes through first.

Why the mismatch tests force `Path.exists` to a byte-exact reimplementation: the bug this module
fixes is Linux-specific -- ext4 (and friends) compare filenames byte-exact. macOS's own
filesystems (HFS+/APFS) are Unicode-normalization-INSENSITIVE at the syscall level, so on a macOS
dev host `Path("…NFC…").exists()` spuriously returns True even when the real directory entry is
NFD-decomposed (the OS treats the two byte sequences as the same name) -- and, worse, writing both
an NFC- and an NFD-named entry into the same real directory collides into a single entry instead
of two. Writing a genuinely OS-portable regression test therefore means reimplementing `exists`
as a byte-exact (Linux-style) name comparison against whatever is really in the parent directory,
rather than trusting the host OS's own (insensitive, on macOS) answer. The resolution logic under
test (find the longest existing ancestor, walk down matching on NFC) is exercised for real either
way, against real `Path.iterdir()` results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import unicodedata

from phaze.services import media_path_resolve
from phaze.services.media_path_resolve import resolve_media_path
from tests._media_path_fakes import byte_exact_exists


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _force_byte_exact_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_path_resolve.Path, "exists", byte_exact_exists)


def test_returns_path_unchanged_when_it_exists_byte_exact(tmp_path) -> None:
    target = tmp_path / "track.mp3"
    target.write_text("data", encoding="utf-8")

    assert resolve_media_path(str(target)) == str(target)


def test_resolves_nfc_reported_path_to_nfd_on_disk_entry(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The production bug: the stored path is NFC, the directory entry is NFD."""
    nfd_name = unicodedata.normalize("NFD", "Hör.mp3")
    nfc_name = unicodedata.normalize("NFC", "Hör.mp3")
    assert nfd_name != nfc_name, "fixture must actually exercise two distinct byte forms"

    on_disk = tmp_path / nfd_name
    on_disk.write_bytes(b"id3-ish bytes")
    reported_nfc_path = str(tmp_path / nfc_name)

    _force_byte_exact_filesystem(monkeypatch)
    resolved = resolve_media_path(reported_nfc_path)

    assert resolved == str(on_disk)


def test_resolves_nfd_reported_path_to_nfc_on_disk_entry(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Symmetric case: an NFC-decomposed directory entry, an NFD-reported path."""
    nfd_name = unicodedata.normalize("NFD", "Hör.mp3")
    nfc_name = unicodedata.normalize("NFC", "Hör.mp3")

    on_disk = tmp_path / nfc_name
    on_disk.write_bytes(b"id3-ish bytes")
    reported_nfd_path = str(tmp_path / nfd_name)

    _force_byte_exact_filesystem(monkeypatch)
    resolved = resolve_media_path(reported_nfd_path)

    assert resolved == str(on_disk)


def test_resolves_pre_existing_nfc_only_row_with_no_data_migration(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row written BEFORE this fix stored only the NFC path -- resolution must still work,
    because it matches against whatever is really on disk, not against any stored provenance.
    """
    nfd_name = unicodedata.normalize("NFD", "Hör Berlin.m4a")
    on_disk = tmp_path / nfd_name
    on_disk.write_bytes(b"m4a-ish bytes")

    pre_existing_stored_path = str(tmp_path / unicodedata.normalize("NFC", "Hör Berlin.m4a"))

    _force_byte_exact_filesystem(monkeypatch)
    assert resolve_media_path(pre_existing_stored_path) == str(on_disk)


def test_resolves_nfd_directory_component_with_byte_exact_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mismatch can live in an ANCESTOR directory, not just the leaf -- e.g. an artist folder
    minted from a yt-dlp uploader name. The file itself is byte-exact once its NFD parent is found.
    """
    nfd_dir_name = unicodedata.normalize("NFD", "Hör")
    nfc_dir_name = unicodedata.normalize("NFC", "Hör")
    assert nfd_dir_name != nfc_dir_name

    on_disk_dir = tmp_path / nfd_dir_name
    on_disk_dir.mkdir()
    on_disk_file = on_disk_dir / "track.mp3"
    on_disk_file.write_bytes(b"id3-ish bytes")

    reported_path = str(tmp_path / nfc_dir_name / "track.mp3")

    _force_byte_exact_filesystem(monkeypatch)
    assert resolve_media_path(reported_path) == str(on_disk_file)


def test_resolves_nfd_directory_and_nfd_file_together(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both the directory AND the file are NFD-decomposed on disk; both must resolve in one pass."""
    nfd_dir_name = unicodedata.normalize("NFD", "Hör")
    nfc_dir_name = unicodedata.normalize("NFC", "Hör")
    nfd_file_name = unicodedata.normalize("NFD", "Hör Berlin.m4a")
    nfc_file_name = unicodedata.normalize("NFC", "Hör Berlin.m4a")

    on_disk_dir = tmp_path / nfd_dir_name
    on_disk_dir.mkdir()
    on_disk_file = on_disk_dir / nfd_file_name
    on_disk_file.write_bytes(b"m4a-ish bytes")

    reported_path = str(tmp_path / nfc_dir_name / nfc_file_name)

    _force_byte_exact_filesystem(monkeypatch)
    assert resolve_media_path(reported_path) == str(on_disk_file)


def test_ambiguous_siblings_prefer_byte_exact_match(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A directory that genuinely contains BOTH an NFC entry and an NFD entry (e.g. from a
    partial prior workaround) must resolve a byte-exact request to itself -- never redirected to
    its NFC-equal sibling. This can't be reproduced with real files on macOS (writing both
    collides into one entry there; see module docstring), so both `Path.exists` and
    `Path.iterdir` are simulated directly.
    """
    nfc_name = unicodedata.normalize("NFC", "Hör.mp3")
    nfd_name = unicodedata.normalize("NFD", "Hör.mp3")
    assert nfc_name != nfd_name

    nfc_path = tmp_path / nfc_name
    nfd_path = tmp_path / nfd_name
    on_disk = {tmp_path, nfc_path, nfd_path}

    def fake_exists(self: Path) -> bool:
        return self in on_disk

    def fake_iterdir(self: Path):
        raise AssertionError("byte-exact fast path should short-circuit before any directory listing is needed")

    monkeypatch.setattr(media_path_resolve.Path, "exists", fake_exists)
    monkeypatch.setattr(media_path_resolve.Path, "iterdir", fake_iterdir)

    assert resolve_media_path(str(nfc_path)) == str(nfc_path)
    assert resolve_media_path(str(nfd_path)) == str(nfd_path)


def test_returns_path_unchanged_when_no_entry_matches(tmp_path) -> None:
    """A genuinely missing file (not a normalization mismatch) is left for the caller to fail on."""
    missing = tmp_path / "never-existed.mp3"

    assert resolve_media_path(str(missing)) == str(missing)


def test_returns_path_unchanged_when_parent_directory_is_unreadable(tmp_path) -> None:
    """A missing/unmounted parent directory must not raise -- callers keep their own OSError path."""
    missing = tmp_path / "no-such-dir" / "track.mp3"

    assert resolve_media_path(str(missing)) == str(missing)


def test_returns_path_unchanged_when_ancestor_directory_cannot_be_resolved(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ancestor directory component that doesn't NFC-match anything on disk is left alone --
    the mismatch isn't just a normalization issue, so callers keep their own missing-file path.
    """
    missing = tmp_path / unicodedata.normalize("NFC", "Hör") / "track.mp3"

    _force_byte_exact_filesystem(monkeypatch)
    assert resolve_media_path(str(missing)) == str(missing)
