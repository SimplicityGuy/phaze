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

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast as typing_cast
import uuid

from sqlalchemy import CursorResult, String, cast as sql_cast, delete, exists, func, select
import structlog

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scan_batch import ScanBatch
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.set_profile import SetProfile
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.services.stage_status import cloud_busy_clause
from phaze.telemetry.pipeline import record_transition


if TYPE_CHECKING:
    from sqlalchemy import ColumnElement, Delete
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


def _scheduling_ledger_cascade_delete_stmt(file_scope: ColumnElement[bool]) -> Delete:
    """The scheduling_ledger step of the cascade (phaze-u5dn), RETURNING the deleted rows' functions.

    scheduling_ledger rows are deliberately FK-free (models/scheduling_ledger.py -- "the row must
    survive even if its target row is mid-flight"), so the FK cascade never touches them and a
    deleted file's ledger row would otherwise be stranded forever. Every ledger row this cascade can
    identify by natural id is file-keyed (``_KEY_BUILDERS`` in tasks/_shared/deterministic_key.py
    stores ``payload["file_id"] = str(file_id)`` for process_file, extract_file_metadata,
    search_tracklist, push_file, s3_upload and submit_cloud_job), so this scopes the delete to
    ``payload->>'file_id'`` matching one of this batch's files. ``recover_orphaned_work``'s done-set
    predicates derive from the very output tables this cascade just deleted, so a stranded row can
    NEVER become domain-complete -- it is replayed on every recovery pass (burning hours of essentia
    CPU for ``process_file``), and its write-back then FK-fails/404s against the missing FileRecord,
    so ``clear_ledger_entry`` is never reached and the row survives to the next recovery cycle.
    Batch-shaped keys (e.g. a hypothetical ``scan_directory:<batch_id>``) are deliberately OUT of
    scope: ``scan_directory`` is absent from ``_KEY_BUILDERS``, so no such row is ever written.

    phaze-qyqig: RETURNING ``function`` names the function of every row this DELETE actually
    removes, so the caller can count "resolved" once per row under its OWN stage label -- a batch's
    stranded rows can span several of the six file-keyed functions above, never just one. Pulled out
    to its own function (rather than inlined in the ``ordered`` list below) purely so the return
    type stays plain ``Delete`` -- ``RETURNING`` changes a statement's STATIC result-row type
    (``ReturningDelete[tuple[str]]``), which the list's shared ``list[tuple[str, Delete]]`` element
    type cannot express; every entry in ``ordered`` is executed identically via
    ``session.execute(...)``, which does not care about that distinction at runtime.
    """
    return typing_cast(
        "Delete",
        delete(SchedulingLedger)
        .where(SchedulingLedger.payload["file_id"].astext.in_(select(sql_cast(FileRecord.id, String)).where(file_scope)))
        .returning(SchedulingLedger.function),
    )


