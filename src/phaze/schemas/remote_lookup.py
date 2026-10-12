"""Operator-triggered remote lookup: commands and typed results. Provider-neutral."""

from datetime import datetime
from typing import Annotated, Literal, Self
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from phaze.tracklist_providers.domain import Completeness, DiscoveryBudget, LoadBudget, ProviderId, SourceIdentity


# What the operator is told. "no_candidates_in_scope" is the ONLY empty result, and it is reported only when
# every read succeeded: it never means "no tracklist exists", only that the searched scope held no candidate.
LookupStatus = Literal[
    "candidates", "no_candidates_in_scope", "loaded", "absent", "changed_source", "unavailable", "retry", "refused", "unsupported", "contract_error"
]


class OperatorOutcome(BaseModel):
    """One provider, registry or transport code, mapped to what the operator can do about it."""

    model_config = ConfigDict(frozen=True)

    status: LookupStatus
    code: str
    message: str
    retry_after_seconds: float | None = None
    stop_code: str | None = None


class LookupCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    media_id: uuid.UUID
    provider_id: ProviderId
    budget: DiscoveryBudget = DiscoveryBudget()


class LoadCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: uuid.UUID
    budget: LoadBudget = LoadBudget()


class LookupCandidate(BaseModel):
    """A persisted review candidate. ``evidence`` carries the assessment; eligibility is always review_required."""

    candidate_id: uuid.UUID
    observation_id: uuid.UUID
    identity: SourceIdentity
    display_name: str | None = None
    url: str | None = None
    evidence: tuple[str, ...]


class PreviouslyRejected(BaseModel):
    """A candidate discovery returned that this recording's operator rejected: not proposed again, but shown."""

    identity: SourceIdentity
    rejected_at: datetime | None = None


class LookupResult(BaseModel):
    media_id: uuid.UUID
    provider_id: str
    outcome: OperatorOutcome
    candidates: tuple[LookupCandidate, ...] = ()
    # Discovery's own statement of how much it searched; incomplete is never absence.
    scope: Completeness | None = None
    # Failed reads behind a partial result, so a candidate list never hides a refused or paced request.
    warnings: tuple[OperatorOutcome, ...] = ()
    # Candidates discovery returned that this recording's operator already rejected; listed, not re-proposed.
    previously_rejected: tuple[PreviouslyRejected, ...] = ()


class LoadResult(BaseModel):
    media_id: uuid.UUID
    candidate_id: uuid.UUID
    outcome: OperatorOutcome
    # The imported PENDING version and its review candidate. Selection is a separate explicit review step.
    observation_id: uuid.UUID | None = None
    review_candidate_id: uuid.UUID | None = None
    evidence: tuple[str, ...] = ()


class RemoteSourceDecision(BaseModel):
    """An explicit operator select/reject of a loaded remote candidate, compare-and-set like local decisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    media_id: uuid.UUID
    observation_id: uuid.UUID
    decision_id: uuid.UUID
    action: Literal["select", "reject", "reopen"] = "select"
    expected_selection_token: uuid.UUID | None = None
    expected_revision: Annotated[str, Field(max_length=2048)] | None
    expected_media_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    accept_conflict: bool = False
    # Required to reopen a rejection: why the operator wants it proposed again. Recorded with the decision.
    reason: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def require_reopen_reason(self) -> Self:
        if self.action == "reopen" and (self.reason is None or not self.reason.strip()):
            raise ValueError("Reopening a rejected candidate requires a reason")
        return self


class ClearStopCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: Annotated[str, Field(min_length=1, max_length=512)]


class ProviderStatus(BaseModel):
    provider_id: str
    display_name: str
    enabled: bool
    stopped: bool
    stop_code: str | None = None
    stopped_at: datetime | None = None
    cleared_at: datetime | None = None
    cleared_by: str | None = None
    clear_reason: str | None = None
