"""AnalysisResult model - audio analysis results."""

from datetime import datetime
import uuid

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
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
    # Phase 43 windowed-analysis coverage (migration 021). All nullable: pre-43
    # rows and empty-body PUTs leave coverage NULL. These are dedicated columns so
    # the five-field coverage contract from analyze_file never funnels into `features`.
    fine_windows_analyzed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fine_windows_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coarse_windows_analyzed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coarse_windows_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sampled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Phase 57.1 completion discriminator (migration 028). NULL while a partial in-flight
    # row exists (D-03 upserts one at analysis START); stamped via func.now() ONLY in the
    # put_analysis completion branch that flips FileState.ANALYZED. The proposal convergence
    # gate requires this IS NOT NULL so a partial row can never leak in with NULL aggregates.
    analysis_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase 77 (D-01, migration 032): nullable analyze-stage failure markers on the 1:1 output table
    # (NOT a generic stage_failure table -- preserves the <=1-row-per-file invariant). Stamped by the
    # go-forward writer + backfilled from FileState.ANALYSIS_FAILED; analysis_completed_at stays NULL
    # for a failed row so the future done-over-failed precedence holds.
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 77 (PERF-01, migration 032): partial indexes mirroring what migration 032 creates, with
    # byte-identical names + normalized IS-NOT-NULL predicate text -- the ORM half of the
    # empty-autogenerate-diff contract. AnalysisResult had no __table_args__ before this phase.
    __table_args__ = (
        Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL")),
        Index("ix_analysis_failed", "file_id", postgresql_where=text("failed_at IS NOT NULL")),
    )


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
