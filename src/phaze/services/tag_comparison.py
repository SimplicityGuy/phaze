"""Shared tag-comparison query + pure-computation helpers (phaze-b4u3p).

These lived as private helpers on ``routers/tags.py`` and were reached into directly by
``services/review.py`` (``get_tagwrite_review_page``) -- a SERVICE importing a ROUTER's
underscore-prefixed surface, which is a layering inversion (the dependency arrow points the
wrong way: routers should depend on services, not the reverse). Moved here, to the service
layer, so both ``routers/tags.py`` and ``services/review.py`` depend on ONE shared module
instead of one reaching into the other. ``routers/tags.py`` re-imports every name below under
the same identifier so its own routes, and the existing white-box tests that import these names
via ``phaze.routers.tags``, are unaffected.

Nothing here is FastAPI-aware (no ``Request``/``HTTPException``) -- the one token helper that
raises ``HTTPException`` on a malformed payload (``_decode_tag_review_token``) stays in
``routers/tags.py``, since translating a wire-format violation into an HTTP response is a
router concern, not a service one.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, and_, exists, or_, select, tuple_

from phaze.models.discogs_link import DiscogsLink
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.services.tag_proposal import CORE_FIELDS


if TYPE_CHECKING:
    from collections.abc import Mapping
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.services.tag_proposal import TagFieldSource


FIELD_LABELS: dict[str, str] = {
    "artist": "Artist",
    "title": "Title",
    "album": "Album",
    "year": "Year",
    "genre": "Genre",
    "track_number": "Track #",
}


def _terminal_tagwrite_subq(file_id: uuid.UUID | None = None) -> Select[tuple[uuid.UUID]]:
    """Subquery of ``file_id``\\ s with a TERMINAL, un-reverted ``TagWriteLog`` (COMPLETED or NO_OP).

    The single source of the tag-write idempotency anti-join, shared by both operator builders
    (``routers.tags.bulk_write_no_discrepancies`` and ``services.review.get_tagwrite_review_page``):
    a file listed here is done (written) or needs no write (zero-change NO_OP) and is dropped from
    the candidate window (WR-01).

    phaze-vwyco: a completed UNDO is itself a ``COMPLETED`` ``TagWriteLog`` row
    (``source="undo"``) -- a naive ``status IN (...)`` form matches it identically to a genuine
    forward completion, permanently evicting a reverted file even though its disk tags are back to
    the pre-write (once again changed) state, with no re-apply path left anywhere (every reachable
    POST target is a rendered row, and a reverted file's row never renders again). This applies the
    SAME chain-boundary rule ``_get_write_log_to_undo`` (``routers/tags.py``) uses to pick an undo's
    TARGET, inverted for terminal DETECTION: a forward terminal row (``COMPLETED`` with
    ``source != "undo"``, or ``NO_OP``) only keeps the file terminal if no LATER completed undo has
    since reverted it. Optionally scoped to a single ``file_id`` -- ``_has_terminal_tagwrite``
    (``routers/tags.py``) reuses this exact predicate for its per-file re-check instead of the
    separate, unfixed ``status``-only query it used to run.
    """
    terminal_predicate = or_(
        TagWriteLog.status == TagWriteStatus.NO_OP,
        and_(TagWriteLog.status == TagWriteStatus.COMPLETED, TagWriteLog.source != "undo"),
    )
    terminal_rows_stmt = select(TagWriteLog.file_id, TagWriteLog.written_at, TagWriteLog.id).where(terminal_predicate)
    later_undo_stmt = select(TagWriteLog.file_id, TagWriteLog.written_at, TagWriteLog.id).where(
        TagWriteLog.source == "undo", TagWriteLog.status == TagWriteStatus.COMPLETED
    )
    if file_id is not None:
        terminal_rows_stmt = terminal_rows_stmt.where(TagWriteLog.file_id == file_id)
        later_undo_stmt = later_undo_stmt.where(TagWriteLog.file_id == file_id)
    terminal_rows = terminal_rows_stmt.subquery()
    later_undo = later_undo_stmt.subquery()
    # phaze-9dwb: built with the standalone ``exists(...)`` construct, not the ``Select.exists()``
    # method -- the router event-loop-hygiene guard flags any ``.exists()`` ATTRIBUTE call in a
    # router function body as (unambiguous) blocking filesystem I/O and cannot tell this SQL
    # construct apart from ``pathlib.Path.exists()``. This module is not a router, but the predicate
    # is shared verbatim with one, so the same construct is kept for a single canonical form.
    later_undo_exists = exists(
        select(1).where(
            later_undo.c.file_id == terminal_rows.c.file_id,
            tuple_(later_undo.c.written_at, later_undo.c.id) > tuple_(terminal_rows.c.written_at, terminal_rows.c.id),
        )
    )
    return select(terminal_rows.c.file_id).where(~later_undo_exists).distinct()


async def _get_tracklist_for_file(session: AsyncSession, file_id: uuid.UUID) -> Tracklist | None:
    """Find the best tracklist associated with a file.

    ``tracklists.file_id`` has only a NON-unique index, and mainline paths (>=90 auto-link,
    a re-scrape) can legitimately create multiple tracklists per file. A ``scalar_one_or_none``
    here would raise ``MultipleResultsFound`` -> 500 the tags page and silently empty the tagwrite queue
    (services/review.py swallows it). Pick the highest-confidence link deterministically instead, mirroring
    services/pipeline.py's ``max(match_confidence)`` per-file model.
    """
    stmt = select(Tracklist).where(Tracklist.file_id == file_id).order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.id).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def _get_accepted_discogs_link(session: AsyncSession, file_id: uuid.UUID) -> DiscogsLink | None:
    """Find the accepted DiscogsLink for the file's tracklist, if any."""
    # Multiplicity-tolerant (see _get_tracklist_for_file): a file may have >1 tracklist; pick the
    # highest-confidence one's latest version rather than raising MultipleResultsFound.
    tl_stmt = (
        select(Tracklist.latest_version_id)
        .where(Tracklist.file_id == file_id)
        .order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.id)
        .limit(1)
    )
    tl_result = await session.execute(tl_stmt)
    version_id = tl_result.scalars().first()
    if version_id is None:
        return None
    track_ids = select(TracklistTrack.id).where(TracklistTrack.version_id == version_id)
    link_stmt = (
        select(DiscogsLink)
        .where(DiscogsLink.track_id.in_(track_ids), DiscogsLink.status == "accepted")
        # phaze-evn9: confidence is non-unique, so a tie left the pick arbitrary and unstable
        # across queries. ``id`` tiebreaks equal confidence deterministically, mirroring the
        # ``_get_latest_write_log`` / ``_get_write_log_to_undo`` pattern in ``routers/tags.py``.
        .order_by(DiscogsLink.confidence.desc(), DiscogsLink.id.desc())
        .limit(1)
    )
    link_result = await session.execute(link_stmt)
    return link_result.scalar_one_or_none()


