"""Persisted per-unique-set record of what happened the last time we asked 1001Tracklists.

WHY THIS TABLE EXISTS (phaze-fq9h.3)
-------------------------------------
The drain's budget is ~4,300 lookups/day for the whole system, against a corpus whose unique-set
count is in the tens of thousands. It runs for months and it restarts. Without a persisted record,
every restart re-asks questions already answered -- and the *cheapest* possible request is the one
never made, which is also the politest thing we can do to a host that asked for an 8s crawl-delay.

WHY ``outcome`` IS NOT A BOOLEAN
---------------------------------
The obvious design is ``found: bool``. It is wrong, and wrong in the direction that silently
destroys work. Four different things produce "no tracklist":

* the set is genuinely not on 1001TL (:attr:`LookupOutcome.NOT_FOUND`),
* a Turnstile interstitial survived the bounded reload loop (``BLOCKED`` -- measured ~2/8 of
  attempts in spike phaze-dmvs, so this is COMMON),
* the detail page never rendered (``RENDER_FAILED``),
* the page rendered but our selectors matched nothing (``PARSE_FAILED`` / ``SEARCH_FAILED``).

Only the first is a fact about the world. Storing the other three as "not found" would remove
those sets from the queue for the negative TTL -- a permanent-feeling data loss produced by a
flaky browser, with no error anywhere and no way for the operator to tell it happened. So the
column stores the actual outcome, and :mod:`phaze.services.tracklist_lookup_cache` decides what
each one means for re-querying.

``expires_at`` therefore carries two different meanings, disambiguated by ``outcome``:
a NEGATIVE TTL for ``NOT_FOUND`` (long -- the site does gain tracklists, so it must eventually be
re-checked) and a short exponential BACKOFF for the transient outcomes. ``FOUND`` rows have
``expires_at IS NULL``: a past event's tracklist does not change.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- SQLAlchemy resolves Mapped[] annotations at runtime
import uuid

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class TracklistLookupCache(TimestampMixin, Base):
    """One row per unique set ever looked up -- positives, negatives, and honest failures."""

    __tablename__ = "tracklist_lookup_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    set_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    """sha256 hex of ``normalized query | duration bucket`` -- ``services.tracklist_candidates.set_key``.

    Hashed rather than the raw query so the key is fixed-width, index-friendly, and safe to put in
    a log line: the raw query is derived from archive filenames, which CLAUDE.md keeps out of
    tracked and published text. ``query_text`` below carries the readable form for the admin UI,
    where showing the operator their own archive is the point."""

    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    """The normalized query this key stands for -- operator-facing display and debugging."""

    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    """A :class:`~phaze.enums.tracklist_candidate.LookupOutcome` value.

    Free ``String(20)`` with no CHECK constraint, matching the ``tracklists.source`` precedent:
    outcomes are a scraping taxonomy that will grow as the site changes, and a CHECK would turn
    each addition into a migration for no integrity gain the service layer does not already give."""

    external_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    """1001TL id on a positive -- lets a duplicate reuse the answer with zero requests."""

    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """phaze-fq9h.6 result-scoring confidence: "did we pick the right search result".

    NEVER a claim that the tracklist matches the audio -- with fingerprinting removed there is no
    way to check that, and none is planned (epic phaze-fq9h, second amendment, point 3)."""

    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Short failure detail for the transient outcomes, so a recurring block is diagnosable."""

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    """Total attempts, driving the transient backoff and the ``TRANSIENT_EXHAUSTED`` park."""

    first_attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When this row stops suppressing a re-query. NULL = never (a positive)."""

    __table_args__ = (
        # The drain's hot query is "which of these keys may I skip", already served by the UNIQUE
        # on set_key. This second index serves the sweep in the other direction -- the maintenance
        # and reporting passes that ask for expired negatives / ready-to-retry transients -- which
        # would otherwise seq-scan a table with one row per unique set in the archive.
        Index("ix_tracklist_lookup_cache_outcome_expires_at", "outcome", "expires_at"),
    )
