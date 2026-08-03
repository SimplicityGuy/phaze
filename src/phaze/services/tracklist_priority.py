"""Operator priority flags + the per-file lookup review the admin UI (phaze-fq9h.8) reads.

TWO JOBS IN ONE MODULE, DELIBERATELY PAIRED
--------------------------------------------
1. **Persist the flag.** :func:`flag_file_for_lookup` / :func:`unflag_file` /
   :func:`load_flagged_file_ids` are the storage half of "trigger/prioritize a lookup for a
   file" -- see :mod:`phaze.models.tracklist_priority_flag` for why nothing did this before.
   :func:`load_flagged_file_ids` is read by
   :func:`phaze.services.tracklist_drain.build_drain_queue` on every call, so a flag set once
   survives every future drain slice, cron run, or restart -- not just the one job it happened
   to be passed into.

2. **Answer "what does the operator see for THIS file".** :func:`get_file_tracklist_review`
   assembles the single-file view the record page (phaze-fq9h.8, RECORD-01) renders: whether a
   tracklist exists, whether it was actually scraped for this file or merely propagated from a
   duplicate, and -- when neither is true -- what the LAST lookup attempt actually said, so a
   Turnstile block, a stale-selector failure, and a genuine "not on the site" render as three
   different things rather than one undifferentiated blank.

SIMPLIFICATION THIS MODULE MAKES, STATED PLAINLY
--------------------------------------------------
:func:`get_file_tracklist_review`'s eligibility read (would this file ever enter the drain
queue) uses ONLY duration and filename -- the same :func:`~phaze.services.tracklist_candidates
.classify` a full corpus pass uses, but it does NOT replicate the cue-companion / embedded-
tracklist "already answered by another source" checks
(:attr:`~phaze.services.tracklist_candidates.CandidateSignals.already_tracklisted`) that
:func:`~phaze.services.tracklist_candidate_queue.load_candidate_signals` performs for the whole
corpus. Getting those exactly right for one file requires the same joins the corpus-wide query
already pays for every file at once; duplicating them here would be the highest-cost, lowest-
value part of this view. The authoritative funnel -- including those exclusions -- is the drain
progress fragment (:func:`phaze.tasks.tracklist_drain.tracklist_drain_status`), which this module
does not re-derive (per the bead: "use that rather than inventing a second status path").
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.enums.tracklist_candidate import CandidateClass
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.models.tracklist_priority_flag import TracklistPriorityFlag
from phaze.services.tracklist_candidates import CandidateSignals, classify, group_unique_sets
from phaze.services.tracklist_lookup_cache import CacheVerdict, lookup
from phaze.services.tracklist_query import derive_query


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.tracklist_lookup_cache import TracklistLookupCache


# --------------------------------------------------------------------------------------------
# Flag persistence
# --------------------------------------------------------------------------------------------


async def flag_file_for_lookup(session: AsyncSession, file_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Upsert the priority flag for ``file_id`` -- idempotent, re-stamps ``updated_at``.

    ``ON CONFLICT DO UPDATE`` rather than check-then-insert: a double-click of the "Prioritize"
    button must never raise a UNIQUE violation, it must just re-confirm the same intent.
    """
    moment = now or datetime.now(UTC)
    statement = pg_insert(TracklistPriorityFlag).values(file_id=file_id, created_at=moment, updated_at=moment)
    upsert = statement.on_conflict_do_update(index_elements=[TracklistPriorityFlag.file_id], set_={"updated_at": moment})
    await session.execute(upsert)