async def _get_tracklists_for_files(session: AsyncSession, file_ids: list[uuid.UUID]) -> dict[uuid.UUID, Tracklist]:
    """Batch form of :func:`_get_tracklist_for_file`: ONE query for a whole page of files (phaze-bto9).

    Same selection rule per file -- highest ``match_confidence``, ``id`` breaking ties -- expressed
    as a Postgres ``DISTINCT ON (file_id)`` with the identical ``ORDER BY``, so a file resolves to
    exactly the tracklist the per-file helper would have picked. The per-file helper stays for the
    single-row mutation routes; this exists because the review scan called it once per CANDIDATE,
    which is unbounded in the applied backlog rather than in the rows actually rendered.
    """
    if not file_ids:
        return {}
    stmt = (
        select(Tracklist)
        .where(Tracklist.file_id.in_(file_ids))
        .distinct(Tracklist.file_id)
        .order_by(Tracklist.file_id, Tracklist.match_confidence.desc().nulls_last(), Tracklist.id)
    )
    return {tl.file_id: tl for tl in (await session.execute(stmt)).scalars().all() if tl.file_id is not None}


async def _get_accepted_discogs_links_for_files(session: AsyncSession, tracklists: dict[uuid.UUID, Tracklist]) -> dict[uuid.UUID, DiscogsLink]:
    """Batch form of :func:`_get_accepted_discogs_link`, keyed by file id (phaze-bto9).

    Takes the already-resolved per-file tracklists (so the "which tracklist" decision is made once,
    not re-derived) and resolves each one's ``latest_version_id`` to its best accepted link in ONE
    query. Same rule as the per-file helper: highest ``confidence``, ``id`` descending as the
    phaze-evn9 deterministic tiebreak, expressed as ``DISTINCT ON (version_id)``.
    """
    version_to_file = {tl.latest_version_id: file_id for file_id, tl in tracklists.items() if tl.latest_version_id is not None}
    if not version_to_file:
        return {}
    stmt = (
        select(TracklistTrack.version_id, DiscogsLink)
        .join(DiscogsLink, DiscogsLink.track_id == TracklistTrack.id)
        .where(TracklistTrack.version_id.in_(list(version_to_file)), DiscogsLink.status == "accepted")
        .distinct(TracklistTrack.version_id)
        .order_by(TracklistTrack.version_id, DiscogsLink.confidence.desc(), DiscogsLink.id.desc())
    )
    return {version_to_file[version_id]: link for version_id, link in (await session.execute(stmt)).tuples().all()}


