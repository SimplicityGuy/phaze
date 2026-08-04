"""The 1001Tracklists drain: the engine that turns six built pieces into one resumable pass.

WHAT THIS MODULE IS
-------------------
Every stage below already exists and is separately tested. This module invents none of them; it
sequences them, decides what each outcome MEANS, and makes the sequence survivable across months
of restarts::

    build_drain_queue   candidates (phaze-fq9h.3) + derived queries (phaze-fq9h.2) + cache
      -> perform_lookup    search (phaze-hu8v) -> select_result (.6) -> render (.1) -> parse (.4)
      -> persist_lookup    tracklists/versions/tracks + PROPAGATION + the lookup cache (.3)

THE THREE CONSTRAINTS THAT SHAPED IT
------------------------------------

**1. One request per 8 s, for the whole host, forever.** robots.txt asks for ``Crawl-delay: 8``
against the 1001Tracklists HOST, so the entire system's budget is ~10,800 requests/day no matter
how many workers run -- adding workers cannot raise it, only make us rude. This module therefore
adds NO rate limiting of its own. Both network calls it makes reach the single module-level
schedule in ``services.tracklist_scraper.reserve_host_request_slot``: ``TracklistScraper.search``
delegates to it, and ``TracklistRenderer`` paces every navigation and every Turnstile reload
through the same function. A second limiter here -- even a correct one -- would let the host see
two independently-8s-paced streams at 4 s, which is exactly the defect phaze-wb1o fixed within one
module and the phaze-fq9h.1 lift fixed across two.

That ceiling is also why this is a DRAIN and not a pipeline stage: at ~2.5 requests per lookup the
system completes ~4,300 lookups/day, so a ~250,000-file archive is months of best-effort
background work. The levers are, in order: never spend a request twice (the cache), spend the
early ones on the most valuable sets (the priority order), and never lose progress to a restart.

**2. Dedup is a 3% lever, not "the big multiplier".** Measured over the live corpus
(phaze-fq9h.11): 9,708 lookupable sets collapse to 9,419 unique -- a collapse ratio of **1.0307**,
saving 289 requests. 97% of unique sets are singletons. Nothing here is sized on collapse, and
``limit`` bounds a run in LOOKUPS (requests spent), never in files covered.

The same measurement is why propagation gates where it does: **9,425 of the 9,451 collapsed links
are byte-identical sha256**, already found by ``services/dedup.py`` without a heuristic. So gating
propagation at :data:`~phaze.enums.tracklist_candidate.DuplicateConfidence.EXACT` costs ~0.3% of
one drain pass (26 links) and removes the false-merge risk entirely. With audio fingerprinting
gone (epic phaze-0jpe) the looser tiers are text-and-duration guesses, and phaze-fq9h.3's own
worked example of a MEDIUM false merge -- two different nights of one residency -- is a shape this
archive genuinely contains. See :data:`DEFAULT_PROPAGATION_MIN_CONFIDENCE`.

**3. A transient failure must never be cached as a real negative.** Four different things produce
"no tracklist", and only one of them is a fact about the world. ``select_result`` already returns
its refusal as a ``LookupOutcome`` carrying ``is_definitive_negative`` / ``is_transient``, and
``RenderResult.is_retryable`` says the same thing on the render side; :func:`perform_lookup`
preserves both distinctions rather than flattening them into "found / not found". Getting this
wrong converts a flaky Turnstile challenge into permanent, silent, un-noticed data loss -- the
defect phaze-hu8v was bounced in review for, in a milder form.

RESUMPTION AND IDEMPOTENCE
--------------------------
There is no checkpoint file and no cursor, because both would be a second source of truth that can
disagree with the database. Resumption is a property of the data:

* The queue is REBUILT from the corpus on every run and is already cache-filtered
  (``build_candidate_queue`` excludes positives forever, negatives for their TTL, and transients
  inside their backoff). A restarted drain therefore resumes with a strictly shorter queue, having
  re-spent nothing.
* Each candidate's outcome is committed in its OWN short transaction, immediately after the
  lookup. A crash loses at most the single in-flight request.
* The queue snapshot can be hours stale by the time a long run reaches its tail, so
  :func:`drain_once` RE-CHECKS the cache for each key immediately before spending on it. That one
  extra round trip is what makes an overlapping restart, or a concurrently-answered set, cost zero
  host requests instead of one.
* Persistence itself is re-runnable: the canonical write takes the same
  ``pg_advisory_xact_lock(hashtext(external_id))`` the legacy store path uses, propagated rows are
  matched on ``(external_id, file_id)`` before being created, and ``record_outcome`` is an
  ``ON CONFLICT (set_key) DO UPDATE`` upsert.

This module does NOT take a distributed lock per set, and that is deliberate. The rate limiter it
depends on is a PROCESS-wide serializer (see ``reserve_host_request_slot``), so a second concurrent
drain process is already outside the politeness contract regardless of what this module does. The
only correct cross-process fix is a shared limiter, and until that exists a per-set lock would buy
a false sense of safety while the host saw double the agreed rate. Within one process the
re-check + upsert make an overlap cost at most one duplicated request, never a corrupted row.

CONNECTION DISCIPLINE
---------------------
No database connection is ever held across network I/O (phaze-1bcc / phaze-igwi). Every phase --
build queue, re-check, persist -- opens a short session and closes it; the search and the render
happen with nothing checked out of the pool. A drain item can take 30+ seconds of wall clock, and
an idle-in-transaction connection for that long, repeated for months, drains the pool.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, select
import structlog

from phaze.enums.tracklist_candidate import CacheDecision, DuplicateConfidence, LookupOutcome
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.tracklist_candidate_queue import CandidateQueue, QueuedCandidate, build_queue_from_signals, load_candidate_signals
from phaze.services.tracklist_candidates import UniqueSet, group_unique_sets
from phaze.services.tracklist_lookup_cache import lookup, lookup_many, record_outcome
from phaze.services.tracklist_parser import TracklistParseError, parse_tracklist_tracks
from phaze.services.tracklist_priority import load_flagged_file_ids
from phaze.services.tracklist_query import DerivedQuery, derive_query
from phaze.services.tracklist_render import RenderOutcome, RenderResult
from phaze.services.tracklist_result_scorer import ScoredResult, select_result
from phaze.services.tracklist_scraper import DisallowedScrapeHostError, SearchParseFailureError, TracklistSearchResult


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date as _date
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.services.tracklist_candidate_queue import CandidateQueueStats
    from phaze.services.tracklist_parser import TracklistTrackPayload


logger = structlog.get_logger(__name__)


DEFAULT_PROPAGATION_MIN_CONFIDENCE: DuplicateConfidence = DuplicateConfidence.EXACT
"""The duplicate-link strength a tracklist may be propagated across. See the module docstring.

