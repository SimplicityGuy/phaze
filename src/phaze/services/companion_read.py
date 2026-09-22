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

# Extensions produced by the same scene-release tooling as .nfo: classic-BBS CP437 ANSI art and
# Latin-1-adjacent glyphs, never UTF-8 (phaze-j9b3z -- 21.5% of companion chars sent to the LLM in
# the 2026-09-22 prod trial were U+FFFD replacement chars from decoding CP437 .nfo bytes as UTF-8).
# .txt is included because scene groups emit it from the SAME tooling for the same purpose --
# release notes, a renamed FILE_ID.DIZ -- and it showed the identical mojibake in that trial.
# .m3u (and anything else) is deliberately excluded: playlist/cue sidecars are ASCII/UTF-8 by
# format spec, so guessing CP437 there would manufacture mojibake instead of removing it.
CP437_FALLBACK_SUFFIXES = frozenset({".nfo", ".txt"})


def read_companion_bounded_sync(path: str, max_chars: int) -> str:
    """Synchronous BOUNDED read of one companion file (phaze-cycw, phaze-j9b3z).

    Reads at most ``max_chars * COMPANION_READ_CHAR_MARGIN`` DECODED characters -- not the whole
    file -- via a text-mode file object, so ``TextIOWrapper.read(n)`` only pulls as many bytes off
    disk as needed to decode ``n`` characters instead of ``Path.read_text()``'s unconditional
    whole-file slurp. The margin (not a bare ``max_chars`` cap) leaves ``clean_companion_content``
    (which strips ASCII-art lines BEFORE truncating) enough headroom to still land >= ``max_chars``
    of real content after stripping. Only ~3000 chars are ever kept, so peak memory for even a
    multi-hundred-MB mis-categorized companion (a big log, an oversized .nfo, or an image matched by
    a COMPANION extension) is a small, fixed multiple of ``max_chars`` -- not the file size.

    Tries strict UTF-8 first -- unchanged behavior, and the common case for anything not produced
    by scene release tooling. On a decode failure, a companion whose suffix is in
    ``CP437_FALLBACK_SUFFIXES`` is re-read as CP437 (a full 256-code-point page, so this second
    read cannot itself raise ``UnicodeDecodeError``); anything else falls back to the previous
    UTF-8-with-replacement behavior, unchanged. Each attempt is its own bounded read -- never a
    whole-file slurp -- so the worst case is two small reads, not one large one.

    Must be run via ``asyncio.to_thread`` by the caller: the open()+read() here is still
    synchronous, blocking disk I/O.
    """
    limit = max_chars * COMPANION_READ_CHAR_MARGIN
    try:
        with Path(path).open(encoding="utf-8", errors="strict") as f:
            return f.read(limit)
    except UnicodeDecodeError:
        pass

    if Path(path).suffix.lower() not in CP437_FALLBACK_SUFFIXES:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            return f.read(limit)

    with Path(path).open(encoding="cp437") as f:
        return f.read(limit)
