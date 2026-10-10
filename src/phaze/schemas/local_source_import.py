"""Bounded local import commands and explicit selected-source authority."""

from datetime import datetime
from typing import Annotated, Literal, Self
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from phaze.tracklist_providers.domain import LoadBudget, ProviderTrack, Snapshot, SourceIdentity


SourceKind = Literal["tracklist", "release_metadata"]
EmbeddedChannel = Annotated[str, Field(min_length=1, max_length=32, pattern=r"(?i)^(comment|comments|description|lyrics|tracklist)$")]


class ImportLocalSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    media_id: uuid.UUID
    raw_observation_id: uuid.UUID | None = None
    embedded_channel: EmbeddedChannel | None = None
    budget: LoadBudget = LoadBudget()

    @model_validator(mode="after")
    def require_one_source(self) -> Self:
        if (self.raw_observation_id is None) == (self.embedded_channel is None):
            raise ValueError("Choose exactly one raw observation or embedded channel")
        return self


class SourceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    media_id: uuid.UUID
    observation_id: uuid.UUID
    kind: SourceKind
    decision_id: uuid.UUID
    expected_selection_token: uuid.UUID | None = None
    expected_revision: Annotated[str, Field(max_length=2048)] | None
    expected_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_media_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    action: Literal["select", "reject"] = "select"
    accept_conflict: bool = False
    cue_file_ordinal: int | None = Field(default=None, gt=0, le=4096)


class ImportResult(BaseModel):
    raw_observation_id: uuid.UUID
    tracklist_observation_id: uuid.UUID | None = None
    release_observation_id: uuid.UUID | None = None
    candidate_ids: tuple[uuid.UUID, ...] = ()
    tracklist_status: str
    release_status: str


class SelectedRecordingSource(BaseModel):
    """Pinned reviewed authority; snapshot is immutable and tracks are the target projection."""

    media_id: uuid.UUID
    kind: SourceKind
    observation_id: uuid.UUID
    source_object_id: uuid.UUID
    identity: SourceIdentity
    channel: str | None
    selection_token: uuid.UUID
    actor: str
    selected_at: datetime
    availability: str
    target_mapping: dict[str, str | int] | None = None
    snapshot: Snapshot
    tracks: tuple[ProviderTrack, ...]
