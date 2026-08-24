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

SIMPLIFICATION THIS MODULE MAKES, STATED PLAINLY -- AND THE ONE IT DELIBERATELY DOES NOT
------------------------------------------------------------------------------------------
:func:`get_file_tracklist_review`'s eligibility read (would this file ever enter the drain
queue) uses ONLY duration and filename for CLASSIFICATION -- the same
:func:`~phaze.services.tracklist_candidates.classify` a full corpus pass uses. Getting the
corpus-wide query's OTHER signals (recency, propagation membership) exactly right for one file
would require the same joins the corpus-wide query already pays for every file at once;
duplicating those here would be the highest-cost, lowest-value part of this view. The
authoritative funnel is the drain progress fragment
(:func:`phaze.tasks.tracklist_drain.tracklist_drain_status`), which this module does not
re-derive (per the bead: "use that rather than inventing a second status path").

The "already answered by another source" check is NOT skipped, though, and that distinction
matters: :attr:`~phaze.services.tracklist_candidates.CandidateSignals.already_tracklisted`
(cue-companion / embedded-tracklist) is what keeps a file OUT of
:func:`~phaze.services.tracklist_candidate_queue.build_queue_from_signals` entirely, before
classification even runs. Skipping it here would let :attr:`FileTracklistReview.eligible` say
"yes" for a file the drain would never look at, and the admin UI would then let an operator spend
a real request from the whole-host 8s budget on a COMPLETELY UNRELATED set while believing they
were answering this one (:func:`phaze.routers.pipeline.prioritize_tracklist_lookup_ui` enqueues a
``limit=1`` slice that answers whatever actually sits at the front of the real queue). So this
check IS replicated -- cheaply, since it is two per-file reads (a raw_tags column already loaded
for classification, and one EXISTS query), not a corpus-wide join.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from phaze.enums.tracklist_candidate import CacheDecision, CandidateClass
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.models.tracklist_priority_flag import TracklistPriorityFlag
from phaze.services.tracklist_candidate_queue import CUE_FILE_TYPE
from phaze.services.tracklist_candidates import CandidateSignals, classify, detect_embedded_tracklist, group_unique_sets
from phaze.services.tracklist_lookup_cache import CacheVerdict, lookup, lookup_by_query_text
from phaze.services.tracklist_query import derive_query


if TYPE_CHECKING:
    from collections.abc import Collection
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


async def flag_files_for_lookup(session: AsyncSession, file_ids: Collection[uuid.UUID], *, now: datetime | None = None) -> None:
    """Upsert the priority flag for a whole set at once -- the bulk twin of :func:`flag_file_for_lookup`.

    phaze-vu88k.4: added for :func:`phaze.tasks.tracklist.refresh_tracklists`, which flagged every
    file a refreshed page serves one round trip at a time. Bulk for exactly the reason
    :func:`clear_flags` gives directly below: a page can serve many files and this runs inside the
    caller's transaction, where a round trip per file is pure cost.

    Semantically identical to calling :func:`flag_file_for_lookup` per id -- the same
    ``ON CONFLICT DO UPDATE`` on the same index, so a double-flag still re-confirms rather than
    raising. Empty input is a no-op, not an unfiltered INSERT.

    ONE deliberate difference: every row in the set is stamped with ONE ``moment`` rather than each
    getting its own ``datetime.now(UTC)`` microseconds apart. Nothing can rely on that ordering --
    the per-file caller this replaces iterated a ``set``, so the relative stamps were already in
    arbitrary hash order rather than any meaningful sequence.
    """
    ids = list(file_ids)
    if not ids:
        return
    moment = now or datetime.now(UTC)
    statement = pg_insert(TracklistPriorityFlag).values([{"file_id": fid, "created_at": moment, "updated_at": moment} for fid in ids])
    upsert = statement.on_conflict_do_update(index_elements=[TracklistPriorityFlag.file_id], set_={"updated_at": moment})
    await session.execute(upsert)


async def unflag_file(session: AsyncSession, file_id: uuid.UUID) -> None:
    """Clear ``file_id``'s priority flag, if any. A no-op (not an error) when none exists."""
    await session.execute(delete(TracklistPriorityFlag).where(TracklistPriorityFlag.file_id == file_id))


