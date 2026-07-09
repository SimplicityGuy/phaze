"""Pydantic schemas for PUT /api/internal/agent/metadata/{file_id} (phase-25)."""

from typing import Literal
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


class MetadataFailurePayload(BaseModel):
    """Optional triage body for POST /metadata/{file_id}/failed (Phase 81, FAIL-02 / D-10).

    Mirrors ``AnalysisFailurePayload`` (schemas/agent_analysis.py) verbatim: a new agent
    image POSTs this so the persisted ``metadata`` failure row carries a triage
    ``error_message``; an OLD (bodyless) agent still gets a 200 because
    ``report_metadata_failed`` binds ``body: MetadataFailurePayload | None = None`` (CR-02
    version-skew guard, D-10). ``reason`` is a ``Literal`` so the wire can only carry the
    three classifications; ``error`` is a bounded free-text detail (``max_length`` caps the
    DoS-via-huge-string threat, T-81-03-04). ``extra='forbid'`` rejects any attempt to
    smuggle an ``agent_id``/``file_id`` in the body (AUTH-01, T-81-03-02 -> 422).
    """

    model_config = ConfigDict(extra="forbid")

    reason: Literal["timeout", "crashed", "error"]
    error: str | None = Field(default=None, max_length=2000)


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
    cleared: Literal[True]
