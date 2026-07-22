"""Tag writer service - format-aware tag writing with verify-after-write.

Writes tags to MP3 (ID3), OGG/FLAC/OPUS (Vorbis), and M4A (MP4) files
using mutagen. Verifies written tags by re-reading and comparing with
NFC Unicode normalization. Creates TagWriteLog audit entries.
"""

from __future__ import annotations

import asyncio
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

# phaze-52qd: the full set of core tag fields a write/undo snapshot must span. ``_extract_before_tags``
# records EVERY one of these -- ``None`` where the field is absent on disk -- so an undo can DELETE a
# frame the write added, not merely leave it.
_CORE_TAG_FIELDS: tuple[str, ...] = ("artist", "title", "album", "year", "genre", "track_number")


def write_tags(file_path: str, tags: dict[str, str | int | None]) -> None:
    """Write tags to an audio file using format-aware mutagen methods.

    Supports ID3 (MP3), Vorbis (OGG/FLAC/OPUS), and MP4 (M4A) formats.

    Args:
        file_path: Path to the audio file.
        tags: Dict of field names to values. A ``None`` value DELETES the corresponding
            frame/atom/comment (phaze-52qd: this is how an undo removes a tag a prior write
            added). A field that is simply absent from the dict is left untouched.

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
    """Write ID3 frames to an MP3 file. A ``None`` value DELETES the frame (phaze-52qd)."""
    for field, value in tags.items():
        frame_cls = _WRITE_ID3_MAP.get(field)
        if frame_cls is None:
            continue
        if value is None:
            audio.tags.delall(frame_cls.__name__)
        else:
            audio.tags.add(frame_cls(encoding=3, text=[str(value)]))


def _write_vorbis(audio: Any, tags: dict[str, str | int | None]) -> None:
    """Write Vorbis comments to an OGG/FLAC/OPUS file. A ``None`` value DELETES the key (phaze-52qd)."""
    for field, value in tags.items():
        vorbis_key = _WRITE_VORBIS_MAP.get(field)
        if vorbis_key is None:
            continue
        if value is None:
            if vorbis_key in audio:
                del audio[vorbis_key]
        else:
            audio[vorbis_key] = [str(value)]


def _write_mp4(audio: Any, tags: dict[str, str | int | None]) -> None:
    """Write MP4 atoms to an M4A file. A ``None`` value DELETES the atom (phaze-52qd)."""
    for field, value in tags.items():
        mp4_key = _WRITE_MP4_MAP.get(field)
        if mp4_key is None:
            continue
        if value is None:
            if mp4_key in audio:
                del audio[mp4_key]
        elif field == "track_number":
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

    Note (phaze-52qd): an ``expected`` value of ``None`` means the field should have been
    DELETED (an undo removing a tag a prior write added). Such a field is a discrepancy iff
    it is still present on disk -- verifying deletions, not skipping them.
    """
    actual_tags = extract_tags(file_path, strict=True)
    discrepancies: dict[str, dict[str, str | None]] = {}

    for field, expected_val in expected.items():
        actual_val = getattr(actual_tags, field, None)

        if expected_val is None:
            # The field was meant to be absent (a deletion). It is a discrepancy only if a
            # value survives on disk.
            if actual_val is not None:
                discrepancies[field] = {
                    "expected": None,
                    "actual": unicodedata.normalize("NFC", str(actual_val)),
                }
            continue

        expected_norm = unicodedata.normalize("NFC", str(expected_val))
        actual_norm = unicodedata.normalize("NFC", str(actual_val)) if actual_val is not None else None

        if expected_norm != actual_norm:
            discrepancies[field] = {
                "expected": expected_norm,
                "actual": actual_norm,
            }

    return discrepancies


def _extract_before_tags(file_path: str) -> dict[str, str | int | None]:
    """Extract current tags as a COMPLETE before/undo snapshot.

    phaze-52qd: records EVERY core field, mapping an absent tag to an explicit ``None`` rather
    than omitting the key. Re-applying this snapshot through :func:`write_tags` therefore DELETES
    any frame the write added to a previously-untagged file (``None`` -> delete), instead of
    silently leaving it -- which is what made undo a no-op in the product's dominant "add tags to
    an untagged file" scenario.
    """
    tags = extract_tags(file_path)
    return {field: getattr(tags, field, None) for field in _CORE_TAG_FIELDS}


def _write_and_verify_sync(
    file_path: str,
    proposed_tags: dict[str, str | int | None],
) -> tuple[str, dict[str, dict[str, str | None]] | None, str | None, dict[str, str | int | None]]:
    """Synchronous disk work for one tag write: read-before, write, verify (phaze-qfxv).

    Bundled into a single function so the ENTIRE blocking sequence -- ``_extract_before_tags``
    (full read), ``write_tags`` (mutagen ``audio.save()``, which rewrites the whole file when the
    tag area must grow), and ``verify_write`` (another full read) -- runs in exactly one
    ``asyncio.to_thread`` offload from :func:`execute_tag_write`, instead of blocking the event
    loop directly. The bulk caller (``bulk_write_no_discrepancies``) loops this up to
    ``_MAX_BULK_TAG_WRITE`` (2000) times with no other await in between, so any synchronous slice
    of this work left on the loop freezes every SSE stream, poll, and concurrent request for the
    whole batch's duration -- an NFS stall inside one ``audio.save()`` would wedge the API
    indefinitely.

    Returns ``(status, discrepancies, error_message, before_tags)`` -- the four fields
    ``execute_tag_write`` persists onto ``TagWriteLog``. ``before_tags`` is captured and returned
    on EVERY path (including a failure in ``write_tags``/``verify_write`` after a successful
    read) so the audit log's before/undo snapshot is preserved exactly as it was when this logic
    ran inline on the event loop.
    """
    before_tags: dict[str, str | int | None] = {}
    try:
        before_tags = _extract_before_tags(file_path)
        write_tags(file_path, proposed_tags)
        discrepancies = verify_write(file_path, proposed_tags)
        status = TagWriteStatus.DISCREPANCY if discrepancies else TagWriteStatus.COMPLETED
        return status, discrepancies, None, before_tags
    except TagReadError as exc:
        # phaze-vq3g: the disk write LANDED but the verify re-read failed. Record a distinct
        # VERIFY_FAILED status with an explanatory message instead of synthesizing an all-field
        # ``actual=None`` DISCREPANCY that misrepresents a correctly-tagged file as written-wrong.
        # ``discrepancies`` stays None so no false per-field mismatch is persisted.
        return TagWriteStatus.VERIFY_FAILED, None, f"verify failed: {exc}", before_tags
    except Exception as exc:
        return TagWriteStatus.FAILED, None, str(exc), before_tags


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

    # phaze-qfxv: the entire disk-touching sequence (read-before, mutagen save, verify re-read) runs
    # off the event loop in a worker thread. Without this, a bulk submit loops this up to 2000 times
    # inline on the API event loop with no yield between the blocking calls of one file, freezing
    # every SSE stream, 5s poll, /health check, and agent callback for the whole batch.
    status, discrepancies, error_message, before_tags = await asyncio.to_thread(_write_and_verify_sync, file_path, proposed_tags)

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
