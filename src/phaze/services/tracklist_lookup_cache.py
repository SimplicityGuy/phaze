"""Read/write layer over ``tracklist_lookup_cache`` -- the "never spend a request twice" rule.

Everything the drain needs to know about a unique set it may have seen before goes through
:func:`lookup_many` (batch, because the queue builder asks about thousands of keys at once) and
:func:`record_outcome`. The interesting logic is not the SQL -- it is the mapping from a stored
:class:`~phaze.enums.tracklist_candidate.LookupOutcome` to a
:class:`~phaze.enums.tracklist_candidate.CacheDecision`, i.e. the single place that answers
"may I skip this?", and the one place the positive/negative/transient distinction is enforced.

See :mod:`phaze.models.tracklist_lookup_cache` for why that distinction is load-bearing rather
than pedantry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, DateTime, case, delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.enums.tracklist_candidate import TRANSIENT_MAX_ATTEMPTS, CacheDecision, LookupOutcome
from phaze.models.tracklist_lookup_cache import TracklistLookupCache


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


NEGATIVE_TTL_DAYS: int = 180
"""How long "1001TL does not have this set" is believed before being re-checked.

Not permanent: 1001TL gains tracklists continuously, mostly from its own community, so a set
absent today can appear next year. Not short either: at ~4,300 lookups/day, re-checking negatives
too eagerly turns the drain into a treadmill that re-asks answered questions instead of reaching
the untouched tail. Six months is roughly "once per corpus pass" at the projected drain duration,
which is the natural cadence -- the re-check costs a request only after every never-asked set has
already had its turn."""

TRANSIENT_BACKOFF_BASE_MINUTES: int = 30
"""First retry delay after a transient failure. Doubles per attempt, capped below.

Half an hour is long enough for a Turnstile challenge wave or a browser-host hiccup to pass, and
short enough that a set failing early in a months-long drain is not effectively abandoned."""

TRANSIENT_BACKOFF_MAX_HOURS: int = 24
"""Ceiling on the doubling. Beyond a day the attempt cap (``TRANSIENT_MAX_ATTEMPTS``) is the
mechanism that stops the retries, not an ever-growing sleep that hides the problem."""

IN_CLAUSE_CHUNK_SIZE: int = 10_000
"""Keys per ``SELECT ... WHERE set_key IN (...)`` statement (phaze-1x31w).

An unchunked ``.in_(list(verdicts))`` renders one bind parameter per key, and asyncpg's wire
protocol caps a single statement at 32767 bind parameters (the same PostgreSQL extended-protocol
limit ``phaze.services.bulk_insert`` chunks INSERTs against, phaze-syxv). :func:`lookup_many` is
called with every unique set key the drain currently cares about in ONE call -- tens of thousands
at the measured corpus size, and the module's own docstring sizes it "tens of thousands of keys"
at the ~250,000-file target. 10k keeps comfortably under the bind cap with headroom, and splitting
across statements is invisible to the caller: the merged verdict dict is identical to what one
(impossible, over the cap) unbounded statement would have returned."""


def chunked[T](items: Sequence[T], size: int) -> Iterator[list[T]]:
    """Yield ``items`` in consecutive slices of at most ``size`` elements.

    Shared by :func:`lookup_many` and (for the sibling ``FileRecord.id.in_(...)`` bind-count
    cluster site, phaze-1x31w) ``tracklist_drain._load_added_at``.
    """
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


@dataclass(frozen=True, slots=True)
class CacheVerdict:
    """What the cache says about one set key, plus the row that says it."""

    set_key: str
    decision: CacheDecision
    entry: TracklistLookupCache | None = None

    @property
    def should_query(self) -> bool:
        """True when the drain should spend one of the day's requests on this set."""
        return self.decision.should_query

    @property
    def external_id(self) -> str | None:
        """The cached 1001TL id on a positive hit -- the answer, for free."""
        if self.decision is CacheDecision.HIT_POSITIVE and self.entry is not None:
            return self.entry.external_id
        return None


