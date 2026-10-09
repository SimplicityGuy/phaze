"""``python -m phaze.agent_watcher locate-stale``: the agent-side step of the stale-row reconcile (phaze-5rfev).

The controller cannot see an agent's disk, so the reconcile runs in three steps, like the phaze-oxn2m
moved-twin cleanup it extends: ``phaze backfill stale-row-candidates`` prints the agent's rows, this
module annotates them on the agent, and ``phaze backfill reconcile-stale-rows`` decides from the
annotated document. Nothing here talks to the network or the database.

For every row it answers two questions:

- ``exists``: is the row's ``current_path`` still there? ``None`` -- not ``False`` -- when no scan root
  the path lies under is mounted in this container, so an unmounted root can never make a live file
  read as gone. Companions open the exact ``resolve_contained_twin`` result with no-follow and
  nonblocking flags and require a regular file; unsafe/unreadable paths are unverifiable.
  Non-companion presence retains the existing ``resolve_media_path`` check.
- ``found`` (only for ``exists: False``): every file under the mounted scan roots whose SHA-256 equals
  the row's, as the upsert record a scan would have posted for it. A file is hashed only when its size
  equals a missing row's size, so the walk costs one ``stat`` per ingestible file plus a hash of the
  few same-size candidates. The walk is the scan's own (``tasks/scan.py::_walk_files``): ingestible
  extensions only, ``.phaze-quarantine`` pruned.

Import-graph invariant: like ``__main__``, this module must not import ``phaze.database``, the models
or SQLAlchemy (``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import os
from pathlib import Path
import stat
from typing import Any
import unicodedata

import structlog

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS
from phaze.services.containment import resolve_contained_twin
from phaze.services.hashing import compute_sha256
from phaze.services.media_path_resolve import resolve_media_path


logger = structlog.get_logger(__name__)


def is_under(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or lies beneath it (string prefix on a path boundary)."""
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def _upsert_record(path: Path, size: int, sha256_hash: str) -> dict[str, Any]:
    """The upsert record a scan of ``path`` would post (``tasks/scan.py::_hash_one_file``): NFC paths, lowercase type."""
    normalized = unicodedata.normalize("NFC", str(path))
    return {
        "sha256_hash": sha256_hash,
        "original_path": normalized,
        "original_filename": unicodedata.normalize("NFC", path.name),
        "current_path": normalized,
        "file_type": path.suffix.lower().lstrip("."),
        "file_size": size,
    }


_COMPANION_VERIFY_CAP = 1_048_576
"""Full-byte companion destination verification cap; larger copies are unverifiable, not missing."""


def _checked_companion(path: Path, roots: list[str], *, expected_size: int | None = None) -> tuple[Path, int, str]:
    """Open only the exact contained twin, no-follow/nonblocking, and require a regular file.

    Presence checks do not read bytes. Destination checks hash at most the expected size plus one,
    bounded by the hard cap, and reject growth/shrinkage or over-cap files instead of certifying them.
    """
    resolved, _root = resolve_contained_twin(str(path), roots)
    descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("companion locate requires a regular file")
        if expected_size is None:
            return resolved, size, ""
        if size != expected_size or size > _COMPANION_VERIFY_CAP:
            raise OSError("companion destination size changed or exceeds verification cap")
        digest = hashlib.sha256()
        remaining = expected_size + 1
        total = 0
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            remaining -= len(chunk)
        if total != expected_size or os.fstat(stream.fileno()).st_size != expected_size:
            raise OSError("companion destination changed during verification")
        return resolved, total, digest.hexdigest()


def _find_by_content(roots: list[str], wanted: dict[int, set[str]], companion_sizes: set[int]) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Walk ``roots`` once; return ``{sha256: [upsert record, ...]}`` for every file whose (size, hash) is wanted, and the error count."""
    from phaze.tasks.scan import _walk_files  # noqa: PLC0415 -- the scan's walk; deferred so a plain check never imports the scan task

    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    errors: list[OSError] = []
    for root in roots:
        for path in _walk_files(Path(root), errors):
            try:
                if companion_sizes or path.suffix.lower() in INGESTIBLE_COMPANION_EXTENSIONS:
                    path, size, _digest = _checked_companion(path, roots)
                else:
                    size = path.stat().st_size
                if size not in wanted:
                    continue
                if size in companion_sizes:
                    path, size, digest = _checked_companion(path, roots, expected_size=size)
                else:
                    digest = compute_sha256(path)
            except (OSError, ValueError) as exc:
                errors.append(OSError(str(exc)))
                continue
            if digest in wanted[size]:
                found[digest].append(_upsert_record(path, size, digest))
    for error in errors:
        logger.warning("locate-stale: unreadable path skipped", error=str(error))
    return found, len(errors)


def locate_stale(document: dict[str, Any]) -> dict[str, Any]:
    """Annotate ``phaze backfill stale-row-candidates``' document with ``exists`` and, for a gone row, ``found``.

    ``walked_roots`` records the mounted roots the content search covered and ``walk_errors`` how
    many paths could not be read, so the controller can tell "found nowhere" from "not looked for".
    """
    roots = [root for root in document.get("scan_roots", []) if Path(root).is_dir()]
    gone: list[dict[str, Any]] = []
    check_errors = 0
    companion_sizes: set[int] = set()
    for row in document["rows"]:
        path = row["path"]
        if not any(is_under(path, root) for root in roots):
            row["exists"] = None
            continue
        if Path(path).suffix.lower() in INGESTIBLE_COMPANION_EXTENSIONS:
            try:
                _checked_companion(Path(path), roots)
                row["exists"] = True
            except FileNotFoundError:
                row["exists"] = False
                companion_sizes.add(int(row["size"]))
            except (OSError, ValueError):
                row["exists"] = None
                check_errors += 1
        else:
            row["exists"] = Path(resolve_media_path(path)).exists()
        if row["exists"] is False:
            gone.append(row)
    wanted: dict[int, set[str]] = defaultdict(set)
    for row in gone:
        wanted[int(row["size"])].add(row["sha256"])
    found, walk_errors = _find_by_content(roots, wanted, companion_sizes) if gone else ({}, 0)
    for row in gone:
        row["found"] = sorted(found.get(row["sha256"], []), key=lambda record: record["original_path"])
    document["walked_roots"] = roots if gone else []
    document["walk_errors"] = walk_errors + check_errors
    return document
