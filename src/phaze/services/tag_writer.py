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

from typing import TYPE_CHECKING
import uuid

import structlog

from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.schemas.agent_tasks import WriteFileTagsPayload
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

    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter

logger = structlog.get_logger(__name__)


class TagWriteDispatchError(RuntimeError):
    """The tag write could not be handed to the owning agent (broker/enqueue failure)."""


async def enqueue_tag_write(
    session: AsyncSession,
    task_router: AgentTaskRouter,
    file_record: FileRecord,
    proposed_tags: dict[str, str | int | None],
    source: str,
) -> TagWriteLog:
    """Create the audit row and dispatch the write to the file's OWNING agent.

    Args:
        session: Async database session.
        task_router: ``app.state.task_router`` -- the per-agent, per-lane SAQ enqueuer.
        file_record: The FileRecord to write tags to (must be applied -- an executed proposal exists).
        proposed_tags: Dict of proposed tag values.
        source: Source of the proposal ("tracklist", "metadata", "manual_edit", "undo", ...).

    Returns:
        The created ``TagWriteLog`` entry: ``queued`` when the job was handed to the agent,
        ``failed`` (with ``error_message``) when the enqueue itself could not be completed.

    Raises:
        ValueError: If the file is not applied (no executed proposal -- READ-05 / D-01).

    The row is created BEFORE the enqueue and flushed, so its id can ride the payload: that
    pre-minted ``log_id`` is what makes the agent's callback retry-stable (a SAQ retry PATCHes the
    same row instead of appending a duplicate). It is deliberately NOT committed here -- the
    callers own their transaction boundaries (the per-file routes commit once; the bulk loop commits
    per file so a mid-loop abort cannot lose earlier rows).

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

    log_entry = TagWriteLog(
        id=uuid.uuid4(),
        file_id=file_record.id,
        # The pre-write on-disk snapshot can only be read where the file is, so it arrives with the
        # agent's result callback. Empty until then -- never a lie about what was on disk.
        before_tags={},
        after_tags=proposed_tags,
        source=source,
        status=TagWriteStatus.QUEUED.value,
        discrepancies=None,
        error_message=None,
    )
    session.add(log_entry)
    await session.flush()

    try:
        await task_router.enqueue_for_file(
            file_record=file_record,
            task_name="write_file_tags",
            payload=WriteFileTagsPayload(
                log_id=log_entry.id,
                file_id=file_record.id,
                agent_id=file_record.agent_id,
                file_path=file_record.current_path,
                tags=proposed_tags,
            ),
        )
    except Exception as exc:
        # The audit row already exists; downgrade it to FAILED in place rather than leaving a
        # ``queued`` row no agent will ever answer for. FAILED is non-terminal, so the file stays
        # in the queue and the operator can retry once the broker is healthy.
        logger.warning("tag write enqueue failed", file_id=str(file_record.id), agent_id=file_record.agent_id, exc_info=True)
        log_entry.status = TagWriteStatus.FAILED.value
        log_entry.error_message = f"could not dispatch the tag write to agent {file_record.agent_id!r}: {exc}"
        await session.flush()
        return log_entry

    logger.info("tag write queued", file_id=str(file_record.id), log_id=str(log_entry.id), agent_id=file_record.agent_id, source=source)
    return log_entry
