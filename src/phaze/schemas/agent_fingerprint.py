"""Pydantic schemas for PUT /api/internal/agent/fingerprints/{file_id}/{engine} (phase-25)."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class FingerprintWriteRequest(BaseModel):
    """Fingerprint result body. Natural key (file_id, engine) is in the URL path -- NOT body."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=20)
    error_message: str | None = None


class FingerprintWriteResponse(BaseModel):
    """Minimal echo response confirming the fingerprint write."""

    agent_id: str
    file_id: uuid.UUID
    engine: str
