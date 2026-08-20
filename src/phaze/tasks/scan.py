"""SAQ task: scan_directory -- HTTP-only agent-side directory scanning (Phase 27 D-11..D-14).

scan_directory (Phase 27 D-11..D-14)
    Walk a directory on the agent host, SHA-256 each known-extension file, POST
    chunks of FileUpsertRecord via PhazeAgentClient.upsert_files, and PATCH the
    ScanBatch's processed_files after each chunk + a terminal status PATCH at
    the end. Mid-walk OSError per file -> warning + continue (mirrors the
    per-file skip pattern of the Phase-89-retired ``services/ingestion.py``, whose
    directory-walk responsibilities this module absorbed). NFC-normalizes
    original_path, original_filename, and current_path (Pitfall 3). Uses os.walk
    with followlinks disabled (Pitfall 4). Hashes via asyncio.to_thread so the SAQ
    event loop isn't blocked.

This module MUST NOT import phaze.database or phaze.models.* or sqlalchemy. Enforced by
tests/shared/core/test_task_split.py::test_agent_worker_does_not_import_phaze_database
(Phase 26 D-25 + Phase 27 D-13 invariant).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
import unicodedata

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertRecord
from phaze.schemas.agent_scan_batches import ScanBatchPatch
from phaze.schemas.agent_tasks import ScanDirectoryPayload
from phaze.services.agent_client import AgentApiServerError
from phaze.services.hashing import compute_sha256


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)


_DEFAULT_SCAN_CHUNK_SIZE = 500
"""Fallback chunk size if get_settings() returns a non-AgentSettings (e.g., in pure unit tests).

