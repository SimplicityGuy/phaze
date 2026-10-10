"""Bounded backfill controls and narrow acquisition summaries."""

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class LatestSourceAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    source_object_id: uuid.UUID
    attempt_id: uuid.UUID
    observation_id: uuid.UUID
    ordinal: int
    received_at: datetime
    attempted_at: datetime
    status: str
    code: str
    revision: str | None
    revision_scope: str
    truncated: bool
    freshness: str
    origin: str
