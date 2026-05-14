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

from pydantic import BaseModel, ConfigDict


class ScanBatchPatch(BaseModel):
    """Partial-update body for PATCH /scan-batches/{id}.

    Status transitions (enforced by the router, mirroring Phase 25 D-15):
    `RUNNING → COMPLETED` and `RUNNING → FAILED` are valid; same-state PATCH is
    a 200 no-op; the LIVE sentinel state is the watcher's terminal own-state and
    is intentionally NOT in this Literal — attempting `status="live"` on the
    wire yields 422 at validation time (D-10 schema-layer guard).
    """

    model_config = ConfigDict(extra="forbid")

    total_files: int | None = None
    processed_files: int | None = None
    status: Literal["running", "completed", "failed"] | None = None
    error_message: str | None = None


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
