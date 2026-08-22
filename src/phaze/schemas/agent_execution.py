"""Pydantic schemas for /api/internal/agent/execution-log (phase-25 D-13, D-15).

Two request schemas use `extra="forbid"` (D-16) to prevent agents from sneaking
extra fields onto the wire. Response schemas remain loose so we can extend them
non-breakingly. Status fields are typed `ExecutionStatus` so Pydantic validates
the four allowed lifecycle values (`pending`, `in_progress`, `completed`, `failed`).

Per D-13 the agent supplies `id` on POST so retries can be coalesced with
`INSERT ... ON CONFLICT (id) DO NOTHING`. Per D-15 status transitions are
monotonic; the router enforces the ladder + terminal-state guard at PATCH time.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from phaze.enums.execution import ExecutionStatus
from phaze.schemas.wire_mixins import SanitizedErrorMessageMixin
from phaze.services.pg_text import contains_pg_invalid_chars


class ExecutionLogCreate(SanitizedErrorMessageMixin):
    """Agent-supplied ExecutionLog row to insert.

    Per D-13, the agent generates `id` (uuid.uuid4 on the agent) and persists
    it in SAQ job state. Server does `INSERT ... ON CONFLICT (id) DO NOTHING`
    so retries are silent no-ops. `agent_id` is NEVER part of the body --
    handlers source it from `Depends(get_authenticated_agent)` only (AUTH-01).

    `error_message` (sink: `ExecutionLog.error_message`, Text) and its sanitize-on-write
    validator come from `SanitizedErrorMessageMixin` (phaze-bk9el.14) -- see that class's
    docstring for the `CharacterNotInRepertoireError` this guards against.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    proposal_id: uuid.UUID
    operation: str = Field(min_length=1, max_length=20)
    source_path: str = Field(min_length=1)
    destination_path: str = Field(min_length=1)
    sha256_verified: bool
    status: ExecutionStatus

    # phaze-hvve5 (site 3): `source_path`/`destination_path` land straight into
    # `ExecutionLog.{source_path,destination_path}` (Text, NOT NULL) via `pg_insert(...).values(...)`
    # (routers/agent_execution.py). REJECT rather than sanitize -- these are filesystem paths, and
    # silently stripping a NUL (services.pg_text.sanitize_pg_text) would point the row at a
    # DIFFERENT path than the agent actually operated on (same rationale as the
    # pipeline_scans.py:497 subpath guard). `min_length=1` already runs first; this only rejects a
    # PG-unstorable byte within an otherwise non-empty path.
    @field_validator("source_path", "destination_path", mode="after")
    @classmethod
    def _reject_pg_unsafe_path(cls, v: str, info: ValidationInfo) -> str:
        if contains_pg_invalid_chars(v):
            raise ValueError(f"{info.field_name} must not contain a NUL byte or a lone Unicode surrogate")
        return v


class ExecutionLogPatch(SanitizedErrorMessageMixin):
    """Partial-update body for PATCH /execution-log/{id}.

    Status transitions enforced monotonic per D-15: PENDING(0) < IN_PROGRESS(1)
    < COMPLETED(2) < FAILED(3). Same-status PATCH is allowed (idempotent retry);
    backward transitions return 409 with detail `"execution-log status would
    regress"`; PATCH against a terminal row returns 409 with detail
    `"execution-log status is terminal"`.

    `error_message` and its sanitize-on-write validator come from
    `SanitizedErrorMessageMixin` (phaze-bk9el.14) -- see `ExecutionLogCreate` and that class's
    docstring; `patch_execution_log`'s `setattr` loop applies the same sink.
    """

    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus
    sha256_verified: bool | None = None

    # phaze-e2b36: `bool | None = None` is Pydantic's idiom for an OPTIONAL field (may be absent),
    # not a NULLABLE one -- but `ExecutionLog.sha256_verified` is `Boolean, nullable=False`. The
    # router's `exclude_unset=True` dump only filters OMITTED fields; an explicitly-sent JSON
    # `null` is in `model_fields_set`, passes `bool | None` validation, and the subsequent
    # `setattr(existing, "sha256_verified", None)` + commit raises an unhandled IntegrityError (NOT
    # NULL violation) -- 500 instead of a clean 422. Reject it here, at the wire boundary, rather
    # than in the router (request_guards rule 4 forbids laundering a validation bug into a
    # try/except IntegrityError). `error_message` on this same schema is correctly `| None` as
    # written -- its column IS nullable, so an explicit null there is a legitimate clear -- this
    # validator intentionally does NOT touch it. `mode="after"` + `model_fields_set` is required
    # (not a plain `field_validator`) because distinguishing "explicitly sent null" from "omitted,
    # defaulted to None" needs the fully-parsed model's set-fields bookkeeping.
    @model_validator(mode="after")
    def _reject_explicit_sha256_verified_null(self) -> "ExecutionLogPatch":
        if self.sha256_verified is None and "sha256_verified" in self.model_fields_set:
            raise ValueError("sha256_verified must not be explicitly null; omit the field to leave it unchanged")
        return self


class ExecutionLogCreateResponse(BaseModel):
    """Minimal echo response confirming the create (or replay no-op) (D-19)."""

    agent_id: str
    execution_log_id: uuid.UUID


class ExecutionLogPatchResponse(BaseModel):
    """Minimal echo response confirming the patch (D-19)."""

    agent_id: str
    execution_log_id: uuid.UUID
    status: ExecutionStatus
