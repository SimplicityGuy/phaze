"""Execution service - copy-verify-delete with write-ahead audit logging.

Implements the critical safety layer for file renames:
1. Copy file to destination
2. Verify SHA256 hash matches
3. Delete original only after verification

Every operation is logged to ExecutionLog BEFORE execution (write-ahead per EXE-02).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import shutil
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileState
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord

logger = logging.getLogger(__name__)

_HASH_CHUNK_SIZE = 8192


def compute_sha256(file_path: Path) -> str:
    """Compute SHA256 hex digest of a file using chunked reads.

    Args:
        file_path: Path to the file to hash.

    Returns:
        64-character lowercase hex digest string.
    """
    sha256 = hashlib.sha256()
    with Path.open(file_path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK_SIZE):
            sha256.update(chunk)
    return sha256.hexdigest()


async def log_operation(
    session: AsyncSession,
    proposal_id: uuid.UUID,
    operation: str,
    source_path: str,
    destination_path: str,
) -> ExecutionLog:
    """Create a write-ahead ExecutionLog entry with IN_PROGRESS status.

    The entry is committed immediately so it persists even if the operation crashes.

    Args:
        session: Async database session.
        proposal_id: ID of the RenameProposal being executed.
        operation: Operation type ("copy", "verify", "delete").
        source_path: Source file path.
        destination_path: Destination file path.

    Returns:
        The created ExecutionLog entry.
    """
    entry = ExecutionLog(
        id=uuid.uuid4(),
        proposal_id=proposal_id,
        operation=operation,
        source_path=source_path,
        destination_path=destination_path,
        sha256_verified=False,
        status=ExecutionStatus.IN_PROGRESS,
    )
    session.add(entry)
    await session.commit()
    return entry


async def complete_operation(
    session: AsyncSession,
    log_entry: ExecutionLog,
    *,
    sha256_verified: bool = False,
    error_message: str | None = None,
) -> None:
    """Update an ExecutionLog entry to COMPLETED or FAILED.

    Args:
        session: Async database session.
        log_entry: The log entry to update.
        sha256_verified: Whether SHA256 verification passed.
        error_message: Error message if operation failed (sets status to FAILED).
    """
    if error_message is not None:
        log_entry.status = ExecutionStatus.FAILED
        log_entry.error_message = error_message
    else:
        log_entry.status = ExecutionStatus.COMPLETED

    log_entry.sha256_verified = sha256_verified
    await session.commit()


async def get_approved_proposals(session: AsyncSession) -> list[RenameProposal]:
    """Get all approved proposals with eagerly loaded file relationships.

    Args:
        session: Async database session.

    Returns:
        List of approved RenameProposal objects with loaded FileRecord.
    """
    stmt = (
        select(RenameProposal)
        .where(RenameProposal.status == ProposalStatus.APPROVED)
        .options(selectinload(RenameProposal.file))
        .order_by(RenameProposal.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def execute_single_file(
    session: AsyncSession,
    proposal: RenameProposal,
    file_record: FileRecord,
) -> bool:
    """Execute a single file rename via copy-verify-delete.

    This is the core safety function. Steps:
    1. COPY: Copy source to destination (preserving metadata)
    2. VERIFY: Compute SHA256 of copy and compare to original hash
    3. DELETE: Remove original file only after verification

    Every step is logged to ExecutionLog BEFORE execution (write-ahead).
    On hash mismatch, the bad copy is deleted and the original is preserved.
    On delete failure, the FileRecord is still updated since the copy is valid.

    Args:
        session: Async database session.
        proposal: The RenameProposal to execute.
        file_record: The FileRecord for the file being renamed.

    Returns:
        True if execution succeeded, False otherwise.
    """
    source = Path(file_record.current_path)
    destination = source.parent / proposal.proposed_filename

    source_str = str(source)
    dest_str = str(destination)

    # Guard: destination must not already exist
    if destination.exists():
        log_entry = await log_operation(session, proposal.id, "copy", source_str, dest_str)
        await complete_operation(session, log_entry, error_message="Destination already exists")
        return False

    # Step 1: COPY
    copy_log = await log_operation(session, proposal.id, "copy", source_str, dest_str)
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        await complete_operation(session, copy_log, error_message=str(exc))
        return False
    await complete_operation(session, copy_log)

    # Step 2: VERIFY
    verify_log = await log_operation(session, proposal.id, "verify", source_str, dest_str)
    dest_hash = compute_sha256(destination)
    if dest_hash != file_record.sha256_hash:
        error_msg = f"SHA256 mismatch: expected {file_record.sha256_hash}, got {dest_hash}"
        await complete_operation(session, verify_log, error_message=error_msg)
        # Delete bad copy (per D-05)
        try:
            destination.unlink()
        except OSError:
            logger.warning("Failed to delete corrupted copy at %s", destination)
        file_record.state = FileState.FAILED
        await session.commit()
        return False
    await complete_operation(session, verify_log, sha256_verified=True)

    # Step 3: DELETE original
    delete_log = await log_operation(session, proposal.id, "delete", source_str, dest_str)
    try:
        Path.unlink(source)
        await complete_operation(session, delete_log)
    except OSError as exc:
        await complete_operation(session, delete_log, error_message=str(exc))
        # Copy is good, so we still update the file record

    # Update FileRecord
    file_record.current_path = dest_str
    file_record.state = FileState.EXECUTED
    await session.commit()

    return True