def _file_descendant_steps(file_scope: ColumnElement[bool]) -> list[tuple[str, Delete]]:
    """Every row hanging off the files ``file_scope`` selects, as ``(tablename, DELETE)`` steps in child -> parent order.

    Ends with the ``scheduling_ledger`` step and stops BEFORE the files themselves, so a caller appends
    its own ``files`` delete (and, for a scan, the ``scan_batches`` row). Shared by
    :func:`delete_scan_cascade` (``file_scope`` = one batch) and :func:`delete_file_cascade` (one file),
    so the two can never drift onto different child-table lists.
    """
    files_in_scope = select(FileRecord.id).where(file_scope)

    # Tracklist chain subqueries (4 levels deep). NULL-file_id tracklists are
    # excluded automatically: ``file_id IN (files_in_scope)`` never matches NULL.
    tracklists_in_scope = select(Tracklist.id).where(Tracklist.file_id.in_(files_in_scope))
    versions_in_scope = select(TracklistVersion.id).where(TracklistVersion.tracklist_id.in_(tracklists_in_scope))
    tracks_in_scope = select(TracklistTrack.id).where(TracklistTrack.version_id.in_(versions_in_scope))

    proposals_in_scope = select(RenameProposal.id).where(RenameProposal.file_id.in_(files_in_scope))

    # (tablename, statement) in verified child -> parent order. Each subquery
    # references only tables not yet deleted at that step.
    return [
        (DiscogsLink.__tablename__, delete(DiscogsLink).where(DiscogsLink.track_id.in_(tracks_in_scope))),
        (TracklistTrack.__tablename__, delete(TracklistTrack).where(TracklistTrack.version_id.in_(versions_in_scope))),
        (TracklistVersion.__tablename__, delete(TracklistVersion).where(TracklistVersion.tracklist_id.in_(tracklists_in_scope))),
        (Tracklist.__tablename__, delete(Tracklist).where(Tracklist.file_id.in_(files_in_scope))),
        (ExecutionLog.__tablename__, delete(ExecutionLog).where(ExecutionLog.proposal_id.in_(proposals_in_scope))),
        (RenameProposal.__tablename__, delete(RenameProposal).where(RenameProposal.file_id.in_(files_in_scope))),
        (AnalysisResult.__tablename__, delete(AnalysisResult).where(AnalysisResult.file_id.in_(files_in_scope))),
        (FileMetadata.__tablename__, delete(FileMetadata).where(FileMetadata.file_id.in_(files_in_scope))),
        (TagWriteLog.__tablename__, delete(TagWriteLog).where(TagWriteLog.file_id.in_(files_in_scope))),
        (
            FileCompanion.__tablename__,
            delete(FileCompanion).where(FileCompanion.media_id.in_(files_in_scope) | FileCompanion.companion_id.in_(files_in_scope)),
        ),
        # DedupResolution has TWO FKs to files.id (file_id + canonical_file_id). A
        # canonical_file_id can point at a file in a DIFFERENT batch, so scope by
        # BOTH directions (FileCompanion two-column precedent above): a row is
        # removed if either side lands in this batch's files.
        (
            DedupResolution.__tablename__,
            delete(DedupResolution).where(DedupResolution.file_id.in_(files_in_scope) | DedupResolution.canonical_file_id.in_(files_in_scope)),
        ),
        (CloudJob.__tablename__, delete(CloudJob).where(CloudJob.file_id.in_(files_in_scope))),
        # phaze-2mwyo: the DURABLE cloud-budget ledger. Its FK carries ON DELETE CASCADE (so it can never
        # block the files delete the way stage_skip once did), but it is deleted explicitly here anyway --
        # the cascade would remove the rows silently, and this list is also the rowcount report an operator
        # reads to see what a scan deletion actually took. Deleting a file legitimately erases its cloud
        # budget history: a re-scanned file is a NEW files.id and has genuinely never spent a cloud budget.
        (CloudBudget.__tablename__, delete(CloudBudget).where(CloudBudget.file_id.in_(files_in_scope))),
        # StageSkip (force-skip sidecar) FKs files.id with NO ON DELETE and is not deferrable. It is
        # the ONLY file sidecar with no undo/reaper, so a force-skipped file leaves a live stage_skip
        # row that blocks the files delete (ForeignKeyViolation -> 500 -> batch permanently undeletable).
        (StageSkip.__tablename__, delete(StageSkip).where(StageSkip.file_id.in_(files_in_scope))),
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
        (SchedulingLedger.__tablename__, _scheduling_ledger_cascade_delete_stmt(file_scope)),
    ]


