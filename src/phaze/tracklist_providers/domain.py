"""Immutable source-neutral data. No persistence, filesystem or transport dependencies."""

from datetime import date as CalendarDate
from enum import StrEnum
from fractions import Fraction
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


Text = Annotated[str, Field(max_length=4096)]
Identifier = Annotated[str, Field(min_length=1, max_length=4096)]
ProviderId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
Evidence = Annotated[tuple[Text, ...], Field(max_length=64)]


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class OutcomeStatus(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    INCOMPLETE = "incomplete"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    RETRY = "retry"
    CONTRACT_ERROR = "contract_error"


class Capability(StrEnum):
    DISCOVERY = "discovery"
    LOAD = "load"
    LOCAL_SIDECAR = "local_sidecar"
    EMBEDDED_METADATA = "embedded_metadata"
    REVISION_EVIDENCE = "revision_evidence"
    TRACK_PARSING = "track_parsing"
    TIMESTAMP_EVIDENCE = "timestamp_evidence"


class ProviderDescriptor(DomainModel):
    id: ProviderId
    display_name: Text
    contract_version: tuple[int, int] = (1, 0)
    capabilities: frozenset[Capability] = frozenset({Capability.LOAD})

    @model_validator(mode="after")
    def require_load(self) -> Self:
        if Capability.LOAD not in self.capabilities:
            raise ValueError("Every provider must support load")
        return self


class SourceIdentity(DomainModel):
    provider_id: ProviderId
    native_id: Identifier


class Fact(DomainModel):
    value: Text | None = None
    certainty: Literal["known", "inferred", "unknown", "conflicting"] = "unknown"
    evidence: Evidence = ()

    @model_validator(mode="after")
    def require_known_value(self) -> Self:
        if self.certainty == "known" and not self.value:
            raise ValueError("Known fact requires a value")
        return self


class ReleaseFact(Fact):
    field: Annotated[str, Field(min_length=1, max_length=128)]
    original_value: Text | None = None
    normalized_value: Text | None = None
    unit: Annotated[str, Field(max_length=64)] | None = None
    source_line: int | None = Field(default=None, gt=0)


class RationalOffset(DomainModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    def as_fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


class Timestamp(DomainModel):
    original: Text
    kind: Literal["offset", "clock", "unknown"] = "unknown"
    precision: Literal["cue_frame_75", "second", "minute", "unknown"] = "unknown"
    origin: Text | None = None
    offset: RationalOffset | None = None
    offset_usability: Literal["qualified", "approximate", "unusable"] = "unusable"
    evidence: Evidence = ()

    @model_validator(mode="after")
    def validate_qualification(self) -> Self:
        if self.kind != "offset" and self.offset is not None:
            raise ValueError("Clock and unknown timestamps cannot contain an offset")
        if self.offset_usability != "unusable" and (self.kind != "offset" or self.offset is None or not self.origin or not self.evidence):
            raise ValueError("Usable offsets require origin and interpretation evidence")
        if self.offset_usability == "qualified" and self.precision not in {"cue_frame_75", "second"}:
            raise ValueError("Qualified offsets require frame or second precision")
        if self.precision == "cue_frame_75" and self.offset is not None and (self.offset.as_fraction() * 75).denominator != 1:
            raise ValueError("CUE offset must preserve whole 75fps frames")
        if self.offset is not None and self.precision == "second" and self.offset.as_fraction().denominator != 1:
            raise ValueError("Second precision requires whole seconds")
        if self.offset is not None and self.precision == "minute" and (self.offset.as_fraction() / 60).denominator != 1:
            raise ValueError("Minute precision requires whole minutes")
        return self


class ProviderTrack(DomainModel):
    position: int = Field(gt=0)
    artist: Text | None = None
    title: Text | None = None
    timestamp: Timestamp | None = None
    label: Text | None = None
    remix_info: Text | None = None
    is_mashup: bool = False
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    evidence: Evidence = ()


class Completeness(DomainModel):
    state: Literal["complete", "incomplete"]
    reason: Text
    evidence: Evidence = ()


class SourceReference(DomainModel):
    """Authorized opaque agent reference; never permission to open a path."""

    identity: SourceIdentity
    channel: Literal["companion", "embedded"]
    format: Text
    display_name: Text | None = None
    revision: Annotated[str, Field(max_length=2048)] | None = None


class RecordingContext(DomainModel):
    recording_id: Identifier
    sources: Annotated[tuple[SourceReference, ...], Field(max_length=256)] = ()
    artist: Fact = Fact()
    event: Fact = Fact()
    date: Fact = Fact()
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class DiscoveryBudget(DomainModel):
    max_candidates: int = Field(default=64, gt=0, le=256)
    max_reads: int = Field(default=16, gt=0, le=256)
    max_bytes: int = Field(default=262144, gt=0, le=1048576)
    max_characters: int = Field(default=131072, gt=0, le=524288)
    max_lines: int = Field(default=4096, gt=0, le=16384)
    deadline_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)


class LoadBudget(DiscoveryBudget):
    max_tracks: int = Field(default=1024, gt=0, le=4096)


class ProviderCandidate(DomainModel):
    identity: SourceIdentity
    source: SourceReference | None = None
    observed_revision: Text | None = None
    url: Text | None = None
    display_name: Text | None = None
    evidence: Evidence = ()

    @model_validator(mode="after")
    def match_reference(self) -> Self:
        if self.source is not None and self.identity != self.source.identity:
            raise ValueError("Candidate source identity mismatch")
        return self


class OpaqueCursor(DomainModel):
    provider_id: ProviderId
    context_key: Identifier
    contract_version: tuple[int, int] = (1, 0)
    token: Annotated[str, Field(max_length=4096)]


class SourceRead(DomainModel):
    status: OutcomeStatus
    code: Text
    scope: Text
    evidence: Evidence = ()
    text: Annotated[str, Field(max_length=262144)] | None = None
    encoding: Text | None = None
    revision: Annotated[str, Field(max_length=2048)] | None = None
    revision_scope: Literal["full", "bounded", "unknown"] = "unknown"
    truncated: bool = False
    bytes_read: int = Field(default=0, ge=0, le=1048576)
    retrieved_at: AwareDatetime


class Snapshot(DomainModel):
    identity: SourceIdentity
    revision: Annotated[str, Field(max_length=2048)] | None = None
    revision_scope: Literal["full", "bounded", "unknown"] = "unknown"
    retrieved_at: AwareDatetime
    url: Text | None = None
    artist: Fact = Fact()
    event: Fact = Fact()
    date: Fact = Fact()
    tracks: Annotated[tuple[ProviderTrack, ...], Field(max_length=4096)] = ()
    release_facts: Annotated[tuple[ReleaseFact, ...], Field(max_length=256)] = ()
    text: Annotated[str, Field(max_length=262144)] | None = None
    encoding: Text | None = None
    source_format: Text
    parser_version: Annotated[str, Field(min_length=1, max_length=128)]
    completeness: Completeness
    provenance: Evidence = ()
    rights_evidence: Evidence = ()

    @model_validator(mode="after")
    def validate_tracks(self) -> Self:
        if self.date.certainty == "known" and self.date.value is not None:
            CalendarDate.fromisoformat(self.date.value)
        positions = [track.position for track in self.tracks]
        if positions != sorted(set(positions)):
            raise ValueError("Track positions must be unique and ordered")
        offsets_by_origin: dict[str, list[Fraction]] = {}
        for track in self.tracks:
            timestamp = track.timestamp
            if timestamp and timestamp.offset_usability == "qualified" and timestamp.offset and timestamp.origin:
                offsets_by_origin.setdefault(timestamp.origin, []).append(timestamp.offset.as_fraction())
        if any(offsets != sorted(offsets) for offsets in offsets_by_origin.values()):
            raise ValueError("Qualified offsets for each origin must be ordered")
        if len(self.model_dump_json().encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("Snapshot serialized payload exceeds 4MiB")
        return self

    def normalized_payload(self) -> dict[str, object]:
        """Stable complete-content comparison; acquisition time/URL are observation noise."""
        return self.model_dump(mode="json", exclude={"retrieved_at", "url"})


class LoadOutcome(DomainModel):
    status: OutcomeStatus
    code: Text
    scope: Text
    detail: Text = ""
    evidence: Evidence = ()
    snapshot: Snapshot | None = None
    alternatives: Annotated[tuple[ProviderCandidate, ...], Field(max_length=256)] = ()
    retry_after_seconds: float | None = Field(default=None, ge=0, le=3600, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_found(self) -> Self:
        if self.status == OutcomeStatus.FOUND and (self.snapshot is None or self.snapshot.completeness.state != "complete"):
            raise ValueError("Found requires a complete snapshot")
        if self.snapshot is not None and self.status not in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE, OutcomeStatus.AMBIGUOUS}:
            raise ValueError("Only found, incomplete and ambiguous outcomes can carry snapshots")
        if self.status == OutcomeStatus.INCOMPLETE and self.snapshot is not None and self.snapshot.completeness.state != "incomplete":
            raise ValueError("Incomplete outcome requires incomplete snapshot")
        if self.status == OutcomeStatus.FOUND and self.snapshot is not None and not self.snapshot.tracks and not self.snapshot.completeness.evidence:
            raise ValueError("Found trackless source requires explicit empty-track evidence")
        return self


class DiscoveryOutcome(DomainModel):
    status: OutcomeStatus
    code: Text
    scope: Text
    detail: Text = ""
    evidence: Evidence = ()
    candidates: Annotated[tuple[ProviderCandidate, ...], Field(max_length=256)] = ()
    completeness: Completeness | None = None
    cursor: OpaqueCursor | None = None

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if self.status == OutcomeStatus.FOUND and (self.completeness is None or self.completeness.state != "complete" or self.cursor is not None):
            raise ValueError("Found discovery requires demonstrated complete enumeration")
        if self.status == OutcomeStatus.INCOMPLETE and (
            self.completeness is None or self.completeness.state != "incomplete" or not self.completeness.reason
        ):
            raise ValueError("Incomplete discovery requires incompleteness reason")
        return self
