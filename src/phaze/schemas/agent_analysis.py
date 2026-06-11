"""Pydantic schemas for PUT /api/internal/agent/analysis/{file_id} (Phase 26 D-26).

Per D-26: idempotent upsert on AnalysisResult.file_id (unique constraint).
All fields optional so partial-PUT semantics (field-level last-write-wins,
mirroring Phase 25 CR-01 fix in agent_metadata.py) preserve unset columns.

NOTE on column types: the AnalysisResult model (src/phaze/models/analysis.py)
currently stores `mood: String(50)` and `style: String(50)`. D-26 specifies
the *wire* type as `dict[str, float]` -- the router will serialize to a
JSON string for storage (or the executor may opt to migrate the column to
JSONB during Plan 06; that's a discretion area documented in Plan 06).
The wire type here matches CONTEXT.md D-26 exactly; storage representation
is the router's concern.
"""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class AnalysisWindowPayload(BaseModel):
    """One per-window time-series row (Phase 31, ANL-01).

    Two tiers share this shape: fine-tier windows populate ``bpm``/``musical_key``;
    coarse-tier windows populate ``mood``/``style``/``danceability``/``features``.
    All analysis columns are optional so either tier omits the other tier's fields.
    ``tier`` is a ``Literal`` (V5 input-validation control) and the numeric ``ge``
    guards bound malformed payloads at the wire boundary.
    """

    model_config = ConfigDict(extra="forbid")  # strict body parsing

    tier: Literal["fine", "coarse"]
    window_index: int = Field(ge=0)
    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)
    # Fine-tier fields
    bpm: float | None = Field(default=None, ge=0.0)
    musical_key: str | None = None
    # Coarse-tier fields
    mood: str | None = None
    style: str | None = None
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    features: dict | None = None


class AnalysisWritePayload(BaseModel):
    """Audio analysis upsert body. All optional -- partial-PUT preserves unset fields."""

    model_config = ConfigDict(extra="forbid")  # D-26 -- strict body parsing

    bpm: float | None = Field(default=None, ge=0.0)
    musical_key: str | None = None
    mood: dict[str, float] | None = None
    style: dict[str, float] | None = None
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
    # Per-window time-series (Phase 31). `| None` default preserves the partial-PUT
    # contract (router only replaces windows when this is not None); `max_length`
    # bounds the DoS-via-huge-bulk-insert threat (a 24h file at 30s windows is
    # ~2,880 fine windows, so 50000 is generous).
    windows: list[AnalysisWindowPayload] | None = Field(default=None, max_length=50000)


class AnalysisWriteResponse(BaseModel):
    """Minimal echo response confirming the upsert (D-26 success body)."""

    agent_id: str
    file_id: uuid.UUID