``EXACT`` is equal sha256 -- not a heuristic at all, so it survives every criticism of the rest of
the ladder -- and it costs almost nothing to insist on: 9,425 of the 9,451 collapsed links in the
measured corpus are already EXACT (phaze-fq9h.11). Loosening this is a decision about FALSE MERGES,
not about throughput, and it needs evidence rather than optimism: at MEDIUM, two different nights
of the same residency are indistinguishable, and the drain marks the wrong one tracklisted so it is
never revisited. :attr:`Tracklist.propagation_confidence` records the tier each row was written at
precisely so a later loosening stays auditable and reversible."""

TRACKLIST_SOURCE: str = "1001tracklists"
"""Every row the drain writes -- scraped or propagated -- carries the same ``source``.

The discriminator is ``propagated_from_set_key``, not ``source``: ``source`` answers "where did
this data come from" (1001Tracklists, in both cases -- the bytes are the same page) and overloading
it with "how did it reach this file" would silently change the meaning of the existing
``refresh_tracklists`` allowlist and of ``routers/cue``'s ordering."""

DEFAULT_LOOKUP_LIMIT: int = 100
"""Lookups per :func:`drain_once` invocation when the caller names no budget.

Small on purpose. A drain run is not a batch job to be completed -- it is a slice of a months-long
pass, and every slice boundary is a free, consistent restart point. At ~2.5 requests and ~8-12 s of
pacing per lookup, 100 lookups is roughly 40 minutes of wall clock, which fits comfortably inside
an operator's attention span and inside a task timeout."""


# --------------------------------------------------------------------------------------------
# Collaborator boundaries
# --------------------------------------------------------------------------------------------


class SearchClient(Protocol):
    """The slice of ``TracklistScraper`` the drain uses.

    Structural rather than nominal for the same reason ``tracklist_render`` states its browser
    boundary that way: the drain's behaviour on every search shape -- a clean hit, a stale-selector
    failure, an off-host redirect -- has to be testable from recorded fixtures, with no live
    request and no httpx client.
    """

    async def search(self, query: str) -> list[TracklistSearchResult]: ...


class DetailRenderer(Protocol):
    """The slice of ``TracklistRenderer`` the drain uses -- one detail page in, one verdict out."""

    async def render(self, url: str) -> RenderResult: ...


