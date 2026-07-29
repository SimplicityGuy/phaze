"""TagWriteLog model - append-only audit trail for tag write operations."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime
import enum
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.file import FileRecord


class TagWriteStatus(enum.StrEnum):
    """Status of a tag write operation."""

    COMPLETED = "completed"
    FAILED = "failed"
    DISCREPANCY = "discrepancy"
    # phaze-vq3g: the on-disk write SUCCEEDED but the immediate verify re-read failed (transient
    # I/O / mutagen re-parse error). Distinct from DISCREPANCY (write landed the WRONG values) and
    # from FAILED (the write itself never landed): here the file is correctly tagged but could not
    # be confirmed, so it must NOT be audited as an all-field ``actual=None`` discrepancy. Like
    # FAILED/DISCREPANCY it is intentionally NON-terminal, so the file resurfaces and a later submit
    # re-verifies and self-heals to COMPLETED once the transient condition clears. ``status`` is a
    # plain ``String(20)`` (no PG enum / CHECK), so adding this value needs no migration.
    VERIFY_FAILED = "verify_failed"
    # WR-01: a terminal marker for an applied file whose server-computed proposal has ZERO changes
    # (nothing to write). It is NOT an audio write -- it records that the file was inspected and
    # needs no tag write, so the idempotency anti-join (``completed``/terminal subquery) EVICTS it
    # from the candidate window and it can never re-occupy the alphabetically-first ``.limit()``
    # slots and starve qualifying files. The ``status`` column is a ``String(20)`` (no PG enum /
    # CHECK constraint), so adding this value needs no migration.
    NO_OP = "no_op"
    # phaze-ysnp: a write-ahead marker. execute_tag_write commits a row in this status (carrying
    # before_tags) BEFORE dispatching the irreversible disk write, so a crash/cancellation between
    # the write and the caller's final commit (e.g. asyncio.CancelledError at the
    # ``asyncio.to_thread`` boundary during a graceful shutdown -- CancelledError derives from
    # BaseException on 3.14 and unwinds straight past an ``except Exception``) leaves an honest
    # "disk state uncertain, snapshot preserved" audit row instead of the mutation vanishing from
    # the append-only trail entirely. Deliberately excluded from BOTH
    # ``routers.tags._TERMINAL_TAGWRITE_STATUSES`` (an orphaned row must not evict the file from the
    # candidate window -- it re-qualifies and self-heals on the next pass) and
    # ``routers.tags._UNDOABLE_TAGWRITE_STATUSES`` (it never confirmed landing on disk, so it must
    # never shadow the real write's snapshot in an undo). Always immediately overwritten with a
    # terminal status inside the SAME ``execute_tag_write`` call under normal operation -- a row
    # actually observed in this status is, by construction, an orphan from an aborted operation.
    IN_PROGRESS = "in_progress"


class TagWriteLog(TimestampMixin, Base):
    """Append-only audit log for tag write operations.

    Records every tag write attempt with before/after snapshots for full
    traceability. Follows the ExecutionLog append-only pattern.
    """

    __tablename__ = "tag_write_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=False)
    before_tags: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after_tags: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    discrepancies: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    written_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    file: Mapped[FileRecord] = relationship("FileRecord", foreign_keys=[file_id], lazy="noload")

    __table_args__ = (
        Index("ix_tag_write_log_file_id", "file_id"),
        Index("ix_tag_write_log_status", "status"),
    )