async def clear_flags(session: AsyncSession, file_ids: Collection[uuid.UUID]) -> None:
    """Retire the flags for a whole unique set at once -- the drain's post-answer sweep.

    Called by :func:`phaze.services.tracklist_drain.persist_lookup` when a lookup produces a
    DEFINITIVE outcome. Bulk rather than a loop over :func:`unflag_file` because a set can carry
    many members and this runs inside the per-candidate transaction, where an extra round trip per
    duplicate is pure cost.

    phaze-2akf: this became load-bearing when a flag stopped being purely advisory. A flag on an
    already-tracklisted file now re-admits that file past the funnel's already-tracklisted filter
    (the refresh override), so leaving one in place after the refresh has been serviced would
    re-queue the same set on every future pass. Empty input is a no-op, not an unfiltered DELETE.
    """
    ids = list(file_ids)
    if not ids:
        return
    await session.execute(delete(TracklistPriorityFlag).where(TracklistPriorityFlag.file_id.in_(ids)))


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

    phaze-2akf: "inert" now depends on the drain retiring the flag once it answers the set (see
    :func:`clear_flags`), because a flag on an ALREADY-TRACKLISTED file is the refresh override and
    re-admits that file past the funnel's already-tracklisted filter. The cache still bounds the
    damage of a leaked flag -- a set whose row says FOUND lands in ``cached``, not ``entries``, and
    costs no request -- but the flag is no longer a pure no-op, so it is cleared deliberately.
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
    latest_version: TracklistVersion | None
    tracks: tuple[TracklistTrack, ...]
    is_propagated: bool
    cache_entry: TracklistLookupCache | None
    classification_class: CandidateClass | None
    """None only when the file carries no duration and no filename signal at all (the classifier
    calls that ``UNKNOWN`` too, but here it distinguishes "we could not even try" from a real
    ``UNKNOWN`` verdict -- in practice these render identically, both meaning "not eligible by
    default")."""
    answered_elsewhere: str | None = None
    """Set (to ``"embedded tracklist"`` or ``"cue companion"``) when the file already carries
    track data from a source OTHER than a scraped ``tracklists`` row. Such a file is excluded from
    the drain's candidate funnel BEFORE classification even runs
    (:attr:`~phaze.services.tracklist_candidates.CandidateSignals.already_tracklisted`), so it can
    never become a :class:`~phaze.services.tracklist_drain.DrainCandidate` regardless of
    ``classification_class`` -- see :attr:`eligible`."""
    cache_decision: CacheDecision | None = None
    """The :class:`~phaze.services.tracklist_lookup_cache.CacheVerdict.decision` computed alongside
    ``cache_entry`` (phaze-z8xq7). ``None`` when no cache row exists yet -- a file that has never
    been looked up carries no verdict to suppress it. See :attr:`actionable`, which is the property
    that actually reads this: :attr:`eligible` stays a pure classification/funnel-membership read
    (does this file's CLASS put it in the drain funnel at all), independent of what the cache
    currently says about it -- collapsing the two would make the "not classified as a live set"
    STATE 3b copy (:attr:`eligible` is False) indistinguishable from the "cache-suppressed" STATE 2
    copy the template renders instead."""

    @property
    def eligible(self) -> bool:
        """True when this file's CLASS would ever let it enter the drain queue as a LIVE_SET
        candidate -- independent of what the cache currently says about it (see :attr:`actionable`
        for the version that also consults the cache).

        ``UNKNOWN`` and ``TRACK`` files are excluded from :func:`~phaze.services
        .tracklist_candidate_queue.build_queue_from_signals` by default (a wrong guess spends a
        request the drain can never reclaim), so prioritizing one of those has no effect -- the
        UI hides the control rather than offering a button that silently does nothing.

        A file already answered by another source (``answered_elsewhere``) is excluded the SAME
        way even when the classifier would call it a LIVE_SET: prioritizing it would flag a file
        the drain never looks at, and the enqueued ``limit=1`` slice would then spend a live
        request on whatever unrelated set actually sits at the front of the real queue.
        """
        if self.answered_elsewhere is not None:
            return False
        return self.classification_class is CandidateClass.LIVE_SET

    @property
    def actionable(self) -> bool:
        """True when clicking Prioritize right now would actually cause a lookup (phaze-z8xq7).

        ``eligible`` alone is not enough: it is True for EVERY cache-suppressed set too (STATE 2 --
        a ``cache_entry`` with no tracklist), because it never consults the cache
        (:mod:`~phaze.services.tracklist_lookup_cache`'s ``CacheVerdict``/``CacheDecision`` --
        computed once, in :func:`_lookup_status_without_a_tracklist`, and previously discarded
        right after, keeping only ``verdict.entry``). A set the drain's own cache verdict keeps
        suppressed (``SUPPRESSED_NEGATIVE`` within its 180-day TTL, ``BACKOFF`` inside its transient
        retry window, or ``TRANSIENT_EXHAUSTED`` parked after repeated failures) never enters the
        queue even when force-flagged -- see
        :mod:`phaze.services.tracklist_candidate_queue`'s module docstring on why a forced file is
        still subject to the cache verdict. Flagging one anyway would upsert an inert flag, falsely
        tell the operator "a lookup has been dispatched", and spend the enqueued ``limit=1`` slice's
        one live request on whatever UNRELATED set actually sits at the front of the real queue --
        the exact misattributed-lookup failure mode ``eligible`` itself exists to prevent for the
        classification/funnel-membership case, just not (until now) for this one.
        """
        if not self.eligible:
            return False
        if self.cache_decision is None:
            return True
        return self.cache_decision.should_query


