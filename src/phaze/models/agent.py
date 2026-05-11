"""Agent model - file-server identity for the v4.0 distributed-agents milestone."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime

from sqlalchemy import CheckConstraint, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class Agent(TimestampMixin, Base):
    """Agent (file server identity) that owns FileRecord and ScanBatch rows."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scan_roots: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="id_charset",
        ),
    )