async def _execute_ordered(session: AsyncSession, ordered: list[tuple[str, Delete]]) -> dict[str, int]:
    """Run each ordered DELETE and return ``{tablename: rows deleted}`` (``scheduling_ledger`` tallied per function)."""
    counts: dict[str, int] = {}
    for tablename, stmt in ordered:
        # A DELETE returns a CursorResult at runtime (exposing rowcount); the
        # execute() overload mypy selects only promises the base Result type.
        result = typing_cast("CursorResult[Any]", await session.execute(stmt.execution_options(synchronize_session=False)))
        if tablename == SchedulingLedger.__tablename__:
            # phaze-qyqig: the RETURNING clause added above names the function of every row this
            # statement actually deleted; tally "resolved" once per row, grouped by function so a
            # batch spanning several keyed functions attributes each correctly.
            deleted_functions = result.scalars().all()
            counts[tablename] = len(deleted_functions)
            for deleted_function, deleted_count in Counter(deleted_functions).items():
                record_transition(deleted_function, "resolved", count=deleted_count)
        else:
            counts[tablename] = result.rowcount

    return counts


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
    # form. Use a locally-ordered clone (the `IN (...)` scoping subqueries `_file_descendant_steps`
    # builds stay unordered -- row order is irrelevant there and an ORDER BY would just be dead
    # weight on the plan).
    #
    # This also closes the identical unordered acquisition an adversarial reviewer flagged at
    # the final `DELETE FROM files WHERE batch_id = :b` (below): that statement targets exactly
    # the row set this sweep already holds FOR UPDATE, in the SAME transaction. Postgres does
    # not re-acquire a lock its own transaction already holds, so that DELETE takes no new
    # locks and cannot re-open the cycle -- and the phaze-8567 ScanBatch FOR UPDATE above
    # blocks any INSERT that would grow this batch's file set between the two statements, so
    # the row set cannot drift in the meantime either.
    await session.execute(files_of_batch.order_by(FileRecord.original_path).with_for_update())

    ordered: list[tuple[str, Delete]] = [
        *_file_descendant_steps(FileRecord.batch_id == batch_id),
        (FileRecord.__tablename__, delete(FileRecord).where(FileRecord.batch_id == batch_id)),
        (ScanBatch.__tablename__, delete(ScanBatch).where(ScanBatch.id == batch_id)),
    ]

    counts = await _execute_ordered(session, ordered)
    logger.info("scan cascade deleted", batch_id=str(batch_id), **counts)
    return counts


async def delete_file_cascade(session: AsyncSession, file_id: uuid.UUID) -> dict[str, int]:
    """Delete ONE file row and every descendant row (phaze-oxn2m: retiring a stale watcher row).

    The single-file twin of :func:`delete_scan_cascade`, over the same :func:`_file_descendant_steps`
    list. Locks the file row ``FOR UPDATE`` first for the phaze-q1ow reason documented there: a
    pipeline writer's child INSERT takes ``FOR KEY SHARE`` on the file row, so it either lands
    before this cascade starts or FK-fails cheaply after it commits. Callers decide WHETHER a row
    may be retired (:func:`retention_blockers`); this only executes the decision. Does NOT commit.
    """
    await session.execute(select(FileRecord.id).where(FileRecord.id == file_id).with_for_update())
    ordered: list[tuple[str, Delete]] = [
        *_file_descendant_steps(FileRecord.id == file_id),
        (FileRecord.__tablename__, delete(FileRecord).where(FileRecord.id == file_id)),
    ]
    counts = await _execute_ordered(session, ordered)
    logger.info("file cascade deleted", file_id=str(file_id), **counts)
    return counts


# phaze-oxn2m: proposal statuses an operator has acted on. Only PENDING is machine state; every
# other status exists because a human approved or rejected it (FAILED/EXECUTED are both downstream
# of an approval). Spelled from ProposalStatus so a new member forces a decision here.
_REVIEWED_PROPOSAL_STATUSES: frozenset[str] = frozenset(status.value for status in ProposalStatus if status is not ProposalStatus.PENDING)


