"""Turn the corpus into an ordered queue of unique sets the drain should actually look up.

This is the only module in the candidate-set builder that knows SQL.
:mod:`phaze.services.tracklist_candidates` (classify + dedup) and
:mod:`phaze.services.tracklist_lookup_cache` (remember) are both database-free or
session-parameterized, so the heuristics can be tuned against a fixture corpus without a
Postgres round trip -- which matters, because they are the parts that will be re-tuned against
real measurements long after this bead closes.

THE FUNNEL, in the order requests are saved:

    every file
      -> media only                      (a .nfo is not a set)
      -> not already tracklisted         (embedded tags / a .cue companion / a prior scrape)
      -> classified LIVE_SET             (never spend a lookup on an individual track)
      -> collapsed to unique sets        (one lookup, propagated to every duplicate)
      -> not answered by the cache       (positives forever, negatives for the TTL)
      = the queue

:class:`CandidateQueueStats` reports the width of the funnel at every step, because the whole
molecule's schedule is a function of the final number and the operator deserves to see where the
reduction came from rather than being handed one figure to trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, exists, func, select
from sqlalchemy.orm import aliased

from phaze.constants import EXTENSION_MAP, FileCategory
from phaze.enums.tracklist_candidate import CacheDecision, CandidateClass
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist
from phaze.services.tracklist_candidates import CandidateSignals, UniqueSet, collapse_ratio, detect_embedded_tracklist, group_unique_sets
from phaze.services.tracklist_lookup_cache import CacheVerdict, lookup_many


if TYPE_CHECKING:
    from collections.abc import Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


MEDIA_FILE_TYPES: frozenset[str] = frozenset(
    ext.lstrip(".") for ext, category in EXTENSION_MAP.items() if category in {FileCategory.MUSIC, FileCategory.VIDEO}
)
"""Audio and video only. Video counts: a concert stream is exactly the long-form recording this
molecule exists to identify, and 1001TL indexes plenty of them."""

CUE_FILE_TYPE: str = "cue"

TRACKLIST_SKIP_STATUSES: frozenset[str] = frozenset({"approved", "pending"})
"""Tracklist statuses that make a file already-tracklisted.

A ``rejected`` tracklist is the operator saying "this is the wrong set", which makes the file
*more* deserving of a fresh lookup, not less -- so rejected rows must not suppress it."""

HOST_CRAWL_DELAY_SECONDS: float = 8.0
"""robots.txt's requested delay, applied per HOST -- not per worker, not per connection."""

HOST_REQUESTS_PER_DAY: int = int(86_400 / HOST_CRAWL_DELAY_SECONDS)
"""10,800. The entire system's daily request budget. Adding workers cannot raise this."""

REQUESTS_PER_LOOKUP: float = 2.5
"""Measured host requests per completed lookup: a search, a detail render, and the fractional
cost of Turnstile reloads (~2/8 attempts need one, per spike phaze-dmvs)."""

DAILY_LOOKUP_CEILING: int = int(HOST_REQUESTS_PER_DAY / REQUESTS_PER_LOOKUP)
"""~4,320 lookups/day. The number every projection in this molecule divides by."""


@dataclass(frozen=True, slots=True)
class QueuedCandidate:
    """One unique set the drain should look up, with the cache verdict that let it through."""

    unique_set: UniqueSet
    verdict: CacheVerdict

    @property
    def priority(self) -> tuple[int, float, str]:
        """Sort key: widest propagation first, then classification confidence, then key.

        Files-served-per-lookup is the right first term precisely because the budget is the
        binding constraint -- a lookup that answers eleven files is worth eleven times one that
        answers a single file, at identical cost. The trailing key makes the order total and
        stable, so a restarted drain resumes the same sequence instead of reshuffling its tail.
        """
        return (-self.unique_set.file_count, -self.unique_set.classification.confidence, self.unique_set.key)


@dataclass(frozen=True, slots=True)
class CandidateQueueStats:
    """Funnel widths and the schedule they imply. Every field is a count the operator can check."""

    media_files: int
    already_tracklisted: int
    skipped_embedded: int
    skipped_cue: int
    skipped_scraped: int
    classified_live_set: int
    classified_track: int
    classified_unknown: int
    unique_sets: int
    cached_positive: int
    cached_negative: int
    cached_backoff: int
    cached_exhausted: int
    queued: int

    @property
    def candidate_files(self) -> int:
        """Files that reached the dedup stage -- media, untracklisted, classified as sets."""
        return self.classified_live_set

    @property
    def collapse_ratio(self) -> float:
        """Candidate files per unique set. THE number: the drain's duration divides by it."""
        return collapse_ratio(self.classified_live_set, self.unique_sets)

    @property
    def projected_days(self) -> float:
        """Days of continuous crawling to drain the queue at the host-imposed ceiling."""
        return project_drain_days(self.queued)

    @property
    def days_without_dedup(self) -> float:
        """The counterfactual: the same corpus with no dedup and no cache, for comparison."""
        return project_drain_days(self.classified_live_set)


