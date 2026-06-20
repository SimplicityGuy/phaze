"""Pydantic schemas for PUT /api/internal/agent/fingerprints/{file_id}/{engine} (phase-25)."""

from typing import Literal
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


class FingerprintFailureResponse(BaseModel):
    """Success body of POST /fingerprints/{file_id}/failed (Phase 45 L-02 / CR-02).

    The terminal-ack endpoint the fingerprint task calls on a retries-exhausted
    failure so every ``fingerprint_file`` run clears its single-per-file
    ``fingerprint_file:<file_id>`` scheduling-ledger row exactly once (the success
    path clears via ``put_fingerprint``). The ledger key is per-file, NOT per
    engine, so there is no ``engine`` field here. ``cleared`` is always ``True``
    -- the clear is a no-op when the row is already absent.
    """

    agent_id: str
    file_id: uuid.UUID
    cleared: Literal[True]
