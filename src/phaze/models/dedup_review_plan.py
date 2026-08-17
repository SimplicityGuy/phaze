"""Durable reviewed duplicate-resolution plans."""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class DedupReviewPlan(TimestampMixin, Base):
    """An opaque, immutable keeper choice and complete reviewed membership snapshot."""

    __tablename__ = "dedup_review_plan"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_hash: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    member_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
