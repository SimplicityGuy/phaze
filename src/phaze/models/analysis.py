"""AnalysisResult model - audio analysis results."""

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class AnalysisResult(TimestampMixin, Base):
    """Audio analysis results for a file (1:1 with files)."""

    __tablename__ = "analysis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    musical_key: Mapped[str | None] = mapped_column(String(10), nullable=True)
    mood: Mapped[str | None] = mapped_column(String(50), nullable=True)
    style: Mapped[str | None] = mapped_column(String(50), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AnalysisWindow(TimestampMixin, Base):
    """Per-window time-series analysis row for a file (1:many with files).

    Unlike the 1:1 :class:`AnalysisResult` aggregate, a single file owns many
    window rows, so ``file_id`` is indexed but NOT unique. It carries
    ``ON DELETE CASCADE`` (the only CASCADE FK in this module) so deleting a file
    removes its windows without leaving orphans. Migration 018 creates this table
    additively and leaves ``AnalysisResult``/``analysis`` structurally unchanged.

    Fine-tier windows populate ``bpm``/``musical_key``; coarse-tier windows
    populate ``mood``/``style``/``danceability``/``features``. All analysis
    columns are nullable so either tier can omit the other tier's fields.
    """

    __tablename__ = "analysis_window"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    tier: Mapped[str] = mapped_column(String, nullable=False)
    window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sec: Mapped[float] = mapped_column(Float, nullable=False)
    end_sec: Mapped[float] = mapped_column(Float, nullable=False)
    # Fine-tier fields
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    musical_key: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Coarse-tier fields
    mood: Mapped[str | None] = mapped_column(String(50), nullable=True)
    style: Mapped[str | None] = mapped_column(String(50), nullable=True)
    danceability: Mapped[float | None] = mapped_column(Float, nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