async def retention_blockers(session: AsyncSession, file: FileRecord, *, content_changed: bool, retiring: bool = False) -> list[str]:
    """Name every reason ``file`` must NOT be re-pointed or retired automatically (phaze-oxn2m).

    Empty means the row carries only machine-derived state, which a re-point keeps (same content)
    or :func:`invalidate_content_state` re-derives (changed content), and which a retirement may
    discard. Anything an operator reviewed is never rewritten or deleted on a guess -- the bead's
    acceptance says to report it instead:

    - ``relocated``: ``current_path`` differs from ``original_path``, so phaze's own execution
      already moved the file. A watcher move of it is most likely that execution being observed,
      and re-pointing ``original_path`` would overwrite the record of where it came from.
    - ``proposal:<status>``: a proposal an operator approved or rejected (or that was executed).
    - ``tag_write``: phaze wrote tags into this file at an operator's request.
    - ``dedup_resolution``: an operator resolved this file's duplicate group, or picked it as
      another file's keeper -- a changed hash moves it to a different group.
    - ``cloud_job:busy`` (only when ``content_changed``): the OLD bytes are staged or being
      analyzed off-host; resetting under a live burst would orphan it, and same-content bytes are
      still valid, so it blocks only a content change.
    - ``tracklist`` / ``companion_link`` (only when ``retiring``): :func:`delete_file_cascade`
      deletes a row's tracklists (with their versions, tracks and Discogs links) and its
      ``file_companions`` link rows -- never the companion FILES, but data the surviving twin does
      not carry. A re-point keeps both with the row, so they block only a retirement.
    """
    reasons: list[str] = []
    if file.current_path != file.original_path:
        reasons.append("relocated")
    proposal_statuses = (
        await session.execute(
            select(RenameProposal.status)
            .where(RenameProposal.file_id == file.id, RenameProposal.status.in_(_REVIEWED_PROPOSAL_STATUSES))
            .distinct()
            .order_by(RenameProposal.status)
        )
    ).scalars()
    reasons.extend(f"proposal:{status}" for status in proposal_statuses)
    if await session.scalar(select(exists().where(TagWriteLog.file_id == file.id))):
        reasons.append("tag_write")
    if await session.scalar(select(exists().where((DedupResolution.file_id == file.id) | (DedupResolution.canonical_file_id == file.id)))):
        reasons.append("dedup_resolution")
    if content_changed and await session.scalar(select(exists().where(FileRecord.id == file.id, cloud_busy_clause()))):
        reasons.append("cloud_job:busy")
    if retiring:
        if await session.scalar(select(exists().where(Tracklist.file_id == file.id))):
            reasons.append("tracklist")
        if await session.scalar(select(exists().where((FileCompanion.media_id == file.id) | (FileCompanion.companion_id == file.id)))):
            reasons.append("companion_link")
    return reasons