async def unflag_file(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Clear ``file_id``'s priority flag, if any. A no-op (not an error) when none exists."""
    await session.execute(delete(TracklistPriorityFlag).where(TracklistPriorityFlag.file_id == file_id))


async def is_flagged(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """True when ``file_id`` currently carries a priority flag."""
    return (await session.get(TracklistPriorityFlag, file_id)) is not None


async def load_flagged_file_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Every file id the operator has flagged, persisted or not yet resolved.

    Read by :func:`phaze.services.tracklist_drain.build_drain_queue` on EVERY call -- this is
    what makes a flag outlive the single job it was originally passed into. A file whose unique
    set the drain has already resolved (found, or a cached negative) simply will not appear in
    ``DrainQueue.entries`` regardless of what this returns, so a stale flag on an answered file
    is inert rather than wrong.
    """
    result = await session.execute(select(TracklistPriorityFlag.file_id))
    return set(result.scalars().all())


# --------------------------------------------------------------------------------------------
# Per-file review
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileTracklistReview:
    """Everything the record page shows for one file's 1001Tracklists status.

    Exactly one of ``tracklist`` / ``cache_entry`` / neither is populated at a time:

    * ``tracklist`` set -- a real result exists. ``is_propagated`` says whether IT was scraped
      for this exact file or inherited from a byte-identical duplicate (phaze-fq9h.7); never
      collapse the two, that distinction is this bead's own acceptance criterion.
    * ``cache_entry`` set (``tracklist`` ``None``) -- a lookup was attempted and did NOT produce
      a tracklist for this file. ``cache_entry.outcome`` is a
      :class:`~phaze.enums.tracklist_candidate.LookupOutcome` value and must be read literally:
      ``not_found`` is a fact about the world, everything else is a statement about us that will
      be retried (see the module docstring's outcome table in
      :mod:`phaze.services.tracklist_drain`).
    * neither set -- never looked up. ``eligible`` says whether it plausibly ever will be
      (LIVE_SET classification); ``flagged`` says whether the operator has already asked for it.
    """

    file_id: uuid.UUID
    flagged: bool
    tracklist: Tracklist | None
    tracks: tuple[TracklistTrack, ...]
    is_propagated: bool
    cache_entry: TracklistLookupCache | None
    classification_class: CandidateClass | None
    """None only when the file carries no duration and no filename signal at all (the classifier
    calls that ``UNKNOWN`` too, but here it distinguishes "we could not even try" from a real
    ``UNKNOWN`` verdict -- in practice these render identically, both meaning "not eligible by
    default")."""

    @property
    def eligible(self) -> bool:
        """True when this file would enter the drain queue as a LIVE_SET candidate.

        ``UNKNOWN`` and ``TRACK`` files are excluded from :func:`~phaze.services
        .tracklist_candidate_queue.build_queue_from_signals` by default (a wrong guess spends a
        request the drain can never reclaim), so prioritizing one of those has no effect -- the
        UI hides the control rather than offering a button that silently does nothing.
        """
        return self.classification_class is CandidateClass.LIVE_SET


async def get_file_tracklist_review(session: AsyncSession, file_id: uuid.UUID) -> FileTracklistReview | None:
    """Assemble one file's tracklist review, or ``None`` if the file does not exist."""
    file = await session.get(FileRecord, file_id)
    if file is None:
        return None

    flagged = await is_flagged(session, file_id)

    tracklist_result = await session.execute(select(Tracklist).where(Tracklist.file_id == file_id).order_by(Tracklist.updated_at.desc()).limit(1))
    tracklist = tracklist_result.scalar_one_or_none()

    if tracklist is not None:
        tracks: tuple[TracklistTrack, ...] = ()
        if tracklist.latest_version_id is not None:
            track_result = await session.execute(
                select(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id).order_by(TracklistTrack.position)
            )
            tracks = tuple(track_result.scalars().all())
        return FileTracklistReview(
            file_id=file_id,
            flagged=flagged,
            tracklist=tracklist,
            tracks=tracks,
            is_propagated=tracklist.propagated_from_set_key is not None,
            cache_entry=None,
            classification_class=CandidateClass.LIVE_SET,
        )

    classification_class, cache_entry = await _lookup_status_without_a_tracklist(session, file)
    return FileTracklistReview(
        file_id=file_id,
        flagged=flagged,
        tracklist=None,
        tracks=(),
        is_propagated=False,
        cache_entry=cache_entry,
        classification_class=classification_class,
    )


async def _lookup_status_without_a_tracklist(session: AsyncSession, file: FileRecord) -> tuple[CandidateClass | None, TracklistLookupCache | None]:
    """Classify ``file`` and, if it is a LIVE_SET candidate, fetch the cache's last verdict.

    See the module docstring's simplification note: this reads duration + filename only, not the
    full already-tracklisted funnel.
    """
    metadata_result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file.id).limit(1))
    metadata = metadata_result.scalar_one_or_none()

    filename = file.original_filename_repaired or file.original_filename
    signals = CandidateSignals(
        file_id=file.id,
        filename=filename,
        sha256_hash=file.sha256_hash,
        original_path=file.original_path,
        file_type=file.file_type,
        file_size=file.file_size,
        duration_seconds=metadata.duration if metadata else None,
        bitrate=metadata.bitrate if metadata else None,
        track_number=metadata.track_number if metadata else None,
        artist=metadata.artist if metadata else None,
        title=metadata.title if metadata else None,
        album=metadata.album if metadata else None,
    )
    classification = classify(signals)
    if classification.candidate_class is not CandidateClass.LIVE_SET:
        return classification.candidate_class, None

    derived = derive_query(signals.filename)
    signals = replace(signals, derived_query=derived.query)
    unique_sets = group_unique_sets([signals])
    if not unique_sets:  # pragma: no cover - defensive; a single-element input always yields one cluster
        return classification.candidate_class, None

    verdict: CacheVerdict = await lookup(session, unique_sets[0].key)
    return classification.candidate_class, verdict.entry