def project_drain_days(lookups: int, *, lookups_per_day: int = DAILY_LOOKUP_CEILING) -> float:
    """Days to complete ``lookups`` at the politeness ceiling. Zero lookups is zero days."""
    if lookups <= 0:
        return 0.0
    return lookups / lookups_per_day


def candidate_signals_query(*, agent_id: str | None = None) -> Select[Any]:
    """Build the SELECT that feeds :func:`load_candidate_signals`.

    Split out from the loader so the predicate set can be asserted directly in a test -- the
    already-tracklisted exclusions are the highest-value logic here (they remove work entirely)
    and the easiest to break silently, since a broken EXISTS just quietly enqueues more.

    ``raw_tags`` is selected rather than probed in SQL: embedded-tracklist detection is a
    multi-line timestamp scan over free text, which belongs in Python where it is testable, not in
    a JSONB expression nobody will ever read again.
    """
    companion = aliased(FileRecord)
    has_cue = exists(
        select(FileCompanion.id)
        .join(companion, companion.id == FileCompanion.companion_id)
        .where(FileCompanion.media_id == FileRecord.id, companion.file_type == CUE_FILE_TYPE)
    )
    has_tracklist = exists(select(Tracklist.id).where(Tracklist.file_id == FileRecord.id, Tracklist.status.in_(sorted(TRACKLIST_SKIP_STATUSES))))

    statement = (
        select(
            FileRecord.id,
            func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename).label("filename"),
            FileRecord.sha256_hash,
            FileRecord.original_path,
            FileRecord.file_type,
            FileRecord.file_size,
            FileMetadata.duration,
            FileMetadata.bitrate,
            FileMetadata.track_number,
            FileMetadata.artist,
            FileMetadata.title,
            FileMetadata.album,
            FileMetadata.raw_tags,
            has_cue.label("has_cue"),
            has_tracklist.label("has_tracklist"),
        )
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(FileRecord.file_type.in_(sorted(MEDIA_FILE_TYPES)))
        .order_by(FileRecord.id)
    )
    if agent_id is not None:
        statement = statement.where(FileRecord.agent_id == agent_id)
    return statement


async def load_candidate_signals(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
    derived_queries: dict[uuid.UUID, str] | None = None,
) -> list[CandidateSignals]:
    """Load every media file as a :class:`CandidateSignals`, including the already-tracklisted.

    They are loaded rather than filtered in SQL so the funnel can REPORT how many were skipped and
    why. A number the operator cannot see is a number nobody notices going wrong, and "we skipped
    40,000 files as already tracklisted" is the single most load-bearing claim this module makes.

    ``derived_queries`` is the phaze-fq9h.2 seam: a caller that has real query derivation passes
    it keyed by file id, and the builder uses it in place of the filename fallback.
    """
    queries = derived_queries or {}
    result = await session.execute(candidate_signals_query(agent_id=agent_id))
    signals: list[CandidateSignals] = []
    for row in result.mappings():
        file_id = row["id"]
        signals.append(
            CandidateSignals(
                file_id=file_id,
                filename=row["filename"] or "",
                sha256_hash=row["sha256_hash"] or "",
                original_path=row["original_path"] or "",
                file_type=row["file_type"] or "",
                file_size=row["file_size"] or 0,
                duration_seconds=row["duration"],
                bitrate=row["bitrate"],
                track_number=row["track_number"],
                artist=row["artist"],
                title=row["title"],
                album=row["album"],
                derived_query=queries.get(file_id),
                has_scraped_tracklist=bool(row["has_tracklist"]),
                has_cue_companion=bool(row["has_cue"]),
                has_embedded_tracklist=detect_embedded_tracklist(row["raw_tags"]),
            )
        )
    return signals


@dataclass(frozen=True, slots=True)
class CandidateQueue:
    """The drain's work list plus the funnel that produced it."""

    entries: tuple[QueuedCandidate, ...]
    stats: CandidateQueueStats
    cached: tuple[QueuedCandidate, ...]
    """Unique sets the cache answered -- excluded from ``entries``, kept for the admin UI so a set
    that vanished from the queue can be explained rather than merely missing."""


