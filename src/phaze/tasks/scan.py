"""SAQ task function for scanning live sets via fingerprint matching."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import func, select

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    from phaze.services.fingerprint import FingerprintOrchestrator


logger = logging.getLogger(__name__)


async def scan_live_set(ctx: dict[str, Any], *, file_id: str) -> dict[str, Any]:
    """Scan a live set file via fingerprint matching and create a proposed tracklist.

    Queries the fingerprint DB for matching tracks, resolves artist/title from
    FileMetadata, and persists results as Tracklist + TracklistVersion + TracklistTrack
    rows with source='fingerprint' and status='proposed'.

    Re-scanning the same file creates a new TracklistVersion rather than a duplicate Tracklist.
    Retries with exponential backoff are handled by SAQ queue configuration.
    """
    async with ctx["async_session"]() as session:
        # 1. Look up file record
        result = await session.execute(select(FileRecord).where(FileRecord.id == uuid.UUID(file_id)))
        file_record = result.scalar_one_or_none()
        if file_record is None:
            return {"file_id": file_id, "status": "not_found"}

        # 2. Query fingerprint engines
        orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]
        matches = await orchestrator.combined_query(file_record.current_path)

        if not matches:
            return {"file_id": file_id, "status": "no_matches"}

        # 3. Resolve artist/title from FileMetadata for each match
        for match in matches:
            try:
                track_uuid = uuid.UUID(match.track_id)
                meta_result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == track_uuid))
                metadata = meta_result.scalar_one_or_none()
                if metadata is not None:
                    match.resolved_artist = metadata.artist
                    match.resolved_title = metadata.title
            except (ValueError, AttributeError):
                # track_id is not a valid UUID or lookup failed
                pass

        # 4. Check for existing tracklist (re-scan case)
        external_id = f"fp-{file_record.id.hex[:12]}"
        existing_result = await session.execute(select(Tracklist).where(Tracklist.external_id == external_id))
        existing_tracklist = existing_result.scalar_one_or_none()

        if existing_tracklist is not None:
            # Re-scan: create new version
            tracklist = existing_tracklist
            max_version_result = await session.execute(
                select(func.max(TracklistVersion.version_number)).where(TracklistVersion.tracklist_id == tracklist.id)
            )
            max_version = max_version_result.scalar_one_or_none() or 0
            version_number = max_version + 1
        else:
            # New tracklist
            tracklist = Tracklist(
                external_id=external_id,
                source_url="",
                file_id=file_record.id,
                source="fingerprint",
                status="proposed",
            )
            session.add(tracklist)
            await session.flush()
            version_number = 1

        # 5. Create version
        version = TracklistVersion(
            tracklist_id=tracklist.id,
            version_number=version_number,
        )
        session.add(version)
        await session.flush()

        # 6. Create tracks
        for position, match in enumerate(matches, start=1):
            track = TracklistTrack(
                version_id=version.id,
                position=position,
                artist=match.resolved_artist,
                title=match.resolved_title,
                timestamp=match.timestamp,
                confidence=match.confidence,
            )
            session.add(track)

        # 7. Update latest_version_id
        tracklist.latest_version_id = version.id

        await session.commit()
        return {
            "file_id": file_id,
            "status": "scanned",
            "tracklist_id": str(tracklist.id),
        }