# --------------------------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrainCandidate:
    """One unique set the drain may spend a request on, with everything needed to spend it well."""

    queued: QueuedCandidate
    derived: DerivedQuery
    """The canonical file's ``derive_query`` output. Carried rather than re-derived at lookup time
    because the scorer needs the STRUCTURED signals (``date_ambiguous`` above all), and
    ``UniqueSet.query_text`` is a normalized string that has already thrown them away."""
    added_at: datetime | None = None
    """The newest ``files.created_at`` among the set's members -- the "recently added" signal."""
    flagged: bool = False
    """The operator asked for this set specifically. Sorts ahead of everything else."""

    @property
    def unique_set(self) -> UniqueSet:
        """The set this candidate stands for."""
        return self.queued.unique_set

    @property
    def set_key(self) -> str:
        """The cache identity: one row in ``tracklist_lookup_cache`` per key."""
        return self.queued.unique_set.key

    @property
    def priority(self) -> tuple[int, int, int, float, float, str]:
        """Sort key, best first. The bead's ordering, made total and stable.

        ``(flagged, fresh, files served, recency, classifier confidence, key)``:

        1. **Operator-flagged first.** A human waiting on an answer outranks any heuristic.
        2. **Never-asked before re-checks.** An expired negative and a retry-ready transient are
           both re-asks of questions the drain has already spent a request on; a ``MISS`` is
           ground never covered. With the budget binding, new ground wins.
        3. **Files served per lookup.** Identical cost, more answered files. This is where the
           (small) dedup collapse actually pays.
        4. **Recently added.** Freshly ingested material is what the operator is most likely to be
           looking at; a set with no known ``created_at`` sorts last within its tier rather than
           first, so missing data never jumps the queue.
        5. **Classifier confidence**, then **key** -- the key makes the order TOTAL, which is what
           lets a restarted drain resume the same sequence instead of reshuffling its tail.
        """
        recency = -self.added_at.timestamp() if self.added_at is not None else float("inf")
        return (
            0 if self.flagged else 1,
            0 if self.queued.verdict.decision is CacheDecision.MISS else 1,
            -self.unique_set.file_count,
            recency,
            -self.unique_set.classification.confidence,
            self.set_key,
        )


@dataclass(frozen=True, slots=True)
class DrainQueue:
    """The ordered work list plus the funnel that produced it."""

    entries: tuple[DrainCandidate, ...]
    stats: CandidateQueueStats
    cached: tuple[QueuedCandidate, ...]
    """Sets the cache answered. Kept so the admin UI (phaze-fq9h.8) can explain a set that vanished
    from the queue rather than leaving it merely missing."""


async def build_drain_queue(
    session: AsyncSession,
    *,
    agent_id: str | None = None,
    include_unknown: bool = False,
    flagged_file_ids: Iterable[uuid.UUID] = (),
    now: datetime | None = None,
) -> DrainQueue:
    """Load the corpus, derive queries, dedup, cache-filter and ORDER it. Writes nothing.

    This is ``build_candidate_queue`` with the phaze-fq9h.2 seam actually plugged in and the drain's
    priority applied on top. The seam matters: without it the candidate builder falls back to raw
    filenames, so scene noise (``-GROUP``, ``[WEB]``, track indices) reaches both the dedup key and
    the search query. Derivation is regex work over already-loaded rows -- no extra round trip --
    and it is done ONCE here, with the structured ``DerivedQuery`` carried through to the scorer
    rather than being recomputed from a string that no longer contains the date.

    Idempotent and side-effect free, which is the whole resumption story: re-running it after the
    drain has recorded outcomes yields a strictly shorter queue.

    ``flagged_file_ids`` is unioned with the PERSISTED priority flags
    (:func:`phaze.services.tracklist_priority.load_flagged_file_ids`) rather than replacing them
    (phaze-fq9h.8): the explicit argument lets a caller flag files for one call without writing
    anything, while the persisted store is what makes an operator's "answer this first" survive
    past the one job it was originally passed into. Callers that pass nothing still get every
    currently-flagged file honored.
    """
    moment = now or datetime.now(UTC)
    raw_signals = await load_candidate_signals(session, agent_id=agent_id)

    derived_by_file: dict[uuid.UUID, DerivedQuery] = {}
    signals = []
    for signal in raw_signals:
        derived = derive_query(signal.filename)
        derived_by_file[signal.file_id] = derived
        signals.append(replace(signal, derived_query=derived.query))

    provisional = group_unique_sets([s for s in signals if not s.already_tracklisted])
    verdicts = await lookup_many(session, [u.key for u in provisional], now=moment)
    queue: CandidateQueue = build_queue_from_signals(signals, verdicts, include_unknown=include_unknown)

    added_at = await _load_added_at(session, {member.file_id for entry in queue.entries for member in entry.unique_set.members})
    persisted_flags = await load_flagged_file_ids(session)
    flagged = set(flagged_file_ids) | persisted_flags

    candidates = [
        DrainCandidate(
            queued=entry,
            # Every member of a unique set shares a normalized query by construction, so the
            # canonical file's derivation is the set's derivation; falling back to a fresh
            # derivation of the stored query text keeps this total if that ever stops holding.
            derived=derived_by_file.get(entry.unique_set.canonical_file_id) or derive_query(entry.unique_set.query_text),
            added_at=_newest(added_at.get(member.file_id) for member in entry.unique_set.members),
            flagged=any(member.file_id in flagged for member in entry.unique_set.members),
        )
        for entry in queue.entries
    ]
    candidates.sort(key=lambda candidate: candidate.priority)
    return DrainQueue(entries=tuple(candidates), stats=queue.stats, cached=queue.cached)


