"""DB-free enums for the 1001Tracklists candidate-set builder (phaze-fq9h.3).

The drain (phaze-fq9h.7) is capped by an EXTERNAL politeness ceiling -- robots.txt asks for an
8s crawl-delay against the whole 1001Tracklists HOST, so the entire system's budget is
~10,800 requests/day no matter how many workers run. At ~2.5 host requests per lookup that is
~4,300 sets/day against a ~250,000-file target. Every enum here exists to keep a request from
being spent twice:

* :class:`CandidateClass` -- never spend a lookup on an individual track.
* :class:`DuplicateConfidence` -- how sure we are that two files are the SAME set, so one lookup
  can propagate to all of them. Without audio fingerprinting (removed in epic phaze-0jpe) this is
  necessarily heuristic, so the strength of each link is carried explicitly rather than being
  flattened into a boolean the drain would have to trust blindly.
* :class:`LookupOutcome` / :class:`CacheDecision` -- remember what happened, and in particular
  keep "this set is genuinely not on 1001TL" strictly separate from "we could not see the page
  this time". Collapsing those two is the defect this module is shaped to prevent: a Turnstile
  interstitial or a render crash cached as ``not_found`` would permanently, silently delete a set
  from the queue.

Lives in ``phaze.enums`` (not ``phaze.models``) so the agent worker -- forbidden from importing
``phaze.database`` / ``phaze.models`` (Phase 26 D-03) -- and the Pydantic schemas can share one
spelling of these values with the SQLAlchemy column.
"""

from __future__ import annotations

import enum


class CandidateClass(enum.StrEnum):
    """What a file looks like, for the purpose of deciding whether it deserves a lookup."""

    LIVE_SET = "live_set"
    """A DJ set / concert / long-form recording -- the only class worth a 1001TL lookup."""

    TRACK = "track"
    """An individual track. 1001TL indexes sets, so a lookup here is a wasted request."""

    UNKNOWN = "unknown"
    """Not enough signal to decide. Excluded from the queue by default (a wrong guess costs a
    request we cannot get back), but counted separately so the operator can see the size of the
    undecided tail rather than having it silently folded into ``TRACK``."""


class DuplicateConfidence(enum.StrEnum):
    """How strongly a file is believed to be the same SET as its cluster's canonical file.

    Ordered weakest to strongest by :data:`DUPLICATE_CONFIDENCE_RANK`. The drain gates tracklist
    PROPAGATION on this (epic phaze-fq9h, second amendment): a tracklist may be written to an
    ``EXACT`` duplicate with no more thought than the canonical itself, while a ``LOW`` link is a
    guess that should stay behind an operator confirmation.
    """

    LOW = "low"
    """Same derived query, but the duration signal is missing on one side -- the pair could not be
    corroborated by anything except text that scene-release filenames routinely get wrong."""

    MEDIUM = "medium"
    """Same derived query and durations within tolerance, but the query carries no date. Two
    different nights of the same artist at the same event are exactly this shape, so this is the
    tier where a false merge is genuinely plausible."""

    HIGH = "high"
    """Same derived query INCLUDING a date, and durations within tolerance. A false merge needs
    two different sets by the same artist, at the same event, on the same date, of near-identical
    length."""

    EXACT = "exact"
    """Byte-identical content (equal sha256). Not a heuristic at all -- these are the same bytes,
    so this survives every criticism of the rest of the ladder."""


DUPLICATE_CONFIDENCE_RANK: dict[DuplicateConfidence, int] = {
    DuplicateConfidence.LOW: 0,
    DuplicateConfidence.MEDIUM: 1,
    DuplicateConfidence.HIGH: 2,
    DuplicateConfidence.EXACT: 3,
}
"""Total order over :class:`DuplicateConfidence`. A dict rather than ``IntEnum`` values so the
column keeps its readable string form while comparisons stay explicit at the call site."""


def meets_confidence(actual: DuplicateConfidence, minimum: DuplicateConfidence) -> bool:
    """Return True when ``actual`` is at least as strong as ``minimum``.

    The one place the confidence ladder is compared. Callers gating propagation should use this
    instead of comparing the StrEnum members directly -- ``StrEnum`` compares LEXICOGRAPHICALLY,
    which orders these members ``exact < high < low < medium`` and would quietly invert the gate.
    """
    return DUPLICATE_CONFIDENCE_RANK[actual] >= DUPLICATE_CONFIDENCE_RANK[minimum]