def _decide(entry: TracklistLookupCache, now: datetime) -> CacheDecision:
    """Map a stored row to a decision. THE honesty boundary -- read this before changing it.

    ``FOUND`` never expires. ``NOT_FOUND`` -- and only ``NOT_FOUND`` -- suppresses a re-query for
    the negative TTL. Everything else is a transient failure: it earns a short backoff, then goes
    straight back into the queue, and after :data:`TRANSIENT_MAX_ATTEMPTS` it is PARKED for an
    operator rather than being reinterpreted as a negative. An unrecognized outcome string (a row
    written by a newer version, or hand-edited) falls through to ``MISS``: re-querying costs one
    request, while guessing "suppress" could silently drop a set forever.
    """
    outcome = _parse_outcome(entry.outcome)
    if outcome is LookupOutcome.FOUND:
        return CacheDecision.HIT_POSITIVE

    if outcome is LookupOutcome.NOT_FOUND:
        if entry.expires_at is not None and _aware(entry.expires_at) <= now:
            return CacheDecision.NEGATIVE_EXPIRED
        return CacheDecision.SUPPRESSED_NEGATIVE

    if outcome is not None and outcome.is_transient:
        if entry.attempts >= TRANSIENT_MAX_ATTEMPTS:
            return CacheDecision.TRANSIENT_EXHAUSTED
        if entry.expires_at is not None and _aware(entry.expires_at) > now:
            return CacheDecision.BACKOFF
        return CacheDecision.TRANSIENT_RETRY_READY

    return CacheDecision.MISS