async def _load_added_at(session: AsyncSession, file_ids: set[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """Bulk-load ``files.created_at`` for the queued sets' members -- the recency signal.

    Read here rather than threaded through ``CandidateSignals``: recency is a SCHEDULING input, not
    a classification or dedup one, and the candidate builder is deliberately kept runnable against
    a fixture corpus with no notion of when a row was inserted.
    """
    if not file_ids:
        return {}
    result = await session.execute(select(FileRecord.id, FileRecord.created_at).where(FileRecord.id.in_(list(file_ids))))
    return {row_id: created for row_id, created in result.all() if created is not None}


def _newest(values: Iterable[datetime | None]) -> datetime | None:
    """The latest non-None timestamp, or None when the set has no known insertion time."""
    known = [value for value in values if value is not None]
    return max(known) if known else None


# --------------------------------------------------------------------------------------------
# One lookup
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LookupAttempt:
    """Everything one attempt learned, decided BEFORE anything is written.

    Separating "what happened" from "what we store" is what keeps the honesty rule checkable: this
    object is produced with no database in scope, so the outcome can be asserted directly in a test
    against a recorded page, and :func:`persist_lookup` has no room to reinterpret it.
    """

    set_key: str
    query_text: str
    outcome: LookupOutcome
    external_id: str | None = None
    source_url: str | None = None
    result_confidence: int | None = None
    detail: str | None = None
    artist: str | None = None
    event: str | None = None
    date: _date | None = None
    tracks: tuple[TracklistTrackPayload, ...] = ()
    host_requests: int = 0
    """Requests this attempt spent against the whole-host budget -- 1 for the search, plus one per
    navigation the renderer needed. Reported so the operator can check the ~2.5-per-lookup figure
    every projection in this molecule divides by, rather than trusting it indefinitely."""

    @property
    def is_found(self) -> bool:
        """True when a tracklist was located and parsed, and is therefore worth persisting."""
        return self.outcome is LookupOutcome.FOUND


async def perform_lookup(candidate: DrainCandidate, *, search: SearchClient, renderer: DetailRenderer) -> LookupAttempt:
    """Run search -> score -> render -> parse for one unique set. No database, never raises.

    Every failure is converted into a :class:`LookupOutcome` rather than an exception, because an
    escaping exception is the one shape that loses the record of a request that has ALREADY been
    spent: the host has been asked, the 8 s has elapsed, and a traceback would leave the cache with
    nothing to show for it and the set queued to be asked again.

    The outcome mapping is the honesty boundary, so it is stated explicitly:

    ================================  ==============================  ==================
    situation                         outcome                         cacheable as "no"?
    ================================  ==============================  ==================
    search raised / selectors stale   ``SEARCH_FAILED``               no (transient)
    scorer refused, definitively      ``NOT_FOUND``                   yes (negative TTL)
    scorer refused, ambiguously       ``SEARCH_FAILED``               no (transient)
    page rendered, no track list      ``NOT_FOUND``                   yes (negative TTL)
    Turnstile survived the retries    ``BLOCKED``                     no (transient)
    timeout / navigation failure      ``RENDER_FAILED``               no (transient)
    parse partial or empty            ``PARSE_FAILED``                no (transient)
    tracks parsed                     ``FOUND``                       n/a (positive)
    ================================  ==============================  ==================

    Two of those deserve their reasoning stated rather than implied:

    * **"page rendered, no track list" is a real negative.** The scorer only reaches the render
      when it has an above-threshold, undisqualified match, so this is a page we are confident IS
      this set, which simply has no tracklist published. That is a fact about 1001Tracklists, and
      ``RenderResult.is_retryable`` deliberately excludes ``NO_TRACKLIST`` for exactly this reason.
    * **An empty parse is NOT.** A rendered page whose track containers yield nothing is our
      selectors drifting against a redesign -- phaze-fq9h.4 found every legacy per-track selector
      matching zero nodes (phaze-2akf), which is what that looks like from here. Caching it as a
      negative would erase sets from the queue in proportion to how broken our parser is, and the
      breakage would be invisible because the queue would just get shorter.
    """
    derived = candidate.derived
    base = LookupAttempt(set_key=candidate.set_key, query_text=derived.query, outcome=LookupOutcome.SEARCH_FAILED)

    try:
        results = await search.search(derived.query)
    except (SearchParseFailureError, DisallowedScrapeHostError) as exc:
        return replace(base, detail=f"search failed: {exc}", host_requests=1)
    except Exception as exc:
        logger.exception("tracklist drain search raised", set_key=candidate.set_key)
        return replace(base, detail=f"search raised {type(exc).__name__}: {exc}", host_requests=1)

    selection = select_result(derived, results)
    if selection.selected is None:
        # `rejection` is never None when `selected` is (the dataclass's stated invariant), but the
        # type is Optional, so fall back to the SAFE side -- a transient -- rather than to a
        # negative, if a future refactor ever violates it.
        return replace(
            base,
            outcome=selection.rejection or LookupOutcome.SEARCH_FAILED,
            detail=selection.reason,
            result_confidence=selection.confidence or None,
            host_requests=1,
        )

    chosen = selection.selected
    try:
        render = await renderer.render(chosen.result.url)
    except Exception as exc:
        # `render` converts navigation problems into outcomes itself, so reaching here means a
        # DisallowedScrapeHostError (the selected href left the allow-list), an XvfbError (no
        # virtual display in this deployment), or a genuine bug. All three are OUR failure, never
        # evidence about whether the set is on 1001Tracklists, so all three are transient.
        logger.exception("tracklist drain render raised", set_key=candidate.set_key, url=chosen.result.url)
        return replace(
            base,
            outcome=LookupOutcome.RENDER_FAILED,
            external_id=chosen.result.external_id,
            source_url=chosen.result.url,
            result_confidence=chosen.confidence,
            detail=f"render raised {type(exc).__name__}: {exc}",
            host_requests=2,
        )
    requests = 1 + max(render.attempts, 1)

    if render.outcome is not RenderOutcome.OK:
        return replace(
            base,
            outcome=_render_outcome(render),
            external_id=chosen.result.external_id,
            source_url=chosen.result.url,
            result_confidence=chosen.confidence,
            detail=f"render {render.outcome.value}: {render.error or 'no detail'}",
            host_requests=requests,
        )

    try:
        tracks = parse_tracklist_tracks(render.html)
    except TracklistParseError as exc:
        return replace(
            base,
            outcome=LookupOutcome.PARSE_FAILED,
            external_id=chosen.result.external_id,
            source_url=chosen.result.url,
            result_confidence=chosen.confidence,
            detail=str(exc),
            host_requests=requests,
        )

    if not tracks:
        return replace(
            base,
            outcome=LookupOutcome.PARSE_FAILED,
            external_id=chosen.result.external_id,
            source_url=chosen.result.url,
            result_confidence=chosen.confidence,
            detail="rendered page yielded zero track rows -- selectors are likely stale",
            host_requests=requests,
        )

    return _found(base, chosen=chosen, reason=selection.reason, render_requests=requests, tracks=tracks)


def _found(base: LookupAttempt, *, chosen: ScoredResult, reason: str, render_requests: int, tracks: Sequence[TracklistTrackPayload]) -> LookupAttempt:
    """Assemble the positive attempt, carrying the SELECTED ROW's metadata forward.

    Artist/event/date come from the search row rather than from the file's derived query on
    purpose: the derived values are our reading of a scene-release filename, while these are the
    site's own description of the page we are about to persist. Storing our guess next to their
    tracks would make a mis-selection unrecognisable afterwards.

    Takes the ``ScoredResult`` itself rather than the enclosing ``ResultSelection``: the caller has
    already narrowed ``selection.selected`` to non-None (it returns early otherwise), so re-deriving
    it here only to re-prove it needed a bare ``assert``, which bandit B101 rightly rejects because
    ``python -O`` strips it and the "guarantee" evaporates in exactly the build where it would
    matter. Passing the non-None value makes the precondition structural instead of asserted.
    """
    return replace(
        base,
        outcome=LookupOutcome.FOUND,
        external_id=chosen.result.external_id,
        source_url=chosen.result.url,
        result_confidence=chosen.confidence,
        detail=reason,
        artist=chosen.result.artist,
        event=chosen.result.event,
        date=chosen.row_date,
        tracks=tuple(tracks),
        host_requests=render_requests,
    )


def _render_outcome(render: RenderResult) -> LookupOutcome:
    """Map a non-OK render onto the lookup taxonomy, keeping "blocked" out of "not found"."""
    if render.outcome is RenderOutcome.NO_TRACKLIST:
        return LookupOutcome.NOT_FOUND
    if render.outcome is RenderOutcome.INTERSTITIAL_PERSISTED:
        return LookupOutcome.BLOCKED
    return LookupOutcome.RENDER_FAILED


# --------------------------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersistResult:
    """What one attempt wrote. Zeroes everywhere is a legitimate, recorded outcome."""

    tracklist_id: uuid.UUID | None = None
    tracks_written: int = 0
    propagated_files: int = 0
    propagation_skipped: int = 0
    """Members whose duplicate link was too weak to propagate across. Counted, never silently
    dropped: this is the number that says how much the ``EXACT`` gate is actually costing."""


async def persist_lookup(
    session: AsyncSession,
    candidate: DrainCandidate,
    attempt: LookupAttempt,
    *,
    propagation_min: DuplicateConfidence = DEFAULT_PROPAGATION_MIN_CONFIDENCE,
    now: datetime | None = None,
) -> PersistResult:
    """Write the tracklist (if any), propagate it, and record the outcome in the cache.

    The cache write happens for EVERY outcome, including the failures, and that is the point: an
    outcome that is not recorded is a request that gets spent again on the next pass. The caller
    commits -- one transaction per candidate, so a crash costs at most the in-flight lookup.
    """
    moment = now or datetime.now(UTC)
    result = PersistResult()
    if attempt.is_found and attempt.external_id:
        result = await _store_and_propagate(session, candidate, attempt, propagation_min=propagation_min)

    await record_outcome(
        session,
        set_key=attempt.set_key,
        query_text=attempt.query_text,
        outcome=attempt.outcome,
        external_id=attempt.external_id,
        source_url=attempt.source_url,
        result_confidence=attempt.result_confidence,
        detail=attempt.detail,
        now=moment,
    )
    return result


async def _store_and_propagate(
    session: AsyncSession,
    candidate: DrainCandidate,
    attempt: LookupAttempt,
    *,
    propagation_min: DuplicateConfidence,
) -> PersistResult:
    """Upsert the canonical tracklist, then project it onto the set's strong-enough duplicates."""
    external_id = attempt.external_id or ""
    unique_set = candidate.unique_set

    # Same serialization the legacy store path uses (phaze-5vmt): two jobs can resolve to the SAME
    # external_id, and a check-then-insert race there either violates the partial unique index or
    # writes duplicate versions that orphan one version's tracks. Transaction-scoped, released on
    # commit, and taken with no network I/O anywhere inside the transaction (phaze-1bcc).
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(external_id))))

    canonical = await _resolve_canonical(session, attempt=attempt, preferred_file_id=unique_set.canonical_file_id)

    # Whichever file OWNS the canonical row is already served by it; every other member needs its
    # own row. Usually the owner is the set's canonical file and this loop is pure propagation. It
    # is not always: the page may already have been scraped for a file OUTSIDE this set (the legacy
    # `search_tracklist` path, or a previously-tracklisted duplicate), and phaze-4a5w forbids
    # stealing that link. In that case the set's own canonical file falls through this loop and is
    # served by a projection like any other member -- correct, because its link to the row is the
    # set membership, which is exactly what `propagated_from_set_key` records.
    eligible = {member.file_id for member in unique_set.members_at_least(propagation_min)}
    propagated = 0
    skipped = 0
    for member in unique_set.members:
        if member.file_id == canonical.file_id:
            continue
        if member.file_id not in eligible:
            skipped += 1
            logger.info(
                "tracklist propagation withheld -- duplicate link below the gate",
                set_key=unique_set.key,
                external_id=external_id,
                file_id=str(member.file_id),
                confidence=member.confidence.value,
                required=propagation_min.value,
                reason=member.reason,
            )
            continue
        await _upsert_propagated(session, attempt=attempt, file_id=member.file_id, set_key=unique_set.key, confidence=member.confidence)
        propagated += 1

    return PersistResult(
        tracklist_id=canonical.id,
        tracks_written=len(attempt.tracks),
        propagated_files=propagated,
        propagation_skipped=skipped,
    )


