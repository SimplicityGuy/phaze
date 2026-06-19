"""SAQ tasks: scan_live_set + scan_directory -- HTTP-only agent-side scanning (Phase 26 D-05, D-27 + Phase 27 D-11..D-14).

scan_live_set
    Fingerprint-query a live-set file and POST the resolved tracklist via the
    agent HTTP boundary. Idempotency: a stable uuid5(NAMESPACE_URL, "phaze-scan-{file_id}")
    request_id collapses SAQ retries to one tracklist on the controller side.

scan_directory (Phase 27 D-11..D-14)
    Walk a directory on the agent host, SHA-256 each known-extension file, POST
    chunks of FileUpsertRecord via PhazeAgentClient.upsert_files, and PATCH the
    ScanBatch's processed_files after each chunk + a terminal status PATCH at
    the end. Mid-walk OSError per file -> warning + continue (mirrors
    services/ingestion.py:65). NFC-normalizes original_path, original_filename,
    and current_path (Pitfall 3). Uses os.walk with followlinks disabled (Pitfall 4).
    Hashes via asyncio.to_thread so the SAQ event loop isn't blocked.

This module MUST NOT import phaze.database, phaze.models.*, sqlalchemy, or
phaze.services.ingestion (which transitively imports phaze.models). Enforced by
tests/test_task_split.py::test_agent_worker_does_not_import_phaze_database
(Phase 26 D-25 + Phase 27 D-13 invariant).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
import unicodedata
import uuid

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertRecord
from phaze.schemas.agent_scan_batches import ScanBatchPatch
from phaze.schemas.agent_tasks import ScanDirectoryPayload, ScanLiveSetPayload
from phaze.schemas.agent_tracklists import TracklistCreatePayload, TracklistTrackPayload
from phaze.services.agent_client import AgentApiServerError
from phaze.services.hashing import compute_sha256


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient
    from phaze.services.fingerprint import FingerprintOrchestrator


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


def _resolve_chunk_size() -> int:
    """Read AgentSettings.scan_chunk_size if available; fall back to 500."""
    cfg = get_settings()
    if isinstance(cfg, AgentSettings):
        return cfg.scan_chunk_size
    return _DEFAULT_SCAN_CHUNK_SIZE


async def scan_live_set(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Run fingerprint-query against a live-set file; POST tracklist via HTTP."""
    payload = ScanLiveSetPayload.model_validate(kwargs)

    api: PhazeAgentClient = ctx["api_client"]
    orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]

    matches = await orchestrator.combined_query(payload.original_path)
    if not matches:
        # No-match COMPLETE: this scan run has NO tracklist callback, so it must ack the
        # control side directly to clear scan_live_set:<file_id> -- otherwise a legitimate
        # no-match scan would re-enqueue on EVERY recovery (Phase 45 Blocker 2 / T-45-16).
        #
        # Guard the ack exactly like the match-failure handler at the bottom of this function
        # (and functions.py:179-189): re-raise on a RETRYABLE attempt so SAQ retries and the
        # row survives for the real retry; on the TERMINAL attempt the ack is best-effort --
        # swallow + log so the no_matches COMPLETE still returns (CR-01 / T-45-16). The one
        # difference from the match path: a no-match is a clean COMPLETE, so the terminal-ack
        # failure does NOT re-raise -- blocking the return would leak the ledger row forever.
        try:
            await api.report_scan_terminal(payload.file_id)
        except Exception:
            job = ctx.get("job")
            if job is not None and not job.retryable:
                logger.warning("scan_live_set no-match terminal-ack failed", file_id=str(payload.file_id), exc_info=True)
            else:
                raise  # retryable (or job absent): let SAQ retry; the row survives for the real retry
        return {"file_id": str(payload.file_id), "status": "no_matches"}

    # Build the wire payload. Idempotency key = stable UUID per file_id so SAQ retries
    # of the same job collapse to one tracklist (server's Redis cache catches the replay).
    # Using uuid5 with payload.file_id + a phase namespace; predictable across retries.
    request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"phaze-scan-{payload.file_id}")
    external_id = f"fp-{payload.file_id.hex[:12]}"

    tracks = [
        TracklistTrackPayload(
            position=i + 1,
            artist=None,  # metadata-join skipped on agent; controller can enrich
            title=None,
            timestamp=match.timestamp,
            confidence=match.confidence,
        )
        for i, match in enumerate(matches)
    ]

    try:
        response = await api.create_tracklist(
            TracklistCreatePayload(
                file_id=payload.file_id,
                source="fingerprint",
                external_id=external_id,
                tracks=tracks,
                request_id=request_id,
            ),
        )
    except Exception:
        # A terminal failure of the MATCH path (e.g. the tracklist POST exhausting retries):
        # ack ONLY on the retries-exhausted attempt (mirrors functions.py:183-189) so the
        # ledger row is cleared exactly once, then re-raise so SAQ records the failed attempt.
        # A retryable attempt re-raises silently so the row survives for the real retry
        # (Phase 45 T-45-06). The successful match clears via create_tracklist -- no double-ack.
        job = ctx.get("job")
        if job is not None and not job.retryable:
            await api.report_scan_terminal(payload.file_id)
        raise

    return {
        "file_id": str(payload.file_id),
        "status": "scanned",
        "tracklist_id": str(response.tracklist_id),
        "version": response.version,
    }


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
    On per-file OSError: log a warning, skip the file, continue the walk (matches services/ingestion.py:65; D-12).
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
    # there. We still pass an onerror callback so a pre-count read failure is logged
    # rather than silently swallowed.
    precount_walk_errors: list[OSError] = []

    def _on_precount_error(exc: OSError) -> None:
        precount_walk_errors.append(exc)
        logger.warning("scan_directory: cannot read directory during pre-count walk: %s", exc)

    precount = 0
    for _dirpath, _dirnames, precount_filenames in os.walk(scan_root, followlinks=False, onerror=_on_precount_error):
        for precount_filename in precount_filenames:
            if _classify(precount_filename) in _EXTRACTABLE:
                precount += 1
    logger.info("scan precount", batch_id=str(payload.batch_id), total=precount)
    try:
        await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(total_files=precount))
    except AgentApiServerError:
        # The pre-count PATCH is best-effort UX (populating the denominator early). If the
        # controller is unavailable here, do NOT abort the scan on a UX-only write -- let the
        # authoritative hashing walk below drive the per-chunk/terminal PATCHes and the
        # controller-5xx failure handling (which surfaces a proper 'failed' terminal PATCH).
        logger.warning("scan_directory: pre-count total_files PATCH failed; continuing", batch_id=str(payload.batch_id))

    # os.walk silently swallows a PermissionError raised while reading a
    # directory unless an onerror callback is supplied. Without it, a fully
    # unreadable tree (e.g. media owned by uid 1000, mode 700, scanned by a
    # container running as a different uid) returns status=completed/0-files --
    # indistinguishable from a genuinely empty directory. This was the exact
    # failure mode that hid the 260608 incident. Collect every walk error so we
    # can fail loudly on a zero-access scan and warn once on partial access.
    walk_errors: list[OSError] = []

    def _on_walk_error(exc: OSError) -> None:
        walk_errors.append(exc)
        logger.warning("scan_directory: cannot read directory during walk: %s", exc)

    batch: list[FileUpsertRecord] = []
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(scan_root, followlinks=False, onerror=_on_walk_error):
            for filename in filenames:
                category = _classify(filename)
                if category not in _EXTRACTABLE:
                    continue

                full_path = Path(dirpath) / filename
                try:
                    stat_result = await asyncio.to_thread(full_path.stat)
                    file_size = stat_result.st_size
                    sha256_hash = await asyncio.to_thread(compute_sha256, full_path)
                except OSError as exc:
                    logger.warning("scan_directory: skipping unreadable file %s: %s", full_path, exc)
                    continue

                # Pitfall 3: NFC-normalize EVERY path field. Drift between the watcher's
                # normalization and scan_directory's would create duplicate FileRecord rows
                # under the composite UQ (agent_id, original_path).
                normalized_path = unicodedata.normalize("NFC", str(full_path))
                normalized_filename = unicodedata.normalize("NFC", filename)
                normalized_current = unicodedata.normalize("NFC", str(full_path))
                record = FileUpsertRecord(
                    sha256_hash=sha256_hash,
                    original_path=normalized_path,
                    original_filename=normalized_filename,
                    current_path=normalized_current,
                    file_type=Path(filename).suffix.lower().lstrip("."),
                    file_size=file_size,
                )
                batch.append(record)
                total += 1
                logger.debug("file discovered", path=normalized_path, size=file_size, ext=record.file_type)
                if len(batch) >= chunk_size:
                    await api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))
                    await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(processed_files=total))
                    logger.info("scan progress", batch_id=str(payload.batch_id), processed=total)
                    batch = []

        # Flush final partial chunk.
        if batch:
            await api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))
            await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(processed_files=total))

        # Zero-access scan: the walk produced no files AND hit at least one
        # directory read error. Surface this as a terminal failure that names
        # the scan_path, the error count, and the first error, and points at
        # the likely container-UID/ownership cause. This makes the incident's
        # silent failure mode impossible to hide again.
        if total == 0 and walk_errors:
            error_message = (
                f"Scanned 0 files but hit {len(walk_errors)} directory read error(s) "
                f"(first: {walk_errors[0]}). The agent container user likely cannot read "
                f"{payload.scan_path} -- check file ownership/permissions vs the container UID."
            )
            logger.error(
                "scan failed",
                batch_id=str(payload.batch_id),
                path=payload.scan_path,
                error="walk_permission_errors",
                walk_error_count=len(walk_errors),
            )
            await api.patch_scan_batch(
                payload.batch_id,
                ScanBatchPatch(status="failed", error_message=error_message),
            )
            return {"status": "failed", "files_posted": 0, "reason": "walk_permission_errors"}

        # Partial access: some directories were unreadable but >=1 file was
        # found. Complete normally, logging a SINGLE summarizing warning rather
        # than flooding the log with one line per skipped directory.
        if walk_errors:
            logger.warning(
                "scan_directory: completed with partial access -- %d director(ies) skipped (first: %s)",
                len(walk_errors),
                walk_errors[0],
            )

        # Terminal success PATCH.
        await api.patch_scan_batch(
            payload.batch_id,
            ScanBatchPatch(status="completed", total_files=total, processed_files=total),
        )
        logger.info(
            "scan completed",
            batch_id=str(payload.batch_id),
            files=total,
            duration_s=round(time.monotonic() - started_at, 3),
        )
        return {"status": "completed", "files_posted": total}

    except AgentApiServerError as exc:
        # 5xx after retries (D-12) -- abort the walk and surface a 'failed' terminal PATCH.
        # NOTE: do NOT use .exception() in the path that re-PATCHes via the same broken
        # controller; if the controller is down, this PATCH may also raise -- but the
        # outer SAQ retry policy handles that. The terminal PATCH is best-effort.
        logger.exception("scan_directory: controller error after retries; aborting walk batch=%s", payload.batch_id)
        logger.error("scan failed", batch_id=str(payload.batch_id), error="controller_5xx", files_posted=total)
        try:
            await api.patch_scan_batch(
                payload.batch_id,
                ScanBatchPatch(status="failed", error_message=f"Controller error: {exc}"),
            )
        except AgentApiServerError:
            logger.exception("scan_directory: terminal failed-PATCH also failed batch=%s", payload.batch_id)
        return {"status": "failed", "files_posted": total, "reason": "controller_5xx"}
