"""AnalysisResult model - audio analysis results."""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
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
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Windowed-analysis progress counts (migration 021; `sampled` dropped by migration 060,
    # phaze-w55w1). All nullable: pre-43 rows and empty-body PUTs leave them NULL. These are
    # dedicated columns so the counts from analyze_file never funnel into `features`. Since
    # analysis is exhaustive they express PROGRESS, not a coverage gap: `analyzed` equals `total`
    # on a healthy file and falls short only where individual windows failed to decode.
    fine_windows_analyzed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fine_windows_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coarse_windows_analyzed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coarse_windows_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULL while a partial in-flight
    # row exists (D-03 upserts one at analysis START); stamped via func.now() ONLY in the
    # put_analysis completion branch that flips FileState.ANALYZED. The proposal convergence
    # gate requires this IS NOT NULL so a partial row can never leak in with NULL aggregates.
    analysis_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Nullable analyze-stage failure markers stay on the 1:1 output table
    # (NOT a generic stage_failure table -- preserves the <=1-row-per-file invariant). Stamped by the
    # go-forward writer + backfilled from FileState.ANALYSIS_FAILED; analysis_completed_at stays NULL
    # for a failed row so the future done-over-failed precedence holds.
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Partial indexes mirror the migration with
    # byte-identical names + normalized IS-NOT-NULL predicate text -- the ORM half of the
    # empty-autogenerate-diff contract. AnalysisResult had no __table_args__ before this phase.
    # The analyze stage is done XOR failed, never both. Mirrored
    # here with the BARE name -- the ck_%(table_name)s_%(constraint_name)s convention renders it as
    # ck_analysis_analysis_completed_xor_failed, matching what migration 033 creates (the ORM half of
    # the empty-autogenerate-diff contract).
    # A partial btree ON THE VALUE of ``analysis_completed_at`` (not just
    # ``ix_analysis_completed``'s existence-only file_id index above) so the lane cards' rolling-24h
    # PROCESSED count (``analysis_completed_at >= :cutoff``) and the lifetime count (both gated
    # ``IS NOT NULL`` first) get an efficient range scan instead of a sequential one as the table grows
    # (acceptance rule 7; see the bead's COST design note). Byte-identical name + predicate text to what
    # migration 058 creates -- the ORM half of the empty-autogenerate-diff contract, same convention as
    # the two indexes above.
    __table_args__ = (
        Index("ix_analysis_completed", "file_id", postgresql_where=text("analysis_completed_at IS NOT NULL")),
        Index("ix_analysis_failed", "file_id", postgresql_where=text("failed_at IS NOT NULL")),
        Index("ix_analysis_completed_at_when", "analysis_completed_at", postgresql_where=text("analysis_completed_at IS NOT NULL")),
        CheckConstraint("NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)", name="analysis_completed_xor_failed"),
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

    ``energy``, ``camelot`` and ``mood_scores`` are a narrow projection of what
    ``features`` already holds, not new measurement: reading a ~5 KB JSONB per row
    does not scale to the 4 million window rows the 200 000-file target implies, so
    every viewer surface queries these columns instead. The projection writer fills them at analysis
    completion and can backfill existing rows from stored JSONB without re-analysis.

    ``mood_scores`` keys are the 11 fixed names in the single fixed archive-wide
    order declared in :data:`phaze.services.set_projection.MOOD_ORDER` -- the same
    order ``SetProfile.mean_vector`` is positional in, and the same order the mood
    river, its legend and the tracklist mood dots share. See that constant for why
    the order is pinned as a literal rather than derived from ``MODEL_SETS``.
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
    # Set-projection columns are nullable; see the class docstring.
    # The energy scalar in [0, 1], one per COARSE window (phaze-x1qr3.2 defines the weighted sum
    # over danceability/party/aggressive/relaxed/sad and the file-local BPM z-score). The range is a
    # writer contract rather than a CHECK constraint because validating millions of rows would require
    # an avoidable table scan.
    energy: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Camelot wheel position of this window's key -- "8A", "12B", at most 3 characters. FINE tier
    # (that is the tier carrying `musical_key`), derived from the "A minor" form via the 24-entry
    # conversion table, which also accepts Essentia's sharp/flat spellings.
    camelot: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # The 11 positive-class means for this COARSE window, averaged over the 3 model variants, keyed
    # and ordered by `services.set_projection.MOOD_ORDER`. A flat {name: float} object, ~200 bytes
    # against `features`' ~5 KB -- which is the entire reason this column exists.
    mood_scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