class LookupOutcome(enum.StrEnum):
    """The result of one 1001TL lookup attempt for a unique set.

    The critical distinction is :attr:`NOT_FOUND` versus everything else that produced no
    tracklist. ``NOT_FOUND`` is a statement about the WORLD (1001TL has no tracklist for this set)
    and is worth remembering for a long time. The transient outcomes are statements about US (our
    browser, our network, our selectors) and must never suppress a future attempt permanently --
    see :data:`TRANSIENT_OUTCOMES`.
    """

    FOUND = "found"
    """A tracklist was located and persisted. Cached forever: a past event's tracklist does not
    change, and re-checking it is a request stolen from a set that has never been looked at."""

    NOT_FOUND = "not_found"
    """The search ran cleanly and 1001TL genuinely has nothing for this set. The one cacheable
    negative; suppressed for the negative TTL, then re-queried (the site gains tracklists over
    time, so this can flip -- which is why the TTL exists rather than a permanent tombstone)."""

    SEARCH_FAILED = "search_failed"
    """The search request itself failed or its selectors did not parse. Says nothing about
    whether the set is on the site."""

    RENDER_FAILED = "render_failed"
    """A result was found but the detail page never rendered (browser crash, timeout, Xvfb
    failure). The set is on 1001TL -- we just could not read it."""

    BLOCKED = "blocked"
    """A Turnstile interstitial persisted through the bounded reload/retry loop. The set's
    existence is unknown AND the block is expected to be flaky (~6/8 per spike phaze-dmvs), so
    this is the outcome most dangerous to confuse with ``NOT_FOUND``."""

    PARSE_FAILED = "parse_failed"
    """The detail page rendered but produced zero tracks -- almost always our selectors drifting
    against a site redesign, not an empty tracklist."""

    @property
    def is_definitive_negative(self) -> bool:
        """True only for :attr:`NOT_FOUND` -- the sole outcome allowed to suppress re-querying."""
        return self is LookupOutcome.NOT_FOUND

    @property
    def is_transient(self) -> bool:
        """True when the attempt failed for OUR reasons and must be retried, not remembered."""
        return self in TRANSIENT_OUTCOMES


TRANSIENT_OUTCOMES: frozenset[LookupOutcome] = frozenset(
    {
        LookupOutcome.SEARCH_FAILED,
        LookupOutcome.RENDER_FAILED,
        LookupOutcome.BLOCKED,
        LookupOutcome.PARSE_FAILED,
    }
)
"""Outcomes that get a short exponential backoff instead of the negative TTL.

Deliberately spelled as a frozenset rather than "everything that is not FOUND/NOT_FOUND": adding a
future outcome should force an explicit decision about which side of the honesty line it falls on,
and an unlisted member defaults to the SAFE side (retryable, never treated as a negative)."""


class CacheDecision(enum.StrEnum):
    """What the persisted cache says the drain should do about a unique set right now."""

    MISS = "miss"
    """Never attempted. Query it."""

    HIT_POSITIVE = "hit_positive"
    """Already resolved to a tracklist. Reuse the stored ``external_id``; spend no request."""

    SUPPRESSED_NEGATIVE = "suppressed_negative"
    """Confirmed absent from 1001TL and still inside the negative TTL. Spend no request."""

    NEGATIVE_EXPIRED = "negative_expired"
    """Was confirmed absent, but the negative TTL has elapsed. Re-query -- 1001TL gains
    tracklists over time. Distinct from ``MISS`` so the operator can see re-checks separately from
    genuinely new work when reasoning about the daily budget."""

    BACKOFF = "backoff"
    """A transient failure is still inside its backoff window. Spend no request YET; this is not a
    negative and the set has not left the queue."""

    TRANSIENT_RETRY_READY = "transient_retry_ready"
    """A transient failure whose backoff has elapsed. Query it again."""

    TRANSIENT_EXHAUSTED = "transient_exhausted"
    """Repeated transient failures have hit :data:`TRANSIENT_MAX_ATTEMPTS`. Parked for operator
    attention rather than retried forever -- and pointedly NOT recorded as ``not_found``, because
    we still do not know whether the set is on 1001TL."""

    @property
    def should_query(self) -> bool:
        """True when the drain should spend a request on this set now."""
        return self in _QUERYABLE_DECISIONS


_QUERYABLE_DECISIONS: frozenset[CacheDecision] = frozenset(
    {
        CacheDecision.MISS,
        CacheDecision.NEGATIVE_EXPIRED,
        CacheDecision.TRANSIENT_RETRY_READY,
    }
)

TRANSIENT_MAX_ATTEMPTS: int = 5
"""Attempts after which a persistently-transient set stops being retried automatically.

Not a negative and not a tombstone: it becomes :attr:`CacheDecision.TRANSIENT_EXHAUSTED`, which
the admin UI (phaze-fq9h.8) can surface as "needs a human look". Five is chosen against the
spike's measured Turnstile flakiness (~6/8 success): five independent attempts at that rate leave
a ~0.03% chance of a set being parked purely by bad luck, while capping the budget any single
pathological set can burn at five requests."""
