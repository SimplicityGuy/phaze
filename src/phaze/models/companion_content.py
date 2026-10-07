"""Per-companion content features, read on the owning agent and stored control-side (phaze-osy6j)."""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


JUNK_CLASSES: tuple[str, ...] = ("empty", "all_nul", "known_stamp", "site_ad")
"""Every junk class a row can carry. ``known_stamp`` is control-side only (``content_junk_class`` never holds it)."""

ENCODINGS: tuple[str, ...] = ("ascii", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "cp437", "cp1252", "all-nul")


class CompanionContentFeatures(TimestampMixin, Base):
    """What one companion file contains: references, tracklist flag, junk class, encoding and fingerprint.

    One row per companion ``files`` row, keyed 1:1 on ``file_id``. The FK cascades so a scan or
    single-file deletion can never be blocked by this sidecar, and ``services/scan_deletion.py``
    also deletes it explicitly, for the rowcount report (the ``cloud_budget`` precedent).

    ``fingerprint`` is the SHA-256 of the bytes the agent read -- the complete file. It equals
    ``files.sha256_hash`` while the file is unchanged, so a row whose fingerprint differs describes
    older bytes and is stale. Identical contents share a fingerprint, which is how the junk review
    groups them and how :func:`phaze.services.companion_content.refresh_known_stamps` finds stamps.

    ``content_junk_class`` is what the bytes alone say (agent). ``junk_class`` is the effective class
    the linking chain and the junk review read: ``empty`` / ``all_nul`` first, then ``known_stamp``
    when this content is a stamp across the agent's folders, then the agent's ``site_ad``.
    """

    __tablename__ = "companion_content_features"

    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), primary_key=True)
    # Copied from files.agent_id so the stamp rule's per-agent fingerprint group is one index read.
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    encoding: Mapped[str] = mapped_column(String(16), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # True when the file exceeded the agent's read cap, so the features describe only its head.
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # [{"name": <NFC basename>, "source": "cue_file" | "m3u" | "pls" | "text_token"}, ...], capped;
    # reference_count is the uncapped total.
    media_references: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    reference_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_tracklist: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_junk_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    junk_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Sorted media names directly in the companion's folder when it was read (capped), and the full count.
    folder_media: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    folder_media_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    extractor_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_companion_content_features_agent_id_fingerprint", "agent_id", "fingerprint"),
        CheckConstraint(f"encoding IN ({', '.join(repr(e) for e in ENCODINGS)})", name="known_encoding"),
        CheckConstraint(
            f"content_junk_class IS NULL OR content_junk_class IN ({', '.join(repr(c) for c in JUNK_CLASSES if c != 'known_stamp')})",
            name="known_content_junk_class",
        ),
        CheckConstraint(f"junk_class IS NULL OR junk_class IN ({', '.join(repr(c) for c in JUNK_CLASSES)})", name="known_junk_class"),
        CheckConstraint("byte_size >= 0 AND reference_count >= 0 AND folder_media_count >= 0", name="counts_nonneg"),
    )
