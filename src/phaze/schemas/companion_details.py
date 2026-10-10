"""Finite file-scoped stored companion projections; no acquisition effects."""

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from phaze.tracklist_providers.domain import ProviderTrack, ReleaseFact


class DetailModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True, revalidate_instances="always")


class DetailPage(DetailModel):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=20, ge=1, le=50)


class StoredTextRequest(DetailModel):
    offset: int = Field(default=0, ge=0, le=262144)
    length: int = Field(default=8192, ge=1, le=32768)


class CompanionAttemptSummary(DetailModel):
    source_object_id: uuid.UUID
    attempt_id: uuid.UUID
    observation_id: uuid.UUID
    received_at: datetime
    attempted_at: datetime
    ordinal: int
    status: str
    code: str
    revision: str | None
    revision_scope: str
    truncated: bool
    freshness: str
    origin: str


class CompanionLinkSummary(DetailModel):
    link_id: uuid.UUID
    file_id: uuid.UUID
    filename: str
    file_type: str
    location: str
    availability: str
    derivation_method: str
    derivation_revision: str | None
    derivation_evidence: tuple[str, ...]
    derived_at: datetime | None


class ObservationSummary(DetailModel):
    observation_id: uuid.UUID
    source_object_id: uuid.UUID
    status: str
    code: str
    revision: str | None
    revision_scope: str
    parser_version: str
    retrieved_at: datetime
    encoding: str | None
    truncated: bool
    text_preview: str | None
    text_length: int
    track_count: int
    release_fact_count: int
    completeness_state: str | None
    completeness_reason: str | None
    parent_id: uuid.UUID | None
    conflict_with_id: uuid.UUID | None
    availability: str
    candidate_status: str | None = None
    selected_kinds: tuple[str, ...] = ()


class SelectedAuthority(DetailModel):
    kind: Literal["tracklist", "release_metadata"]
    observation_id: uuid.UUID
    source_object_id: uuid.UUID
    selection_token: uuid.UUID
    actor: str
    selected_at: datetime
    availability: str
    target_mapping: dict[str, Any] | None


class CompanionSourceSummary(DetailModel):
    source_object_id: uuid.UUID
    provider_id: str
    native_id: str
    channel: str | None
    source_file_id: uuid.UUID | None
    original_file_id: uuid.UUID | None
    filename: str | None
    file_type: str | None
    location: str | None
    inventory_availability: str
    latest_stored_content: ObservationSummary | None
    latest_received_attempt: CompanionAttemptSummary | None = None
    attempt_history: Literal["unknown", "recorded"] = "unknown"


class CompanionDetails(DetailModel):
    file_id: uuid.UUID
    links: tuple[CompanionLinkSummary, ...]
    sources: tuple[CompanionSourceSummary, ...]
    selected: tuple[SelectedAuthority, ...]
    next_link_offset: int | None
    next_source_offset: int | None


class ObservationPage(DetailModel):
    file_id: uuid.UUID
    source_object_id: uuid.UUID
    observations: tuple[ObservationSummary, ...]
    next_offset: int | None


class StoredObservationDetail(DetailModel):
    file_id: uuid.UUID
    summary: ObservationSummary
    tracks: tuple[ProviderTrack, ...]
    release_facts: tuple[ReleaseFact, ...]
    evidence: tuple[str, ...]
    next_track_offset: int | None
    next_fact_offset: int | None
    # Target projection is populated only for pinned authority; intrinsic source rows stay distinct.
    selected_tracks: tuple[ProviderTrack, ...] = ()
    selected_track_count: int = 0
    next_selected_track_offset: int | None = None
    selected_availability: str | None = None
    target_mapping: dict[str, Any] | None = None


class StoredTextChunk(DetailModel):
    file_id: uuid.UUID
    observation_id: uuid.UUID
    text: str | None
    encoding: str | None
    source_truncated: bool
    offset: int
    total_characters: int
    next_offset: int | None


class ReverseMediaLink(DetailModel):
    link_id: uuid.UUID
    media_id: uuid.UUID
    filename: str
    file_type: str
    location: str


class ReverseMediaPage(DetailModel):
    companion_id: uuid.UUID
    media: tuple[ReverseMediaLink, ...]
    next_offset: int | None
