"""Durable batch cursor, recording/provider work, memberships and bounded continuation evidence."""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class RemoteLookupBatch(TimestampMixin, Base):
    __tablename__ = "remote_lookup_batches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    providers: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    cursor: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    upper_bound: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expanded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    existing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RemoteLookupWork(TimestampMixin, Base):
    __tablename__ = "remote_lookup_work"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Retain work if a recording is deleted; execution records an explicit exclusion.
    media_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    code: Mapped[str | None] = mapped_column(String(4096))
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_digest: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (
        Index(
            "uq_remote_lookup_active_pair",
            "media_id",
            "provider_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','deferred','blocked')"),
        ),
    )


class RemoteLookupMembership(Base):
    __tablename__ = "remote_lookup_memberships"
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("remote_lookup_batches.id", ondelete="CASCADE"), primary_key=True)
    work_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("remote_lookup_work.id", ondelete="CASCADE"), primary_key=True)


class RemoteLookupRead(Base):
    __tablename__ = "remote_lookup_reads"
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("remote_lookup_work.id", ondelete="CASCADE"), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
