"""Ingestion service: directory scanning, hashing, classification, and bulk upsert."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import itertools
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
import unicodedata
import uuid

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.constants import BULK_INSERT_BATCH_SIZE, EXTENSION_MAP, FileCategory
from phaze.models.agent import LEGACY_AGENT_ID
from phaze.models.file import FileRecord, FileState
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.hashing import compute_sha256


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = structlog.get_logger(__name__)


def normalize_path(path: str) -> str:
    """NFC-normalize a Unicode path string."""
    return unicodedata.normalize("NFC", path)


def classify_file(filename: str) -> FileCategory:
    """Classify a file by its extension using the EXTENSION_MAP.

    Extension matching is case-insensitive.
    """
    suffix = Path(filename).suffix.lower()
    return EXTENSION_MAP.get(suffix, FileCategory.UNKNOWN)


def discover_and_hash_files(scan_path: str, batch_id: uuid.UUID) -> list[dict[str, Any]]:
    """Walk a directory tree and return file records for known extensions.

    Skips files with unknown extensions. NFC-normalizes all paths.
    Computes SHA-256 hashes using chunked reads. Handles unreadable
    files gracefully by logging a warning and skipping.
    """
    scan_root = Path(scan_path)
    records: list[dict[str, Any]] = []

    for dirpath, _dirnames, filenames in os.walk(scan_root, followlinks=False):
        for filename in filenames:
            category = classify_file(filename)
            if category == FileCategory.UNKNOWN:
                continue

            full_path = Path(dirpath) / filename
            try:
                file_size = full_path.stat().st_size
                sha256_hash = compute_sha256(full_path)
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", full_path, exc)
                continue

            normalized_path = normalize_path(str(full_path))
            normalized_filename = normalize_path(filename)
            file_ext = Path(filename).suffix.lower().lstrip(".")

            logger.debug("file discovered", path=normalized_path, size=file_size, ext=file_ext)
            records.append(
                {
                    "id": uuid.uuid4(),
                    "agent_id": LEGACY_AGENT_ID,
                    "sha256_hash": sha256_hash,
                    "original_path": normalized_path,
                    "original_filename": normalized_filename,
                    "current_path": normalized_path,
                    "file_type": file_ext,
                    "file_size": file_size,
                    "state": FileState.DISCOVERED,
                    "batch_id": batch_id,
                }
            )

    return records


async def bulk_upsert_files(
    session: AsyncSession,
    records: list[dict[str, Any]],
    batch_size: int = BULK_INSERT_BATCH_SIZE,
) -> int:
    """Bulk upsert file records into the database.

    Uses PostgreSQL INSERT ... ON CONFLICT DO UPDATE for resumability.
    Records are processed in batches to manage memory and transaction size.
    Returns total number of records upserted.
    """
    total = 0
    for batch in itertools.batched(records, batch_size, strict=False):
        batch_list = list(batch)
        stmt = pg_insert(FileRecord).values(batch_list)
        stmt = stmt.on_conflict_do_update(
            index_elements=["agent_id", "original_path"],  # composite UQ swapped in migration 013
            set_={
                "sha256_hash": stmt.excluded.sha256_hash,
                "file_size": stmt.excluded.file_size,
                "state": stmt.excluded.state,
                "batch_id": stmt.excluded.batch_id,
                "file_type": stmt.excluded.file_type,
            },
        )
        await session.execute(stmt)
        await session.commit()
        total += len(batch_list)
    return total


async def run_scan(
    scan_path: str,
    batch_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    queue: Any | None = None,  # noqa: ARG001 -- retained for caller/signature stability; Phase 35 D-06 removed the auto-enqueue that consumed it
) -> None:
    """Top-level scan orchestrator: create batch, discover files, persist.

    Creates a ScanBatch record, runs directory scanning via asyncio.to_thread,
    bulk upserts discovered files, and updates batch status.

    ``queue`` contract: Phase 35 (D-06) removed the per-discovery auto-enqueue of the
    metadata-extraction task — metadata extraction is operator-triggered ONLY
    (MANUAL-META). The ``queue`` parameter is retained for caller/signature stability
    (``routers/scan.py`` still passes a resolved per-agent queue) but is no longer used:
    discovery now persists rows only. Re-running extraction is an explicit operator action.

    Terminal completion is written in exactly two places in the codebase: here
    (the legacy application-server path) and the agent PATCH in
    ``routers/agent_scan_batches.py``. BOTH stamp ``completed_at`` on the
    COMPLETED/FAILED transition so the admin UI's elapsed timer freezes instead
    of climbing forever (incident 260609). The watcher / scan_live_set path
    writes only the non-terminal ``ScanStatus.LIVE`` and so needs no
    ``completed_at``. If a third terminal-status writer is ever added, it must
    stamp ``completed_at`` the same way.
    """
    started_at = time.monotonic()
    logger.info("scan started", batch_id=str(batch_id), path=scan_path)
    async with session_factory() as session:
        # Create scan batch record
        batch = ScanBatch(
            id=batch_id,
            agent_id=LEGACY_AGENT_ID,
            scan_path=scan_path,
            status=ScanStatus.RUNNING,
            total_files=0,
            processed_files=0,
            # PR4: stamp the heartbeat on create so the stall reaper does not
            # immediately consider a freshly-started legacy scan stalled.
            last_progress_at=datetime.now(UTC),
        )
        session.add(batch)
        await session.commit()

        try:
            # Run synchronous file discovery in a thread
            file_records = await asyncio.to_thread(discover_and_hash_files, scan_path, batch_id)

            # Update total files count
            await session.execute(update(ScanBatch).where(ScanBatch.id == batch_id).values(total_files=len(file_records)))
            await session.commit()

            # Bulk upsert discovered files
            upserted = await bulk_upsert_files(session, file_records)
            logger.info("scan progress", batch_id=str(batch_id), processed=upserted, total=len(file_records))

            # Phase 35 (D-06): NO auto-enqueue of the metadata-extraction task here.
            # Discovery persists rows only; metadata extraction is operator-triggered
            # (MANUAL-META). See the ``queue`` contract in this function's docstring.

            # Mark scan as completed
            await session.execute(
                update(ScanBatch)
                .where(ScanBatch.id == batch_id)
                .values(
                    status=ScanStatus.COMPLETED,
                    processed_files=upserted,
                    completed_at=datetime.now(UTC),
                    last_progress_at=datetime.now(UTC),
                )
            )
            await session.commit()
            logger.info(
                "scan completed",
                batch_id=str(batch_id),
                files=upserted,
                duration_s=round(time.monotonic() - started_at, 3),
            )

        except Exception as exc:
            logger.exception("scan failed", batch_id=str(batch_id), path=scan_path, error=str(exc))
            await session.execute(
                update(ScanBatch)
                .where(ScanBatch.id == batch_id)
                .values(
                    status=ScanStatus.FAILED,
                    error_message=str(exc),
                    completed_at=datetime.now(UTC),
                    last_progress_at=datetime.now(UTC),
                )
            )
            await session.commit()
            raise
