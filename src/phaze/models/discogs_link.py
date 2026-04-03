"""DiscogsLink model for storing candidate Discogs release matches per tracklist track."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.tracklist import TracklistTrack


class DiscogsLink(TimestampMixin, Base):
    """A candidate or accepted link between a tracklist track and a Discogs release.

    Stores denormalized Discogs metadata so search never calls discogsography live (D-09).
    Top 3 candidates per track enforced at query time, not schema level (D-06).
    One accepted link per track enforced at application level (D-07).
    """

    __tablename__ = "discogs_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    track_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklist_tracks.id"), nullable=False)
    discogs_release_id: Mapped[str] = mapped_column(String(50), nullable=False)
    discogs_artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    discogs_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="candidate")

    track: Mapped[TracklistTrack] = relationship("TracklistTrack", lazy="noload")

    __table_args__ = (
        Index("ix_discogs_links_track_id", "track_id"),
        Index("ix_discogs_links_status", "status"),
        Index("ix_discogs_links_discogs_release_id", "discogs_release_id"),
    )
