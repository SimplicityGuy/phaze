"""Tag writer service - format-aware tag writing with verify-after-write.

Writes tags to MP3 (ID3), OGG/FLAC/OPUS (Vorbis), and M4A (MP4) files
using mutagen. Verifies written tags by re-reading and comparing with
NFC Unicode normalization. Creates TagWriteLog audit entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import unicodedata

import mutagen
from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4
import structlog

from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services.metadata import TagReadError, extract_tags
from phaze.services.stage_status import is_applied


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord

logger = structlog.get_logger(__name__)

# Write maps: field name -> format-specific key/class
_WRITE_ID3_MAP: dict[str, type] = {
    "artist": TPE1,
    "title": TIT2,
    "album": TALB,
    "year": TDRC,
    "genre": TCON,
    "track_number": TRCK,
}

_WRITE_VORBIS_MAP: dict[str, str] = {
    "artist": "artist",
    "title": "title",
    "album": "album",
    "year": "date",
    "genre": "genre",
    "track_number": "tracknumber",
}

_WRITE_MP4_MAP: dict[str, str] = {
    "artist": "\xa9ART",
    "title": "\xa9nam",
    "album": "\xa9alb",
    "year": "\xa9day",
    "genre": "\xa9gen",
    "track_number": "trkn",
}


def write_tags(file_path: str, tags: dict[str, str | int | None]) -> None:
    """Write tags to an audio file using format-aware mutagen methods.

    Supports ID3 (MP3), Vorbis (OGG/FLAC/OPUS), and MP4 (M4A) formats.

    Args:
        file_path: Path to the audio file.
        tags: Dict of field names to values. None values are skipped.

    Raises:
        ValueError: If the file is not a recognized audio format.
    """
    audio = mutagen.File(file_path)
    if audio is None:
        msg = f"{file_path} is not a recognized audio file"
        raise ValueError(msg)

    # Ensure tags container exists
    if audio.tags is None:
        audio.add_tags()

    if isinstance(audio.tags, ID3):
        _write_id3(audio, tags)
    elif isinstance(audio, MP4):
        _write_mp4(audio, tags)
    else:
        _write_vorbis(audio, tags)

    audio.save()


def _write_id3(audio: Any, tags: dict[str, str | int | None]) -> None:
    """Write ID3 frames to an MP3 file."""
    for field, value in tags.items():
        if value is None:
            continue
        frame_cls = _WRITE_ID3_MAP.get(field)
        if frame_cls is not None:
            audio.tags.add(frame_cls(encoding=3, text=[str(value)]))


def _write_vorbis(audio: Any, tags: dict[str, str | int | None]) -> None:
    """Write Vorbis comments to an OGG/FLAC/OPUS file."""
    for field, value in tags.items():
        if value is None:
            continue
        vorbis_key = _WRITE_VORBIS_MAP.get(field)
        if vorbis_key is not None:
            audio[vorbis_key] = [str(value)]


def _write_mp4(audio: Any, tags: dict[str, str | int | None]) -> None:
    """Write MP4 atoms to an M4A file."""
    for field, value in tags.items():
        if value is None:
            continue
        mp4_key = _WRITE_MP4_MAP.get(field)
        if mp4_key is not None:
            if field == "track_number":
                audio[mp4_key] = [(int(value), 0)]
            else:
                audio[mp4_key] = [str(value)]


def verify_write(file_path: str, expected: dict[str, str | int | None]) -> dict[str, dict[str, str | None]]:
    """Verify written tags by re-reading and comparing with NFC normalization.

    Args:
        file_path: Path to the audio file to verify.
        expected: Dict of expected field values.

    Returns:
        Dict of discrepancies: {field: {"expected": exp, "actual": act}}.
        Empty dict means perfect write.

    Raises:
        TagReadError: If the just-written file cannot be re-read/parsed (phaze-vq3g). This is a
            VERIFY failure, NOT a discrepancy -- the re-read is done in ``strict`` mode so a
            transient I/O/parse error surfaces as an exception the caller records distinctly,
            instead of an all-field ``actual=None`` false discrepancy. A file that opens cleanly
            but has no tags still returns a normal (all-field) discrepancy dict.
    """
    actual_tags = extract_tags(file_path, strict=True)
    discrepancies: dict[str, dict[str, str | None]] = {}

    for field, expected_val in expected.items():
        if expected_val is None:
            continue

        actual_val = getattr(actual_tags, field, None)
        expected_norm = unicodedata.normalize("NFC", str(expected_val))
        actual_norm = unicodedata.normalize("NFC", str(actual_val)) if actual_val is not None else None

        if expected_norm != actual_norm:
            discrepancies[field] = {
                "expected": expected_norm,
                "actual": actual_norm,
            }

    return discrepancies


def _extract_before_tags(file_path: str) -> dict[str, str | int | None]:
    """Extract current tags as a serializable dict for before_tags snapshot."""
    tags = extract_tags(file_path)
    result: dict[str, str | int | None] = {}
    for field in ("artist", "title", "album", "year", "genre", "track_number"):
        val = getattr(tags, field, None)
        if val is not None:
            result[field] = val
    return result


async def execute_tag_write(
    session: AsyncSession,
    file_record: FileRecord,
    proposed_tags: dict[str, str | int | None],
    source: str,
) -> TagWriteLog:
    """Orchestrate a tag write: read before, write, verify, create audit log.

    Args:
        session: Async database session.
        file_record: The FileRecord to write tags to (must be applied -- an executed proposal exists).
        proposed_tags: Dict of proposed tag values.
        source: Source of the proposal ("tracklist", "metadata", "manual_edit").

    Returns:
        The created TagWriteLog entry.

    Raises:
        ValueError: If the file is not applied (no executed proposal -- READ-05 / D-01).
    """
    if not await is_applied(session, file_record.id):
        msg = "Only executed files can have tags written"
        raise ValueError(msg)

    file_path = file_record.current_path
    status: str = TagWriteStatus.FAILED
    discrepancies: dict[str, dict[str, str | None]] | None = None
    error_message: str | None = None
    before_tags: dict[str, str | int | None] = {}

    try:
        before_tags = _extract_before_tags(file_path)
        write_tags(file_path, proposed_tags)
        discrepancies = verify_write(file_path, proposed_tags)
        status = TagWriteStatus.DISCREPANCY if discrepancies else TagWriteStatus.COMPLETED
    except TagReadError as exc:
        # phaze-vq3g: the disk write LANDED but the verify re-read failed. Record a distinct
        # VERIFY_FAILED status with an explanatory message instead of synthesizing an all-field
        # ``actual=None`` DISCREPANCY that misrepresents a correctly-tagged file as written-wrong.
        # ``discrepancies`` stays None so no false per-field mismatch is persisted.
        status = TagWriteStatus.VERIFY_FAILED
        error_message = f"verify failed: {exc}"
        discrepancies = None
    except Exception as exc:
        status = TagWriteStatus.FAILED
        error_message = str(exc)

    log_entry = TagWriteLog(
        file_id=file_record.id,
        before_tags=before_tags,
        after_tags=proposed_tags,
        source=source,
        status=status,
        discrepancies=discrepancies if discrepancies else None,
        error_message=error_message,
    )
    session.add(log_entry)
    await session.flush()
    return log_entry
