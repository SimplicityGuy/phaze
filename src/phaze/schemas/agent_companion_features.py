"""Wire schemas for POST /api/internal/agent/companion-features and the backfill task (phaze-osy6j).

A separate endpoint, not new optional fields on ``FileUpsertRecord``: that schema is
``extra="forbid"``, so a newer agent's whole upsert would 422 against an older control plane (the
``FileMoveRequest`` precedent). An older control plane answers this route 404, which the agent
treats as "not reported yet" -- ``phaze backfill companion-features`` fills the gap after it upgrades.

Records are keyed on ``original_path`` (the agent does not know ``files.id`` at ingest time); the
control plane resolves it against the authenticated agent's own rows. Field names match the
``companion_content_features`` columns they land in (``tests/shared/schemas/
test_wire_bounds_contract.py`` checks each bound against the live column).
"""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

from phaze.config import get_settings
from phaze.schemas.wire_bounds import INT16_MAX, INT32_MAX, INT64_MAX
from phaze.services.companion_features import (
    EXTRACTOR_VERSION,
    MAX_FOLDER_MEDIA,
    MAX_REFERENCE_LENGTH,
    MAX_REFERENCES,
    CompanionReading,
    ContentJunkClass,
    Encoding,
    ReferenceSource,
)


_CHUNK_MAX: int = get_settings().agent_file_chunk_max


class CompanionReferenceWire(BaseModel):
    """One media reference: a normalized basename and its source (``media_references`` JSONB element)."""

    model_config = ConfigDict(extra="forbid")

    # DoS bound (wire_bounds rule 7), not a column width: the element lands in a JSONB array. It
    # equals the extractor's own cap, so a real reading always fits.
    name: str = Field(min_length=1, max_length=MAX_REFERENCE_LENGTH)
    source: ReferenceSource


class CompanionFeaturesRecord(BaseModel):
    """One companion's content features, as read by the agent that owns the file."""

    model_config = ConfigDict(extra="forbid")

    # Resolution key, never stored here: matched against files.original_path (Text) for the
    # authenticated agent. NFC-normalized like every other path on the wire.
    original_path: str = Field(min_length=1)
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    encoding: Encoding
    # -> byte_size BigInteger (int8), rule 3: no narrower domain for a file's size.
    byte_size: int = Field(ge=0, le=INT64_MAX)
    truncated: bool
    # DoS bound (rule 7): the extractor keeps MAX_REFERENCES and stops counting one past it.
    media_references: list[CompanionReferenceWire] = Field(max_length=MAX_REFERENCES)
    reference_count: int = Field(ge=0, le=INT32_MAX)
    is_tracklist: bool
    content_junk_class: ContentJunkClass | None
    # DoS bound (rule 7): the agent lists at most MAX_FOLDER_MEDIA names; the full count rides beside.
    folder_media: list[str] = Field(max_length=MAX_FOLDER_MEDIA)
    folder_media_count: int = Field(ge=0, le=INT32_MAX)
    # -> extractor_version SmallInteger (int2), rule 3.
    extractor_version: int = Field(ge=1, le=INT16_MAX)

    @field_validator("original_path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("original_path must not contain NUL")
        return unicodedata.normalize("NFC", value)

    @classmethod
    def from_reading(cls, original_path: str, reading: CompanionReading) -> CompanionFeaturesRecord:
        """Build the wire record for ``original_path`` (the NFC row key) from what the agent read."""
        features = reading.features
        return cls(
            original_path=original_path,
            fingerprint=reading.fingerprint,
            encoding=features.encoding,
            byte_size=reading.byte_size,
            truncated=reading.truncated,
            media_references=[CompanionReferenceWire(name=ref.name, source=ref.source) for ref in features.references],
            reference_count=features.reference_count,
            is_tracklist=features.is_tracklist,
            content_junk_class=features.junk_class,
            folder_media=list(reading.folder_media),
            folder_media_count=reading.folder_media_count,
            extractor_version=EXTRACTOR_VERSION,
        )


class CompanionFeaturesChunk(BaseModel):
    """Body of POST /api/internal/agent/companion-features: a bounded list of records."""

    model_config = ConfigDict(extra="forbid")

    features: list[CompanionFeaturesRecord] = Field(min_length=1, max_length=_CHUNK_MAX)


class CompanionFeaturesResponse(BaseModel):
    """How many records were stored, and how many named no companion row of the calling agent."""

    agent_id: str
    stored: int
    unknown: int