async def _resolve_canonical(session: AsyncSession, *, attempt: LookupAttempt, preferred_file_id: uuid.UUID) -> Tracklist:
    """Return the ONE canonical row for this page, creating it or adding a version to it.

    "Canonical" means ``propagated_from_set_key IS NULL`` -- the row the partial unique index keeps
    at most one of per ``external_id``. If it already exists (this page was scraped before, by this
    drain or by the legacy ``search_tracklist`` path) the fresh tracks become a NEW VERSION of it
    rather than a second row, which is what makes a replay after a crash-before-commit harmless.

    The file link follows phaze-4a5w exactly: take it when the row is unowned or already points at
    the file we wanted, and otherwise leave it alone and log. Never steal -- the existing link may
    be one a human accepted manually, and silently flipping it would vanish the tracklist from that
    file's every view with no audit trail.
    """
    external_id = attempt.external_id or ""
    found = await session.execute(select(Tracklist).where(Tracklist.external_id == external_id, Tracklist.propagated_from_set_key.is_(None)).limit(1))
    tracklist = found.scalar_one_or_none()

    if tracklist is None:
        return await _write_row(session, attempt=attempt, file_id=preferred_file_id, set_key=None, confidence=None)

    if tracklist.file_id is None or tracklist.file_id == preferred_file_id:
        tracklist.file_id = preferred_file_id
        tracklist.match_confidence = attempt.result_confidence
        tracklist.auto_linked = True
    else:
        logger.warning(
            "canonical tracklist already linked to another file -- serving this set by projection",
            external_id=external_id,
            tracklist_id=str(tracklist.id),
            existing_file_id=str(tracklist.file_id),
            candidate_file_id=str(preferred_file_id),
        )
    await _append_version(session, tracklist, attempt)
    return tracklist


