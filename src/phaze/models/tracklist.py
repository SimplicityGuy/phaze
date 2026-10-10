"""Stored tracklist models and source provenance."""

from __future__ import annotations

from datetime import date, datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

    from phaze.models.file import FileRecord


CANONICAL_TRACKLIST_CLAUSE = "propagated_from_set_key IS NULL"
"""SQL predicate selecting canonical stored rows.

Spelled once because it is BOTH the partial-unique-index predicate below and the filter every
``WHERE external_id = ...`` read must carry (see :class:`Tracklist`). The two drifting apart is
how a propagated projection would start being mistaken for the canonical scrape."""


class Tracklist(TimestampMixin, Base):
    """A tracklist linked to a file.

    New rows default to the manual source. Existing source identifiers, URLs and duplicate
    propagation fields are retained as provenance after external acquisition was retired.
    Legacy external identifiers are unique among unexpanded canonical rows; expanded rows use
    provider object identity. Inherited projections remain
    distinguishable by propagated_from_set_key. Every read resolving an external identifier
    must use is_canonical() to avoid selecting an inherited row.
    """

    __tablename__ = "tracklists"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_objects.id"))
    external_id: Mapped[str] = mapped_column(String(50), nullable=False)
    """Compatibility display identifier; uniqueness applies to unexpanded canonical rows only."""
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)
    match_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, server_default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="approved")

    propagated_from_set_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Historical duplicate-set identity; NULL on canonical rows."""

    propagation_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """Historical confidence of the duplicate link used to inherit this row."""

    file: Mapped[FileRecord | None] = relationship("FileRecord", foreign_keys=[file_id], lazy="noload")
    versions: Mapped[list[TracklistVersion]] = relationship("TracklistVersion", back_populates="tracklist", lazy="noload")

    @classmethod
    def is_canonical(cls) -> ColumnElement[bool]:
        """The SQL form of :data:`CANONICAL_TRACKLIST_CLAUSE` -- the canonical stored row.

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

        Note the asymmetry with ``services/tracklist_review.py``'s
        ``tracklist.propagated_from_set_key is not None``: that is a Python ``is not`` check on an
        already-loaded INSTANCE attribute, not a SQL predicate built for a query, so it is a
        different thing wearing similar words and is deliberately NOT routed through this
        classmethod (and the guard test acquits it explicitly).
        """
        return cls.propagated_from_set_key.is_not(None)

    __table_args__ = (
        Index("ix_tracklists_file_id", "file_id"),
        # PARTIAL unique: one canonical row per source identifier, with propagated projections exempt.
        Index(
            "ix_tracklists_external_id",
            "external_id",
            unique=True,
            postgresql_where=text(CANONICAL_TRACKLIST_CLAUSE + " AND provider_object_id IS NULL"),
        ),
        Index("ix_tracklists_provider_object", "provider_object_id", unique=True, postgresql_where=text(CANONICAL_TRACKLIST_CLAUSE)),
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
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_observations.id"))
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
    timestamp_evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    timestamp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_mashup: Mapped[bool] = mapped_column(Boolean, default=False)
    remix_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    version: Mapped[TracklistVersion] = relationship("TracklistVersion", back_populates="tracks", lazy="noload")

    __table_args__ = (Index("ix_tracklist_tracks_version_id", "version_id"),)