async def invalidate_content_state(session: AsyncSession, file_id: uuid.UUID) -> dict[str, int]:
    """Drop the per-file state computed from a file's OLD bytes so every stage re-runs (phaze-oxn2m).

    Called when a watcher move re-hashes a file to a different SHA-256: the row was ingested
    mid-write, so everything below was derived from a partial file. Each stage reads "done" from
    its own output table (``services/stage_status.py``), so deleting the output is what makes the
    stage eligible again:

    - ``analysis_window`` / ``analysis`` / ``set_profile``: analysis of the old audio, including a
      failure row (``failed_at``), which would otherwise read FAILED and never retry.
    - ``metadata``: tags and duration read from the old bytes, failure rows included.
    - ``proposals`` with status PENDING (and their execution rows, of which a pending proposal has
      none): proposed from the old metadata/analysis. Reviewed statuses never reach here --
      :func:`retention_blockers` stops the re-point first.
    - ``cloud_job`` (never busy here: :func:`retention_blockers` refused the re-point otherwise, and
      the caller holds the file row's lock) and ``cloud_budget``: a
      SUCCEEDED burst reads as analyzed, and budget spent failing on a truncated file must not
      ration the final file's analysis.
    - ``scheduling_ledger`` rows keyed to this file: cleared through the guarded
      :func:`~phaze.services.scheduling_ledger.clear_ledger_entry`, so a row whose job is still
      live in ``saq_jobs`` is left for that job (it was enqueued with the old path and fails or
      completes on its own; residual documented on the bead).

    Dedup needs no reset: duplicate groups are computed from ``files.sha256_hash``, which the
    caller has just rewritten, and a ``dedup_resolution`` blocks the re-point. Deliberately KEPT:
    ``stage_skip`` (an operator's skip decision), ``file_companions`` (directory pairing, not
    content), and the tracklist tables (matched from names and the 1001TL catalogue, and an
    approved tracklist is operator state). Does NOT commit.
    """
    pending_proposals = select(RenameProposal.id).where(RenameProposal.file_id == file_id, RenameProposal.status == ProposalStatus.PENDING.value)
    ordered: list[tuple[str, Delete]] = [
        (AnalysisWindow.__tablename__, delete(AnalysisWindow).where(AnalysisWindow.file_id == file_id)),
        (AnalysisResult.__tablename__, delete(AnalysisResult).where(AnalysisResult.file_id == file_id)),
        (SetProfile.__tablename__, delete(SetProfile).where(SetProfile.file_id == file_id)),
        (FileMetadata.__tablename__, delete(FileMetadata).where(FileMetadata.file_id == file_id)),
        (ExecutionLog.__tablename__, delete(ExecutionLog).where(ExecutionLog.proposal_id.in_(pending_proposals))),
        (RenameProposal.__tablename__, delete(RenameProposal).where(RenameProposal.id.in_(pending_proposals))),
        (CloudJob.__tablename__, delete(CloudJob).where(CloudJob.file_id == file_id)),
        (CloudBudget.__tablename__, delete(CloudBudget).where(CloudBudget.file_id == file_id)),
    ]
    counts = await _execute_ordered(session, ordered)
    ledger_keys = (
        (await session.execute(select(SchedulingLedger.key).where(SchedulingLedger.payload["file_id"].astext == str(file_id)))).scalars().all()
    )
    for key in ledger_keys:
        await clear_ledger_entry(session, key)
    remaining = await session.scalar(
        select(func.count()).select_from(SchedulingLedger).where(SchedulingLedger.payload["file_id"].astext == str(file_id))
    )
    counts[SchedulingLedger.__tablename__] = len(ledger_keys) - (remaining or 0)
    logger.info("file content state invalidated", file_id=str(file_id), **counts)
    return counts


# phaze-oxn2m: one-off cleanup of the stale rows watcher moves left behind before the move endpoint
# existed. Whether a path still exists can only be answered on the AGENT's filesystem, never here, so
# the operator command is three steps: candidates (control) -> existence check (agent) -> retire
# (control). See ``phaze backfill moved-twin-candidates`` in ``phaze.cli``.