async def _upsert_propagated(
    session: AsyncSession,
    *,
    attempt: LookupAttempt,
    file_id: uuid.UUID,
    set_key: str,
    confidence: DuplicateConfidence,
) -> Tracklist:
    """Create or refresh the projection of this page onto ONE duplicate file.

    Structurally identical to a scraped row -- same version chain, same track rows -- so every
    existing consumer (``stage_status``, ``routers/tags``, ``routers/cue``, the pipeline's
    "untracked files" set) reads it without knowing the difference. That sameness is the point:
    a propagation the pipeline cannot see is not a propagation, it is a file that gets looked up
    again on the next pass, spending the request propagation exists to save.

    The two provenance columns are the ONLY difference, and they are what makes a false merge
    correctable: one ``DELETE ... WHERE propagated_from_set_key = :key`` reverses a whole cluster
    and leaves the canonical scrape intact.
    """
    existing = await session.execute(
        select(Tracklist)
        .where(
            Tracklist.external_id == (attempt.external_id or ""),
            Tracklist.file_id == file_id,
            Tracklist.propagated_from_set_key.is_not(None),
        )
        .limit(1)
    )
    tracklist = existing.scalar_one_or_none()
    if tracklist is None:
        return await _write_row(session, attempt=attempt, file_id=file_id, set_key=set_key, confidence=confidence)

    tracklist.propagated_from_set_key = set_key
    tracklist.propagation_confidence = confidence.value
    tracklist.match_confidence = attempt.result_confidence
    await _append_version(session, tracklist, attempt)
    return tracklist


