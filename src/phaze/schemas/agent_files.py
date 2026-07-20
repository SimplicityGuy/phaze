"""Pydantic v2 schemas for /api/internal/agent/files (phase-25 file-upsert endpoint).

Every request schema sets `model_config = ConfigDict(extra="forbid")` per
phase-25 D-16. Nested item schemas (`FileUpsertRecord`) also set it because
`ConfigDict` is per-class, NOT inherited (RESEARCH Pitfall 5).

Schemas explicitly omit `agent_id` -- AUTH-01 mandates that agent_id comes
from the bearer-token resolver, NEVER from the request body.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from phaze.config import settings
from phaze.schemas.wire_bounds import INT64_MAX


_CHUNK_MAX: int = settings.agent_file_chunk_max
"""Server-side cap on chunk size. Configurable via ``AGENT_FILE_CHUNK_MAX`` env var.

Resolved at module-import time; env override at runtime requires a process restart.
"""


class FileUpsertRecord(BaseModel):
    """Single file's metadata in a chunked upsert request."""

    model_config = ConfigDict(extra="forbid")

    sha256_hash: str = Field(min_length=64, max_length=64)
    original_path: str = Field(min_length=1)
    original_filename: str
    current_path: str
    file_type: str = Field(min_length=1, max_length=10)
    # -> files.file_size BigInteger (int8), rule 3. No narrower real-world domain is established
    # for a single music/concert-video file's byte size (the archive intentionally holds full
    # concert video sets, so an arbitrary GB-scale cap would be a guess, not a domain fact) --
    # the int8 column bound is the honest fallback.
    file_size: int = Field(ge=0, le=INT64_MAX)


class FileUpsertChunk(BaseModel):
    """Body of POST /api/internal/agent/files: bounded list of FileUpsertRecord."""

    model_config = ConfigDict(extra="forbid")

    files: list[FileUpsertRecord] = Field(min_length=1, max_length=_CHUNK_MAX)
    batch_id: uuid.UUID | None = None  # Phase 27 D-09: present -> bind to batch; absent -> LIVE sentinel resolution


class FileUpsertResponse(BaseModel):
    """Minimal echo response confirming the upsert + auto-enqueue counts."""

    agent_id: str
    upserted: int
    inserted: int
    enqueued: int
