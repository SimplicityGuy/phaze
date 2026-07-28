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
"""

from __future__ import annotations

from pathlib import Path


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