async def get_file_tracklist_review(session: AsyncSession, file_id: uuid.UUID) -> FileTracklistReview | None:
    """Assemble one file's tracklist review, or ``None`` if the file does not exist."""
    file = await session.get(FileRecord, file_id)
    if file is None:
        return None

    flagged = await is_flagged(session, file_id)

    tracklist_result = await session.execute(select(Tracklist).where(Tracklist.file_id == file_id).order_by(Tracklist.updated_at.desc()).limit(1))
    tracklist = tracklist_result.scalar_one_or_none()

    if tracklist is not None:
        latest_version: TracklistVersion | None = None
        tracks: tuple[TracklistTrack, ...] = ()
        if tracklist.latest_version_id is not None:
            latest_version = await session.get(TracklistVersion, tracklist.latest_version_id)
            track_result = await session.execute(
                select(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id).order_by(TracklistTrack.position)
            )
            tracks = tuple(track_result.scalars().all())
        return FileTracklistReview(
            file_id=file_id,
            flagged=flagged,
            tracklist=tracklist,
            latest_version=latest_version,
            tracks=tracks,
            is_propagated=tracklist.propagated_from_set_key is not None,
            cache_entry=None,
            classification_class=CandidateClass.LIVE_SET,
        )

    classification_class, cache_entry, cache_decision, answered_elsewhere = await _lookup_status_without_a_tracklist(session, file)
    return FileTracklistReview(
        file_id=file_id,
        flagged=flagged,
        tracklist=None,
        latest_version=None,
        tracks=(),
        is_propagated=False,
        cache_entry=cache_entry,
        classification_class=classification_class,
        answered_elsewhere=answered_elsewhere,
        cache_decision=cache_decision,
    )


async def _already_answered_elsewhere(session: AsyncSession, file: FileRecord, metadata: FileMetadata | None) -> str | None:
    """Cheap, per-file mirror of :attr:`CandidateSignals.already_tracklisted`'s OTHER two sources.

    A scraped ``tracklists`` row is checked by the caller already (a real ``Tracklist`` for this
    file means the "has a tracklist" branch runs instead of this one at all); this checks the two
    sources that make a file already-answered with NO ``tracklists`` row to show for it:

    * an embedded tracklist in the file's own tags (:func:`~phaze.services.tracklist_candidates
      .detect_embedded_tracklist` over ``metadata.raw_tags`` -- already loaded, no extra query);
    * a linked ``.cue`` companion file (one EXISTS query, mirroring
      :func:`~phaze.services.tracklist_candidate_queue.candidate_signals_query`'s join).

    Either one means :func:`~phaze.services.tracklist_candidate_queue.build_queue_from_signals`
    would exclude this file BEFORE classification runs, so it can never become a
    :class:`~phaze.services.tracklist_drain.DrainCandidate` -- see ``FileTracklistReview.eligible``.
    """
    if metadata is not None and detect_embedded_tracklist(metadata.raw_tags):
        return "embedded tracklist"

    companion = aliased(FileRecord)
    has_cue = await session.execute(
        select(
            exists(
                select(FileCompanion.id)
                .join(companion, companion.id == FileCompanion.companion_id)
                .where(FileCompanion.media_id == file.id, companion.file_type == CUE_FILE_TYPE)
            )
        )
    )
    if has_cue.scalar_one():
        return "cue companion"
    return None


