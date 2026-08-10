"""Pydantic schemas for PATCH /api/internal/agent/scan-batches/{batch_id} (Phase 27 D-10).

The PATCH endpoint lets an agent's `scan_directory` SAQ task report progress and
final state for its assigned ScanBatch. The watcher's per-agent sentinel batch is
NEVER PATCH-able (its `status='live'` is terminal-by-construction), so the body
schema's `status` field is restricted to `Literal["running", "completed",
"failed"]` — `"live"` is intentionally absent from the Literal alternatives.

Both classes mirror `phaze.schemas.agent_execution.ExecutionLogPatch{,Response}`
byte-for-byte in shape (Phase 25 D-15 analog). The PATCH body class forbids
extras per the Phase 25 D-16 / Phase 26 D-22 invariant; the response class
stays loose so the server can extend the echo non-breakingly.

`agent_id` is NEVER part of the PATCH body — AUTH-01 mandates that agent_id
comes from the bearer-token resolver, never from the wire.
"""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from phaze.schemas.wire_bounds import INT32_MAX
from phaze.services.pg_text import sanitize_pg_text


class ScanBatchPatch(BaseModel):
    """Partial-update body for PATCH /scan-batches/{id}.

    Status transitions (enforced by the router, mirroring Phase 25 D-15):
    `RUNNING → COMPLETED` and `RUNNING → FAILED` are valid; same-state PATCH is
    a 200 no-op; the LIVE sentinel state is the watcher's terminal own-state and
    is intentionally NOT in this Literal — attempting `status="live"` on the
    wire yields 422 at validation time (D-10 schema-layer guard).
    """

    model_config = ConfigDict(extra="forbid")

    # -> scan_batches.total_files / processed_files Integer (int4), rule 3. A scan batch counts
    # files found on one agent's filesystem walk -- there is no established real-world cap on how
    # many files a single scan directory can hold (an arbitrary round number here would be a guess,
    # not a domain fact, per the contract's own caution against picking one), so the int4 column
    # bound is the honest fallback rather than an invented domain.
    total_files: int | None = Field(default=None, ge=0, le=INT32_MAX)
    processed_files: int | None = Field(default=None, ge=0, le=INT32_MAX)
    status: Literal["running", "completed", "failed"] | None = None
    error_message: str | None = None

    # phaze-hvve5 (site 1): this is the ONE agent PATCH endpoint that did not sanitize
    # `error_message` before the router's generic `setattr(batch, field, value)` loop writes it
    # into `ScanBatch.error_message` (Text) and commits -- unlike agent_metadata.py:162,
    # agent_analysis.py:456, agent_tag_writes.py:109. A NUL/lone-surrogate 500s the commit and
    # permanently loses the terminal FAILED report (the batch stays RUNNING with no way to
    # retry -- the identical PATCH replay 500s forever). Sanitize at the wire boundary so the
    # router's shared setattr loop needs no special-casing per field.
    @field_validator("error_message", mode="after")
    @classmethod
    def _sanitize_error_message(cls, v: str | None) -> str | None:
        return sanitize_pg_text(v) if v is not None else v

    @model_validator(mode="after")
    def _reject_explicit_null_for_non_nullable_fields(self) -> "ScanBatchPatch":
        """phaze-q6i5g: `None` here is overloaded as BOTH the unset sentinel (the field's
        default, letting `model_dump(exclude_unset=True)` build a partial-update `set_fields`
        dict) AND, when the client transmits a literal JSON null, an explicit request to null
        the field out -- `model_validate({"status": None})` validates and survives
        `exclude_unset=True` (it IS in `model_fields_set`) identically to a real value. The
        handler's guards are all keyed on `body.status is not None` (unset-vs-null), not on
        `"status" in set_fields` (unset-vs-set) -- two different predicates that only diverge on
        an explicit null, letting one skip every guard and fall into the unconditional `setattr`
        apply loop. `status`/`total_files`/`processed_files` back NOT NULL columns
        (models/scan_batch.py), so that setattr flushes an explicit `SET ... = NULL` and
        `session.commit()` raises an unhandled 500 NotNullViolation instead of this module's own
        'reject at validation time' posture (mirrors the `status="live"` 422 above). Reject an
        explicit null for those three here, at the schema layer, before it ever reaches the
        handler.

        `error_message` is deliberately excluded: it is the one nullable column
        (models/scan_batch.py) and a null is a legitimate clear-the-error-message request.
        """
        non_nullable_fields = ("status", "total_files", "processed_files")
        explicit_nulls = [f for f in non_nullable_fields if f in self.model_fields_set and getattr(self, f) is None]
        if explicit_nulls:
            raise ValueError(f"explicit null not allowed for: {', '.join(explicit_nulls)} (field is unsettable, not nullable)")
        return self


class ScanBatchPatchResponse(BaseModel):
    """Full-row echo confirming the PATCH (D-Discretion §4 — saves the agent a follow-up GET).

    Response `status` is a free-form `str` (NOT the Literal) — mirrors the
    sibling `ExecutionLogPatchResponse.status: ExecutionStatus` which is also
    less constrained than the patch body. The PATCH endpoint will never produce
    `"live"` in its response (it cannot transition INTO live), but the loose
    type lets shared response code echo any batch row without re-validation.
    """

    batch_id: uuid.UUID
    agent_id: str
    scan_path: str
    status: str
    total_files: int
    processed_files: int
    error_message: str | None = None
