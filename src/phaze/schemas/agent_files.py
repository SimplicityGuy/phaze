"""Pydantic v2 schemas for /api/internal/agent/files (phase-25 file-upsert endpoint).

Every request schema sets `model_config = ConfigDict(extra="forbid")` per
phase-25 D-16. Nested item schemas (`FileUpsertRecord`) also set it because
`ConfigDict` is per-class, NOT inherited (RESEARCH Pitfall 5).

Schemas explicitly omit `agent_id` -- AUTH-01 mandates that agent_id comes
from the bearer-token resolver, NEVER from the request body.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from phaze.config import get_settings
from phaze.schemas.wire_bounds import INT64_MAX


_CHUNK_MAX: int = get_settings().agent_file_chunk_max
"""Server-side cap on chunk size. Configurable via ``AGENT_FILE_CHUNK_MAX`` env var.

Resolved at module-import time (``get_settings()`` is itself process-wide ``lru_cache``d,
so this is still exactly one construction); env override at runtime requires a process
restart.
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
    batch_id: uuid.UUID | None = None  # D-09: present -> bind to batch; absent -> LIVE sentinel resolution


class FileUpsertResponse(BaseModel):
    """Minimal echo response confirming the upsert + auto-enqueue counts."""

    agent_id: str
    upserted: int
    inserted: int
    enqueued: int


MOVE_LINEAGE_MAX: int = 4
"""Cap on ``FileMoveRequest.previous_paths``. A paired move event has ONE source; the lineage grows
past one only when a file is renamed again before it settles, or while its previous name's POST is
in flight (``Debouncer.move``) -- two entries cover that race, four leave room for a rename burst.
A file renamed more often than this before settling keeps only its most recent names (the agent
truncates), and the controller never acts on more than this many rows per request."""


class FileMoveRequest(BaseModel):
    """Body of POST /api/internal/agent/files/move: a settled file that arrived by an in-tree move.

    phaze-oxn2m. ``previous_paths`` is the file's lineage, oldest first: every path it was moved
    away from since it last settled. Usually one entry; more when it was renamed again before
    settling, or while the post of its previous name was still in flight -- the controller cannot
    know which of those names a row was last posted under, so the agent sends them all.

    A separate endpoint rather than a new optional field on :class:`FileUpsertRecord`, whose
    ``extra="forbid"`` would 422 a newer agent's whole upsert against an older control plane. An
    older control plane answers this route 404 instead, and the watcher falls back to a plain
    upsert of ``file`` -- exactly the pre-phaze-oxn2m behavior.
    """

    model_config = ConfigDict(extra="forbid")

    previous_paths: list[str] = Field(min_length=1, max_length=MOVE_LINEAGE_MAX)
    file: FileUpsertRecord


class FileMoveResponse(BaseModel):
    """What the controller did with a :class:`FileMoveRequest`.

    ``outcome`` is a plain string, not a Literal, so a newer control plane can add one without an
    older agent failing to parse the reply: ``moved`` (an existing row now points at the new path),
    ``upserted`` (no row had any previous path; ``file`` was upserted exactly as a plain post would),
    or ``kept_reviewed`` (a previous row carries operator-reviewed state, so it was left alone and
    ``file`` was upserted as its own row). ``retired`` counts stale previous-path rows deleted.
    """

    agent_id: str
    file_id: uuid.UUID
    outcome: str
    content_changed: bool
    retired: int