async def _write_row(
    session: AsyncSession,
    *,
    attempt: LookupAttempt,
    file_id: uuid.UUID,
    set_key: str | None,
    confidence: DuplicateConfidence | None,
) -> Tracklist:
    """Insert one tracklist row and its first version."""
    tracklist = Tracklist(
        external_id=attempt.external_id or "",
        source_url=attempt.source_url or "",
        file_id=file_id,
        match_confidence=attempt.result_confidence,
        auto_linked=True,
        artist=attempt.artist,
        event=attempt.event,
        date=attempt.date,
        source=TRACKLIST_SOURCE,
        propagated_from_set_key=set_key,
        propagation_confidence=confidence.value if confidence is not None else None,
    )
    session.add(tracklist)
    await session.flush()
    await _append_version(session, tracklist, attempt)
    return tracklist


async def _append_version(session: AsyncSession, tracklist: Tracklist, attempt: LookupAttempt) -> None:
    """Add the next version to ``tracklist`` and write ``attempt.tracks`` into it.

    Never nulls out a resolved value with an unresolved one -- phaze-gfyr's rule, which matters
    here for the same reason it did there: a re-lookup that resolved less than the first one must
    degrade nothing. It cannot write an EMPTY version at all, because ``perform_lookup`` records a
    track-less render as ``PARSE_FAILED`` and this function is never reached for it.
    """
    if attempt.artist is not None:
        tracklist.artist = attempt.artist
    if attempt.event is not None:
        tracklist.event = attempt.event
    if attempt.date is not None:
        tracklist.date = attempt.date
    if attempt.source_url:
        tracklist.source_url = attempt.source_url

    latest = await session.execute(
        select(TracklistVersion).where(TracklistVersion.tracklist_id == tracklist.id).order_by(TracklistVersion.version_number.desc()).limit(1)
    )
    previous = latest.scalar_one_or_none()
    next_version = (previous.version_number + 1) if previous else 1

    version = TracklistVersion(tracklist_id=tracklist.id, version_number=next_version)
    session.add(version)
    await session.flush()
    tracklist.latest_version_id = version.id

    for track in attempt.tracks:
        session.add(
            TracklistTrack(
                version_id=version.id,
                position=track.position,
                artist=track.artist,
                title=track.title,
                label=track.label,
                timestamp=track.timestamp,
                is_mashup=track.is_mashup,
                remix_info=track.remix_info,
                confidence=track.confidence,
            )
        )


