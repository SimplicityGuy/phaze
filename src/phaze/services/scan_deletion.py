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
  to 16 to add the ``stage_skip`` force-skip sidecar (phaze-6l74), then to 17 to
  purge stranded ``scheduling_ledger`` rows for the batch's files (phaze-u5dn).
  Note ``scheduling_ledger`` carries no FK to ``files`` -- this delete is by
  natural-id predicate (``payload->>'file_id'``), not FK order.
- The caller owns the transaction: this function does NOT commit. That keeps it
  composable and lets the endpoint commit the whole cascade atomically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast as typing_cast

from sqlalchemy import CursorResult, String, cast as sql_cast, delete, select
import structlog

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scan_batch import ScanBatch
from phaze.models.scheduling_ledger import SchedulingLedger
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

    Executes 17 ordered set-based deletes (child -> parent). Every statement is
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
    # phaze-8567: lock the ScanBatch row itself FIRST, before anything else. The phaze-q1ow
    # fix below locks only the batch's PRE-EXISTING file rows -- it does nothing for a brand
    # NEW FileRecord INSERT into this batch, because that INSERT takes only an implicit `FOR
    # KEY SHARE` on the *scan_batches* row (the parent of `FileRecord.batch_id`'s FK), and
    # nothing in the cascade locked that row until the final `DELETE FROM scan_batches` at the
    # very end. Under READ COMMITTED, a new file row committed after the files DELETE (step 15
    # below) scans its snapshot but before the `scan_batches` DELETE's RI check runs leaves a
    # live `files.batch_id` reference, aborting the whole 16-step transaction with a bare
    # ForeignKeyViolation -> 500. Taking `FOR UPDATE` on the ScanBatch row up front closes the
    # window the same way the file-row lock already closes it for child-table inserts: a
    # worker's `upsert_files` INSERT already in flight blocks on this row's lock until this
    # transaction ends (so it can never land mid-cascade), and a post-commit INSERT simply
    # FK-fails cheaply against the already-deleted batch (the correct, cheap place for that
    # race to resolve).
    await session.execute(select(ScanBatch.id).where(ScanBatch.id == batch_id).with_for_update())

    # Files belonging to this batch -- the scoping anchor for every child delete.
    files_of_batch = select(FileRecord.id).where(FileRecord.batch_id == batch_id)

    # phaze-q1ow: lock the batch's file rows FOR UPDATE before running any of the child
    # deletes. Every FK below is a bare `ForeignKey("files.id")` with NO `ondelete` (see the
    # module docstring), so the FINAL `DELETE FROM files` (step 15 below) runs Postgres' normal
    # RI NO-ACTION check -- and a still-running pipeline worker (metadata/analysis/
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
    #
    # phaze-zfxy6: this FOR UPDATE sweep and `upsert_files`' (routers/agent_files.py) multi-row
    # `INSERT ... ON CONFLICT DO UPDATE` are TWO independent multi-row lockers over an
    # overlapping row set (a rescan reassigning a completed batch's files to the agent's live
    # batch, while an operator deletes that completed batch). With no explicit order, this
    # sweep locked in heap/plan order while the upsert locks in VALUES order (the agent's
    # directory-walk order) -- two lockers visiting the same rows in different orders is the
    # classic ABBA deadlock: cascade holds row A waiting on row B while the upsert holds B
    # waiting on A. Postgres aborts one side after `deadlock_timeout`, either 500ing this whole
    # cascade or losing the agent's chunk. `original_path` is the only column both sides can
    # sort on (the cascade only has `batch_id`; the upsert's natural key is
    # `(agent_id, original_path)`), so order THIS lock acquisition by it -- `upsert_files` sorts
    # its deduped VALUES rows by the same column, giving both lockers one global acquisition
    # order. Whichever transaction reaches a given row first holds it and the other blocks on
    # that exact row; neither can be holding a "later" row the other needs, so the cycle cannot
    # form. Use a locally-ordered clone (`files_of_batch` itself stays unordered below -- it is
    # only ever used as an `IN (...)` subquery scoping predicate, where row order is
    # irrelevant and an ORDER BY would just be dead weight on the plan).
    #
    # This also closes the identical unordered acquisition an adversarial reviewer flagged at
    # the final `DELETE FROM files WHERE batch_id = :b` (below): that statement targets exactly
    # the row set this sweep already holds FOR UPDATE, in the SAME transaction. Postgres does
    # not re-acquire a lock its own transaction already holds, so that DELETE takes no new
    # locks and cannot re-open the cycle -- and the phaze-8567 ScanBatch FOR UPDATE above
    # blocks any INSERT that would grow this batch's file set between the two statements, so
    # the row set cannot drift in the meantime either.
    await session.execute(files_of_batch.order_by(FileRecord.original_path).with_for_update())

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
        # phaze-2mwyo: the DURABLE cloud-budget ledger. Its FK carries ON DELETE CASCADE (so it can never
        # block the files delete the way stage_skip once did), but it is deleted explicitly here anyway --
        # the cascade would remove the rows silently, and this list is also the rowcount report an operator
        # reads to see what a scan deletion actually took. Deleting a file legitimately erases its cloud
        # budget history: a re-scanned file is a NEW files.id and has genuinely never spent a cloud budget.
        (CloudBudget.__tablename__, delete(CloudBudget).where(CloudBudget.file_id.in_(files_of_batch))),
        # StageSkip (force-skip sidecar) FKs files.id with NO ON DELETE and is not deferrable. It is
        # the ONLY file sidecar with no undo/reaper, so a force-skipped file leaves a live stage_skip
        # row that blocks the files delete (ForeignKeyViolation -> 500 -> batch permanently undeletable).
        (StageSkip.__tablename__, delete(StageSkip).where(StageSkip.file_id.in_(files_of_batch))),
        # phaze-u5dn: scheduling_ledger rows are deliberately FK-free (models/scheduling_ledger.py --
        # "the row must survive even if its target row is mid-flight"), so this cascade otherwise never
        # touches them and a deleted file's ledger row is stranded forever. Every ledger row this
        # cascade can identify by natural id is file-keyed (`_KEY_BUILDERS` in
        # tasks/_shared/deterministic_key.py stores `payload["file_id"] = str(file_id)` for
        # process_file, extract_file_metadata, search_tracklist, push_file, s3_upload and
        # submit_cloud_job), so scope the delete to `payload->>'file_id'` matching one of this batch's
        # files. `recover_orphaned_work`'s done-set predicates derive from the very output tables this
        # cascade just deleted, so a stranded row can NEVER become domain-complete -- it is replayed on
        # every recovery pass (burning hours of essentia CPU for `process_file`), and its write-back
        # then FK-fails/404s against the missing FileRecord, so `clear_ledger_entry` is never reached
        # and the row survives to the next recovery cycle. Batch-shaped keys (e.g. a hypothetical
        # `scan_directory:<batch_id>`) are deliberately OUT of scope: `scan_directory` is absent from
        # `_KEY_BUILDERS`, so no such row is ever written.
        (
            SchedulingLedger.__tablename__,
            delete(SchedulingLedger).where(
                SchedulingLedger.payload["file_id"].astext.in_(select(sql_cast(FileRecord.id, String)).where(FileRecord.batch_id == batch_id))
            ),
        ),
        (FileRecord.__tablename__, delete(FileRecord).where(FileRecord.batch_id == batch_id)),
        (ScanBatch.__tablename__, delete(ScanBatch).where(ScanBatch.id == batch_id)),
    ]

    counts: dict[str, int] = {}
    for tablename, stmt in ordered:
        # A DELETE returns a CursorResult at runtime (exposing rowcount); the
        # execute() overload mypy selects only promises the base Result type.
        result = typing_cast("CursorResult[Any]", await session.execute(stmt.execution_options(synchronize_session=False)))
        counts[tablename] = result.rowcount

    logger.info("scan cascade deleted", batch_id=str(batch_id), **counts)
    return counts
