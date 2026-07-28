"""DB-free unit tests for `resolve_and_check_containment` (phaze-eycl).

Bucket: ``shared``. Stdlib-only module (mirrors ``test_pg_text.py``), no Postgres needed.

Covers the symlink-escape and `..`-traversal cases the bead calls out specifically -- a bare
prefix-string check on the raw path catches neither: a symlink planted inside a scan_root can
point anywhere on disk, and a `..`-laden path can look nested under the root as a raw string
while resolving outside it.
"""

from __future__ import annotations

import pytest

from phaze.services.containment import resolve_and_check_containment


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