# --------------------------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DrainReport:
    """One pass's accounting. Every candidate lands in exactly one counter."""

    queued: int = 0
    """Sets the queue held when the pass started -- the remaining work, not this pass's slice."""
    attempted: int = 0
    found: int = 0
    not_found: int = 0
    transient: int = 0
    skipped_cached: int = 0
    """Answered by the cache between the queue snapshot and the attempt -- the re-check's yield,
    and the number that proves a restart re-spends nothing."""
    stopped_early: bool = False
    host_requests: int = 0
    tracks_written: int = 0
    propagated_files: int = 0
    propagation_skipped: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    """Per-``LookupOutcome`` counts. The transient breakdown is the diagnostic that separates "the
    site is blocking us" from "our selectors broke", which the aggregate cannot."""

    def as_dict(self) -> dict[str, Any]:
        """Flatten to the JSON-safe mapping a SAQ task returns."""
        return {
            "queued": self.queued,
            "attempted": self.attempted,
            "found": self.found,
            "not_found": self.not_found,
            "transient": self.transient,
            "skipped_cached": self.skipped_cached,
            "stopped_early": self.stopped_early,
            "host_requests": self.host_requests,
            "tracks_written": self.tracks_written,
            "propagated_files": self.propagated_files,
            "propagation_skipped": self.propagation_skipped,
            "outcomes": dict(self.outcomes),
        }


SessionFactory = Callable[[], Any]
"""A zero-argument async-context-manager factory -- SAQ's ``ctx["async_session"]``.

Typed loosely on purpose: the drain must open and close MANY short sessions rather than being
handed one long-lived session, and that requirement is what the factory expresses. A single
``AsyncSession`` parameter would make holding a pooled connection across a 30 s render the
DEFAULT."""


async def drain_once(
    session_factory: SessionFactory,
    *,
    search: SearchClient,
    renderer: DetailRenderer,
    limit: int = DEFAULT_LOOKUP_LIMIT,
    agent_id: str | None = None,
    include_unknown: bool = False,
    flagged_file_ids: Iterable[uuid.UUID] = (),
    propagation_min: DuplicateConfidence = DEFAULT_PROPAGATION_MIN_CONFIDENCE,
    should_stop: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> DrainReport:
    """Drain up to ``limit`` unique sets, in priority order, committing after each.

    ``limit`` is a budget in LOOKUPS -- i.e. in host requests -- not in files or in time, because
    requests are the scarce resource. A pass ends when the budget is spent, the queue is empty, or
    ``should_stop`` returns True; all three are ordinary, and the next pass resumes from the
    rebuilt queue with nothing re-spent.

    ``should_stop`` is checked BETWEEN candidates, never mid-lookup: abandoning a lookup after the
    search has already been sent would discard a request that has been spent and cannot be
    reclaimed, so a shutdown waits out at most one in-flight item.
    """
    moment = now or datetime.now(UTC)
    async with session_factory() as session:
        queue = await build_drain_queue(
            session,
            agent_id=agent_id,
            include_unknown=include_unknown,
            flagged_file_ids=flagged_file_ids,
            now=moment,
        )

    report = DrainReport(queued=len(queue.entries))
    logger.info(
        "tracklist drain started",
        queued=report.queued,
        limit=limit,
        propagation_min=propagation_min.value,
        projected_days=round(queue.stats.projected_days, 1),
    )

    for candidate in queue.entries:
        if report.attempted >= limit:
            break
        if should_stop is not None and should_stop():
            report.stopped_early = True
            break

        # The snapshot can be hours old by the time a long pass reaches its tail. Re-ask the cache
        # about THIS key, under the current clock, before spending: this is what makes an
        # overlapping restart cost zero requests instead of one per already-answered set.
        async with session_factory() as session:
            verdict = await lookup(session, candidate.set_key)
        if not verdict.should_query:
            report.skipped_cached += 1
            continue

        attempt = await perform_lookup(candidate, search=search, renderer=renderer)

        async with session_factory() as session:
            persisted = await persist_lookup(session, candidate, attempt, propagation_min=propagation_min)
            await session.commit()

        _tally(report, attempt, persisted)
        logger.info(
            "tracklist drain lookup recorded",
            set_key=attempt.set_key,
            outcome=attempt.outcome.value,
            external_id=attempt.external_id,
            confidence=attempt.result_confidence,
            tracks=persisted.tracks_written,
            propagated=persisted.propagated_files,
            host_requests=attempt.host_requests,
        )

    logger.info("tracklist drain finished", **report.as_dict())
    return report


def _tally(report: DrainReport, attempt: LookupAttempt, persisted: PersistResult) -> None:
    """Fold one completed candidate into the running report."""
    report.attempted += 1
    report.host_requests += attempt.host_requests
    report.tracks_written += persisted.tracks_written
    report.propagated_files += persisted.propagated_files
    report.propagation_skipped += persisted.propagation_skipped
    report.outcomes[attempt.outcome.value] = report.outcomes.get(attempt.outcome.value, 0) + 1

    if attempt.outcome is LookupOutcome.FOUND:
        report.found += 1
    elif attempt.outcome.is_definitive_negative:
        report.not_found += 1
    else:
        report.transient += 1
