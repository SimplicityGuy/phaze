"""SAQ task: extract_file_metadata -- mutagen tag read, posted via HTTP (Phase 26 D-05).

Reads tags from local disk via payload.original_path (NOT current_path -- D-24)
and posts via ctx["api_client"].put_metadata.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/test_task_split.py (Plan 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.schemas.agent_metadata import MetadataWriteRequest
from phaze.schemas.agent_tasks import ExtractMetadataPayload
from phaze.services.metadata import extract_tags


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)

# Music and video file types eligible for tag extraction (per D-10)
_EXTRACTABLE_CATEGORIES = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})


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
        # Sync mutagen call -- I/O bound header read on the local file
        tags = extract_tags(payload.original_path)
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
    except Exception:
        # Phase 45 (L-02 / CR-02): clear the scheduling-ledger row on the TERMINAL attempt only,
        # then re-raise so SAQ records the failed attempt. A retryable attempt (or job absent in a
        # pure unit test) re-raises silently so the one real retry can run -- the row survives for
        # it (T-45-06). Mirrors process_file's generic guard (functions.py:179-189). Without this
        # ack a terminally-failed metadata file stays in get_metadata_pending_files forever, so
        # is_domain_completed can never fire and recover_orphaned_work re-enqueues it on every pass.
        job = ctx.get("job")
        if job is not None and not job.retryable:
            await api.report_metadata_failed(payload.file_id)
        raise
    logger.info("metadata extraction completed", file_id=str(payload.file_id), status="extracted")
    return {"file_id": str(payload.file_id), "status": "extracted"}
