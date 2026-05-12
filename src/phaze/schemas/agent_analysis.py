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

import uuid

from pydantic import BaseModel, ConfigDict, Field


class AnalysisWritePayload(BaseModel):
    """Audio analysis upsert body. All optional -- partial-PUT preserves unset fields."""

    model_config = ConfigDict(extra="forbid")  # D-26 -- strict body parsing

    bpm: float | None = Field(default=None, ge=0.0)
    musical_key: str | None = None
    mood: dict[str, float] | None = None
    style: dict[str, float] | None = None
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)


class AnalysisWriteResponse(BaseModel):
    """Minimal echo response confirming the upsert (D-26 success body)."""

    agent_id: str
    file_id: uuid.UUID
