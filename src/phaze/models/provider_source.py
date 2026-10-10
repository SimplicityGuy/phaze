"""Provider identity, immutable observations, target candidates and explicit selection."""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base


class ProviderSourceObject(Base):
    __tablename__ = "provider_source_objects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    native_id: Mapped[str] = mapped_column(Text, nullable=False)
    native_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL"))
    original_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    channel: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_provider_objects_bucket", "provider_id", "native_digest"),
        Index("ix_provider_objects_source_file_id", "source_file_id"),
        CheckConstraint("octet_length(native_id) BETWEEN 1 AND 16384", name="provider_native_bound"),
        CheckConstraint("channel IS NULL OR channel IN ('companion', 'embedded')", name="provider_channel"),
    )


class ProviderSourceObservation(Base):
    __tablename__ = "provider_source_observations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_objects.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[str | None] = mapped_column(Text)
    revision_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(128), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decoded_text: Mapped[str | None] = mapped_column(Text)
    encoding: Mapped[str | None] = mapped_column(Text)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_observations.id"))
    conflict_with_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_observations.id"))

    __table_args__ = (
        Index("ix_provider_observations_object", "object_id"),
        Index("ix_provider_observations_content", "object_id", "content_digest"),
        CheckConstraint("decoded_text IS NULL OR octet_length(decoded_text) <= 1048576", name="provider_text_bound"),
        CheckConstraint("revision IS NULL OR octet_length(revision) <= 8192", name="provider_revision_bound"),
        CheckConstraint("revision_scope IN ('full', 'bounded', 'unknown')", name="provider_revision_scope"),
    )


class ProviderRecordingCandidate(Base):
    __tablename__ = "provider_recording_candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_observations.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("media_id", "observation_id", name="uq_provider_candidate_target"),
        Index("ix_provider_candidates_media", "media_id"),
    )


class ProviderRecordingSelection(Base):
    __tablename__ = "provider_recording_selections"

    media_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(128), primary_key=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_observations.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    selection_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    target_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tracklist_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklists.id", ondelete="SET NULL"))
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tracklist_versions.id", ondelete="SET NULL"))


class ProviderSelectionEvent(Base):
    """Append-only reviewed choices; target UUID remains evidence after inventory deletion."""

    __tablename__ = "provider_selection_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("provider_source_observations.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    decision_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    action: Mapped[str | None] = mapped_column(String(16))
    target_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (Index("ix_provider_selection_events_media", "media_id"),)
