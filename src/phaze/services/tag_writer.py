"""Tag writer service -- the CONTROL-PLANE half: audit row + dispatch to the owning agent.

phaze-6bkk (DIST-01). This module used to do the mutagen write itself, inside the api process,
against ``FileRecord.current_path``. That path is the FILE SERVER's absolute archive path, and the
``api`` container mounts no media at all ("api + worker have NO music/model/output file mounts",
docker-compose.yml) -- so every tag write, undo, and bulk submit in the documented production
topology failed with ``[Errno 2] No such file or directory`` and rendered a toast blaming file
permissions. Two shipped operator-facing surfaces were 100% non-functional.

The write now runs where the file is. :func:`enqueue_tag_write` creates the ``TagWriteLog`` audit
row in the ``queued`` state and enqueues ``write_file_tags`` onto the owning agent's ``meta`` lane
via ``AgentTaskRouter.enqueue_for_file`` -- the same shape ``execute_approved_batch`` already uses
for the move stage. The agent runs :mod:`phaze.services.tag_write_disk` against its mount and
PATCHes the terminal status back through ``/api/internal/agent/tag-writes/{log_id}``.

The pure on-disk helpers (``write_tags`` / ``verify_write`` / ``_extract_before_tags`` /
``write_and_verify_sync``) moved to :mod:`phaze.services.tag_write_disk`, which is import-safe for
the agent worker (D-25). They are re-exported here so existing importers are unchanged; call sites
that actually perform I/O must import from the disk module so it is obvious which process they run
in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
import uuid

from sqlalchemy import func, select
import structlog

from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.schemas.agent_tasks import WriteFileTagsPayload
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.stage_status import is_applied

# Re-exported for back-compat: the disk-side helpers now live in the agent-importable module.
from phaze.services.tag_write_disk import (  # noqa: F401  -- re-export
    _CORE_TAG_FIELDS,
    _extract_before_tags,
    _write_id3,
    _write_mp4,
    _write_vorbis,
    verify_write,
    write_and_verify_sync,
    write_tags,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.services.agent_task_router import AgentTaskRouter

logger = structlog.get_logger(__name__)


class TagWriteAlreadyQueuedError(RuntimeError):
    """A tag write is already ``queued`` for this file -- refuse a second concurrent dispatch.

    phaze-lwqk: a per-file mutual-exclusion lock inside the mutagen write is no longer possible --
    phaze-6bkk moved the disk write onto the agent, and DIST-04 gives the agent NO database access
    at all, so there is nowhere agent-side to take a DB lock. The guard moves control-side instead,
    to the one place both a double-click and a per-file write racing the bulk loop's dispatch of the
    SAME file must still pass through: deciding whether to create a new ``queued`` row. Without it,
    two ``write_file_tags`` jobs for the same file could run concurrently on the agent -- two
    mutagen full-file rewrites racing each other, a real corruption risk for an irreplaceable
    archive file.
    """


class TagWriteTarget(Protocol):
    """Structural subset of ``FileRecord`` :func:`enqueue_tag_write` actually touches.

    phaze-o2ln: the bulk tag-write loop stopped passing live ``FileRecord`` ORM instances across a
    per-file ``session.rollback()`` (which expires every attribute of every object still in the
    identity map, not just the one that failed) and now passes a plain, non-ORM snapshot instead
    (``routers.tags._BulkCandidate``). This Protocol is the minimal surface both a real
    ``FileRecord`` and that snapshot satisfy, so this function works unchanged for either caller --
    including ``.agent_id``, which ``enqueue_for_file``/``WriteFileTagsPayload`` route on.
    """

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def agent_id(self) -> str: ...

    @property
    def current_path(self) -> str: ...


async def enqueue_tag_write(
    session: AsyncSession,
    task_router: AgentTaskRouter,
    file_record: TagWriteTarget,
    proposed_tags: dict[str, str | int | list[str] | None],
    source: str,
    *,
    reviewed_before_tags: dict[str, object] | None = None,
    review_source_versions: dict[str, object] | None = None,
) -> TagWriteLog:
    """Create the audit row and dispatch the write to the file's OWNING agent.

    Args:
        session: Async database session.
        task_router: ``app.state.task_router`` -- the per-agent, per-lane SAQ enqueuer.
        file_record: The file to write tags to (must be applied -- an executed proposal exists). A
            live ``FileRecord`` or any :class:`TagWriteTarget`-shaped snapshot (phaze-o2ln).
        proposed_tags: Dict of proposed tag values.
        source: Source of the proposal ("tracklist", "metadata", "manual_edit", "undo", ...).

    Returns:
        The created ``TagWriteLog`` entry: ``queued`` when the job was handed to the agent,
        ``failed`` (with ``error_message``) when the enqueue itself could not be completed.

    Raises:
        ValueError: If the file is not applied (no executed proposal -- READ-05 / D-01).
        TagWriteAlreadyQueuedError: If this file already has an unresolved ``queued`` row
            (phaze-lwqk) -- refuses the second concurrent dispatch rather than racing two
            ``write_file_tags`` jobs against the same file on the agent.

    ``queued`` is intentionally NOT in ``_TERMINAL_TAGWRITE_STATUSES``: a dispatched-but-unreported
    write must keep its file in the tag-write candidate window, so an agent that never reports
    leaves a visibly-stuck row rather than silently evicting the file forever.

    Routing goes to ``file_record.agent_id`` -- the agent that reported the file -- never to
    "some live agent" (phaze-c9w9): ``current_path`` only means anything on the mount it came from,
    and the composite ``(agent_id, original_path)`` key explicitly models the same path existing
    under two different agents as two different files.
    """
    if not await is_applied(session, file_record.id):
        msg = "Only executed files can have tags written"
        raise ValueError(msg)

    # phaze-lwqk: serialize the enqueue DECISION for THIS file -- see TagWriteAlreadyQueuedError's
    # docstring for why the guard lives here rather than around the (now agent-side) disk write.
    # xact-scoped: this function commits once, immediately after the check+insert below, so the
    # lock's lifetime matches exactly the transaction that decides whether a new row gets created --
    # a concurrent caller either blocks until that commit and then correctly sees the row (refused),
    # or got here first and refuses THIS caller instead. Matches the xact-lock convention used
    # everywhere else in the tree (e.g. routers/agent_push.py:325).
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"tagwrite:{file_record.id}"))))

    already_queued = await session.execute(
        select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_record.id, TagWriteLog.status == TagWriteStatus.QUEUED.value)
    )
    if already_queued.scalar():
        msg = f"a tag write is already queued for file {file_record.id}"
        raise TagWriteAlreadyQueuedError(msg)

    log_entry = TagWriteLog(
        id=uuid.uuid4(),
        file_id=file_record.id,
        # The pre-write on-disk snapshot can only be read where the file is, so it arrives with the
        # agent's result callback. Empty until then -- never a lie about what was on disk.
        before_tags={},
        after_tags=proposed_tags,
        reviewed_before_tags=reviewed_before_tags or {},
        review_source_versions=review_source_versions or {},
        source=source,
        status=TagWriteStatus.QUEUED.value,
        discrepancies=None,
        error_message=None,
    )
    session.add(log_entry)
    await session.flush()
    # phaze-ysnp: commit the QUEUED row NOW -- durably, before the dispatch below, not merely
    # flushed. enqueue_for_file hands the job to a REAL external system (the agent's SAQ queue, a
    # separate Postgres connection/transaction from `session`), which can pick the job up and start
    # running before this transaction would otherwise have committed. Without this commit, a crash
    # (or simply a caller that never reaches its own commit) between here and there strands an
    # already-dispatched job with NO row for the agent's callback to PATCH: exactly the "audit row
    # lost while the mutation stands" failure phaze-ysnp closed for the pre-phaze-6bkk single-process
    # write, now recurring one hop later at the enqueue boundary instead of the disk-write boundary.
    await session.commit()

    try:
        # phaze-o2ln: ``enqueue_for_agent`` directly, not the ``FileRecord``-typed
        # ``enqueue_for_file`` convenience wrapper -- it only ever does
        # ``enqueue_for_agent(agent_id=file_record.agent_id, ...)`` internally, and calling it
        # directly here lets ``file_record`` stay :class:`TagWriteTarget`-typed (a live
        # ``FileRecord`` OR the bulk loop's plain snapshot) instead of narrowing back to a
        # nominal ``FileRecord``.
        await task_router.enqueue_for_agent(
            agent_id=file_record.agent_id,
            task_name="write_file_tags",
            payload=WriteFileTagsPayload(
                log_id=log_entry.id,
                file_id=file_record.id,
                agent_id=file_record.agent_id,
                file_path=file_record.current_path,
                tags=proposed_tags,
            ),
        )
    except AmbiguousEnqueueError:
        # phaze-9f82r: the broker connection was already live when this raised, so the
        # ``saq_jobs`` INSERT (and the scheduling-ledger row the before_enqueue hook writes ahead
        # of it) may already be durably committed even though this call never got its ack --
        # SAQ's Postgres broker forces autocommit and runs a separate NOTIFY round trip after the
        # INSERT (see ``AmbiguousEnqueueError``). Downgrading to FAILED here would let the
        # phaze-lwqk guard wave through an operator retry (FAILED is not QUEUED), minting a
        # SECOND queued row + job for the SAME file while a possibly-live ghost job from THIS
        # attempt is still queued behind it -- two concurrent mutagen rewrites of one archive
        # file, exactly what phaze-lwqk exists to prevent. Leave the row QUEUED instead: a retry
        # is refused by that same guard, and the row surfaces as visibly-stuck -- the same
        # non-terminal contract an agent that dispatched fine but never reported already has. A
        # genuinely-lost enqueue (no ghost job ever landed) self-heals through
        # ``recover_orphaned_work``, which re-drives an orphaned ledger row exactly like this one.
        logger.error(
            "tag write enqueue ambiguous -- leaving row queued rather than risk a duplicate dispatch",
            file_id=str(file_record.id),
            log_id=str(log_entry.id),
            agent_id=file_record.agent_id,
            exc_info=True,
        )
        return log_entry
    except Exception as exc:
        # Anything else (e.g. ``queue.connect()`` failing, above -- provably BEFORE the broker
        # connection existed) is unambiguous: nothing was ever created for this dispatch, so it is
        # safe to downgrade in place rather than leaving a ``queued`` row no agent will ever answer
        # for. FAILED is non-terminal, so the file stays in the queue and the operator can retry
        # once the broker is healthy.
        logger.warning("tag write enqueue failed", file_id=str(file_record.id), agent_id=file_record.agent_id, exc_info=True)
        log_entry.status = TagWriteStatus.FAILED.value
        log_entry.error_message = f"could not dispatch the tag write to agent {file_record.agent_id!r}: {exc}"
        await session.flush()
        await session.commit()
        return log_entry

    logger.info("tag write queued", file_id=str(file_record.id), log_id=str(log_entry.id), agent_id=file_record.agent_id, source=source)
    return log_entry
