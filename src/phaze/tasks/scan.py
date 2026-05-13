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
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
import unicodedata
import uuid

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


logger = logging.getLogger(__name__)


_DEFAULT_SCAN_CHUNK_SIZE = 500
"""Fallback chunk size if get_settings() returns a non-AgentSettings (e.g., in pure unit tests).

Mirrors AgentSettings.scan_chunk_size default (Phase 27 Plan 01). The runtime value
is read via get_settings() in scan_directory; this constant is the safety net for
test contexts that monkeypatch get_settings() or run under PHAZE_ROLE=control.
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

    response = await api.create_tracklist(
        TracklistCreatePayload(
            file_id=payload.file_id,
            source="fingerprint",
            external_id=external_id,
            tracks=tracks,
            request_id=request_id,
        ),
    )

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

    scan_root = Path(payload.scan_path)
    if not scan_root.is_dir():
        await api.patch_scan_batch(
            payload.batch_id,
            ScanBatchPatch(
                status="failed",
                error_message=f"Scan path does not exist on agent: {payload.scan_path}",
            ),
        )
        return {"status": "failed", "files_posted": 0, "reason": "scan_path_not_a_directory"}

    batch: list[FileUpsertRecord] = []
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(scan_root, followlinks=False):
            for filename in filenames:
                category = _classify(filename)
                if category == FileCategory.UNKNOWN:
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
                if len(batch) >= chunk_size:
                    await api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))
                    await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(processed_files=total))
                    batch = []

        # Flush final partial chunk.
        if batch:
            await api.upsert_files(FileUpsertChunk(files=batch, batch_id=payload.batch_id))
            await api.patch_scan_batch(payload.batch_id, ScanBatchPatch(processed_files=total))

        # Terminal success PATCH.
        await api.patch_scan_batch(
            payload.batch_id,
            ScanBatchPatch(status="completed", total_files=total, processed_files=total),
        )
        return {"status": "completed", "files_posted": total}

    except AgentApiServerError as exc:
        # 5xx after retries (D-12) -- abort the walk and surface a 'failed' terminal PATCH.
        # NOTE: do NOT use .exception() in the path that re-PATCHes via the same broken
        # controller; if the controller is down, this PATCH may also raise -- but the
        # outer SAQ retry policy handles that. The terminal PATCH is best-effort.
        logger.exception("scan_directory: controller error after retries; aborting walk batch=%s", payload.batch_id)
        try:
            await api.patch_scan_batch(
                payload.batch_id,
                ScanBatchPatch(status="failed", error_message=f"Controller error: {exc}"),
            )
        except AgentApiServerError:
            logger.exception("scan_directory: terminal failed-PATCH also failed batch=%s", payload.batch_id)
        return {"status": "failed", "files_posted": total, "reason": "controller_5xx"}
