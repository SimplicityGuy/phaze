"""Format-aware tag writing + verify-after-write -- the pure ON-DISK half (phaze-6bkk).

Split out of ``phaze.services.tag_writer`` so the code that actually touches the archive can
run where the archive actually is. Under DIST-01 the api and controller containers hold NO media
mount, so every mutagen ``save()`` in this module executes on the FILE SERVER, inside the agent
worker's ``meta`` lane (``phaze.tasks.tag_write.write_file_tags``). ``phaze.services.tag_writer``
keeps the control-plane orchestration (audit row + enqueue) and re-exports these helpers so
existing imports are unchanged.

This module MUST NOT import ``phaze.database``, ``phaze.models.*``, or SQLAlchemy -- it is loaded
inside the agent worker process (D-25 import boundary, ``tests/shared/core/test_task_split.py``).
The statuses it returns come from the DB-free :mod:`phaze.enums.tag_write`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import unicodedata

import mutagen
from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4
import structlog

from phaze.enums.tag_write import TagWriteStatus
from phaze.services.metadata import TagReadError, extract_tags, normalize_track_number_text, normalize_year_text


if TYPE_CHECKING:
    from collections.abc import Callable


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


def _mp4_track_tuple(value: str | int) -> tuple[int, int]:
    """Parse a track-number tag value into MP4's ``(track, total)`` atom form.

    phaze-2zl7: undo supplies the RAW on-disk ``"N/total"`` text (see ``_extract_before_tags``)
    instead of a bare int, so an unconditional ``int(value)`` would crash restoring an M4A file's
    track number. A plain int -- every FORWARD write, since ``compute_proposed_tags`` has no
    track-total source -- still writes ``(N, 0)`` exactly as before; only a ``"N/total"`` string
    (undo) additionally restores the total.
    """
    if isinstance(value, str) and "/" in value:
        num_text, _, total_text = value.partition("/")
        num = int(num_text.strip()) if num_text.strip().isdigit() else 0
        total = int(total_text.strip()) if total_text.strip().isdigit() else 0
        return (num, total)
    return (int(value), 0)


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
            audio[mp4_key] = [_mp4_track_tuple(value)]
        else:
            audio[mp4_key] = [str(value)]


# phaze-2zl7: fields whose normalized value is a LOSSY parse of richer raw text (a full release
# date truncates to a 4-digit year; "N/total" drops the total). An undo now writes the RAW text
# back (see _extract_before_tags), which legitimately differs from a byte-for-byte NFC comparison
# against the value our own reader can represent -- compare through the SAME normalization rule
# extract_tags applies instead, so a faithful raw write is not misreported as a discrepancy.
_SEMANTIC_COMPARE_FIELDS: dict[str, Callable[[Any], int | None]] = {
    "year": normalize_year_text,
    "track_number": normalize_track_number_text,
}


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

    Note (phaze-2zl7): ``year``/``track_number`` are compared through the extractor's OWN
    normalization rule (see :data:`_SEMANTIC_COMPARE_FIELDS`), not raw NFC text -- see that
    mapping's docstring.
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

        normalizer = _SEMANTIC_COMPARE_FIELDS.get(field)
        if normalizer is not None:
            if normalizer(expected_val) == actual_val:
                continue
            discrepancies[field] = {
                "expected": unicodedata.normalize("NFC", str(expected_val)),
                "actual": unicodedata.normalize("NFC", str(actual_val)) if actual_val is not None else None,
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

    phaze-2zl7: ``year``/``track_number``/``genre`` prefer the RAW on-disk text
    (``ExtractedTags.raw_year``/``raw_track_number``/``raw_genre``) over the normalized fields --
    ``extract_tags``'s normalization is correct for search/matching but lossy for an undo snapshot
    (a full release date truncates to a 4-digit year, "N/total" drops the total, a multi-value
    genre keeps only the first). Falls back to the normalized value when no raw text exists (the
    field is genuinely absent on disk, or the format has no richer representation to lose).
    """
    tags = extract_tags(file_path)
    before: dict[str, str | int | None] = {field: getattr(tags, field, None) for field in _CORE_TAG_FIELDS}
    if tags.raw_year is not None:
        before["year"] = tags.raw_year
    if tags.raw_track_number is not None:
        before["track_number"] = tags.raw_track_number
    if tags.raw_genre is not None:
        before["genre"] = tags.raw_genre
    return before


def write_and_verify_sync(
    file_path: str,
    proposed_tags: dict[str, str | int | None],
) -> tuple[TagWriteStatus, dict[str, dict[str, str | None]] | None, str | None, dict[str, str | int | None]]:
    """Synchronous disk work for one tag write: read-before, write, verify (phaze-qfxv).

    Bundled into a single function so the ENTIRE blocking sequence -- ``_extract_before_tags``
    (full read), ``write_tags`` (mutagen ``audio.save()``, which rewrites the whole file when the
    tag area must grow), and ``verify_write`` (another full read) -- runs in exactly one
    ``asyncio.to_thread`` offload from its caller, instead of blocking the event loop directly.
    phaze-6bkk moved that caller from the api request handler to the agent worker's ``meta`` lane
    (``phaze.tasks.tag_write.write_file_tags``), where the media mount actually exists -- but the
    offload discipline still matters there: the agent's event loop also runs the Phase-46 liveness
    heartbeat, so an on-loop mutagen stall risks a false DEAD classification.

    Returns ``(status, discrepancies, error_message, before_tags)`` -- the four fields the
    control plane persists onto ``TagWriteLog``. ``before_tags`` is captured and returned on
    EVERY path (including a failure in ``write_tags``/``verify_write`` after a successful read)
    so the audit log's before/undo snapshot is preserved.
    """
    before_tags: dict[str, str | int | None] = {}
    try:
        before_tags = _extract_before_tags(file_path)
        write_tags(file_path, proposed_tags)
        discrepancies = verify_write(file_path, proposed_tags)
        status: TagWriteStatus = TagWriteStatus.DISCREPANCY if discrepancies else TagWriteStatus.COMPLETED
        return status, discrepancies, None, before_tags
    except TagReadError as exc:
        # phaze-vq3g: the disk write LANDED but the verify re-read failed. Record a distinct
        # VERIFY_FAILED status with an explanatory message instead of synthesizing an all-field
        # ``actual=None`` DISCREPANCY that misrepresents a correctly-tagged file as written-wrong.
        # ``discrepancies`` stays None so no false per-field mismatch is persisted.
        return TagWriteStatus.VERIFY_FAILED, None, f"verify failed: {exc}", before_tags
    except Exception as exc:
        return TagWriteStatus.FAILED, None, str(exc), before_tags
