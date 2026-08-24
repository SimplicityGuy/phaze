"""PATCH /api/internal/agent/tag-writes/{log_id}[/before-snapshot] -- agent -> control tag-write callbacks (phaze-6bkk)."""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.enums.tag_write import TagWriteStatus
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.tag_write_log import TagWriteLog
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_tag_writes import (
    TagWriteBeforeSnapshotPayload,
    TagWriteBeforeSnapshotResponse,
    TagWriteResultPayload,
    TagWriteResultResponse,
)
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.scheduling_ledger import clear_ledger_entry


router = APIRouter(prefix="/api/internal/agent/tag-writes", tags=["agent-internal"])

# Mirrors the wire bound on TagWriteResultPayload.error_message. The column is unbounded ``Text``,
# so this is the DoS guard, not a column-width echo (parity with agent_metadata.py).
_ERROR_MESSAGE_MAX = 2000


@router.patch("/{log_id}", status_code=status.HTTP_200_OK, response_model=TagWriteResultResponse)
async def patch_tag_write(
    log_id: uuid.UUID,
    body: TagWriteResultPayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TagWriteResultResponse:
    """Move a ``queued`` ``TagWriteLog`` row to the terminal status the agent observed on disk.

    The control plane creates the row in ``queued`` and enqueues ``write_file_tags`` onto the
    owning agent's ``meta`` lane (``services.tag_writer.enqueue_tag_write``); the agent performs
    the mutagen write on the media mount the api container does not have (DIST-01) and reports
    here. ``before_tags`` can ONLY be captured agent-side -- it is the pre-write on-disk snapshot an
    undo re-applies (phaze-52qd) -- so it arrives with the result rather than being written at
    enqueue time.

    **Idempotent on ``log_id``** (Phase 28 D-15 discipline): the ``log_id`` is pre-minted by the
    control plane and carried in the task payload, so a SAQ retry PATCHes the SAME row. A row that
    has already left ``queued`` is NOT re-written -- the callback returns 200 with
    ``applied=false``. That matters beyond tidiness: the audit row is the undo anchor, and letting a
    late duplicate overwrite ``before_tags`` with a snapshot taken AFTER the first write landed
    would make undo restore the written tags instead of the original ones.

    ``before_tags`` is FIRST-WRITE-WINS (phaze-anrw4): if a snapshot already arrived -- via
    :func:`record_tag_write_before_snapshot` (the normal path) or an earlier call to this same
    endpoint -- this call's ``before_tags`` is discarded and the existing value is kept. A SAQ retry
    of ``write_file_tags`` re-extracts ``before_tags`` from disk on every attempt; if an earlier
    attempt's write already landed but its result callback never arrived, that re-extraction reads
    the ALREADY-WRITTEN state. Only the FIRST snapshot any attempt ever reports can be trusted as
    the true original, regardless of which attempt's callback happens to reach this endpoint.

    ``sha256_hash`` (phaze-2zeu0) refreshes ``FileRecord.sha256_hash`` from what the agent observed
    on disk AFTER the write, in the same transaction. The write rewrote the file's bytes, and that
    column is otherwise set once at ingest and never again -- see the inline comment at the write
    site for the two consumers that then fail permanently, and for why ``None`` leaves the column
    alone instead of nulling it.

    ``agent`` comes from the auth dep (token, never body -- AUTH-01) and the row is addressed by
    the PATH ``log_id`` only, so a forged body cannot redirect the write. The endpoint deliberately
    does NOT check that the row's file belongs to the calling agent: ``log_id`` is a server-minted
    UUID the agent only learns by being handed the job, which is the same unguessable-handle
    authorization the execution-log callbacks use.
    """
    if body.status == TagWriteStatus.QUEUED:
        # ``queued`` is the state this endpoint EXISTS to leave. Accepting it would silently strand
        # the row (and the file, which stays out of every terminal count) with no error anywhere.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tag-write callback must report a terminal status, not 'queued'",
        )

    # Row-level write lock (SELECT ... FOR UPDATE), not a plain PK SELECT: the engine runs at
    # PostgreSQL's default READ COMMITTED and this row carries no `version_id_col`, so a bare read
    # here is a TOCTOU -- two CONCURRENT duplicate PATCHes for the same log_id (e.g. an agent's
    # tenacity retry firing while the first request is still in flight server-side) can both
    # observe `queued`, both pass the guard below, and the second commit overwrites `before_tags`
    # with a snapshot taken AFTER the first write landed -- corrupting the undo anchor the
    # docstring above warns about. FOR UPDATE serializes the two: the second PATCH blocks on the
    # row lock until the first commits, then re-reads the now-terminal status and takes the no-op
    # branch. Same shape as agent_execution.py's patch_execution_log (D-15 / phaze-6zxs) and the
    # phaze-v0iy resolve_group fix.
    result = await session.execute(select(TagWriteLog).where(TagWriteLog.id == log_id).with_for_update())
    log_entry = result.scalar_one_or_none()
    if log_entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tag write log not found")

    if log_entry.status != TagWriteStatus.QUEUED:
        # Late / duplicate replay of a callback that already landed -- a no-op 200, never an error
        # (the agent would otherwise burn its tenacity retries on a permanent 409).
        return TagWriteResultResponse(agent_id=agent.id, log_id=log_id, status=TagWriteStatus(log_entry.status), applied=False)

    log_entry.status = body.status.value
    # phaze-anrw4: first-write-wins -- ``TagWriteLog.before_tags`` starts as a literal ``{}`` at
    # ``enqueue_tag_write`` and only ever gets keys from an actual snapshot (every core field,
    # ``None`` where absent -- see ``_extract_before_tags``), so "still ``{}``" unambiguously means
    # "no snapshot recorded yet". A retry's redundant re-extraction must never clobber an already
    # -- correct -- captured original.
    if not log_entry.before_tags:
        log_entry.before_tags = dict(body.before_tags)
    log_entry.discrepancies = body.discrepancies or None
    # Sanitize BEFORE truncating (stripping can only shorten, so the bound still holds): a NUL from
    # a mangled filename in an OSError string passes pydantic but aborts the transaction in Postgres
    # (CharacterNotInRepertoireError), which would roll the whole callback back and strand the row.
    log_entry.error_message = sanitize_pg_text(body.error_message)[:_ERROR_MESSAGE_MAX] if body.error_message else None
    # phaze-2zeu0: refresh the file's stored digest from what the agent OBSERVED on disk after the
    # write, in THIS SAME TRANSACTION as the terminal status. A tag write rewrites the file's bytes
    # (mutagen's `audio.save()`), and `FileRecord.sha256_hash` is otherwise written exactly once, at
    # ingest (`tasks/scan.py:218`), and never refreshed -- so without this every byte-verify against
    # that column fails PERMANENTLY for the rest of the file's life: `tasks/execution.py`'s pre-copy
    # verify raises "sha256 mismatch" on the file's very next execution, and the cloud lane's
    # `job_runner._verify_integrity_step` exits 11 with no retry. Every retry recomputes the same
    # real hash against the same stale column, so nothing in the system could ever clear it.
    #
    # SAME TRANSACTION IS THE POINT, not tidiness. The status write is what makes the tag write
    # durable and visible; if the hash landed separately, a crash between the two would leave a
    # write recorded as COMPLETED with a hash that no longer describes the file -- precisely the
    # state this bead exists to eliminate, reached by a narrower window.
    #
    # `None` means "not observed" (see the payload field), never "unchanged", so the column is left
    # alone rather than nulled -- `sha256_hash` is `nullable=False` and a NULL would additionally
    # collapse every such file into one bogus duplicate group under `services/dedup.py`'s
    # `GROUP BY sha256_hash` (Postgres groups all NULLs together).
    #
    # Reached only on the applied path: a late/duplicate replay returns above, because the first
    # callback already recorded the digest for this write.
    if body.sha256_hash is not None:
        await session.execute(
            update(FileRecord).where(FileRecord.id == log_entry.file_id).values(sha256_hash=body.sha256_hash, updated_at=func.now())
        )
    # phaze-yy9bk: clear the write_file_tags:<log_id> scheduling-ledger row in the SAME
    # transaction as the terminal write. No other code path clears it -- unlike every other
    # agent-side stage (agent_analysis.py / agent_metadata.py / agent_push.py / agent_s3), this
    # callback previously left it forever, so EVERY tag write (not just a broker-hiccup retry)
    # became a permanently orphaned ledger row that ``recover_orphaned_work`` would replay --
    # re-executing a real mutagen rewrite -- on every subsequent controller restart or manual
    # Recover, without limit. ``write_file_tags`` is keyed on ``log_id`` (deterministic_key.py),
    # not ``file_id``, so the key mirrors that exactly.
    await clear_ledger_entry(session, f"write_file_tags:{log_id}")
    await session.commit()

    return TagWriteResultResponse(agent_id=agent.id, log_id=log_id, status=body.status, applied=True)


