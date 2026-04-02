"""Ingestion service: directory scanning, hashing, classification, and bulk upsert."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
import unicodedata
import uuid

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.constants import BULK_INSERT_BATCH_SIZE, EXTENSION_MAP, HASH_CHUNK_SIZE, FileCategory
from phaze.models.file import FileRecord, FileState
from phaze.models.scan_batch import ScanBatch, ScanStatus


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = logging.getLogger(__name__)


def normalize_path(path: str) -> str:
    """NFC-normalize a Unicode path string."""
    return unicodedata.normalize("NFC", path)


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of a file using chunked reads.

    Reads the file in HASH_CHUNK_SIZE (64KB) chunks to avoid loading
    entire files into memory.
    """
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


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

            records.append(
                {
                    "id": uuid.uuid4(),
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
            index_elements=["original_path"],
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
    queue: Any | None = None,
) -> None:
    """Top-level scan orchestrator: create batch, discover files, persist.

    Creates a ScanBatch record, runs directory scanning via asyncio.to_thread,
    bulk upserts discovered files, and updates batch status.
    """
    async with session_factory() as session:
        # Create scan batch record
        batch = ScanBatch(
            id=batch_id,
            scan_path=scan_path,
            status=ScanStatus.RUNNING,
            total_files=0,
            processed_files=0,
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

            # Auto-enqueue tag extraction for newly discovered files (per D-09)
            if queue is not None and file_records:
                extractable_categories = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})
                for record in file_records:
                    ext = "." + record.get("file_type", "").lower()
                    category = EXTENSION_MAP.get(ext, FileCategory.UNKNOWN)
                    if category in extractable_categories:
                        await queue.enqueue("extract_file_metadata", file_id=str(record["id"]))
                logger.info("Auto-enqueued tag extraction for %d files in batch %s", len(file_records), batch_id)

            # Mark scan as completed
            await session.execute(
                update(ScanBatch)
                .where(ScanBatch.id == batch_id)
                .values(
                    status=ScanStatus.COMPLETED,
                    processed_files=upserted,
                )
            )
            await session.commit()

        except Exception as exc:
            logger.exception("Scan failed for path %s", scan_path)
            await session.execute(
                update(ScanBatch)
                .where(ScanBatch.id == batch_id)
                .values(
                    status=ScanStatus.FAILED,
                    error_message=str(exc),
                )
            )
            await session.commit()
            raise