Mirrors AgentSettings.scan_chunk_size default (Phase 27 Plan 01). The runtime value
is read via get_settings() in scan_directory; this constant is the safety net for
test contexts that monkeypatch get_settings() or run under PHAZE_ROLE=control.
"""


_EXTRACTABLE: frozenset[FileCategory] = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})
"""Extension categories that scan_directory ingests; matches the watcher's filter
(``agent_watcher/observer.py``) and the controller-side auto-enqueue gate
(``routers/agent_files.py``). COMPANION extensions (``.cue``, ``.nfo``, ``.txt``,
images, playlists, ...) are deliberately excluded so the manual-scan ingestion
set is identical to the watcher's ingestion set (Phase 27 CR-01).
"""


def _classify(filename: str) -> FileCategory:
    """Classify a filename by extension. Mirrors services.ingestion.classify_file but
    is duplicated here to keep the agent task module's import graph Postgres-free
    (services.ingestion transitively imports phaze.models).
    """
    return EXTENSION_MAP.get(Path(filename).suffix.lower(), FileCategory.UNKNOWN)


def _walk_ingestible(scan_root: Path) -> tuple[list[Path], list[OSError]]:
    """Authoritative walk over `scan_root`, run entirely off the event loop (phaze-j54q).

    Walks the tree once WITHOUT stat or hashing, collecting the full path of every file
    whose extension is ingestible (MUSIC/VIDEO) plus any directory-read OSError raised by
    os.walk. Returns ``(paths, errors)``; the caller stats/hashes each path (still via
    asyncio.to_thread per file) and logs the collected errors back on the loop.

    Mirrors ``_count_ingestible`` (phaze-bfd1), which moved the pre-count walk off-loop for
    exactly this reason but left this, the authoritative hashing walk, iterating os.walk
    directly on the event loop -- i.e. every directory's os.scandir executed synchronously
    in the caller. A directory whose files are ALL non-ingestible (artwork, .cue/.nfo/.txt
    companions, playlists -- all excluded by _EXTRACTABLE) produced a loop-body iteration
    with no await at all, so a companion-heavy subtree (or a stalled network mount) could
    monopolize the loop with zero yields -- the same starvation shape phaze-bfd1 diagnosed,
    just on the second walk. Dispatched via asyncio.to_thread so the full synchronous os.walk
    never runs back-to-back on the agent worker's event loop. Only ingestible-file paths are
    retained (not every file in the tree), keeping memory bounded to the music/video count.
    """
    errors: list[OSError] = []
    paths: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(scan_root, followlinks=False, onerror=errors.append):
        for filename in filenames:
            if _classify(filename) in _EXTRACTABLE:
                paths.append(Path(dirpath) / filename)
    return paths, errors


def _count_ingestible(scan_root: Path) -> tuple[int, list[OSError]]:
    """Pre-count pass over `scan_root`, run entirely off the event loop (phaze-bfd1).

    Walks the tree once WITHOUT stat or hashing, counting only files whose extension
    is ingestible (MUSIC/VIDEO), and collecting any directory-read OSError raised by
    os.walk. Returns ``(count, errors)``; the caller logs the collected errors back
    on the loop.

    This whole function is dispatched via ``asyncio.to_thread`` so the full synchronous
    os.walk -- tens of thousands of readdir round-trips on a large network mount with a
    cold cache -- never runs back-to-back on the agent worker's event loop. In all-mode
    (no lane split) that loop is the agent's ONLY heartbeat source, and a pre-count walk
    that monopolizes it past the 300s DEAD threshold ages ``last_seen_at`` into the DEAD
    band while the worker is perfectly healthy -- a false DEAD that re-enqueues the
    agent's in-flight work, the exact cascade Phase 46 restructured the heartbeat to
    prevent. Mirrors the hashing walk below, which already offloads its per-file stat/
    sha256 via asyncio.to_thread.
    """
    errors: list[OSError] = []
    count = 0
    for _dirpath, _dirnames, filenames in os.walk(scan_root, followlinks=False, onerror=errors.append):
        for filename in filenames:
            if _classify(filename) in _EXTRACTABLE:
                count += 1
    return count, errors


def _resolve_chunk_size() -> int:
    """Read AgentSettings.scan_chunk_size if available; fall back to 500.

    Clamped to ``agent_file_chunk_max`` (phaze-flxrz): ``FileUpsertChunk.files`` enforces
    ``max_length=agent_file_chunk_max`` (schemas/agent_files.py) at construction time --
    client-side, before any HTTP call. ``scan_chunk_size`` has no upper bound of its own
    (config.py), so an operator setting PHAZE_SCAN_CHUNK_SIZE above the server's chunk cap
    made the very first full ``FileUpsertChunk(...)`` construction below raise an uncaught
    pydantic ValidationError -- scan_directory's only handler is ``except AgentApiServerError``
    -- crash-looping the SAQ job without ever sending the terminal 'failed' PATCH, so the
    ScanBatch was stranded RUNNING. Clamping here at the one place the two knobs meet keeps
    every downstream chunk (including the final partial flush, which only ever holds
    ``< chunk_size`` records) at or under the cap regardless of which knob moved.
    ``agent_file_chunk_max`` lives on the shared ``BaseSettings``, so it is available on
    both ``AgentSettings`` and ``ControlSettings`` -- the clamp applies to the fallback
    default too.
    """
    cfg = get_settings()
    raw_chunk_size = cfg.scan_chunk_size if isinstance(cfg, AgentSettings) else _DEFAULT_SCAN_CHUNK_SIZE
    return min(raw_chunk_size, cfg.agent_file_chunk_max)


class _ScanProgress:
    """The walk's two counters, mutated in place so the abort handler can still read them.

    Same out-parameter shape, and the same reason, as ``tasks/functions.py``'s ``scratch_state`` and
    ``tasks/execution.py``'s ``_MoveStep``: the ``AgentApiServerError`` handler must report
    ``files_posted`` even when the raise came from inside the chunk loop, so the count cannot live in
    that frame. ``files_skipped`` rides along because the zero-access guard reads both.
    """

    __slots__ = ("files_skipped", "total")

    def __init__(self) -> None:
        self.total = 0
        # phaze-0p90: count per-FILE read failures too. The zero-access loud-failure guard originally
        # counted only directory-walk errors (walk_errors), so a tree with LISTABLE directories but
        # UNREADABLE files (dirs 0755, files 0600 owned by a foreign uid -- the common container-UID
        # mismatch) walked every filename, skipped every hash with a per-file warning, and terminal-PATCHed
        # status=completed/0-files -- reproducing the exact 260608 silent-failure mode the guard exists to
        # prevent. Track the skips so a 0-file scan with unreadable files also fails loudly.
        self.files_skipped = 0


async def _publish_precount(api: PhazeAgentClient, payload: ScanDirectoryPayload, scan_root: Path) -> None:
    """Populate ``ScanBatch.total_files`` up front from a hash-free name-only walk (best-effort UX)."""
    # Pre-count pass (UX denominator): walk the tree once WITHOUT stat or hashing,
    # counting only files whose extension is ingestible (MUSIC/VIDEO). This populates
    # ScanBatch.total_files up front so the Recent Scans "N / Z" progress widget shows a
    # real denominator during a RUNNING scan instead of "—" (which previously only
    # filled in at the terminal success PATCH). Counting names is cheap even on a large
    # network mount. The hashed `total` from the walk below remains the source of truth
    # and self-corrects any drift via the terminal total_files PATCH.
    #
    # Pre-count walk errors are collected in a SEPARATE local list that is deliberately
    # NOT merged into the hashing walk's `walk_errors`. The zero-access failure check
    # (`total == 0 and walk_errors`) and its error-count message must stay driven solely
    # by the authoritative hashing walk, so a permission failure is counted exactly once
    # there. We still collect pre-count read failures so they are logged rather than
    # silently swallowed.
    #
    # phaze-bfd1: the entire synchronous os.walk runs OFF the event loop via
    # asyncio.to_thread. On a large network mount (cold cache) this traversal is tens of
    # thousands of readdir round-trips that would otherwise execute back-to-back on the
    # loop with zero yields, starving the Phase-46 heartbeat past the 300s DEAD threshold
    # and getting a healthy agent classified DEAD. The collected errors are logged here on
    # the loop (structlog stays on the event loop, not the worker thread).
    precount, precount_walk_errors = await asyncio.to_thread(_count_ingestible, scan_root)
    for precount_error in precount_walk_errors:
        logger.warning("scan_directory: cannot read directory during pre-count walk: %s", precount_error)
    logger.info("scan precount", batch_id=str(payload.batch_id), total=precount)
    try:
        await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(total_files=precount))
    except AgentApiServerError:
        # The pre-count PATCH is best-effort UX (populating the denominator early). If the
        # controller is unavailable here, do NOT abort the scan on a UX-only write -- let the
        # authoritative hashing walk below drive the per-chunk/terminal PATCHes and the
        # controller-5xx failure handling (which surfaces a proper 'failed' terminal PATCH).
        logger.warning("scan_directory: pre-count total_files PATCH failed; continuing", batch_id=str(payload.batch_id))


async def _hash_one_file(full_path: Path) -> FileUpsertRecord:
    """stat + SHA-256 one candidate and build its upsert record. Raises ``OSError`` on an unreadable file.

    Both blocking calls stay individually offloaded via ``asyncio.to_thread`` (phaze-j54q), and the
    ``OSError`` deliberately propagates: the caller owns the per-file skip counter that the zero-access
    guard reads, so swallowing it here would hide the 260608 failure mode all over again.
    """
    filename = full_path.name
    stat_result = await asyncio.to_thread(full_path.stat)
    file_size = stat_result.st_size
    sha256_hash = await asyncio.to_thread(compute_sha256, full_path)
    # Pitfall 3: NFC-normalize EVERY path field. Drift between the watcher's
    # normalization and scan_directory's would create duplicate FileRecord rows
    # under the composite UQ (agent_id, original_path).
    normalized_path = unicodedata.normalize("NFC", str(full_path))
    normalized_filename = unicodedata.normalize("NFC", filename)
    normalized_current = unicodedata.normalize("NFC", str(full_path))
    return FileUpsertRecord(
        sha256_hash=sha256_hash,
        original_path=normalized_path,
        original_filename=normalized_filename,
        current_path=normalized_current,
        file_type=Path(filename).suffix.lower().lstrip("."),
        file_size=file_size,
    )


async def _hash_and_post_chunks(
    api: PhazeAgentClient, payload: ScanDirectoryPayload, candidate_paths: list[Path], chunk_size: int, progress: _ScanProgress
) -> None:
    """Hash every candidate and POST it in ``chunk_size`` batches, PATCHing progress after each.

    Per-file ``OSError`` is a warn-and-skip that does NOT abort the walk (D-12); ``AgentApiServerError``
    from either HTTP call propagates to ``scan_directory``'s single terminal handler, which is why the
    counts live on ``progress`` rather than in this frame.
    """
    batch: list[FileUpsertRecord] = []
    for full_path in candidate_paths:
        try:
            record = await _hash_one_file(full_path)
        except OSError as exc:
            progress.files_skipped += 1
            logger.warning("scan_directory: skipping unreadable file %s: %s", full_path, exc)
            continue

        batch.append(record)
        progress.total += 1
        logger.debug("file discovered", path=record.original_path, size=record.file_size, ext=record.file_type)
        if len(batch) >= chunk_size:
            await api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))
            await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(processed_files=progress.total))
            logger.info("scan progress", batch_id=str(payload.batch_id), processed=progress.total)
            batch = []

    # Flush final partial chunk.
    if batch:
        await api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))
        await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(processed_files=progress.total))


async def _fail_zero_access(
    api: PhazeAgentClient, payload: ScanDirectoryPayload, walk_errors: list[OSError], progress: _ScanProgress
) -> dict[str, Any]:
    """Terminal-fail a scan that produced no files AND hit at least one access error."""
    reasons = []
    if walk_errors:
        reasons.append(f"{len(walk_errors)} directory read error(s) (first: {walk_errors[0]})")
    if progress.files_skipped:
        reasons.append(f"{progress.files_skipped} unreadable file(s)")
    error_message = (
        f"Scanned 0 files but hit {' and '.join(reasons)}. The agent container user likely "
        f"cannot read {payload.scan_path} -- check file ownership/permissions vs the container UID."
    )
    logger.error(
        "scan failed",
        batch_id=str(payload.batch_id),
        path=payload.scan_path,
        error="access_errors",
        walk_error_count=len(walk_errors),
        files_skipped=progress.files_skipped,
    )
    await api.patch_scan_batch(
        payload.batch_id,
        ScanBatchPatch(status="failed", error_message=error_message),
    )
    # Preserve the historical reason for the directory-walk case; name the file-skip case distinctly.
    reason = "walk_permission_errors" if walk_errors else "unreadable_files"
    return {"status": "failed", "files_posted": 0, "reason": reason}


async def _finish_scan(
    api: PhazeAgentClient, payload: ScanDirectoryPayload, walk_errors: list[OSError], progress: _ScanProgress, started_at: float
) -> dict[str, Any]:
    """Decide the walk's terminal outcome: loud zero-access failure, or completion (partial or clean)."""
    # Zero-access scan: the walk produced no files AND hit at least one access error -- either an
    # unreadable DIRECTORY (walk_errors) OR an unreadable FILE (files_skipped, phaze-0p90). Surface
    # this as a terminal failure that names the scan_path, the error/skip counts, and the first
    # error, and points at the likely container-UID/ownership cause. This makes the incident's
    # silent completed/0-files failure mode impossible to hide again -- for BOTH access-denial shapes.
    if progress.total == 0 and (walk_errors or progress.files_skipped):
        return await _fail_zero_access(api, payload, walk_errors, progress)

    # Partial access: some directories and/or files were unreadable but >=1 file was found.
    # Complete normally, logging a SINGLE summarizing warning rather than flooding the log with one
    # line per skipped directory/file. phaze-0p90: include the per-file skip count so a
    # mostly-unreadable-but-nonzero scan is visible rather than silently reporting only the readable subset.
    if walk_errors or progress.files_skipped:
        logger.warning(
            "scan_directory: completed with partial access -- %d director(ies) and %d file(s) skipped (first dir error: %s)",
            len(walk_errors),
            progress.files_skipped,
            walk_errors[0] if walk_errors else None,
        )

    # Terminal success PATCH.
    await api.patch_scan_batch(
        payload.batch_id,
        ScanBatchPatch(status="completed", total_files=progress.total, processed_files=progress.total),
    )
    logger.info(
        "scan completed",
        batch_id=str(payload.batch_id),
        files=progress.total,
        duration_s=round(time.monotonic() - started_at, 3),
    )
    return {"status": "completed", "files_posted": progress.total}