def _parse_outcome(raw: str) -> LookupOutcome | None:
    """Parse a stored outcome string, returning None for values this version does not know."""
    try:
        return LookupOutcome(raw)
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    """Coerce a possibly-naive timestamp to UTC-aware.

    ``TIMESTAMPTZ`` round-trips as aware through asyncpg, but a row constructed in a unit test (or
    by a future driver change) can be naive, and comparing the two raises ``TypeError`` at the
    exact moment the cache is deciding whether to spend a request. Assume UTC -- the column is
    timezone-aware and every writer here uses UTC.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _backoff_delay(attempts: int) -> timedelta:
    """Exponential backoff for transient failures, capped at :data:`TRANSIENT_BACKOFF_MAX_HOURS`."""
    exponent = max(attempts - 1, 0)
    minutes = TRANSIENT_BACKOFF_BASE_MINUTES * (2**exponent)
    return timedelta(minutes=min(minutes, TRANSIENT_BACKOFF_MAX_HOURS * 60))


def compute_expires_at(outcome: LookupOutcome, attempts: int, now: datetime, *, negative_ttl_days: int = NEGATIVE_TTL_DAYS) -> datetime | None:
    """When a row written now should stop suppressing a re-query (None = never).

    Exposed rather than inlined so a test -- and the design doc's arithmetic -- can assert the
    policy directly instead of inferring it from database state.
    """
    if outcome is LookupOutcome.FOUND:
        return None
    if outcome is LookupOutcome.NOT_FOUND:
        return now + timedelta(days=negative_ttl_days)
    return now + _backoff_delay(attempts)


async def lookup_many(session: AsyncSession, set_keys: Sequence[str], *, now: datetime | None = None) -> dict[str, CacheVerdict]:
    """Return a verdict for every key in ``set_keys`` -- keys absent from the cache = ``MISS``.

    Batched on purpose: the queue builder holds tens of thousands of keys, and a per-key SELECT
    would make the cache lookup more expensive than the crawl it protects. Chunked into multiple
    round trips of at most :data:`IN_CLAUSE_CHUNK_SIZE` keys each (phaze-1x31w) -- past that count
    a single ``IN (...)`` statement would exceed asyncpg's 32767 bind-parameter cap; see
    :data:`IN_CLAUSE_CHUNK_SIZE` for the full explanation. Splitting is transparent to the caller:
    the merged dict below is what one unbounded statement would have returned.
    """
    moment = now or datetime.now(UTC)
    verdicts = {key: CacheVerdict(set_key=key, decision=CacheDecision.MISS) for key in set_keys}
    if not verdicts:
        return verdicts

    for chunk in chunked(list(verdicts), IN_CLAUSE_CHUNK_SIZE):
        result = await session.execute(select(TracklistLookupCache).where(TracklistLookupCache.set_key.in_(chunk)))
        for entry in result.scalars():
            verdicts[entry.set_key] = CacheVerdict(set_key=entry.set_key, decision=_decide(entry, moment), entry=entry)
    return verdicts


async def lookup(session: AsyncSession, set_key: str, *, now: datetime | None = None) -> CacheVerdict:
    """Single-key convenience wrapper over :func:`lookup_many`."""
    return (await lookup_many(session, [set_key], now=now))[set_key]


async def lookup_by_query_text(session: AsyncSession, query_text: str, *, now: datetime | None = None) -> CacheVerdict | None:
    """Fallback probe by the readable ``query_text`` column, ``None`` when nothing matches.

    phaze-3dwsp: a per-file review recomputes a SINGLETON ``set_key`` -- ``set_key(this file's own
    query, this file's own duration bucket)`` -- but the drain writes the row under the CLUSTER's
    key, built from the cluster's most-common query and its members' MEDIAN duration bucket. The
    two agree for a singleton cluster and can disagree for a multi-member one: a linked part whose
    own duration lands in a different 5-minute bucket than the cluster median has no way to find
    the row its own set was actually parked or answered under, by key alone.

    ``normalize_query`` already merges same-set variants (part markers, hash-linked renames) into
    one string BEFORE clustering, so the file's own normalized query is usually exactly what the
    cluster wrote -- the divergence is almost always the duration bucket, not the query. Matching
    on ``query_text`` alone (ignoring the bucket) recovers those cases without a schema change or a
    corpus-wide join. It does not recover the rarer case where the cluster's chosen query differs
    from this file's own (distinct filenames sharing only a content hash) -- persisting the
    file-to-cluster edge at write time would be the complete fix; see the bead for the tradeoff.

    Most-recently-updated match wins when more than one row shares the query text, since a newer
    attempt supersedes an older one for the same set.
    """
    moment = now or datetime.now(UTC)
    result = await session.execute(
        select(TracklistLookupCache).where(TracklistLookupCache.query_text == query_text).order_by(TracklistLookupCache.updated_at.desc()).limit(1)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        return None
    return CacheVerdict(set_key=entry.set_key, decision=_decide(entry, moment), entry=entry)


_NON_TRANSIENT_OUTCOME_VALUES: frozenset[str] = frozenset({LookupOutcome.FOUND.value, LookupOutcome.NOT_FOUND.value})
"""The stored ``outcome`` values that end a transient streak: everything NOT in
``TRANSIENT_OUTCOMES``. Spelled as a literal set (mirroring that frozenset) because it needs to
appear inside a SQL ``case()``/``in_()`` expression, which the enum's own ``is_transient`` -- a
Python-only computed property -- cannot generate."""


def _next_attempts_on_conflict(outcome: LookupOutcome) -> Any:
    """The ``attempts`` value the ON CONFLICT UPDATE should write for this write's ``outcome``.

    ``attempts`` must count the CURRENT CONSECUTIVE TRANSIENT streak, not a lifetime total
    (phaze-w1c25): both ``TRANSIENT_MAX_ATTEMPTS`` (park after 5) and the backoff exponent below
    are calibrated against "N INDEPENDENT attempts at the CURRENT problem" -- a claim that only
    holds if a definitive answer (``FOUND``/``NOT_FOUND``) resets the counter. Without this, a set
    that fails transiently a few times, resolves cleanly to ``NOT_FOUND`` (180-day TTL), then
    re-enters the queue after the TTL and hits ONE transient failure on the new streak inherits the
    OLD attempts count and can park (or over-back-off) on that single failure.

    Resets to 1 when either:

    * THIS write's outcome is ``FOUND``/``NOT_FOUND`` -- a definitive answer ends whatever streak
      preceded it, so the next transient failure (if any) starts a fresh one; or
    * the STORED row's outcome is non-transient (``_NON_TRANSIENT_OUTCOME_VALUES``) -- this write
      is the FIRST transient failure of a brand-new streak, on top of an unrelated definitive
      history.

    Otherwise this write continues an existing transient streak: increment the STORED value (never
    a Python-read value -- see :func:`record_outcome`'s docstring for why that matters under
    concurrent writers).
    """
    if outcome in (LookupOutcome.FOUND, LookupOutcome.NOT_FOUND):
        return 1
    return case(
        (TracklistLookupCache.outcome.in_(_NON_TRANSIENT_OUTCOME_VALUES), 1),
        else_=TracklistLookupCache.attempts + 1,
    )


async def record_outcome(
    session: AsyncSession,
    *,
    set_key: str,
    query_text: str,
    outcome: LookupOutcome,
    external_id: str | None = None,
    source_url: str | None = None,
    result_confidence: int | None = None,
    detail: str | None = None,
    now: datetime | None = None,
    negative_ttl_days: int = NEGATIVE_TTL_DAYS,
) -> TracklistLookupCache:
    """Upsert the result of one lookup attempt.

    ``ON CONFLICT (set_key) DO UPDATE`` rather than read-modify-write: the drain is resumable and
    may briefly run two workers over the same key after a restart, and a check-then-insert there
    raises a UNIQUE violation that rolls back the surrounding transaction -- losing a result we
    just spent one of the day's ~4,300 requests to obtain. ``attempts`` -- the current consecutive
    transient streak, see :func:`_next_attempts_on_conflict` -- is computed from the STORED value
    inside the UPDATE (not from a value read earlier in Python) so a concurrent writer cannot make
    two attempts look like one and defeat the ``TRANSIENT_EXHAUSTED`` park.

    ``expires_at`` follows the same rule: on the UPDATE path the transient backoff is computed IN
    SQL from the stored ``attempts``, so it matches :func:`compute_expires_at` exactly rather than
    being approximated from a possibly-stale Python read.
    """
    moment = now or datetime.now(UTC)
    insert_attempts = 1
    insert_expires = compute_expires_at(outcome, insert_attempts, moment, negative_ttl_days=negative_ttl_days)

    statement = pg_insert(TracklistLookupCache).values(
        set_key=set_key,
        query_text=query_text,
        outcome=outcome.value,
        external_id=external_id,
        source_url=source_url,
        result_confidence=result_confidence,
        detail=detail,
        attempts=insert_attempts,
        first_attempted_at=moment,
        last_attempted_at=moment,
        expires_at=insert_expires,
    )
    upsert = statement.on_conflict_do_update(
        index_elements=[TracklistLookupCache.set_key],
        set_={
            "query_text": query_text,
            "outcome": outcome.value,
            "external_id": external_id,
            "source_url": source_url,
            "result_confidence": result_confidence,
            "detail": detail,
            "attempts": _next_attempts_on_conflict(outcome),
            "last_attempted_at": moment,
            "expires_at": _update_expires_at(outcome, moment, negative_ttl_days),
        },
    ).returning(TracklistLookupCache)

    # ``populate_existing`` is REQUIRED, not decoration: RETURNING hands the ORM a row for a
    # primary key the identity map may already hold from an earlier attempt in the same session,
    # and by default SQLAlchemy keeps the already-loaded (pre-increment) attribute values. Without
    # it, ``attempts`` reads back as its stale value and the ``TRANSIENT_EXHAUSTED`` park -- the
    # thing that stops a pathological set burning budget forever -- never trips.
    result = await session.execute(upsert, execution_options={"populate_existing": True})
    entry: TracklistLookupCache = result.scalar_one()
    return entry


def _update_expires_at(outcome: LookupOutcome, moment: datetime, negative_ttl_days: int) -> Any:
    """The ``expires_at`` the ON CONFLICT UPDATE should write.

    Positives (never) and definitive negatives (a fixed TTL) do not depend on the attempt count and
    are plain Python values. The transient backoff DOES depend on it, and the UPDATE cannot read
    its own post-write ``attempts`` from Python without re-introducing the read-modify-write race
    the upsert exists to avoid -- so it is computed in SQL from the same
    :func:`_next_attempts_on_conflict` expression the ``attempts`` column itself is written from
    (phaze-w1c25: BOTH must agree on what this write's attempts count IS, or a streak reset would
    write a consistent ``attempts`` column next to a backoff computed from the old, un-reset
    count). :func:`_backoff_delay` is ``base * 2**(attempts - 1)``, so the exponent here is
    ``_next_attempts_on_conflict(outcome) - 1`` -- the SQL and the Python policy agree by
    construction, which ``test_backoff_sql_matches_python_policy_across_attempts`` pins.

    ``make_interval`` rather than a text interval literal: it takes bound parameters, so no SQL is
    ever assembled from a formatted string (semgrep's ``formatted-sql-query`` rule blocks that in
    CI, and rightly).
    """
    if outcome is LookupOutcome.FOUND:
        return None
    if outcome is LookupOutcome.NOT_FOUND:
        return moment + timedelta(days=negative_ttl_days)
    exponent = _next_attempts_on_conflict(outcome) - 1
    backoff_seconds = func.least(
        float(TRANSIENT_BACKOFF_BASE_MINUTES * 60) * func.power(2.0, exponent),
        float(TRANSIENT_BACKOFF_MAX_HOURS * 3600),
    )
    return literal(moment, DateTime(timezone=True)) + func.make_interval(0, 0, 0, 0, 0, 0, backoff_seconds)


async def purge_expired_negatives(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete negatives whose TTL has elapsed; return the number removed.

    Optional hygiene, not correctness: :func:`_decide` already reports an elapsed negative as
    ``NEGATIVE_EXPIRED`` and re-queues it. This exists so the table does not accumulate rows the
    drain will rewrite anyway. Transient and exhausted rows are deliberately NOT purged -- their
    ``attempts`` count is the only record of a set that keeps failing, and deleting it would reset
    the ``TRANSIENT_EXHAUSTED`` park and start the futile retries over.
    """
    moment = now or datetime.now(UTC)
    result = await session.execute(
        delete(TracklistLookupCache).where(
            TracklistLookupCache.outcome == LookupOutcome.NOT_FOUND.value,
            TracklistLookupCache.expires_at.isnot(None),
            TracklistLookupCache.expires_at <= moment,
        )
    )
    return cast("CursorResult[Any]", result).rowcount or 0