def build_queue_from_signals(
    signals: Sequence[CandidateSignals],
    verdicts: dict[str, CacheVerdict],
    *,
    include_unknown: bool = False,
) -> CandidateQueue:
    """Run the funnel over already-loaded signals. Pure -- no session, no clock.

    ``include_unknown`` lets an operator deliberately spend budget on the undecided tail once the
    confident work is drained. Off by default: a wrong guess costs a request that cannot be
    reclaimed, and the tail is exactly the population where the classifier has no evidence.
    """
    skipped_embedded = skipped_cue = skipped_scraped = 0
    live_set = track = unknown = 0

    candidates: list[CandidateSignals] = []
    for item in signals:
        if item.already_tracklisted:
            source = item.tracklist_source
            if source == "scraped":
                skipped_scraped += 1
            elif source == "cue":
                skipped_cue += 1
            else:
                skipped_embedded += 1
            continue
        candidates.append(item)

    unique_sets = group_unique_sets(candidates)
    wanted: list[UniqueSet] = []
    for unique_set in unique_sets:
        candidate_class = unique_set.classification.candidate_class
        if candidate_class is CandidateClass.LIVE_SET:
            live_set += unique_set.file_count
            wanted.append(unique_set)
        elif candidate_class is CandidateClass.TRACK:
            track += unique_set.file_count
        else:
            unknown += unique_set.file_count
            if include_unknown:
                wanted.append(unique_set)

    queued: list[QueuedCandidate] = []
    cached: list[QueuedCandidate] = []
    counts = dict.fromkeys(
        (CacheDecision.HIT_POSITIVE, CacheDecision.SUPPRESSED_NEGATIVE, CacheDecision.BACKOFF, CacheDecision.TRANSIENT_EXHAUSTED), 0
    )
    for unique_set in wanted:
        verdict = verdicts.get(unique_set.key) or CacheVerdict(set_key=unique_set.key, decision=CacheDecision.MISS)
        entry = QueuedCandidate(unique_set=unique_set, verdict=verdict)
        if verdict.should_query:
            queued.append(entry)
        else:
            cached.append(entry)
            if verdict.decision in counts:
                counts[verdict.decision] += 1

    queued.sort(key=lambda item: item.priority)
    cached.sort(key=lambda item: item.priority)

    stats = CandidateQueueStats(
        media_files=len(signals),
        already_tracklisted=skipped_embedded + skipped_cue + skipped_scraped,
        skipped_embedded=skipped_embedded,
        skipped_cue=skipped_cue,
        skipped_scraped=skipped_scraped,
        classified_live_set=live_set,
        classified_track=track,
        classified_unknown=unknown,
        unique_sets=len(wanted),
        cached_positive=counts[CacheDecision.HIT_POSITIVE],
        cached_negative=counts[CacheDecision.SUPPRESSED_NEGATIVE],
        cached_backoff=counts[CacheDecision.BACKOFF],
        cached_exhausted=counts[CacheDecision.TRANSIENT_EXHAUSTED],
        queued=len(queued),
    )
    return CandidateQueue(entries=tuple(queued), stats=stats, cached=tuple(cached))


async def build_candidate_queue(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
    derived_queries: dict[uuid.UUID, str] | None = None,
    include_unknown: bool = False,
    now: datetime | None = None,
) -> CandidateQueue:
    """Load, classify, dedup and cache-filter the corpus into a drain-ready queue.

    Idempotent and side-effect free: it writes nothing. Re-running it after the drain has recorded
    outcomes yields a strictly shorter queue, which is the acceptance criterion "the cache
    prevents re-querying a set (positive or negative) on re-run" stated operationally.
    """
    moment = now or datetime.now(UTC)
    signals = await load_candidate_signals(session, agent_id=agent_id, derived_queries=derived_queries)

    # Group once to learn the keys, ask the cache about all of them in one round trip, then run
    # the funnel. Grouping is pure and cheap relative to the query, so doing it twice is a better
    # trade than either holding the whole ORM result open or issuing per-key SELECTs.
    provisional = group_unique_sets([s for s in signals if not s.already_tracklisted])
    verdicts = await lookup_many(session, [u.key for u in provisional], now=moment)
    return build_queue_from_signals(signals, verdicts, include_unknown=include_unknown)


def format_corpus_report(stats: CandidateQueueStats) -> str:
    """Render the funnel as the plain-text block the design doc and the admin UI both quote.

    One renderer so the documented arithmetic and the operator's live numbers can never drift into
    disagreeing about what "unique sets" means.
    """
    lines = [
        "1001Tracklists candidate funnel",
        "-------------------------------",
        f"  media files                     {stats.media_files:>9,}",
        f"  already tracklisted             {stats.already_tracklisted:>9,}  "
        f"(embedded {stats.skipped_embedded:,} / cue {stats.skipped_cue:,} / scraped {stats.skipped_scraped:,})",
        f"  classified live_set (files)     {stats.classified_live_set:>9,}",
        f"  classified track (files)        {stats.classified_track:>9,}",
        f"  classified unknown (files)      {stats.classified_unknown:>9,}",
        f"  unique sets                     {stats.unique_sets:>9,}",
        f"  collapse ratio                  {stats.collapse_ratio:>9.2f}  files per lookup",
        f"  answered by cache               {stats.cached_positive + stats.cached_negative:>9,}  "
        f"(positive {stats.cached_positive:,} / negative {stats.cached_negative:,})",
        f"  deferred (backoff/parked)       {stats.cached_backoff + stats.cached_exhausted:>9,}  "
        f"(backoff {stats.cached_backoff:,} / parked {stats.cached_exhausted:,})",
        f"  QUEUED                          {stats.queued:>9,}",
        "",
        f"  ceiling {DAILY_LOOKUP_CEILING:,} lookups/day "
        f"({HOST_REQUESTS_PER_DAY:,} host requests at {HOST_CRAWL_DELAY_SECONDS:.0f}s crawl-delay / {REQUESTS_PER_LOOKUP} per lookup)",
        f"  projected drain                 {stats.projected_days:>9.1f} days",
        f"  without dedup or cache          {stats.days_without_dedup:>9.1f} days",
    ]
    return "\n".join(lines)
