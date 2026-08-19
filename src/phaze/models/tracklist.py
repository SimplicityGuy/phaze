"""Tracklist models for 1001Tracklists-sourced tracklists."""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

    from phaze.models.file import FileRecord


CANONICAL_TRACKLIST_CLAUSE = "propagated_from_set_key IS NULL"
"""SQL predicate selecting the rows that were actually scraped from 1001Tracklists.

Spelled once because it is BOTH the partial-unique-index predicate below and the filter every
``WHERE external_id = ...`` read must carry (see :class:`Tracklist`). The two drifting apart is
how a propagated projection would start being mistaken for the canonical scrape."""


class Tracklist(TimestampMixin, Base):
    """A tracklist linked to a file.

    Source: '1001tracklists' (scraped from 1001Tracklists.com). Historical rows may still carry
    'fingerprint' from the retired audio-fingerprint scan path (phaze-0jpe); nothing writes that
    value any more and ``refresh_tracklists`` allowlists '1001tracklists', so surviving rows are
    inert. ``source`` has no CHECK constraint, so no schema change was needed to retire the value.

    CANONICAL ROWS vS PROPAGATED PROJECTIONS (phaze-fq9h.7)
    -------------------------------------------------------
    The drain looks a UNIQUE SET up once and PROPAGATES the answer to the set's duplicate files --
    that is the only reason a ~250,000-file archive is drainable against a whole-host ceiling of
    ~1 request / 8 s. Propagation has to produce a real ``Tracklist`` row per duplicate file,
    because ``Tracklist.file_id`` is what every consumer means by "this file has a tracklist":
    ``services/stage_status.done_clause``, ``services/pipeline.get_untracked_files``,
    ``routers/tags``, ``routers/cue``. A propagation recorded anywhere else would leave the
    duplicate files looking un-tracklisted, and the very next operator "search all" would spend a
    live request on each of them -- defeating the dedup the propagation exists to deliver.

    So ``external_id`` is no longer globally unique. It is unique among CANONICAL rows
    (``propagated_from_set_key IS NULL``), which is the invariant the old global UNIQUE actually
    encoded: exactly one row per 1001TL page that we scraped. Propagated rows carry the SAME
    ``external_id`` (they are the same page) plus:

    * ``propagated_from_set_key`` -- the ``services.tracklist_candidates.set_key`` of the unique set
      that produced them, non-NULL and therefore the discriminator itself; and
    * ``propagation_confidence`` -- the ``DuplicateConfidence`` tier of the link that justified it.

    Both exist because the dedup that drives propagation is HEURISTIC now (audio fingerprinting was
    removed in epic phaze-0jpe), so a false merge is possible and must be CORRECTABLE: one
    ``DELETE ... WHERE propagated_from_set_key = :key`` reverses a bad cluster and leaves the
    canonical scrape untouched. That is why the epic's second amendment insists propagated rows be
    "distinguishable from directly-scraped ones" rather than indistinguishable from a real result.

    Every read that resolves an ``external_id`` to "the row we scraped" MUST filter on
    ``propagated_from_set_key IS NULL`` (:data:`CANONICAL_TRACKLIST_CLAUSE`); a bare
    ``scalar_one_or_none()`` on ``external_id`` now raises ``MultipleResultsFound`` once a
    propagation exists, which is the loud failure this design prefers to a silent wrong pick.
    """

    __tablename__ = "tracklists"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str] = mapped_column(String(50), nullable=False)
    """The 1001TL page id. Unique among CANONICAL rows only -- see the class docstring."""
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)
    match_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, server_default="1001tracklists")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="approved")

    propagated_from_set_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """NULL on a scraped row; the unique-set key on a propagated projection (phaze-fq9h.7).

    Deliberately the unique-set KEY rather than a foreign key to the canonical row. The key is what
    ``tracklist_lookup_cache.set_key`` stores, so a suspect cluster can be traced from the cache
    entry to every row it produced and back; and it survives the canonical row's deletion, which a
    self-FK would either forbid or cascade -- and phaze's tracklist deletion cascade
    (``services/scan_deletion.py``) is an explicit ordered statement list, so a DB-level cascade
    firing inside it would delete rows out from under that ordering and orphan their versions."""

    propagation_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """The :class:`~phaze.enums.tracklist_candidate.DuplicateConfidence` value of the duplicate
    link this row was propagated across. NULL on a scraped row.

    Stored even though the drain currently propagates only at ``EXACT``, because "which tier was
    this written at" is the first question asked when a merge turns out to be wrong, and it must be
    answerable from the row rather than from whatever the gate happened to be at the time."""

    file: Mapped[FileRecord | None] = relationship("FileRecord", foreign_keys=[file_id], lazy="noload")
    versions: Mapped[list[TracklistVersion]] = relationship("TracklistVersion", back_populates="tracklist", lazy="noload")

    @classmethod
    def is_canonical(cls) -> ColumnElement[bool]:
        """The SQL form of :data:`CANONICAL_TRACKLIST_CLAUSE` -- the row actually scraped from 1001Tracklists.

        phaze-vtovq was a hand-spelled copy of this predicate ANDed onto a file-id filter where the
        row in scope was itself a propagated projection, so the intersection was always empty: the
        refresh silently did nothing. Every ``WHERE external_id = ...`` read (or any other read that
        means "the canonical row") MUST go through this classmethod rather than re-spell
        ``propagated_from_set_key.is_(None)``, so the guard in
        ``tests/shared/test_canonical_tracklist_clause.py`` can enforce it mechanically instead of
        by review.
        """
        return cls.propagated_from_set_key.is_(None)

    @classmethod
    def is_propagated(cls) -> ColumnElement[bool]:
        """The complement of :meth:`is_canonical` -- a projection propagated across a duplicate (phaze-fq9h.7).

        Note the asymmetry with ``services/tracklist_priority.py``'s
        ``tracklist.propagated_from_set_key is not None``: that is a Python ``is not`` check on an
        already-loaded INSTANCE attribute, not a SQL predicate built for a query, so it is a
        different thing wearing similar words and is deliberately NOT routed through this
        classmethod (and the guard test acquits it explicitly).
        """
        return cls.propagated_from_set_key.is_not(None)

    __table_args__ = (
        Index("ix_tracklists_file_id", "file_id"),
        # PARTIAL unique: one canonical scrape per 1001TL page, with propagated projections exempt.
        Index("ix_tracklists_external_id", "external_id", unique=True, postgresql_where=text(CANONICAL_TRACKLIST_CLAUSE)),
        # Serves the correction path -- "show/undo every row this cluster produced" -- without
        # seq-scanning a table that carries one row per tracklisted file in the archive.
        Index("ix_tracklists_propagated_from_set_key", "propagated_from_set_key", postgresql_where=text("propagated_from_set_key IS NOT NULL")),
        Index("ix_tracklists_source", "source"),
        Index("ix_tracklists_status", "status"),
    )


class TracklistVersion(TimestampMixin, Base):
    """A versioned snapshot of a tracklist's track data."""

    __tablename__ = "tracklist_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tracklist_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklists.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tracklist: Mapped[Tracklist] = relationship("Tracklist", back_populates="versions", lazy="noload")
    tracks: Mapped[list[TracklistTrack]] = relationship("TracklistTrack", back_populates="version", lazy="noload")

    # phaze-5vmt: a UNIQUE (tracklist_id, version_number) makes a concurrent version race fail loudly
    # (IntegrityError -> SAQ retry) instead of silently creating duplicate versions that orphan tracks.
    __table_args__ = (UniqueConstraint("tracklist_id", "version_number", name="uq_tracklist_versions_tracklist_id_version_number"),)


class TracklistTrack(TimestampMixin, Base):
    """An individual track within a tracklist version."""

    __tablename__ = "tracklist_tracks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklist_versions.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_mashup: Mapped[bool] = mapped_column(Boolean, default=False)
    remix_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    version: Mapped[TracklistVersion] = relationship("TracklistVersion", back_populates="tracks", lazy="noload")

    __table_args__ = (Index("ix_tracklist_tracks_version_id", "version_id"),)