async def moved_twin_candidates(session: AsyncSession, agent_id: str) -> dict[str, Any]:
    """Every group of ``agent_id``'s rows sharing a filename and size: the rows a move could have duplicated.

    A moved file keeps its name and size, so a stale row and the row its move created always land in
    one group. Returned as the JSON document the agent-side existence check annotates
    (``python -m phaze.agent_watcher check-paths``). ``path`` is ``current_path`` -- where the row says
    the file is now -- and ``scan_roots`` lets the checker refuse to judge a path whose root it cannot
    see. Read-only.
    """
    agent = await session.get(Agent, agent_id)
    if agent is None:
        msg = f"no agent {agent_id!r}"
        raise ValueError(msg)
    twin_key = (FileRecord.original_filename, FileRecord.file_size)
    shared = select(*twin_key).where(FileRecord.agent_id == agent_id).group_by(*twin_key).having(func.count() > 1).subquery()
    rows = (
        await session.execute(
            select(FileRecord.id, FileRecord.current_path, *twin_key)
            .join(shared, (FileRecord.original_filename == shared.c.original_filename) & (FileRecord.file_size == shared.c.file_size))
            .where(FileRecord.agent_id == agent_id)
            .order_by(*twin_key, FileRecord.current_path)
        )
    ).all()
    groups: dict[tuple[str, int], list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault((row.original_filename, row.file_size), []).append({"id": str(row.id), "path": row.current_path})
    return {"agent_id": agent_id, "scan_roots": list(agent.scan_roots or []), "groups": list(groups.values())}


@dataclass(slots=True)
class MovedTwinReport:
    """What ``retire_moved_twins`` found, and (with ``apply``) did. Every list holds ``(stale, twin)`` id pairs."""

    retire: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    same_content: int = 0
    kept_reviewed: list[tuple[uuid.UUID, uuid.UUID, list[str]]] = field(default_factory=list)
    absent_without_twin: int = 0
    absent_with_several_twins: int = 0
    unverifiable_groups: int = 0
    changed_since_check: int = 0


def _parent_name(path: str) -> str:
    return PurePosixPath(path).parent.name


async def retire_moved_twins(session: AsyncSession, agent_id: str, checked: dict[str, Any], *, apply: bool) -> MovedTwinReport:
    """Retire each row whose file is gone from the agent's disk and whose moved-to twin is there (phaze-oxn2m).

    ``checked`` is :func:`moved_twin_candidates`' document after the agent annotated every member
    with ``exists`` (``True``/``False``, or ``None`` when no scan root it lies under is mounted). A
    row reading ``exists: False`` is stale, and its twin is the ONE member reading ``True`` -- the row
    its move created, which is kept. When several members are present, only those whose parent
    directory has the stale row's parent directory NAME are candidates: a release directory keeps
    its name when it is moved, while a fixed-size stamp file of one name sits in thousands of
    releases (measured 2026-10-06: a 3,424-row ``.nfo`` group). Anything else -- no candidate,
    several, or any member of the group the agent could not judge -- is counted and left untouched.
    A stale row carrying operator-reviewed state (:func:`retention_blockers`) is reported, never
    deleted.

    Idempotent: a stale row already retired, or a pair whose ``current_path`` changed after the
    check, no longer matches the document and is skipped. Without ``apply`` nothing is written; with
    it each stale row goes through :func:`delete_file_cascade`. Does NOT commit.
    """
    if checked.get("agent_id") != agent_id:
        msg = f"the checked document is for agent {checked.get('agent_id')!r}, not {agent_id!r}"
        raise ValueError(msg)
    report = MovedTwinReport()
    for group in checked["groups"]:
        members = {uuid.UUID(member["id"]): member["path"] for member in group}
        if any(member.get("exists") is None for member in group):
            report.unverifiable_groups += 1
            continue
        present = [uuid.UUID(member["id"]) for member in group if member["exists"] is True]
        for member in group:
            if member["exists"] is not False:
                continue
            twins = (
                present if len(present) <= 1 else [file_id for file_id in present if _parent_name(members[file_id]) == _parent_name(member["path"])]
            )
            if len(twins) != 1:
                if present:
                    report.absent_with_several_twins += 1
                else:
                    report.absent_without_twin += 1
                continue
            await _retire_pair(session, agent_id, (uuid.UUID(member["id"]), twins[0]), members, report, apply=apply)
    return report


async def _retire_pair(
    session: AsyncSession, agent_id: str, pair: tuple[uuid.UUID, uuid.UUID], paths: dict[uuid.UUID, str], report: MovedTwinReport, *, apply: bool
) -> None:
    """Re-read one (stale, twin) pair under lock, then retire the stale row unless it changed or carries reviewed state."""
    query = select(FileRecord).where(FileRecord.agent_id == agent_id, FileRecord.id.in_(pair)).order_by(FileRecord.original_path)
    records = {record.id: record for record in (await session.execute(query.with_for_update() if apply else query)).scalars()}
    if len(records) != len(pair) or any(records[file_id].current_path != paths[file_id] for file_id in pair):
        report.changed_since_check += 1
        return
    stale, twin = records[pair[0]], records[pair[1]]
    blockers = await retention_blockers(session, stale, content_changed=True, retiring=True)
    if blockers:
        report.kept_reviewed.append((stale.id, twin.id, blockers))
        return
    report.retire.append((stale.id, twin.id))
    report.same_content += stale.sha256_hash == twin.sha256_hash
    if apply:
        logger.info(
            "retire_moved_twins: retiring stale row",
            agent_id=agent_id,
            file_id=str(stale.id),
            old_path=stale.current_path,
            new_path=twin.current_path,
            kept_file_id=str(twin.id),
        )
        await delete_file_cascade(session, stale.id)