def _signals_for(file: FileRecord, metadata: FileMetadata | None) -> CandidateSignals:
    """Project one file row (+ its optional metadata row) onto the classifier's input record.

    Pure field mapping, extracted from :func:`_lookup_status_without_a_tracklist` (phaze-bk9el.28)
    because it carried seven of that function's thirteen decision points while expressing no
    decision at all -- six ``metadata is None`` guards plus the repaired-filename fallback. Keeping
    them here leaves the caller's real control flow (classify -> early return -> derive -> cluster
    -> cache -> query-text fallback) legible, and makes the mapping testable without a session.

    The metadata guards are per-field rather than one branch because a file with no
    ``FileMetadata`` row is ordinary, not exceptional: ``classify`` reads duration + filename and
    is specified to cope with an absent duration.
    """
    return CandidateSignals(
        file_id=file.id,
        filename=file.original_filename_repaired or file.original_filename,
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


async def _lookup_status_without_a_tracklist(
    session: AsyncSession, file: FileRecord
) -> tuple[CandidateClass | None, TracklistLookupCache | None, CacheDecision | None, str | None]:
    """Classify ``file`` and, if it is a LIVE_SET candidate with no other answer, fetch the
    cache's last verdict.

    See the module docstring: classification itself reads duration + filename only (the
    corpus-wide funnel's OTHER signals are deliberately not replicated here), but the
    already-answered-elsewhere check runs regardless of classification, because it is what makes
    ``eligible`` -- and therefore the Prioritize button -- honest.

    phaze-z8xq7: the ``CacheVerdict`` this fetches carries a ``decision`` (``should_query``) that
    used to be discarded here, keeping only ``verdict.entry`` -- so nothing downstream could tell a
    cache-suppressed set (``SUPPRESSED_NEGATIVE`` / ``BACKOFF`` / ``TRANSIENT_EXHAUSTED``) apart
    from one the cache would actually re-query. Now returned alongside the entry, for
    ``FileTracklistReview.actionable``.
    """
    metadata_result = await session.execute(select(FileMetadata).where(FileMetadata.file_id == file.id).limit(1))
    metadata = metadata_result.scalar_one_or_none()

    answered_elsewhere = await _already_answered_elsewhere(session, file, metadata)

    signals = _signals_for(file, metadata)
    classification = classify(signals)
    if classification.candidate_class is not CandidateClass.LIVE_SET or answered_elsewhere is not None:
        return classification.candidate_class, None, None, answered_elsewhere

    derived = derive_query(signals.filename)
    signals = replace(signals, derived_query=derived.query)
    unique_sets = group_unique_sets([signals])
    if not unique_sets:  # pragma: no cover - defensive; a single-element input always yields one cluster
        return classification.candidate_class, None, None, answered_elsewhere

    verdict: CacheVerdict = await lookup(session, unique_sets[0].key)
    if verdict.entry is None:
        # phaze-3dwsp: the drain writes cache rows under the CLUSTER's key -- built from the most
        # common query and the MEDIAN duration across every member of the corpus-wide cluster --
        # while this singleton lookup was just built from this one file's own query and duration.
        # They agree for a lone file and can disagree for one part of a multi-part set (a linked
        # part's own duration bucket need not match the cluster median) or a hash-linked rename.
        # A miss on the exact key does not mean "never looked up"; probe by the readable query
        # text too before reporting that to the operator -- see lookup_by_query_text's docstring
        # for which divergence cases this does and does not recover.
        fallback = await lookup_by_query_text(session, unique_sets[0].query_text)
        if fallback is not None:
            verdict = fallback
    return classification.candidate_class, verdict.entry, verdict.decision, answered_elsewhere
