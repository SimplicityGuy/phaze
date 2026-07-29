"""SAQ task: write_file_tags -- mutagen tag write + verify on the agent, reported via HTTP (phaze-6bkk).

DIST-01 gives the application server NO media mount, so the api container could never perform the
mutagen ``save()`` that ``POST /tags/{file_id}/write`` (and ``/undo``, and the bulk submit) claimed
to perform: ``current_path`` is a file-server path that simply does not exist inside the api
container, and every write failed with ``[Errno 2] No such file or directory`` under a toast that
blamed file permissions. This task is the other half of the fix -- the control plane creates the
``tag_write_log`` row in the ``queued`` state and enqueues here, onto the OWNING agent's ``meta``
lane (``services.tag_writer.enqueue_tag_write``), and this task does the disk work where the disk
actually is and PATCHes the terminal outcome back.

Requires the media mount to be READ-WRITE on the meta lane (docker-compose.agent.yml). That is the
same second-order constraint tracked by phaze-mwvt / phaze-au0r for ``execute_approved_batch``; the
compose change ships with this bead, and ``tests/agents/deployment/test_agent_compose.py`` asserts
it so the mount cannot silently regress to ``:ro`` and re-break every write.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10 / D-25).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from phaze.schemas.agent_tag_writes import TagWriteResultPayload
from phaze.schemas.agent_tasks import WriteFileTagsPayload
from phaze.services.tag_write_disk import write_and_verify_sync


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)

# Same bound as TagWriteResultPayload.error_message -- truncate BEFORE constructing the payload so a
# pathological exception string can never raise a ValidationError that would swallow the real error.
_ERROR_MESSAGE_MAX = 2000


async def write_file_tags(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Write tags to one file on the agent's media mount and PATCH the outcome back."""
    payload = WriteFileTagsPayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]

    logger.info("tag write started", file_id=str(payload.file_id), log_id=str(payload.log_id))

    # phaze-qfxv discipline, carried over from the api: the ENTIRE blocking sequence (read-before,
    # mutagen save -- which rewrites the whole file when the tag area must grow -- and the verify
    # re-read) runs in ONE thread offload. The agent's event loop also runs the Phase-46 liveness
    # heartbeat, whose design premise is 'the event loop is free'; an on-loop stall against a slow
    # media mount risks a false DEAD classification and duplicate-work re-enqueue.
    status, discrepancies, error_message, before_tags = await asyncio.to_thread(write_and_verify_sync, payload.file_path, payload.tags)

    result = TagWriteResultPayload(
        status=status,
        before_tags=before_tags,
        discrepancies=discrepancies or None,
        error_message=error_message[:_ERROR_MESSAGE_MAX] if error_message else None,
    )
    # A failed WRITE is not a failed JOB -- ``write_and_verify_sync`` already classified it into a
    # terminal audit status the operator retries from the workspace. Only the CALLBACK is allowed to
    # fail the job: if the control plane never hears the outcome, the row is stranded in `queued`
    # and the file silently disappears from the queue, so let it raise and take SAQ's retry.
    await api.patch_tag_write(payload.log_id, result)

    logger.info("tag write completed", file_id=str(payload.file_id), log_id=str(payload.log_id), status=str(status))
    return {"file_id": str(payload.file_id), "log_id": str(payload.log_id), "status": str(status)}
