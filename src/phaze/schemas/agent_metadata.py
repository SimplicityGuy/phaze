"""Pydantic schemas for PUT /api/internal/agent/metadata/{file_id} (phase-25)."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class MetadataWriteRequest(BaseModel):
    """Tag-metadata write body. Mirrors FileMetadata column shape 1:1 (no agent_id, no file_id -- both come from path/auth).

    Fields are all optional because the agent may have partial knowledge
    depending on file format / read success. Last-write-wins per D-14.
    """

    model_config = ConfigDict(extra="forbid")

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    duration: float | None = Field(default=None, ge=0.0)
    bitrate: int | None = Field(default=None, ge=0)
    raw_tags: dict | None = None


class MetadataWriteResponse(BaseModel):
    """Minimal echo response confirming the metadata write."""

    agent_id: str
    file_id: uuid.UUID
