"""DiscogsLink model for storing candidate Discogs release matches per tracklist track."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.tracklist import TracklistTrack


class DiscogsLink(TimestampMixin, Base):
    """A candidate or accepted Discogs match for exactly one legacy or immutable provider track.

    Stores denormalized Discogs metadata so search never calls discogsography live (D-09).
    Top 3 candidates per track enforced at query time, not schema level (D-06).
    One accepted link per track enforced at application level (D-07).
    """

    __tablename__ = "discogs_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    track_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklist_tracks.id"), nullable=True)
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_observations.id"), nullable=True)
    source_track_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discogs_release_id: Mapped[str] = mapped_column(String(50), nullable=False)
    discogs_artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="candidate")

    track: Mapped[TracklistTrack | None] = relationship("TracklistTrack", lazy="noload")

    __table_args__ = (
        CheckConstraint(
            "(track_id IS NOT NULL AND source_observation_id IS NULL AND source_track_position IS NULL) OR "
            "(track_id IS NULL AND source_observation_id IS NOT NULL AND source_track_position IS NOT NULL AND source_track_position > 0)",
            name="discogs_track_binding",
        ),
        Index("ix_discogs_links_source_track", "source_observation_id", "source_track_position"),
        Index("ix_discogs_links_track_id", "track_id"),
        Index("ix_discogs_links_status", "status"),
        Index("ix_discogs_links_discogs_release_id", "discogs_release_id"),
        # Defense-in-depth for D-07 ("one accepted link per track"): the primary fix is
        # application-level (bulk_link_discogs/accept_discogs_link dismiss siblings before
        # accepting), but this partial unique index makes a concurrent double-accept fail
        # loudly with an IntegrityError instead of silently landing two accepted rows.
        Index(
            "ix_discogs_links_one_accepted_per_track",
            "track_id",
            unique=True,
            postgresql_where=text("status = 'accepted'"),
        ),
        Index(
            "ix_discogs_links_one_accepted_per_source_track",
            "source_observation_id",
            "source_track_position",
            unique=True,
            postgresql_where=text("status = 'accepted' AND source_observation_id IS NOT NULL"),
        ),
    )
