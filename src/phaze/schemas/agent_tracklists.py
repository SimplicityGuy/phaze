"""Pydantic schemas for POST /api/internal/agent/tracklists (Phase 26 D-27).

Per D-27: atomic Tracklist + new TracklistVersion + N TracklistTrack rows
in one transaction, idempotency-keyed via agent-supplied `request_id` (UUID)
in a Redis SET NX EX cache (1-hour TTL).

Nested item schema TracklistTrackPayload also sets extra='forbid' because
ConfigDict is per-class (RESEARCH Pitfall 5 / Phase 25 schemas/agent_files.py
established this convention).

Threat T-26-07-DoS: `tracks` is capped at max_length=2000 to prevent a single
request from holding a Redis lock + DB transaction while inserting an unbounded
number of rows. 2000 is well above any realistic live-set tracklist (~200-300
tracks is the largest Spotify-fingerprintable concert).
"""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class TracklistTrackPayload(BaseModel):
    """One row in the tracks[] array of a TracklistCreatePayload."""

    model_config = ConfigDict(extra="forbid")  # nested-class strictness per RESEARCH Pitfall 5

    position: int = Field(ge=0)
    artist: str | None = None
    title: str | None = None
    timestamp: str | None = None
    confidence: float | None = None


class TracklistCreatePayload(BaseModel):
    """POST /tracklists body. request_id is agent-generated UUID for idempotency."""

    model_config = ConfigDict(extra="forbid")  # D-27 -- strict body parsing

    file_id: uuid.UUID
    source: Literal["fingerprint"]  # D-27 -- only fingerprint-sourced tracklists for now
    external_id: str
    tracks: list[TracklistTrackPayload] = Field(min_length=1, max_length=2000)  # T-26-07-DoS control
    request_id: uuid.UUID  # idempotency key (Stripe-style)


class TracklistCreateResponse(BaseModel):
    """Success body of POST /tracklists (D-27)."""

    tracklist_id: uuid.UUID
    version: int
    track_count: int
