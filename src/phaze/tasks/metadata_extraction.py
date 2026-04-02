"""SAQ task function for audio tag extraction via mutagen."""

from __future__ import annotations

import logging
from typing import Any
import uuid

from sqlalchemy import select

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.services.metadata import extract_tags


logger = logging.getLogger(__name__)

# Music and video file types eligible for tag extraction (per D-10)
_EXTRACTABLE_CATEGORIES = frozenset({FileCategory.MUSIC, FileCategory.VIDEO})


async def extract_file_metadata(ctx: dict[str, Any], *, file_id: str) -> dict[str, Any]:
    """Extract audio tags from a single file and store in FileMetadata.

    Per D-10: extracts from music and video files (not companions).
    Per D-11: files with no tags get empty FileMetadata row.
    Per D-04: idempotent -- upserts FileMetadata.
    Retries with exponential backoff are handled by SAQ queue configuration.
    """
    async with ctx["async_session"]() as session:
        # 1. Fetch file record
        result = await session.execute(select(FileRecord).where(FileRecord.id == uuid.UUID(file_id)))
        file_record = result.scalar_one_or_none()
        if file_record is None:
            return {"file_id": file_id, "status": "not_found"}

        # 2. Skip companion files (per D-10)
        ext = "." + file_record.file_type.lower()
        category = EXTENSION_MAP.get(ext, FileCategory.UNKNOWN)
        if category not in _EXTRACTABLE_CATEGORIES:
            return {"file_id": file_id, "status": "skipped", "reason": "not_extractable"}

        # 3. Extract tags (sync, I/O-bound header read)
        tags = extract_tags(file_record.current_path)

        # 4. Upsert FileMetadata row
        existing = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_record.id))
        metadata = existing.scalar_one_or_none()
        if metadata is None:
            metadata = FileMetadata(file_id=file_record.id)
            session.add(metadata)

        metadata.artist = tags.artist
        metadata.title = tags.title
        metadata.album = tags.album
        metadata.year = tags.year
        metadata.genre = tags.genre
        metadata.track_number = tags.track_number
        metadata.duration = tags.duration
        metadata.bitrate = tags.bitrate
        metadata.raw_tags = tags.raw_tags

        # 5. Transition state to METADATA_EXTRACTED (per D-03)
        file_record.state = FileState.METADATA_EXTRACTED

        await session.commit()
        return {"file_id": file_id, "status": "extracted"}