@router.patch("/{log_id}/before-snapshot", status_code=status.HTTP_200_OK, response_model=TagWriteBeforeSnapshotResponse)
async def record_tag_write_before_snapshot(
    log_id: uuid.UUID,
    body: TagWriteBeforeSnapshotPayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TagWriteBeforeSnapshotResponse:
    """Durably record the pre-write on-disk snapshot, first-write-wins (phaze-anrw4).

    ``phaze.tasks.tag_write.write_file_tags`` calls this BEFORE it performs the mutating mutagen
    write, so the true original tags are captured independent of whether that attempt's write, or
    its later result callback (:func:`patch_tag_write`), ever succeeds. A SAQ retry re-invokes this
    same call with a snapshot re-extracted from (by then) the ALREADY-WRITTEN disk -- accepted only
    when no snapshot has been recorded yet, so the first attempt's capture always wins.

    Accepts only while the row is still ``queued`` -- a row that has left ``queued`` already has
    its final ``before_tags`` (set by :func:`patch_tag_write`, itself first-write-wins) and a late
    snapshot report has nothing left to protect. Same ``FOR UPDATE`` discipline as the result
    callback for the same TOCTOU reason: two snapshot reports for the same row (a genuine retry
    racing an in-flight first attempt) must serialize, not both observe "empty" and both write.
    """
    result = await session.execute(select(TagWriteLog).where(TagWriteLog.id == log_id).with_for_update())
    log_entry = result.scalar_one_or_none()
    if log_entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tag write log not found")

    if log_entry.status != TagWriteStatus.QUEUED or log_entry.before_tags:
        # Already terminal, or a snapshot already won -- always a safe no-op, never an error (the
        # agent must not burn tenacity retries on this call, and it does not gate the write itself).
        return TagWriteBeforeSnapshotResponse(agent_id=agent.id, log_id=log_id, applied=False)

    log_entry.before_tags = dict(body.before_tags)
    await session.commit()

    return TagWriteBeforeSnapshotResponse(agent_id=agent.id, log_id=log_id, applied=True)
