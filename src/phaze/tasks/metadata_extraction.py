"""SAQ task: extract_file_metadata -- mutagen tag read, posted via HTTP (Phase 26 D-05).

Reads tags from local disk via payload.original_path (NOT current_path -- D-24)
and posts via ctx["api_client"].put_metadata.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.schemas.agent_metadata import MetadataFailurePayload, MetadataWriteRequest
from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services.metadata import extract_tags


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)

# Music and video file types eligible for tag extraction (per D-10)
_EXTRACTABLE_CATEGORIES = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})


async def _ack_terminal_failure(api: PhazeAgentClient, file_id: Any, exc: Exception, ctx: dict[str, Any]) -> None:
    """Phase 45 (L-02/CR-02) terminal-attempt ack: best-effort, never masks the original error.

    Called only when the SAQ job in ``ctx`` is present and NOT retryable -- a retryable attempt
    (or job absent in a pure unit test) re-raises silently so the one real retry can run (T-45-06);
    the row survives for it. Mirrors process_file's generic guard (functions.py:179-189). Without
    this ack a terminally-failed metadata file stays in get_metadata_pending_files forever, so
    is_domain_completed can never fire and recover_orphaned_work re-enqueues it on every pass.
    """
    job = ctx.get("job")
    if job is None or job.retryable:
        return
    # Phase 81 (FAIL-02): compose a triage payload so control persists a durable metadata failure
    # marker (failed_at set) with the exception detail. `reason="error"` -- the SAQ terminal ack
    # does not distinguish timeout/crash here. Truncate to the payload's `error` bound
    # (max_length=2000) BEFORE construction so a very long exception string can't raise a
    # ValidationError that would clobber the original error E1.
    failure = MetadataFailurePayload(reason="error", error=str(exc)[:2000])
    # Best-effort ack: if report_metadata_failed ALSO raises (E2) while handling the original
    # failure (E1), swallow + log E2 so the caller's bare `raise` always re-raises E1 -- SAQ must
    # record the real task error, not the ack error (WR-01).
    try:
        await api.report_metadata_failed(file_id, failure)
    except Exception:
        logger.warning("extract_file_metadata terminal-ack failed", file_id=str(file_id), exc_info=True)


async def extract_file_metadata(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Extract audio tags from a file on disk and PUT them via HTTP."""
    payload = ExtractMetadataPayload.model_validate(kwargs)

    logger.info("metadata extraction started", file_id=str(payload.file_id), file_type=payload.file_type)

    # Skip companion / unknown file types (parity with prior body)
    ext = "." + payload.file_type.lower()
    category = EXTENSION_MAP.get(ext, FileCategory.UNKNOWN)
    if category not in _EXTRACTABLE_CATEGORIES:
        logger.debug("metadata extraction skipped", file_id=str(payload.file_id), reason="not_extractable", file_type=payload.file_type)
        return {"file_id": str(payload.file_id), "status": "skipped", "reason": "not_extractable"}

    api: PhazeAgentClient = ctx["api_client"]

    try:
        # phaze-j8bj: mutagen's full tag parse (multi-MB APIC art / moov-atom seek+read
        # against the media mount) is blocking I/O. Offload to a worker thread so a slow
        # or hard-hung read cannot freeze the agent worker's event loop -- the loop also
        # runs the Phase-46 liveness heartbeat (heartbeat.py's design premise is 'the
        # event loop is free'), so an on-loop block risks a false DEAD classification and
        # duplicate-work re-enqueue. Mirrors scan.py's asyncio.to_thread discipline.
        tags = await asyncio.to_thread(extract_tags, payload.original_path)
        logger.debug("metadata tags read", file_id=str(payload.file_id), artist=tags.artist, title=tags.title, duration=tags.duration)

        # Map to Phase 25 MetadataWriteRequest schema; PUT idempotent upsert (CR-01 field-level LWW)
        body = MetadataWriteRequest(
            artist=tags.artist,
            title=tags.title,
            album=tags.album,
            year=tags.year,
            genre=tags.genre,
            track_number=tags.track_number,
            duration=tags.duration,
            bitrate=tags.bitrate,
            raw_tags=tags.raw_tags,
        )
        await api.put_metadata(payload.file_id, body)
    except Exception as exc:
        # Phase 45 (L-02 / CR-02): ack the terminal attempt only, then re-raise so SAQ records the
        # failed attempt -- see _ack_terminal_failure for the retryable/job-absent skip and the
        # best-effort E1-vs-E2 discipline.
        await _ack_terminal_failure(api, payload.file_id, exc, ctx)
        raise
    logger.info("metadata extraction completed", file_id=str(payload.file_id), status="extracted")
    return {"file_id": str(payload.file_id), "status": "extracted"}
