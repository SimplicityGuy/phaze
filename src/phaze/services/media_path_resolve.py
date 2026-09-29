"""Agent-side fallback resolver for an NFC/NFD Unicode-normalization path mismatch (phaze-9pg11).

``routers/agent_files.py::upsert_files`` NFC-normalizes ``FileRecord.original_path`` (and the
agent's own ``scan.py`` / ``agent_watcher/poster.py`` normalize ``current_path`` the same way)
because NFC is the right identity/dedup/matching key: it is what makes a file reported once in
NFC form and once in NFD form (the same bytes, decomposed differently) collide on the
``(agent_id, original_path)`` natural key instead of double-inserting.

That normalization must never leak into a filesystem open/stat/rename, though. Linux filenames
are byte-exact -- a directory entry created with an NFD-decomposed name (routine for files that
started life on macOS, or titles minted by some web sources/yt-dlp) does NOT compare equal to its
NFC form, so any agent task that opens the STORED (NFC) path directly gets a permanent
``FileNotFoundError``: the file can never be metadata-extracted, analyzed, tagged, or moved.

:func:`resolve_media_path` is the single shared fallback every filesystem-touching agent task
routes through before it opens/stats/moves a reported path. It changes nothing for the common
case (the reported path already exists byte-exact). On a miss it resolves **component by
component** rather than just the filename: a mismatch can live in any ancestor directory too, not
just the leaf -- e.g. an artist folder named from a yt-dlp uploader (``Hör``) containing files that
are themselves byte-exact. Starting from the longest ancestor that actually exists, it walks back
down one path segment at a time, at each level preferring a byte-exact child and falling back to
the child whose NFC-normalized name matches -- so an NFD directory, an NFD file, or both together
all resolve the same way. Because it resolves from whatever is actually on disk, it needs no data
migration and fixes rows written before this fix exactly the same way it fixes rows written after
it. No caching: each call re-lists whatever directories it actually needs to.

Stdlib-only by design (mirrors ``services/containment.py`` and ``services/pg_text.py``): this
module must stay importable from every agent task module banned from importing
``phaze.database`` / ``phaze.models.*`` / SQLAlchemy (D-25,
``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

from pathlib import Path
import unicodedata

import structlog


logger = structlog.get_logger(__name__)


def _nfc_match(parent: Path, wanted_name: str) -> Path | None:
    """Return the child of *parent* whose NFC-normalized name equals *wanted_name*'s NFC form.

    Lists *parent* once (never recursive). Returns ``None`` if the directory cannot be listed
    (``OSError`` -- a genuinely missing/unmounted directory) or no entry matches.
    """
    target_nfc = unicodedata.normalize("NFC", wanted_name)
    try:
        entries = list(parent.iterdir())
    except OSError:
        return None

    for entry in entries:
        if unicodedata.normalize("NFC", entry.name) == target_nfc:
            return entry

    return None


def resolve_media_path(path: str) -> str:
    """Return a path this agent can actually open, resolving an NFC/NFD on-disk mismatch.

    Fast path: if *path* exists byte-exact, it is returned unchanged -- no directory listing, no
    normalization performed on the value handed back.

    Fallback: the mismatch is not necessarily in the final component -- an ancestor directory
    (e.g. an artist folder minted from a yt-dlp uploader name) can be NFD too. Find the longest
    ancestor of *path* that actually exists, then walk back down one component at a time: at each
    level, prefer the byte-exact child if it exists, else the child whose NFC-normalized name
    matches (one directory listing per level, never a recursive walk). This is what makes a
    PRE-EXISTING NFC-only row (written before this fix, or by an agent that never saw an NFD name)
    resolve correctly too: the match is against the real filesystem, not against any stored
    provenance.

    Returns *path* unchanged if any level can't be resolved -- an ancestor is missing entirely, a
    directory can't be listed, or no entry's NFC form matches at some level. Callers keep their
    existing "file really is missing" error handling in that case; this function only ever narrows
    a normalization mismatch, never masks any other failure.
    """
    candidate = Path(path)
    if candidate.exists():
        return path

    # Walk up from `candidate` to find the longest ancestor that actually exists, collecting the
    # path components below it (top-down order) that still need resolving.
    ancestor = candidate.parent
    pending = [candidate.name]
    while not ancestor.exists():
        parent = ancestor.parent
        if parent == ancestor:
            # Reached the filesystem root without finding an existing ancestor.
            return path
        pending.append(ancestor.name)
        ancestor = parent
    pending.reverse()

    resolved = ancestor
    for component in pending:
        child = resolved / component
        if child.exists():
            resolved = child
            continue

        match = _nfc_match(resolved, component)
        if match is None:
            return path
        resolved = match

    resolved_str = str(resolved)
    if resolved_str != path:
        logger.info(
            "resolve_media_path: NFC/NFD on-disk name mismatch resolved",
            reported_path=path,
            resolved_path=resolved_str,
        )
    return resolved_str
