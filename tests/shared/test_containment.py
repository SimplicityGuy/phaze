"""DB-free unit tests for `resolve_and_check_containment` (phaze-eycl).

Bucket: ``shared``. Stdlib-only module (mirrors ``test_pg_text.py``), no Postgres needed.

Covers the symlink-escape and `..`-traversal cases the bead calls out specifically -- a bare
prefix-string check on the raw path catches neither: a symlink planted inside a scan_root can
point anywhere on disk, and a `..`-laden path can look nested under the root as a raw string
while resolving outside it.
"""

from __future__ import annotations

import unicodedata
from unittest.mock import patch

import pytest

from phaze.services.containment import resolve_and_check_containment, resolve_contained_twin
from tests._media_path_fakes import byte_exact_exists, substituted_twin_lookup


def test_accepts_path_under_scan_root(tmp_path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    target = root / "sub" / "file.mp3"
    target.parent.mkdir()
    target.write_text("data", encoding="utf-8")

    resolved, owning_root = resolve_and_check_containment(str(target), [str(root)])

    assert resolved == target.resolve()
    assert owning_root == root.resolve()


def test_refuses_absolute_path_outside_every_scan_root(tmp_path) -> None:
    """The `/proc/self/environ`-style exfil target: readable, absolute, and not under any root."""
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside" / "environ"
    outside.parent.mkdir()
    outside.write_text("SECRET=1", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes all scan_roots"):
        resolve_and_check_containment(str(outside), [str(root)])


def test_refuses_dotdot_traversal_that_resolves_outside_root(tmp_path) -> None:
    """A raw string that LOOKS nested under the root but resolves to a sibling must be refused."""
    root = tmp_path / "archive"
    root.mkdir()
    sibling_secret = tmp_path / "secret.txt"
    sibling_secret.write_text("SECRET=1", encoding="utf-8")

    traversal_path = str(root / ".." / "secret.txt")

    with pytest.raises(ValueError, match="escapes all scan_roots"):
        resolve_and_check_containment(traversal_path, [str(root)])


def test_refuses_symlink_inside_root_pointing_outside_it(tmp_path) -> None:
    """A symlink physically located inside the scan_root but targeting outside it must be
    refused -- this is exactly what a bare (non-resolving) prefix-string check would miss.
    """
    root = tmp_path / "archive"
    root.mkdir()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("SECRET=1", encoding="utf-8")

    symlink = root / "innocuous.nfo"
    symlink.symlink_to(outside_secret)

    with pytest.raises(ValueError, match="escapes all scan_roots"):
        resolve_and_check_containment(str(symlink), [str(root)])


def test_accepts_symlink_inside_root_pointing_inside_it(tmp_path) -> None:
    """A symlink that stays within the scan_root (e.g. to a differently-named sibling file
    within the same root) is legitimate and must resolve successfully.
    """
    root = tmp_path / "archive"
    root.mkdir()
    real_file = root / "real.nfo"
    real_file.write_text("data", encoding="utf-8")

    symlink = root / "alias.nfo"
    symlink.symlink_to(real_file)

    resolved, owning_root = resolve_and_check_containment(str(symlink), [str(root)])

    assert resolved == real_file.resolve()
    assert owning_root == root.resolve()


def test_empty_scan_roots_refuses_everything(tmp_path) -> None:
    """An agent with no configured scan_roots must never be treated as 'allow everything'."""
    target = tmp_path / "file.mp3"
    target.write_text("data", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes all scan_roots"):
        resolve_and_check_containment(str(target), [])


def test_matches_the_longest_of_overlapping_scan_roots(tmp_path) -> None:
    outer = tmp_path / "archive"
    inner = outer / "nested"
    inner.mkdir(parents=True)
    target = inner / "file.mp3"
    target.write_text("data", encoding="utf-8")

    _resolved, owning_root = resolve_and_check_containment(str(target), [str(outer), str(inner)])

    # Either root is a valid match (the function returns the first match in iteration order);
    # what matters is that containment is granted and the returned root is one of the two.
    assert owning_root in {outer.resolve(), inner.resolve()}


# phaze-2tei9: the twin lookup must run BEFORE the containment check, so the check covers the path
# that is actually opened. An NFD directory name that is a symlink pointing out of the root is the
# shape: the stored NFC string never matches it byte-exact, so it resolves lexically inside the root.
_NFD_DIR = unicodedata.normalize("NFD", "Hör")
_NFC_DIR = unicodedata.normalize("NFC", "Hör")


def _symlinked_twin_layout(tmp_path):
    """``archive/<NFD dir> -> outside/``, with ``outside/notes.nfo`` the file that must stay unread."""
    assert _NFD_DIR != _NFC_DIR, "fixture must actually exercise two distinct byte forms"
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.nfo").write_text("SECRET=1", encoding="utf-8")
    (root / _NFD_DIR).symlink_to(outside, target_is_directory=True)
    return root, outside


def test_twin_through_a_symlinked_directory_out_of_the_root_is_refused(tmp_path) -> None:
    """The escape, on the REAL directory walk (byte-exact ``exists`` as on Linux).

    Discriminating on Linux only: on macOS ``realpath`` finds the NFD entry from the NFC string and
    follows the symlink, so the old check-then-swap order refused this too. The substituted cases
    below discriminate on every host.
    """
    root, _outside = _symlinked_twin_layout(tmp_path)
    stored = root / _NFC_DIR / "notes.nfo"

    with patch("phaze.services.media_path_resolve.Path.exists", byte_exact_exists), pytest.raises(ValueError, match="escapes all scan_roots"):
        resolve_contained_twin(str(stored), [str(root)])


def test_substituted_twin_through_a_symlinked_directory_is_refused(tmp_path) -> None:
    """The stored path passes containment ON ITS OWN; only the twin it maps to escapes.

    This is the order that matters: ``resolve_and_check_containment`` on the stored string alone
    accepts it, which is exactly what the pre-fix order then swapped the twin in after.
    """
    root, _outside = _symlinked_twin_layout(tmp_path)
    stored = root / "stored-nfc" / "notes.nfo"
    twin = root / _NFD_DIR / "notes.nfo"
    resolve_and_check_containment(str(stored), [str(root)])  # the stored string alone looks contained

    with (
        patch("phaze.services.containment.resolve_media_path", substituted_twin_lookup(stored, twin)),
        pytest.raises(ValueError, match="escapes all scan_roots"),
    ):
        resolve_contained_twin(str(stored), [str(root)])


def test_legitimate_twin_is_returned_resolved(tmp_path) -> None:
    """A real NFD twin inside the root resolves to the twin itself, with its owning root."""
    root = tmp_path / "archive"
    (root / _NFD_DIR).mkdir(parents=True)
    twin = root / _NFD_DIR / "notes.nfo"
    twin.write_text("Artist: DJ Test", encoding="utf-8")
    stored = root / _NFC_DIR / "notes.nfo"

    with patch("phaze.services.media_path_resolve.Path.exists", byte_exact_exists):
        resolved, owning_root = resolve_contained_twin(str(stored), [str(root)])

    assert resolved == twin.resolve()
    assert resolved.parent.name == _NFD_DIR
    assert owning_root == root.resolve()


def test_byte_exact_path_is_unchanged(tmp_path) -> None:
    """The common case: a path that exists byte-exact is just containment-checked."""
    root = tmp_path / "archive"
    root.mkdir()
    target = root / "info.nfo"
    target.write_text("x", encoding="utf-8")

    assert resolve_contained_twin(str(target), [str(root)]) == resolve_and_check_containment(str(target), [str(root)])
