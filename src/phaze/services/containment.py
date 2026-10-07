"""Path containment check shared by every consumer of agent-supplied paths (phaze-eycl).

Stdlib-only by design (mirrors ``services/pg_text.py``): this module must stay importable from
``phaze.tasks.execution`` -- which is banned from importing ``phaze.database`` / ``sqlalchemy``
(see ``tests/shared/core/test_task_split.py``) -- as well as from ordinary DB-backed services.

Extracted from ``phaze.tasks.execution._resolve_and_check_containment`` (T-26-11-S1), which
already got this right for the executor's ``original_path`` / destination checks: resolve
symlinks FIRST (via ``Path.resolve()``), then compare against each resolved ``scan_root``. Doing
the comparison on the resolved path, not the raw string, is what keeps a symlink planted inside a
scan_root from pointing anywhere outside it -- a prefix-string check on the unresolved path does
not catch that.

``load_companion_contents`` (``services/proposal.py``) reuses this exact function for the
companion-file read boundary rather than reimplementing it, so the two enforcement points can
never drift apart.

:func:`resolve_contained_twin` is the entry point for every agent task that also needs the
NFC/NFD on-disk twin (phaze-9pg11): it runs the twin lookup FIRST and the containment check on
its result, so the check always covers the exact path that gets opened (phaze-2tei9). Its one
non-stdlib dependency, ``services/media_path_resolve.py``, is held to the same import ban.
"""

from __future__ import annotations

from pathlib import Path

from phaze.services.media_path_resolve import resolve_media_path


def resolve_and_check_containment(candidate: str, scan_roots: list[str]) -> tuple[Path, Path]:
    """Resolve `candidate` and assert it lives under at least one of `scan_roots`.

    Returns ``(resolved, owning_root)`` -- the resolved candidate path and the resolved
    scan_root it lives under.

    Raises ValueError on path traversal or symlink escape (T-26-11-S1). The resolved path
    (not the raw candidate string) is what callers must use for any subsequent file op or
    read, so a symlink-out is also caught by whatever follows.

    An empty ``scan_roots`` list means "no root is configured" and therefore matches
    nothing -- callers must treat that as "refuse", never as "allow".
    """
    resolved = Path(candidate).resolve()
    for root in scan_roots:
        root_resolved = Path(root).resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved, root_resolved
        except ValueError:
            continue
    msg = f"path {candidate!r} (resolved to {resolved}) escapes all scan_roots {scan_roots}"
    raise ValueError(msg)


def resolve_contained_twin(candidate: str, scan_roots: list[str]) -> tuple[Path, Path]:
    """Resolve `candidate`'s on-disk NFC/NFD twin, then assert THAT lives under `scan_roots`.

    Returns ``(resolved, owning_root)`` exactly like :func:`resolve_and_check_containment`;
    ``resolved`` is the path to open, and nothing else is.

    The order is the point (phaze-2tei9). Checking `candidate` first and swapping in the twin
    afterwards leaves the opened path unchecked: ``Path.resolve()`` is non-strict, so a stored NFC
    tail that does not exist byte-exact stays LEXICAL and passes, and ``resolve_media_path`` then
    walks down from the longest existing ancestor through real children -- including a symlinked
    DIRECTORY pointing outside every root. Looking up the twin first hands the check a path whose
    components all exist, so ``resolve()`` follows every symlink in it before comparing.

    ``resolved`` is symlink-free at check time. A caller that reads it opens it with
    ``O_NOFOLLOW``, so a final component swapped for a symlink after the check is refused
    (``ELOOP``) rather than followed out of the roots.

    Raises ValueError on traversal or symlink escape, like :func:`resolve_and_check_containment`.
    """
    return resolve_and_check_containment(resolve_media_path(candidate), scan_roots)