def _build_comparison(
    file_metadata: TagFieldSource | None,
    proposed_tags: dict[str, str | int | None],
) -> list[dict[str, Any]]:
    """Build comparison list for all CORE_FIELDS."""
    comparison = []
    for field in CORE_FIELDS:
        current_val = getattr(file_metadata, field, None) if file_metadata else None
        proposed_val = proposed_tags.get(field)
        changed = (
            str(current_val) != str(proposed_val)
            if current_val is not None and proposed_val is not None
            else (current_val is not None) != (proposed_val is not None)
        )
        comparison.append(
            {
                "field": field,
                "label": FIELD_LABELS.get(field, field),
                "current": current_val,
                "proposed": proposed_val,
                "changed": changed,
            }
        )
    return comparison


def _count_changes(comparison: list[dict[str, Any]]) -> int:
    """Count number of changed fields in a comparison."""
    return sum(1 for c in comparison if c["changed"])


def _qualifies_for_bulk_write(comparison: list[dict[str, Any]]) -> bool:
    """LOCKED D-03 / OQ-1 predicate for the no-discrepancies bulk tag write.

    A file qualifies iff its server-computed comparison has ``>= 1`` changed field (there IS
    something to write) AND no field would blank an existing tag (``current is not None and
    proposed is None``) -- a bulk write NEVER erases an existing tag. Files failing either clause
    stay per-file Approve/Edit/Skip.

    The blank clause is defensive: ``compute_proposed_tags`` copies every non-None metadata field
    into the proposal, so a server-computed comparison never blanks a tag. The guard makes that
    invariant explicit + future-proof, and is asserted directly at the unit level.
    """
    if _count_changes(comparison) < 1:
        return False
    return not any(c["current"] is not None and c["proposed"] is None for c in comparison)


def _summarize_tags(comparison: list[dict[str, Any]], side: str) -> str:
    """Join a comparison's ``current`` (before) or ``proposed`` (after) side into a display string.

    Renders ``"label: value · label: value · …"`` across every CORE field, with an em dash for a
    ``None`` value (an absent tag). ``side`` is ``"current"`` or ``"proposed"``. All values are plain
    Python data -- the caller's template autoescapes them on render (T-60-XSS). Shared by
    ``routers.tags`` (the mutation routes' ``before``/``after`` cells) and
    ``services.review.get_tagwrite_review_page`` (the tagwrite queue's ``before_summary`` /
    ``after_summary``) so a row's diff text never drifts between the queue and the mutation routes.
    """
    parts = [f"{c['label']}: {c[side] if c[side] is not None else '—'}" for c in comparison]
    return " · ".join(parts)


def _tag_review_payload(
    file_record: FileRecord,
    tracklist: Tracklist | None,
    discogs_link: DiscogsLink | None,
    proposed: Mapping[str, object],
) -> dict[str, Any]:
    """Capture the exact rendered tag decision and every source version that produced it."""
    metadata = file_record.file_metadata
    return {
        "file_id": str(file_record.id),
        "before": {field: getattr(metadata, field, None) if metadata is not None else None for field in CORE_FIELDS},
        "after": {field: proposed.get(field) for field in CORE_FIELDS},
        "sources": {
            "file_updated_at": file_record.updated_at.isoformat(),
            "metadata_updated_at": metadata.updated_at.isoformat() if metadata is not None else None,
            "tracklist_id": str(tracklist.id) if tracklist is not None else None,
            "tracklist_updated_at": tracklist.updated_at.isoformat() if tracklist is not None else None,
            "tracklist_version_id": str(tracklist.latest_version_id) if tracklist is not None and tracklist.latest_version_id is not None else None,
            "discogs_link_id": str(discogs_link.id) if discogs_link is not None else None,
            "discogs_link_updated_at": discogs_link.updated_at.isoformat() if discogs_link is not None else None,
        },
    }


def _encode_tag_review_token(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(encoded).decode()
