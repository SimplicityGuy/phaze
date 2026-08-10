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

phaze-2xg8n: ``payload.file_path`` (``FileRecord.current_path``) is agent-supplied -- upserted from
wire input via ``POST``/``PATCH /agent/files`` -- and this task performs a full mutagen rewrite of
it. It is therefore gated through the SAME ``resolve_and_check_containment`` boundary the
move/execution path already applies, before any disk I/O: refuse (as a terminal FAILED report, not
a raise -- see below) a path that resolves outside this agent's configured ``scan_roots``.

phaze-anrw4: the pre-write on-disk snapshot (``before_tags`` -- the undo anchor) is reported to the
control plane in its OWN call, BEFORE the mutating write, via
``PhazeAgentClient.report_tag_write_before_snapshot``. See that method's and
``phaze.routers.agent_tag_writes.record_tag_write_before_snapshot``'s docstrings for why: a SAQ
retry's ``write_and_verify_sync`` re-extracts ``before_tags`` from whatever is on disk NOW, which
-- if an earlier attempt's write landed but its result callback never arrived -- is the
ALREADY-WRITTEN state, not the true original. Reporting it up front, first-write-wins, makes the
captured snapshot independent of which attempt's write or callback actually completes.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10 / D-25).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.enums.tag_write import TagWriteStatus
from phaze.schemas.agent_tag_writes import TagWriteBeforeSnapshotPayload, TagWriteResultPayload
from phaze.schemas.agent_tasks import WriteFileTagsPayload
from phaze.services.containment import resolve_and_check_containment
from phaze.services.tag_write_disk import _extract_before_tags, write_and_verify_sync


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

    # phaze-2xg8n: containment check BEFORE any disk I/O, against THIS agent's own configured scan
    # roots -- only the agent's filesystem can resolve symlinks honestly (mirrors
    # ``tasks.cue_write._write_sync``'s reasoning: the control plane cannot resolve a path that
    # does not exist inside its own container).
    cfg = get_settings()
    scan_roots = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []
    try:
        resolved_path, _owning_root = resolve_and_check_containment(payload.file_path, scan_roots)
    except ValueError as exc:
        # A path that escapes scan_roots will not resolve differently on a SAQ retry, so this must
        # NOT raise and burn the retry budget leaving the row stranded ``queued`` -- report a
        # terminal FAILED outcome instead, exactly like a disk-level write failure below.
        logger.warning(
            "tag write refused -- path escapes scan_roots",
            file_id=str(payload.file_id),
            log_id=str(payload.log_id),
            error=str(exc),
        )
        result = TagWriteResultPayload(status=TagWriteStatus.FAILED, before_tags={}, error_message=str(exc)[:_ERROR_MESSAGE_MAX])
        await api.patch_tag_write(payload.log_id, result)
        return {"file_id": str(payload.file_id), "log_id": str(payload.log_id), "status": str(TagWriteStatus.FAILED)}
    file_path = str(resolved_path)

    # phaze-anrw4: capture + durably report the pre-write snapshot in its own thread offload and
    # HTTP round trip, BEFORE the mutating write below -- see the module docstring. Best-effort:
    # neither an extraction failure nor a transport failure here may abort the write itself, since
    # ``write_and_verify_sync`` below still captures + reports its own ``before_tags`` as a
    # single-attempt fallback (the endpoint's first-write-wins guard keeps whichever call lands
    # first; a second, redundant report is simply ignored).
    try:
        before_snapshot = await asyncio.to_thread(_extract_before_tags, file_path)
        await api.report_tag_write_before_snapshot(payload.log_id, TagWriteBeforeSnapshotPayload(before_tags=before_snapshot))
    except Exception:
        logger.warning(
            "tag write pre-snapshot failed -- falling back to the write-time capture",
            file_id=str(payload.file_id),
            log_id=str(payload.log_id),
            exc_info=True,
        )

    # phaze-qfxv discipline, carried over from the api: the ENTIRE blocking sequence (read-before,
    # mutagen save -- which rewrites the whole file when the tag area must grow -- and the verify
    # re-read) runs in ONE thread offload. The agent's event loop also runs the Phase-46 liveness
    # heartbeat, whose design premise is 'the event loop is free'; an on-loop stall against a slow
    # media mount risks a false DEAD classification and duplicate-work re-enqueue.
    status, discrepancies, error_message, before_tags = await asyncio.to_thread(write_and_verify_sync, file_path, payload.tags)

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
