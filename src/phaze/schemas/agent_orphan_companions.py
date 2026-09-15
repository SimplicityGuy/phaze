"""Strict wire schemas for scan-owned orphan COMPANION diagnostics."""

from __future__ import annotations

from typing import Literal
import unicodedata
import uuid  # noqa: TC003  # Pydantic resolves response annotations at runtime.

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from phaze.config import settings


CompanionExtension = Literal[".cue", ".m3u", ".m3u8", ".nfo", ".pls", ".txt"]


class OrphanCompanionRecord(BaseModel):
    """One absolute metadata-only path observed by the filesystem-owning agent."""

    model_config = ConfigDict(extra="forbid")

    normalized_path: str = Field(min_length=1)
    companion_extension: CompanionExtension

    @field_validator("normalized_path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("normalized_path must not contain NUL")
        return unicodedata.normalize("NFC", value)

    @model_validator(mode="after")
    def _extension_matches_path(self) -> OrphanCompanionRecord:
        if not self.normalized_path.lower().endswith(self.companion_extension):
            raise ValueError("companion_extension must match normalized_path")
        return self


class OrphanCompanionChunk(BaseModel):
    """Bounded diagnostic chunk for one scan batch."""

    model_config = ConfigDict(extra="forbid")

    diagnostics: list[OrphanCompanionRecord] = Field(min_length=1, max_length=settings.agent_file_chunk_max)


class OrphanCompanionChunkResponse(BaseModel):
    """Idempotent persistence tally for the distinct normalized paths in a chunk."""

    batch_id: uuid.UUID
    inserted: int
    existing: int
