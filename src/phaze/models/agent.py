"""Agent model - file-server identity for the v4.0 distributed-agents milestone."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy resolves Mapped[] annotations at runtime

from sqlalchemy import CheckConstraint, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


LEGACY_AGENT_ID = "legacy-application-server"
"""Phase 24 placeholder agent_id stamped on every file and scan batch created without
explicit agent attribution. Phase 25 wires real per-agent attribution through the HTTP
API; until then every FileRecord/ScanBatch belongs to this single seeded agent."""


class Agent(TimestampMixin, Base):
    """Agent (file server identity) that owns FileRecord and ScanBatch rows."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'fileserver'"))
    scan_roots: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="id_charset",
        ),
        CheckConstraint(
            "kind IN ('fileserver', 'compute')",
            name="kind_enum",
        ),
    )
