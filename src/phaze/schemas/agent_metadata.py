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


class MetadataFailureResponse(BaseModel):
    """Success body of POST /metadata/{file_id}/failed (Phase 45 L-02 / CR-02).

    The terminal-ack endpoint the metadata task calls on a retries-exhausted
    failure so every ``extract_file_metadata`` run clears its
    ``extract_file_metadata:<file_id>`` scheduling-ledger row exactly once (the
    success path clears via ``put_metadata``). ``cleared`` is always ``True`` --
    the clear is a no-op when the row is already absent, but the ack semantics
    are "the row is gone now" regardless.
    """

    agent_id: str
    file_id: uuid.UUID
    cleared: bool
