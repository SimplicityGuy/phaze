"""Ordered, set-based transactional cascade for deleting a scan batch (PR5).

``delete_scan_cascade`` removes a ``ScanBatch`` and EVERY row that transitively
hangs off its files, in a single transaction, scoped strictly to that batch.

Why application-level instead of DB ``ON DELETE CASCADE``: most of the FK columns
in this schema were declared with no ``ondelete`` rule (see CLAUDE.md tech-stack /
the verified FK DAG in the PR plan), so the database will not cascade for us, and
adding cascade rules now would require a migration. An explicit ordered cascade is
also self-documenting and does not silently depend on engine behavior.

Design choices:
- Set-based ``DELETE ... WHERE col IN (SELECT ...)`` with nested subqueries -- a
  scan can hold tens of thousands of files, so we never load rows into the ORM
  identity map. ``synchronize_session=False`` is required for bulk deletes that
  bypass the identity map.
- Child -> parent ordering so every subquery references only tables not yet
  deleted at that step (verified order in the PR plan's 15-step block; extended
  from 13 to add the ``dedup_resolution`` and ``cloud_job`` file sidecars, then
  to 16 to add the ``stage_skip`` force-skip sidecar -- phaze-6l74).
- The caller owns the transaction: this function does NOT commit. That keeps it
  composable and lets the endpoint commit the whole cascade atomically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, select
import structlog

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scan_batch import ScanBatch
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    import uuid

    from sqlalchemy import Delete
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def delete_scan_cascade(session: AsyncSession, batch_id: uuid.UUID) -> dict[str, int]:
    """Delete ``batch_id`` and every descendant row, scoped strictly to its files.

    Executes 16 ordered set-based deletes (child -> parent). Every statement is
    scoped to the files of THIS batch via nested ``SELECT`` subqueries, so no
    other batch's data is ever touched. The ``dedup_resolution`` step is scoped by
    BOTH of its FK columns (``file_id`` OR ``canonical_file_id``), so a canonical
    file living in another batch never leaves a dangling pointer into this one.
    Does NOT commit -- the caller owns the transaction.

    Args:
        session: Active async session whose transaction the caller will commit.
        batch_id: The ScanBatch primary key to delete.

    Returns:
        A dict mapping each affected ``__tablename__`` to the number of rows
        deleted from it (``result.rowcount`` per statement).
    """
    # Files belonging to this batch -- the scoping anchor for every child delete.
    files_of_batch = select(FileRecord.id).where(FileRecord.batch_id == batch_id)

    # phaze-q1ow: lock the batch's file rows FOR UPDATE before running any of the child
    # deletes. Every FK below is a bare `ForeignKey("files.id")` with NO `ondelete` (see the
    # module docstring), so the FINAL `DELETE FROM files` (step 15 below) runs Postgres' normal
    # RI NO-ACTION check -- and a still-running pipeline worker (fingerprint/metadata/analysis/
    # proposal) can legitimately commit a NEW child row referencing one of these files AFTER an
    # earlier step here deletes that table's existing rows but BEFORE the files delete runs,
    # leaving a freshly-committed row referencing a file this transaction is about to delete.
    # That raises ForeignKeyViolation on the files DELETE and aborts the WHOLE cascade.
    #
    # Any INSERT of a row with an FK to files.id takes an implicit `FOR KEY SHARE` lock on the
    # referenced file row to enforce RI -- which conflicts with `FOR UPDATE`. Acquiring `FOR
    # UPDATE` on every file row of this batch FIRST, inside this same transaction, means: a
    # worker's insert that arrives after commit simply FK-fails against the already-deleted file
    # (the correct, cheap place for that race to resolve); a worker's insert already in flight
    # blocks on the file row's lock until ITS transaction ends, so this cascade's own `FOR
    # UPDATE` acquisition waits for it -- never racing partway through the ordered deletes. This
    # does not eliminate the wasted-retry cost for the losing side, it only serializes the
    # cascade against the read window so `DELETE FROM files` can never observe a row committed
    # mid-cascade (see the fix's own longer-term note: gating deletion on no live/queued jobs for
    # the batch's files would remove the retry cost entirely, but is a larger scheduling change).
    await session.execute(files_of_batch.with_for_update())

    # Tracklist chain subqueries (4 levels deep). NULL-file_id tracklists are
    # excluded automatically: ``file_id IN (files_of_batch)`` never matches NULL.
    tracklists_of_batch = select(Tracklist.id).where(Tracklist.file_id.in_(files_of_batch))
    versions_of_batch = select(TracklistVersion.id).where(TracklistVersion.tracklist_id.in_(tracklists_of_batch))
    tracks_of_batch = select(TracklistTrack.id).where(TracklistTrack.version_id.in_(versions_of_batch))

    proposals_of_batch = select(RenameProposal.id).where(RenameProposal.file_id.in_(files_of_batch))

    # (tablename, statement) in verified child -> parent order. Each subquery
    # references only tables not yet deleted at that step.
    ordered: list[tuple[str, Delete]] = [
        (DiscogsLink.__tablename__, delete(DiscogsLink).where(DiscogsLink.track_id.in_(tracks_of_batch))),
        (TracklistTrack.__tablename__, delete(TracklistTrack).where(TracklistTrack.version_id.in_(versions_of_batch))),
        (TracklistVersion.__tablename__, delete(TracklistVersion).where(TracklistVersion.tracklist_id.in_(tracklists_of_batch))),
        (Tracklist.__tablename__, delete(Tracklist).where(Tracklist.file_id.in_(files_of_batch))),
        (ExecutionLog.__tablename__, delete(ExecutionLog).where(ExecutionLog.proposal_id.in_(proposals_of_batch))),
        (RenameProposal.__tablename__, delete(RenameProposal).where(RenameProposal.file_id.in_(files_of_batch))),
        (FingerprintResult.__tablename__, delete(FingerprintResult).where(FingerprintResult.file_id.in_(files_of_batch))),
        (AnalysisResult.__tablename__, delete(AnalysisResult).where(AnalysisResult.file_id.in_(files_of_batch))),
        (FileMetadata.__tablename__, delete(FileMetadata).where(FileMetadata.file_id.in_(files_of_batch))),
        (TagWriteLog.__tablename__, delete(TagWriteLog).where(TagWriteLog.file_id.in_(files_of_batch))),
        (
            FileCompanion.__tablename__,
            delete(FileCompanion).where(FileCompanion.media_id.in_(files_of_batch) | FileCompanion.companion_id.in_(files_of_batch)),
        ),
        # DedupResolution has TWO FKs to files.id (file_id + canonical_file_id). A
        # canonical_file_id can point at a file in a DIFFERENT batch, so scope by
        # BOTH directions (FileCompanion two-column precedent above): a row is
        # removed if either side lands in this batch's files.
        (
            DedupResolution.__tablename__,
            delete(DedupResolution).where(DedupResolution.file_id.in_(files_of_batch) | DedupResolution.canonical_file_id.in_(files_of_batch)),
        ),
        (CloudJob.__tablename__, delete(CloudJob).where(CloudJob.file_id.in_(files_of_batch))),
        # StageSkip (force-skip sidecar) FKs files.id with NO ON DELETE and is not deferrable. It is
        # the ONLY file sidecar with no undo/reaper, so a force-skipped file leaves a live stage_skip
        # row that blocks the files delete (ForeignKeyViolation -> 500 -> batch permanently undeletable).
        (StageSkip.__tablename__, delete(StageSkip).where(StageSkip.file_id.in_(files_of_batch))),
        (FileRecord.__tablename__, delete(FileRecord).where(FileRecord.batch_id == batch_id)),
        (ScanBatch.__tablename__, delete(ScanBatch).where(ScanBatch.id == batch_id)),
    ]

    counts: dict[str, int] = {}
    for tablename, stmt in ordered:
        # A DELETE returns a CursorResult at runtime (exposing rowcount); the
        # execute() overload mypy selects only promises the base Result type.
        result = cast("CursorResult[Any]", await session.execute(stmt.execution_options(synchronize_session=False)))
        counts[tablename] = result.rowcount

    logger.info("scan cascade deleted", batch_id=str(batch_id), **counts)
    return counts
