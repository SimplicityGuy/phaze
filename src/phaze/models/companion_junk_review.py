"""The junk-companion review queue, audit trail and tombstone (phaze-bk5jp).

One FK-free table, following the table-per-flow precedent (``TagWriteLog``, the CUE writes):
``docs/spikes/phaze-ib3ub-junk-companion-review.md`` E3 measured why a delete kind on ``proposals``
fails open across that table's readers, and E4/E5 why the record must outlive the ``files`` row.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- SQLAlchemy resolves Mapped[] annotations at runtime
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS
from phaze.enums.junk_review import TERMINAL_STATUSES, TRANSITIONS, JunkReviewReason, JunkReviewStatus, allowed_from
from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from collections.abc import Iterable


__all__ = ["TERMINAL_STATUSES", "TRANSITIONS", "CompanionJunkReview", "JunkReviewReason", "JunkReviewStatus", "allowed_from"]


def _sql_list(values: Iterable[str]) -> str:
    """A sorted SQL ``IN`` list of string literals (``str()`` so an enum member renders as its value)."""
    return ", ".join(repr(str(value)) for value in sorted(values))


_COMPANION_TYPES = [ext.lstrip(".") for ext in INGESTIBLE_COMPANION_EXTENSIONS]
_TERMINAL_SQL = _sql_list(TERMINAL_STATUSES)

LIVE_IDENTITY_WHERE = text(f"status NOT IN ({_TERMINAL_SQL})")
"""The partial-index predicate: at most one NON-terminal row per natural identity."""


class CompanionJunkReview(TimestampMixin, Base):
    """One proposal to quarantine one companion file, and what became of it.

    **Keyed on the natural identity** ``(agent_id, original_path, sha256_hash)``, never on
    ``files.id``: a scan or file deletion re-creates the row under a new id, and a decision keyed on
    the old id would be lost (a rejected file proposed again). There is **no foreign key** to any
    table, so neither ``delete_scan_cascade`` nor ``delete_file_cascade`` can be blocked by a row or
    erase one; ``file_id`` is a plain UUID for traceability while the row lives (the
    ``scheduling_ledger`` precedent).

    The identity is unique among NON-terminal rows only (``uq_companion_junk_review_live_identity``).
    A terminal row (quarantined, failed) is the audit record of an executed attempt and is never
    reopened; the same identity turning up again gets a new pending row beside it (decision 4).

    ``content_group`` is the content's SHA-256 as the detector read it from the stored features. The
    review decides a group at once (one decision per identical content), and a rejection covers every
    row with that ``sha256_hash`` (decision 5). Approval records a time only, ``decided_at``
    (decision 7): there is no actor column. ``decided_at`` is the time of the CURRENT decision, so an
    undo back to pending clears it.
    """

    __tablename__ = "companion_junk_review"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    content_group: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=JunkReviewStatus.PENDING.value)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "uq_companion_junk_review_live_identity",
            "agent_id",
            "original_path",
            "sha256_hash",
            unique=True,
            postgresql_where=LIVE_IDENTITY_WHERE,
        ),
        Index("ix_companion_junk_review_sha256_hash", "sha256_hash"),
        Index("ix_companion_junk_review_status_content_group", "status", "content_group"),
        # BARE constraint names: the ck naming convention adds the table prefix (phaze-x8tof).
        CheckConstraint(f"status IN ({_sql_list(JunkReviewStatus)})", name="known_status"),
        CheckConstraint(f"reason IN ({_sql_list(JunkReviewReason)})", name="known_reason"),
        # Media is refused by construction: only an ingestible companion extension can be proposed.
        CheckConstraint(f"file_type IN ({_sql_list(_COMPANION_TYPES)})", name="companion_file_type"),
        CheckConstraint("file_size >= 0", name="file_size_nonneg"),
        CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decided_at_iff_decided"),
        CheckConstraint(f"(status IN ({_TERMINAL_SQL})) = (executed_at IS NOT NULL)", name="executed_at_iff_terminal"),
    )