async def _abort_on_controller_error(
    api: PhazeAgentClient, payload: ScanDirectoryPayload, exc: AgentApiServerError, progress: _ScanProgress
) -> dict[str, Any]:
    """5xx after retries (D-12) -- abort the walk and surface a 'failed' terminal PATCH."""
    # NOTE: do NOT use .exception() in the path that re-PATCHes via the same broken
    # controller; if the controller is down, this PATCH may also raise -- but the
    # outer SAQ retry policy handles that. The terminal PATCH is best-effort.
    logger.exception("scan_directory: controller error after retries; aborting walk batch=%s", payload.batch_id)
    logger.error("scan failed", batch_id=str(payload.batch_id), error="controller_5xx", files_posted=progress.total)
    try:
        await api.patch_scan_batch(
            payload.batch_id,
            ScanBatchPatch(status="failed", error_message=f"Controller error: {exc}"),
        )
    except AgentApiServerError:
        logger.exception("scan_directory: terminal failed-PATCH also failed batch=%s", payload.batch_id)
    return {"status": "failed", "files_posted": progress.total, "reason": "controller_5xx"}


async def scan_directory(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Walk a directory, SHA-256 known-extension files, POST chunks via HTTP (Phase 27 D-11..D-13).

    Per-chunk flow:
      1. Append records until len(batch) == AgentSettings.scan_chunk_size (default 500).
      2. POST FileUpsertChunk(files=batch, batch_id=payload.batch_id) via api.upsert_files.
      3. PATCH ScanBatchPatch(processed_files=total) via api.patch_scan_batch.
      4. Reset batch.

    On clean walk: terminal PATCH ScanBatchPatch(status='completed', total_files=N, processed_files=N).
    On scan_path-missing: short-circuit PATCH ScanBatchPatch(status='failed', error_message=...).
    On AgentApiServerError after retries (D-12): abort + PATCH 'failed' with the cause.
    On per-file OSError: log a warning, skip the file, continue the walk (mirrors the
    Phase-89-retired ``services/ingestion.py``'s per-file skip pattern; D-12).

    phaze-vu88k.7: the same four phases in the same order as before they were split into helpers
    (``_publish_precount``, ``_hash_and_post_chunks``, ``_finish_scan``, ``_abort_on_controller_error``)
    -- a pure decomposition, not a behaviour change. The single ``AgentApiServerError`` handler still
    spans the chunk loop AND the terminal PATCH, which is why the counters moved onto ``_ScanProgress``.
    """
    payload = ScanDirectoryPayload.model_validate(kwargs)

    api: PhazeAgentClient = ctx["api_client"]
    chunk_size = _resolve_chunk_size()

    # Operational logging (PR3): prove a running scan is doing work. agent context is
    # the resolved agent_id when the worker stashed an identity; omitted in pure unit
    # tests. time.monotonic() drives the duration so a clock change cannot skew it.
    agent_id = getattr(ctx.get("agent_identity"), "agent_id", None)
    started_at = time.monotonic()
    logger.info("scan started", batch_id=str(payload.batch_id), path=payload.scan_path, agent=agent_id)

    scan_root = Path(payload.scan_path)
    if not scan_root.is_dir():
        logger.error("scan failed", batch_id=str(payload.batch_id), path=payload.scan_path, error="scan_path_not_a_directory")
        await api.patch_scan_batch(
            payload.batch_id,
            ScanBatchPatch(
                status="failed",
                error_message=f"Scan path does not exist on agent: {payload.scan_path}",
            ),
        )
        return {"status": "failed", "files_posted": 0, "reason": "scan_path_not_a_directory"}

    await _publish_precount(api, payload, scan_root)

    # os.walk silently swallows a PermissionError raised while reading a
    # directory unless an onerror callback is supplied. Without it, a fully
    # unreadable tree (e.g. media owned by uid 1000, mode 700, scanned by a
    # container running as a different uid) returns status=completed/0-files --
    # indistinguishable from a genuinely empty directory. This was the exact
    # failure mode that hid the 260608 incident. Collect every walk error so we
    # can fail loudly on a zero-access scan and warn once on partial access.
    #
    # phaze-j54q: the traversal itself (including every os.scandir readdir inside
    # os.walk) runs OFF the event loop via asyncio.to_thread, mirroring the pre-count
    # walk above (phaze-bfd1). Only the per-file stat/hash below still run on the loop,
    # each individually offloaded via asyncio.to_thread.
    candidate_paths, walk_errors = await asyncio.to_thread(_walk_ingestible, scan_root)
    for walk_error in walk_errors:
        logger.warning("scan_directory: cannot read directory during walk: %s", walk_error)

    progress = _ScanProgress()
    try:
        await _hash_and_post_chunks(api, payload, candidate_paths, chunk_size, progress)
        return await _finish_scan(api, payload, walk_errors, progress, started_at)
    except AgentApiServerError as exc:
        return await _abort_on_controller_error(api, payload, exc, progress)
