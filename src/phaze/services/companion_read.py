"""Bounded companion-sidecar read -- the pure ON-DISK half (phaze-6bkk).

Split out of ``phaze.services.proposal`` so the read can run where the files are. Under DIST-01 the
controller worker that runs ``generate_proposals`` is fileless ("No volume mounts: the controller is
fileless and never touches SCAN_PATH", docker-compose.yml), so ``open()`` on an agent-reported
``current_path`` there raises ``FileNotFoundError`` and the ``.nfo`` / ``.txt`` / ``.m3u`` context an
LLM proposal is supposed to see was silently always empty in production (the read sat behind a bare
``except OSError: continue``). ``phaze.tasks.companion_read.read_companion_files`` calls this on the
owning agent instead; ``services.proposal`` awaits that job's result.

Stdlib-only by design (mirrors ``services/containment.py``): this module must stay importable from
the agent worker, which is banned from importing ``phaze.database`` / ``sqlalchemy`` (D-25,
``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

from pathlib import Path


# Read this multiple of `max_chars` so clean_companion_content's ASCII-art-line stripping (which
# runs BEFORE truncation) still has enough raw text left to yield >= max_chars of real content.
COMPANION_READ_CHAR_MARGIN = 4


def read_companion_bounded_sync(path: str, max_chars: int) -> str:
    """Synchronous BOUNDED read of one companion file (phaze-cycw).

    Reads at most ``max_chars * COMPANION_READ_CHAR_MARGIN`` DECODED characters -- not the whole
    file -- via a text-mode file object, so ``TextIOWrapper.read(n)`` only pulls as many bytes off
    disk as needed to decode ``n`` characters instead of ``Path.read_text()``'s unconditional
    whole-file slurp. The margin (not a bare ``max_chars`` cap) leaves ``clean_companion_content``
    (which strips ASCII-art lines BEFORE truncating) enough headroom to still land >= ``max_chars``
    of real content after stripping. Only ~3000 chars are ever kept, so peak memory for even a
    multi-hundred-MB mis-categorized companion (a big log, an oversized .nfo, or an image matched by
    a COMPANION extension) is a small, fixed multiple of ``max_chars`` -- not the file size.

    Must be run via ``asyncio.to_thread`` by the caller: the open()+read() here is still
    synchronous, blocking disk I/O.
    """
    with Path(path).open(encoding="utf-8", errors="replace") as f:
        return f.read(max_chars * COMPANION_READ_CHAR_MARGIN)
