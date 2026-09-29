"""Shared byte-exact-filesystem test double for `phaze.services.media_path_resolve` (phaze-9pg11).

Every NFC/NFD regression test that exercises the resolver's fallback path needs to force
`Path.exists` to behave like a byte-exact (Linux ext4-style) filesystem rather than macOS's own
(HFS+/APFS), which resolve `open()`/`stat()`/`exists()` calls NORMALIZATION-INSENSITIVELY at the
syscall level. Naively patching `Path.exists` to a blanket ``return_value=False`` used to be
enough when the resolver only ever checked the final path component, but the resolver now walks
its whole ancestor chain (an NFD directory, not just an NFD file, is the point of phaze-9pg11) --
blanket-False also hides `tmp_path` itself, so the walk never finds an existing ancestor and gives
up, returning the unresolved path unchanged. That regression is invisible on macOS (its real
`open()` opens the wrong path anyway, insensitively) but fails for real on this repo's byte-exact
target platform (Linux CI).

:func:`byte_exact_exists` fixes this by reimplementing `Path.exists` as a genuine byte-exact name
comparison against whatever `Path.iterdir()` really finds, recursing on the parent chain *before*
listing anything -- so a real ancestor (like `tmp_path`) still reads as existing, and only a
genuinely NFC/NFD-mismatched component reads as missing, mirroring Linux regardless of host OS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


def byte_exact_exists(path: Path) -> bool:
    """Reimplement `Path.exists` with byte-exact (Linux ext4-style) name comparison.

    Recurses on the parent chain before listing anything: macOS's `opendir()` resolves a
    normalization-mismatched directory name insensitively (it happily opens "Hör" (NFC) when only
    "Hör" (NFD) is really on disk), so listing `path.parent` directly -- without first confirming
    `path.parent` itself is byte-exact-present -- would silently launder exactly the mismatch these
    tests exist to catch.
    """
    parent = path.parent
    if path == parent:  # filesystem root
        return True
    if not byte_exact_exists(parent):
        return False
    try:
        return any(entry.name == path.name for entry in parent.iterdir())
    except OSError:
        return False
