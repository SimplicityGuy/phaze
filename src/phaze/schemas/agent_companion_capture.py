"""Revision-bound companion capture contracts; finite limits bound DoS work and wire size."""

from typing import Annotated, Self
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from phaze.schemas.wire_payload import WirePayload
from phaze.tracklist_providers.domain import DiscoveryBudget, SourceRead


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CaptureBudget(DiscoveryBudget):
    max_characters: int = Field(default=131072, gt=0, le=262144)
    max_wire_bytes: int = Field(default=1048576, ge=4096, le=2097152)


class CaptureTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: uuid.UUID
    media_id: uuid.UUID | None = None
    path: str = Field(min_length=1, max_length=4096)  # DoS bound, inventory path is Text.
    expected_sha256: Digest
    expected_size: int = Field(ge=0, le=9223372036854775807)


class CaptureCompanionPayload(WirePayload):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    target: CaptureTarget
    budget: CaptureBudget = CaptureBudget()


class CaptureReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: uuid.UUID | None = None
    target: CaptureTarget
    budget: CaptureBudget = CaptureBudget()
    read: SourceRead

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        read = self.read
        for value in read.model_dump(mode="json").values():
            strings = (value,) if isinstance(value, str) else value if isinstance(value, list) else ()
            if any(isinstance(item, str) and "\0" in item for item in strings):
                raise ValueError("Capture strings cannot contain PostgreSQL NUL")
        if read.scope != str(self.target.file_id):
            raise ValueError("Read scope must be its authorized source UUID")
        if read.bytes_read > self.budget.max_bytes:
            raise ValueError("Read exceeds byte budget")
        if read.text is not None and (len(read.text) > self.budget.max_characters or len(read.text.splitlines()) > self.budget.max_lines):
            raise ValueError("Read exceeds decoded text budget")
        if len(self.model_dump_json().encode()) > self.budget.max_wire_bytes:
            raise ValueError("Capture exceeds wire budget")
        if read.status == "found" and (read.truncated or read.revision_scope != "full" or read.revision != self.target.expected_sha256):
            raise ValueError("Found capture must match complete expected bytes")
        if read.status == "found" and (read.text is None or not read.encoding or read.bytes_read != self.target.expected_size):
            raise ValueError("Found capture requires decoded text, encoding and complete expected byte count")
        if read.revision_scope == "full" and read.revision is not None and "revision:sha256:full" not in read.evidence:
            raise ValueError("Complete companion digest requires explicit evidence")
        if read.revision_scope == "full" and (
            read.bytes_read != self.target.expected_size
            or read.revision is None
            or len(read.revision) != 64
            or any(c not in "0123456789abcdef" for c in read.revision)
        ):
            raise ValueError("Full companion revision requires all expected bytes and a SHA-256 digest")
        if read.revision_scope == "bounded" and not read.truncated:
            raise ValueError("Bounded companion revision must describe truncated bytes")
        if read.revision_scope != "full" and "revision:sha256:full" in read.evidence:
            raise ValueError("Bounded digest cannot claim full-byte evidence")
        return self


class CaptureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    reused: bool
    freshness: str
