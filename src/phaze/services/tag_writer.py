"""Tag writer service - format-aware tag writing with verify-after-write.

Writes tags to MP3 (ID3), OGG/FLAC/OPUS (Vorbis), and M4A (MP4) files
using mutagen. Verifies written tags by re-reading and comparing with
NFC Unicode normalization. Creates TagWriteLog audit entries.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol
import unicodedata

import mutagen
from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection
import structlog

from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services.metadata import TagReadError, extract_tags, normalize_track_number_text, normalize_year_text
from phaze.services.stage_status import is_applied


if TYPE_CHECKING:
    from collections.abc import Callable
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


async def _dedicated_lock_connection(session: AsyncSession) -> tuple[AsyncConnection, bool]:
    """Resolve a connection to hold the per-file tag-write advisory lock on, PINNED for the whole
    :func:`execute_tag_write` call -- independent of how many times ``session`` itself commits.

    phaze-ysnp/phaze-lwqk: ``execute_tag_write`` now commits a durable write-ahead audit row BEFORE
    the disk write (phaze-ysnp), and a SESSION-scoped advisory lock is bound to the physical DBAPI
    connection, not to any ORM transaction -- acquiring on ``session`` would repeat the exact
    phaze-yhhy bug (the lock's release landing on whatever connection the pool hands `session` back
    after that internal commit, not the one that took it) at the per-file level. Opening a
    connection OUTSIDE the session and holding it for the whole call sidesteps that entirely: the
    lock's identity never depends on what `session` does with its own connection in between.

    Mirrors ``routers.tags._bulk_tagwrite_lock_connection`` exactly (including the test-harness
    reuse case -- see that function's docstring for the full rationale). Returns
    ``(connection, owns_it)``; the caller must ``await connection.close()`` iff ``owns_it``.
    """
    bind = session.bind
    if isinstance(bind, AsyncConnection):
        return bind, False
    conn = await bind.connect()
    return conn, True


class TagWriteTarget(Protocol):
    """Structural subset of ``FileRecord`` :func:`execute_tag_write` actually touches.

    phaze-o2ln: the bulk tag-write loop stopped passing live ``FileRecord`` ORM instances across a
    per-file ``session.rollback()`` (which expires every attribute of every object still in the
    identity map, not just the one that failed) and now passes a plain, non-ORM snapshot instead
    (``routers.tags._BulkCandidate``). This Protocol is the minimal surface both a real
    ``FileRecord`` and that snapshot satisfy, so this function works unchanged for either caller.
    """

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def current_path(self) -> str: ...


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

    phaze-2zl7: undo now supplies the RAW on-disk ``"N/total"`` text (see
    ``_extract_before_tags``) instead of a bare int, so the previous unconditional ``int(value)``
    would crash restoring an M4A file's track number. A plain int -- every FORWARD write, since
    ``compute_proposed_tags`` has no track-total source -- still writes ``(N, 0)`` exactly as
    before; only a ``"N/total"`` string (undo) additionally restores the total.
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


def _write_and_verify_only_sync(
    file_path: str,
    proposed_tags: dict[str, str | int | None],
) -> tuple[str, dict[str, dict[str, str | None]] | None, str | None]:
    """Synchronous disk work for one tag write's write+verify phase (phaze-qfxv / phaze-ysnp).

    phaze-ysnp: split from the former single ``_write_and_verify_sync`` (which also captured
    ``before_tags``) so :func:`execute_tag_write` can durably commit a write-ahead audit row
    BETWEEN the before-read and this, the irreversible part. ``write_tags`` (mutagen
    ``audio.save()``, which rewrites the whole file when the tag area must grow) and
    ``verify_write`` (a full re-read) still run in exactly one ``asyncio.to_thread`` offload each
    call, never inline on the event loop (phaze-qfxv): the bulk caller
    (``bulk_write_no_discrepancies``) loops this up to ``_MAX_BULK_TAG_WRITE`` (2000) times with no
    other await in between, so any synchronous slice left on the loop freezes every SSE stream,
    poll, and concurrent request for the whole batch's duration.

    Returns ``(status, discrepancies, error_message)``.
    """
    try:
        write_tags(file_path, proposed_tags)
        discrepancies = verify_write(file_path, proposed_tags)
        status = TagWriteStatus.DISCREPANCY if discrepancies else TagWriteStatus.COMPLETED
        return status, discrepancies, None
    except TagReadError as exc:
        # phaze-vq3g: the disk write LANDED but the verify re-read failed. Record a distinct
        # VERIFY_FAILED status with an explanatory message instead of synthesizing an all-field
        # ``actual=None`` DISCREPANCY that misrepresents a correctly-tagged file as written-wrong.
        # ``discrepancies`` stays None so no false per-field mismatch is persisted.
        return TagWriteStatus.VERIFY_FAILED, None, f"verify failed: {exc}"
    except Exception as exc:
        return TagWriteStatus.FAILED, None, str(exc)


async def execute_tag_write(
    session: AsyncSession,
    file_record: TagWriteTarget,
    proposed_tags: dict[str, str | int | None],
    source: str,
) -> TagWriteLog:
    """Orchestrate a tag write: read before, write, verify, create audit log.

    Args:
        session: Async database session.
        file_record: The file to write tags to (must be applied -- an executed proposal exists). A
            live ``FileRecord`` or any :class:`TagWriteTarget`-shaped snapshot (phaze-o2ln).
        proposed_tags: Dict of proposed tag values.
        source: Source of the proposal ("tracklist", "metadata", "manual_edit").

    Returns:
        The created TagWriteLog entry.

    Raises:
        ValueError: If the file is not applied (no executed proposal -- READ-05 / D-01).
    """
    # phaze-lwqk/phaze-ysnp: serialize every tag-write/undo operation against THIS file, on a
    # connection pinned for the WHOLE call (see _dedicated_lock_connection's docstring for why a
    # plain xact lock on `session` is not enough once this function commits mid-call). One lock
    # here closes: (a) two per-file requests racing each other (a double-click, two tabs, or a
    # per-file write racing its own retry), and (b) a per-file write/undo racing the bulk loop's
    # write of the SAME file -- previously two concurrent mutagen full-file rewrites on one audio
    # file, an irreplaceable-archive corruption risk.
    lock_conn, owns_lock_conn = await _dedicated_lock_connection(session)
    lock_key = func.hashtext(f"tagwrite:{file_record.id}")
    try:
        await lock_conn.execute(select(func.pg_advisory_lock(lock_key)))
        try:
            if not await is_applied(session, file_record.id):
                msg = "Only executed files can have tags written"
                raise ValueError(msg)

            file_path = file_record.current_path

            # The before-read is a blocking disk read, offloaded like the write (phaze-qfxv). A
            # failure HERE (the file vanished, an NFS hiccup, a permission error) never touched
            # disk -- record it as a FAILED write with no snapshot and return via the caller's
            # normal single commit, exactly as the pre-phaze-ysnp bundled function did. Only a
            # failure AFTER a successful before-read reaches the write-ahead durability path below.
            try:
                before_tags = await asyncio.to_thread(_extract_before_tags, file_path)
            except Exception as exc:
                log_entry = TagWriteLog(
                    file_id=file_record.id,
                    before_tags={},
                    after_tags=proposed_tags,
                    source=source,
                    status=TagWriteStatus.FAILED.value,
                    error_message=str(exc),
                )
                session.add(log_entry)
                await session.flush()
                return log_entry

            # phaze-ysnp: write-ahead the intent. Committing this IN_PROGRESS row NOW -- durably,
            # before the irreversible disk write -- means a crash/cancellation between here and the
            # caller's own commit (e.g. asyncio.CancelledError at an ``asyncio.to_thread`` boundary
            # during a graceful shutdown; CancelledError derives from BaseException on 3.14 and
            # unwinds straight past an ``except Exception``) leaves an honest "disk state uncertain,
            # snapshot preserved" row instead of the mutation vanishing from the append-only audit
            # trail entirely. IN_PROGRESS is excluded from BOTH
            # ``routers.tags._TERMINAL_TAGWRITE_STATUSES`` and ``..._UNDOABLE_TAGWRITE_STATUSES`` (see
            # TagWriteStatus.IN_PROGRESS's docstring), so an orphaned row never shadows a later undo
            # and never evicts the file from the bulk candidate window -- it simply re-qualifies and
            # self-heals into a terminal row on the next attempt.
            log_entry = TagWriteLog(
                file_id=file_record.id,
                before_tags=before_tags,
                after_tags=proposed_tags,
                source=source,
                status=TagWriteStatus.IN_PROGRESS.value,
            )
            session.add(log_entry)
            await session.flush()
            await session.commit()

            # phaze-qfxv: mutagen ``audio.save()`` (which rewrites the whole file when the tag area
            # must grow) and the verify re-read both run off the event loop in a worker thread, never
            # inline on the loop -- a bulk submit loops this up to 2000 times with no other await in
            # between, so any synchronous slice left on the loop freezes every SSE stream, poll, and
            # concurrent request for the whole batch's duration.
            status, discrepancies, error_message = await asyncio.to_thread(_write_and_verify_only_sync, file_path, proposed_tags)

            log_entry.status = status
            log_entry.discrepancies = discrepancies if discrepancies else None
            log_entry.error_message = error_message
            await session.flush()
            return log_entry
        finally:
            await lock_conn.execute(select(func.pg_advisory_unlock(lock_key)))
    finally:
        if owns_lock_conn:
            await lock_conn.close()
